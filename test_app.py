import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
import app as service
import storage
from collector import CollectionError, EXTRACT


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = storage.DATA
        storage.DATA = Path(self.temp.name)
        self.client_context = TestClient(service.app)
        self.client = self.client_context.__enter__()
        self.auth = {'X-API-Key': (storage.DATA / 'api-key').read_text()}
        service.last_network_attempt = 0

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        storage.DATA = self.original
        self.temp.cleanup()

    def search(self):
        response = self.client.post('/searches', json={'keywords':'Engineer'}, headers=self.auth)
        self.assertEqual(response.status_code, 201)
        return response.json()['id']

    def test_auth_and_missing_session(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/jobs').status_code, 401)
        self.assertEqual(self.client.get('/jobs', headers={'X-API-Key':'wrong'}).status_code, 401)
        search_id = self.search()
        self.assertEqual(self.client.post(f'/searches/{search_id}/poll', headers=self.auth).status_code, 409)
        self.assertFalse(self.client.get('/status', headers=self.auth).json()['session_saved'])

    def test_deduplication_and_cursor(self):
        first = self.search()
        second = self.search()
        job = {'id':'123', 'title':'Engineer', 'url':'https://www.linkedin.com/jobs/view/123/'}
        self.assertEqual(storage.save_jobs(first, [job,job]), 1)
        self.assertEqual(storage.save_jobs(first, [job]), 0)
        self.assertEqual(storage.save_jobs(second, [job]), 1)
        one = self.client.get('/jobs?limit=1', headers=self.auth).json()
        two = self.client.get('/jobs', params={'after_id':one['next_after_id']}, headers=self.auth).json()
        self.assertEqual(len(two['items']), 1)
        self.assertEqual(two['items'][0]['search_id'], second)
        self.assertEqual(self.client.get('/jobs', params={'after_id':two['next_after_id']}, headers=self.auth).json()['items'], [])
        self.assertEqual(len(self.client.get('/jobs', params={'search_id':first}, headers=self.auth).json()['items']), 1)

    def test_poll_failure_is_visible_and_throttled(self):
        search_id = self.search()
        (storage.DATA / 'session.json').write_text('{}')
        async def fail(search):
            raise CollectionError('reconnect_required')
        with patch.object(service, 'collect', fail):
            response = self.client.post(f'/searches/{search_id}/poll', headers=self.auth)
        self.assertEqual(response.status_code, 502)
        row = self.client.get('/searches', headers=self.auth).json()[0]
        self.assertEqual(row['error'], 'reconnect_required')
        self.assertIsNone(row['last_success'])
        self.assertEqual(self.client.post(f'/searches/{search_id}/poll', headers=self.auth).status_code, 409)

    def test_successful_poll_and_pause(self):
        search_id = self.search()
        (storage.DATA / 'session.json').write_text('{}')
        async def success(search):
            return [{'id':'123', 'title':'Engineer', 'url':'https://www.linkedin.com/jobs/view/123/'}]
        with patch.object(service, 'collect', success):
            response = self.client.post(f'/searches/{search_id}/poll', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['new'], 1)
        self.assertIsNotNone(service.get_search(search_id)['last_success'])
        from datetime import datetime
        search=service.get_search(search_id)
        self.assertAlmostEqual((datetime.fromisoformat(search['next_due'])-datetime.fromisoformat(search['last_attempt'])).total_seconds(),3600,delta=1)
        self.client.patch(f'/searches/{search_id}',json={'max_pages':8},headers=self.auth)
        self.assertEqual(service.get_search(search_id)['next_due'],search['next_due'])
        self.client.patch(f'/searches/{search_id}', json={'enabled':False}, headers=self.auth)
        self.assertFalse(service.due(service.get_search(search_id)))
        self.assertEqual(self.client.delete(f'/searches/{search_id}', headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get('/jobs', headers=self.auth).json()['items'], [])

    def test_input_validation(self):
        for body in [{'keywords':'   '}, {'keywords':'Engineer','interval_minutes':1}]:
            self.assertEqual(self.client.post('/searches', json=body, headers=self.auth).status_code, 422)
        self.assertEqual(service.retry_delay({'interval_minutes':10080,'failures':5}),10080)
        self.assertEqual(service.retry_delay({'interval_minutes':30,'failures':4}),360)

    def test_application_workflow_and_followups(self):
        search_id = self.search()
        storage.save_jobs(search_id, [{'id':'123','title':'Engineer','url':'https://www.linkedin.com/jobs/view/123/'}])
        response = self.client.post('/applications', headers=self.auth,
            json={'job_id':'123','follow_up_at':'2020-01-01T12:00:00+02:00','cv_version':'backend-v2'})
        self.assertEqual(response.status_code, 201)
        application_id = response.json()['id']
        self.assertEqual(self.client.post('/applications', headers=self.auth,json={'job_id':'123'}).status_code, 409)
        updated = self.client.patch(f'/applications/{application_id}', headers=self.auth,json={'status':'applied','notes':'Sent application'})
        self.assertEqual(updated.status_code, 200)
        self.assertIsNotNone(updated.json()['applied_at'])
        self.assertEqual(len(updated.json()['history']), 2)
        self.assertEqual(self.client.get('/applications?due_only=true',headers=self.auth).json()['total'], 1)
        self.client.patch(f'/applications/{application_id}', headers=self.auth,json={'status':'rejected'})
        self.assertEqual(self.client.get('/applications?due_only=true',headers=self.auth).json()['total'], 0)
        self.assertEqual(self.client.patch(f'/applications/{application_id}',headers=self.auth,json={'status':'invalid'}).status_code, 422)
        self.assertEqual(self.client.patch(f'/applications/{application_id}',headers=self.auth,json={'follow_up_at':'2026-01-01T10:00:00'}).status_code, 422)
        self.assertEqual(self.client.get('/overview',headers=self.auth).json()['applications_by_status']['rejected'], 1)

    def test_alerts_and_detail_persistence(self):
        search_id = self.search()
        storage.save_jobs(search_id, [{'id':'123','title':'Engineer','url':'https://www.linkedin.com/jobs/view/123/'}])
        storage.save_details('123', {'description':'Python engineering','employment_type':'Full-time'})
        self.assertEqual(self.client.get('/jobs/123',headers=self.auth).json()['description'], 'Python engineering')
        self.assertEqual(self.client.get('/catalog?q=Python',headers=self.auth).json()['total'], 1)
        storage.alert('test', 'first'); storage.alert('test', 'second')
        rows = self.client.get('/alerts',headers=self.auth).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message'], 'second')
        self.assertEqual(self.client.post(f"/alerts/{rows[0]['id']}/acknowledge",headers=self.auth).status_code, 200)
        storage.resolve_alert('test')
        self.assertEqual(self.client.get('/alerts',headers=self.auth).json(), [])

    def test_partial_collection_and_persistent_throttle(self):
        from collector import CollectionResult
        search_id = self.search()
        (storage.DATA / 'session.json').write_text('{}')
        async def partial(search):
            return CollectionResult([{'id':'123','title':'Engineer','url':'https://www.linkedin.com/jobs/view/123/'}],pages=2,truncated=True,error='rate_limited')
        with patch.object(service, 'collect', partial):
            response = self.client.post(f'/searches/{search_id}/poll',headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['coverage_limited'])
        self.assertEqual(self.client.get('/runs',headers=self.auth).json()['items'][0]['status'], 'partial')
        self.assertEqual(self.client.get('/searches',headers=self.auth).json()[0]['status'], 'partial')
        service.last_network_attempt = 0
        second = self.search()
        self.assertEqual(self.client.post(f'/searches/{second}/poll',headers=self.auth).status_code, 409)

    def test_reconnect_blocks_retries_until_session_changes(self):
        import time
        search_id = self.search()
        session = storage.DATA / 'session.json'
        session.write_text('{}')
        service.network_failure('reconnect_required')
        self.assertEqual(self.client.post(f'/searches/{search_id}/poll',headers=self.auth).status_code, 409)
        import os
        os.utime(session,(time.time()+1,time.time()+1))
        async def success(search):
            return []
        with patch.object(service,'collect',success):
            self.assertEqual(self.client.post(f'/searches/{search_id}/poll',headers=self.auth).status_code, 200)


class MigrationTests(unittest.TestCase):
    def test_original_database_upgrades_without_losing_jobs(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            con = sqlite3.connect(folder/'jobs.sqlite3')
            con.executescript('''CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT NOT NULL,company TEXT,location TEXT,url TEXT NOT NULL,posted_text TEXT,first_seen TEXT NOT NULL);
              INSERT INTO jobs VALUES ('123','Existing',NULL,NULL,'https://example.com',NULL,'2026-01-01');''')
            con.close()
            with patch.object(storage,'DATA',folder):
                storage.init(); storage.init()
                with storage.db() as con:
                    row = dict(con.execute('SELECT * FROM jobs').fetchone())
                    self.assertEqual(row['title'], 'Existing')
                    self.assertEqual(row['details_status'], 'pending')


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_deduplicates_and_reports_limit(self):
        import collector
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        @asynccontextmanager
        async def session():
            class Context:
                async def new_page(self): return object()
            yield Context()
        def batch(start):
            return [{'id':str(i),'title':'Role'} for i in range(start,start+25)]
        with patch.object(collector,'browser_session',session), \
             patch.object(collector,'collection_page',AsyncMock(side_effect=[(batch(0),False),(batch(20),False)])), \
             patch.object(collector.asyncio,'sleep',AsyncMock()):
            result = await collector.collect({'keywords':'Engineer','location':'','remote':False,'max_pages':2})
        self.assertEqual(len(result),45)
        self.assertTrue(result.truncated)
        self.assertEqual(result.pages,2)

    async def test_midrun_failure_preserves_earlier_page(self):
        import collector
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        @asynccontextmanager
        async def session():
            class Context:
                async def new_page(self): return object()
            yield Context()
        batch = [{'id':str(i),'title':'Role'} for i in range(25)]
        with patch.object(collector,'browser_session',session), \
             patch.object(collector,'collection_page',AsyncMock(side_effect=[(batch,False),CollectionError('access_denied')])), \
             patch.object(collector.asyncio,'sleep',AsyncMock()):
            result = await collector.collect({'keywords':'Engineer','location':'','remote':False})
        self.assertEqual(len(result),25)
        self.assertEqual(result.error,'access_denied')
        self.assertTrue(result.truncated)

    async def test_confirmed_last_page_is_not_a_coverage_error(self):
        import collector
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        @asynccontextmanager
        async def session():
            class Context:
                async def new_page(self): return object()
            yield Context()
        with patch.object(collector,'browser_session',session), \
             patch.object(collector,'collection_page',AsyncMock(return_value=([{'id':'1','title':'Role'}],True))):
            result=await collector.collect({'keywords':'Engineer','location':'','remote':False})
        self.assertFalse(result.truncated)
        self.assertEqual(result.pages,1)

    async def test_hourly_window_is_short_and_catches_up_after_outage(self):
        from datetime import datetime,timezone,timedelta
        from collector import lookback
        now=datetime.now(timezone.utc)
        self.assertEqual(lookback({'lookback_hours':48}),48)
        self.assertLessEqual(lookback({'lookback_hours':48,'last_success':(now-timedelta(minutes=59)).isoformat()}),3)
        self.assertGreaterEqual(lookback({'lookback_hours':48,'last_success':(now-timedelta(hours=12)).isoformat()}),14)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_search_wins_over_description_backlog(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as temp, patch.object(storage,'DATA',Path(temp)):
            storage.init()
            (storage.DATA/'session.json').write_text('{}')
            with storage.db() as con:
                search_id=con.execute("INSERT INTO searches(keywords,location,remote,interval_minutes) VALUES ('Engineer','Israel',0,60)").lastrowid
            storage.save_jobs(search_id,[{'id':'1','title':'Engineer','url':'https://www.linkedin.com/jobs/view/1/'}])
            ticks=0
            async def tick(delay):
                nonlocal ticks
                ticks+=1
                if ticks>1:
                    raise asyncio.CancelledError()
            with patch.object(service.asyncio,'sleep',tick),patch.object(service,'poll',AsyncMock()) as poll,patch.object(service,'enrich',AsyncMock()) as enrich:
                with self.assertRaises(asyncio.CancelledError):
                    await service.worker()
                poll.assert_awaited_once_with(search_id)
                enrich.assert_not_awaited()


class BrowserTests(unittest.TestCase):
    def test_page_count_confirms_end_without_next_button(self):
        async def run():
            from playwright.async_api import async_playwright
            from collector import PAGE_END
            async with async_playwright() as p:
                browser=await p.chromium.launch()
                try:
                    page=await browser.new_page()
                    await page.set_content('<p class="jobs-search-pagination__page-state">Page 3 of 3</p>')
                    self.assertTrue(await page.evaluate(PAGE_END))
                    await page.set_content('<p class="jobs-search-pagination__page-state">Page 2 of 3</p>')
                    self.assertFalse(await page.evaluate(PAGE_END))
                    await page.set_content('<div>No pagination information</div>')
                    self.assertIsNone(await page.evaluate(PAGE_END))
                finally:
                    await browser.close()
        asyncio.run(run())

    def test_new_description_layout_is_scoped_to_job(self):
        async def run():
            from playwright.async_api import async_playwright
            from collector import DETAIL_EXTRACT
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page()
                    await page.set_content('''<main><h1>Engineer</h1><p>Full-time</p>
                      <div><div><h2>About the job</h2></div><p><span data-testid="expandable-text-box">Build APIs.<br>Python required.<button>… more</button></span></p></div>
                      <div><h2>About the company</h2><span data-testid="expandable-text-box">Unrelated company description</span></div></main>''')
                    result = await page.evaluate(DETAIL_EXTRACT)
                    self.assertIn('Python required.',result['description'])
                    self.assertNotIn('Unrelated',result['description'])
                    self.assertNotIn('… more',result['description'])
                    self.assertEqual(result['employment_type'],'Full-time')
                    self.assertIsNone(result['salary_text'])
                finally:
                    await browser.close()
        asyncio.run(run())

    def test_browser_extracts_cards(self):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page()
                    await page.set_content('''<ul><li data-occludable-job-id="123">
                    <a href="https://www.linkedin.com/jobs/view/123/" class="job-card-list__title--link"><strong>Backend Engineer</strong></a>
                    <div class="artdeco-entity-lockup__subtitle">Example</div>
                    <div class="job-card-container__metadata-item">Remote</div>
                    <span class="job-card-container__footer-item">Viewed</span>
                    <span class="job-card-container__footer-item">1 hour ago</span></li>
                    <li data-occludable-job-id="123"><a href="/jobs/view/123/">Duplicate</a></li></ul>''')
                    rows = await page.evaluate(EXTRACT)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['company'], 'Example')
                    self.assertEqual(rows[0]['title'], 'Backend Engineer')
                    self.assertEqual(rows[0]['posted_text'], '1 hour ago')
                finally:
                    await browser.close()
        asyncio.run(run())

    def test_interactive_desktop_cancel(self):
        import connect
        import urllib.request
        import io
        import contextlib
        def cancel(prompt):
            with urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=5) as response:
                self.assertEqual(response.status, 200)
            return 'cancel'
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'session.json').write_text('existing-session')
            with patch.object(connect, 'DATA', folder), patch('builtins.input', cancel), patch('playwright.async_api.Page.goto', new_callable=AsyncMock):
                with contextlib.redirect_stdout(io.StringIO()):
                    asyncio.run(connect.main())
            self.assertEqual((folder / 'session.json').read_text(), 'existing-session')
            self.assertFalse((folder / 'connecting.lock').exists())


if __name__ == '__main__':
    unittest.main()
