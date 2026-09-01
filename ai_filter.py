"""
Phase 3 — Smart filtering.

Two separate steps, deliberately kept separate:

1. `extract_eligibility_fields()` — a single Gemini API call per job listing
   that reads the raw, inconsistently-worded eligibility text scraped in
   Phase 1 and pulls out four structured fields: minimum qualification,
   field of study, age range, and domicile requirement. This is the part
   that actually needs an LLM — government ads phrase the same requirement
   a dozen different ways ("Bachelor's degree", "BS/BA (16 years education)",
   "at least a graduate", ...).

2. `filter_jobs_for_profile()` — plain, deterministic Python that compares
   those already-structured fields against a user profile and explains why
   each match fits. No LLM call here: once the fields are structured, exact
   matching is more reliable and far cheaper than asking the model to judge
   fit on every run.

Model choice: Google's Gemini API (`gemini-2.0-flash`) has a genuinely free
tier (generous daily request quota at no cost), which is why this was swapped
in for the originally-planned Claude API — this project's extraction volume
(a handful of new job listings per day) comfortably fits inside it. Get a key
at https://aistudio.google.com/apikey and set GEMINI_API_KEY in .env.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from google import genai
from google.genai import types as genai_types

from qualification_schema import QUALIFICATION_PROMPT_SNIPPET

log = logging.getLogger("ai_filter")

MODEL = "gemini-3.6-flash"

EXTRACTION_SYSTEM_PROMPT = f"""\
You extract structured eligibility fields from Pakistani government job \
advertisement text. The wording varies a lot between ads (different \
departments, different eras of the same form) — infer the intent, don't \
require an exact phrase match.

Respond with ONLY a JSON object (no markdown fences, no commentary) with \
exactly these keys:

{QUALIFICATION_PROMPT_SNIPPET}
- "field_of_study": the required field(s) of study as a short string (e.g. \
  "Computer Science, Data Science, or related field"), or "any discipline" \
  if the ad accepts any field, or null if not stated.
- "age_range": the age range as "MIN-MAX" (e.g. "18-30"), or null if not \
  stated. If only a max age is given, use "0-MAX". Apply any explicitly \
  stated age relaxation only if it's part of the general eligibility, not \
  quota-specific.
- "domicile_requirement": the required domicile/province (e.g. "Punjab", \
  "Any Pakistani province", "Federal/Islamabad"), or null if not stated.
"""


@dataclass
class ExtractedEligibility:
    min_qualification: str | None
    field_of_study: str | None
    age_range: str | None
    domicile_requirement: str | None
    qualification_raw_text: str | None = None


def _parse_json_response(text: str) -> dict:
    """Claude is instructed to return raw JSON, but strip markdown fences
    defensively in case a model wraps it anyway."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.DOTALL)
    return json.loads(cleaned)


def _get_client() -> "genai.Client":
    import os

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def extract_eligibility_fields(
    raw_text: str, client: "genai.Client | None" = None
) -> ExtractedEligibility:
    """One Gemini API call: raw scraped eligibility text -> structured fields."""
    client = client or _get_client()

    if not raw_text or not raw_text.strip():
        return ExtractedEligibility(None, None, None, None, None)

    response = client.models.generate_content(
        model=MODEL,
        contents=raw_text,
        config=genai_types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
        ),
    )

    try:
        data = _parse_json_response(response.text)
    except (json.JSONDecodeError, ValueError):
        log.warning("Could not parse extraction response as JSON: %r", response.text[:200])
        return ExtractedEligibility(None, None, None, None, None)

    return ExtractedEligibility(
        min_qualification=data.get("min_qualification"),
        field_of_study=data.get("field_of_study"),
        age_range=data.get("age_range"),
        domicile_requirement=data.get("domicile_requirement"),
        qualification_raw_text=data.get("qualification_raw_text"),
    )


def extract_all_pending(db_path: str = "jobs.db") -> int:
    """Run extraction for every job row that hasn't been AI-processed yet.

    Returns the number of rows updated.
    """
    from database import get_connection, save_extracted_fields

    client = _get_client()
    updated = 0
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, raw_eligibility_text FROM jobs WHERE min_qualification IS NULL "
            "AND raw_eligibility_text IS NOT NULL"
        ).fetchall()

    for row in rows:
        fields = extract_eligibility_fields(row["raw_eligibility_text"], client=client)
        save_extracted_fields(
            row["id"],
            {
                "min_qualification": fields.min_qualification,
                "field_of_study": fields.field_of_study,
                "age_range": fields.age_range,
                "domicile_requirement": fields.domicile_requirement,
                "qualification_raw_text": fields.qualification_raw_text,
            },
            db_path=db_path,
        )
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# Deterministic profile matching (no LLM call — operates on extracted fields)
# ---------------------------------------------------------------------------

QUALIFICATION_LEVELS = {
    "matric": 1,
    "intermediate": 2,
    "bachelor": 3,
    "master": 4,
    "mphil": 5,
    "phd": 6,
}


def _qualification_level(text: str | None) -> int | None:
    if not text:
        return None
    return QUALIFICATION_LEVELS.get(text.strip().lower())


def _parse_age_range(age_range: str | None) -> tuple[int, int] | None:
    if not age_range:
        return None
    match = re.match(r"\s*(\d+)\s*-\s*(\d+)\s*", age_range)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass
class UserProfile:
    qualification: str          # e.g. "Bachelor" or "BS Data Science"
    field_of_study: str | None  # e.g. "Data Science"
    age: int
    domicile: str               # e.g. "Punjab"


def _infer_qualification_level(qualification_text: str) -> int | None:
    lowered = qualification_text.lower()
    for name, level in QUALIFICATION_LEVELS.items():
        if name in lowered:
            return level
    # Common shorthand
    if re.search(r"\bbs\b|\bba\b|\bbsc\b", lowered):
        return QUALIFICATION_LEVELS["bachelor"]
    if re.search(r"\bms\b|\bma\b|\bmsc\b", lowered):
        return QUALIFICATION_LEVELS["master"]
    return None


def match_job(job: sqlite3.Row, profile: UserProfile) -> tuple[bool, list[str]]:
    """Compare one job row's AI-extracted fields against a user profile.

    Returns (is_match, reasons). A job only counts as a match if every
    criterion that IS stated on the ad is satisfied — unstated criteria are
    skipped (we don't reject a job just because the ad omitted a field).
    """
    reasons: list[str] = []

    # "Professional/Specialist" quals (FCPS, FRCS, CFA, ...) aren't on the
    # Matric->PhD ladder at all — silently skipping the check (like an
    # unstated qualification) risks a false match, and mapping it onto the
    # ladder risks mis-flattening a specialist credential into "Bachelor".
    # Neither is safe against a plain profile string, so this is treated as
    # requiring manual review: never auto-matched, but still shown (with its
    # raw wording) in the dashboard rather than hidden or silently rejected.
    if job["min_qualification"] == "Professional/Specialist":
        return False, []

    # "Not Specified" means the ad genuinely states no minimum qualification
    # — same handling as an unstated/None field: skip this criterion rather
    # than reject or default it to any particular level.

    # Qualification
    required_level = _qualification_level(job["min_qualification"])
    user_level = _infer_qualification_level(profile.qualification)
    if required_level is not None:
        if user_level is None or user_level < required_level:
            return False, []
        reasons.append(
            f"{profile.qualification} meets the minimum requirement "
            f"({job['min_qualification']})"
        )

    # Field of study
    field = job["field_of_study"]
    if field:
        field_lower = field.lower()
        if "any discipline" in field_lower or "any field" in field_lower:
            reasons.append("Any discipline accepted")
        elif profile.field_of_study and profile.field_of_study.lower() in field_lower:
            reasons.append(f"Field of study matches ({field})")
        elif profile.field_of_study:
            return False, []
        # if profile.field_of_study is None and field is specific, we can't
        # confirm a match — treat as non-match to avoid false positives.
        else:
            return False, []

    # Age
    age_bounds = _parse_age_range(job["age_range"])
    if age_bounds is not None:
        lo, hi = age_bounds
        if not (lo <= profile.age <= hi):
            return False, []
        reasons.append(f"Age {profile.age} is within the {lo}-{hi} range")

    # Domicile
    domicile = job["domicile_requirement"]
    if domicile:
        domicile_lower = domicile.lower()
        if "any" in domicile_lower or "all pakistan" in domicile_lower:
            reasons.append("Open to any domicile")
        elif profile.domicile.lower() in domicile_lower:
            reasons.append(f"Domicile matches ({domicile})")
        else:
            return False, []

    if not reasons:
        # Nothing on the ad was structured/stated enough to confirm a match —
        # don't claim a match we can't actually justify.
        return False, []

    return True, reasons


def filter_jobs_for_profile(
    jobs: list[sqlite3.Row], profile: UserProfile
) -> list[tuple[sqlite3.Row, list[str]]]:
    """Return only jobs that genuinely match, each paired with why."""
    matches = []
    for job in jobs:
        is_match, reasons = match_job(job, profile)
        if is_match:
            matches.append((job, reasons))
    return matches


if __name__ == "__main__":
    import logging as _logging

    from database import fetch_open_jobs

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    updated = extract_all_pending()
    _logging.info("AI-extracted eligibility fields for %d jobs", updated)

    demo_profile = UserProfile(
        qualification="BS Data Science", field_of_study="Data Science", age=24, domicile="Punjab"
    )
    results = filter_jobs_for_profile(fetch_open_jobs(), demo_profile)
    for job, reasons in results:
        _logging.info("MATCH: %s (%s) — %s", job["post_title"], job["department"],
                       "; ".join(reasons))
