"""
Phase 4 (rebuilt) — deadline alerts via Telegram, replacing the removed
Gmail/SMTP alerting.

Sends one Telegram message per run listing every job that:
  (a) matches the profile per ai_filter.py's deterministic match_job(), AND
  (b) has a closing_date within the next ALERT_WINDOW_DAYS days, AND
  (c) hasn't already been alerted about (tracked via the `alerted` column —
      see database.py's mark_alerted()/schema migration).

Rows are marked alerted only after a successful send, so a failed send is
retried on the next run rather than silently marking jobs as notified.

Configuration (environment variables):
  TELEGRAM_BOT_TOKEN            — from @BotFather (see setup steps below)
  TELEGRAM_CHAT_ID               — your chat ID (see setup steps below)
  TELEGRAM_SEND_EMPTY_DIGEST     — "true" to send a brief "no urgent
                                    deadlines today" message when there's
                                    nothing to alert on; default "false"
                                    (skip silently — most days will have
                                    nothing new, and a message every single
                                    day regardless is exactly the kind of
                                    noise that gets a bot muted)

Setup — creating a Telegram bot and getting a chat ID (3 steps):
  1. Open Telegram, search for **@BotFather**, send `/newbot`, follow the
     prompts (choose a name and a username ending in "bot"). BotFather
     replies with your bot token — looks like `123456789:AAF...`. That's
     TELEGRAM_BOT_TOKEN.
  2. Send your new bot any message (e.g. "hi") — bots can't message you
     first, so you have to message it once to open the conversation.
  3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
     browser (substitute your real token). Find `"chat":{"id":...}` in the
     JSON response — that number is TELEGRAM_CHAT_ID.

Run manually:
    python telegram_alert.py
"""

from __future__ import annotations

import datetime
import logging
import os

import requests

from ai_filter import UserProfile, match_job
from database import fetch_open_jobs, mark_alerted

log = logging.getLogger("telegram_alert")

ALERT_WINDOW_DAYS = 5
URGENT_WINDOW_DAYS = 2
TELEGRAM_API_BASE = "https://api.telegram.org"


def _days_until(closing_date: str | None) -> int | None:
    if not closing_date:
        return None
    try:
        closing = datetime.date.fromisoformat(closing_date)
    except ValueError:
        log.warning("Unparseable closing_date %r, skipping from alert window", closing_date)
        return None
    return (closing - datetime.date.today()).days


def find_jobs_to_alert(profile: UserProfile, db_path: str = "jobs.db"):
    """Matching, closing-soon, not-yet-alerted jobs. Returns (job, days_left)
    tuples sorted soonest-first."""
    to_alert = []
    for job in fetch_open_jobs(db_path):
        if job["alerted"]:
            continue
        is_match, _reasons = match_job(job, profile)
        if not is_match:
            continue
        days_left = _days_until(job["closing_date"])
        if days_left is None or not (0 <= days_left <= ALERT_WINDOW_DAYS):
            continue
        to_alert.append((job, days_left))
    to_alert.sort(key=lambda item: item[1])
    return to_alert


def format_message(jobs_with_deadlines) -> str:
    lines = [f"*{len(jobs_with_deadlines)} job(s) closing soon:*", ""]
    for job, days_left in jobs_with_deadlines:
        flag = "🚨 *URGENT* — " if days_left <= URGENT_WINDOW_DAYS else ""
        closes_in = "today" if days_left == 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
        # Telegram Markdown: escape characters that would otherwise break formatting
        title = job["post_title"].replace("_", "\\_").replace("*", "\\*")
        lines.append(
            f"{flag}*{title}*\n"
            f"Department: {job['department'] or 'N/A'}\n"
            f"Closes: {job['closing_date']} ({closes_in})\n"
            f"Apply: {job['advertisement_link'] or 'N/A'}\n"
        )
    return "\n".join(lines)


def send_telegram_message(text: str, bot_token: str | None = None, chat_id: str | None = None) -> bool:
    """Send one Telegram message. Returns True on success; logs and returns
    False on any failure rather than raising, so a Telegram outage doesn't
    take down the rest of the pipeline."""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set — cannot send alert. "
                  "See telegram_alert.py's docstring for setup steps.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        resp.raise_for_status()
        if not resp.json().get("ok"):
            log.error("Telegram API returned ok=false: %s", resp.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def run_alerts(profile: UserProfile, db_path: str = "jobs.db") -> None:
    jobs_to_alert = find_jobs_to_alert(profile, db_path)

    if not jobs_to_alert:
        log.info("No new matching jobs closing within the alert window.")
        if os.environ.get("TELEGRAM_SEND_EMPTY_DIGEST", "false").lower() == "true":
            send_telegram_message("No urgent deadlines today.")
        return

    message = format_message(jobs_to_alert)
    sent = send_telegram_message(message)
    if sent:
        mark_alerted([job["id"] for job, _days_left in jobs_to_alert], db_path)
        log.info("Sent Telegram alert for %d job(s), marked as alerted", len(jobs_to_alert))
    else:
        log.error("Alert send failed — jobs NOT marked as alerted, will retry next run")


if __name__ == "__main__":
    import logging as _logging

    from dotenv import load_dotenv

    load_dotenv()
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    demo_profile = UserProfile(
        qualification=os.environ.get("PROFILE_QUALIFICATION", "BS Data Science"),
        field_of_study=os.environ.get("PROFILE_FIELD_OF_STUDY", "Data Science"),
        age=int(os.environ.get("PROFILE_AGE", "24")),
        domicile=os.environ.get("PROFILE_DOMICILE", "Punjab"),
    )
    run_alerts(demo_profile)
