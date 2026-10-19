import unittest
from datetime import datetime,timezone,timedelta
from feed_rules import match_job,freshness_label,level_of,local_time,role_of,DEFAULT_PREFERENCES


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
