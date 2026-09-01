"""
The daily pipeline: scrape -> store -> AI-extract -> Telegram alert.

This is the single entry point Phase 6's scheduler (GitHub Actions or cron)
calls once a day. Each stage logs what it did so a failed run is diagnosable
from the workflow's log output alone.

Phase 4 (deadline alerts) is now Telegram-based (telegram_alert.py),
replacing the removed Gmail/SMTP alerting — see that module's docstring for
bot setup steps. If TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set, the alert
step logs an error and the rest of the pipeline still completes normally (a
missing alert channel shouldn't block scraping/storage/extraction).
`pdf_extract.py` is a separate, manually-run, opt-in step that backfills
eligibility data (qualification/age/domicile) from the PDF advertisements —
it isn't part of this daily pipeline since it goes beyond what robots.txt
allows for crawling and was a deliberate one-off decision, not a default.

Required environment variables (see .env.example):
  GEMINI_API_KEY
  PROFILE_QUALIFICATION, PROFILE_FIELD_OF_STUDY, PROFILE_AGE, PROFILE_DOMICILE
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional — alerts skipped if unset)
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

from ai_filter import UserProfile, extract_all_pending, filter_jobs_for_profile
from database import fetch_open_jobs, store_listings
from scraper import scrape_all
from telegram_alert import run_alerts

load_dotenv()  # no-op in GitHub Actions (no .env file there — secrets come from env directly)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")


def load_profile_from_env() -> UserProfile:
    return UserProfile(
        qualification=os.environ.get("PROFILE_QUALIFICATION", "BS Data Science"),
        field_of_study=os.environ.get("PROFILE_FIELD_OF_STUDY", "Data Science"),
        age=int(os.environ.get("PROFILE_AGE", "24")),
        domicile=os.environ.get("PROFILE_DOMICILE", "Punjab"),
    )


def run() -> None:
    log.info("Step 1/4: scraping FPSC + PPSC")
    listings = scrape_all()
    log.info("Scraped %d raw listings", len(listings))

    log.info("Step 2/4: storing + deduplicating")
    num_new, num_dup = store_listings(listings)
    log.info("Stored %d new listings, %d already seen", num_new, num_dup)

    log.info("Step 3/4: AI extraction + profile filtering")
    num_extracted = extract_all_pending()
    log.info("AI-extracted eligibility fields for %d newly-seen jobs", num_extracted)

    open_jobs = fetch_open_jobs()
    profile = load_profile_from_env()
    matches = filter_jobs_for_profile(open_jobs, profile)
    log.info("%d of %d open jobs match profile %r — check the dashboard for the full list",
             len(matches), len(open_jobs), profile)

    log.info("Step 4/4: Telegram deadline alerts")
    try:
        run_alerts(profile)
    except Exception:
        # Alerting is a nice-to-have on top of an already-completed scrape —
        # a Telegram-side failure shouldn't make the whole daily run "fail"
        # in CI when the actual data pipeline succeeded.
        log.exception("Alert step failed — scrape/store/extract already completed successfully")


if __name__ == "__main__":
    run()
