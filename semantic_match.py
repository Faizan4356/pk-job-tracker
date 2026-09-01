"""
Semantic (embedding-similarity) matching — a genuinely separate, additive
signal alongside ai_filter.py's deterministic rule-based matcher.

Why kept separate rather than merged into one score: ai_filter.py's
deterministic matcher compares exact structured fields (qualification
level, age range, domicile) and is fully auditable — every match comes with
a human-readable reason ("age 24 is within the 18-30 range"). This semantic
matcher instead embeds free text with a local sentence-transformer model and
measures cosine similarity — it can catch a job that reads as a strong
conceptual fit even when the structured fields are missing or ambiguous, but
it can't explain WHY with the same rigor, and it can be wrong in ways that
are hard to audit (embedding similarity is fuzzy, not a proof). Merging the
two into one score would hide which kind of confidence you're looking at, so
they stay as two separate, clearly-labeled signals: `is_match`/`reasons`
(rules) and `semantic_match_score` (embeddings) — see dashboard.py, which
shows both.

Model: all-MiniLM-L6-v2 (sentence-transformers) — free, runs locally, no API
cost or quota (unlike the Gemini extraction steps, which do have a daily cap).

Note on job text: FPSC/PPSC publish real eligibility details only inside PDF
advertisements (see README's Known Limitations), so `raw_eligibility_text` is
almost always empty in this project's data. Rather than embed an empty
string, each job's embedding text is built from whatever structured fields
ARE populated (post title, department, qualification, field of study, raw
qualification wording) — falling back to raw_eligibility_text too, for any
job that does have it.

Run manually:
    python semantic_match.py
"""

from __future__ import annotations

import logging

log = logging.getLogger("semantic_match")

MODEL_NAME = "all-MiniLM-L6-v2"

_model = None


def _get_model():
    """Lazy singleton — loading the model (~80MB download on first use,
    cached after) is the slow part; don't repeat it across calls in the
    same process."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        log.info("Loading sentence-transformer model %s (first call only, "
                  "downloads ~80MB on first ever run)...", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _job_text(job) -> str:
    """Build the text to embed for one job from whatever fields are
    populated — see module docstring for why raw_eligibility_text alone
    isn't enough in this project's actual data."""
    parts = [
        job["post_title"],
        job["department"],
        job["min_qualification"],
        job["field_of_study"],
        job["qualification_raw_text"],
        job["raw_eligibility_text"],
    ]
    return " | ".join(p for p in parts if p)


def compute_semantic_scores(profile_description: str, db_path: str = "jobs.db") -> int:
    """Embed the profile description and every open job's text, score cosine
    similarity (both embeddings are normalized, so cosine similarity is just
    the dot product), and persist each job's score via save_semantic_score.

    Returns the number of jobs scored.
    """
    from database import fetch_open_jobs, save_semantic_score

    jobs = fetch_open_jobs(db_path)
    jobs_with_text = [(job, _job_text(job)) for job in jobs]
    jobs_with_text = [(job, text) for job, text in jobs_with_text if text]

    if not jobs_with_text:
        log.info("No jobs with any text to embed.")
        return 0

    model = _get_model()
    profile_embedding = model.encode(profile_description, normalize_embeddings=True)
    job_texts = [text for _job, text in jobs_with_text]
    job_embeddings = model.encode(job_texts, normalize_embeddings=True)

    scores = job_embeddings @ profile_embedding

    for (job, _text), score in zip(jobs_with_text, scores):
        save_semantic_score(job["id"], float(score))

    return len(jobs_with_text)


if __name__ == "__main__":
    import logging as _logging
    import os

    from dotenv import load_dotenv

    load_dotenv()
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Free-text description of your own background — separate from
    # ai_filter.UserProfile's structured fields, deliberately: this is meant
    # to capture nuance ("interested in", "skills in") that a structured
    # qualification/age/domicile profile can't express.
    profile_description = os.environ.get(
        "PROFILE_DESCRIPTION",
        f"{os.environ.get('PROFILE_QUALIFICATION', 'BS Data Science')} graduate. "
        f"Skills: Python, machine learning, SQL, data analysis, statistics. "
        f"Interested in {os.environ.get('PROFILE_FIELD_OF_STUDY', 'Data Science')}, "
        f"computer science, IT, and analytics roles.",
    )
    n = compute_semantic_scores(profile_description)
    _logging.info("Scored %d jobs against profile: %r", n, profile_description)
