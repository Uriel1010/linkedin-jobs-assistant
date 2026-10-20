"""Public, documented employer job-board adapters. No accounts or browser sessions."""
import html
import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

HOSTS = {'greenhouse':'boards-api.greenhouse.io', 'lever':'api.lever.co',
         'lever_eu':'api.eu.lever.co', 'ashby':'api.ashbyhq.com'}


class SourceError(Exception):
    def __init__(self, reason, *, blocked=False, retry_after=0):
        self.reason, self.blocked, self.retry_after = reason, blocked, retry_after
        super().__init__(reason)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.skip += 1
        if tag in ('p','br','div','li','h1','h2','h3'): self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script','style'): self.skip = max(0,self.skip-1)

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def plain(value):
    parser=PlainText()
    parser.feed(html.unescape(str(value or ''))[:200000])
    return re.sub(r'\n[ \t]*\n+', '\n\n', ''.join(parser.parts)).strip()[:100000]


def canonical_url(value):
    """Normalize links without requesting them; never follow email tracking links."""
    try:
        p=urlsplit(str(value or ''))
        if p.scheme!='https' or not p.hostname or p.username or p.password:
            return None
        host=p.hostname.lower()
        if '.' not in host or host.endswith(('.local','.internal','.localhost')): return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if p.port not in (None,443): return None
        query=[(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_')
               and k.lower() not in ('source','gh_src','lever-source','lever-via','ref','trk','trackingid')]
        return urlunsplit(('https',host,p.path.rstrip('/') or '/',urlencode(sorted(query)),''))
    except (ValueError,TypeError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value):
    try:
        seconds=float(value)
    except (ValueError,TypeError):
        try: seconds=parsedate_to_datetime(value).timestamp()-time.time()
        except (ValueError,TypeError,OverflowError): seconds=3600
    return max(3600, min(seconds, 7*86400))


def get_json(source, path, request_number):
    """One request, bounded response, conditional cache, durable provider-wide budget."""
    from source_store import reserve_request, read_cache, write_cache
    host=HOSTS[source['provider']]
    if request_number > source['max_requests']:
        raise SourceError('request_budget_exhausted')
    reserve_request(source, host)
    url='https://'+host+path
    cached=read_cache(source['id'],url)
    headers={'Accept':'application/json','User-Agent':'Jobspace/3.0 (personal job-board reader)'}
    if cached and cached['etag']: headers['If-None-Match']=cached['etag']
    if cached and cached['modified']: headers['If-Modified-Since']=cached['modified']
    req=urllib.request.Request(url,headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as response:
            raw=response.read(12*1024*1024+1)
            if len(raw)>12*1024*1024: raise SourceError('response_too_large')
            data=json.loads(raw)
            write_cache(source['id'],url,raw.decode('utf-8'),response.headers.get('ETag'),response.headers.get('Last-Modified'))
            return data
    except urllib.error.HTTPError as exc:
        code=exc.code; retry_header=exc.headers.get('Retry-After')
        exc.close()
        if code==304 and cached: return json.loads(cached['body'])
        if code in (401,403): raise SourceError('access_denied',blocked=True) from None
        if code==429: raise SourceError('rate_limited',retry_after=retry_seconds(retry_header)) from None
        if code in (404,410): raise SourceError('board_not_found',blocked=True) from None
        if 300<=code<400: raise SourceError('redirect_requires_review',blocked=True) from None
        raise SourceError('provider_http_error') from None
    except (OSError,ValueError,UnicodeError):
        raise SourceError('invalid_response_or_network_error') from None


def date(value):
    if not value: return None
    try:
        d=datetime.fromtimestamp(value/1000,timezone.utc) if isinstance(value,(float,int)) else datetime.fromisoformat(value.replace('Z','+00:00'))
        if d.tzinfo is None: return None
        return d.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError,OverflowError): return None


def normalize(source, row):
    provider=source['provider']
    location=''
    if provider=='greenhouse':
        external=row.get('id'); title=row.get('title'); url=row.get('absolute_url')
        location=(row.get('location') or {}).get('name','')
        description=plain(row.get('content'))
        posted=date(row.get('first_published'))  # Never substitute updated_at.
        arrangement=None
    elif provider in ('lever','lever_eu'):
        external=row.get('id'); title=row.get('text'); url=row.get('hostedUrl')
        cats=row.get('categories') or {}
        location=cats.get('location','')
        description=plain(row.get('descriptionPlain') or row.get('description'))
        description+='\n\n'+'\n\n'.join(plain(x.get('text'))+'\n'+plain(x.get('content')) for x in row.get('lists',[]))
        description+='\n\n'+plain(row.get('additionalPlain') or row.get('additional'))
        posted=date(row.get('createdAt'))
        arrangement=row.get('workplaceType')
    else:
        if row.get('isListed') is False: return None
        external=row.get('id') or row.get('jobUrl'); title=row.get('title'); url=row.get('jobUrl')
        locations=[row.get('location','')]+[loc.get('location','') for loc in row.get('secondaryLocations',[])]
        country=((row.get('address') or {}).get('postalAddress') or {}).get('addressCountry')
        if country: locations.append('Israel' if country=='IL' else country)
        location=' · '.join(dict.fromkeys(v for v in locations if v))
        description=plain(row.get('descriptionPlain') or row.get('descriptionHtml'))
        posted=date(row.get('publishedAt'))
        arrangement=row.get('workplaceType') or ('Remote' if row.get('isRemote') else None)
    link=canonical_url(url)
    if not external or not isinstance(title,str) or not title.strip() or not link:
        raise SourceError('unexpected_job_schema')
    return {'external_id':str(external)[:500], 'title':title[:500], 'company':source['company'],
            'location':str(location)[:1000], 'url':link, 'description':description.strip()[:100000],
            'posted_at':posted,'work_arrangement':arrangement,
            'employment_type':row.get('employmentType') or (row.get('categories') or {}).get('commitment'),
            'application_url':canonical_url(row.get('applyUrl')) or link}


def collect_source(source, fetch=get_json):
    board=source['board']; provider=source['provider']
    rows=[]; requests=0
    if provider=='greenhouse':
        data=fetch(source,f'/v1/boards/{board}/jobs?content=true',1); requests=1
        if not isinstance(data,dict) or not isinstance(data.get('jobs'),list): raise SourceError('unexpected_board_schema')
        rows=data['jobs']
    elif provider=='ashby':
        data=fetch(source,f'/posting-api/job-board/{board}',1); requests=1
        if not isinstance(data,dict) or not isinstance(data.get('jobs'),list): raise SourceError('unexpected_board_schema')
        rows=data['jobs']
    elif provider in ('lever','lever_eu'):
        for page in range(source['max_requests']):
            data=fetch(source,f'/v0/postings/{board}?mode=json&limit=100&skip={page*100}',page+1)
            requests+=1
            if not isinstance(data,list): raise SourceError('unexpected_board_schema')
            rows.extend(data)
            if len(data)<100: break
        else:
            raise SourceError('pagination_budget_exhausted')
    else:
        raise SourceError('unsupported_provider',blocked=True)
    if len(rows)>10000: raise SourceError('board_too_large')
    jobs=[]
    for row in rows:
        if not isinstance(row,dict): raise SourceError('unexpected_job_schema')
        job=normalize(source,row)
        if job and (not source['location_filter'] or source['location_filter'].casefold() in job['location'].casefold()):
            jobs.append(job)
    return jobs, requests
