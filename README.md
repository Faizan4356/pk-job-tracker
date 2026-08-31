# PK Govt Job Tracker

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
   fields: minimum qualification, field of study, age range, and domicile
   requirement. This is the part that genuinely needs a model — government
   departments phrase the same rule a dozen different ways, and a
   regex-based approach breaks the moment a new ad's wording differs
   slightly from the last one. Gemini (`gemini-3.6-flash`) was chosen for
   its free tier — see the quota note below on what that actually means in
   practice.
2. **Matching (plain Python, deterministic).** Once a job has structured
   fields, comparing it against your profile is just arithmetic and string
   matching — no LLM call needed, which is both cheaper and gives a
   repeatable, auditable answer with an explicit reason attached, e.g.:

   > Matches: Bachelor's in any discipline accepted, age 24 is within 18-30
   > range, open to any domicile

   A job is only reported as a match if every criterion the ad actually
   states is satisfied — an ad that doesn't mention domicile isn't rejected
   for a domicile mismatch that was never asserted.

## Tech stack

| Layer | Tool |
|---|---|
| Scraping | `requests` + `BeautifulSoup` |
| PDF extraction | `pdfplumber` (text) + Gemini vision (scanned pages) — `pdf_extract.py`, a separate opt-in step |
| Storage | SQLite (dedup on post title + department + closing date, tracks `first_seen_date`) |
| AI extraction | Google Gemini API (`gemini-3.6-flash`, free tier) |
| Dashboard | Streamlit, jobs grouped by degree level |
| Automation | GitHub Actions, daily cron trigger (scrape + AI-extract only — no alert step currently, see below) |

## Project layout

```
scraper.py       Phase 1 — FPSC/PPSC HTML scraping (robots.txt-respecting, rate-limited)
database.py      Phase 2 — SQLite storage + dedup
ai_filter.py     Phase 3 — Gemini-based extraction + deterministic profile matching
pdf_extract.py   Separate opt-in step — fetches & parses the PDF advertisements for real eligibility data
dashboard.py     Phase 5 — Streamlit UI, jobs grouped by degree level + calendar + applied tracker
pipeline.py      Phase 6 — orchestrates scrape -> store -> AI-extract (daily, automated)
.github/workflows/daily.yml   Scheduled daily run
```

**Phase 4 (deadline alerts) has been removed.** It was originally built on
Gmail/SMTP; that's been dropped and no replacement channel has been wired up
yet (Telegram was the other option on the table). For now, check upcoming
deadlines via the dashboard's calendar view.

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

View the dashboard:

```bash
streamlit run dashboard.py
```

## Automating it (Phase 6)

`.github/workflows/daily.yml` runs `pipeline.py` every day at 08:00 PKT via
GitHub Actions — no machine of yours needs to be on. It reads `GEMINI_API_KEY`
and `PROFILE_*` from the repo's Actions secrets and commits the updated
`jobs.db` back to the repo after each run, so dedup state and your
applied/not-applied checkboxes persist across runs on GitHub's ephemeral
runners. Set the secrets under **Settings → Secrets and variables → Actions**.

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

## Known accuracy limitation

Gemini's extraction is good for straightforward posts (verified word-for-word
against the PPSC PDF for entries like "Assistant" — Bachelor's, 18-25,
Faisalabad District Only) but can understate complex, OR-conditional
requirements. Example: a "Senior Registrar Oncology" post whose real
requirement is FRCS/MRCP/MD (or MBBS+FCPS as a fallback) — genuine
postgraduate medical qualifications — got flattened to "Bachelor" because the
extraction schema's qualification levels (Matric → PhD) don't have a slot for
that kind of professional/specialist degree. Worth a manual PDF check for any
job you're seriously considering, especially medical and other specialist
posts.

## Other things worth knowing

- FPSC's site is a Next.js app; its job-listing page happens to be
  server-rendered HTML so `scraper.py` doesn't need Selenium, but other FPSC
  pages may not be.
- PPSC's table showed 40 rows with no pagination controls as of 2026-08-31 —
  if PPSC starts paginating a larger batch, `scraper.py`'s PPSC parser
  doesn't currently follow "next page" links.
- `ai_filter.py`'s `filter_jobs_for_profile()` still runs during the daily
  pipeline and logs how many jobs match your profile — check `dashboard.py`
  for the actual filtered/grouped view.

## Screenshots

_Add screenshots of the dashboard here:_ `screenshots/dashboard.png`

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
