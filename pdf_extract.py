"""
Explicit, opt-in step: fetch the PDF advertisements FPSC and PPSC bundle
per-post eligibility (qualification/age/domicile) into, and extract that
data back onto the matching job rows in jobs.db.

Deliberately NOT part of the daily scraper (scraper.py) or pipeline.py.
Both sites' robots.txt disallow crawling *.pdf files — scraper.py honors
that. This script is a separate, manually-run, explicitly-authorized step
(the user asked for this data specifically): it fetches a small, fixed
number of public advertisement PDFs for personal use, not bulk/automated
crawling, extracts structured eligibility fields via Gemini, and writes
them onto the existing DB rows that scraper.py already created from HTML.

PPSC's PDF has a real embedded text layer — extracted directly and parsed
by one Gemini call across the whole document (small enough to fit).
FPSC's PDF is scanned page images with no text layer, so it's parsed via
Gemini's vision input instead of OCR, one page (or batch of pages) at a time.
Since FPSC's HTML only exposed one bundle-level row (no per-post rows to
enrich), FPSC's extracted posts are inserted as new individual job listings
sharing the bundle's advertisement link — normal dedup applies on repeat runs.

Run manually:
    python pdf_extract.py
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import time
import urllib.parse

import pdfplumber
import requests
from google.genai import errors as genai_errors
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

from database import get_connection, save_extracted_fields, upsert_listing
from qualification_schema import QUALIFICATION_PROMPT_SNIPPET
from scraper import JobListing

load_dotenv()


def _open_connection(db_path: str) -> sqlite3.Connection:
    """A plain (non-context-manager) connection, so callers can commit
    incrementally across a long-running loop instead of one all-or-nothing
    transaction — see run_fpsc for why that matters here."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pdf_extract")

MODEL = "gemini-3.6-flash"

PPSC_PDF_URL = "https://ppsc.gop.pk/Adds/Advt No-08-2026 18-08-2025  X7 Version.pdf"
FPSC_PDF_URL = ("https://www.fpsc.gov.pk/uploads/content/"
                "1786536972642_consolidated_Advertisement_No._3-2026.pdf")

DOWNLOAD_USER_AGENT = ("PKJobAlertBot/1.0 (+mailto:youremail@example.com; "
                        "personal job-alert tool, explicit opt-in PDF fetch)")


def download_pdf(url: str) -> bytes:
    safe_url = urllib.parse.quote(url, safe=":/")
    resp = requests.get(safe_url, headers={"User-Agent": DOWNLOAD_USER_AGENT}, timeout=180)
    resp.raise_for_status()
    return resp.content


class DailyQuotaExhausted(Exception):
    """Free-tier daily request quota is exhausted — retrying within the same
    day is futile (unlike a per-minute 429, this doesn't clear in seconds)."""


def _with_retry(fn, max_attempts: int = 5, base_delay: float = 5.0):
    """Retry on transient Gemini server errors (503 overload, per-minute 429
    rate limit) with exponential backoff. A daily-quota 429 is NOT retried —
    it won't clear within this run, so it's raised immediately as
    DailyQuotaExhausted for the caller to stop the whole batch, not just skip
    one page."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except genai_errors.ServerError as exc:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Gemini server error (%s), retrying in %.0fs (attempt %d/%d)",
                        exc, delay, attempt + 1, max_attempts)
            time.sleep(delay)
        except genai_errors.ClientError as exc:
            if getattr(exc, "code", None) != 429:
                raise
            if "PerDay" in str(exc):
                raise DailyQuotaExhausted(str(exc)) from exc
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Gemini rate limited, retrying in %.0fs (attempt %d/%d)",
                        delay, attempt + 1, max_attempts)
            time.sleep(delay)


# ---------------------------------------------------------------------------
# PPSC — real text layer, one Gemini call across the whole document
# ---------------------------------------------------------------------------

PPSC_PDF_PROMPT = f"""\
This is text extracted from a Punjab Public Service Commission (PPSC) job \
advertisement PDF. It covers several government departments, each with one \
or more posts. Table headers appear garbled (doubled letters like \
"SSRR.. CCAASSEE") — ignore those, they're a text-extraction artifact, not \
real content. Data rows are still readable despite column text sometimes \
interleaving.

Extract every individual post entry in the document. For each one, output:
- "department": the department section heading this post falls under \
  (copy it exactly as it appears, e.g. "BOARD OF INTERMEDIATE & SECONDARY \
  EDUCATION, LAHORE").
- "post_name": the post title (e.g. "Assistant", "Junior Clerk").
{QUALIFICATION_PROMPT_SNIPPET}
- "field_of_study": the required field(s), or "any discipline" if none \
  specified, or null.
- "age_range": as "MIN-MAX" using the general (non-relaxed, non-quota) age \
  range stated for the post, or null.
- "domicile_requirement": the required domicile (e.g. "Faisalabad District \
  Only", "Punjab"), or null.

Respond with ONLY a JSON array of these objects, no markdown fences, no \
commentary.
"""


def extract_ppsc_entries(pdf_bytes: bytes, client: genai.Client) -> list[dict]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n\n".join(page.extract_text() or "" for page in pdf.pages)

    response = _with_retry(lambda: client.models.generate_content(
        model=MODEL,
        contents=full_text,
        config=genai_types.GenerateContentConfig(
            system_instruction=PPSC_PDF_PROMPT,
            response_mime_type="application/json",
        ),
    ))
    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, ValueError):
        log.warning("Could not parse PPSC PDF extraction as JSON: %r", response.text[:300])
        return []


def _normalize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _best_match_row(rows: list, department: str, post_name: str):
    """Find the DB row whose department has the most token overlap (handles
    PDF line-break differences vs. the single-line HTML department string)
    and whose post_title has the most word overlap with the PDF's post_name."""
    dept_tokens = _normalize(department)
    dept_scores = [
        (r, len(dept_tokens & _normalize(r["department"] or "")) / max(len(dept_tokens), 1))
        for r in rows
    ]
    best_dept_score = max((s for _, s in dept_scores), default=0)
    candidates = [r for r, s in dept_scores if s >= 0.8 and s == best_dept_score] if best_dept_score >= 0.8 else []
    if not candidates:
        return None
    post_tokens = _normalize(post_name)
    best, best_score = None, 0
    for row in candidates:
        score = len(post_tokens & _normalize(row["post_title"] or ""))
        if score > best_score:
            best, best_score = row, score
    return best if best_score > 0 else (candidates[0] if len(candidates) == 1 else None)


def run_ppsc(db_path: str = "jobs.db") -> None:
    log.info("Downloading PPSC advertisement PDF...")
    pdf_bytes = download_pdf(PPSC_PDF_URL)
    log.info("Downloaded %d bytes", len(pdf_bytes))

    client = genai.Client()
    entries = extract_ppsc_entries(pdf_bytes, client)
    log.info("Gemini extracted %d entries from the PPSC PDF", len(entries))

    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE source = 'PPSC'").fetchall()

    matched, unmatched = 0, 0
    for entry in entries:
        row = _best_match_row(rows, entry.get("department", ""), entry.get("post_name", ""))
        if row is None:
            unmatched += 1
            log.warning("No DB match for PDF entry: %s / %s",
                        entry.get("department"), entry.get("post_name"))
            continue
        save_extracted_fields(
            row["id"],
            {
                "min_qualification": entry.get("min_qualification"),
                "field_of_study": entry.get("field_of_study"),
                "age_range": entry.get("age_range"),
                "domicile_requirement": entry.get("domicile_requirement"),
                "qualification_raw_text": entry.get("qualification_raw_text"),
            },
            db_path=db_path,
        )
        matched += 1
    log.info("PPSC: wrote eligibility data for %d jobs, %d PDF entries unmatched", matched, unmatched)


# ---------------------------------------------------------------------------
# FPSC — scanned pages, Gemini vision (no text layer to extract)
# ---------------------------------------------------------------------------

FPSC_PDF_VISION_PROMPT = f"""\
This is a page image from an FPSC (Federal Public Service Commission, \
Pakistan) consolidated job advertisement. It may list one or more posts \
with their department, qualification, age limit, and domicile requirement,
or it may be a cover page / general instructions page with no post listings
— in that case, return an empty array.

For every actual post entry found on this page, output an object with:
- "post_name": the post title.
- "department": the department/organization for this post.
- "bps_scale": the BPS (Basic Pay Scale) number if stated (e.g. "17"), or null.
{QUALIFICATION_PROMPT_SNIPPET}
- "field_of_study": the required field(s), or "any discipline", or null.
- "age_range": as "MIN-MAX", or null.
- "domicile_requirement": the required domicile, or null.

Respond with ONLY a JSON array (empty if no posts on this page), no \
markdown fences, no commentary.
"""


def extract_fpsc_page(image_bytes: bytes, client: genai.Client) -> list[dict]:
    response = _with_retry(lambda: client.models.generate_content(
        model=MODEL,
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        ],
        config=genai_types.GenerateContentConfig(
            system_instruction=FPSC_PDF_VISION_PROMPT,
            response_mime_type="application/json",
        ),
    ))
    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, ValueError):
        log.warning("Could not parse FPSC page extraction as JSON: %r", response.text[:300])
        return []


FPSC_PROGRESS_FILE = "fpsc_pdf_progress.json"


def _load_resume_page() -> int:
    """Number of pages already fully processed in a prior run — resume from
    here instead of re-spending quota re-extracting pages already done."""
    try:
        with open(FPSC_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("last_completed_page", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _save_resume_page(page_num: int) -> None:
    with open(FPSC_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_completed_page": page_num}, f)


def run_fpsc(db_path: str = "jobs.db", max_pages: int | None = None) -> None:
    log.info("Downloading FPSC advertisement PDF (scanned, no text layer — using vision)...")
    pdf_bytes = download_pdf(FPSC_PDF_URL)
    log.info("Downloaded %d bytes", len(pdf_bytes))

    resume_from = _load_resume_page()
    if resume_from:
        log.info("Resuming from page %d (already completed in a prior run)", resume_from + 1)

    client = genai.Client()
    total_posts = 0
    quota_exhausted = False
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        # One connection reused across pages, but committed after EACH page —
        # a 32-page run makes 32 API calls over several minutes, and without
        # a per-page commit, a crash on page 20 would silently lose 1-19's
        # already-extracted data (get_connection only commits on clean exit).
        conn = _open_connection(db_path)
        try:
            for i, page in enumerate(pages):
                if i < resume_from:
                    continue
                img = page.to_image(resolution=150).original
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                try:
                    entries = extract_fpsc_page(buf.getvalue(), client)
                except DailyQuotaExhausted:
                    log.error("FPSC page %d/%d: hit the free-tier DAILY quota — stopping here, "
                              "not retrying further pages (won't clear until quota resets). "
                              "Re-run this script tomorrow; it will resume from page %d.",
                              i + 1, len(pages), i + 1)
                    quota_exhausted = True
                    break
                except (genai_errors.ServerError, genai_errors.ClientError) as exc:
                    log.error("FPSC page %d/%d: giving up after retries (%s), skipping this page",
                              i + 1, len(pages), exc)
                    _save_resume_page(i + 1)
                    continue
                if entries:
                    log.info("FPSC page %d/%d: %d post(s) found", i + 1, len(pages), len(entries))
                for entry in entries:
                    if not entry.get("post_name"):
                        continue
                    listing = JobListing(
                        source="FPSC",
                        post_title=entry["post_name"],
                        department=entry.get("department"),
                        bps_scale=entry.get("bps_scale"),
                        qualification=entry.get("min_qualification"),
                        age_limit=entry.get("age_range"),
                        closing_date=None,  # not present on these post-detail pages
                        advertisement_link=FPSC_PDF_URL,
                        raw_eligibility_text=None,
                        notes="Extracted from the consolidated advertisement PDF via Gemini vision.",
                    )
                    _is_new, row_id = upsert_listing(conn, listing)
                    conn.execute(
                        """
                        UPDATE jobs SET min_qualification = ?, field_of_study = ?,
                            age_range = ?, domicile_requirement = ?, qualification_raw_text = ?
                        WHERE id = ?
                        """,
                        (
                            entry.get("min_qualification"),
                            entry.get("field_of_study"),
                            entry.get("age_range"),
                            entry.get("domicile_requirement"),
                            entry.get("qualification_raw_text"),
                            row_id,
                        ),
                    )
                    total_posts += 1
                conn.commit()
                _save_resume_page(i + 1)
        finally:
            conn.close()

    if not quota_exhausted and resume_from < len(pages):
        log.info("All FPSC pages processed — clearing resume checkpoint.")
        try:
            import os
            os.remove(FPSC_PROGRESS_FILE)
        except FileNotFoundError:
            pass

    log.info("FPSC PDF: extracted and stored %d posts with eligibility data this run", total_posts)


if __name__ == "__main__":
    run_ppsc()
    run_fpsc()
