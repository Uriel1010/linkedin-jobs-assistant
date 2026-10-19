import asyncio
import contextlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, AwareDatetime

import storage
from collector import CollectionError, collect, fetch_details
from maintenance import backup_database

lock = asyncio.Lock()
last_network_attempt = 0.0


def later(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def retry_delay(search):
    return max(search['interval_minutes'], min(360, 30 * 2 ** min(search['failures'], 4)))


def network_guard():
    session = storage.DATA / 'session.json'
    if not session.exists():
        raise HTTPException(409, 'Connect LinkedIn first')
    if (storage.DATA / 'connecting.lock').exists():
        raise HTTPException(409, 'LinkedIn connection is in progress')
    with storage.db() as con:
        state = dict(con.execute('SELECT * FROM network_state WHERE id=1').fetchone())
        if state['blocked_session'] is not None and session.stat().st_mtime <= state['blocked_session']:
            raise HTTPException(409, 'Reconnect LinkedIn: ' + (state['reason'] or 'session blocked'))
        if time.time() < state['retry_after']:
            raise HTTPException(409, 'LinkedIn rate limit cooldown is active')
        if time.time() - state['last_attempt'] < 60:
            raise HTTPException(409, 'Wait at least 60 seconds between collection batches')
        con.execute('UPDATE network_state SET last_attempt=?,blocked_session=NULL,reason=NULL WHERE id=1', (time.time(),))


def network_failure(reason):
    with storage.db() as con:
        if reason in ('reconnect_required', 'access_denied', 'session_required', 'invalid_session'):
            path = storage.DATA / 'session.json'
            con.execute('UPDATE network_state SET blocked_session=?,reason=? WHERE id=1',
                        (path.stat().st_mtime if path.exists() else time.time(), reason))
        elif reason == 'rate_limited':
            con.execute('UPDATE network_state SET retry_after=? WHERE id=1', (time.time() + 3600,))
    storage.alert('linkedin_connection', reason)


def get_search(search_id):
    with storage.db() as con:
        row = con.execute('SELECT * FROM searches WHERE id=?', (search_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Search not found')
    return dict(row)


def due(search):
    if not search['enabled']:
        return False
    if not search['last_attempt']:
        return True
    if search.get('next_due'):
        return datetime.now(timezone.utc) >= datetime.fromisoformat(search['next_due'])
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(search['last_attempt'])).total_seconds()
    return elapsed >= search['interval_minutes'] * 60


async def poll(search_id):
    global last_network_attempt
    if lock.locked():
        raise HTTPException(409, 'Another collection is running')
    async with lock:
        search = get_search(search_id)
        if not due(search):
            raise HTTPException(409, 'Search disabled or polling interval has not elapsed')
        network_guard()
        last_network_attempt = asyncio.get_running_loop().time()
        started_at = storage.now()
        with storage.db() as con:
            con.execute("UPDATE searches SET last_attempt=?,status='running',error=NULL WHERE id=?",
                        (started_at, search_id))
            run_id = con.execute("INSERT INTO runs(search_id,started_at,status) VALUES (?,?,'running')",
                                 (search_id, storage.now())).lastrowid
        try:
            jobs = await collect(search)
            added = storage.save_jobs(search_id, jobs)
        except Exception as exc:
            # Never send browser exceptions, cookies, or page contents to API callers/logs.
            reason = str(exc) if isinstance(exc, CollectionError) else 'collection_failed'
            delay = retry_delay(search)
            with storage.db() as con:
                con.execute("UPDATE searches SET status='error',error=?,failures=failures+1,next_due=? WHERE id=?",
                            (reason, later(delay), search_id))
                con.execute("UPDATE runs SET status='error',error=?,finished_at=? WHERE id=?", (reason, storage.now(), run_id))
            network_failure(reason)
            storage.alert(f'search:{search_id}', reason)
            raise HTTPException(502, reason) from None
        partial = bool(getattr(jobs, 'truncated', False))
        error = getattr(jobs, 'error', None)
        pages = getattr(jobs, 'pages', 1)
        failed_partway = error and error not in ('short_page_coverage_uncertain', 'pagination_not_advancing')
        failures = search['failures'] + 1 if failed_partway else 0
        delay = retry_delay(search) if failed_partway else search['interval_minutes']
        next_due = later(delay) if failed_partway else (datetime.fromisoformat(started_at)+timedelta(minutes=search['interval_minutes'])).isoformat()
        with storage.db() as con:
            con.execute('UPDATE searches SET last_success=?,status=?,error=?,failures=?,next_due=? WHERE id=?',
                        (storage.now(), 'partial' if partial else 'ok', error, failures, next_due, search_id))
            con.execute('UPDATE runs SET status=?,found=?,added=?,pages=?,truncated=?,error=?,finished_at=? WHERE id=?',
                        ('partial' if partial else 'ok', len(jobs), added, pages, partial, error, storage.now(), run_id))
        if partial:
            storage.alert(f'search:{search_id}', error or 'Page limit reached; additional jobs may exist')
            if error in ('reconnect_required', 'access_denied', 'rate_limited'):
                network_failure(error)
        else:
            storage.resolve_alert(f'search:{search_id}')
        if not error or error in ('short_page_coverage_uncertain','pagination_not_advancing'):
            storage.resolve_alert('linkedin_connection')
        if search.get('last_success') and (datetime.now(timezone.utc)-datetime.fromisoformat(search['last_success'])).total_seconds() > 7*86400:
            storage.alert(f'gap:{search_id}', 'Collection gap exceeded the seven-day catch-up window; older postings may be missing')
        return {'run_id':run_id, 'search_id': search_id, 'found': len(jobs), 'new': added,
                'pages':pages, 'coverage_limited':partial, 'warning':error}


async def enrich(job_ids):
    if lock.locked():
        raise HTTPException(409, 'Another collection is running')
    async with lock:
        network_guard()
        with storage.db() as con:
            con.executemany("UPDATE jobs SET details_attempt=?,details_status='running' WHERE id=?",
                            [(storage.now(), job_id) for job_id in job_ids])
        try:
            results = await fetch_details(job_ids)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, CollectionError) else 'details_collection_failed'
            results = {job_id:{'error':reason} for job_id in job_ids}
        for job_id in job_ids:
            result = results.get(job_id, {'error':'batch_interrupted'})
            if result.get('error'):
                reason = result['error']
                with storage.db() as con:
                    con.execute("UPDATE jobs SET details_status='error',details_error=? WHERE id=?", (reason, job_id))
                if reason in ('reconnect_required', 'access_denied', 'rate_limited', 'invalid_session'):
                    network_failure(reason)
                storage.alert(f'details:{job_id}', reason)
            else:
                storage.save_details(job_id, result)
                storage.resolve_alert(f'details:{job_id}')
        if results and all(not result.get('error') for result in results.values()):
            storage.resolve_alert('linkedin_connection')
        return {'updated':sum(not r.get('error') for r in results.values()),
                'errors':{job_id:r['error'] for job_id,r in results.items() if r.get('error')}}


async def worker():
    last_backup_check = 0
    while True:
        await asyncio.sleep(30)
        app.state.worker_last_tick = time.time()
        if time.time()-last_backup_check>3600:
            try:
                await asyncio.to_thread(backup_database,storage.DATA/'jobs.sqlite3',storage.DATA/'backups','jobs')
                storage.resolve_alert('backup')
                last_backup_check = time.time()
            except Exception:
                storage.alert('backup','Daily database backup failed; check server disk space and permissions')
        if not (storage.DATA / 'session.json').exists() or (storage.DATA / 'connecting.lock').exists():
            continue
        try:
            with storage.db() as con:
                searches = [dict(r) for r in con.execute('SELECT * FROM searches WHERE enabled=1 ORDER BY COALESCE(next_due, \'\')')]
            # A due search always wins over optional description enrichment.
            with storage.db() as con:
                pending = [r['id'] for r in con.execute("""SELECT id FROM jobs WHERE details_status IN ('pending','error')
                  AND (details_attempt IS NULL OR details_attempt<?)
                  AND EXISTS(SELECT 1 FROM events e JOIN searches s ON e.search_id=s.id WHERE e.job_id=jobs.id AND s.enabled=1)
                  ORDER BY COALESCE(details_attempt,''),first_seen LIMIT 5""", (later(-360),))]
            ready = next((s for s in searches if due(s)), None)
            if ready:
                await poll(ready['id'])
            elif pending:
                await enrich(pending)
            storage.resolve_alert('worker')
        except HTTPException:
            pass
        except Exception:
            storage.alert('worker', 'Background collection encountered an internal error; check service health')


@asynccontextmanager
async def lifespan(app):
    storage.init()
    with storage.db() as con:
        con.execute("UPDATE runs SET status='interrupted',finished_at=?,error='service_restarted' WHERE status='running'", (storage.now(),))
        con.execute("UPDATE searches SET status='error',error='service_restarted',next_due=? WHERE status='running'", (later(1),))
        con.execute("UPDATE jobs SET details_status='pending' WHERE details_status='running'")
    path = storage.DATA / 'api-key'
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    path.chmod(0o600)
    app.state.api_key = path.read_text().strip()
    task = asyncio.create_task(worker())
    app.state.worker = task
    app.state.worker_last_tick = time.time()
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def authorize(key: Annotated[str | None, Depends(APIKeyHeader(name='X-API-Key', auto_error=False))]):
    if not key or not hmac.compare_digest(key, app.state.api_key):
        raise HTTPException(401, 'Invalid API key')


app = FastAPI(title='LinkedIn Jobs API', version='2.0.0', lifespan=lifespan,
              description='Personal LinkedIn session collector. Configure searches after connecting. Browser collection is experimental; live login is required.')
protected = [Depends(authorize)]


class Search(BaseModel):
    keywords: str = Field(min_length=1, max_length=200, pattern=r'\S')
    location: str = Field(default='', max_length=200)
    remote: bool = False
    interval_minutes: int = Field(default=60, ge=30, le=10080)
    max_pages: int = Field(default=8, ge=1, le=12)
    lookback_hours: int = Field(default=48, ge=24, le=168)


class SearchUpdate(BaseModel):
    enabled: bool | None = None
    keywords: str | None = Field(default=None, min_length=1, max_length=200, pattern=r'\S')
    location: str | None = Field(default=None, max_length=200)
    remote: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=30, le=10080)
    max_pages: int | None = Field(default=None, ge=1, le=12)
    lookback_hours: int | None = Field(default=None, ge=24, le=168)


class SearchBatch(BaseModel):
    searches: list[Search] = Field(min_length=1, max_length=20)


@app.get('/health')
def health():
    task = getattr(app.state,'worker',None)
    if task is not None and task.done():
        raise HTTPException(503,'Background collector stopped')
    if time.time()-getattr(app.state,'worker_last_tick',time.time())>900:
        raise HTTPException(503,'Background collector is not responding')
    return {'status': 'ok'}


@app.get('/status', dependencies=protected)
def status():
    with storage.db() as con:
        count = con.execute('SELECT count(*) FROM searches WHERE enabled=1').fetchone()[0]
        alerts = con.execute('SELECT count(*) FROM alerts WHERE resolved_at IS NULL AND acknowledged_at IS NULL').fetchone()[0]
        pending = con.execute("SELECT count(*) FROM jobs WHERE details_status IN ('pending','running')").fetchone()[0]
        failed = con.execute("SELECT count(*) FROM jobs WHERE details_status='error'").fetchone()[0]
        state = dict(con.execute('SELECT * FROM network_state WHERE id=1').fetchone())
    return {'session_saved': (storage.DATA / 'session.json').exists(),
            'session_note': 'A saved session is not proof of an active LinkedIn login; see search status after polling.',
            'enabled_searches': count, 'collection_running': lock.locked(),
            'connecting': (storage.DATA / 'connecting.lock').exists(),
            'open_alerts':alerts, 'details_pending_or_failed':pending+failed,
            'descriptions_pending':pending,'descriptions_failed':failed,'network':state}


@app.post('/searches', dependencies=protected, status_code=201)
def create_search(search: Search):
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT count(*) FROM searches').fetchone()[0] >= 20:
            raise HTTPException(409, 'Maximum 20 searches; update or remove an existing search')
        cur = con.execute('INSERT INTO searches(keywords,location,remote,interval_minutes,max_pages,lookback_hours) VALUES (?,?,?,?,?,?)',
                          (search.keywords.strip(), search.location.strip(), search.remote, search.interval_minutes,search.max_pages,search.lookback_hours))
        search_id = cur.lastrowid
    return get_search(search_id)


@app.post('/searches/bulk', dependencies=protected)
def create_search_batch(batch: SearchBatch):
    def identity(keywords,location,remote):
        return (' '.join(keywords.split()).casefold(),' '.join(location.split()).casefold(),bool(remote))
    with storage.db() as con:
        # Serialize creation and preflight the full batch before any INSERT.
        con.execute('BEGIN IMMEDIATE')
        existing={}
        for row in con.execute('SELECT * FROM searches ORDER BY enabled DESC,id'):
            existing.setdefault(identity(row['keywords'],row['location'],row['remote']),dict(row))
        requested={}
        for search in batch.searches:
            requested.setdefault(identity(search.keywords,search.location,search.remote),search)
        new_count=sum(key not in existing for key in requested)
        if con.execute('SELECT count(*) FROM searches').fetchone()[0]+new_count>20:
            raise HTTPException(409,'Maximum 20 searches; nothing added. Update or remove an existing search first.')
        items=[]
        for key,search in requested.items():
            created=key not in existing
            if created:
                cur=con.execute('INSERT INTO searches(keywords,location,remote,interval_minutes,max_pages,lookback_hours) VALUES (?,?,?,?,?,?)',
                    (search.keywords.strip(),search.location.strip(),search.remote,search.interval_minutes,search.max_pages,search.lookback_hours))
                row=dict(con.execute('SELECT * FROM searches WHERE id=?',(cur.lastrowid,)).fetchone())
            else:
                row=existing[key]
            items.append({**row,'created':created})
    return {'items':items,'created':new_count,'existing':len(items)-new_count}


@app.get('/searches', dependencies=protected)
def list_searches():
    with storage.db() as con:
        return [dict(r) for r in con.execute('SELECT * FROM searches ORDER BY id')]


@app.patch('/searches/{search_id}', dependencies=protected)
async def update_search(search_id: int, update: SearchUpdate):
    previous=get_search(search_id)
    fields = update.model_dump(exclude_unset=True)
    if not fields or any(v is None for v in fields.values()):
        raise HTTPException(422, 'Supply at least one non-null search field')
    for key in ('keywords','location'):
        if key in fields:
            fields[key] = fields[key].strip()
    changed_criteria=bool({'keywords','location','remote'} & fields.keys())
    if changed_criteria:
        fields['next_due']=storage.now()
    elif 'interval_minutes' in fields or not previous.get('next_due'):
        base=datetime.fromisoformat(previous['last_attempt']) if previous.get('last_attempt') else datetime.now(timezone.utc)
        fields['next_due']=(base+timedelta(minutes=fields.get('interval_minutes',previous['interval_minutes']))).isoformat() if previous.get('last_attempt') else storage.now()
    else:
        fields['next_due']=previous['next_due']
    async with lock:
        with storage.db() as con:
            assignments = ','.join(f'{key}=?' for key in fields)
            reset = ',last_success=NULL,last_attempt=NULL,failures=0' if changed_criteria else ''
            con.execute(f'UPDATE searches SET {assignments}{reset} WHERE id=?', list(fields.values())+[search_id])
    return get_search(search_id)


@app.delete('/searches/{search_id}', dependencies=protected)
async def delete_search(search_id: int):
    async with lock:
        get_search(search_id)
        with storage.db() as con:
            con.execute('DELETE FROM events WHERE search_id=?', (search_id,))
            con.execute('DELETE FROM searches WHERE id=?', (search_id,))
        storage.resolve_alert(f'search:{search_id}')
        storage.resolve_alert(f'gap:{search_id}')
    return {'deleted': search_id}


@app.post('/searches/{search_id}/poll', dependencies=protected)
async def poll_search(search_id: int):
    if (storage.DATA / 'connecting.lock').exists():
        raise HTTPException(409, 'LinkedIn connection is in progress')
    return await poll(search_id)


@app.get('/jobs', dependencies=protected)
def jobs(after_id: int = Query(0, ge=0), search_id: int | None = None,
         limit: int = Query(100, ge=1, le=500)):
    sql = '''SELECT e.id AS event_id,e.search_id,e.first_seen AS matched_at,j.*
             FROM events e JOIN jobs j ON e.job_id=j.id WHERE e.id>?'''
    args = [after_id]
    if search_id is not None:
        sql += ' AND e.search_id=?'
        args.append(search_id)
    sql += ' ORDER BY e.id LIMIT ?'
    args.append(limit)
    with storage.db() as con:
        items = [dict(r) for r in con.execute(sql, args)]
    return {'items': items, 'next_after_id': items[-1]['event_id'] if items else after_id,
            'note': 'Cursor tracks newly discovered matches, not the original posting time. A job can match multiple searches.'}


def get_job(job_id):
    with storage.db() as con:
        row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Job not found')
    return dict(row)


@app.get('/catalog', dependencies=protected)
def catalog(q: str = Query('', max_length=200), offset: int = Query(0, ge=0),
            limit: int = Query(50, ge=1, le=200)):
    with storage.db() as con:
        where = ' WHERE title LIKE ? OR company LIKE ? OR description LIKE ?'
        terms = ['%'+q+'%'] * 3
        count = con.execute('SELECT count(*) FROM jobs'+where, terms).fetchone()[0]
        items = [dict(r) for r in con.execute('SELECT * FROM jobs'+where+' ORDER BY first_seen DESC,id LIMIT ? OFFSET ?', terms+[limit,offset])]
    return {'items':items, 'total':count, 'offset':offset}


@app.get('/jobs/{job_id}', dependencies=protected)
def job_detail(job_id: str):
    return get_job(job_id)


@app.post('/jobs/{job_id}/refresh', dependencies=protected)
async def refresh_detail(job_id: str):
    get_job(job_id)
    result = await enrich([job_id])
    if result['errors']:
        raise HTTPException(502, result['errors'])
    return get_job(job_id)


@app.get('/runs', dependencies=protected)
def runs(search_id: int | None = None, before_id: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200)):
    where, args = ['1=1'], []
    if search_id is not None:
        where.append('search_id=?'); args.append(search_id)
    if before_id is not None:
        where.append('id<?'); args.append(before_id)
    with storage.db() as con:
        items = [dict(r) for r in con.execute('SELECT * FROM runs WHERE '+' AND '.join(where)+' ORDER BY id DESC LIMIT ?', args+[limit])]
    return {'items':items, 'next_before_id':items[-1]['id'] if items else None}


@app.get('/alerts', dependencies=protected)
def alerts(include_resolved: bool = False, limit: int = Query(100, ge=1, le=500)):
    with storage.db() as con:
        return [dict(r) for r in con.execute('SELECT * FROM alerts'+('' if include_resolved else ' WHERE resolved_at IS NULL')+' ORDER BY id DESC LIMIT ?', (limit,))]


@app.post('/alerts/{alert_id}/acknowledge', dependencies=protected)
def acknowledge(alert_id: int):
    with storage.db() as con:
        if not con.execute('SELECT id FROM alerts WHERE id=?', (alert_id,)).fetchone():
            raise HTTPException(404, 'Alert not found')
        con.execute('UPDATE alerts SET acknowledged_at=? WHERE id=?', (storage.now(),alert_id))
    return {'acknowledged':alert_id}


ApplicationStatus = Literal['saved','reviewing','applied','interviewing','offer','rejected','withdrawn','archived']


class ApplicationCreate(BaseModel):
    job_id: str = Field(pattern=r'^\d+$', max_length=30)
    status: ApplicationStatus = 'saved'
    notes: str = Field(default='', max_length=20000)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=320)
    cv_version: str | None = Field(default=None, max_length=300)
    applied_at: AwareDatetime | None = None
    follow_up_at: AwareDatetime | None = None


class ApplicationUpdate(BaseModel):
    status: ApplicationStatus | None = None
    notes: str | None = Field(default=None, max_length=20000)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=320)
    cv_version: str | None = Field(default=None, max_length=300)
    applied_at: AwareDatetime | None = None
    follow_up_at: AwareDatetime | None = None


def application_row(application_id):
    with storage.db() as con:
        row = con.execute('''SELECT a.*,j.title,j.company,j.url FROM applications a JOIN jobs j ON a.job_id=j.id WHERE a.id=?''', (application_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Application not found')
        result = dict(row)
        result['history'] = [dict(r) for r in con.execute('SELECT * FROM application_history WHERE application_id=? ORDER BY id', (application_id,))]
    return result


def serialized_fields(model):
    fields = model.model_dump(exclude_unset=True)
    for key in ('applied_at','follow_up_at'):
        if isinstance(fields.get(key), datetime):
            fields[key] = fields[key].astimezone(timezone.utc).isoformat()
    return fields


@app.post('/applications', dependencies=protected, status_code=201)
def create_application(body: ApplicationCreate):
    get_job(body.job_id)
    fields = body.model_dump()
    for key in ('applied_at','follow_up_at'):
        if fields[key] is not None:
            fields[key] = fields[key].astimezone(timezone.utc).isoformat()
    if body.status == 'applied' and fields['applied_at'] is None:
        fields['applied_at'] = storage.now()
    fields.update(created_at=storage.now(), updated_at=storage.now())
    with storage.db() as con:
        if con.execute('SELECT id FROM applications WHERE job_id=?', (body.job_id,)).fetchone():
            raise HTTPException(409, 'An application already exists for this job')
        keys = ','.join(fields)
        application_id = con.execute(f"INSERT INTO applications({keys}) VALUES ({','.join('?' for _ in fields)})", list(fields.values())).lastrowid
        con.execute('INSERT INTO application_history(application_id,to_status,changed_at) VALUES (?,?,?)', (application_id,body.status,storage.now()))
    return application_row(application_id)


@app.patch('/applications/{application_id}', dependencies=protected)
def update_application(application_id: int, body: ApplicationUpdate):
    fields = serialized_fields(body)
    if not fields or ('status' in fields and fields['status'] is None) or ('notes' in fields and fields['notes'] is None):
        raise HTTPException(422, 'Supply changes; status and notes cannot be null')
    with storage.db() as con:
        previous = con.execute('SELECT * FROM applications WHERE id=?', (application_id,)).fetchone()
        if previous is None:
            raise HTTPException(404, 'Application not found')
        changed_status = fields.get('status', previous['status'])
        if changed_status == 'applied' and not previous['applied_at'] and 'applied_at' not in fields:
            fields['applied_at'] = storage.now()
        fields['updated_at'] = storage.now()
        assignments = ','.join(f'{key}=?' for key in fields)
        con.execute(f'UPDATE applications SET {assignments} WHERE id=?', list(fields.values())+[application_id])
        if changed_status != previous['status']:
            con.execute('INSERT INTO application_history(application_id,from_status,to_status,changed_at) VALUES (?,?,?,?)',
                        (application_id,previous['status'],changed_status,storage.now()))
    return application_row(application_id)


@app.get('/applications', dependencies=protected)
def applications(status: ApplicationStatus | None = None, due_only: bool = False,
                 offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    where, args = ['1=1'], []
    if status is not None:
        where.append('a.status=?'); args.append(status)
    if due_only:
        where.append("a.follow_up_at<=? AND a.status NOT IN ('rejected','withdrawn','archived')")
        args.append(storage.now())
    sql = ' FROM applications a JOIN jobs j ON a.job_id=j.id WHERE '+' AND '.join(where)
    with storage.db() as con:
        total = con.execute('SELECT count(*)'+sql, args).fetchone()[0]
        items = [dict(r) for r in con.execute('SELECT a.*,j.title,j.company,j.url'+sql+' ORDER BY a.updated_at DESC,a.id DESC LIMIT ? OFFSET ?',args+[limit,offset])]
    return {'items':items, 'total':total, 'offset':offset}


@app.get('/applications/{application_id}', dependencies=protected)
def application_detail(application_id: int):
    return application_row(application_id)


@app.get('/overview', dependencies=protected)
def overview():
    with storage.db() as con:
        counts = {r['status']:r['n'] for r in con.execute('SELECT status,count(*) AS n FROM applications GROUP BY status')}
        total = con.execute('SELECT count(*) FROM jobs').fetchone()[0]
        due_count = con.execute("SELECT count(*) FROM applications WHERE follow_up_at<=? AND status NOT IN ('rejected','withdrawn','archived')", (storage.now(),)).fetchone()[0]
    return {'jobs':total, 'applications_by_status':counts, 'follow_ups_due':due_count, 'collector':status()}
