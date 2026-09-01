"""
Validation step for Step 8's degree-extraction fix — run this after any
change to the qualification classification prompts (qualification_schema.py,
ai_filter.py, pdf_extract.py) to spot-check for mis-classifications before
trusting the data.

Prints the count of jobs per qualification_level bucket, and flags any job
whose min_qualification is null (not yet extracted) or doesn't match one of
the 8 known categories (a genuine bug — the prompt is supposed to constrain
the model to exactly these values).

Run manually:
    python validate_degree_extraction.py
"""

from __future__ import annotations

from database import fetch_all_jobs
from qualification_schema import QUALIFICATION_CATEGORIES


def validate(db_path: str = "jobs.db") -> None:
    jobs = fetch_all_jobs(db_path)

    counts: dict[str, int] = {level: 0 for level in QUALIFICATION_CATEGORIES}
    counts["(not yet extracted)"] = 0
    unexpected: list[tuple[int, str, str]] = []

    for job in jobs:
        level = job["min_qualification"]
        if level is None:
            counts["(not yet extracted)"] += 1
        elif level in QUALIFICATION_CATEGORIES:
            counts[level] += 1
        else:
            unexpected.append((job["id"], job["post_title"], level))

    print(f"Total jobs: {len(jobs)}\n")
    print("Counts per qualification_level bucket:")
    for level in QUALIFICATION_CATEGORIES + ["(not yet extracted)"]:
        print(f"  {level:<26} {counts[level]}")

    if unexpected:
        print(f"\n{len(unexpected)} job(s) with an UNEXPECTED min_qualification value "
              f"(not one of the 8 known categories — the extraction prompt should "
              f"never produce these; investigate):")
        for row_id, title, level in unexpected:
            print(f"  id={row_id}  {title!r}  ->  {level!r}")
    else:
        print("\nNo unexpected values — every extracted row falls into a known bucket.")

    # Spot-check candidates worth a manual look: Professional/Specialist rows
    # (never auto-matched, worth eyeballing the raw text) and anything with
    # qualification_raw_text set (ambiguous/special-cased by the model).
    flagged = [j for j in jobs if j["min_qualification"] == "Professional/Specialist"
               or j["qualification_raw_text"]]
    if flagged:
        print(f"\n{len(flagged)} job(s) worth a manual spot-check "
              f"(Professional/Specialist and/or ambiguous wording preserved):")
        for job in flagged:
            print(f"  id={job['id']}  {job['post_title']!r}  "
                  f"level={job['min_qualification']!r}  "
                  f"raw={job['qualification_raw_text']!r}")


if __name__ == "__main__":
    validate()
