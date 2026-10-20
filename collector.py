import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
try:
    from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
except ImportError:
    # The production read API and public-feed worker do not include a browser.
    async_playwright = None
    BrowserTimeout = TimeoutError
import storage


class CollectionError(Exception):
    pass


EXTRACT = r'''() => {
  const cards = [...document.querySelectorAll('li[data-occludable-job-id], .job-card-container[data-job-id], .base-card[data-entity-urn]')];
  const seen = new Set();
  return cards.map(card => {
    const a = card.querySelector('a[href*="/jobs/view/"]');
    const raw = card.getAttribute('data-occludable-job-id') || card.getAttribute('data-job-id') || (a?.href.match(/\/jobs\/view\/(?:[^/?#]*-)?(\d+)/)||[])[1] || (card.getAttribute('data-entity-urn')||'').split(':').pop();
    if (!/^\d+$/.test(raw || '') || seen.has(raw)) return null;
    seen.add(raw);
    const pick = s => card.querySelector(s)?.innerText?.trim() || null;
    const title = pick('.job-card-list__title--link strong, .job-card-list__title strong, .base-search-card__title') || a?.innerText?.trim();
    if (!title) return null;
    const posted = pick('time') || [...card.querySelectorAll('.job-card-container__footer-item')]
      .map(el => el.innerText.trim()).find(text => /\b(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b|\b(?:just now|today|yesterday)\b|לפני|פורסם מחדש|היום|אתמול|זה עתה/i.test(text)) || null;
    return {id:raw, title, company:pick('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .base-search-card__subtitle'),
      location:pick('.job-card-container__metadata-item, .artdeco-entity-lockup__caption, .job-search-card__location'),
      url:'https://www.linkedin.com/jobs/view/'+raw+'/',
      posted_text:posted && /\breposted\b|פורסם מחדש/i.test(card.innerText) && !/\breposted\b|פורסם מחדש/i.test(posted)
        ? (/פורסם מחדש/.test(card.innerText) ? 'פורסם מחדש ' : 'Reposted ')+posted : posted,
      posting_observed_at:new Date().toISOString(),
      posted_at:card.querySelector('time[datetime]')?.getAttribute('datetime') || null};
  }).filter(Boolean);
}'''


CARDS = 'li[data-occludable-job-id], .job-card-container[data-job-id], .base-card[data-entity-urn]'
EMPTY = '.jobs-search-no-results-banner'
DETAIL_SELECTOR = '.jobs-description__content, .jobs-box__html-content, .show-more-less-html__markup, main [data-testid="expandable-text-box"]'


class CollectionResult(list):
    def __init__(self, jobs=(), *, pages=0, truncated=False, error=None):
        super().__init__(jobs)
        self.pages, self.truncated, self.error = pages, truncated, error


@asynccontextmanager
async def browser_session():
    session = storage.DATA / 'session.json'
    if not session.exists():
        raise CollectionError('session_required')
    try:
        state = json.loads(session.read_text())
        if not any(c['name'] == 'li_at' and c.get('value') for c in state.get('cookies', [])):
            raise CollectionError('session_required')
    except (ValueError, KeyError):
        raise CollectionError('invalid_session') from None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=state, locale='en-US')
            yield context
        finally:
            await browser.close()


def check_access(page, response=None):
    if any(s in urlsplit(page.url).path for s in ('/login', '/checkpoint', '/authwall', '/uas/')):
        raise CollectionError('reconnect_required')
    if response:
        if response.status == 429:
            raise CollectionError('rate_limited')
        if response.status in (401, 403):
            raise CollectionError('access_denied')


async def navigate(page, url):
    # Retry one transient failure, never a login challenge or an access denial.
    for attempt in range(2):
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
            check_access(page, response)
            if response and response.status >= 500:
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                raise CollectionError('linkedin_server_error')
            if response and response.status >= 400:
                raise CollectionError('job_unavailable' if response.status in (404, 410) else 'linkedin_http_error')
            return
        except BrowserTimeout:
            if attempt:
                raise CollectionError('navigation_timeout') from None
            await asyncio.sleep(3)


SCROLL = '''() => {
  let el = document.querySelector('li[data-occludable-job-id], .job-card-container[data-job-id], .base-card[data-entity-urn]');
  while (el && el !== document.body) {
    if (el.scrollHeight > el.clientHeight + 50 && ['auto','scroll'].includes(getComputedStyle(el).overflowY)) {
      el.scrollTop += Math.max(450, el.clientHeight * 0.8); return;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 600);
}'''


async def read_cards(page):
    try:
        await page.wait_for_selector(CARDS + ', ' + EMPTY, timeout=20000)
    except BrowserTimeout:
        check_access(page)
        raise CollectionError('page_not_recognized_or_verification_required') from None
    found, stable = {}, 0
    for _ in range(14):
        before = len(found)
        found.update({job['id']: job for job in await page.evaluate(EXTRACT)})
        stable = stable + 1 if len(found) == before else 0
        if stable >= 3 or len(found) >= 25:
            break
        await page.evaluate(SCROLL)
        await asyncio.sleep(0.8)
    if not found and not await page.locator(EMPTY).count():
        raise CollectionError('job_cards_not_recognized')
    return list(found.values())[:25]


def lookback(search):
    hours = search.get('lookback_hours', 48)
    if search.get('last_success'):
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(search['last_success'])).total_seconds() / 3600
        # Normal hourly runs need a short overlap, not the entire 48-hour baseline.
        hours = max(2, math.ceil(gap)+2)
    return min(168, hours)


PAGE_END = r'''() => {
  const state = document.querySelector('.jobs-search-pagination__page-state')?.textContent?.match(/\bPage\s+(\d+)\s+of\s+(\d+)/i);
  if (state && +state[1]>0 && +state[2]>0 && +state[1]<=+state[2]) return +state[1]===+state[2];
  const next = document.querySelector('button.artdeco-pagination__button--next, button.jobs-search-pagination__button--next, button[aria-label="View next page"], button[aria-label="Next page"], button[aria-label="Next"]');
  if (next) return next.disabled || next.getAttribute('aria-disabled') === 'true';
  return null;
}'''


async def collection_page(page, url):
    await navigate(page,url)
    jobs=await read_cards(page)
    return jobs, await page.evaluate(PAGE_END)


async def collect(search):
    params = {'keywords':search['keywords'], 'location':search['location'],
              'sortBy':'DD', 'f_TPR':'r' + str(lookback(search) * 3600)}
    if search['remote']:
        params['f_WT'] = '2'
    found, pages, truncated, error = {}, 0, False, None
    started=time.monotonic()
    async with browser_session() as context:
        page = await context.new_page()
        max_pages = search.get('max_pages', 3)
        for index in range(max_pages):
            params['start'] = index * 25
            try:
                remaining=240-(time.monotonic()-started)
                if remaining<=0:
                    raise CollectionError('collection_time_budget')
                batch, confirmed_end = await asyncio.wait_for(collection_page(page,'https://www.linkedin.com/jobs/search/?'+urlencode(params)),timeout=min(60,remaining))
            except asyncio.TimeoutError:
                if not found:
                    raise CollectionError('navigation_timeout') from None
                truncated,error=True,'collection_time_budget'
                break
            except CollectionError as exc:
                if not found:
                    raise
                truncated, error = True, str(exc)
                break
            pages += 1
            before = len(found)
            found.update({job['id']:job for job in batch})
            if not batch:
                break
            if confirmed_end is True:
                break
            if len(found) == before:
                truncated, error = True, 'pagination_not_advancing'
                break
            # A short page can mean lazy loading changed, so report uncertain coverage.
            if len(batch) < 25 and confirmed_end is not False:
                truncated, error = True, 'short_page_coverage_uncertain'
                break
            if index == max_pages - 1:
                truncated = True
            else:
                await asyncio.sleep(2)
    return CollectionResult(found.values(), pages=pages, truncated=truncated, error=error)


DETAIL_EXTRACT = r'''() => {
  const pick = s => document.querySelector(s)?.innerText?.trim() || null;
  let description = pick('.jobs-description__content .jobs-box__html-content, .jobs-box__html-content, .show-more-less-html__markup') || pick('.jobs-description__content');
  if (!description) {
    const heading = [...document.querySelectorAll('main h2')].find(el => el.textContent.trim() === 'About the job');
    let section = heading?.parentElement;
    for (let level=0; section && level<3; level++, section=section.parentElement) {
      const box = section.querySelector('[data-testid="expandable-text-box"]');
      if (box) { description = box.innerText.replace(/\n*…\s*more\s*$/i,'').trim(); break; }
    }
  }
  const criteria = [...document.querySelectorAll('.description__job-criteria-item')];
  const criterion = label => criteria.find(el => el.querySelector('h3')?.innerText?.trim().toLowerCase() === label)?.querySelector('.description__job-criteria-text')?.innerText?.trim() || null;
  const header = (document.querySelector('main')?.innerText || '').split('About the job')[0];
  const insights = [...document.querySelectorAll('.job-details-jobs-unified-top-card__job-insight, .job-details-fit-level-preferences .artdeco-button__text')].map(el => el.innerText.trim()).concat(header.split('\n').map(t=>t.trim()).filter(Boolean));
  const arrangement = insights.find(t => /^(remote|hybrid|on-site)$/i.test(t)) || null;
  const employment = criterion('employment type') || insights.find(t => /^(full-time|part-time|contract|temporary|internship|volunteer)$/i.test(t)) || null;
  const apply = document.querySelector('a.jobs-apply-button[href], a.topcard__link--apply[href]') || [...document.querySelectorAll('main a[href]')].find(el=>/^apply$/i.test(el.innerText.trim()));
  let application_url = apply?.href || null;
  if (application_url && !/^https?:\/\//i.test(application_url)) application_url = null;
  const salary = pick('.job-details-jobs-unified-top-card__salary-details, .compensation__salary, .salary-main-rail__data-body') || insights.find(t=>/[$₪€£]/.test(t) && /\/(yr|mo|hr|year|month|hour)\b/i.test(t)) || null;
  let posted_at = null;
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const raw = JSON.parse(script.textContent);
      const entries = Array.isArray(raw) ? raw : raw['@graph'] || [raw];
      const job = entries.find(x => x['@type'] === 'JobPosting');
      if (job?.datePosted) posted_at = job.datePosted;
    } catch {}
  }
  return {description, salary_text:salary, seniority:criterion('seniority level'), employment_type:employment,
    work_arrangement:arrangement, application_url, posted_at};
}'''


async def fetch_details(job_ids):
    results = {}
    async with browser_session() as context:
        page = await context.new_page()
        for index, job_id in enumerate(job_ids):
            if not str(job_id).isdigit():
                raise CollectionError('invalid_job_id')
            if index:
                await asyncio.sleep(2)
            try:
                await navigate(page, f'https://www.linkedin.com/jobs/view/{job_id}/')
                try:
                    await page.wait_for_selector(DETAIL_SELECTOR, timeout=15000)
                except BrowserTimeout:
                    check_access(page)
                    raise CollectionError('description_not_available') from None
                details = await page.evaluate(DETAIL_EXTRACT)
                if not details['description']:
                    raise CollectionError('description_not_available')
                results[job_id] = details
            except CollectionError as exc:
                results[job_id] = {'error':str(exc)}
                if str(exc) in ('reconnect_required', 'access_denied', 'rate_limited'):
                    break
    return results
