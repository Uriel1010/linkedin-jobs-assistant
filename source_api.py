import time
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
import storage
import source_store

router=APIRouter()


class SourceCreate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider: Literal['greenhouse','lever','lever_eu','ashby']
    board: str = Field(min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    company: str = Field(min_length=1,max_length=120,pattern=r'\S')
    location_filter: str = Field(default='Israel',max_length=100)
    interval_minutes: int = Field(default=120,ge=60,le=10080)
    daily_budget: int = Field(default=48,ge=1,le=200)
    max_requests: int = Field(default=10,ge=1,le=20)


class SourceUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: int
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None,ge=60,le=10080)
    daily_budget: int | None = Field(default=None,ge=1,le=200)
    max_requests: int | None = Field(default=None,ge=1,le=20)


@router.get('/sources')
def sources():
    return source_store.list_sources()


@router.get('/sources/closed-jobs')
def closed_jobs(offset: int = Query(0,ge=0)):
    with storage.db() as con:
        rows=con.execute("SELECT id FROM jobs WHERE source!='linkedin' AND is_active=0 ORDER BY id LIMIT 500 OFFSET ?",(offset,)).fetchall()
        return {'ids':[r['id'] for r in rows]}


@router.post('/sources',status_code=201)
def create(body: SourceCreate):
    try:
        return source_store.add_source(body.model_dump())
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


@router.patch('/sources/{source_id}')
def update(source_id: int,body: SourceUpdate):
    fields=body.model_dump(exclude_unset=True,exclude={'revision'})
    if not fields or any(v is None for v in fields.values()):
        raise HTTPException(422,'Choose at least one setting.')
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
        if not row: raise HTTPException(404,'Source not found.')
        if row['revision']!=body.revision: raise HTTPException(409,'Source settings changed. Reload before saving.')
        if fields.get('enabled') and row['blocked']: raise HTTPException(409,'Review the source failure, then use Resume after review.')
        if row['lease_until']>time.time() and fields!={'enabled':False}:
            raise HTTPException(409,'A collection is running. Try again after it finishes.')
        fields['revision']=row['revision']+1
        if 'interval_minutes' in fields:
            fields['next_due']=max(time.time(),row['retry_after'],(row['last_success'] or time.time())+fields['interval_minutes']*60)
        con.execute('UPDATE sources SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[source_id])
        return dict(con.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone())


@router.post('/sources/{source_id}/poll')
def queue(source_id: int):
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
        if not row: raise HTTPException(404,'Source not found.')
        now=time.time()
        if not row['enabled'] or row['blocked']: raise HTTPException(409,'Source is paused or blocked. Review before resuming.')
        if row['lease_until']>now or row['retry_after']>now:
            raise HTTPException(409,'Source is running or cooling down.')
        # Manual refresh cannot bypass provider budgets or minimum spacing.
        if row['last_attempt'] and now-row['last_attempt']<900:
            raise HTTPException(429,'Wait at least 15 minutes between manual collections.')
        con.execute('UPDATE sources SET next_due=? WHERE id=?',(now,source_id))
    return {'status':'queued'}


@router.post('/sources/{source_id}/resume')
def resume(source_id: int):
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone()
        if not row: raise HTTPException(404,'Source not found.')
        if row['lease_until']>time.time(): raise HTTPException(409,'A collection is still running.')
        con.execute("""UPDATE sources SET enabled=1,blocked=0,failures=0,revision=revision+1,
                       status='pending',next_due=? WHERE id=?""",(max(time.time(),row['retry_after']),source_id))
    return {'status':'queued','note':'Provider cooldown and daily request budgets still apply.'}


@router.get('/sources/{source_id}/runs')
def history(source_id: int):
    with storage.db() as con:
        return {'items':[dict(r) for r in con.execute('SELECT * FROM source_runs WHERE source_id=? ORDER BY id DESC LIMIT 20',(source_id,))]}


class EmailPreview(BaseModel):
    content: str = Field(min_length=1,max_length=500000)


@router.post('/imports/email/preview')
def email_preview(body: EmailPreview):
    from email_import import parse_email
    return {'items':parse_email(body.content.encode()),'saved':False,
            'note':'Offline preview only. No mailbox is connected and no links were requested.'}
