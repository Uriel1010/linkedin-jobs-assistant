import copy
import json
import tempfile
import time
import urllib.error
import io
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from fastapi.testclient import TestClient
import app
import storage
import source_store as store
from source_providers import collect_source, canonical_url, SourceError, get_json
from source_worker import tick
from email_import import parse_email


VALUES={'provider':'greenhouse','board':'example','company':'Example Devices','location_filter':'Israel',
        'interval_minutes':120,'daily_budget':48,'max_requests':10}
JOB={'external_id':'abc','title':'Firmware Engineer','company':'Example Devices','location':'Haifa, Israel',
     'url':'https://job-boards.greenhouse.io/example/jobs/1','description':'Firmware work',
     'posted_at':None,'application_url':'https://job-boards.greenhouse.io/example/jobs/1'}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original=storage.DATA; storage.DATA=Path(self.temp.name)
        self.guard=patch.object(app,'LINKEDIN_ENABLED',False);self.guard.start()
        self.client=TestClient(app.app);self.client.__enter__()
        self.headers={'X-API-Key':(storage.DATA/'api-key').read_text()}
        self.source=store.add_source(VALUES)

    def tearDown(self):
        self.client.__exit__(None,None,None);self.guard.stop()
        storage.DATA=self.original;self.temp.cleanup()

    def test_linkedin_disabled_and_database_available(self):
        (storage.DATA/'session.json').write_text('{"cookies":[]}')
        with patch.object(app,'collect') as collect,patch.object(app,'fetch_details') as details:
            self.assertEqual(self.client.post('/searches',json={'keywords':'Engineer'},headers=self.headers).status_code,423)
            self.assertEqual(self.client.post(f"/searches/{self.source['search_id']}/poll",headers=self.headers).status_code,409)
            with self.assertRaises(Exception) as error: app.network_guard()
            self.assertEqual(error.exception.status_code,423)
            self.assertEqual(self.client.get('/catalog',headers=self.headers).status_code,200)
            collect.assert_not_called();details.assert_not_called()
        self.assertFalse(self.client.get('/status',headers=self.headers).json()['linkedin_enabled'])

    def test_validation_auth_and_conflict(self):
        self.assertEqual(self.client.get('/sources').status_code,401)
        self.assertEqual(self.client.post('/sources',json={**VALUES,'board':'../evil'},headers=self.headers).status_code,422)
        self.assertEqual(self.client.post('/sources',json=VALUES,headers=self.headers).status_code,409)
        url='/sources/'+str(self.source['id'])
        self.assertEqual(self.client.patch(url,json={'revision':1,'interval_minutes':180},headers=self.headers).status_code,200)
        self.assertEqual(self.client.patch(url,json={'revision':1,'enabled':False},headers=self.headers).status_code,409)

    def test_baseline_dedup_and_new_event(self):
        store.save_snapshot(self.source,[JOB])
        source=store.list_sources()['items'][0]
        store.save_snapshot(source,[JOB,JOB])
        items=self.client.get('/jobs',headers=self.headers).json()['items']
        self.assertEqual(len(items),1);self.assertEqual(items[0]['baseline'],1)
        self.assertEqual(items[0]['source'],'greenhouse')
        self.assertTrue(items[0]['id'].isdigit());self.assertLessEqual(len(items[0]['id']),30)
        newer={**JOB,'external_id':'new','url':JOB['url']+'2'}
        store.save_snapshot(source,[JOB,newer])
        items=self.client.get('/jobs',headers=self.headers).json()['items']
        self.assertEqual(len(items),2);self.assertEqual(items[1]['baseline'],0)

    def test_exact_link_dedup_and_closing(self):
        store.save_snapshot(self.source,[JOB,{**JOB,'external_id':'second','url':JOB['url']+'2'}])
        source=store.list_sources()['items'][0]
        other=store.add_source({**VALUES,'board':'another'})
        store.save_snapshot(other,[{**JOB,'external_id':'elsewhere','url':JOB['url']+'?utm_source=test'}])
        with storage.db() as con: self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
        store.save_snapshot(source,[JOB])
        with storage.db() as con:
            self.assertEqual(con.execute('SELECT is_active FROM jobs WHERE source_job_id=?',('second',)).fetchone()[0],0)
            self.assertEqual(con.execute('SELECT is_active FROM jobs WHERE source_job_id=?',('abc',)).fetchone()[0],1)

    def test_empty_board_needs_review_not_mass_close(self):
        store.save_snapshot(self.source,[JOB])
        with self.assertRaises(SourceError) as error: store.save_snapshot(store.list_sources()['items'][0],[])
        self.assertTrue(error.exception.blocked)
        with storage.db() as con: self.assertEqual(con.execute('SELECT is_active FROM jobs').fetchone()[0],1)

    def test_atomic_snapshot_invalid_job_does_not_partially_save(self):
        with self.assertRaises(SourceError):
            store.save_snapshot(self.source,[JOB,{**JOB,'external_id':'bad','url':'http://127.0.0.1'}])
        with storage.db() as con: self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_block_persists_and_resume_respects_retry_after(self):
        source=store.claim();store.fail(source,SourceError('access_denied',blocked=True))
        self.assertIsNone(store.claim())
        row=store.list_sources()['items'][0]
        self.assertEqual(row['status'],'blocked')
        response=self.client.post(f"/sources/{row['id']}/resume",headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertIsNone(store.claim())
        self.assertEqual(self.client.post(f"/sources/{row['id']}/poll",headers=self.headers).status_code,409)

    def test_daily_budget_cannot_be_bypassed(self):
        with storage.db() as con:
            con.execute('UPDATE sources SET daily_budget=1')
        store.reserve_request(self.source,'boards-api.greenhouse.io')
        with self.assertRaises(SourceError) as error: store.reserve_request(self.source,'boards-api.greenhouse.io')
        self.assertEqual(error.exception.reason,'daily_budget_exhausted')

    def test_worker_claim_and_health(self):
        with patch('source_worker.collect_source',return_value=([JOB],1)):
            self.assertTrue(tick());self.assertFalse(tick())
        self.assertTrue(store.list_sources()['worker_alive'])
        self.assertEqual(store.list_sources()['items'][0]['status'],'ok')

    def test_adapters_and_no_updated_date_substitution(self):
        source=self.source
        fixture={'jobs':[{'id':1,'title':'Firmware Engineer','absolute_url':JOB['url'],
                         'location':{'name':'Israel'},'content':'<p>Hello</p><script>bad()</script>','updated_at':'2026-01-01'}]}
        jobs,_=collect_source(source,lambda *args:fixture)
        self.assertIsNone(jobs[0]['posted_at']);self.assertNotIn('bad()',jobs[0]['description'])
        lever={**source,'provider':'lever_eu'}
        jobs,_=collect_source(lever,lambda *args:[{'id':'a','text':'Engineer','hostedUrl':'https://jobs.eu.lever.co/example/a','categories':{'location':'Israel'},'createdAt':1760000000000}])
        self.assertEqual(jobs[0]['external_id'],'a')
        ashby={**source,'provider':'ashby'}
        jobs,_=collect_source(ashby,lambda *args:{'jobs':[{'id':'a','title':'Engineer','jobUrl':'https://jobs.ashbyhq.com/example/a','location':'Israel','isListed':False}]})
        self.assertEqual(jobs,[])

    def test_partial_pagination_does_not_save_snapshot(self):
        with self.assertRaises(SourceError) as error:
            collect_source({**self.source,'provider':'lever','max_requests':1},lambda *args:[{}]*100)
        self.assertEqual(error.exception.reason,'pagination_budget_exhausted')

    def test_email_preview_never_fetches_or_saves(self):
        raw=b'Content-Type: text/html; charset=utf-8\r\n\r\n<a href="https://www.linkedin.com/jobs/view/123/?trk=mail">Firmware Engineer</a><a href="http://127.0.0.1">bad</a>'
        with patch('urllib.request.urlopen') as request:
            jobs=parse_email(raw)
        request.assert_not_called()
        self.assertEqual(jobs[0]['source_job_id'],'123')
        self.assertEqual(jobs[0]['url'],'https://www.linkedin.com/jobs/view/123/')
        self.assertEqual(self.client.post('/imports/email/preview',headers=self.headers,json={'content':raw.decode()}).json()['saved'],False)
        with storage.db() as con: self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_safe_urls(self):
        for url in ('http://example.com','https://127.0.0.1','https://localhost','https://example.local','https://user:pass@example.com'):
            self.assertIsNone(canonical_url(url))
        self.assertEqual(canonical_url('https://jobs.example.com/a?utm_source=email&job=1'),'https://jobs.example.com/a?job=1')

    def test_http_access_denial_and_retry_after(self):
        opener=Mock()
        for code,headers,reason in [(403,{},'access_denied'),(429,{'Retry-After':'7200'},'rate_limited'),(302,{'Location':'https://www.linkedin.com'},'redirect_requires_review')]:
            opener.open.side_effect=urllib.error.HTTPError('https://example.invalid',code,'error',headers,io.BytesIO(b''))
            with patch('source_store.reserve_request'),patch('source_providers.urllib.request.build_opener',return_value=opener):
                with self.assertRaises(SourceError) as exc: get_json(self.source,'/v1/boards/example/jobs',1)
            self.assertEqual(exc.exception.reason,reason)
            if code==429:self.assertEqual(exc.exception.retry_after,7200)
            else:self.assertTrue(exc.exception.blocked)
        req=opener.open.call_args.args[0]
        self.assertIsNone(req.get_header('Cookie'));self.assertIsNone(req.get_header('Authorization'))
        self.assertTrue(req.full_url.startswith('https://boards-api.greenhouse.io/'))

    def test_conditional_cache_and_first_baseline_not_notified(self):
        from telegram_bot import State
        path='/v1/boards/example/jobs'
        store.write_cache(self.source['id'],'https://boards-api.greenhouse.io'+path,'{"jobs":[]}','version-1',None)
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://example.invalid',304,'cache',{},io.BytesIO(b''))
        with patch('source_store.reserve_request'),patch('source_providers.urllib.request.build_opener',return_value=opener):
            self.assertEqual(get_json(self.source,path,1),{'jobs':[]})
        self.assertEqual(opener.open.call_args.args[0].get_header('If-none-match'),'version-1')
        store.save_snapshot(self.source,[JOB])
        page=self.client.get('/jobs',headers=self.headers).json()
        bot=State(Path(self.temp.name)/'bot')
        try:
            bot.enqueue(page['items'],page['next_after_id'])
            self.assertEqual(bot.pending_count(),0)
            self.assertEqual(bot.get('cursor'),page['next_after_id'])
        finally:bot.con.close()


if __name__=='__main__':
    unittest.main()
