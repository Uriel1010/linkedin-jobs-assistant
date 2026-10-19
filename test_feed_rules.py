import unittest
from datetime import datetime,timezone,timedelta
from feed_rules import match_job,freshness_label,level_of,local_time,role_of,DEFAULT_PREFERENCES,job_card,age_hours


class FeedRulesTests(unittest.TestCase):
    def job(self,title='Software Engineer',location='Israel',**fields):
        return {'title':title,'location':location,**fields}

    def test_unrelated_roles_are_excluded(self):
        for title in ['DevOps Engineer','Cloud Support Engineer','AI Researcher','Senior Data Engineer',
                      'Silicon Validation Engineer','Associate Director Lead AI Engineer','Offensive Security Engineer']:
            self.assertFalse(match_job(self.job(title),{})[0],title)

    def test_software_families_are_included(self):
        for title in ['Senior Frontend Engineer','Backend Engineer - Extensions','Fullstack Engineer','Principal C++ Software Engineer']:
            self.assertTrue(match_job(self.job(title),{})[0],title)

    def test_embedded_search_titles_are_classified_and_match_selected_roles(self):
        titles=['Embedded Software Engineer','Firmware Engineer','Embedded Linux Engineer',
                'Low Level Software Engineer','Linux Kernel Engineer','Device Driver Engineer','BSP Engineer']
        for title in titles:
            self.assertEqual(role_of(title),'embedded',title)
            self.assertTrue(match_job(self.job(title),{'roles':['embedded','software','qa','other']})[0],title)
        self.assertEqual(role_of('System Software Engineer'),'software')

    def test_remote_eligibility_is_not_invented(self):
        self.assertFalse(match_job(self.job(location='EMEA (Remote)'),{})[0])
        self.assertTrue(match_job(self.job(location='Israel (Remote)'),{})[0])
        self.assertTrue(match_job(self.job(location='EMEA (Remote)'),{'region':'broad'})[0])

    def test_explicit_seniority_filter(self):
        self.assertFalse(match_job(self.job('Principal Software Engineer'),{'levels':['junior']})[0])
        self.assertTrue(match_job(self.job('Junior Software Engineer'),{'levels':['junior']})[0])
        self.assertTrue(match_job(self.job(),{'levels':['junior']})[0])
        self.assertFalse(match_job(self.job(),{'levels':['junior'],'keep_unknown_level':False})[0])

    def test_include_exclude_use_whole_phrases(self):
        self.assertFalse(match_job(self.job('Python Software Engineer'),{'exclude':['python']})[0])
        self.assertTrue(match_job(self.job('Email Software Engineer'),{'exclude':['ai']})[0])
        self.assertFalse(match_job(self.job('Java Software Engineer'),{'include':['python']})[0])

    def test_known_old_post_is_excluded(self):
        old=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
        self.assertFalse(match_job(self.job(posted_at=old),{})[0])
        self.assertFalse(match_job(self.job(posted_at='2020-01-01'),{})[0])

    def test_relative_age_accounts_for_observation_time(self):
        observed=(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        self.assertFalse(match_job(self.job(posted_text='2 minutes ago',posting_observed_at=observed),{})[0])

    def test_unknown_date_is_kept_and_labeled(self):
        self.assertTrue(match_job(self.job(),{})[0])
        self.assertIn('not provided',freshness_label(self.job()))
        self.assertIn('observation time unknown',freshness_label(self.job(posted_text='2 minutes ago')))

    def test_reposts_are_not_presented_as_new_publication(self):
        self.assertIn('original posting date unknown',freshness_label(self.job(posted_text='Reposted 2 minutes ago')))

    def test_status_converts_legacy_utc_to_israel(self):
        self.assertIn('17:37',local_time('2026-10-19 14:37:26 UTC'))
        self.assertIn('17:37',local_time('2026-10-19T14:37:26+00:00'))

    def test_hebrew_roles_levels_and_location(self):
        for title in ('מהנדס קושחה בכיר','מהנדסת תוכנה משובצת','מפתח ליבת לינוקס','מהנדס דרייברים'):
            self.assertEqual(role_of(title),'embedded',title)
            self.assertTrue(match_job(self.job(title,location='תל אביב, ישראל'),{'roles':['embedded']})[0],title)
        self.assertEqual(role_of('בודקת תוכנה'),'qa')
        self.assertEqual(role_of('מפתח צד שרת'),'backend')
        self.assertEqual(level_of(self.job('מהנדסת תוכנה בכירה')),'senior')
        self.assertEqual(level_of(self.job('ראש צוות תוכנה')),'lead')
        self.assertEqual(level_of(self.job('מפתח תוכנה זוטר')),'junior')

    def test_hebrew_posting_age_and_repost_information(self):
        now=datetime.now(timezone.utc)
        for label,expected in [('לפני 3 שעות',3),('לפני שעה',1),('לפני יומיים',48),('לפני 5 דקות',5/60)]:
            job=self.job(posted_text=label,posting_observed_at=now.isoformat())
            self.assertAlmostEqual(age_hours(job,now),expected)
        old=self.job(posted_text='לפני 4 ימים',posting_observed_at=now.isoformat())
        self.assertFalse(match_job(old,{'max_age_hours':72})[0])
        repost=self.job(posted_text='פורסם מחדש לפני 3 שעות',posting_observed_at=now.isoformat())
        self.assertIsNone(age_hours(repost,now))
        self.assertIn('מועד הפרסום המקורי לא ידוע',freshness_label(repost,'he'))

    def test_known_keyword_aliases_work_across_languages(self):
        self.assertFalse(match_job(self.job('מפתח צד לקוח'),{'exclude':['frontend']})[0])
        self.assertFalse(match_job(self.job('Frontend Engineer'),{'exclude':['פרונטאנד']})[0])
        self.assertFalse(match_job(self.job('מפתח פול סטאק'),{'exclude':['full stack']})[0])
        self.assertTrue(match_job(self.job('מפתח פייתון'),{'include':['python']})[0])
        self.assertFalse(match_job(self.job('מפתח תוכנה'),{'include':['מוצרמיוחד']})[0])

    def test_hebrew_cards_keep_original_source_and_url(self):
        job=self.job('מהנדס/ת תוכנה {C++}',company='Company {name}',id='123',
            url='https://www.linkedin.com/jobs/view/123/')
        text=job_card(job,'he')
        self.assertIn(job['title'],text)
        self.assertIn(job['company'],text)
        self.assertIn(job['url'],text)
        self.assertIn('תוכנה',text)
        self.assertIn('לא סיפקה תאריך פרסום',text)
        self.assertIn('\u2068',text)
