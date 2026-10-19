import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.environ.get('DATA_DIR', '/app/data'))


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db():
    con = sqlite3.connect(DATA / 'jobs.sqlite3', timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    os.umask(0o077)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS searches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT NOT NULL,
          location TEXT NOT NULL, remote INTEGER NOT NULL, interval_minutes INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, last_attempt TEXT, last_success TEXT,
          status TEXT NOT NULL DEFAULT 'pending', error TEXT);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT, location TEXT,
          url TEXT NOT NULL, posted_text TEXT, first_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          search_id INTEGER NOT NULL REFERENCES searches(id),
          job_id TEXT NOT NULL REFERENCES jobs(id), first_seen TEXT NOT NULL,
          UNIQUE(search_id, job_id));
        ''')
        additions = {
            'searches': {'max_pages':'INTEGER NOT NULL DEFAULT 3', 'lookback_hours':'INTEGER NOT NULL DEFAULT 48',
                         'failures':'INTEGER NOT NULL DEFAULT 0', 'next_due':'TEXT'},
            'jobs': {'description':'TEXT', 'salary_text':'TEXT', 'seniority':'TEXT', 'employment_type':'TEXT',
                     'posting_observed_at':'TEXT',
                     'work_arrangement':'TEXT', 'application_url':'TEXT', 'posted_at':'TEXT',
                     'details_status':"TEXT NOT NULL DEFAULT 'pending'", 'details_error':'TEXT',
                     'details_attempt':'TEXT', 'details_updated':'TEXT'}
        }
        for table, columns in additions.items():
            existing = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            for column, definition in columns.items():
                if column not in existing:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
          started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
          found INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
          pages INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, alert_key TEXT NOT NULL, message TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT, acknowledged_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS active_alert_key ON alerts(alert_key) WHERE resolved_at IS NULL;
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
          status TEXT NOT NULL DEFAULT 'saved', notes TEXT NOT NULL DEFAULT '',
          contact_name TEXT, contact_email TEXT, cv_version TEXT, applied_at TEXT, follow_up_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL REFERENCES applications(id),
          from_status TEXT, to_status TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS network_state (
          id INTEGER PRIMARY KEY CHECK(id=1), last_attempt REAL NOT NULL DEFAULT 0,
          blocked_session REAL, reason TEXT, retry_after REAL NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO network_state(id) VALUES (1);
        ''')


def save_jobs(search_id, jobs):
    added = 0
    with db() as con:
        for job in jobs:
            timestamp = now()
            con.execute('''INSERT INTO jobs(id,title,company,location,url,posted_text,first_seen,posting_observed_at,posted_at) VALUES (?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET title=excluded.title,
              company=excluded.company, location=excluded.location,
              posted_text=excluded.posted_text,posting_observed_at=excluded.posting_observed_at,
              posted_at=COALESCE(excluded.posted_at,jobs.posted_at)''',
              (job['id'], job['title'], job.get('company'), job.get('location'),
               job['url'], job.get('posted_text'), timestamp, job.get('posting_observed_at'),job.get('posted_at')))
            cur = con.execute('INSERT OR IGNORE INTO events(search_id,job_id,first_seen) VALUES (?,?,?)',
                              (search_id, job['id'], timestamp))
            added += cur.rowcount
    return added


def alert(key, message):
    with db() as con:
        con.execute('''INSERT INTO alerts(alert_key,message,created_at,updated_at) VALUES (?,?,?,?)
          ON CONFLICT(alert_key) WHERE resolved_at IS NULL
          DO UPDATE SET message=excluded.message,updated_at=excluded.updated_at''',
                    (key, message, now(), now()))


def resolve_alert(key):
    with db() as con:
        con.execute('UPDATE alerts SET resolved_at=? WHERE alert_key=? AND resolved_at IS NULL', (now(), key))


DETAIL_FIELDS = ('description', 'salary_text', 'seniority', 'employment_type', 'work_arrangement', 'application_url', 'posted_at')


def save_details(job_id, details):
    with db() as con:
        assignments = ','.join('posted_at=COALESCE(?,posted_at)' if field=='posted_at' else f'{field}=?' for field in DETAIL_FIELDS)
        con.execute(f"UPDATE jobs SET {assignments},details_status='ok',details_error=NULL,details_updated=?,details_attempt=? WHERE id=?",
                    [details.get(field) for field in DETAIL_FIELDS] + [now(), now(), job_id])
