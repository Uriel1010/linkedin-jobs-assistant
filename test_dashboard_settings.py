import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import dashboard
from telegram_bot import State


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bot = State(root / 'bot')
        self.bot.set('feed_initialized', True)
        self.bot.set('notify', True)
        self.patches = [patch.dict(os.environ, {'BOT_STATE_DB': str(root/'bot/bot.sqlite3'), 'DASHBOARD_DEMO': '0'}),
                        patch.object(dashboard, 'DEMO', False), patch.object(dashboard, 'STATE', root/'dashboard')]
        for p in self.patches: p.start()
        dashboard.attempts.clear()
        self.client = TestClient(dashboard.app)
        self.headers = {'X-Dashboard-Request': '1'}
        self.client.post('/api/login', json={'key': dashboard.access_key()}, headers=self.headers)

    def tearDown(self):
        self.client.close()
        self.bot.con.close()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def get(self):
        response = self.client.get('/api/settings')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def save(self, body):
        return self.client.put('/api/settings', json=body, headers=self.headers)

    def test_save_shared_settings_and_preserve_sent_jobs(self):
        self.bot.con.execute("INSERT INTO deliveries VALUES ('sent','{}','sent',1,2,NULL,1)")
        self.bot.con.execute("INSERT INTO deliveries VALUES ('filtered','{}','filtered',1,NULL,'role',NULL)")
        self.bot.con.commit()
        body = self.get()
        body['preferences']['roles'] = ['embedded', 'qa']
        body['preferences']['exclude'] = ['שיווק']
        body['quiet_hours'] = {'enabled': True, 'start': 1380, 'end': 480, 'timezone': 'Asia/Jerusalem'}
        body['notify'] = False
        self.assertEqual(self.save(body).status_code, 200)
        self.assertEqual(self.bot.preferences()['roles'], ['embedded', 'qa'])
        self.assertFalse(self.bot.get('notify'))
        self.assertTrue(self.bot.get('quiet_hours')['enabled'])
        statuses = dict(self.bot.con.execute('SELECT job_id,status FROM deliveries'))
        self.assertEqual(statuses, {'sent': 'sent', 'filtered': 'pending'})

    def test_conflicting_telegram_change_is_not_overwritten(self):
        body = self.get()
        self.bot.set('notify', False)
        self.assertEqual(self.save(body).status_code, 409)
        self.assertFalse(self.bot.get('notify'))

    def test_invalid_inputs_are_atomic(self):
        original = self.get()
        for section, key, value in [('quiet_hours', 'timezone', 'invalid/zone'),
                                    ('quiet_hours', 'start', 1440), ('preferences', 'roles', []),
                                    ('preferences', 'roles', ['invalid']), ('preferences', 'max_age_hours', 999),
                                    ('preferences', 'exclude', ['a']*21)]:
            body = copy.deepcopy(original)
            body[section][key] = value
            self.assertEqual(self.save(body).status_code, 422)
            self.assertEqual(self.get(), original)
        body = copy.deepcopy(original)
        body['quiet_hours'].update(enabled=True, start=0, end=0)
        self.assertEqual(self.save(body).status_code, 422)

    def test_auth_csrf_and_allowlist(self):
        self.bot.set('secret_example', 'not-a-real-secret')
        self.assertNotIn('secret_example', str(self.get()))
        self.assertEqual(self.client.put('/api/settings', json=self.get()).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/settings').status_code, 401)

    def test_first_notification_enable_requires_bot_initialization(self):
        self.bot.set('feed_initialized', False)
        body = self.get()
        body['notify'] = True
        self.assertEqual(self.save(body).status_code, 409)

    def test_interval_reschedules_only_when_changed(self):
        body=self.get()
        self.assertEqual(body['notification_interval_minutes'],60)
        body['notification_interval_minutes']=120
        with patch('dashboard_settings.time.time',return_value=1000):
            self.assertEqual(self.save(body).status_code,200)
        self.assertEqual(self.bot.get('next_digest_at'),8200)
        self.assertEqual(self.save(self.get()).status_code,200)
        self.assertEqual(self.bot.get('next_digest_at'),8200)
        body=self.get(); body['notification_interval_minutes']=14
        self.assertEqual(self.save(body).status_code,422)


if __name__ == '__main__':
    unittest.main()
