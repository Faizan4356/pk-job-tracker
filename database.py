"""
Phase 2 — Store scraped job listings in SQLite, deduplicated across daily runs.

A listing is considered "the same job" if (post_title, department, closing_date)
match an existing row — that's the natural key government job ads repeat across
scrape runs (the same ad often gets re-rendered with cosmetic HTML changes, so
we don't dedup on the full raw text or the link, which can vary slightly).

`first_seen_date` is set once, on insert, and never touched again. `last_seen_date`
is bumped every time a duplicate is scraped again, so you can tell a listing is
still live vs. it dropped off the site (and infer it closed/was pulled).
"""

from __future__ import annotations

import dataclasses
import datetime
import sqlite3
from contextlib import contextmanager

from scraper import JobListing

DB_PATH = "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL,
    post_title              TEXT,
    department              TEXT,
    bps_scale               TEXT,
    qualification           TEXT,
    age_limit               TEXT,
    closing_date            TEXT,
    advertisement_link      TEXT,
    raw_eligibility_text    TEXT,
    notes                   TEXT,

    -- Phase 3 AI-extracted structured fields (nullable until filtered)
    min_qualification       TEXT,
    field_of_study          TEXT,
    age_range               TEXT,
    domicile_requirement    TEXT,
    -- Original wording when min_qualification is "Professional/Specialist"
    -- or otherwise doesn't cleanly map to a standard level (see ai_filter.py)
    qualification_raw_text  TEXT,

    -- Semantic (embedding-similarity) match score, separate from the
    -- deterministic is_match flag computed at query time — see semantic_match.py
    semantic_match_score    REAL,

    -- Phase 5 dashboard tracker
    applied                 INTEGER NOT NULL DEFAULT 0,

    -- Telegram alerting (see telegram_alert.py) — whether this job has
    -- already been sent in an alert, so re-running daily doesn't re-notify
    alerted                 INTEGER NOT NULL DEFAULT 0,

    first_seen_date         TEXT NOT NULL,
    last_seen_date          TEXT NOT NULL,

    UNIQUE (post_title, department, closing_date)
);
"""

# Additive migrations for databases created before a column existed — each
# is a no-op (caught) if the column is already there. New installs get every
# column straight from SCHEMA above; this only matters for jobs.db files
# created by an earlier version of this project.
MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN qualification_raw_text TEXT",
    "ALTER TABLE jobs ADD COLUMN semantic_match_score REAL",
    "ALTER TABLE jobs ADD COLUMN alerted INTEGER NOT NULL DEFAULT 0",
]


@contextmanager
def get_connection(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists


def upsert_listing(conn: sqlite3.Connection, listing: JobListing) -> tuple[bool, int]:
    """Insert a listing if it's new, otherwise bump last_seen_date on the existing row.

    Returns (is_new, row_id).
    """
    today = datetime.date.today().isoformat()

    existing = conn.execute(
        """
        SELECT id FROM jobs
        WHERE post_title IS ? AND department IS ? AND closing_date IS ?
        """,
        (listing.post_title, listing.department, listing.closing_date),
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE jobs SET last_seen_date = ? WHERE id = ?",
            (today, existing["id"]),
        )
        return False, existing["id"]

    cursor = conn.execute(
        """
        INSERT INTO jobs (
            source, post_title, department, bps_scale, qualification,
            age_limit, closing_date, advertisement_link, raw_eligibility_text,
            notes, first_seen_date, last_seen_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            listing.source, listing.post_title, listing.department, listing.bps_scale,
            listing.qualification, listing.age_limit, listing.closing_date,
            listing.advertisement_link, listing.raw_eligibility_text, listing.notes,
            today, today,
        ),
    )
    return True, cursor.lastrowid


def store_listings(listings: list[JobListing], db_path: str = DB_PATH) -> tuple[int, int]:
    """Store a batch of listings. Returns (num_new, num_duplicates)."""
    init_db(db_path)
    num_new = 0
    num_dup = 0
    with get_connection(db_path) as conn:
        for listing in listings:
            is_new, _ = upsert_listing(conn, listing)
            if is_new:
                num_new += 1
            else:
                num_dup += 1
    return num_new, num_dup


def save_extracted_fields(row_id: int, fields: dict, db_path: str = DB_PATH) -> None:
    """Persist Phase 3's AI-extracted structured fields onto a job row."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs SET
                min_qualification = ?,
                field_of_study = ?,
                age_range = ?,
                domicile_requirement = ?,
                qualification_raw_text = ?
            WHERE id = ?
            """,
            (
                fields.get("min_qualification"),
                fields.get("field_of_study"),
                fields.get("age_range"),
                fields.get("domicile_requirement"),
                fields.get("qualification_raw_text"),
                row_id,
            ),
        )


def set_applied(row_id: int, applied: bool, db_path: str = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute("UPDATE jobs SET applied = ? WHERE id = ?", (int(applied), row_id))


def save_semantic_score(row_id: int, score: float, db_path: str = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute("UPDATE jobs SET semantic_match_score = ? WHERE id = ?", (score, row_id))


def mark_alerted(row_ids: list[int], db_path: str = DB_PATH) -> None:
    if not row_ids:
        return
    with get_connection(db_path) as conn:
        conn.executemany(
            "UPDATE jobs SET alerted = 1 WHERE id = ?", [(rid,) for rid in row_ids]
        )


def fetch_all_jobs(db_path: str = DB_PATH) -> list[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute("SELECT * FROM jobs ORDER BY closing_date ASC").fetchall()


def fetch_open_jobs(db_path: str = DB_PATH) -> list[sqlite3.Row]:
    """Jobs whose closing_date hasn't passed (or is unknown, so we don't hide it)."""
    today = datetime.date.today().isoformat()
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE closing_date IS NULL OR closing_date >= ? "
            "ORDER BY closing_date ASC",
            (today,),
        ).fetchall()


if __name__ == "__main__":
    import logging

    from scraper import scrape_all

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    scraped = scrape_all()
    new_count, dup_count = store_listings(scraped)
    logging.info("Stored %d new listings, %d already seen", new_count, dup_count)
