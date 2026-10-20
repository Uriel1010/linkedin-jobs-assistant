"""Offline email parser foundation. No mailbox login, fetching, or redirect resolution."""
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, parse_qs


def linkedin_id(url):
    try:
        p=urlsplit(url)
        if p.scheme!='https' or p.hostname not in ('linkedin.com','www.linkedin.com') or p.username or p.password:
            return None
        match=re.search(r'/jobs/view/(?:[^/?]*-)?(\d+)(?:/|$)',p.path)
        if match: return match.group(1)
        value=parse_qs(p.query).get('currentJobId',[''])[0]
        return value if value.isascii() and value.isdigit() and len(value)<=30 else None
    except ValueError:
        return None


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.current=None

    def handle_starttag(self,tag,attrs):
        if tag=='a': self.current=[dict(attrs).get('href',''),'']

    def handle_data(self,data):
        if self.current: self.current[1]+=data

    def handle_endtag(self,tag):
        if tag=='a' and self.current:
            self.links.append(self.current); self.current=None


def parse_email(raw):
    if len(raw)>500000: raise ValueError('Email preview is limited to 500 KB.')
    message=BytesParser(policy=policy.default).parsebytes(raw)
    links=[]
    for part in message.walk():
        if part.get_content_disposition()=='attachment': continue
        if part.get_content_type() not in ('text/html','text/plain'): continue
        content=part.get_content()
        if part.get_content_type()=='text/html':
            parser=Links(); parser.feed(content); links.extend(parser.links)
        else:
            links.extend((url,'') for url in re.findall(r'https://[^\s<>"]+',content))
    jobs={}
    for url,title in links:
        job_id=linkedin_id(url)
        if job_id:
            jobs.setdefault(job_id,{'source':'linkedin_email','source_job_id':job_id,
                'title':re.sub(r'\s+',' ',title).strip()[:500] or None,
                'url':f'https://www.linkedin.com/jobs/view/{job_id}/'})
    return list(jobs.values())
