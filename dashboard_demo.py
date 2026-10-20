"""Fictional demo records only. Never reads deployment databases or credentials."""
from datetime import datetime,timedelta,timezone
from urllib.parse import urlsplit,parse_qs

now=datetime.now(timezone.utc)
companies=['Northstar Labs','Paperplane Systems','Juniper Robotics','Orbit Works','Fieldnote Technologies','Aster Devices']
titles=['Senior Embedded Software Engineer','Firmware Engineer','Linux Kernel Engineer','Backend Software Engineer','QA Automation Engineer','Systems Integration Engineer','Senior Software Engineer','Device Driver Engineer']
cities=['Haifa','Tel Aviv','Herzliya','Yokneam','Ramat Gan','Jerusalem']
jobs=[{
    'id':str(9000+i),'title':titles[i%8],'company':companies[i%6],
    'location':cities[i%6]+', Israel','work_arrangement':['Hybrid','On-site','Remote'][i%3],
    'seniority':'Senior' if i%3==0 else 'Mid-level','employment_type':'Full-time',
    'first_seen':(now-timedelta(hours=i*2)).isoformat(),
    'posted_at':(now-timedelta(days=i%5)).isoformat(),
    'posted_text':f'{i%5} days ago',
    'url':'https://www.linkedin.com/jobs/search/',
    'description':f"""Build things that make a difference.

{companies[i%6]} is a fictional company used to demonstrate this dashboard.

About the role
Join a small engineering team building thoughtful, reliable products. You will own features from design through production and collaborate closely with hardware and platform engineers.

What you will do
• Design and develop reliable software for connected devices.
• Investigate performance, reliability and integration issues.
• Review code and share practical engineering knowledge.
• Work with product and hardware teams on clear technical plans.

What you bring
• Experience with C, C++ or Python and Linux environments.
• A thoughtful approach to testing and debugging.
• Clear communication and curiosity about how things work.

This is a fictional listing for product demonstrations. No actual vacancy or application is represented.""",
    'details_status':'ok',
} for i in range(42)]
apps=[{'id':i+1,'job_id':jobs[i+1]['id'],'status':stage,'title':jobs[i+1]['title'],
       'company':jobs[i+1]['company'],'url':jobs[i+1]['url'],
       'notes':'Example notes for demonstration.','cv_version':'Engineering CV',
       'follow_up_at':(now+timedelta(days=2)).isoformat() if i==2 else None,
       'updated_at':now.isoformat()} for i,stage in enumerate(['saved','saved','applied','interviewing','reviewing','offer'])]
searches=[{'id':i+1,'keywords':name,'location':'Israel','enabled':True,'interval_minutes':60,
    'last_success':(now-timedelta(minutes=18+i)).isoformat(),'next_due':(now+timedelta(minutes=42-i)).isoformat(),
    'status':'ok'} for i,name in enumerate(['Embedded Software Engineer','Firmware Engineer','Linux Kernel Engineer'])]


def handle(method,path,payload=None):
    parts=urlsplit(path); route=parts.path
    query=parse_qs(parts.query); offset=int(query.get('offset',[0])[0]); limit=int(query.get('limit',[200])[0])
    if method=='GET':
        if route in ('/catalog','/applications'):
            values=jobs if route=='/catalog' else apps
            return {'items':values[offset:offset+limit],'total':len(values)}
        if route.startswith('/jobs/'):
            return next(j for j in jobs if j['id']==route.rsplit('/',1)[1])
        if route=='/searches': return searches
        if route=='/alerts': return []
        if route=='/overview':
            return {'jobs':len(jobs),'applications_by_status':{s:sum(a['status']==s for a in apps) for s in ['saved','reviewing','applied','interviewing','offer']},
                    'follow_ups_due':0,'collector':{'open_alerts':0,'network':{},'descriptions_pending':0,'descriptions_failed':0}}
    if route=='/applications':
        from fastapi import HTTPException
        if any(a['job_id']==payload['job_id'] for a in apps): raise HTTPException(409,'Already saved.')
        job=next(j for j in jobs if j['id']==payload['job_id'])
        item={'id':max([a['id'] for a in apps]+[0])+1,'title':job['title'],'company':job['company'],
              'url':job['url'],'notes':'','follow_up_at':None,'cv_version':None,**payload}
        apps.append(item); return item
    if route.startswith('/applications/'):
        item=next(a for a in apps if a['id']==int(route.rsplit('/',1)[1]))
        item.update(payload); return item
    if route=='/searches':
        item={'id':max(s['id'] for s in searches)+1,'enabled':True,'interval_minutes':60,**payload}
        searches.append(item); return item
    if route.endswith('/poll'): return {'status':'queued'}
    if route.startswith('/searches/'):
        item=next(s for s in searches if s['id']==int(route.rsplit('/',1)[1]))
        item.update(payload); return item
    return {'ok':True}
