import json
import html
import feed_ui
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
from i18n import tr, source, HELP_HE, KEY_ALIASES, preference_value, command_alias, is_hebrew_command


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
            '')]
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
        accepted.sort(key=lambda job:feed_ui.rank(job,prefs))
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
                heading='Catch-up after quiet hours' if catchup else ('Hourly update' if self.get('notification_interval_minutes',60)==60 else 'Scheduled job update')
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
/preferences — filters; combine settings with semicolons or newlines
/quiet — choose quiet hours, custom times or turn them off
/language he|en|auto — Hebrew, English or Telegram language detection
/job JOB_ID — details and description preview
/description JOB_ID — full stored description
/refresh JOB_ID — fetch a description from LinkedIn
/status — database and collector health
/track job title | location — hourly searches; paste multiple lines to add a batch
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
/interval 120 — minutes between Telegram job digests (15–1440); collection is unchanged

Stages: saved, reviewing, applied, interviewing, offer, rejected, withdrawn, archived.
Tracking updates do not submit applications or contact employers.
Search results and descriptions come from the database; collection is separate.'''

STAGES = {'saved','reviewing','applied','interviewing','offer','rejected','withdrawn','archived'}


class Bot:
    def __init__(self, token, owner, api_key, state, api_base='http://api:8765'):
        self.token, self.owner, self.api_key, self.state = token, int(owner), api_key, state
        self.api_base = api_base.rstrip('/')
        self.last_send = 0.0

    @property
    def language(self):
        selected=self.state.get('language','auto')
        return selected if selected in ('en','he') else self.state.get('detected_language','en')

    def t(self,text,**fields):
        return tr(text,self.language,**fields)

    def time_label(self,value,clock_only=False):
        return local_time(value,clock_only,self.language)

    def labels(self,values):
        return ', '.join(self.t(value) for value in values)

    def local_buttons(self,buttons):
        return [[{**button,'text':self.t(button['text'])} for button in row] for row in (buttons or [])]

    def configure_language(self,args=''):
        aliases={'עברית':'he','אנגלית':'en','אוטומטי':'auto','iw':'he'}
        selected=aliases.get(args.strip().casefold(),args.strip().casefold())
        if selected:
            if selected not in ('he','en','auto'):
                raise ValueError('Use /language he, /language en or /language auto.')
            self.state.set('language',selected)
            self.configure_owner_commands()
        current=self.state.get('language','auto')
        self.send(self.t('language_prompt',selected=self.t('language_name_'+current)),
            [[{'text':'עברית','callback_data':'language:he'},{'text':'English','callback_data':'language:en'},
              {'text':'אוטומטי · Auto','callback_data':'language:auto'}]])

    def command_definitions(self):
        return [('start','Open your private job feed'),('new','Latest update with page navigation'),
            ('jobs','Browse all stored jobs'),('preferences','Choose roles, levels and location'),
            ('quiet','Choose quiet hours and timezone'),('language','Choose English, Hebrew or automatic'),
            ('status','Feed and search health'),('searches','Your collection searches'),
            ('track','Add search: title | location'),('setsearch','Edit search: ID title | location'),
            ('collect','Run a search now'),('pause','Pause collection for a search'),
            ('resume','Resume collection for a search'),('notify','Turn notifications on or off'),
            ('interval','Choose minutes between job notifications'),
            ('job','Job details: JOB_ID'),('description','Stored description: JOB_ID'),
            ('refresh','Fetch description: JOB_ID'),('save','Track an application: JOB_ID'),
            ('applications','Your application tracker'),('stage','Update application: APP_ID stage'),
            ('followups','Due follow-ups'),('alerts','Collection alerts'),('help','All commands and examples')]

    def configure_owner_commands(self):
        selected=self.state.get('language','auto')
        for code in ('','en','he'):
            language=selected if selected in ('en','he') else ('he' if code=='he' else 'en')
            self.telegram('setMyCommands',{'scope':{'type':'chat','chat_id':self.owner},
                'language_code':code,'commands':[{'command':name,'description':tr(description,language)}
                    for name,description in self.command_definitions()]})

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
        commands=self.command_definitions()
        payload={'commands':[{'command':name,'description':description} for name,description in commands]}
        self.telegram('setMyCommands',payload)
        self.telegram('setMyCommands',{'language_code':'he','commands':[
            {'command':name,'description':tr(description,'he')} for name,description in commands]})
        self.configure_owner_commands()
        self.telegram('setChatMenuButton',{'menu_button':{'type':'commands'}})
        self.telegram('setChatMenuButton',{'chat_id':self.owner,'menu_button':{'type':'commands'}})
        self.telegram('setMyDescription',{'description':
            'Your private job-search assistant. Discover new LinkedIn jobs hourly, browse updates with page buttons, '
            'choose role and location filters, and set your own quiet hours. Collection continues while notifications '
            'are quiet, with a catch-up update afterward. Browse your saved jobs and track applications from one chat.'})
        self.telegram('setMyShortDescription',{'short_description':
            'Private hourly job alerts with links, filters, quiet hours and paginated catch-up updates.'})
        self.telegram('setMyDescription',{'language_code':'he','description':
            'העוזר הפרטי שלך לחיפוש עבודה. משרות חדשות מ-LinkedIn מדי שעה, עם קישורים וכפתורי עמודים. '
            'בחרו תפקידים, רמות ותק, מיקומים ושעות שקטות. האיסוף ממשיך בשעות השקטות, ואחריהן נשלחת השלמה. '
            'אפשר לחפש במאגר ולעקוב אחרי מועמדויות מאותו צ׳אט.'})
        self.telegram('setMyShortDescription',{'language_code':'he','short_description':
            'פיד משרות פרטי: עדכונים שעתיים, קישורים, סינון, שעות שקטות ומעקב מועמדויות.'})
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

    def send(self, text, buttons=None, rich=False):
        text = self.t(str(text))
        if rich and len(text.encode("utf-16-le"))//2>3500:
            raise ValueError("Feed page exceeds Telegram message budget")
        text=text[:14000]
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
            if rich:
                data['parse_mode']='HTML'
            if buttons and index==len(parts)-1:
                data['reply_markup'] = {'inline_keyboard':self.local_buttons(buttons)}
            result = self.telegram('sendMessage',data)
            self.last_send = time.monotonic()
        return result

    def edit(self, message_id, text, buttons, rich=False):
        try:
            return self.telegram('editMessageText',{
                'chat_id':self.owner,'message_id':message_id,'text':text,
                **({'parse_mode':'HTML'} if rich else {}),
                'link_preview_options':{'is_disabled':True},
                'reply_markup':{'inline_keyboard':self.local_buttons(buttons)}})
        except ServiceError as exc:
            if exc.status==400 and 'message is not modified' in exc.detail.lower():
                return
            raise

    def feed_snapshot(self,batch_id,category_key='all'):
        rows=list(self.state.con.execute('SELECT * FROM delivery_parts WHERE batch_id=? ORDER BY part',(batch_id,)))
        if not rows:
            raise ValueError('This update is no longer available.')
        ids=list(dict.fromkeys(job_id for row in rows for job_id in json.loads(row['job_ids'])))
        prefs=self.state.preferences(); jobs=[]
        for job_id in ids:
            row=self.state.con.execute('SELECT payload,discovered_at FROM deliveries WHERE job_id=?',(job_id,)).fetchone()
            if row:
                job=json.loads(row['payload'])
                if match_job(job,prefs,now=datetime.fromtimestamp(row['discovered_at'],timezone.utc))[0]:
                    jobs.append(job)
        jobs.sort(key=lambda job:feed_ui.rank(job,prefs))
        counts={}
        for job in jobs:
            key=feed_ui.category(job); counts[key]=counts.get(key,0)+1
        if category_key!='all' and category_key not in counts:
            raise ValueError('No matches in this category. Use /new to reopen the update.')
        selected=[job for job in jobs if category_key=='all' or feed_ui.category(job)==category_key]
        return rows,jobs,counts,feed_ui.paginate(selected,prefs,self.language) or [[]],len(selected)

    def validate_feed_message(self,batch_id,message_id):
        binding=self.state.con.execute('SELECT batch_id FROM feed_messages WHERE message_id=?',(message_id,)).fetchone()
        if binding is None or binding['batch_id']!=batch_id:
            raise ValueError('This update is no longer available. Use /new.')

    def feed_page(self, batch_id, part=1, message_id=None, category_key='all'):
        rows,jobs,counts,pages,total=self.feed_snapshot(batch_id,category_key)
        if part<1:
            raise ValueError('This page does not exist.')
        # Older keyboards may refer to pages removed by tighter preferences.
        part=min(part,len(pages))
        original=rows[0]['text'].split('\n',1)[0]
        heading='Catch-up after quiet hours' if original.startswith('Catch-up') else ('Hourly update' if self.state.get('notification_interval_minutes',60)==60 else 'Scheduled job update')
        stamp=original.split(' · ',1)[-1].replace(' Israel time','')
        noun='newly found job' if total==1 else 'newly found jobs'
        text='💼 <b>'+self.t(heading)+'</b> · '+html.escape(stamp)+'\n'
        text+=f'<b>{total} {self.t(noun)}</b> · {self.t("Page")} {part}/{len(pages)}'
        if len(jobs)<rows[0]['total_jobs']:
            text+='\n'+self.t('Matching your current preferences')
        if category_key!='all':
            text+='\n'+feed_ui.label(category_key,self.language)
        text+='\n\n'+('\n\n'.join(pages[part-1]) or self.t('No jobs match your current preferences. Use /preferences to adjust.'))
        text+='\n\n'+self.t('Tap a title to open LinkedIn.')
        buttons=[]
        if len(counts)>1:
            tabs=[('all',self.t('All'),len(jobs))]+[(key,feed_ui.label(key,self.language),count) for key,count in counts.items()]
            tabs=[{'text':('✓ ' if key==category_key else '')+f'{label} {count}',
                   'callback_data':f'feed:{batch_id}:1:{key}'} for key,label,count in tabs]
            buttons.extend([tabs[index:index+3] for index in range(0,len(tabs),3)])
        if len(pages)>1:
            nav=[]
            def button(label,page):
                return {'text':label,'callback_data':f'feed:{batch_id}:{page}:{category_key}'}
            if part>2:
                nav.append(button('«',1))
            if part>1:
                nav.append(button('‹ Previous',part-1))
            nav.append({'text':f'{part}/{len(pages)} ▦','callback_data':f'jump:{batch_id}:{category_key}:{part}'})
            if part<len(pages):
                nav.append(button('Next ›',part+1))
            if part<len(pages)-1:
                nav.append(button('»',len(pages)))
            buttons.append(nav)
        if message_id is not None:
            self.validate_feed_message(batch_id,message_id)
            self.edit(message_id,text,buttons,rich=True)
        else:
            result=self.send(text,buttons,rich=True)
            with self.state.con:
                self.state.con.execute('INSERT INTO feed_messages VALUES (?,?)',(result['message_id'],batch_id))
        return [{'part':index+1} for index in range(len(pages))]

    def feed_jump(self,batch_id,category_key,current,message_id):
        self.validate_feed_message(batch_id,message_id)
        _,_,_,pages,_=self.feed_snapshot(batch_id,category_key)
        size=len(pages)
        choices=list(range(1,size+1)) if size<=50 else sorted({1,size,*range(max(1,current-3),min(size,current+3)+1),*[1+round(i*(size-1)/12) for i in range(13)]})
        buttons=[{'text':('✓ ' if page==current else '')+str(page),'callback_data':f'feed:{batch_id}:{page}:{category_key}'} for page in choices]
        keyboard=[buttons[index:index+5] for index in range(0,len(buttons),5)]
        keyboard.append([{'text':self.t('Back to jobs'),'callback_data':f'feed:{batch_id}:{current}:{category_key}'}])
        self.edit(message_id,self.t('Choose a page')+f' · {size}',keyboard)

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
        lines = [self.t('Stored jobs {start}–{end} of {total}',start=offset+1,end=offset+len(data['items']),total=data['total']) + (f' · {query}' if query else '')]
        buttons = []
        for job in data['items']:
            lines.append(f"\n{job['id']} · {job['title'][:160]}\n{(job.get('company') or self.t('Company unknown'))[:100]} · {(job.get('location') or self.t('Location unknown'))[:180]}\n{job['url']}")
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
                fields.append(self.t('Full description: /description {id}',id=job_id))
        else:
            fields += ['',self.t('Description: {status}. Use the fetch button below.',status=self.t(job['details_status']))]
        self.send('\n'.join(str(v) for v in fields if v is not None),
                  [[{'text':'Fetch description','callback_data':'refresh:'+job_id},
                    {'text':'Save job','callback_data':'save:'+job_id}]])

    def save_job(self, job_id):
        try:
            result = self.api('POST','/applications',{'job_id':self.number(job_id)})
            self.send(self.t('Saved to your tracker. Application ID: {id}\n{title}',id=result['id'],title=result['title']))
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
        lines = [f"{self.t('Follow-ups due' if due else 'Applications')}: {data['total']}"]
        for row in data['items']:
            lines.append(f"\n#{row['id']} · {self.t(row['status'])} · {row['title'][:150]}\n{row.get('company') or ''}\n{row['url']}"+
                         ('\n'+self.t('Follow-up: {time}',time=self.time_label(row['follow_up_at'])) if row.get('follow_up_at') else ''))
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
        args='off' if args.strip() in ('כיבוי','כבוי') else args
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
            status=self.t('Quiet now' if self.quiet_active() else 'Outside quiet hours')
        else:
            schedule=self.t('Off · choose your hours below')
            status=''
        explanation=(self.t('quiet_explanation') if self.language=='he' else
            'Collection continues. Automatic messages wait until quiet hours end, then new jobs from the quiet period arrive together with page buttons. '
            'Your commands still work at any time.\n\nCustom: /quiet 23:00 08:00\nOptional timezone: /quiet 23:00 08:00 Europe/London')
        self.send(self.t('Quiet hours')+'\n'+schedule+('\n'+status if status else '')+'\n\n'+explanation,
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
        lines=[self.t('Your job feed · Israel time'),
               self.t('Delivery: {state}',state=self.t('On' if self.state.get('notify',False) else 'Paused')),
               self.t('Quiet hours: {state}',state=self.t('Active · automatic messages queued' if self.quiet_active() else
                   ('Scheduled · /quiet' if self.state.get('quiet_hours',{}).get('enabled') else 'Off · /quiet to choose'))),
               'LinkedIn: '+self.t(connection),
               self.t('Roles: {roles} · Levels: {levels}',roles=self.labels(prefs['roles']),levels=self.labels(prefs['levels'])),
               self.t('Location rule: {rule}',rule=self.t('Israel explicitly listed' if prefs['region']=='israel' else 'Broader locations allowed'))]
        lines.append(self.t('Job notifications every {minutes} minutes',minutes=self.state.get('notification_interval_minutes',60)))
        next_digest=self.state.get('next_digest_at')
        if next_digest:
            lines.append(self.t('Next digest: {time}',time=self.time_label(datetime.fromtimestamp(next_digest,timezone.utc).isoformat(),True)))
        for row in active[:3]:
            lines+=['',f"{row['keywords']} · {row['location'] or self.t('Any location')}",
                    self.t('Last successful search: {time}',time=self.time_label(row.get('last_success'),True)),
                    self.t('Next search: {time}',time=self.time_label(row.get('next_due'),True))]
            if row.get('status')=='partial':
                lines.append(self.t('Coverage: limited · some results may be missing'))
        if not active:
            lines.append(self.t('Collection is paused. Use /resume to restart your search.'))
        filtered=self.state.con.execute("SELECT count(*) FROM deliveries WHERE status='filtered'").fetchone()[0]
        lines+=['',self.t('Saved jobs: {jobs} · Filtered from feed: {filtered}',jobs=data['jobs'],filtered=filtered),
                self.t('Waiting to send: {count}',count=self.state.pending_count()),
                self.t('Last delivery: {time}',time=self.time_label(self.state.get('last_delivery_at'),True)),
                self.t('Descriptions: {pending} preparing · {failed} unavailable',pending=state.get('descriptions_pending',state.get('details_pending_or_failed',0)),failed=state.get('descriptions_failed',0)),
                self.t('Issues: {issues} · Follow-ups due: {due}',issues=state['open_alerts'],due=data['follow_ups_due'])]
        self.send('\n'.join(lines),[[{'text':'New jobs','callback_data':'cmd:new'},
                                    {'text':'Preferences','callback_data':'cmd:preferences'}],
                                   [{'text':'Searches','callback_data':'cmd:searches'},
                                    {'text':'Pause alerts' if self.state.get('notify',False) else 'Resume alerts',
                                     'callback_data':'cmd:notify '+('off' if self.state.get('notify',False) else 'on')}]])

    def configure_preferences(self,args=''):
        prefs=self.state.preferences()
        if args:
            updates=[part.strip() for part in re.split(r';|\r?\n',args) if part.strip()]
            if not updates:
                raise ValueError('Add a setting after /preferences, for example roles software; age 72.')
            seen=set()
            for update in updates:
                # Also accept several existing /preferences lines pasted together.
                update=re.sub(r'^/preferences(?:@\w+)?\s+', '',update,flags=re.IGNORECASE)
                parts=update.split(maxsplit=1)
                if len(parts)!=2:
                    raise ValueError('Use setting value, for example roles software; levels senior; age 72. Nothing changed.')
                key,value=parts[0].casefold(),parts[1].strip().casefold()
                key=KEY_ALIASES.get(key,key)
                if key not in ('roles','levels','include','exclude'):
                    value=preference_value(value)
                if key in seen:
                    raise ValueError(self.t('Specify {key} only once. Nothing changed.',key=self.t(key)))
                seen.add(key)
                if key in ('roles','levels'):
                    choices=[preference_value(part.strip()) for part in value.split(',') if part.strip()]
                    allowed=ROLES if key=='roles' else LEVELS
                    if not choices or any(part not in allowed for part in choices):
                        raise ValueError(self.t('Choose {key}: {choices}. Nothing changed.',key=self.t(key),choices=self.labels(sorted(allowed))))
                    prefs[key]=list(dict.fromkeys(choices))
                elif key=='region' and value in ('israel','broad'):
                    prefs[key]=value
                elif key in ('include','exclude'):
                    prefs[key]=[part.strip() for part in value.split(',') if part.strip()][:20] if value not in ('clear','נקה') else []
                elif key=='age' and value.isascii() and value.isdigit() and 1<=int(value)<=168:
                    prefs['max_age_hours']=int(value)
                elif key=='other_scope' and value in ('technical','all'):
                    prefs[key]=value
                elif key=='unknown' and value in ('keep','hide'):
                    prefs['keep_unknown_level']=value=='keep'
                else:
                    raise ValueError('Use /preferences roles software,data; levels senior,lead; region israel; age 72; unknown keep; include clear; exclude words. Nothing changed.')
            # Validate the whole command before saving or re-planning deliveries.
            self.state.set_preferences(prefs)
        text=('Feed preferences\n\nRoles: '+', '.join(prefs['roles'])+'\nSeniority: '+', '.join(prefs['levels'])+
              '\nLocation: '+('Israel must be explicitly listed' if prefs['region']=='israel' else 'Broader regions and remote locations allowed')+
              f"\nPosting age limit: {prefs['max_age_hours']} hours when LinkedIn supplies usable age information"+
              '\nUnknown seniority: '+('kept' if prefs['keep_unknown_level'] else 'hidden')+
              '\nInclude title words: '+(', '.join(prefs['include']) or 'Any')+
              '\nExclude title words: '+(', '.join(prefs['exclude']) or 'None')+
              '\n\nSoftware includes backend, frontend and full stack. Title-based categories are rules, not a CV match score. '
              'Posting dates that LinkedIn does not provide stay unknown.\n\n'
              'Combine settings: /preferences roles software,data; levels senior,lead; age 72. '
              'Newlines and pasted /preferences commands also work. All settings are checked before anything changes. '
              'Previously filtered discoveries are rechecked when you change preferences; sent jobs stay deduplicated.')
        if self.language=='he':
            text=self.t('preferences_summary',roles=self.labels(prefs['roles']),levels=self.labels(prefs['levels']),
                region=self.t('Israel must be explicitly listed' if prefs['region']=='israel' else 'Broader regions and remote locations allowed'),
                age=prefs['max_age_hours'],unknown=self.t('kept' if prefs['keep_unknown_level'] else 'hidden'),
                include=', '.join(prefs['include']) or self.t('Any'),exclude=', '.join(prefs['exclude']) or self.t('None'))
        text+='\n'+self.t('Uncategorized roles: related engineering only' if prefs['other_scope']=='technical' else 'Uncategorized roles: all titles')
        self.send(text,[[{'text':'Software roles','callback_data':'pref:roles:software'},{'text':'All roles','callback_data':'pref:roles:all'}],
                        [{'text':'All seniorities','callback_data':'pref:levels:all'},{'text':'Junior','callback_data':'pref:levels:junior'}],
                        [{'text':'Senior','callback_data':'pref:levels:senior'},{'text':'Lead / Principal','callback_data':'pref:levels:lead,principal'}],
                        [{'text':'Israel only','callback_data':'pref:region:israel'},{'text':'Broader locations','callback_data':'pref:region:broad'}],
                        [{'text':'Related engineering only','callback_data':'pref:other_scope:technical'},
                         {'text':'All uncategorized titles','callback_data':'pref:other_scope:all'}]])

    def track_searches(self,args):
        lines=[line.strip() for line in re.split(r';|\r?\n',args) if line.strip()]
        if not lines or len(lines)>20:
            raise ValueError('Use /track job title | location. Paste up to 20 lines in one message.')
        searches=[]
        for line in lines:
            line=re.sub(r'^/track(?:@\w+)?\s+','',line,flags=re.IGNORECASE)
            if line.count('|')!=1:
                raise ValueError('Each line must be /track job title | location. Nothing added.')
            keywords,location=[part.strip() for part in line.split('|',1)]
            if not keywords or keywords.startswith('/') or len(keywords)>200 or len(location)>200:
                raise ValueError('Use a non-empty title and at most 200 characters per title/location. Nothing added.')
            searches.append({'keywords':keywords,'location':location})
        result=self.api('POST','/searches/bulk',{'searches':searches})
        text=[self.t('Searches updated · {created} added · {existing} already tracked',created=result['created'],existing=result['existing'])]
        for row in result['items']:
            state='Hourly collection enabled' if row['created'] else ('Already tracked' if row['enabled'] else 'Already tracked · paused')
            text.append(f"\n#{row['id']} · {row['keywords']} | {row['location'] or self.t('Any location')}\n{self.t(state)}")
        text.append('\n'+self.t('New searches start in the background. Existing schedules and pause settings are preserved. Use /searches to manage them.'))
        self.send('\n'.join(text))

    def command(self, text):
        if self.state.get('language','auto')=='auto' and is_hebrew_command(text):
            self.state.set('detected_language','he')
        text=command_alias(text)
        parts=text.strip().split(maxsplit=1)
        first=parts[0] if parts else ''
        args=parts[1] if len(parts)>1 else ''
        command = first.split('@')[0].lower()
        args = args.strip()
        if command=='/start':
            self.state.set('chat_ready',True)
            self.send('Your hourly job feed is ready. New discoveries arrive with links; existing jobs are not repeated.\n\nUse /new, /preferences or /status. Browse every stored job with /jobs.',
                      [[{'text':'New jobs','callback_data':'cmd:new'},{'text':'Preferences','callback_data':'cmd:preferences'}],
                       [{'text':'Feed status','callback_data':'cmd:status'},{'text':'Help','callback_data':'cmd:help'}]])
        elif command=='/help':
            self.send(HELP_HE if self.language=='he' else HELP)
        elif command=='/language':
            self.configure_language(args)
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
            self.send(self.t('Tracker #{id}: {status}\n{title}',id=result['id'],status=self.t(result['status']),title=result['title']))
        elif command=='/status':
            self.show_status()
        elif command=='/track':
            self.track_searches(args)
        elif command=='/searches':
            rows = self.api('GET','/searches')
            lines=[self.t('Searches · Israel time')]
            for row in rows:
                lines+=['',f"#{row['id']} · {self.t('Active' if row['enabled'] else 'Paused search')} · {row['keywords']} · {row['location']}",
                    self.t('Every {minutes} minutes',minutes=row['interval_minutes']),
                    self.t('Last successful search: {time}',time=self.time_label(row.get('last_success'),True)),
                    self.t('Next search: {time}',time=self.time_label(row.get('next_due'),True)),
                    self.t('Coverage: {status}',status=self.t('Limited' if row['status']=='partial' else row['status'].replace('_',' ')))]
            self.send('\n'.join(lines) if rows else 'No collection searches yet. Use /track job title | location.')
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
            self.send(self.t('Search {id} resumed.' if command=='/resume' else 'Search {id} paused.',id=args))
        elif command=='/collect':
            args=args or self.primary_search()
            self.number(args)
            self.send('Collecting jobs… This can take a few minutes.')
            data = self.api('POST','/searches/'+args+'/poll')
            self.send(self.t('Found: {found}\nNew matches: {new}\nPages: {pages}\nCoverage limited: {limited}\nWarning: {warning}',
                found=data['found'],new=data['new'],pages=data['pages'],limited=data['coverage_limited'],warning=data.get('warning') or self.t('None')))
        elif command=='/alerts':
            rows = self.api('GET','/alerts?limit=8')
            self.send('\n\n'.join(self.alert_label(r)+'\n'+self.t('Last seen: {time}',time=self.time_label(r['updated_at'],True)) for r in rows) or 'No open issues.')
        elif command=='/interval':
            self.configure_interval(args)
        elif command=='/notify':
            args={'הפעלה':'on','פעיל':'on','כיבוי':'off','כבוי':'off'}.get(args,args)
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
            self.send(self.t('Job notifications every {minutes} minutes',minutes=self.state.get('notification_interval_minutes',60)) if args=='on' else 'Notifications paused. Collection continues, and queued new jobs will remain saved until you resume.')
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
        code=sender.get('language_code','').split('-')[0].lower()
        if code:
            self.state.set('detected_language','he' if code in ('he','iw') else 'en')
        self.state.set('chat_ready',True)
        try:
            if callback:
                self.telegram('answerCallbackQuery',{'callback_query_id':callback['id']})
                data = callback.get('data','').split(':')
                if data[0]=='language' and len(data)==2:
                    self.configure_language(data[1])
                elif callback.get('data','').startswith('quiet:'):
                    self.configure_quiet(callback['data'][6:])
                elif data[0]=='cmd' and len(data)==2 and data[1] in ('new','preferences','quiet','language','searches','status','help','notify on','notify off'):
                    self.command('/'+data[1])
                elif data[0]=='pref' and len(data)==3:
                    self.configure_preferences(data[1]+' '+data[2])
                elif data[0]=='page' and len(data)==3:
                    query = self.state.query(int(self.number(data[1])))
                    if query is None:
                        raise ValueError('This browsing session expired. Use /jobs again.')
                    self.browse(query,int(self.number(data[2])),int(data[1]),message['message_id'])
                elif data[0]=='feed' and len(data) in (3,4):
                    self.feed_page(int(self.number(data[1])),int(self.number(data[2])),message['message_id'],data[3] if len(data)==4 else 'all')
                elif data[0]=='jump' and len(data)==4:
                    self.feed_jump(int(self.number(data[1])),data[2],int(self.number(data[3])),message['message_id'])
                elif data[0] in ('job','save','refresh') and len(data)==2:
                    self.command('/'+data[0]+' '+data[1])
            elif message.get('text'):
                self.command(message['text'])
        except ValueError as exc:
            self.send(str(exc))
        except ServiceError as exc:
            self.send(self.t('Request could not complete ({status}): {detail}\nTry again later or check /status.',status=exc.status,
                detail=self.t(exc.detail)))

    def alert_label(self,alert):
        text=friendly_alert(alert)
        return self.t(text) if self.language=='en' or tr(text,'he')!=text else self.t('open_alerts')+'\n'+text

    @staticmethod
    def job_digest(jobs, heading='New job matches'):
        return f'{heading}: {len(jobs)}\n\n'+'\n\n'.join(job_card(job) for job in jobs)

    def configure_interval(self, args=''):
        value=args.strip()
        if value:
            if not value.isascii() or not value.isdigit() or not 15<=int(value)<=1440:
                raise ValueError(self.t('Use /interval MINUTES, from 15 to 1440.'))
            minutes=int(value)
            with self.state.con:
                for key,item in {'notification_interval_minutes':minutes,'next_digest_at':time.time()+minutes*60}.items():
                    self.state.con.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                           (key,json.dumps(item)))
        self.send(self.t('Job notifications every {minutes} minutes',minutes=self.state.get('notification_interval_minutes',60))+
                  '\n'+self.t('Only Telegram job digests change. Collection schedules, important alerts and follow-up reminders stay unchanged. Quiet hours and catch-up still apply.'))

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
        now=time.time()
        interval=self.state.get('notification_interval_minutes',60)*60
        next_digest=self.state.get('next_digest_at')
        if next_digest is None and self.state.get('last_delivery_at'):
            last=datetime.fromisoformat(self.state.get('last_delivery_at')).timestamp()
            next_digest=last+interval
        retry=bool(self.state.con.execute('SELECT 1 FROM delivery_parts WHERE sent_at IS NULL LIMIT 1').fetchone())
        if catchup or retry or next_digest is None or now>=next_digest:
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
            self.state.set('next_digest_at',now+interval)
        if catchup:
            self.state.set('quiet_started_at',None)
        for alert in self.api('GET','/alerts?limit=30'):
            if str(alert.get('alert_key','')).startswith('details:'):
                continue
            key = 'alert:'+str(alert['id'])
            if not self.state.notice_sent(key) and not alert.get('acknowledged_at'):
                self.send(self.t('Feed notice')+'\n'+self.alert_label(alert))
                self.state.mark_notice(key)
        for application in self.api('GET','/applications?due_only=true&limit=100')['items']:
            key = f"followup:{application['id']}:{application.get('follow_up_at')}"
            if not self.state.notice_sent(key,within=86400):
                self.send(self.t('Follow-up due: #{id} · {title}\n{url}',id=application['id'],title=application['title'][:180],url=application['url']))
                self.state.mark_notice(key)
        # A stopped or stuck scheduler must not look like an empty job market.
        for search in self.api('GET','/searches'):
            if not search['enabled'] or not search.get('next_due'):
                continue
            overdue = time.time()-datetime.fromisoformat(search['next_due']).timestamp()
            key = 'stale-search:'+str(search['id'])
            if overdue>max(1200,search['interval_minutes']*60) and not self.state.notice_sent(key,within=86400):
                self.send(self.t('Search #{id} is overdue: {keywords}. Check /alerts and /status; LinkedIn may need reconnection.',id=search['id'],keywords=search['keywords']))
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
