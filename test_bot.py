import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock,patch
from telegram_bot import Bot, State, ServiceError

OWNER = 123456789
JOB = {'id':'123','title':'Software Engineer','company':'Example','location':'Israel',
       'url':'https://www.linkedin.com/jobs/view/123/','description':'Build APIs', 'details_status':'ok'}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(self.temp.name)
        self.bot = Bot('fake-token',OWNER,'fake-key',self.state)
        self.bot.api = Mock()
        self.bot.send = Mock(return_value={'message_id':100})
        self.bot.telegram = Mock()
        self.bot.upload_profile_photo = Mock()

    def tearDown(self):
        self.state.con.close()
        self.temp.cleanup()

    def message(self,text,owner=OWNER,kind='private',chat=OWNER):
        return {'message':{'from':{'id':owner},'chat':{'id':chat,'type':kind},'text':text}}

    def test_other_users_and_groups_are_ignored(self):
        for update in [self.message('/jobs',owner=9),self.message('/jobs',kind='group'),self.message('/jobs',chat=9)]:
            self.bot.handle(update)
        self.bot.api.assert_not_called()
        self.bot.send.assert_not_called()

    def test_authorized_job_browsing_uses_catalog(self):
        self.bot.api.return_value = {'items':[JOB],'total':6}
        self.bot.handle(self.message('/jobs Python & APIs'))
        self.assertIn('/catalog?q=Python+%26+APIs',self.bot.api.call_args.args[1])
        self.assertIn('Engineer',self.bot.send.call_args.args[0])
        self.assertEqual(self.bot.send.call_args.args[1][-1][0]['text'],'Next')

    def test_unauthorized_callback_cannot_write(self):
        self.bot.handle({'callback_query':{'id':'x','from':{'id':9},'data':'save:123','message':{'chat':{'id':OWNER,'type':'private'}}}})
        self.bot.api.assert_not_called()
        self.bot.telegram.assert_not_called()

    def test_save_job_calls_application_api(self):
        self.bot.api.return_value = {'id':2,'title':'Engineer'}
        self.bot.handle(self.message('/save 123'))
        self.bot.api.assert_called_once_with('POST','/applications',{'job_id':'123'})

    def test_invalid_stage_and_id_do_not_mutate(self):
        self.bot.handle(self.message('/stage 4 invented'))
        self.bot.handle(self.message('/refresh ../secrets'))
        self.bot.api.assert_not_called()

    def test_track_criteria_are_explicit(self):
        self.bot.handle(self.message('/track'))
        self.bot.api.assert_not_called()
        self.bot.api.return_value = {'created':1,'existing':0,'items':[{'id':1,'keywords':'Python','location':'Israel','created':True,'enabled':True}]}
        self.bot.handle(self.message('/track Python | Israel'))
        self.bot.api.assert_called_once_with('POST','/searches/bulk',{'searches':[{'keywords':'Python','location':'Israel'}]})

    def test_track_pasted_commands_use_one_bulk_request_and_one_reply(self):
        titles=['Embedded Software Engineer','Firmware Engineer','Embedded Linux Engineer','Low Level Software Engineer',
                'Linux Kernel Engineer','Device Driver Engineer','System Software Engineer','BSP Engineer']
        self.bot.api.return_value={'created':8,'existing':0,'items':[
            {'id':index,'keywords':title,'location':'Israel','created':True,'enabled':True}
            for index,title in enumerate(titles,1)]}
        self.bot.handle(self.message('\n'.join('/track '+title+' | Israel' for title in titles)))
        self.bot.api.assert_called_once_with('POST','/searches/bulk',{'searches':[
            {'keywords':title,'location':'Israel'} for title in titles]})
        self.bot.send.assert_called_once()
        self.assertIn('8 added',self.bot.send.call_args.args[0])

    def test_track_invalid_later_line_creates_nothing(self):
        for command in ('/track Firmware Engineer | Israel\n/track missing separator',
                        '/track Firmware Engineer | Israel; | Israel',
                        '/track Firmware Engineer | Israel; /pause 1 | Israel'):
            self.bot.handle(self.message(command))
        self.bot.api.assert_not_called()

    def test_track_newline_settings_and_existing_paused_search(self):
        self.bot.api.return_value={'created':0,'existing':1,'items':[
            {'id':3,'keywords':'Firmware Engineer','location':'','created':False,'enabled':False}]}
        self.bot.handle(self.message('/track\nFirmware Engineer |'))
        self.bot.api.assert_called_once_with('POST','/searches/bulk',{'searches':[{'keywords':'Firmware Engineer','location':''}]})
        self.assertIn('Already tracked · paused',self.bot.send.call_args.args[0])

    def test_notifications_off_by_default(self):
        self.bot.notifications()
        self.bot.api.assert_not_called()

    def test_notifications_deduplicate_jobs_across_searches(self):
        self.state.set('notify',True); self.state.set('chat_ready',True)
        def api(method,path,payload=None):
            if path.startswith('/jobs?'):
                return {'items':[JOB,JOB],'next_after_id':2}
            if path.startswith('/alerts'):
                return []
            if path=='/searches':
                return []
            return {'items':[]}
        self.bot.api.side_effect = api
        self.bot.notifications()
        self.assertIn('1 newly found job',self.bot.send.call_args.args[0])
        self.assertEqual(self.state.get('cursor'),2)
        self.bot.send.reset_mock()
        self.bot.notifications()
        self.bot.send.assert_not_called()

    def test_state_persists_across_restart(self):
        self.state.set('offset',1234)
        self.state.con.close()
        self.state = State(self.temp.name)
        self.assertEqual(self.state.get('offset'),1234)

    def test_chunks_respect_message_size(self):
        bot = Bot('fake-token',OWNER,'fake-key',self.state)
        bot.telegram = Mock()
        with patch('telegram_bot.time.sleep'):
            bot.send('x'*8500)
        self.assertEqual(bot.telegram.call_count,3)
        self.assertTrue(all(len(c.args[1]['text'])<=3800 for c in bot.telegram.call_args_list))

    def feed_api(self, jobs):
        def api(method,path,payload=None):
            if path.startswith('/jobs?'):
                return {'items':jobs,'next_after_id':len(jobs)}
            if path.startswith('/alerts') or path=='/searches':
                return []
            return {'items':[]}
        self.bot.api.side_effect=api
        self.state.set('notify',True); self.state.set('chat_ready',True)

    def test_large_batch_delivers_every_link(self):
        jobs = [{**JOB,'id':str(i),'url':f'https://www.linkedin.com/jobs/view/{i}/'} for i in range(25)]
        self.feed_api(jobs)
        self.bot.notifications()
        self.assertEqual(self.bot.send.call_count,1)
        self.assertIn('25 newly found jobs',self.bot.send.call_args.args[0])
        self.assertIn('Page 1/5',self.bot.send.call_args.args[0])
        buttons=self.bot.send.call_args.args[1][0]
        self.assertEqual([b['text'] for b in buttons],['1/5 ▦','Next ›','»'])
        text=self.bot.send.call_args.args[0]
        for part in range(2,6):
            self.bot.feed_page(1,part,100)
            method,payload=self.bot.telegram.call_args.args
            self.assertEqual(method,'editMessageText')
            self.assertEqual(payload['message_id'],100)
            self.assertIn(f'Page {part}/5',payload['text'])
            text+='\n'+payload['text']
        for job in jobs:
            self.assertIn(job['url'],text)
        self.assertEqual(self.state.pending_count(),0)

    def test_failed_delivery_remains_queued_and_resumes(self):
        jobs = [{**JOB,'id':str(i)} for i in range(12)]
        self.feed_api(jobs)
        self.bot.send.side_effect=ServiceError(503)
        with self.assertRaises(ServiceError):
            self.bot.notifications()
        self.assertEqual(self.state.get('cursor'),12)
        self.assertEqual(self.state.pending_count(),12)
        self.bot.send.side_effect=None
        self.bot.notifications()
        self.assertEqual(self.state.pending_count(),0)
        self.bot.send.reset_mock()
        self.bot.notifications()
        self.bot.send.assert_not_called()

    def test_baseline_is_silent_and_never_resent(self):
        self.state.seed_baseline([JOB],9)
        self.state.enqueue([JOB,{**JOB,'id':'456'}],11)
        self.assertEqual([job['id'] for job in self.state.pending()],['456'])
        self.assertEqual(self.state.get('cursor'),11)

    def test_deduplication_survives_more_than_thousand_jobs(self):
        self.state.enqueue([{**JOB,'id':str(i)} for i in range(1200)],1200)
        self.state.delivered(['0'])
        self.state.con.close()
        self.state=State(self.temp.name)
        self.state.enqueue([{**JOB,'id':'0'}],1201)
        self.assertEqual(self.state.pending_count(),1199)

    def test_notification_resume_does_not_drop_backlog(self):
        self.state.seed_baseline([],0)
        self.state.enqueue([JOB],1)
        self.state.set('notify',False)
        self.bot.command('/notify on')
        self.bot.api.assert_not_called()
        self.assertEqual(self.state.pending_count(),1)
        self.assertEqual(self.state.get('cursor'),1)

    def test_filtering_keeps_database_and_records_reason(self):
        self.feed_api([JOB,{**JOB,'id':'2','title':'DevOps Engineer'}])
        self.bot.notifications()
        self.assertEqual(self.bot.send.call_count,1)
        row=self.state.con.execute("SELECT status,reason FROM deliveries WHERE job_id='2'").fetchone()
        self.assertEqual(row['status'],'filtered')
        self.assertIn('devops',row['reason'])
        self.assertEqual(self.state.get('cursor'),2)

    def test_preferences_are_editable_and_unknown_level_policy_is_explicit(self):
        self.bot.command('/preferences levels senior')
        self.assertEqual(self.state.preferences()['levels'],['senior'])
        self.bot.command('/preferences region broad')
        self.assertEqual(self.state.preferences()['region'],'broad')
        self.bot.handle(self.message('/preferences levels invented'))
        self.assertEqual(self.state.preferences()['levels'],['senior'])

    def test_combined_preferences_match_requested_profile_and_save_once(self):
        command=('/preferences roles embedded,software,qa,other; levels mid,senior; region israel; '
            'age 72; unknown keep; include clear; '
            'exclude frontend,full stack,web developer,marketing,product manager,technical support')
        with patch.object(self.state,'set_preferences',wraps=self.state.set_preferences) as save:
            self.bot.handle(self.message(command))
        save.assert_called_once()
        prefs=self.state.preferences()
        self.assertEqual(prefs['roles'],['embedded','software','qa','other'])
        self.assertEqual(prefs['levels'],['mid','senior'])
        self.assertEqual(prefs['region'],'israel')
        self.assertEqual(prefs['max_age_hours'],72)
        self.assertTrue(prefs['keep_unknown_level'])
        self.assertEqual(prefs['include'],[])
        self.assertEqual(prefs['exclude'],['frontend','full stack','web developer','marketing','product manager','technical support'])
        self.bot.send.assert_called_once()

    def test_pasted_preference_commands_form_one_atomic_update(self):
        command='/preferences roles embedded,software\n/preferences levels mid,senior\n/preferences@my_jobs_bot age 72\n/preferences exclude full stack,technical support'
        self.bot.handle(self.message(command))
        prefs=self.state.preferences()
        self.assertEqual(prefs['roles'],['embedded','software'])
        self.assertEqual(prefs['levels'],['mid','senior'])
        self.assertEqual(prefs['max_age_hours'],72)
        self.assertEqual(prefs['exclude'],['full stack','technical support'])
        self.bot.send.assert_called_once()

    def test_preferences_support_one_command_followed_by_newline_settings(self):
        self.bot.handle(self.message('/preferences\nroles qa\nlevels mid\nage 72\nexclude full stack,web developer'))
        self.assertEqual(self.state.preferences()['roles'],['qa'])
        self.assertEqual(self.state.preferences()['levels'],['mid'])
        self.assertEqual(self.state.preferences()['max_age_hours'],72)
        self.assertEqual(self.state.preferences()['exclude'],['full stack','web developer'])

    def test_invalid_combined_preferences_preserve_settings_and_delivery_plan(self):
        self.state.enqueue([JOB],1)
        self.state.prepare_batch()
        before=self.state.preferences()
        plan=[tuple(row) for row in self.state.con.execute('SELECT * FROM delivery_parts')]
        for args in ('roles qa; age 999','roles qa; levels invented','roles qa; age',
                     'roles qa; region moon','roles qa; unknown maybe','roles qa; roles software','; ;'):
            with patch.object(self.state,'set_preferences',wraps=self.state.set_preferences) as save:
                self.bot.handle(self.message('/preferences '+args))
            save.assert_not_called()
            self.assertEqual(self.state.preferences(),before)
            self.assertEqual([tuple(row) for row in self.state.con.execute('SELECT * FROM delivery_parts')],plan)

    def test_unsent_numbered_batch_survives_restart(self):
        self.state.enqueue([{**JOB,'id':str(i)} for i in range(7)],7)
        self.state.prepare_batch()
        before=[tuple(row) for row in self.state.con.execute('SELECT batch_id,part,text FROM delivery_parts ORDER BY part')]
        self.state.con.close(); self.state=State(self.temp.name)
        self.state.prepare_batch()
        after=[tuple(row) for row in self.state.con.execute('SELECT batch_id,part,text FROM delivery_parts ORDER BY part')]
        self.assertEqual(before,after)
        self.assertEqual(self.state.get('cursor'),7)

    def test_status_is_one_message_in_israel_time(self):
        overview={'jobs':100,'follow_ups_due':0,'collector':{'network':{},'open_alerts':0,
            'descriptions_pending':2,'descriptions_failed':1}}
        searches=[{'id':2,'enabled':1,'keywords':'Software Engineer','location':'Israel',
                   'last_success':'2026-10-19T15:35:00+00:00','next_due':'2026-10-19T16:35:00+00:00','status':'ok'}]
        self.bot.api.side_effect=lambda method,path:overview if path=='/overview' else searches
        self.bot.command('/status')
        self.bot.send.assert_called_once()
        text=self.bot.send.call_args.args[0]
        self.assertIn('18:35',text)
        self.assertIn('19:35',text)
        self.assertNotIn('saved: True',text)
        self.assertNotIn('UTC',text)

    def test_cursor_and_queue_are_atomic(self):
        with self.assertRaises(KeyError):
            self.state.enqueue([JOB,{}],2)
        self.assertEqual(self.state.pending_count(),0)
        self.assertEqual(self.state.get('cursor',0),0)

    def test_unicode_chunking_respects_utf16_budget(self):
        bot=Bot('fake-token',OWNER,'fake-key',self.state)
        bot.telegram=Mock()
        with patch('telegram_bot.time.sleep'):
            bot.send('😀'*4000)
        self.assertTrue(all(len(c.args[1]['text'].encode('utf-16-le'))//2<=3500 for c in bot.telegram.call_args_list))

    def test_daily_backup_is_consistent_and_idempotent(self):
        import sqlite3
        from pathlib import Path
        from maintenance import backup_database
        self.state.enqueue([JOB],1)
        folder=Path(self.temp.name)/'backups'
        self.assertTrue(backup_database(Path(self.temp.name)/'bot.sqlite3',folder,'bot'))
        self.assertFalse(backup_database(Path(self.temp.name)/'bot.sqlite3',folder,'bot'))
        copy=sqlite3.connect(next(folder.glob('*.sqlite3')))
        self.assertEqual(copy.execute('SELECT count(*) FROM deliveries').fetchone()[0],1)
        copy.close()

    def test_restart_after_message_recorded_does_not_resend_batch(self):
        jobs=[{**JOB,'id':str(i),'url':f'https://www.linkedin.com/jobs/view/{i}/'} for i in range(12)]
        self.feed_api(jobs)
        self.state.enqueue(jobs,12)
        self.state.prepare_batch()
        self.bot.feed_page(1)
        self.assertEqual(self.state.pending_count(),12)
        self.bot.send.reset_mock()
        self.bot.notifications()
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.pending_count(),0)

    def test_feed_callback_edits_original_message_after_restart(self):
        self.feed_api([{**JOB,'id':str(i)} for i in range(7)])
        self.bot.notifications()
        self.state.con.close(); self.state=State(self.temp.name); self.bot.state=self.state
        self.bot.send.reset_mock(); self.bot.telegram.reset_mock()
        self.bot.handle({'callback_query':{'id':'x','from':{'id':OWNER},'data':'feed:1:2',
            'message':{'message_id':100,'chat':{'id':OWNER,'type':'private'}}}})
        self.bot.send.assert_not_called()
        self.assertEqual(self.bot.telegram.call_args.args[0],'editMessageText')
        self.assertIn('Page 2/2',self.bot.telegram.call_args.args[1]['text'])
        self.assertEqual(self.bot.telegram.call_args.args[1]['message_id'],100)

    def test_page_cannot_switch_a_different_update_message(self):
        self.feed_api([{**JOB,'id':str(i)} for i in range(7)])
        self.bot.notifications()
        with self.assertRaises(ValueError):
            self.bot.feed_page(1,2,999)

    def test_single_page_has_no_job_keyboard_and_counter_does_not_send(self):
        self.feed_api([JOB])
        self.bot.notifications()
        self.assertEqual(self.bot.send.call_args.args[1],[])
        self.bot.send.reset_mock()
        self.bot.handle({'callback_query':{'id':'x','from':{'id':OWNER},'data':'noop',
            'message':{'message_id':100,'chat':{'id':OWNER,'type':'private'}}}})
        self.bot.send.assert_not_called()

    def test_new_reopens_latest_complete_update_with_pagination(self):
        self.feed_api([{**JOB,'id':str(i)} for i in range(12)])
        self.bot.notifications()
        self.bot.send.reset_mock(); self.bot.send.return_value={'message_id':101}
        self.bot.command('/new')
        self.bot.send.assert_called_once()
        self.assertIn('12 newly found jobs',self.bot.send.call_args.args[0])
        self.assertIn('Page 1/3',self.bot.send.call_args.args[0])
        self.bot.feed_page(1,3,101)
        self.assertIn('Page 3/3',self.bot.telegram.call_args.args[1]['text'])

    def test_catalog_callback_edits_same_message_without_job_buttons(self):
        view=self.state.view('Python')
        self.bot.api.return_value={'items':[JOB],'total':12}
        self.bot.handle({'callback_query':{'id':'x','from':{'id':OWNER},'data':f'page:{view}:5',
            'message':{'message_id':88,'chat':{'id':OWNER,'type':'private'}}}})
        self.bot.send.assert_not_called()
        method,payload=self.bot.telegram.call_args.args
        self.assertEqual(method,'editMessageText')
        self.assertEqual(payload['message_id'],88)
        self.assertTrue(all(button['callback_data'].startswith('page:')
            for row in payload['reply_markup']['inline_keyboard'] for button in row))

    def test_quiet_hours_remain_off_until_user_selects(self):
        self.bot.command('/quiet')
        self.assertFalse(self.bot.quiet_active())
        self.assertIsNone(self.state.get('quiet_hours'))

    def test_quiet_boundaries_daytime_overnight_and_dst(self):
        self.bot.command('/quiet 23:00 08:00')
        at=lambda value:datetime.fromisoformat(value)
        self.assertTrue(self.bot.quiet_active(at('2026-10-19T23:00:00+03:00')))
        self.assertTrue(self.bot.quiet_active(at('2026-10-20T07:59:00+03:00')))
        self.assertFalse(self.bot.quiet_active(at('2026-10-20T08:00:00+03:00')))
        self.assertFalse(self.bot.quiet_active(at('2026-10-19T22:59:00+03:00')))
        self.bot.command('/quiet 10:00 12:00')
        self.assertTrue(self.bot.quiet_active(at('2026-10-19T11:00:00+03:00')))
        self.assertFalse(self.bot.quiet_active(at('2026-10-19T13:00:00+03:00')))
        self.bot.command('/quiet 01:00 02:00 Asia/Jerusalem')
        self.assertTrue(self.bot.quiet_active(at('2026-10-24T22:30:00+00:00')))
        self.assertTrue(self.bot.quiet_active(at('2026-10-24T23:30:00+00:00')))

    def test_invalid_quiet_hours_do_not_change_settings(self):
        self.bot.command('/quiet 23:00 08:00')
        before=self.state.get('quiet_hours')
        for args in ('23:00 23:00','25:00 08:00','23:00','23:00 08:00 Bad/Zone'):
            with self.assertRaises(ValueError):
                self.bot.configure_quiet(args)
            self.assertEqual(self.state.get('quiet_hours'),before)

    def test_quiet_callback_sets_user_schedule(self):
        self.bot.handle({'callback_query':{'id':'x','from':{'id':OWNER},'data':'quiet:22:00 08:00',
            'message':{'message_id':88,'chat':{'id':OWNER,'type':'private'}}}})
        self.assertEqual(self.state.get('quiet_hours')['start'],22*60)
        self.bot.command('/quiet off')
        self.assertFalse(self.bot.quiet_active())

    def test_quiet_period_collects_cursor_without_sending_then_catches_up_after_restart(self):
        jobs=[{**JOB,'id':str(i)} for i in range(12)]
        self.feed_api(jobs)
        with patch.object(self.bot,'quiet_active',return_value=True):
            self.bot.notifications()
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.get('cursor'),12)
        self.assertEqual(self.state.pending_count(),12)
        self.assertTrue(self.state.get('quiet_started_at'))
        self.state.con.close(); self.state=State(self.temp.name); self.bot.state=self.state
        with patch.object(self.bot,'quiet_active',return_value=False):
            self.bot.notifications()
        self.bot.send.assert_called_once()
        self.assertIn('Catch-up after quiet hours',self.bot.send.call_args.args[0])
        self.assertIn('12 newly found jobs',self.bot.send.call_args.args[0])
        self.assertIsNone(self.state.get('quiet_started_at'))
        self.assertEqual(self.state.pending_count(),0)
        self.bot.send.reset_mock(); self.bot.notifications()
        self.bot.send.assert_not_called()

    def test_quiet_exit_requests_scan_and_includes_gap_jobs(self):
        self.feed_api([JOB])
        self.state.set('quiet_started_at','2026-10-19T20:00:00+00:00')
        calls=[]
        def api(method,path,payload=None):
            calls.append((method,path))
            if path=='/searches':
                return [{'id':2,'enabled':True}]
            if path.startswith('/jobs?'):
                self.assertIn(('POST','/searches/2/poll'),calls)
                return {'items':[JOB],'next_after_id':1}
            if path.startswith('/alerts'):
                return []
            return {'items':[]}
        self.bot.api.side_effect=api
        self.bot.notifications()
        self.assertIn('1 newly found job',self.bot.send.call_args.args[0])

    def test_queued_job_freshness_uses_discovery_time(self):
        self.state.enqueue([{**JOB,'posted_at':'2026-01-01T00:00:00+00:00'}],1)
        self.state.con.execute('UPDATE deliveries SET discovered_at=?',
            (datetime(2026,1,2,tzinfo=timezone.utc).timestamp(),))
        self.state.con.commit()
        self.state.prepare_batch(catchup=True)
        self.assertEqual(self.state.pending_count(),1)
        self.assertEqual(self.state.con.execute('SELECT count(*) FROM delivery_parts').fetchone()[0],1)

    def test_large_quiet_backlog_is_one_complete_paginated_update(self):
        self.state.enqueue([{**JOB,'id':str(i)} for i in range(501)],501)
        self.state.prepare_batch(catchup=True)
        pages=list(self.state.con.execute('SELECT batch_id,total_jobs FROM delivery_parts'))
        self.assertEqual(len(pages),101)
        self.assertEqual({row['batch_id'] for row in pages},{1})
        self.assertEqual({row['total_jobs'] for row in pages},{501})

    def test_manual_notification_pause_is_not_overridden_by_quiet_exit(self):
        self.state.set('quiet_started_at','2026-10-19T20:00:00+00:00')
        self.state.set('notify',False)
        self.bot.notifications()
        self.bot.api.assert_not_called()
        self.bot.send.assert_not_called()
        self.assertTrue(self.state.get('quiet_started_at'))

    def test_profile_registers_commands_menu_and_descriptions(self):
        self.bot.configure_profile()
        calls=self.bot.telegram.call_args_list
        commands=next(call.args[1]['commands'] for call in calls if call.args[0]=='setMyCommands')
        self.assertIn('quiet',[command['command'] for command in commands])
        self.assertIn('start',[command['command'] for command in commands])
        self.assertTrue(any(call.args[0]=='setChatMenuButton' and call.args[1].get('chat_id')==OWNER for call in calls))
        self.assertTrue(any(call.args[0]=='setMyDescription' for call in calls))

    def test_hebrew_preferences_and_aliases_are_canonical_and_atomic(self):
        self.bot.handle(self.message('העדפות תפקידים משובצת,תוכנה,בדיקות,אחר; רמות ביניים,בכיר; אזור ישראל; גיל 72; לא_ידוע שמור; כלול נקה; החרג פרונטאנד,תמיכה טכנית'))
        prefs=self.state.preferences()
        self.assertEqual(prefs['roles'],['embedded','software','qa','other'])
        self.assertEqual(prefs['levels'],['mid','senior'])
        self.assertEqual(prefs['region'],'israel')
        self.assertEqual(prefs['max_age_hours'],72)
        self.assertEqual(prefs['exclude'],['פרונטאנד','תמיכה טכנית'])
        self.assertTrue(prefs['keep_unknown_level'])
        self.assertIn('העדפות הפיד',self.bot.send.call_args.args[0])
        before=self.state.preferences()
        self.bot.handle(self.message('העדפות תפקידים בדיקות; גיל 999'))
        self.assertEqual(self.state.preferences(),before)

    def test_hebrew_bulk_search_preserves_query_and_location(self):
        self.bot.api.return_value={'created':2,'existing':0,'items':[
            {'id':1,'keywords':'מהנדס קושחה','location':'ישראל','created':True,'enabled':True},
            {'id':2,'keywords':'מהנדס תוכנה משובצת','location':'ישראל','created':True,'enabled':True}]}
        self.bot.handle(self.message('עקוב מהנדס קושחה | ישראל\nעקוב מהנדס תוכנה משובצת | ישראל'))
        self.bot.api.assert_called_once_with('POST','/searches/bulk',{'searches':[
            {'keywords':'מהנדס קושחה','location':'ישראל'},
            {'keywords':'מהנדס תוכנה משובצת','location':'ישראל'}]})
        self.assertIn('2 נוספו',self.bot.send.call_args.args[0])

    def test_language_is_persisted_and_menu_override_can_return_to_auto(self):
        self.bot.command('/language he')
        self.assertEqual(self.bot.language,'he')
        self.assertIn('שפת הבוט',self.bot.send.call_args.args[0])
        calls=[call.args[1] for call in self.bot.telegram.call_args_list if call.args[0]=='setMyCommands']
        self.assertEqual({call['language_code'] for call in calls},{'','en','he'})
        self.assertTrue(all(any(c['command']=='language' for c in call['commands']) for call in calls))
        self.state.con.close(); self.state=State(self.temp.name); self.bot.state=self.state
        self.assertEqual(self.bot.language,'he')
        self.bot.command('שפה אנגלית')
        self.assertEqual(self.bot.language,'en')
        self.bot.command('/language auto')
        self.assertEqual(self.state.get('language'),'auto')

    def test_telegram_auto_detection_and_explicit_english_override(self):
        update=self.message('/help')
        update['message']['from']['language_code']='he'
        self.bot.handle(update)
        self.assertIn('הבוט הפרטי',self.bot.send.call_args.args[0])
        self.state.set('language','en')
        self.bot.handle(update)
        self.assertIn('Your private jobs',self.bot.send.call_args.args[0])
        before=self.state.get('detected_language')
        unauthorized=self.message('/language he',owner=9)
        unauthorized['message']['from']['language_code']='en'
        self.bot.handle(unauthorized)
        self.assertEqual(self.state.get('detected_language'),before)
        self.assertEqual(self.state.get('language'),'en')

    def test_hebrew_feed_renders_existing_pages_without_resending_jobs(self):
        self.feed_api([{**JOB,'id':str(i),'title':'מהנדס/ת תוכנה {C++}','company':'Example Ltd',
            'url':f'https://www.linkedin.com/jobs/view/{i}/'} for i in range(7)])
        self.bot.notifications()
        self.state.set('language','he')
        self.bot.send.reset_mock()
        self.bot.feed_page(1,2,100)
        method,payload=self.bot.telegram.call_args.args
        self.assertEqual(method,'editMessageText')
        self.assertIn('עדכון שעתי',payload['text'])
        self.assertIn('עמוד 2/2',payload['text'])
        self.assertIn('מהנדס/ת תוכנה {C++}',payload['text'])
        self.assertIn('https://www.linkedin.com/jobs/view/5/',payload['text'])
        self.assertIn('תאריך לא צוין',payload['text'])
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.pending_count(),0)

    def test_hebrew_send_preserves_source_text_and_localizes_buttons(self):
        self.state.set('language','he')
        bot=Bot('fake-token',OWNER,'fake-key',self.state)
        bot.telegram=Mock(return_value={'message_id':1})
        text='מהנדס/ת תוכנה · Example {company}\nhttps://www.linkedin.com/jobs/view/123/'
        with patch('telegram_bot.time.sleep'):
            bot.send(text,[[{'text':'Next ›','callback_data':'feed:1:2'}]])
        payload=bot.telegram.call_args.args[1]
        self.assertEqual(payload['text'],text)
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['text'],'הבא ›')
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'],'feed:1:2')

    def test_hebrew_profile_registers_localized_descriptions(self):
        self.bot.configure_profile()
        calls=self.bot.telegram.call_args_list
        menus=[call.args[1] for call in calls if call.args[0]=='setMyCommands' and call.args[1].get('language_code')=='he']
        self.assertTrue(menus)
        self.assertTrue(all(any(command['command']=='language' for command in payload['commands']) for payload in menus))
        self.assertTrue(any(call.args[0]=='setMyDescription' and call.args[1].get('language_code')=='he' for call in calls))

    def test_api_recovery_notice_is_sent_once(self):
        self.feed_api([])
        self.state.set('api_down',True)
        self.bot.notifications()
        self.assertIn('recovered',self.bot.send.call_args.args[0])
        self.bot.send.reset_mock()
        self.bot.notifications()
        self.bot.send.assert_not_called()

    def test_telegram_rate_limit_is_sanitized_and_preserved(self):
        import io
        import urllib.error
        from telegram_bot import http_json
        error=urllib.error.HTTPError('https://api.telegram.org/botfake-token/sendMessage',429,'limit',{},
            io.BytesIO(b'{"parameters":{"retry_after":42}}'))
        with patch('telegram_bot.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(ServiceError) as caught:
                http_json('https://api.telegram.org/botfake-token/sendMessage',telegram=True)
        self.assertEqual(caught.exception.retry_after,42)
        self.assertEqual(caught.exception.service,'telegram')
        self.assertNotIn('fake-token',str(caught.exception))

    def test_blocked_bot_retains_queue_and_disables_delivery(self):
        self.state.set('chat_ready',True)
        self.state.enqueue([JOB],1)
        bot=Bot('fake-token',OWNER,'fake-key',self.state)
        with patch('telegram_bot.http_json',side_effect=ServiceError(403,service='telegram')):
            with self.assertRaises(ServiceError):
                bot.telegram('sendMessage',{})
        self.assertFalse(self.state.get('chat_ready'))
        self.assertEqual(self.state.pending_count(),1)


    def test_hourly_digest_coalesces_separate_searches(self):
        self.feed_api([])
        self.state.set('next_digest_at',2000000000)
        with patch('telegram_bot.time.time',return_value=1999999000):
            self.feed_api([JOB]); self.bot.notifications()
            self.feed_api([{**JOB,'id':'456'}]); self.bot.notifications()
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.pending_count(),2)
        with patch('telegram_bot.time.time',return_value=2000000001):
            self.bot.notifications()
        self.bot.send.assert_called_once()
        self.assertIn('2 newly found jobs',self.bot.send.call_args.args[0])
        self.assertGreater(self.state.get('next_digest_at'),2000000001)

    def test_empty_hour_waits_for_next_digest(self):
        self.feed_api([])
        with patch('telegram_bot.time.time',return_value=2000000000):
            self.bot.notifications()
            self.feed_api([JOB]); self.bot.notifications()
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.pending_count(),1)

    def test_category_navigation_and_page_picker_edit_same_message(self):
        prefs=self.state.preferences(); prefs['roles']=['embedded','software']; self.state.set_preferences(prefs)
        self.feed_api([{**JOB,'id':str(i),'title':'Embedded Engineer' if i<7 else 'Software Engineer'} for i in range(12)])
        self.bot.notifications()
        self.assertIn('Embedded Engineer',self.bot.send.call_args.args[0])
        self.bot.send.reset_mock()
        self.bot.feed_page(1,1,100,'embedded')
        payload=self.bot.telegram.call_args.args[1]
        self.assertEqual(payload['message_id'],100)
        self.assertIn('7 newly found jobs',payload['text'])
        self.assertNotIn('Software Engineer',payload['text'])
        self.bot.feed_jump(1,'embedded',1,100)
        keyboard=self.bot.telegram.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual(keyboard[0][1]['callback_data'],'feed:1:2:embedded')
        self.bot.feed_page(1,2,100,'embedded')
        self.assertIn('Page 2/2',self.bot.telegram.call_args.args[1]['text'])
        self.bot.send.assert_not_called()
        with self.assertRaises(ValueError):
            self.bot.feed_jump(1,'embedded',1,999)

    def test_large_page_picker_reaches_last_page(self):
        self.feed_api([{**JOB,'id':str(i)} for i in range(225)])
        self.bot.notifications()
        self.bot.feed_jump(1,'all',23,100)
        keyboard=self.bot.telegram.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertTrue(any(b['callback_data']=='feed:1:45:all' for row in keyboard for b in row))

    def test_old_keyboard_clamps_pages_after_filtering(self):
        self.feed_api([{**JOB,'id':str(i)} for i in range(7)])
        self.bot.notifications()
        self.bot.send.reset_mock()
        self.bot.feed_page(1,45,100)
        self.assertIn('Page 2/2',self.bot.telegram.call_args.args[1]['text'])
        self.bot.send.assert_not_called()

    def test_description_alerts_stay_in_alerts_command(self):
        def api(method,path,payload=None):
            if path.startswith('/jobs?'): return {'items':[],'next_after_id':0}
            if path.startswith('/alerts'): return [{'id':9,'alert_key':'details:123','message':'No description'}]
            if path=='/searches': return []
            return {'items':[]}
        self.feed_api([]); self.bot.api.side_effect=api
        self.bot.notifications()
        self.bot.send.assert_not_called()

    def test_other_role_scope_is_user_configurable(self):
        self.bot.configure_preferences('roles other; other_scope all')
        self.assertEqual(self.state.preferences()['other_scope'],'all')
        self.bot.configure_preferences('other_scope technical')
        self.assertEqual(self.state.preferences()['other_scope'],'technical')

    def test_notification_interval_validation_and_reschedule(self):
        with patch('telegram_bot.time.time',return_value=1000):
            self.bot.configure_interval('120')
        self.assertEqual(self.state.get('notification_interval_minutes'),120)
        self.assertEqual(self.state.get('next_digest_at'),8200)
        self.bot.api.assert_not_called()
        for value in ('0','14','1441','abc'):
            with self.assertRaises(ValueError):
                self.bot.configure_interval(value)
        self.assertEqual(self.state.get('next_digest_at'),8200)

    def test_custom_digest_interval_is_respected(self):
        self.feed_api([JOB])
        self.state.set('notification_interval_minutes',120)
        self.state.set('next_digest_at',8200)
        with patch('telegram_bot.time.time',return_value=8199):
            self.bot.notifications()
        self.bot.send.assert_not_called()
        with patch('telegram_bot.time.time',return_value=8200):
            self.bot.notifications()
        self.assertEqual(self.state.get('next_digest_at'),15400)
        self.assertTrue(self.bot.send.called)

    def test_hourly_interval_keeps_full_hour_after_non_aligned_run(self):
        self.feed_api([])
        self.state.set('notification_interval_minutes',60)
        with patch('telegram_bot.time.time',return_value=1000):
            self.bot.notifications()
        self.assertEqual(self.state.get('next_digest_at'),4600)

    def test_closed_public_job_is_removed_from_pending_digest(self):
        self.feed_api([{**JOB,'source':'greenhouse'}])
        original=self.bot.api.side_effect
        def api(method,path,payload=None):
            if path.startswith('/sources/closed-jobs'):
                return {'ids':[JOB['id']]}
            return original(method,path,payload)
        self.bot.api.side_effect=api
        self.bot.notifications()
        self.bot.send.assert_not_called()
        self.assertEqual(self.state.pending_count(),0)

    def test_legacy_snapshot_hides_irrelevant_titles(self):
        prefs=self.state.preferences(); prefs.update(roles=['software','other'],other_scope='all')
        self.state.set_preferences(prefs)
        self.state.enqueue([JOB,{**JOB,'id':'456','title':'Retail Seller'}],2)
        self.state.prepare_batch()
        self.state.con.execute('UPDATE delivery_parts SET sent_at=1'); self.state.con.commit()
        prefs['other_scope']='technical'; self.state.set_preferences(prefs)
        self.bot.feed_page(1)
        self.assertNotIn('Retail Seller',self.bot.send.call_args.args[0])


if __name__=='__main__':
    unittest.main()
