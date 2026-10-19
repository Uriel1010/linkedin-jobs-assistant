"""Explainable title/location filters and source-aware presentation."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROLES = {'software','backend','frontend','fullstack','data','devops','ml','security','embedded','qa','support','research','management','other','all'}
LEVELS = {'intern','junior','mid','senior','lead','principal','all'}
DEFAULT_PREFERENCES = {'roles':['software'],'levels':['all'],'region':'israel',
                       'include':[],'exclude':[],'max_age_hours':48,'keep_unknown_level':True}


def role_of(title):
    title=title.casefold()
    patterns=[('management',r'\b(director|head of|engineering manager|vice president|vp of)\b'),
              ('support',r'\b(support|customer success|sales engineer|solutions engineer)\b'),
              ('qa',r'\b(validation|verification|quality assurance|qa|test engineer|testing engineer)\b'),
              ('embedded',r'\b(firmware|embedded|silicon|hardware|asic|fpga|low[ -]level|linux kernel|kernel engineer|device drivers?|bsp|board support package)\b'),
              ('security',r'\b(offensive|penetration|security engineer|cybersecurity)\b'),
              ('devops',r'\b(devops|site reliability|sre|platform engineer|cloud engineer)\b'),
              ('data',r'\b(data engineer|data scientist|analytics engineer)\b'),
              ('research',r'\b(researcher|research scientist|algorithms? engineer|control algorithms)\b'),
              ('ml',r'\b(ai engineer|ai researcher|machine learning|ml engineer|agentic ai)\b'),
              ('fullstack',r'\bfull[ -]?stack\b'),('backend',r'\bback[ -]?end\b'),
              ('frontend',r'\bfront[ -]?end\b'),
              ('software',r'\b(software|developer|programmer|development engineer)\b')]
    return next((role for role,pattern in patterns if re.search(pattern,title)),'other')


def level_of(job):
    text=(job.get('seniority') or job.get('title') or '').casefold()
    for level,pattern in [('intern',r'\b(intern|internship|student)\b'),('principal',r'\b(principal|staff|distinguished)\b'),
                          ('lead',r'\b(lead|team leader|tech lead)\b'),('senior',r'\b(senior|sr\.?|experienced)\b'),
                          ('junior',r'\b(junior|jr\.?|entry[- ]level|graduate|associate)\b'),('mid',r'\b(mid[- ]level|intermediate)\b')]:
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


def local_time(value, clock_only=False):
    if isinstance(value,str) and value.endswith(' UTC'):
        value=value[:-4]+'+00:00'
    dt=parse_time(value)
    if dt is None:
        return 'Not yet' if not value else str(value)
    local=dt.astimezone(ZoneInfo('Asia/Jerusalem'))
    today=datetime.now(ZoneInfo('Asia/Jerusalem')).date()
    return local.strftime('%H:%M') if clock_only and local.date()==today else local.strftime('%d %b %H:%M')


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
    if 'reposted' in text:
        return None  # A repost label does not establish the original posting date.
    match=re.search(r'(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago',text)
    if not match:
        return None
    multiplier={'second':1/3600,'minute':1/60,'hour':1,'day':24,'week':168,'month':720,'year':8760}[match[2]]
    observed=parse_time(job.get('posting_observed_at'))
    if observed is None:
        return None
    elapsed=max(0,(now-observed).total_seconds()/3600) if observed else 0
    return int(match[1])*multiplier+elapsed


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
    level=level_of(job)
    if 'all' not in prefs['levels']:
        if level and level not in prefs['levels']:
            return False,'Different seniority: '+level
        if level is None and not prefs['keep_unknown_level']:
            return False,'Seniority not specified'
    if prefs['region']=='israel' and not re.search(r'\bisrael\b|ישראל',(job.get('location') or '').casefold()):
        return False,'Israel eligibility not stated'
    def has(phrase):
        return bool(re.search(r'(?<!\w)'+re.escape(phrase.casefold())+r'(?!\w)',title.casefold()))
    if prefs['include'] and not any(has(phrase) for phrase in prefs['include']):
        return False,'Required title keywords not found'
    if any(has(phrase) for phrase in prefs['exclude']):
        return False,'Excluded title keyword'
    age=age_hours(job,now=now)
    if age is not None and age>prefs['max_age_hours']:
        return False,'Posting older than freshness limit'
    return True,'Matches configured role and location'


def freshness_label(job):
    posted=job.get('posted_at')
    raw=' '.join((job.get('posted_text') or '').split())
    if raw and 'reposted' in raw.casefold():
        return 'LinkedIn: '+raw[:90]+' · original posting date unknown'
    if posted:
        return 'Posted on LinkedIn: '+local_time(posted)
    if raw:
        observed=job.get('posting_observed_at')
        return 'LinkedIn label: '+raw[:90]+(' · observed '+local_time(observed,True) if observed else ' · observation time unknown')
    return 'Posting date not provided by LinkedIn'


def job_card(job):
    level=level_of(job)
    tags=role_of(job.get('title') or '').replace('fullstack','full stack').title()
    if level:
        tags+=' · '+level.title()+' (title/metadata)'
    return (f"{(job.get('title') or 'Job')[:150]} · {(job.get('company') or 'Company unknown')[:90]}\n"
            f"{(job.get('location') or 'Location not specified')[:140]}\n{tags}\n{freshness_label(job)}\n{job['url']}")


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
