import json
import hashlib
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from maintenance import backup_database
from feed_rules import DEFAULT_PREFERENCES, ROLES, LEVELS, match_job, job_card, local_time, friendly_alert


class ServiceError(Exception):
    def __init__(self, status, detail='Service temporarily unavailable', retry_after=0, service='jobs'):
        self.status, self.detail = status, str(detail)[:500]
        self.retry_after = retry_after
        self.service = service
        super().__init__(self.detail)


class State:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.folder/'bot.sqlite3')
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.executescript('''
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS views(id INTEGER PRIMARY KEY AUTOINCREMENT,query TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries (
            job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            discovered_at REAL NOT NULL, sent_at REAL);
          CREATE TABLE IF NOT EXISTS notices (
            notice_key TEXT PRIMARY KEY, sent_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS delivery_parts (
            batch_id INTEGER NOT NULL, part INTEGER NOT NULL,total_parts INTEGER NOT NULL,
            total_jobs INTEGER NOT NULL,job_ids TEXT NOT NULL,text TEXT NOT NULL,sent_at REAL,
            PRIMARY KEY(batch_id,part));
          CREATE TABLE IF NOT EXISTS feed_messages (
            message_id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL);
        ''')
        columns={row[1] for row in self.con.execute('PRAGMA table_info(deliveries)')}
        for column,definition in {'reason':'TEXT','batch_id':'INTEGER'}.items():
            if column not in columns:
                self.con.execute(f'ALTER TABLE deliveries ADD COLUMN {column} {definition}')
        self.con.commit()
        # Upgrade the previous bounded cache to permanent suppression records.
        if not self.get('delivery_ledger_migrated',False):
            with self.con:
                for job_id in self.get('recent_job_ids',[]):
                    self.con.execute("INSERT OR IGNORE INTO deliveries(job_id,payload,status,discovered_at,sent_at) VALUES (?,?,'sent',?,?)",
                                     (job_id,json.dumps({'id':job_id}),time.time(),time.time()))
            self.set('delivery_ledger_migrated',True)

    def get(self, key, default=None):
        row = self.con.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))
        self.con.commit()

    def view(self, query):
        row = self.con.execute('INSERT INTO views(query) VALUES (?)',(query,))
        self.con.commit()
        return row.lastrowid

    def query(self, view_id):
        row = self.con.execute('SELECT query FROM views WHERE id=?',(view_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def job_payload(job):
        return {field:job.get(field) for field in ('id','title','company','location','url','posted_text',
            'posted_at','posting_observed_at','first_seen','seniority','employment_type','work_arrangement')}

    def enqueue(self, items, cursor):
        with self.con:
            for job in items:
                self.con.execute("INSERT OR IGNORE INTO deliveries(job_id,payload,status,discovered_at,sent_at) VALUES (?,?,'pending',?,NULL)",
                                 (job['id'],json.dumps(self.job_payload(job)),time.time()))
            self.con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             ('cursor',json.dumps(cursor)))

    def seed_baseline(self, items, cursor):
        with self.con:
            for job in items:
                self.con.execute("INSERT OR IGNORE INTO deliveries(job_id,payload,status,discovered_at,sent_at) VALUES (?,?,'baseline',?,NULL)",
                                 (job['id'],json.dumps(self.job_payload(job)),time.time()))
            for key,value in {'cursor':cursor,'feed_initialized':True,'feed_started':time.time(),
                              'baseline_jobs':len(items),'notify':True}.items():
                self.con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))

    def pending(self, limit=50):
        return [json.loads(row['payload']) for row in self.con.execute(
            "SELECT * FROM deliveries WHERE status='pending' ORDER BY discovered_at,job_id LIMIT ?",(limit,))]

    def delivered(self, job_ids):
        with self.con:
            self.con.executemany("UPDATE deliveries SET status='sent',sent_at=? WHERE job_id=? AND status='pending'",
                                 [(time.time(),job_id) for job_id in job_ids])

    def recent(self, limit=10):
        return [json.loads(row['payload']) for row in self.con.execute(
            "SELECT * FROM deliveries WHERE status='sent' ORDER BY sent_at DESC LIMIT ?",(limit,))]

    def pending_count(self):
        return self.con.execute("SELECT count(*) FROM deliveries WHERE status='pending'").fetchone()[0]

    def notice_sent(self, key, within=None):
        row = self.con.execute('SELECT sent_at FROM notices WHERE notice_key=?',(key,)).fetchone()
        return bool(row and (within is None or time.time()-row[0]<within))

    def mark_notice(self, key):
        with self.con:
            self.con.execute('INSERT INTO notices VALUES (?,?) ON CONFLICT(notice_key) DO UPDATE SET sent_at=excluded.sent_at', (key,time.time()))

    def preferences(self):
        return {**DEFAULT_PREFERENCES,**self.get('preferences',{})}

    def set_preferences(self, preferences):
        self.set('preferences',preferences)
        # Re-plan only undelivered parts; sent jobs stay permanently deduplicated.
        with self.con:
            self.con.execute('DELETE FROM delivery_parts WHERE sent_at IS NULL')
            self.con.execute("UPDATE deliveries SET batch_id=NULL WHERE status='pending'")
            self.con.execute("UPDATE deliveries SET status='pending',reason=NULL WHERE status='filtered'")

    def prepare_batch(self, catchup=False):
        if self.con.execute('SELECT 1 FROM delivery_parts WHERE sent_at IS NULL LIMIT 1').fetchone():
            return
        rows=[dict(row) for row in self.con.execute("SELECT * FROM deliveries WHERE status='pending' ORDER BY discovered_at,job_id"+
            ('' if catchup else ' LIMIT 500'))]
        if not rows:
            return
        accepted=[]
        prefs=self.preferences()
        with self.con:
            for row in rows:
                job=json.loads(row['payload'])
                matches,reason=match_job(job,prefs,now=datetime.fromtimestamp(row['discovered_at'],timezone.utc))
                if matches:
                    accepted.append(job)
                else:
                    self.con.execute("UPDATE deliveries SET status='filtered',reason=? WHERE job_id=?",(reason,row['job_id']))
        if not accepted:
            return
        groups=[]; group=[]; size=0
        for job in accepted:
            length=len(job_card(job).encode('utf-16-le'))//2+2
            if group and (len(group)>=5 or size+length>3000):
                groups.append(group); group=[]; size=0
            group.append(job); size+=length
        if group:
            groups.append(group)
        batch_id=int(self.get('last_batch_id',0))+1
        stamp=local_time(datetime.now(timezone.utc).isoformat(),True)
        with self.con:
            for index,group in enumerate(groups,1):
                noun='job' if len(accepted)==1 else 'jobs'
                heading='Catch-up after quiet hours' if catchup else 'Hourly update'
                text=f"{heading} · {stamp} Israel time\n{len(accepted)} newly found {noun} · Page {index}/{len(groups)}\n\n"+'\n\n'.join(job_card(job) for job in group)
                ids=[job['id'] for job in group]
                self.con.execute('INSERT INTO delivery_parts VALUES (?,?,?,?,?,?,NULL)',
                                 (batch_id,index,len(groups),len(accepted),json.dumps(ids),text))
                self.con.executemany('UPDATE deliveries SET batch_id=? WHERE job_id=?',[(batch_id,job_id) for job_id in ids])
            self.con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('last_batch_id',json.dumps(batch_id)))

    def complete_part(self, batch_id, part, job_ids):
        with self.con:
            self.con.executemany("UPDATE deliveries SET status='sent',sent_at=? WHERE job_id=? AND status='pending'",[(time.time(),job_id) for job_id in job_ids])
            self.con.execute('UPDATE delivery_parts SET sent_at=? WHERE batch_id=? AND part=?',(time.time(),batch_id,part))


def http_json(url, payload=None, headers=None, timeout=30, method=None, telegram=False):
    req = urllib.request.Request(url, headers=headers or {}, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None)
    req.add_header('Content-Type','application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        if telegram and not data.get('ok'):
            raise ServiceError(data.get('error_code',502),'Telegram request failed',service='telegram')
        return data.get('result') if telegram else data
    except urllib.error.HTTPError as exc:
        detail = 'Telegram request failed' if telegram else 'Jobs API request failed'
        retry_after = 0
        try:
            body = json.loads(exc.read())
            if telegram:
                retry_after = int(body.get('parameters',{}).get('retry_after',0))
                if exc.code==400 and 'message is not modified' in str(body.get('description','')).lower():
                    detail='Message is not modified'
            else:
                detail = body.get('detail',detail)
        except (ValueError,AttributeError,TypeError):
            pass
        raise ServiceError(exc.code,detail,retry_after,'telegram' if telegram else 'jobs') from None
    except (urllib.error.URLError,TimeoutError,ValueError,OSError):
        raise ServiceError(503,service='telegram' if telegram else 'jobs') from None


HELP = '''Your private jobs database bot.

/jobs [words] — browse stored jobs
/new — reopen the latest job update
/preferences — role, seniority, region and freshness filters
/quiet — choose quiet hours, custom times or turn them off
/job JOB_ID — details and description preview
/description JOB_ID — full stored description
/refresh JOB_ID — fetch a description from LinkedIn
/status — database and collector health
/track job title | location — start an hourly collection search
/searches — list collection searches
/setsearch ID job title | location — edit a search
/collect SEARCH_ID — collect now (cooldown applies)
/pause SEARCH_ID or /resume SEARCH_ID
/save JOB_ID — add to application tracker
/applications [stage] — tracked applications
/stage APP_ID stage — update tracking stage
/followups — due follow-ups
/alerts — collection problems
/notify on or /notify off — new-job, alert and follow-up notifications

Stages: saved, reviewing, applied, interviewing, offer, rejected, withdrawn, archived.
Tracking updates do not submit applications or contact employers.
Search results and descriptions come from the database; collection is separate.'''

STAGES = {'saved','reviewing','applied','interviewing','offer','rejected','withdrawn','archived'}


class Bot:
    def __init__(self, token, owner, api_key, state, api_base='http://api:8765'):
        self.token, self.owner, self.api_key, self.state = token, int(owner), api_key, state
        self.api_base = api_base.rstrip('/')
        self.last_send = 0.0

    def telegram(self, method, payload=None, timeout=40):
        if method=='sendMessage':
            remaining = self.state.get('send_after',0)-time.time()
            if remaining>0:
                raise ServiceError(429,'Telegram delivery cooldown',int(remaining)+1)
        try:
            return http_json('https://api.telegram.org/bot'+self.token+'/'+method,payload,timeout=timeout,telegram=True)
        except ServiceError as exc:
            if exc.status==429:
                self.state.set('send_after',time.time()+max(10,exc.retry_after))
            if method=='sendMessage' and exc.status==403:
                self.state.set('chat_ready',False)
            raise

    def api(self, method, path, payload=None):
        return http_json(self.api_base+path,payload,headers={'X-API-Key':self.api_key},
                         method=method,timeout=300 if method=='POST' else 30)

    def configure_profile(self):
        commands=[('start','Open your private job feed'),('new','Latest update with page navigation'),
            ('jobs','Browse all stored jobs'),('preferences','Choose roles, levels and location'),
            ('quiet','Choose quiet hours and timezone'),('status','Feed and search health'),
            ('searches','Your collection searches'),('track','Add search: title | location'),
            ('setsearch','Edit search: ID title | location'),('collect','Run a search now'),
            ('pause','Pause collection for a search'),('resume','Resume collection for a search'),
            ('notify','Turn notifications on or off'),('job','Job details: JOB_ID'),
            ('description','Stored description: JOB_ID'),('refresh','Fetch description: JOB_ID'),
            ('save','Track an application: JOB_ID'),('applications','Your application tracker'),
            ('stage','Update application: APP_ID stage'),('followups','Due follow-ups'),
            ('alerts','Collection alerts'),('help','All commands and examples')]
        payload={'commands':[{'command':name,'description':description} for name,description in commands]}
        self.telegram('setMyCommands',payload)
        self.telegram('setMyCommands',{**payload,'scope':{'type':'chat','chat_id':self.owner}})
        self.telegram('setChatMenuButton',{'menu_button':{'type':'commands'}})
        self.telegram('setChatMenuButton',{'chat_id':self.owner,'menu_button':{'type':'commands'}})
        self.telegram('setMyDescription',{'description':
            'Your private job-search assistant. Discover new LinkedIn jobs hourly, browse updates with page buttons, '
            'choose role and location filters, and set your own quiet hours. Collection continues while notifications '
            'are quiet, with a catch-up update afterward. Browse your saved jobs and track applications from one chat.'})
        self.telegram('setMyShortDescription',{'short_description':
            'Private hourly job alerts with links, filters, quiet hours and paginated catch-up updates.'})
        photo=Path(__file__).with_name('job-search-profile.jpg')
        if photo.exists():
            content=photo.read_bytes()
            digest=hashlib.sha256(content).hexdigest()
            if self.state.get('profile_photo_hash')!=digest:
                self.upload_profile_photo(content)
                self.state.set('profile_photo_hash',digest)

    def upload_profile_photo(self, content):
        boundary='jobs-'+uuid.uuid4().hex
        photo=json.dumps({'type':'static','photo':'attach://avatar'})
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"\r\n\r\n{photo}\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="avatar"; filename="profile.jpg"\r\n'
              'Content-Type: image/jpeg\r\n\r\n').encode()+content+f'\r\n--{boundary}--\r\n'.encode()
        request=urllib.request.Request('https://api.telegram.org/bot'+self.token+'/setMyProfilePhoto',data=body,
            headers={'Content-Type':'multipart/form-data; boundary='+boundary},method='POST')
        try:
            with urllib.request.urlopen(request,timeout=60) as response:
                data=json.load(response)
            if not data.get('ok'):
                raise ServiceError(data.get('error_code',502),'Profile photo update failed',service='telegram')
        except urllib.error.HTTPError as exc:
            raise ServiceError(exc.code,'Profile photo update failed',service='telegram') from None
        except (urllib.error.URLError,TimeoutError,ValueError,OSError):
            raise ServiceError(503,'Profile photo update failed',service='telegram') from None

    def send(self, text, buttons=None):
        text = str(text)[:14000]
        parts, chunk, size = [], [], 0
        for char in text:
            width = 2 if ord(char)>0xffff else 1
            if size+width>3500:
                parts.append(''.join(chunk)); chunk=[]; size=0
            chunk.append(char); size+=width
        if chunk:
            parts.append(''.join(chunk))
        for index,part in enumerate(parts):
            time.sleep(max(0,1.05-(time.monotonic()-self.last_send)))
            data = {'chat_id':self.owner,'text':part, 'link_preview_options':{'is_disabled':True}}
            if buttons and index==len(parts)-1:
                data['reply_markup'] = {'inline_keyboard':buttons}
            result = self.telegram('sendMessage',data)
            self.last_send = time.monotonic()
        return result

    def edit(self, message_id, text, buttons):
        try:
            return self.telegram('editMessageText',{
                'chat_id':self.owner,'message_id':message_id,'text':text,
                'link_preview_options':{'is_disabled':True},
                'reply_markup':{'inline_keyboard':buttons}})
        except ServiceError as exc:
            if exc.status==400 and 'message is not modified' in exc.detail.lower():
                return
            raise

    def feed_page(self, batch_id, part=1, message_id=None):
        pages=[dict(row) for row in self.state.con.execute(
            'SELECT * FROM delivery_parts WHERE batch_id=? ORDER BY part',(batch_id,))]
        if not pages:
            raise ValueError('This update is no longer available.')
        page=next((row for row in pages if row['part']==part),None)
        if page is None:
            raise ValueError('This page does not exist.')
        buttons=[]
        if len(pages)>1:
            navigation=[]
            index=pages.index(page)
            if index:
                navigation.append({'text':'‹ Previous','callback_data':f'feed:{batch_id}:{pages[index-1]["part"]}'})
            navigation.append({'text':f'{index+1}/{len(pages)}','callback_data':'noop'})
            if index+1<len(pages):
                navigation.append({'text':'Next ›','callback_data':f'feed:{batch_id}:{pages[index+1]["part"]}'})
            buttons=[navigation]
        if message_id is not None:
            binding=self.state.con.execute('SELECT batch_id FROM feed_messages WHERE message_id=?',(message_id,)).fetchone()
            if binding is None or binding['batch_id']!=batch_id:
                raise ValueError('This update is no longer available. Use /new.')
            self.edit(message_id,page['text'].replace(' · Part ',' · Page '),buttons)
        else:
            result=self.send(page['text'].replace(' · Part ',' · Page '),buttons)
            with self.state.con:
                self.state.con.execute('INSERT INTO feed_messages VALUES (?,?)',(result['message_id'],batch_id))
        return pages

    @staticmethod
    def number(value):
        if not re.fullmatch(r'\d{1,30}',value):
            raise ValueError('Use a numeric ID.')
        return value

    def browse(self, query='', offset=0, view_id=None, message_id=None):
        query = query.strip()[:200]
        view_id = view_id or self.state.view(query)
        data = self.api('GET','/catalog?'+urllib.parse.urlencode({'q':query,'offset':offset,'limit':5}))
        if not data['items']:
            self.send('No stored jobs match this search. Use /track job title | location to start collecting.')
            return
        lines = [f"Stored jobs {offset+1}–{offset+len(data['items'])} of {data['total']}" + (f' · {query}' if query else '')]
        buttons = []
        for job in data['items']:
            lines.append(f"\n{job['id']} · {job['title'][:160]}\n{(job.get('company') or 'Company unknown')[:100]} · {(job.get('location') or 'Location unknown')[:180]}\n{job['url']}")
        navigation = []
        if offset:
            navigation.append({'text':'Previous','callback_data':f'page:{view_id}:{max(0,offset-5)}'})
        if offset+len(data['items'])<data['total']:
            navigation.append({'text':'Next','callback_data':f'page:{view_id}:{offset+5}'})
        if navigation:
            buttons.append(navigation)
        if message_id is None:
            self.send('\n'.join(lines),buttons)
        else:
            self.edit(message_id,'\n'.join(lines),buttons)

    def show_job(self, job_id, full=False):
        job = self.api('GET','/jobs/'+self.number(job_id))
        fields = [job['title'],job.get('company'),job.get('location'),job.get('employment_type'),
                  job.get('salary_text'),job['url']]
        description = job.get('description')
        if description:
            fields += ['',description[:12000 if full else 2300]]
            if not full and len(description)>2300:
                fields.append('Full description: /description '+job_id)
        else:
            fields += ['','Description: '+job['details_status']+'. Use the fetch button below.']
        self.send('\n'.join(str(v) for v in fields if v is not None),
                  [[{'text':'Fetch description','callback_data':'refresh:'+job_id},
                    {'text':'Save job','callback_data':'save:'+job_id}]])

    def save_job(self, job_id):
        try:
            result = self.api('POST','/applications',{'job_id':self.number(job_id)})
            self.send(f"Saved to your tracker. Application ID: {result['id']}\n{result['title']}")
        except ServiceError as exc:
            if exc.status!=409:
                raise
            self.send('This job is already in your tracker. See /applications.')

    def application_list(self, stage='', due=False):
        if stage and stage not in STAGES:
            raise ValueError('Unknown stage. See /help.')
        params = {'limit':10}
        if stage: params['status']=stage
        if due: params['due_only']='true'
        data = self.api('GET','/applications?'+urllib.parse.urlencode(params))
        lines = [f"{'Follow-ups due' if due else 'Applications'}: {data['total']}"]
        for row in data['items']:
            lines.append(f"\n#{row['id']} · {row['status']} · {row['title'][:150]}\n{row.get('company') or ''}\n{row['url']}"+
                         (f"\nFollow-up: {row['follow_up_at']}" if row.get('follow_up_at') else ''))
        self.send('\n'.join(lines))

    def primary_search(self):
        search_id=self.state.get('hourly_search_id')
        if search_id:
            return str(search_id)
        rows=self.api('GET','/searches')
        if len(rows)==1:
            return str(rows[0]['id'])
        raise ValueError('Choose a search ID from /searches.')

    def quiet_active(self, now=None):
        config=self.state.get('quiet_hours',{})
        if not config.get('enabled'):
            return False
        local=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo(config['timezone']))
        minute=local.hour*60+local.minute
        start,end=config['start'],config['end']
        return start<=minute<end if start<end else minute>=start or minute<end

    def configure_quiet(self, args=''):
        config=self.state.get('quiet_hours',{'enabled':False,'timezone':'Asia/Jerusalem'})
        if args=='off':
            config['enabled']=False
        elif args:
            parts=args.split()
            if len(parts) not in (2,3) or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',v) for v in parts[:2]):
                raise ValueError('Use /quiet 23:00 08:00 [timezone], or /quiet off. Example timezone: Asia/Jerusalem.')
            zone=parts[2] if len(parts)==3 else config['timezone']
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError,ValueError):
                raise ValueError('Unknown timezone. Example: Asia/Jerusalem.') from None
            start,end=[int(v[:2])*60+int(v[3:]) for v in parts[:2]]
            if start==end:
                raise ValueError('Start and end must differ. Use /quiet off to disable.')
            config={'enabled':True,'start':start,'end':end,'timezone':zone}
        if args:
            self.state.set('quiet_hours',config)
        if config['enabled']:
            label=lambda m:f'{m//60:02d}:{m%60:02d}'
            schedule=f"{label(config['start'])}–{label(config['end'])} · {config['timezone']}"
            status='Quiet now' if self.quiet_active() else 'Outside quiet hours'
        else:
            schedule='Off · choose your hours below'
            status=''
        self.send('Quiet hours\n'+schedule+('\n'+status if status else '')+
            '\n\nCollection continues. Automatic messages wait until quiet hours end, then new jobs from the quiet period arrive together with page buttons. '
            'Your commands still work at any time.\n\nCustom: /quiet 23:00 08:00\nOptional timezone: /quiet 23:00 08:00 Europe/London',
            [[{'text':'23:00–08:00','callback_data':'quiet:23:00 08:00'},
              {'text':'22:00–08:00','callback_data':'quiet:22:00 08:00'}],
             [{'text':'00:00–07:00','callback_data':'quiet:00:00 07:00'},
              {'text':'Turn off','callback_data':'quiet:off'}]])

    def show_status(self):
        data=self.api('GET','/overview')
        searches=self.api('GET','/searches')
        active=[row for row in searches if row['enabled']]
        state=data['collector']
        network=state.get('network',{})
        if network.get('blocked_session'):
            connection='Sign-in required · open /alerts'
        elif network.get('retry_after',0)>time.time():
            connection='Cooling down; the server will retry automatically'
        elif any(row.get('last_success') for row in searches):
            connection='Working at the last successful search'
        else:
            connection='Awaiting the first successful search'
        prefs=self.state.preferences()
        lines=['Your job feed · Israel time',
               'Delivery: '+('On' if self.state.get('notify',False) else 'Paused'),
               'Quiet hours: '+('Active · automatic messages queued' if self.quiet_active() else
                   ('Scheduled · /quiet' if self.state.get('quiet_hours',{}).get('enabled') else 'Off · /quiet to choose')),
               'LinkedIn: '+connection,
               'Roles: '+', '.join(prefs['roles'])+' · Levels: '+', '.join(prefs['levels']),
               'Location rule: '+('Israel explicitly listed' if prefs['region']=='israel' else 'Broader locations allowed')]
        for row in active[:3]:
            lines+=['',f"{row['keywords']} · {row['location'] or 'Any location'}",
                    'Last successful search: '+local_time(row.get('last_success'),True),
                    'Next search: '+local_time(row.get('next_due'),True)]
            if row.get('status')=='partial':
                lines.append('Coverage: limited · some results may be missing')
        if not active:
            lines.append('Collection is paused. Use /resume to restart your search.')
        filtered=self.state.con.execute("SELECT count(*) FROM deliveries WHERE status='filtered'").fetchone()[0]
        lines+=['',f"Saved jobs: {data['jobs']} · Filtered from feed: {filtered}",
                f"Waiting to send: {self.state.pending_count()}",
                'Last delivery: '+local_time(self.state.get('last_delivery_at'),True),
                f"Descriptions: {state.get('descriptions_pending',state.get('details_pending_or_failed',0))} preparing · {state.get('descriptions_failed',0)} unavailable",
                f"Issues: {state['open_alerts']} · Follow-ups due: {data['follow_ups_due']}"]
        self.send('\n'.join(lines),[[{'text':'New jobs','callback_data':'cmd:new'},
                                    {'text':'Preferences','callback_data':'cmd:preferences'}],
                                   [{'text':'Searches','callback_data':'cmd:searches'},
                                    {'text':'Pause alerts' if self.state.get('notify',False) else 'Resume alerts',
                                     'callback_data':'cmd:notify '+('off' if self.state.get('notify',False) else 'on')}]])

    def configure_preferences(self,args=''):
        prefs=self.state.preferences()
        if args:
            key,_,value=args.partition(' ')
            value=value.strip().casefold()
            if key in ('roles','levels'):
                choices=[part.strip() for part in value.split(',') if part.strip()]
                allowed=ROLES if key=='roles' else LEVELS
                if not choices or any(part not in allowed for part in choices):
                    raise ValueError('Choose '+key+': '+', '.join(sorted(allowed)))
                prefs[key]=list(dict.fromkeys(choices))
            elif key=='region' and value in ('israel','broad'):
                prefs[key]=value
            elif key in ('include','exclude'):
                prefs[key]=[part.strip() for part in value.split(',') if part.strip()][:20] if value!='clear' else []
            elif key=='age' and value.isdigit() and 1<=int(value)<=168:
                prefs['max_age_hours']=int(value)
            elif key=='unknown' and value in ('keep','hide'):
                prefs['keep_unknown_level']=value=='keep'
            else:
                raise ValueError('Use /preferences roles software,data · levels senior,lead · region israel/broad · include/exclude words · age 48 · unknown keep/hide.')
            self.state.set_preferences(prefs)
        text=('Feed preferences\n\nRoles: '+', '.join(prefs['roles'])+'\nSeniority: '+', '.join(prefs['levels'])+
              '\nLocation: '+('Israel must be explicitly listed' if prefs['region']=='israel' else 'Broader regions and remote locations allowed')+
              f"\nPosting age limit: {prefs['max_age_hours']} hours when LinkedIn supplies usable age information"+
              '\nUnknown seniority: '+('kept' if prefs['keep_unknown_level'] else 'hidden')+
              '\nInclude title words: '+(', '.join(prefs['include']) or 'Any')+
              '\nExclude title words: '+(', '.join(prefs['exclude']) or 'None')+
              '\n\nSoftware includes backend, frontend and full stack. Title-based categories are rules, not a CV match score. '
              'Posting dates that LinkedIn does not provide stay unknown.\n\n'
              'Custom: /preferences roles software,data or /preferences levels senior,lead. '
              'Previously filtered discoveries are rechecked when you change preferences; sent jobs stay deduplicated.')
        self.send(text,[[{'text':'Software roles','callback_data':'pref:roles:software'},{'text':'All roles','callback_data':'pref:roles:all'}],
                        [{'text':'All seniorities','callback_data':'pref:levels:all'},{'text':'Junior','callback_data':'pref:levels:junior'}],
                        [{'text':'Senior','callback_data':'pref:levels:senior'},{'text':'Lead / Principal','callback_data':'pref:levels:lead,principal'}],
                        [{'text':'Israel only','callback_data':'pref:region:israel'},{'text':'Broader locations','callback_data':'pref:region:broad'}]])

    def command(self, text):
        first, _, args = text.strip().partition(' ')
        command = first.split('@')[0].lower()
        args = args.strip()
        if command=='/start':
            self.state.set('chat_ready',True)
            self.send('Your hourly job feed is ready. New discoveries arrive with links; existing jobs are not repeated.\n\nUse /new, /preferences or /status. Browse every stored job with /jobs.',
                      [[{'text':'New jobs','callback_data':'cmd:new'},{'text':'Preferences','callback_data':'cmd:preferences'}],
                       [{'text':'Feed status','callback_data':'cmd:status'},{'text':'Help','callback_data':'cmd:help'}]])
        elif command=='/help':
            self.send(HELP)
        elif command=='/preferences':
            self.configure_preferences(args)
        elif command=='/quiet':
            self.configure_quiet(args)
        elif command=='/new':
            batch=self.state.con.execute('SELECT batch_id FROM delivery_parts WHERE sent_at IS NOT NULL ORDER BY batch_id DESC LIMIT 1').fetchone()
            if batch:
                self.feed_page(batch['batch_id'])
            else:
                self.send('No new jobs delivered yet. The hourly feed stays quiet when nothing new is found.')
        elif command in ('/jobs','/search'):
            self.browse(args)
        elif command in ('/job','/description'):
            self.show_job(args,full=command=='/description')
        elif command=='/refresh':
            self.number(args)
            self.send('Fetching the job description from LinkedIn…')
            self.api('POST','/jobs/'+args+'/refresh')
            self.show_job(args)
        elif command=='/save':
            self.save_job(args)
        elif command=='/applications':
            self.application_list(args)
        elif command=='/followups':
            self.application_list(due=True)
        elif command=='/stage':
            app_id, _, stage = args.partition(' ')
            self.number(app_id)
            if stage not in STAGES:
                raise ValueError('Use /stage APP_ID stage. See /help for stages.')
            result = self.api('PATCH','/applications/'+app_id,{'status':stage})
            self.send(f"Tracker #{result['id']}: {result['status']}\n{result['title']}")
        elif command=='/status':
            self.show_status()
        elif command=='/track':
            keywords, sep, location = args.partition('|')
            if not sep or not keywords.strip():
                raise ValueError('Use /track Software Engineer | Israel. A blank location after | means any location.')
            data = self.api('POST','/searches',{'keywords':keywords.strip(),'location':location.strip()})
            self.send(f"Hourly collection enabled. Search ID: {data['id']}\n{data['keywords']} · {data['location'] or 'Any location'}\nThe background collector will start when due. Use /searches to manage it.")
        elif command=='/searches':
            rows = self.api('GET','/searches')
            self.send('Searches · Israel time\n\n'+'\n\n'.join(f"#{r['id']} · {'Active' if r['enabled'] else 'Paused'} · {r['keywords']} · {r['location']}\nEvery {r['interval_minutes']} minutes\nLast successful search: {local_time(r.get('last_success'),True)}\nNext search: {local_time(r.get('next_due'),True)}\nCoverage: {'Limited' if r['status']=='partial' else r['status'].replace('_',' ')}" for r in rows) if rows else 'No collection searches yet. Use /track job title | location.')
        elif command=='/setsearch':
            search_id, _, criteria = args.partition(' ')
            if not search_id.isdigit():
                criteria=args; search_id=self.primary_search()
            self.number(search_id)
            keywords, separator, location = criteria.partition('|')
            if not separator or not keywords.strip():
                raise ValueError('Use /setsearch ID job title | location.')
            self.api('PATCH','/searches/'+search_id,{'keywords':keywords.strip(),'location':location.strip(),'interval_minutes':60})
            self.send('Search updated. The next collection will use the new criteria; previously delivered jobs will not be repeated.')
        elif command in ('/pause','/resume'):
            args=args or self.primary_search()
            self.api('PATCH','/searches/'+self.number(args),{'enabled':command=='/resume'})
            self.send('Search '+args+(' resumed.' if command=='/resume' else ' paused.'))
        elif command=='/collect':
            args=args or self.primary_search()
            self.number(args)
            self.send('Collecting jobs… This can take a few minutes.')
            data = self.api('POST','/searches/'+args+'/poll')
            self.send(f"Found: {data['found']}\nNew matches: {data['new']}\nPages: {data['pages']}\nCoverage limited: {data['coverage_limited']}\nWarning: {data.get('warning') or 'none'}")
        elif command=='/alerts':
            rows = self.api('GET','/alerts?limit=8')
            self.send('\n\n'.join(f"{friendly_alert(r)}\nLast seen: {local_time(r['updated_at'],True)}" for r in rows) or 'No open issues.')
        elif command=='/notify':
            if args not in ('on','off'):
                raise ValueError('Use /notify on or /notify off.')
            if args=='on' and not self.state.get('feed_initialized',False):
                cursor = 0
                for _ in range(20):
                    page = self.api('GET',f'/jobs?after_id={cursor}&limit=500')
                    cursor = page['next_after_id']
                    if len(page['items'])<500:
                        break
                else:
                    raise ValueError('Too much historical data to initialize notifications. Contact the server administrator.')
                jobs, offset = [], 0
                while True:
                    page = self.api('GET',f'/catalog?limit=200&offset={offset}')
                    jobs.extend(page['items']); offset += len(page['items'])
                    if not page['items'] or offset>=page['total']:
                        break
                self.state.seed_baseline(jobs,cursor)
            self.state.set('notify',args=='on')
            self.send('Notifications on. New jobs, important alerts, and due follow-ups are checked once a minute; existing jobs are not resent.' if args=='on' else 'Notifications paused. Collection continues, and queued new jobs will remain saved until you resume.')
        elif command.startswith('/'):
            self.send('Unknown command. Use /help.')
        elif text.strip():
            self.browse(text)

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message',{}) if callback else update.get('message',{})
        sender = callback.get('from',{}) if callback else message.get('from',{})
        chat = message.get('chat',{})
        if sender.get('id')!=self.owner or chat.get('type')!='private' or chat.get('id')!=self.owner:
            return
        self.state.set('chat_ready',True)
        try:
            if callback:
                self.telegram('answerCallbackQuery',{'callback_query_id':callback['id']})
                data = callback.get('data','').split(':')
                if callback.get('data','').startswith('quiet:'):
                    self.configure_quiet(callback['data'][6:])
                elif data[0]=='cmd' and len(data)==2 and data[1] in ('new','preferences','quiet','searches','status','help','notify on','notify off'):
                    self.command('/'+data[1])
                elif data[0]=='pref' and len(data)==3:
                    self.configure_preferences(data[1]+' '+data[2])
                elif data[0]=='page' and len(data)==3:
                    query = self.state.query(int(self.number(data[1])))
                    if query is None:
                        raise ValueError('This browsing session expired. Use /jobs again.')
                    self.browse(query,int(self.number(data[2])),int(data[1]),message['message_id'])
                elif data[0]=='feed' and len(data)==3:
                    self.feed_page(int(self.number(data[1])),int(self.number(data[2])),message['message_id'])
                elif data[0] in ('job','save','refresh') and len(data)==2:
                    self.command('/'+data[0]+' '+data[1])
            elif message.get('text'):
                self.command(message['text'])
        except ValueError as exc:
            self.send(str(exc))
        except ServiceError as exc:
            self.send(f"Request could not complete ({exc.status}): {exc.detail}\nTry again later or check /status.")

    @staticmethod
    def job_digest(jobs, heading='New job matches'):
        return f'{heading}: {len(jobs)}\n\n'+'\n\n'.join(job_card(job) for job in jobs)

    def notifications(self):
        if not self.state.get('notify',False) or not self.state.get('chat_ready',False):
            return
        quiet=self.quiet_active()
        if quiet and not self.state.get('quiet_started_at'):
            self.state.set('quiet_started_at',datetime.now(timezone.utc).isoformat())
        catchup=not quiet and bool(self.state.get('quiet_started_at'))
        if catchup:
            # Collection never stops overnight. Also request a fresh scan to cover
            # the interval since the latest hourly scan; normal API throttles apply.
            for search in self.api('GET','/searches'):
                if search['enabled']:
                    try:
                        self.api('POST',f"/searches/{search['id']}/poll")
                    except ServiceError as exc:
                        if exc.status not in (409,429):
                            raise
        cursor = self.state.get('cursor',0)
        for _ in range(5):
            page = self.api('GET',f'/jobs?after_id={cursor}&limit=500')
            self.state.enqueue(page['items'],page['next_after_id'])
            cursor=page['next_after_id']
            if len(page['items'])<500:
                break
        if quiet or self.quiet_active():
            return
        self.state.prepare_batch(catchup=catchup)
        batches=list(self.state.con.execute('SELECT DISTINCT batch_id FROM delivery_parts WHERE sent_at IS NULL ORDER BY batch_id LIMIT 10'))
        for batch in batches:
            batch_id=batch['batch_id']
            binding=self.state.con.execute('SELECT message_id FROM feed_messages WHERE batch_id=? LIMIT 1',(batch_id,)).fetchone()
            if binding is None:
                self.feed_page(batch_id)
            # All pages are now accessible through one durable message.
            with self.state.con:
                self.state.con.execute("UPDATE deliveries SET status='sent',sent_at=? WHERE batch_id=? AND status='pending'",(time.time(),batch_id))
                self.state.con.execute('UPDATE delivery_parts SET sent_at=COALESCE(sent_at,?) WHERE batch_id=?',(time.time(),batch_id))
            self.state.set('last_delivery_at',datetime.now(timezone.utc).isoformat())
        if catchup:
            self.state.set('quiet_started_at',None)
        for alert in self.api('GET','/alerts?limit=30'):
            key = 'alert:'+str(alert['id'])
            if not self.state.notice_sent(key) and not alert.get('acknowledged_at'):
                self.send('Feed notice\n'+friendly_alert(alert))
                self.state.mark_notice(key)
        for application in self.api('GET','/applications?due_only=true&limit=100')['items']:
            key = f"followup:{application['id']}:{application.get('follow_up_at')}"
            if not self.state.notice_sent(key,within=86400):
                self.send(f"Follow-up due: #{application['id']} · {application['title'][:180]}\n{application['url']}")
                self.state.mark_notice(key)
        # A stopped or stuck scheduler must not look like an empty job market.
        for search in self.api('GET','/searches'):
            if not search['enabled'] or not search.get('next_due'):
                continue
            overdue = time.time()-datetime.fromisoformat(search['next_due']).timestamp()
            key = 'stale-search:'+str(search['id'])
            if overdue>max(1200,search['interval_minutes']*60) and not self.state.notice_sent(key,within=86400):
                self.send(f"Search #{search['id']} is overdue: {search['keywords']}. Check /alerts and /status; LinkedIn may need reconnection.")
                self.state.mark_notice(key)
        self.state.set('api_failure_count',0)
        if self.state.get('api_down',False):
            self.send('The jobs service has recovered. Your hourly feed and queued deliveries are running again.')
            self.state.set('api_down',False)


def load_bot():
    os.umask(0o077)
    config = json.loads(Path(os.environ.get('BOT_CONFIG','/run/bot-config.json')).read_text())
    key = Path(os.environ.get('API_KEY_FILE','/run/jobs-api-key')).read_text().strip()
    return Bot(config['token'],config['allowed_user_id'],key,
               State(os.environ.get('BOT_STATE_DIR','/app/state')),
               os.environ.get('JOBS_API_URL','http://api:8765'))


def main():
    bot = load_bot()
    info = bot.telegram('getMe')
    webhook = bot.telegram('getWebhookInfo')
    if webhook.get('url'):
        print('Startup stopped: this bot has an existing webhook. No webhook was changed.',flush=True)
        raise SystemExit(1)
    print('Jobs Telegram bot running: @'+info['username'],flush=True)
    bot.configure_profile()
    next_notify = 0
    while True:
        try:
            updates = bot.telegram('getUpdates',{'offset':bot.state.get('offset',0),'timeout':25,
                                   'allowed_updates':['message','callback_query']},timeout=35)
            bot.state.set('last_poll',time.time())
            for update in updates:
                # Advance first so a failed reply cannot replay a mutating command.
                bot.state.set('offset',update['update_id']+1)
                bot.handle(update)
            if time.time()>=next_notify:
                bot.notifications()
                backup_database(bot.state.folder/'bot.sqlite3',bot.state.folder/'backups','bot')
                next_notify = time.time()+60
        except ServiceError as exc:
            # Do not log exception URLs: Telegram URLs contain the bot token.
            print('Service unavailable; status='+str(exc.status),flush=True)
            if exc.service=='jobs':
                failures=bot.state.get('api_failure_count',0)+1
                bot.state.set('api_failure_count',failures)
                if failures>=3 and not bot.state.get('api_down',False) and bot.state.get('notify',False) and bot.state.get('chat_ready',False) and not bot.quiet_active():
                    try:
                        bot.send('The jobs API is not responding. New-job delivery is paused; queued jobs are retained. The server will keep retrying.')
                        bot.state.set('api_down',True)
                    except ServiceError:
                        pass
            time.sleep(min(60,max(exc.retry_after,10 if exc.status!=409 else 60)))
        except Exception:
            print('Bot operation failed; private payloads were not logged.',flush=True)
            time.sleep(10)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        print('Bot startup failed; check private configuration and connectivity.',flush=True)
        raise SystemExit(1)
