"""Explainable title/location filters and source-aware presentation."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from i18n import tr, source

ROLES = {'software','backend','frontend','fullstack','data','devops','ml','security','embedded','qa','support','research','management','other','all'}
LEVELS = {'intern','junior','mid','senior','lead','principal','all'}
DEFAULT_PREFERENCES = {'roles':['software'],'levels':['all'],'region':'israel',
                       'include':[],'exclude':[],'max_age_hours':48,'keep_unknown_level':True,'other_scope':'technical'}
KEYWORD_EQUIVALENTS=[('frontend','front end','front-end','פרונטאנד','פרונט','צד לקוח'),
    ('full stack','fullstack','full-stack','פול סטאק','פולסטאק','פול-סטאק'),
    ('web developer','web development','מפתח ווב','מפתחת ווב','מפתח אתרים','מפתחת אתרים','פיתוח אתרים'),
    ('marketing','שיווק'),('product manager','מנהל מוצר','מנהלת מוצר'),
    ('technical support','תמיכה טכנית'),('python','פייתון'),
    ('backend','back end','back-end','בקאנד','בק אנד','צד שרת')]


def role_of(title):
    title=title.casefold()
    patterns=[('management',r'\b(director|head of|engineering manager|vice president|vp(?: of)? (?:engineering|sales)|chief\s+\w+\s+officer|מנהל(?:ת)? פיתוח|מנהל(?:ת)? הנדסה|ראש מחלקה)\b'),
              ('support',r'\b(support|customer success|sales engineer|solutions engineer|תמיכה|הצלחת לקוח)\b'),
              ('qa',r'\b(validation|verification|quality assurance|qa|test engineer|testing engineer|בדיקות|בודק(?:ת)?|אוטומציה|אבטחת איכות)\b'),
              ('embedded',r'\b(firmware|embedded|silicon|hardware|asic|fpga|low[ -]level|linux kernel|kernel engineer|device drivers?|bsp|board support package|משובצ(?:ת|ות)|אמבדד|קושחה|חומרה|ליבת לינוקס|קרנל|דרייבר(?:ים)?|מנהלי? התקן|רמה נמוכה)\b'),
              ('security',r'\b(offensive|penetration|security engineer|cybersecurity|אבטחת מידע|סייבר)\b'),
              ('devops',r'\b(devops|site reliability|sre|platform engineer|cloud engineer|דבאופס|תשתיות ענן)\b'),
              ('data',r'\b(data engineer|data scientist|analytics engineer|דאטה|מהנדס(?:ת)? נתונים|מדען נתונים|מדענית נתונים)\b'),
              ('research',r'\b(researcher|research scientist|algorithms? engineer|control algorithms|חוקר(?:ת)?|אלגוריתמים)\b'),
              ('ml',r'\b(ai engineer|ai researcher|machine learning|ml engineer|agentic ai|למידת מכונה|בינה מלאכותית)\b'),
              ('fullstack',r'\b(full[ -]?stack|פול[ -]?סטאק)\b'),('backend',r'\b(back[ -]?end|בק[ -]?אנד|צד שרת)\b'),
              ('frontend',r'\b(front[ -]?end|פרונט(?:אנד)?|צד לקוח)\b'),
              ('software',r'\b(software|developer|programmer|development engineer|תוכנה|תכנות|מתכנת(?:ת)?|מפתח(?:ת)?)\b')]
    return next((role for role,pattern in patterns if re.search(pattern,title)),'other')


def level_of(job):
    text=(job.get('seniority') or job.get('title') or '').casefold()
    for level,pattern in [('intern',r'\b(intern|internship|student|מתמח(?:ה|ים)|סטודנט(?:ית)?)\b'),('principal',r'\b(principal|staff|distinguished|פרינסיפל)\b'),
                          ('lead',r'\b(lead|team leader|tech lead|ראש(?:ת)? צוות|מוביל(?:ת)? צוות)\b'),('senior',r'\b(senior|sr\.?|experienced|בכיר(?:ה|ים|ות)?|סניור|מנוס(?:ה|ים))\b'),
                          ('junior',r'\b(junior|jr\.?|entry[- ]level|graduate|associate|זוטר(?:ה|ים|ות)?|ג[׳\x27]?וניור)\b'),('mid',r'\b(mid[- ]level|intermediate|ביניים)\b')]:
        if re.search(pattern,text):
            return level
    return None


def parse_time(value):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt if dt.tzinfo is not None else None
    except ValueError:
        return None


def local_time(value, clock_only=False, language='en'):
    if isinstance(value,str) and value.endswith(' UTC'):
        value=value[:-4]+'+00:00'
    dt=parse_time(value)
    if dt is None:
        return tr('Not yet',language) if not value else str(value)
    local=dt.astimezone(ZoneInfo('Asia/Jerusalem'))
    today=datetime.now(ZoneInfo('Asia/Jerusalem')).date()
    return local.strftime('%H:%M') if clock_only and local.date()==today else local.strftime('%d/%m %H:%M' if language=='he' else '%d %b %H:%M')


def age_hours(job, now=None):
    now=now or datetime.now(timezone.utc)
    posted=parse_time(job.get('posted_at'))
    if posted is not None:
        return max(0,(now-posted).total_seconds()/3600)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(job.get('posted_at') or '')):
        try:
            date=datetime.fromisoformat(job['posted_at']).date()
            # Use a lower bound when the source supplies a date without a time.
            return max(0,((now.astimezone(ZoneInfo('Asia/Jerusalem')).date()-date).days-1)*24)
        except ValueError:
            pass
    text=(job.get('posted_text') or '').casefold()
    if 'reposted' in text or 'פורסם מחדש' in text:
        return None  # A repost label does not establish the original posting date.
    match=re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago',text)
    if match:
        hours=int(match[1])*{'second':1/3600,'minute':1/60,'hour':1,'day':24,'week':168,'month':720,'year':8760}[match[2]]
    else:
        hebrew=re.search(r'לפני\s+(?:(\d+)\s+)?(שניות|שנייה|שניה|דקות|דקה|שעות|שעה|יומיים|ימים|יום|שבועיים|שבועות|שבוע|חודשיים|חודשים|חודש|שנים|שנה)\b',text)
        if not hebrew:
            return None
        units={'שניות':1/3600,'שנייה':1/3600,'שניה':1/3600,'דקות':1/60,'דקה':1/60,'שעות':1,'שעה':1,
            'ימים':24,'יום':24,'יומיים':48,'שבועות':168,'שבוע':168,'שבועיים':336,
            'חודשים':720,'חודש':720,'חודשיים':1440,'שנים':8760,'שנה':8760}
        hours=int(hebrew[1] or 1)*units[hebrew[2]]
    observed=parse_time(job.get('posting_observed_at'))
    if observed is None:
        return None
    elapsed=max(0,(now-observed).total_seconds()/3600) if observed else 0
    return hours+elapsed


def match_job(job, preferences, now=None):
    prefs={**DEFAULT_PREFERENCES,**preferences}
    title=job.get('title') or ''
    role=role_of(title)
    roles=set(prefs['roles'])
    allowed=set(roles)
    if 'software' in roles:
        allowed|={'backend','frontend','fullstack'}
    if 'all' not in roles and role not in allowed:
        return False,'Different role: '+role
    if role=='other' and 'all' not in roles and prefs['other_scope']=='technical':
        technical=re.search(r'\b(electronics?|electrical|integrator|integration|systems? engineer|systems? architect|computer vision|ai (?:sw|system|research) engineer|r&d software|אינטגרטור|אינטגרציה|אלקטרוניקה|מהנדס(?:ת)? מערכות|מהנדס(?:ת)? חשמל)\b',title.casefold())
        if not technical:
            return False,'Unclassified title lacks software, embedded or related technical context'
    level=level_of(job)
    if 'all' not in prefs['levels']:
        if level and level not in prefs['levels']:
            return False,'Different seniority: '+level
        if level is None and not prefs['keep_unknown_level']:
            return False,'Seniority not specified'
    if prefs['region']=='israel' and not re.search(r'\bisrael\b|ישראל',(job.get('location') or '').casefold()):
        return False,'Israel eligibility not stated'
    def has(phrase):
        value=phrase.casefold()
        variants=next((group for group in KEYWORD_EQUIVALENTS if value in group),(value,))
        return any(re.search(r'(?<!\w)'+re.escape(variant)+r'(?!\w)',title.casefold()) for variant in variants)
    if prefs['include'] and not any(has(phrase) for phrase in prefs['include']):
        return False,'Required title keywords not found'
    if any(has(phrase) for phrase in prefs['exclude']):
        return False,'Excluded title keyword'
    age=age_hours(job,now=now)
    if age is not None and age>prefs['max_age_hours']:
        return False,'Posting older than freshness limit'
    return True,'Matches configured role and location'


def freshness_label(job,language='en'):
    posted=job.get('posted_at')
    raw=' '.join((job.get('posted_text') or '').split())
    if raw and ('reposted' in raw.casefold() or 'פורסם מחדש' in raw):
        return tr('LinkedIn: {label} · original posting date unknown',language,label=source(raw[:90],language))
    if posted:
        return tr('Posted on LinkedIn: {date}',language,date=local_time(posted,language=language))
    if raw:
        observed=job.get('posting_observed_at')
        return tr('LinkedIn label: {label}',language,label=source(raw[:90],language))+(tr(' · observed {time}',language,time=local_time(observed,True,language)) if observed else tr(' · observation time unknown',language))
    return tr('Posting date not provided by LinkedIn',language)


def job_card(job,language='en'):
    level=level_of(job)
    role=role_of(job.get('title') or '')
    tags=tr(role,language) if language=='he' else role.replace('fullstack','full stack').title()
    if level:
        tags+=' · '+(tr(level,language) if language=='he' else level.title())+tr(' (title/metadata)',language)
    return (f"{source((job.get('title') or tr('Job',language))[:150],language)} · {source((job.get('company') or tr('Company unknown',language))[:90],language)}\n"
            f"{source((job.get('location') or tr('Location not specified',language))[:140],language)}\n{tags}\n{freshness_label(job,language)}\n{source(job['url'],language)}")


def friendly_alert(alert):
    message=alert.get('message') or ''
    key=alert.get('alert_key') or ''
    if 'Page limit reached' in message or message in ('short_page_coverage_uncertain','pagination_not_advancing','collection_time_budget'):
        return 'Search coverage is limited. Some jobs may be missing. Open /searches to narrow the search or inspect its latest run.'
    if message in ('reconnect_required','access_denied','invalid_session','session_required'):
        return 'LinkedIn needs you to sign in again. Collection is paused; saved jobs and queued messages are safe.'
    if message=='rate_limited':
        return 'LinkedIn asked the collector to slow down. It will retry after the cooldown; saved jobs are safe.'
    if key.startswith('details:'):
        return 'A job description could not be refreshed. Its job link is still available; the server will retry later.'
    return message.replace('_',' ')
