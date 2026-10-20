"""End-to-end demo tests. Screenshots contain only built-in fictional records."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with socket.socket() as s:
            s.bind(('127.0.0.1',0)); cls.port=s.getsockname()[1]
        env={**os.environ,'DASHBOARD_DEMO':'1','DASHBOARD_STATE_DIR':cls.temp.name,
             'PYTHONDONTWRITEBYTECODE':'1'}
        cls.process=subprocess.Popen([sys.executable,'-m','uvicorn','dashboard:app','--host','127.0.0.1',
            '--port',str(cls.port),'--no-access-log'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cls.url=f'http://127.0.0.1:{cls.port}'
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url+'/health',timeout=1).close();break
            except OSError:time.sleep(.1)
        else:
            cls.process.terminate();raise RuntimeError('Demo dashboard did not start')
        cls.pw=sync_playwright().start()
        options={'executable_path':os.environ['DASHBOARD_BROWSER_PATH']} if os.environ.get('DASHBOARD_BROWSER_PATH') else {}
        cls.browser=cls.pw.chromium.launch(headless=True,args=['--no-sandbox'],**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()
        cls.process.terminate();cls.process.wait(timeout=10);cls.temp.cleanup()

    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1440,'height':1050})
        self.errors=[]
        self.page.on('pageerror',lambda err:self.errors.append(str(err)))
        self.page.goto(self.url)
        self.page.locator('.job-card').first.wait_for()

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors,[])

    def screenshot(self,name):
        folder=os.environ.get('DASHBOARD_SCREENSHOTS')
        if folder:
            path=Path(folder);path.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(path/name),full_page=False)

    def test_settings_mobile_save_reload_and_validation(self):
        p=self.page
        p.set_viewport_size({'width':390,'height':844})
        p.locator('[data-view="settings"]').click()
        p.locator('#pref-age').wait_for()
        p.locator('[name="pref-levels"][value="senior"]').check()
        expect(p.locator('[name="pref-levels"][value="all"]')).not_to_be_checked()
        p.locator('#pref-age').fill('72')
        p.locator('#pref-interval').fill('120')
        p.locator('#quiet-enabled').check()
        p.locator('#quiet-start').fill('23:00')
        p.locator('#quiet-end').fill('23:00')
        p.locator('#settings-save').click()
        expect(p.locator('#settings-error')).to_contain_text('different start')
        p.locator('#quiet-end').fill('08:00')
        p.locator('#settings-save').click()
        expect(p.locator('#settings-status')).to_have_text('Saved · shared with Telegram')
        p.locator('#settings-reload').click()
        expect(p.locator('#settings-status')).to_contain_text('Up to date')
        expect(p.locator('#pref-age')).to_have_value('72')
        expect(p.locator('#pref-interval')).to_have_value('120')
        expect(p.locator('#quiet-enabled')).to_be_checked()
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        nav=p.locator('[data-view="settings"]').bounding_box()
        self.assertLessEqual(nav['y']+nav['height'],844)
        p.locator('#toast').wait_for(state='hidden')
        p.locator('#page-title').scroll_into_view_if_needed()
        self.screenshot('dashboard-settings-mobile.png')
        p.set_viewport_size({'width':1440,'height':1050})
        self.screenshot('dashboard-settings.png')

    def test_desktop_details_dismiss_undo_and_filter(self):
        p=self.page
        p.locator('.card-open').first.click()
        p.locator('.description').wait_for()
        self.assertIn('fictional',p.locator('.description').inner_text())
        self.screenshot('dashboard-desktop.png')
        card=p.locator('.job-card').first
        job_id=card.get_attribute('data-job')
        card.locator('[data-dismiss]').click()
        p.locator('#undo').wait_for(state='visible')
        self.assertEqual(p.locator(f'.job-card[data-job="{job_id}"]').count(),0)
        p.locator('#undo').click()
        p.locator(f'.job-card[data-job="{job_id}"]').wait_for()
        p.select_option('#role','embedded')
        expect(p.locator('#results-count')).to_have_text('22 opportunities')
        expect(p.locator('#job-list')).to_have_attribute('aria-busy','false')
        self.assertTrue(all('Embedded' in s for s in p.locator('.card-bottom .pill:first-child').all_inner_texts()))
        p.locator('#query').fill('no such fictional company')
        p.locator('.empty').wait_for()
        self.assertIn('No jobs match',p.locator('.empty').inner_text())

    def test_shortlist_application_notes_and_board(self):
        p=self.page
        p.locator('.job-card[data-job="9000"] [data-save]').click()
        p.locator('#toast').filter(has_text='Added to your shortlist').wait_for()
        p.locator('.job-card[data-job="9000"] [data-save]').click()
        p.locator('#editor').wait_for(state='visible')
        p.select_option('#app-stage','applied')
        p.locator('#app-notes').fill('Fictional demo note for UI verification.')
        p.locator('#editor-form button[type="submit"]').click()
        p.locator('#editor').wait_for(state='hidden')
        p.locator('[data-view="applications"]').click()
        p.locator('.board-card').first.wait_for()
        self.assertIn('Firmware Engineer',p.locator('#board').inner_text())
        self.screenshot('dashboard-applications.png')

    def test_saved_job_can_be_dismissed_and_restored_in_shortlist(self):
        p=self.page
        p.locator('[data-view="saved"]').click()
        card=p.locator('.job-card[data-job="9001"]')
        card.wait_for()
        card.locator('[data-dismiss]').click()
        expect(card.locator('[data-dismiss]')).to_have_attribute('aria-label','Restore job')
        card.locator('[data-dismiss]').click()
        expect(card.locator('[data-dismiss]')).to_have_attribute('aria-label','Dismiss job')

    def test_mobile_filters_detail_and_no_page_overflow(self):
        p=self.page
        p.set_viewport_size({'width':390,'height':844})
        self.assertTrue(p.locator('#filter-toggle').is_visible())
        self.assertFalse(p.locator('#filters').is_visible())
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
        self.screenshot('dashboard-mobile.png')
        p.locator('#filter-toggle').click()
        self.assertTrue(p.locator('#filters').is_visible())
        p.select_option('#mode','Remote')
        expect(p.locator('#results-count')).to_have_text('14 opportunities')
        expect(p.locator('#job-list')).to_have_attribute('aria-busy','false')
        p.locator('.card-open').first.click()
        p.locator('.description').wait_for()
        self.assertTrue(p.locator('#detail-panel').is_visible())
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
        self.screenshot('dashboard-mobile-detail.png')
        p.locator('[data-close-detail]').click()
        self.assertFalse(p.locator('#detail-panel').is_visible())

    def test_search_controls(self):
        p=self.page
        p.locator('[data-view="searches"]').click()
        p.locator('.search-card').first.wait_for()
        p.locator('[data-toggle-search]').first.click()
        p.locator('.search-card').first.locator('.pill').filter(has_text='Paused').wait_for()
        p.locator('[data-toggle-search]').first.click()
        p.locator('.search-card').first.locator('.pill').filter(has_text='Active').wait_for()
        p.locator('#add-search').click()
        p.fill('#search-title','Fictional Systems Engineer')
        p.fill('#search-location','Israel')
        p.locator('#editor-form button[type="submit"]').click()
        p.locator('.search-card').filter(has_text='Fictional Systems Engineer').wait_for()
        self.screenshot('dashboard-searches.png')
