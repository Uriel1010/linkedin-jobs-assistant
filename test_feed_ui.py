import unittest
from html.parser import HTMLParser
from feed_ui import card,paginate,compact_location
from feed_rules import DEFAULT_PREFERENCES,match_job


class FeedUITests(unittest.TestCase):
    def test_source_fields_are_escaped_and_safe_links_preserved(self):
        job={'id':'1','title':'Software <Engineer> & C++','company':'A & B',
             'url':'https://www.linkedin.com/jobs/view/1/','location':'Tel Aviv, Tel Aviv District, Israel (Hybrid)'}
        text=card(job,1,DEFAULT_PREFERENCES,'en')
        self.assertIn('Software &lt;Engineer&gt; &amp; C++',text)
        self.assertIn('<a href="https://www.linkedin.com/jobs/view/1/">',text)
        self.assertIn('Tel Aviv · Hybrid',text)
        self.assertNotIn('District',text)
        job['url']='javascript:alert(1)'
        self.assertNotIn('<a ',card(job,1,DEFAULT_PREFERENCES,'en'))

    def test_emoji_and_escaped_fields_fit_page_budget(self):
        job={'id':'1','title':'Software '+'<&😀'*50,'company':'<&😀'*30,'location':'Israel'}
        pages=paginate([job]*30,DEFAULT_PREFERENCES,'he')
        self.assertEqual(sum(map(len,pages)),30)
        for page in pages:
            self.assertLess(len('\n\n'.join(page).encode('utf-16-le'))//2,2700)
            HTMLParser().feed('\n\n'.join(page))

    def test_other_means_related_engineering_by_default(self):
        prefs={**DEFAULT_PREFERENCES,'roles':['embedded','software','qa','other']}
        for title in ('Attorney','Retail Seller','Sales Manager','Warehouse Worker','Mechanical Engineer','Ambassador','VP Engineering Software','Chief AI Officer'):
            with self.subTest(title=title):
                self.assertFalse(match_job({'title':title,'location':'Israel'},prefs)[0])
        for title in ('Electronics Engineer','System Engineer','Firmware Engineer','Integration Engineer'):
            self.assertTrue(match_job({'title':title,'location':'Israel'},prefs)[0],title)
        prefs['other_scope']='all'
        self.assertTrue(match_job({'title':'Retail Seller','location':'Israel'},prefs)[0])
        prefs.update(roles=['all'],other_scope='technical')
        self.assertTrue(match_job({'title':'Retail Seller','location':'Israel'},prefs)[0])

    def test_staff_metadata_is_not_relabelled_principal(self):
        text=card({'title':'Software Engineer','seniority':'Staff','location':'Israel'},1,DEFAULT_PREFERENCES,'en')
        self.assertIn('Staff',text)
        self.assertNotIn('Principal',text)
