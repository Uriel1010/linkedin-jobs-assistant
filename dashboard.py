"""Private, same-origin dashboard. Upstream credentials never reach the browser."""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from feed_rules import role_of, level_of, parse_time
from maintenance import backup_database
from dashboard_settings import router as settings_router

ROOT = Path(__file__).parent
STATE = Path(os.environ.get('DASHBOARD_STATE_DIR', '/app/state'))
DEMO = os.environ.get('DASHBOARD_DEMO') == '1'
SECURE = os.environ.get('DASHBOARD_SECURE_COOKIE') == '1'
UPSTREAM = os.environ.get('JOBS_API_URL', 'http://api:8765')
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(settings_router)
cache = {}
lock = threading.Lock()
attempts = {}


@contextmanager
def db():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(STATE / 'dashboard.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript('''
        CREATE TABLE IF NOT EXISTS job_state(job_id TEXT PRIMARY KEY,dismissed INTEGER NOT NULL DEFAULT 0,seen INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL);
    ''')
    try:
        with con:
            yield con
    finally:
        con.close()


def access_key():
    path = STATE / 'access-key'
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with path.open('x', encoding='utf-8') as file:
            os.chmod(path, 0o600)
            file.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text().strip()


@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith('/api/') and path not in ('/api/login', '/api/session'):
        token = request.cookies.get('jobs_session', '')
        with db() as con:
            authenticated = con.execute('SELECT 1 FROM sessions WHERE token_hash=? AND expires>?',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not DEMO and not authenticated:
            return JSONResponse({'detail':'Sign in to continue.'}, status_code=401)
    if path.startswith('/api/') and request.method not in ('GET', 'HEAD'):
        # Custom header plus same-origin checks prevent form-based CSRF.
        origin = request.headers.get('origin')
        if request.headers.get('x-dashboard-request') != '1' or (origin and origin != str(request.base_url).rstrip('/')):
            return JSONResponse({'detail':'Use the dashboard to make this request.'}, status_code=403)
    response = await call_next(request)
    response.headers.update({
        'Cache-Control':'no-store',
        'X-Content-Type-Options':'nosniff',
        'Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    })
    return response


class Login(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    ip = request.client.host
    recent = [stamp for stamp in attempts.get(ip, []) if stamp > time.time()-60]
    if len(recent) >= 5:
        raise HTTPException(429, 'Too many attempts. Wait a minute and try again.')
    attempts[ip] = recent + [time.time()]
    if not hmac.compare_digest(body.key, access_key()):
        raise HTTPException(401, 'That access key is not correct.')
    attempts.pop(ip, None)
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        con.execute('INSERT INTO sessions VALUES (?,?)',
            (hashlib.sha256(token.encode()).hexdigest(), time.time()+604800))
    response.set_cookie('jobs_session', token, httponly=True, samesite='strict', secure=SECURE, max_age=604800)
    return {'ok':True}


@app.get('/api/session')
def session(request: Request):
    access_key()
    token = request.cookies.get('jobs_session', '')
    with db() as con:
        valid = con.execute('SELECT 1 FROM sessions WHERE token_hash=? AND expires>?',
            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return {'authenticated': DEMO or bool(valid), 'demo':DEMO}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with db() as con:
        con.execute('DELETE FROM sessions WHERE token_hash=?',
            (hashlib.sha256(request.cookies.get('jobs_session','').encode()).hexdigest(),))
    response.delete_cookie('jobs_session')
    return {'ok':True}


def upstream(method, path, payload=None):
    if DEMO:
        from dashboard_demo import handle
        return handle(method, path, payload)
    key = Path(os.environ.get('API_KEY_FILE', '/run/jobs-api-key')).read_text().strip()
    req = urllib.request.Request(UPSTREAM+path, method=method,
        headers={'X-API-Key':key, 'Content-Type':'application/json'},
        data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = {404:'This item no longer exists.',409:'A collection is running, cooling down, or these settings changed. Refresh and try again.',
                  423:'LinkedIn automation is disabled. Use public employer sources.',
                  429:'Collection is cooling down. Try again later.'}.get(exc.code,'The jobs service could not complete this request.')
        raise HTTPException(exc.code if exc.code in (404,409,422,423,429) else 502, detail) from None
    except (OSError,ValueError):
        raise HTTPException(503, 'The jobs service is unavailable. Your saved work is safe; try again shortly.') from None


def all_items(path):
    result = []
    for offset in range(0, 1000000, 200):
        page = upstream('GET', f'{path}?limit=200&offset={offset}')
        result.extend(page['items'])
        if len(result) >= page['total'] or not page['items']:
            return result
    raise HTTPException(503, 'The catalog is too large to load.')


def catalog():
    with lock:
        if time.time()-cache.get('at',0) > 30:
            jobs = all_items('/catalog')
            cache.update(jobs=jobs, at=time.time())
        return cache['jobs']


def filtered(params):
    jobs = catalog()
    applications = {str(a['job_id']):a for a in all_items('/applications')}
    with db() as con:
        states = {row['job_id']:dict(row) for row in con.execute('SELECT * FROM job_state')}
    enriched = []
    counts = {'inbox':0,'saved':0,'dismissed':0,'unread':0}
    for job in jobs:
        item = dict(job)
        item.update(states.get(str(job['id']), {'dismissed':0,'seen':0}))
        item['application'] = applications.get(str(job['id']))
        item['role'] = role_of(job.get('title') or '')
        item['level'] = level_of(job) or 'unknown'
        active_saved = item['application'] and item['application']['status'] not in ('archived','withdrawn','rejected')
        counts['dismissed' if item['dismissed'] else 'inbox'] += bool(item['dismissed'] or job.get('is_active',1))
        counts['saved'] += bool(active_saved)
        counts['unread'] += not item['seen'] and not item['dismissed'] and job.get('is_active',1)
        view = params.get('view','inbox')
        if view=='inbox' and job.get('is_active')==0:
            continue
        if view=='inbox' and item['dismissed'] or view=='dismissed' and not item['dismissed'] or view=='saved' and not active_saved:
            continue
        q = params.get('q','').casefold().strip()
        if q and q not in ' '.join(str(job.get(k) or '') for k in ('title','company','location','description')).casefold():
            continue
        role = params.get('role','all')
        if role != 'all' and item['role'] != role and not (role=='software' and item['role'] in ('backend','frontend','fullstack')):
            continue
        if params.get('level','all') not in ('all',item['level']):
            continue
        if params.get('unread')=='1' and item['seen']:
            continue
        mode = params.get('mode','all')
        if mode != 'all' and mode.casefold() not in (str(job.get('work_arrangement') or '')+' '+str(job.get('location') or '')).casefold():
            continue
        if params.get('days') in ('1','7','30'):
            discovered = parse_time(job.get('first_seen'))
            if not discovered or (datetime.now(timezone.utc)-discovered).total_seconds()>int(params['days'])*86400:
                continue
        enriched.append(item)
    sort = params.get('sort','newest')
    enriched.sort(key=lambda j: (j.get('title') or '').casefold() if sort=='title' else j.get('first_seen') or '',reverse=sort=='newest')
    return enriched, counts


@app.get('/api/jobs')
def jobs(request: Request, offset: int = 0):
    items,counts = filtered(request.query_params)
    offset = max(0,offset)
    return {'items':[{k:v for k,v in item.items() if k!='description'} for item in items[offset:offset+24]],
            'total':len(items), 'counts':counts, 'offset':offset, 'limit':24}


@app.get('/api/export')
def export(request: Request):
    items,_ = filtered(request.query_params)
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['Title','Company','Location','URL','Discovered','Stage'])
    for job in items:
        values=[job.get('title'),job.get('company'),job.get('location'),job.get('url'),job.get('first_seen'),(job.get('application') or {}).get('status')]
        writer.writerow([("'"+str(v)) if str(v or '').lstrip().startswith(('=','+','-','@')) else str(v or '') for v in values])
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="jobs.csv"'})


@app.get('/api/jobs/{job_id}')
def detail(job_id: str):
    if not job_id.isdigit():
        raise HTTPException(422,'Invalid job ID.')
    return upstream('GET',f'/jobs/{job_id}')


class JobState(BaseModel):
    ids: list[str] = Field(min_length=1,max_length=100)
    dismissed: bool | None = None
    seen: bool | None = None


@app.post('/api/job-state')
def update_state(body: JobState):
    if any(not re_id(job_id) for job_id in body.ids) or body.dismissed is None and body.seen is None:
        raise HTTPException(422,'Choose jobs and a valid action.')
    known={str(job['id']) for job in catalog()}
    if not set(body.ids)<=known:
        raise HTTPException(404,'A selected job no longer exists.')
    with db() as con:
        for job_id in body.ids:
            con.execute('INSERT OR IGNORE INTO job_state(job_id) VALUES (?)',(job_id,))
            for key in ('dismissed','seen'):
                value=getattr(body,key)
                if value is not None:
                    con.execute(f'UPDATE job_state SET {key}=? WHERE job_id=?',(int(value),job_id))
    return {'updated':len(body.ids)}


def re_id(value):
    return value.isascii() and value.isdigit() and len(value)<=30


@app.get('/api/overview')
def overview():
    return upstream('GET','/overview')


@app.get('/api/applications')
def applications():
    return {'items':all_items('/applications')}


class Application(BaseModel):
    job_id: str | None = Field(default=None,pattern=r'^\d{1,30}$')
    status: Literal['saved','reviewing','applied','interviewing','offer','rejected','withdrawn','archived'] = 'saved'
    notes: str = Field(default='',max_length=20000)
    follow_up_at: str | None = None
    cv_version: str | None = Field(default=None,max_length=300)


@app.post('/api/applications')
def save_application(body: Application):
    if not body.job_id:
        raise HTTPException(422,'Choose a job.')
    return upstream('POST','/applications',body.model_dump(exclude_unset=True))


@app.patch('/api/applications/{application_id}')
def edit_application(application_id: int,body: Application):
    return upstream('PATCH',f'/applications/{application_id}',body.model_dump(exclude_unset=True,exclude={'job_id'}))


@app.get('/api/searches')
def searches():
    return upstream('GET','/searches')


class Search(BaseModel):
    keywords: str = Field(min_length=1,max_length=200)
    location: str = Field(default='',max_length=200)


@app.post('/api/searches')
def add_search(body: Search):
    return upstream('POST','/searches',body.model_dump())


class Enabled(BaseModel):
    enabled: bool


@app.get('/api/sources')
def public_sources():
    return upstream('GET','/sources')


class PublicSource(BaseModel):
    provider: Literal['greenhouse','lever','lever_eu','ashby']
    board: str = Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    company: str = Field(min_length=1,max_length=120)
    location_filter: str = Field(default='Israel',max_length=100)
    interval_minutes: int = Field(default=120,ge=60,le=10080)
    daily_budget: int = Field(default=48,ge=1,le=200)
    max_requests: int = Field(default=10,ge=1,le=20)


@app.post('/api/sources')
def add_public_source(body: PublicSource):
    return upstream('POST','/sources',body.model_dump())


class PublicSourceUpdate(BaseModel):
    revision: int
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None,ge=60,le=10080)
    daily_budget: int | None = Field(default=None,ge=1,le=200)
    max_requests: int | None = Field(default=None,ge=1,le=20)


@app.patch('/api/sources/{source_id}')
def edit_public_source(source_id: int,body: PublicSourceUpdate):
    return upstream('PATCH',f'/sources/{source_id}',body.model_dump(exclude_unset=True))


@app.post('/api/sources/{source_id}/poll')
def queue_public_source(source_id: int):
    return upstream('POST',f'/sources/{source_id}/poll')


@app.post('/api/sources/{source_id}/resume')
def resume_public_source(source_id: int):
    return upstream('POST',f'/sources/{source_id}/resume')


@app.get('/api/sources/{source_id}/runs')
def public_source_runs(source_id: int):
    return upstream('GET',f'/sources/{source_id}/runs')


@app.patch('/api/searches/{search_id}')
def toggle_search(search_id: int,body: Enabled):
    return upstream('PATCH',f'/searches/{search_id}',body.model_dump())


@app.post('/api/searches/{search_id}/poll')
def poll(search_id: int):
    return upstream('POST',f'/searches/{search_id}/poll')


@app.get('/api/alerts')
def alerts():
    return upstream('GET','/alerts?limit=100')


@app.post('/api/alerts/{alert_id}/acknowledge')
def acknowledge(alert_id: int):
    return upstream('POST',f'/alerts/{alert_id}/acknowledge')


@app.get('/health')
def health():
    with db() as con:
        con.execute('SELECT 1').fetchone()
    access_key()
    backup_database(STATE/'dashboard.sqlite3',STATE/'backups','dashboard')
    return {'status':'ok'}


app.mount('/assets',StaticFiles(directory=ROOT/'dashboard_static'),name='assets')


@app.get('/')
def index():
    return FileResponse(ROOT/'dashboard_static/index.html')
