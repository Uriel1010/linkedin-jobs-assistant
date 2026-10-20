import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(dashboard,'STATE',Path(self.temp.name)),patch.object(dashboard,'DEMO',False),
                      patch.object(dashboard,'upstream',side_effect=self.upstream)]
        for p in self.patches:p.start()
        dashboard.cache.clear(); dashboard.attempts.clear()
        self.jobs=[{'id':'1','title':'Firmware Engineer','company':'Example Lab','location':'Haifa, Israel',
                    'description':'Build reliable devices','work_arrangement':'Hybrid','first_seen':'2026-01-02T12:00:00+00:00','url':'https://www.linkedin.com/jobs/view/1/'},
                   {'id':'2','title':'=Software Engineer','company':'Example Software','location':'Israel',
                    'description':'<script>source text</script>','first_seen':'2026-01-01T12:00:00+00:00','url':'https://www.linkedin.com/jobs/view/2/'}]
        self.apps=[]
        self.client=TestClient(dashboard.app)
        self.headers={'X-Dashboard-Request':'1'}

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def upstream(self,method,path,payload=None):
        if path.startswith('/catalog'):return {'items':copy.deepcopy(self.jobs),'total':len(self.jobs)}
        if path.startswith('/applications?'):return {'items':copy.deepcopy(self.apps),'total':len(self.apps)}
        if path.startswith('/jobs/'):return next(copy.deepcopy(j) for j in self.jobs if j['id']==path.rsplit('/',1)[1])
        if path=='/applications' and method=='POST':
            self.apps.append({'id':1,**payload,'title':'Firmware Engineer','company':'Example Lab'})
            return self.apps[-1]
        if path=='/applications/1' and method=='PATCH':
            self.apps[0].update(payload);return self.apps[0]
        return {'ok':True}

    def login(self):
        return self.client.post('/api/login',json={'key':dashboard.access_key()},headers=self.headers)

    def test_auth_cookie_logout_and_no_key_disclosure(self):
        self.assertEqual(self.client.get('/api/jobs').status_code,401)
        self.assertFalse(self.client.get('/api/session').json()['authenticated'])
        response=self.login()
        self.assertEqual(response.status_code,200)
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        self.assertIn('SameSite=strict',response.headers['set-cookie'])
        self.assertNotIn(dashboard.access_key(),response.text)
        self.assertEqual(self.client.get('/api/jobs').status_code,200)
        self.client.post('/api/logout',headers=self.headers)
        self.assertEqual(self.client.get('/api/jobs').status_code,401)

    def test_csrf_origin_and_custom_header(self):
        self.assertEqual(self.client.post('/api/login',json={'key':'wrong'}).status_code,403)
        self.login()
        response=self.client.post('/api/job-state',json={'ids':['1'],'dismissed':True},
            headers={**self.headers,'Origin':'https://untrusted.example'})
        self.assertEqual(response.status_code,403)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/login',json={'key':'wrong'},headers=self.headers).status_code,401)
        self.assertEqual(self.login().status_code,429)

    def test_dismiss_restore_and_seen_are_durable(self):
        self.login()
        self.assertEqual(self.client.post('/api/job-state',json={'ids':['1'],'dismissed':True,'seen':True},headers=self.headers).status_code,200)
        self.assertEqual([j['id'] for j in self.client.get('/api/jobs').json()['items']],['2'])
        dismissed=self.client.get('/api/jobs?view=dismissed').json()
        self.assertEqual(dismissed['items'][0]['id'],'1')
        self.assertTrue(dismissed['items'][0]['seen'])
        with dashboard.db() as con:self.assertEqual(con.execute('SELECT dismissed FROM job_state WHERE job_id="1"').fetchone()[0],1)
        self.client.post('/api/job-state',json={'ids':['1'],'dismissed':False},headers=self.headers)
        self.assertEqual(self.client.get('/api/jobs').json()['total'],2)
        self.assertEqual(self.client.get('/api/jobs?unread=1').json()['total'],1)

    def test_invalid_bulk_is_atomic(self):
        self.login()
        r=self.client.post('/api/job-state',json={'ids':['1','999'],'dismissed':True},headers=self.headers)
        self.assertEqual(r.status_code,404)
        self.assertEqual(self.client.get('/api/jobs?view=dismissed').json()['total'],0)
        r=self.client.post('/api/job-state',json={'ids':['../secrets'],'dismissed':True},headers=self.headers)
        self.assertEqual(r.status_code,422)

    def test_filters_order_and_summaries(self):
        self.login()
        self.assertEqual(self.client.get('/api/jobs?role=embedded&mode=Hybrid&q=haifa').json()['total'],1)
        self.assertEqual(self.client.get('/api/jobs?sort=oldest').json()['items'][0]['id'],'2')
        self.assertEqual(self.client.get('/api/jobs?sort=newest').json()['items'][0]['id'],'1')
        self.assertNotIn('description',self.client.get('/api/jobs').json()['items'][0])
        self.assertIn('description',self.client.get('/api/jobs/1').json())

    def test_saved_application_notes_and_followup_share_api(self):
        self.login()
        self.client.post('/api/applications',json={'job_id':'1','status':'saved'},headers=self.headers)
        self.assertEqual(self.client.get('/api/jobs?view=saved').json()['total'],1)
        update={'status':'applied','notes':'Private test notes','follow_up_at':'2026-01-10T12:00:00Z'}
        self.client.patch('/api/applications/1',json=update,headers=self.headers)
        self.assertEqual(self.apps[0]['notes'],'Private test notes')
        self.assertEqual(self.apps[0]['status'],'applied')

    def test_csv_is_filtered_and_neutralizes_formulas(self):
        self.login()
        response=self.client.get('/api/export?role=software')
        self.assertIn("'=Software Engineer",response.text)
        self.assertNotIn('Firmware Engineer',response.text)
        self.assertIn('attachment',response.headers['content-disposition'])

    def test_static_shell_has_security_headers(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get('/assets/../dashboard.py').status_code,404)
