"""Compact Telegram HTML views; source fields are escaped, not translated."""
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from feed_rules import role_of,level_of,parse_time
from i18n import tr,source

SOFTWARE={'software','backend','frontend','fullstack'}

def category(job):
    role=role_of(job.get('title') or '')
    return 'software' if role in SOFTWARE else role

def rank(job,prefs):
    role=role_of(job.get('title') or '')
    selected=prefs['roles']
    if role in SOFTWARE and 'software' in selected:
        role='software'
    return (selected.index(role) if role in selected else len(selected),level_of(job) is None,
            (job.get('title') or '').casefold(),job.get('id',''))

def label(role,language):
    if role=='other':
        return 'תפקידים קרובים' if language=='he' else 'Related engineering'
    if role=='qa':
        return 'בדיקות' if language=='he' else 'QA'
    return tr(role,language) if language=='he' else role.replace('fullstack','full stack').title()

def posting(job,language):
    raw=' '.join((job.get('posted_text') or '').split())
    if 'reposted' in raw.casefold() or 'פורסם מחדש' in raw:
        return tr('Reposted · original date unknown',language)
    date=job.get('posted_at')
    if date:
        dt=parse_time(date)
        try:
            day=dt.astimezone(ZoneInfo('Asia/Jerusalem')).date() if dt else datetime.fromisoformat(date).date()
            today=datetime.now(ZoneInfo('Asia/Jerusalem')).date()
            if day==today:
                return tr('Posted today',language)
            if (today-day).days==1:
                return tr('Posted yesterday',language)
            return day.strftime('%d/%m/%Y')
        except (ValueError,TypeError):
            pass
    # Relative labels remain explicitly tied to the recorded source observation.
    return tr('Date not listed',language)

def compact_location(job,israel_only,language):
    text=' '.join((job.get('location') or '').split())
    mode=job.get('work_arrangement') or ''
    match=re.search(r'\s*\((Hybrid|Remote|On-site)\)\s*$',text,re.I)
    if match:
        mode=match[1]
        text=text[:match.start()]
    text=re.sub(r',?\s*(?:Center|Tel Aviv|North|South|Jerusalem|Haifa) District\b','',text,flags=re.I)
    if israel_only:
        text=re.sub(r'(?:,\s*|^)(?:Israel|ישראל)(?=,|$)','',text,flags=re.I).strip(' ,')
    text=text.strip(' ,') or ('ישראל' if language=='he' and israel_only else 'Israel' if israel_only else tr('Location not specified',language))
    modes={'hybrid':'Hybrid','remote':'Remote','on-site':'On-site'}
    mode=tr(modes.get(mode.casefold(),mode),language) if mode else ''
    return text[:100]+(' · '+mode if mode else '')

def card(job,index,prefs,language):
    esc=lambda value:html.escape(source(value,language),quote=True)
    title=(job.get('title') or tr('Job',language))[:140]
    url=job.get('url') or ''
    parsed=urlsplit(url)
    valid=parsed.scheme=='https' and parsed.netloc.lower() in ('linkedin.com','www.linkedin.com') and re.fullmatch(r'/jobs/view/\d+/?',parsed.path)
    if job.get('source') in ('greenhouse','lever','lever_eu','ashby'):
        from source_providers import canonical_url
        valid=bool(canonical_url(url))
    heading=f'<a href="{html.escape(url,quote=True)}"><b>{esc(title)}</b></a>' if valid else f'<b>{esc(title)}</b>'
    company=(job.get('company') or tr('Company unknown',language))[:80]
    place=compact_location(job,prefs['region']=='israel',language)
    level=level_of(job)
    original=(job.get('seniority') or job.get('title') or '').casefold()
    if re.search(r'\bsenior staff\b',original):
        level_text='Senior Staff'
    elif re.search(r'\bstaff\b',original):
        level_text='Staff'
    else:
        level_text=tr(level,language) if language=='he' and level else level.title() if level else ''
    tags=[label(role_of(job.get('title') or ''),language)]
    if level_text:
        tags.append(level_text)
    tags.append(posting(job,language))
    prefix='\u200f' if language=='he' else ''
    return f'{prefix}{index}. {heading}\n{esc(company)} · {esc(place)}\n'+html.escape(' · '.join(tags))

def paginate(jobs,prefs,language):
    pages=[]; group=[]; size=0
    for index,job in enumerate(jobs,1):
        text=card(job,index,prefs,language)
        length=len(text.encode('utf-16-le'))//2+2
        if group and (len(group)>=5 or size+length>2600):
            pages.append(group); group=[]; size=0
        group.append(text); size+=length
    if group:
        pages.append(group)
    return pages
