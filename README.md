# PK Govt Job Tracker

**Live dashboard:** https://pk-job-tracker-xgbxf6amw5icfxszctem8q.streamlit.app/
*(currently set to private on Streamlit Cloud — switch it to Public in the app's sharing settings if you want this link to actually work for others)*

A pipeline that scrapes Pakistani government job listings (FPSC, PPSC),
extracts each post's real eligibility requirements out of the PDF
advertisements they're bundled in, and shows you every open job grouped by
degree level in a dashboard — instead of you refreshing two government
websites and reading PDFs by hand.

## The problem this solves

FPSC and PPSC ads are inconsistent by nature: the same "minimum
qualification" requirement shows up as "Bachelor's degree", "BS/BA (16 years
of education)", "at least a graduate", or buried inside a PDF advertisement
covering dozens of unrelated posts at once, with no consistent template.
Reading every new ad to check age limit, domicile, and qualification against
your own eligibility is exactly the kind of repetitive, easy-to-get-wrong
task that costs people real opportunities — not because they weren't
qualified, but because they never opened the right PDF.

## How the AI-based extraction works

The pipeline splits "read messy text/scanned pages" from "decide what it
means" into two separate steps, deliberately:

1. **Extraction (the AI part).** Every post's eligibility text — or, for
   `pdf_extract.py`, entire PDF pages of text or scanned images — goes to
   Google's Gemini API, with instructions to normalize it into structured
   fields: minimum qualification (one of 8 categories — see below),
   qualification field of study, age range, and domicile requirement. This
   is the part that genuinely needs a model — government departments phrase
   the same rule a dozen different ways, and a regex-based approach breaks
   the moment a new ad's wording differs slightly from the last one. Gemini
   (`gemini-3.6-flash`) was chosen for its free tier — see the quota note
   below on what that actually means in practice.
2. **Matching (plain Python, deterministic).** Once a job has structured
   fields, comparing it against your profile is just arithmetic and string
   matching — no LLM call needed, which is both cheaper and gives a
   repeatable, auditable answer with an explicit reason attached, e.g.:

   > Matches: Bachelor's in any discipline accepted, age 24 is within 18-30
   > range, open to any domicile

   A job is only reported as a match if every criterion the ad actually
   states is satisfied — an ad that doesn't mention domicile isn't rejected
   for a domicile mismatch that was never asserted.
3. **Semantic matching (a separate, additive third layer — `semantic_match.py`).**
   A local sentence-transformer model (`all-MiniLM-L6-v2`, free, no API cost)
   embeds a free-text description of your background and every job's
   available text (post title, department, qualification, field of study),
   then scores cosine similarity. This is a genuinely different kind of
   signal from #2: it can surface a job that reads as a strong conceptual
   fit even when the structured fields are missing or ambiguous ("Database
   Administrator" scored 61% for a "BS Data Science, Python, ML, SQL"
   profile, well above most Bachelor's-level posts) — but unlike the
   deterministic matcher, it can't explain *why* with the same rigor, and it
   can be wrong in ways that are harder to audit. Deliberately **not**
   merged into one score: the dashboard shows both, clearly labeled (✅
   deterministic match vs. 🧠 semantic fit %), so you always know which kind
   of confidence you're looking at.

**Degree-level classification (8 categories).** `min_qualification` is
normalized to exactly one of: Matric, Intermediate, Bachelor, Master, MPhil,
PhD, **Professional/Specialist**, or **Not Specified** — this schema lives in
one place, `qualification_schema.py`, shared by every extraction prompt
(`ai_filter.py`, `pdf_extract.py`'s PPSC and FPSC paths) and the dashboard's
grouping, so all three classify consistently. "Professional/Specialist"
(FCPS, FRCS, CFA, bar-at-law, etc.) is deliberately **not** slotted onto the
Matric→PhD ladder — mapping a specialist credential onto "closest academic
level" is exactly the kind of silent mis-flattening that produced the
Oncology bug below, so instead it's its own category, `match_job()` never
auto-matches it (manual review only), and the dashboard shows it with its
own gold-colored section and the original wording preserved in
`qualification_raw_text`. Run `python validate_degree_extraction.py` after
any extraction run to see counts per bucket and flag anything unexpected.

## Tech stack

| Layer | Tool |
|---|---|
| Scraping | `requests` + `BeautifulSoup`, follows ASP.NET postback pagination if PPSC's table ever paginates |
| PDF extraction | `pdfplumber` (text) + Gemini vision (scanned pages) — `pdf_extract.py`, a separate opt-in step |
| Storage | SQLite (dedup on post title + department + closing date, tracks `first_seen_date`) |
| AI extraction | Google Gemini API (`gemini-3.6-flash`, free tier) |
| Semantic matching | `sentence-transformers` (`all-MiniLM-L6-v2`), local, free, no API quota |
| Dashboard | Streamlit, jobs grouped by degree level, animated theme |
| Automation | GitHub Actions, daily cron trigger (scrape + AI-extract only — no alert step, see below) |

## Project layout

```
scraper.py                    Phase 1 — FPSC/PPSC HTML scraping (robots.txt-respecting, rate-limited, paginated)
database.py                   Phase 2 — SQLite storage + dedup
qualification_schema.py       Shared 8-category degree classification schema (single source of truth)
ai_filter.py                  Phase 3 — Gemini-based extraction + deterministic profile matching
pdf_extract.py                Separate opt-in step — fetches & parses the PDF advertisements for real eligibility data
semantic_match.py             Separate, additive embedding-similarity matching layer
validate_degree_extraction.py Spot-check tool — run after any extraction change
dashboard.py                  Phase 5 — Streamlit UI, jobs grouped by degree level + calendar + applied tracker
pipeline.py                   Phase 6 — orchestrates scrape -> store -> AI-extract (daily, automated)
.github/workflows/daily.yml   Scheduled daily run
```

**Phase 4 (deadline alerts) has been removed** — twice, actually: first a
Gmail/SMTP implementation, then a Telegram rebuild, both taken back out.
There's currently no alert channel; use the dashboard's calendar view to
check upcoming deadlines. The `alerted` column still exists in the DB schema
(harmless, unused) in case alerting comes back later.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your Gemini API key and profile
```

Run the daily pipeline once manually (scrape + store + AI-extract from HTML):

```bash
python pipeline.py
```

Backfill real eligibility data from the PDF advertisements (separate,
explicit, opt-in — see "PDFs and robots.txt" below):

```bash
python pdf_extract.py
```

Compute semantic match scores (separate, additive — see above):

```bash
python semantic_match.py
```

View the dashboard:

```bash
streamlit run dashboard.py
```

## Automating it (Phase 6)

`.github/workflows/daily.yml` runs `pipeline.py` every day at 08:00 PKT via
GitHub Actions — no machine of yours needs to be on. It reads `GEMINI_API_KEY`,
`PROFILE_*`, and `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from the repo's
Actions secrets and commits the updated `jobs.db` back to the repo after each
run, so dedup state and your applied/not-applied checkboxes persist across
runs on GitHub's ephemeral runners. Set the secrets under **Settings →
Secrets and variables → Actions**.

`semantic_match.py` is **not** part of this scheduled workflow — it's cheap
to run (no API quota) but adds a real dependency (`sentence-transformers`,
which pulls in `torch`) and a slow first-run model download, so it's a
manual/opt-in step for now rather than something every daily run pays for.

`pdf_extract.py` is **not** part of this scheduled workflow — it's a
manually-run, explicitly-authorized step (see below), and its Gemini vision
calls are slow enough (free-tier rate limits) that it doesn't fit a daily
automated run anyway.

## PDFs and robots.txt — read this before running `pdf_extract.py`

Both FPSC and PPSC publish each post's real qualification/age/domicile
requirements only inside a PDF advertisement, and both sites' `robots.txt`
disallow crawling `*.pdf` files. `scraper.py` (the daily, automated part of
this project) honors that and never fetches PDFs.

`pdf_extract.py` is a **separate, manually-run script** that deliberately
fetches a small, fixed number of public advertisement PDFs — a judgment call
made because this is personal-use, low-volume, rate-limited fetching of
public documents, not bulk/automated crawling, and it was explicitly
requested. It is not wired into the daily pipeline or CI, and going past
`robots.txt` in your own automated systems is a decision you should make
deliberately, not something a scraper should do by default.

**What it does:**
- **PPSC's PDF** has a real embedded text layer — one Gemini call parses the
  whole document's text into structured entries, matched back to the
  existing HTML-scraped job rows by department + post-title overlap.
- **FPSC's PDF** is scanned page images with no text layer — each page is
  sent to Gemini's vision input individually (32 pages), and since FPSC's
  HTML only exposed one bundle-level placeholder row, the extracted posts
  are inserted as new individual job listings.

**Gemini's free tier is a hard 20 requests/day cap per model** (not just a
per-minute throttle) — `gemini-3.6-flash` on the free tier returns a
`RESOURCE_EXHAUSTED` error with `GenerateRequestsPerDayPerProjectPerModel-FreeTier`
once you hit it, and it does not clear until the next day. FPSC's 32-page
vision extraction alone can burn most of a day's quota. `pdf_extract.py`
handles this by:
- Retrying transient errors (503 overload, per-minute 429) with backoff.
- Detecting the *daily* quota error specifically and stopping immediately
  instead of wasting minutes retrying every remaining page.
- Committing progress to the database after every single page (not one
  all-or-nothing transaction), and writing a resume checkpoint
  (`fpsc_pdf_progress.json`) so re-running the next day continues from where
  it left off instead of re-spending quota on pages already done.

If you hit the daily cap often, enabling billing on the Gemini API key
removes it almost entirely at a cost of pennies for this project's volume.

## Known accuracy limitation (fixed going forward, 2026-09-01)

Gemini's extraction was originally good for straightforward posts (verified
word-for-word against the PPSC PDF for entries like "Assistant" —
Bachelor's, 18-25, Faisalabad District Only) but understated complex,
OR-conditional requirements: a "Senior Registrar Oncology" post whose real
requirement is FRCS/MRCP/MD (or MBBS+FCPS as a fallback) — genuine
postgraduate medical qualifications — got flattened to "Bachelor" because the
old schema's qualification levels (Matric → PhD) had no slot for a
professional/specialist degree.

**Fixed** by adding "Professional/Specialist" and "Not Specified" as their
own categories (not slotted onto the academic ladder — see "Degree-level
classification" above), verified against the exact same Oncology text that
originally exposed the bug: it now correctly extracts
`min_qualification="Professional/Specialist"` with the full original wording
preserved in `qualification_raw_text`, and `match_job()` never auto-matches
it. Also verified: Matric/Intermediate clerical posts aren't defaulted
upward to Bachelor's, and jobs with genuinely no stated requirement get
"Not Specified" rather than null or a guessed level.

**The fix applies to extraction going forward** (new prompt, new schema) —
it does **not** retroactively reclassify the ~68 rows already extracted
under the old schema before this fix; those still show whatever the old
prompt produced until `pdf_extract.py` is re-run against them (blocked by
today's exhausted Gemini quota as of this writing — see the quota note
above). Run `python validate_degree_extraction.py` to check current bucket
counts and spot-check anything flagged.

## Other things worth knowing

- FPSC's site is a Next.js app; its job-listing page happens to be
  server-rendered HTML so `scraper.py` doesn't need Selenium, but other FPSC
  pages may not be. As of 2026-09-01, FPSC currently has **zero active
  advertisements** listed (its own empty-state UI, not a scraper bug) — the
  "Consolidated Advertisement No. 3/2026" seen in earlier testing has since
  closed.
- PPSC's table showed 40 rows with no pagination controls as of 2026-08-31.
  `scraper.py` now detects and follows ASP.NET postback-style "next page"
  pagination if PPSC starts paginating a larger batch — but since no
  paginated PPSC page exists to test against yet, that path is implemented
  per standard ASP.NET GridView pager conventions and verified only for the
  "no pagination present" case (confirmed to behave identically to before:
  same 40 rows, single page). Worth a spot-check against a real paginated
  page if/when PPSC posts one large enough to trigger it.
- `ai_filter.py`'s `filter_jobs_for_profile()` still runs during the daily
  pipeline and logs how many jobs match your profile — check `dashboard.py`
  for the actual filtered/grouped view.

## Browse by Degree

The dashboard's main view ("Browse All Jobs by Degree Level") lists every
open job — not filtered to your profile — grouped under a fixed-order
section per qualification level (Matric → Intermediate → Bachelor → Master →
MPhil → PhD → Professional/Specialist → Not Specified → Not yet extracted),
each with a colored header and post count, and a sticky jump-to-section nav
bar at the top. Jobs matching your sidebar profile get a ✅ badge and green
"why it matched" box; non-matching jobs still show, labeled either "Outside
your current profile" or — for Professional/Specialist posts — a distinct
"requires manual review" warning, since those are never auto-matched.

## Screenshots

_Add screenshots of the dashboard here:_ `screenshots/dashboard.png` — not
yet added; needs an actual browser session against the live dashboard, which
wasn't available while building this.

## Why I built this

<!-- Personal note — replace with your own story. A couple of sentences on
what prompted this (a missed deadline, a friend who missed one, the sheer
tedium of checking two sites by hand) makes the project's value obvious to
anyone reading the repo, and is worth keeping even after the code changes. -->

---

**This pattern generalizes.** Scrape → AI-normalize inconsistent (including
PDF/scanned) text → deterministic filter isn't specific to government jobs —
the same architecture tracks real estate listings against a buyer's
criteria, scholarship deadlines against a student's eligibility, or price
drops against a wishlist. If you'd like a version of this built for a
different site or use case, that's a service I offer — reach out.
