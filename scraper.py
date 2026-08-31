"""
Phase 1 — Scraping FPSC (fpsc.gov.pk) and PPSC (ppsc.gop.pk) job listings.

Respectful-scraping notes
--------------------------
- robots.txt is fetched and honored for every host before any page is requested.
  If a path is disallowed, that request is skipped (never silently ignored —
  it's logged).
- A minimum delay is enforced between requests (see MIN_DELAY_SECONDS), plus a
  small random jitter, so we don't hammer either server.
- A descriptive User-Agent with a contact string is sent — replace the email
  below with a real one before running this against production sites.
- ppsc.gop.pk's robots.txt disallows crawling *.pdf files, and PPSC currently
  publishes its actual advertisements as PDFs. This script therefore only
  extracts what is present in the HTML listing (title, date, advert link) for
  PPSC and does NOT download/parse the PDFs. Phase 3's AI extraction step
  works on whatever raw eligibility text IS available (HTML listing text, or
  a manually-pasted PDF excerpt) — it does not bypass robots.txt to get it.
- fpsc.gov.pk was unreachable while writing this (connection refused on every
  attempt), so its selectors are a best-effort structure, not a verified DOM.
  Inspect the live page in devtools and adjust the CSS selectors marked TODO
  before relying on this in production.

If a target page turns out to require JavaScript rendering (content missing
from the raw HTML response), swap the `session.get()` calls for
`fetch_with_selenium()` at the bottom of this file.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")

USER_AGENT = "PKJobAlertBot/1.0 (+mailto:youremail@example.com; personal job-alert tool)"
MIN_DELAY_SECONDS = 3.0
JITTER_SECONDS = 2.0
REQUEST_TIMEOUT = 20


@dataclasses.dataclass
class JobListing:
    source: str
    post_title: str | None
    department: str | None
    bps_scale: str | None
    qualification: str | None
    age_limit: str | None
    closing_date: str | None
    advertisement_link: str | None
    raw_eligibility_text: str | None = None  # fed to Phase 3's AI extraction
    notes: str | None = None


class PoliteSession:
    """A requests.Session that checks robots.txt and rate-limits itself per host."""

    def __init__(self, user_agent: str = USER_AGENT):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request_at: dict[str, float] = {}

    def _get_robots(self, url: str) -> urllib.robotparser.RobotFileParser:
        host = urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            try:
                resp = self.session.get(robots_url, timeout=REQUEST_TIMEOUT)
                rp.parse(resp.text.splitlines())
                log.info("Loaded robots.txt for %s", host)
            except requests.RequestException as exc:
                log.warning("Could not fetch robots.txt for %s (%s); assuming allow-all", host, exc)
                rp.parse([])
            self._robots[host] = rp
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        rp = self._get_robots(url)
        return rp.can_fetch(self.session.headers["User-Agent"], url)

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = MIN_DELAY_SECONDS - (now - last)
            if wait > 0:
                wait += random.uniform(0, JITTER_SECONDS)
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def get(self, url: str) -> requests.Response | None:
        if not self.allowed(url):
            log.warning("Blocked by robots.txt, skipping: %s", url)
            return None
        host = urlparse(url).netloc
        self._throttle(host)
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log.error("Request failed for %s: %s", url, exc)
            return None


def text_or_none(node) -> str | None:
    if node is None:
        return None
    value = node.get_text(strip=True)
    return value or None


# ---------------------------------------------------------------------------
# FPSC (fpsc.gov.pk)
# ---------------------------------------------------------------------------

# The old /fpsc/advertisement URL 404s — verified live on 2026-08-31 that FPSC
# now runs the listing at /Jobs?section=GR (linked from the homepage as
# "Jobs/ Advertisements"). It's a Next.js app, but this particular listing is
# server-rendered (present in the raw HTML), so no Selenium is needed here.
FPSC_LISTING_URL = "https://www.fpsc.gov.pk/Jobs?section=GR"

# Verified live on 2026-08-31. FPSC currently publishes one card per
# "Consolidated Advertisement No. N/YYYY" — a bundle PDF covering many posts,
# not one HTML row per post. Per-post department/BPS/qualification/age-limit/
# closing-date live only inside that PDF, same limitation as PPSC (see below).
# The class names are Tailwind utility soup and may well change on a
# redeploy — the fallback selectors are best-effort if the primary breaks.
FPSC_ROW_SELECTORS = ["div.cursor-pointer.rounded-2xl", "article", ".advertisement-list li"]


def parse_fpsc_listing(html: str, base_url: str) -> list[JobListing]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for selector in FPSC_ROW_SELECTORS:
        cards = [c for c in soup.select(selector) if c.find("h2")]
        if cards:
            log.info("FPSC: matched %d cards with selector %r", len(cards), selector)
            break
    if not cards:
        log.warning("FPSC: no advertisement cards matched any known selector; page structure "
                     "may have changed. Inspect the live HTML and update FPSC_ROW_SELECTORS.")
        return []

    listings = []
    for card in cards:
        title_tag = card.find("h2")
        post_title = text_or_none(title_tag)
        if not post_title:
            continue

        link_tag = card.find("a", href=True)
        advertisement_link = urljoin(base_url, link_tag["href"]) if link_tag else None

        listings.append(
            JobListing(
                source="FPSC",
                post_title=post_title,
                department=None,
                bps_scale=None,
                qualification=None,
                age_limit=None,
                closing_date=None,  # not published in HTML — only inside the bundle PDF
                advertisement_link=advertisement_link,
                raw_eligibility_text=None,  # nothing eligibility-related is in the HTML card
                notes="This is a consolidated advertisement covering multiple posts; "
                      "per-post department/BPS/qualification/age limit/closing date are "
                      "inside the linked PDF, not scraped from HTML.",
            )
        )
    return listings


def scrape_fpsc(session: PoliteSession) -> list[JobListing]:
    resp = session.get(FPSC_LISTING_URL)
    if resp is None:
        return []
    return parse_fpsc_listing(resp.text, FPSC_LISTING_URL)


# ---------------------------------------------------------------------------
# PPSC (ppsc.gop.pk)
# ---------------------------------------------------------------------------

PPSC_LISTING_URL = "https://ppsc.gop.pk/Jobs.aspx"

# Verified live on 2026-08-31. PPSC renders current jobs in a single ASP.NET
# table with a stable id, columns (0-indexed by <td> position): SR NO, AD NO,
# CASE NO, POST NAME (JS postback link, no real href), FEE, DEPARTMENT,
# AD DATE, CLOSING DATE (both dates as DD-MM-YYYY). It's a plain rendered
# <table>, not a JS-populated grid, so requests+BeautifulSoup sees it fine.
PPSC_TABLE_SELECTORS = ["table#ctl00_ContentPlaceHolder1_tbl_Jobs tbody tr",
                         "table[id*='tbl_Jobs'] tbody tr", "table[id*='GridView'] tr"]
# The "Current Advertisements" marquee above the table maps each AD NO (e.g.
# "08/2026") to the single bundle PDF covering every post under that ad —
# individual rows don't carry their own PDF link (postbacks aren't real URLs).
PPSC_AD_MARQUEE_SELECTOR = "#tab_SlidingAds a[href]"


def _parse_ppsc_date(text: str | None) -> str | None:
    """PPSC dates are DD-MM-YYYY; normalize to ISO (YYYY-MM-DD) so DB sort/
    compare and alerts.py's date arithmetic work correctly."""
    if not text:
        return None
    match = re.match(r"(\d{2})-(\d{2})-(\d{4})", text.strip())
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def parse_ppsc_listing(html: str, base_url: str) -> list[JobListing]:
    soup = BeautifulSoup(html, "html.parser")

    ad_no_to_pdf = {}
    for a in soup.select(PPSC_AD_MARQUEE_SELECTOR):
        ad_no = text_or_none(a)
        if ad_no:
            ad_no_to_pdf[ad_no] = urljoin(base_url, a["href"])

    rows = []
    for selector in PPSC_TABLE_SELECTORS:
        rows = soup.select(selector)
        if rows:
            log.info("PPSC: matched %d rows with selector %r", len(rows), selector)
            break
    if not rows:
        log.warning("PPSC: no listing rows matched any known selector; page structure "
                     "may have changed. Inspect the live HTML and update PPSC_TABLE_SELECTORS.")
        return []

    listings = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 8 or row.find("th"):
            continue

        ad_no = text_or_none(cells[1])
        post_title = text_or_none(cells[3])
        department = text_or_none(cells[5])
        closing_date = _parse_ppsc_date(text_or_none(cells[7]))

        if not post_title:
            continue

        advertisement_link = ad_no_to_pdf.get(ad_no)
        is_pdf = bool(advertisement_link and advertisement_link.lower().endswith(".pdf"))

        listings.append(
            JobListing(
                source="PPSC",
                post_title=post_title,
                department=department,
                bps_scale=None,
                qualification=None,
                age_limit=None,
                closing_date=closing_date,
                advertisement_link=advertisement_link,
                raw_eligibility_text=None,  # qualification/age/domicile aren't in this table
                notes="details in PDF advertisement, not scraped (robots.txt disallows /*.pdf)"
                if is_pdf else None,
            )
        )
    return listings


def scrape_ppsc(session: PoliteSession) -> list[JobListing]:
    resp = session.get(PPSC_LISTING_URL)
    if resp is None:
        return []
    return parse_ppsc_listing(resp.text, PPSC_LISTING_URL)


def scrape_all() -> list[JobListing]:
    session = PoliteSession()
    listings = []
    listings.extend(scrape_fpsc(session))
    listings.extend(scrape_ppsc(session))
    return listings


# ---------------------------------------------------------------------------
# Optional Selenium fallback (use if a site turns out to need JS rendering)
# ---------------------------------------------------------------------------

def fetch_with_selenium(url: str, wait_selector: str | None = None) -> str:
    """Fetch a JS-rendered page's HTML. Requires: pip install selenium webdriver-manager

    Swap `session.get(url).text` for this in scrape_fpsc / scrape_ppsc if
    requests+BeautifulSoup returns an empty shell (a common sign the listing
    is populated client-side).
    """
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument(f"user-agent={USER_AGENT}")
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(url)
        if wait_selector:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
            )
        else:
            time.sleep(3)
        return driver.page_source
    finally:
        driver.quit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for job in scrape_all():
        log.info("%s | %s | closes %s | %s", job.source, job.post_title,
                  job.closing_date, job.advertisement_link)
