"""Durable source scheduling, request budgets, provenance and conservative deduplication."""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
import storage
from source_providers import SourceError, canonical_url


def init_sources():
    with storage.db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, board TEXT NOT NULL,
          company TEXT NOT NULL, location_filter TEXT NOT NULL DEFAULT '',
          interval_minutes INTEGER NOT NULL DEFAULT 120, daily_budget INTEGER NOT NULL DEFAULT 48,
          max_requests INTEGER NOT NULL DEFAULT 10, enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'pending', error TEXT, failures INTEGER NOT NULL DEFAULT 0,
          last_attempt REAL, last_success REAL, next_due REAL NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
          lease_until REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
          jobs_count INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
          search_id INTEGER REFERENCES searches(id), UNIQUE(provider,board));
        CREATE TABLE IF NOT EXISTS source_items(
          source_id INTEGER NOT NULL REFERENCES sources(id), external_id TEXT NOT NULL,
          job_id TEXT NOT NULL REFERENCES jobs(id), active INTEGER NOT NULL DEFAULT 1,
          last_seen REAL NOT NULL, PRIMARY KEY(source_id,external_id));
        CREATE TABLE IF NOT EXISTS source_runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,source_id INTEGER NOT NULL REFERENCES sources(id),
          started REAL NOT NULL,finished REAL,status TEXT NOT NULL,error TEXT,
          jobs_count INTEGER NOT NULL DEFAULT 0,added INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS source_requests(source_id INTEGER NOT NULL,host TEXT NOT NULL,at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS source_requests_time ON source_requests(at);
        CREATE TABLE IF NOT EXISTS source_cache(
          source_id INTEGER NOT NULL,url TEXT NOT NULL,etag TEXT,modified TEXT,body TEXT NOT NULL,
          PRIMARY KEY(source_id,url));
        CREATE TABLE IF NOT EXISTS service_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        columns={r['name'] for r in con.execute('PRAGMA table_info(events)')}
        if 'baseline' not in columns:
            con.execute('ALTER TABLE events ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0')
        con.execute('CREATE INDEX IF NOT EXISTS jobs_canonical_url ON jobs(canonical_url)')
        for row in con.execute('SELECT id,url,application_url FROM jobs WHERE canonical_url IS NULL').fetchall():
            canonical=canonical_url(row['application_url']) or canonical_url(row['url'])
            if canonical:
                con.execute('UPDATE jobs SET canonical_url=? WHERE id=?',(canonical,row['id']))


def list_sources():
    with storage.db() as con:
        rows=[dict(r) for r in con.execute('SELECT * FROM sources ORDER BY company,id')]
        for row in rows:
            row['requests_today']=con.execute('SELECT count(*) FROM source_requests WHERE source_id=? AND at>?',
                                             (row['id'],time.time()-86400)).fetchone()[0]
        heartbeat=con.execute("SELECT value FROM service_state WHERE key='feeds_heartbeat'").fetchone()
    return {'items':rows,'worker_alive':bool(heartbeat and time.time()-float(heartbeat[0])<900),
            'linkedin_enabled':False,'email_connected':False}


def add_source(values):
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT count(*) FROM sources').fetchone()[0]>=30:
            raise ValueError('Maximum 30 sources.')
        source=dict(values)
        if con.execute('SELECT 1 FROM sources WHERE provider=? AND board=?',(source['provider'],source['board'])).fetchone():
            raise ValueError('This board is already tracked.')
        search_id=con.execute("""INSERT INTO searches(keywords,location,remote,interval_minutes,enabled,kind)
                              VALUES (?,?,0,?,0,'public_feed')""",
                              (source['company'],source['location_filter'],source['interval_minutes'])).lastrowid
        keys=list(source)
        source_id=con.execute('INSERT INTO sources('+','.join(keys)+',search_id) VALUES ('+
                              ','.join('?' for _ in keys)+',?)',[source[k] for k in keys]+[search_id]).lastrowid
        return dict(con.execute('SELECT * FROM sources WHERE id=?',(source_id,)).fetchone())


def reserve_request(source,host):
    # Shared per-host pacing applies to every HTTP page, not merely whole runs.
    while True:
        with storage.db() as con:
            con.execute('BEGIN IMMEDIATE')
            now=time.time()
            con.execute('DELETE FROM source_requests WHERE at<?',(now-86400,))
            current=con.execute('SELECT * FROM sources WHERE id=?',(source['id'],)).fetchone()
            if not current or not current['enabled'] or current['blocked']:
                raise SourceError('source_paused')
            if con.execute('SELECT count(*) FROM source_requests WHERE source_id=?',(source['id'],)).fetchone()[0]>=current['daily_budget']:
                raise SourceError('daily_budget_exhausted',retry_after=3600)
            if con.execute('SELECT count(*) FROM source_requests WHERE host=?',(host,)).fetchone()[0]>=300:
                raise SourceError('provider_daily_budget_exhausted',retry_after=3600)
            last=con.execute('SELECT max(at) FROM source_requests WHERE host=?',(host,)).fetchone()[0] or 0
            delay=max(0,3-(now-last))
            if not delay:
                con.execute('INSERT INTO source_requests VALUES (?,?,?)',(source['id'],host,now))
                return
        time.sleep(delay)


def read_cache(source_id,url):
    with storage.db() as con:
        row=con.execute('SELECT * FROM source_cache WHERE source_id=? AND url=?',(source_id,url)).fetchone()
        return dict(row) if row else None


def write_cache(source_id,url,body,etag,modified):
    with storage.db() as con:
        con.execute('INSERT INTO source_cache VALUES (?,?,?,?,?) ON CONFLICT(source_id,url) DO UPDATE SET etag=excluded.etag,modified=excluded.modified,body=excluded.body',
                    (source_id,url,etag,modified,body))


def save_snapshot(source,jobs):
    now=time.time(); stamp=storage.now(); added=0
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        current=con.execute('SELECT * FROM sources WHERE id=?',(source['id'],)).fetchone()
        if current['revision']!=source['revision'] or not current['enabled']:
            raise SourceError('settings_changed_during_collection')
        previous=con.execute('SELECT count(*) FROM source_items WHERE source_id=? AND active=1',(source['id'],)).fetchone()[0]
        if previous and not jobs:
            raise SourceError('empty_result_requires_review',blocked=True)
        con.execute('UPDATE source_items SET active=0 WHERE source_id=?',(source['id'],))
        for job in jobs:
            url=canonical_url(job['url'])
            if not url: raise SourceError('invalid_job_url')
            existing=con.execute('SELECT job_id FROM source_items WHERE source_id=? AND external_id=?',
                                 (source['id'],job['external_id'])).fetchone()
            if not existing:
                # Exact canonical links only: never merge distinct roles by similar titles.
                # Match the posting URL, not a potentially shared application form.
                existing=con.execute('SELECT id FROM jobs WHERE canonical_url=? OR url=? OR application_url=? ORDER BY first_seen LIMIT 1',
                                     (url,url,url)).fetchone()
            job_id=existing[0] if existing else '99'+str(int(hashlib.sha256(
                (source['provider']+':'+source['board']+':'+job['external_id']).encode()).hexdigest(),16) % 10**27).zfill(27)
            if not existing and con.execute('SELECT 1 FROM jobs WHERE id=?',(job_id,)).fetchone():
                raise SourceError('job_identity_collision',blocked=True)
            if not existing:
                con.execute('''INSERT INTO jobs(id,title,company,location,url,first_seen,source,source_job_id,
                  canonical_url,description,posted_at,work_arrangement,employment_type,application_url,details_status,details_updated)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ok',?)''',
                  (job_id,job['title'],job['company'],job['location'],url,stamp,source['provider'],job['external_id'],url,
                   job.get('description'),job.get('posted_at'),job.get('work_arrangement'),job.get('employment_type'),job.get('application_url'),stamp))
                added+=1
            else:
                con.execute('''UPDATE jobs SET is_active=1,description=COALESCE(?,description),
                  application_url=COALESCE(?,application_url),details_status='ok',details_updated=? WHERE id=?''',
                  (job.get('description'),job.get('application_url'),stamp,job_id))
                # Preserve original LinkedIn identity/provenance when matching an existing job.
                con.execute('''UPDATE jobs SET title=?,location=?,posted_at=COALESCE(?,posted_at),
                  work_arrangement=?,employment_type=? WHERE id=? AND source!='linkedin' ''',
                  (job['title'],job['location'],job.get('posted_at'),job.get('work_arrangement'),job.get('employment_type'),job_id))
            con.execute('''INSERT INTO source_items VALUES (?,?,?,1,?) ON CONFLICT(source_id,external_id)
                           DO UPDATE SET active=1,last_seen=excluded.last_seen''',(source['id'],job['external_id'],job_id,now))
            con.execute('INSERT OR IGNORE INTO events(search_id,job_id,first_seen,baseline) VALUES (?,?,?,?)',
                        (source['search_id'],job_id,stamp,int(source['last_success'] is None)))
        con.execute("""UPDATE jobs SET is_active=0 WHERE source!='linkedin'
                       AND id IN (SELECT job_id FROM source_items WHERE source_id=?)
                       AND NOT EXISTS(SELECT 1 FROM source_items i WHERE i.job_id=jobs.id AND i.active=1)""",(source['id'],))
        con.execute("""UPDATE sources SET status='ok',error=NULL,failures=0,last_success=?,next_due=?,
                       lease_until=0,jobs_count=?,added=? WHERE id=?""",
                    (now,now+source['interval_minutes']*60,len(jobs),added,source['id']))
    storage.resolve_alert('source:'+str(source['id']))
    return added


def claim():
    with storage.db() as con:
        con.execute('BEGIN IMMEDIATE')
        now=time.time()
        row=con.execute('''SELECT * FROM sources WHERE enabled=1 AND blocked=0 AND provider!='email'
                           AND next_due<=? AND retry_after<=? AND lease_until<=? ORDER BY next_due,id LIMIT 1''',
                        (now,now,now)).fetchone()
        if not row: return None
        source=dict(row)
        con.execute("UPDATE source_runs SET status='interrupted',finished=?,error='worker_restarted' WHERE source_id=? AND status='running'",
                    (now,source['id']))
        con.execute("UPDATE sources SET status='running',last_attempt=?,lease_until=? WHERE id=?",(now,now+900,source['id']))
        source['run_id']=con.execute("INSERT INTO source_runs(source_id,started,status) VALUES (?,?,'running')",
                                    (source['id'],now)).lastrowid
        return source


def fail(source,error):
    now=time.time()
    with storage.db() as con:
        failures=source['failures']+1
        blocked=error.blocked or (error.reason=='rate_limited' and failures>=3)
        retry=now+max(error.retry_after, min(86400,source['interval_minutes']*60*2**min(failures,4)))
        con.execute("""UPDATE sources SET status=?,error=?,failures=?,blocked=?,retry_after=?,
                       next_due=?,lease_until=0 WHERE id=?""",
                    ('blocked' if blocked else 'error',error.reason,failures,int(blocked),retry,retry,source['id']))
        con.execute("UPDATE source_runs SET status='error',error=?,finished=? WHERE id=?",(error.reason,now,source['run_id']))
    storage.alert('source:'+str(source['id']), source['company']+': '+error.reason.replace('_',' ')+
                  (' · manual review and resume required' if blocked else ' · next attempt is delayed'))


def heartbeat():
    with storage.db() as con:
        con.execute("INSERT INTO service_state VALUES ('feeds_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(time.time()),))
        con.execute('DELETE FROM source_runs WHERE id NOT IN (SELECT id FROM source_runs ORDER BY id DESC LIMIT 2000)')
