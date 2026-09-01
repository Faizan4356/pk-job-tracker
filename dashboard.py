"""
Phase 5 — Streamlit dashboard.

Run with:
    streamlit run dashboard.py

Shows:
  1. Every currently open job from FPSC + PPSC, grouped by minimum degree
     level (Matric / Intermediate / Bachelor / Master / MPhil / PhD / Not
     listed), sorted by closing date within each group — not just ones
     matching one profile. Run `python pdf_extract.py` first (a separate,
     explicit, opt-in step — see its docstring) to populate qualification/
     age/domicile from the PDF advertisements; without that, everything
     falls into "Not yet extracted" and you'll need to check each PDF yourself.
  2. A calendar view of upcoming deadlines for the current month, across all
     open jobs — click a date with a 🔴 marker to list every job closing
     that day below the calendar.
  3. An "applied / not applied" checkbox per job, persisted back to SQLite
     immediately on toggle (no separate save step).

The sidebar profile is optional: when a job has AI-extracted eligibility
fields, it's shown with a "matches your profile" badge. A checkbox lets you
switch to showing only those matches.
"""

from __future__ import annotations

import calendar
import datetime

import streamlit as st

from ai_filter import UserProfile, match_job
from database import fetch_open_jobs, init_db, set_applied
from qualification_schema import QUALIFICATION_CATEGORIES

st.set_page_config(page_title="PK Govt Job Tracker", layout="wide")

init_db()

# ---------------------------------------------------------------------------
# Theming — animated moving-gradient background + frosted-glass cards so
# text stays legible over it, plus a distinct accent color per degree level.
# Streamlit doesn't support this via config.toml (no animation support), so
# it's injected as raw CSS targeting stable data-testid selectors.
# ---------------------------------------------------------------------------

DEGREE_COLORS = {
    "Matric": "#3b82f6",                # blue
    "Intermediate": "#14b8a6",          # teal
    "Bachelor": "#22c55e",              # green
    "Master": "#a855f7",                # purple
    "MPhil": "#f97316",                 # orange
    "PhD": "#ec4899",                   # pink
    "Professional/Specialist": "#eab308",  # gold — deliberately distinct,
    #  never auto-matched (see ai_filter.match_job)
    "Not Specified": "#94a3b8",         # slate — ad states no requirement
    "Not yet extracted": "#6b7280",     # gray — extraction hasn't run yet
}

st.markdown(
    """
    <style>
    @keyframes moveGradient {
        0%   { background-position: 0% 50%; }
        50%  { background-position: 100% 50%; }
        100% { background-position: 0% 50%; }
    }
    .stApp {
        background: linear-gradient(
            -45deg, #0f2027, #2c5364, #1e3c72, #134e5e, #2b5876, #0f2027
        );
        background-size: 400% 400%;
        animation: moveGradient 20s ease infinite;
    }
    [data-testid="stSidebar"] {
        background: rgba(15, 23, 42, 0.85);
        backdrop-filter: blur(6px);
    }
    [data-testid="stExpander"] {
        background: rgba(255, 255, 255, 0.94);
        border-radius: 12px;
        border: 1px solid rgba(255, 255, 255, 0.3);
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
        margin-bottom: 0.5rem;
    }
    /* Light text everywhere by default (readable on the dark moving
       background), overridden back to dark text specifically inside the
       white expander cards — without this split, job details inside the
       cards would render as near-invisible light-on-white text. */
    h1, h2, h3, h4 {
        color: #ffffff !important;
        text-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
    }
    p, span, label, .stMarkdown, .stCaption, [data-testid="stCaptionContainer"] {
        color: #f1f5f9;
    }
    /* Catch-all (not just specific tags) so this holds regardless of which
       element Streamlit's expander header/body actually use internally. */
    [data-testid="stExpander"] * {
        color: #1e293b !important;
        text-shadow: none !important;
    }
    .stButton > button, .stLinkButton > a {
        border-radius: 8px;
        transition: transform 0.15s ease;
    }
    .stButton > button:hover, .stLinkButton > a:hover {
        transform: scale(1.03);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

GITHUB_REPO_URL = "https://github.com/Faizan4356/pk-job-tracker"

title_col, link_col = st.columns([5, 1])
with title_col:
    st.title("🇵🇰 Pakistani Government Job Tracker")
with link_col:
    st.link_button("⭐ View on GitHub", GITHUB_REPO_URL, use_container_width=True)

# ---------------------------------------------------------------------------
# Sidebar — optional profile (used only to badge/filter jobs that DO have
# AI-extracted eligibility fields; most won't yet — see module docstring)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.link_button("⭐ View source on GitHub", GITHUB_REPO_URL, use_container_width=True)
    st.divider()
    st.header("Your Profile")
    st.caption("Used to badge jobs that have eligibility data. Most listings "
               "don't yet — see the note at the top of the page.")
    qualification = st.text_input("Qualification", value="BS Data Science")
    field_of_study = st.text_input("Field of study", value="Data Science")
    age = st.number_input("Age", min_value=16, max_value=65, value=24)
    domicile = st.text_input("Domicile", value="Punjab")
    only_matches = st.checkbox("Only show jobs matching my profile", value=False)
    st.divider()
    st.caption("Semantic match score (from semantic_match.py) is a separate, "
               "fuzzier signal from embedding similarity — not a substitute "
               "for the deterministic match above.")
    sort_by_semantic = st.checkbox("Sort each degree section by semantic match score", value=False)

profile = UserProfile(
    qualification=qualification,
    field_of_study=field_of_study or None,
    age=int(age),
    domicile=domicile,
)


def render_job_card(job, is_match: bool, reasons: list[str], key_prefix: str = "") -> None:
    """One expander for a single job — shared by the grouped list and the
    calendar's click-a-date view so both render identically. key_prefix keeps
    widget keys unique when the same job appears in both places at once."""
    days_left = None
    if job["closing_date"]:
        try:
            days_left = (datetime.date.fromisoformat(job["closing_date"]) - datetime.date.today()).days
        except ValueError:
            pass

    urgent = days_left is not None and days_left <= 2
    match_flag = " ✅" if is_match else ""
    semantic_score = job["semantic_match_score"]
    semantic_flag = f" 🧠{semantic_score * 100:.0f}%" if semantic_score is not None else ""
    label = (f"{'🚨 ' if urgent else ''}{job['post_title']} — {job['department'] or 'N/A'}"
             f"{match_flag}{semantic_flag}")

    with st.expander(label, expanded=urgent, key=f"{key_prefix}expander-{job['id']}"):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.write(f"**Source:** {job['source']}")
            st.write(f"**Closing date:** {job['closing_date'] or 'Not stated'}"
                      + (f" ({days_left} days left)" if days_left is not None else ""))
            st.write(f"**BPS scale:** {job['bps_scale'] or 'N/A'}")
            st.write(f"**Degree/qualification required:** "
                      f"{job['min_qualification'] or 'Not yet extracted — check the PDF'}")
            st.write(f"**Field of study:** {job['field_of_study'] or 'N/A'}")
            st.write(f"**Age range:** {job['age_range'] or 'N/A'}")
            st.write(f"**Domicile:** {job['domicile_requirement'] or 'N/A'}")
            if is_match:
                st.success(f"✅ Matches your profile (deterministic): {'; '.join(reasons)}")
            elif job["min_qualification"] == "Professional/Specialist":
                st.warning("Professional/specialist qualification — requires manual "
                           "review, not auto-matched to any profile.")
            else:
                st.caption("Outside your current profile (or not enough data to confirm a match).")
            if semantic_score is not None:
                st.info(f"🧠 {semantic_score * 100:.0f}% semantic fit — a separate, fuzzier "
                        f"embedding-similarity signal (semantic_match.py), not the same as "
                        f"the deterministic match above.")
            if job["notes"]:
                st.caption(job["notes"])
        with col2:
            applied = st.checkbox(
                "Applied", value=bool(job["applied"]), key=f"{key_prefix}applied-{job['id']}"
            )
            if applied != bool(job["applied"]):
                set_applied(job["id"], applied)
                st.rerun()
            if job["advertisement_link"]:
                st.link_button(
                    "📄 Full PDF advertisement",
                    job["advertisement_link"],
                    key=f"{key_prefix}pdf-{job['id']}",
                    help="Open the complete advertisement PDF for full details "
                         "(syllabus, exact eligibility wording, how to apply).",
                    use_container_width=True,
                )

# ---------------------------------------------------------------------------
# Load all open jobs (unfiltered) + compute match status per job
# ---------------------------------------------------------------------------

open_jobs = fetch_open_jobs()
open_jobs = sorted(open_jobs, key=lambda j: j["closing_date"] or "9999-99-99")

jobs_with_match = []
for job in open_jobs:
    is_match, reasons = match_job(job, profile)
    jobs_with_match.append((job, is_match, reasons))

match_count = sum(1 for _, is_match, _ in jobs_with_match if is_match)
have_eligibility_count = sum(1 for job in open_jobs if job["min_qualification"] is not None)

st.caption(
    f"{len(open_jobs)} open listings scraped from FPSC + PPSC · "
    f"{have_eligibility_count} have AI-extracted eligibility data · "
    f"{match_count} match your profile"
)

if have_eligibility_count == 0:
    st.info(
        "None of the currently scraped listings have eligibility text to match "
        "against yet. Run `python pdf_extract.py` (a separate, explicit, "
        "opt-in step) to backfill qualification/age/domicile from the PDF "
        "advertisements — see README. Showing every open job below under "
        "'Not yet extracted' until then.",
        icon="ℹ️",
    )

# ---------------------------------------------------------------------------
# 1. Job list grouped by degree level, sorted by closing date within group
# ---------------------------------------------------------------------------

st.subheader("Browse All Jobs by Degree Level")
st.caption("Every open job, grouped by extracted degree level — not filtered to "
           "your profile. Jobs outside your profile are still shown, labeled "
           "accordingly, so you can browse the full picture.")

display_jobs = [item for item in jobs_with_match if item[1]] if only_matches else jobs_with_match

if not display_jobs:
    st.info("No jobs to show." if only_matches else
             "No open listings right now — check back after the next scrape run.")

# Fixed degree order, single source of truth shared with the extraction
# prompts (qualification_schema.py) — plus one dashboard-only bucket for
# rows extraction genuinely hasn't run on yet (None), distinct from the AI
# explicitly determining "Not Specified".
DEGREE_ORDER = QUALIFICATION_CATEGORIES + ["Not yet extracted"]

groups: dict[str, list] = {level: [] for level in DEGREE_ORDER}
for item in display_jobs:
    job = item[0]
    level = job["min_qualification"] if job["min_qualification"] in QUALIFICATION_CATEGORIES else "Not yet extracted"
    groups[level].append(item)

# Sticky anchor nav — jump straight to a degree section instead of scrolling.
nonempty_levels = [level for level in DEGREE_ORDER if groups[level]]
if nonempty_levels:
    nav_html = " &nbsp;·&nbsp; ".join(
        f'<a href="#{level.lower().replace("/", "-").replace(" ", "-")}" '
        f'style="color:{DEGREE_COLORS.get(level, "#fff")} !important; font-weight:600; '
        f'text-decoration:none;">{level} ({len(groups[level])})</a>'
        for level in nonempty_levels
    )
    st.markdown(
        f'<div style="position:sticky; top:0; z-index:100; background:rgba(15,23,42,0.92); '
        f'backdrop-filter:blur(6px); padding:0.6rem 1rem; border-radius:10px; '
        f'margin-bottom:0.8rem; font-size:0.9rem;">{nav_html}</div>',
        unsafe_allow_html=True,
    )

for level in DEGREE_ORDER:
    jobs_in_group = groups[level]
    if not jobs_in_group:
        continue

    color = DEGREE_COLORS.get(level, "#ffffff")
    anchor = level.lower().replace("/", "-").replace(" ", "-")
    count_label = f"{len(jobs_in_group)} post{'s' if len(jobs_in_group) != 1 else ''}"
    st.markdown(
        f'<h3 id="{anchor}" style="color:{color} !important; text-shadow: 0 2px 8px rgba(0,0,0,0.4); '
        f'border-left: 6px solid {color}; padding-left: 0.6rem; scroll-margin-top: 4rem;">{level} '
        f'<span style="font-size:0.6em; opacity:0.85;">({count_label})</span></h3>',
        unsafe_allow_html=True,
    )
    if level == "Professional/Specialist":
        st.caption("These require a professional/specialist credential (FCPS, FRCS, CFA, etc.) "
                   "that doesn't map onto a standard academic level — never auto-matched, "
                   "check the raw wording and PDF yourself.")

    if sort_by_semantic:
        jobs_in_group = sorted(
            jobs_in_group,
            key=lambda item: item[0]["semantic_match_score"] if item[0]["semantic_match_score"] is not None else -1,
            reverse=True,
        )

    for job, is_match, reasons in jobs_in_group:
        render_job_card(job, is_match, reasons)

# ---------------------------------------------------------------------------
# 2. Calendar view of upcoming deadlines (all open jobs) — click a date to
#    list every job closing that day, below the calendar.
# ---------------------------------------------------------------------------

st.subheader("Upcoming Deadlines — Calendar")
st.caption("Click a date with 🔴 to list every job closing that day.")

today = datetime.date.today()

# Default to whichever month actually has the nearest upcoming deadline —
# a plain "current month" calendar renders empty near month-end (e.g. the
# last day of August, when every open job closes in September).
if "calendar_year" not in st.session_state:
    upcoming_dates = []
    for job in open_jobs:
        if not job["closing_date"]:
            continue
        try:
            d = datetime.date.fromisoformat(job["closing_date"])
        except ValueError:
            continue
        if d >= today:
            upcoming_dates.append(d)
    default_date = min(upcoming_dates) if upcoming_dates else today
    st.session_state.calendar_year = default_date.year
    st.session_state.calendar_month = default_date.month

if "selected_calendar_day" not in st.session_state:
    st.session_state.selected_calendar_day = None

nav_col1, nav_col2, nav_col3 = st.columns([1, 3, 1])
with nav_col1:
    if st.button("◀ Prev"):
        st.session_state.calendar_month -= 1
        if st.session_state.calendar_month < 1:
            st.session_state.calendar_month = 12
            st.session_state.calendar_year -= 1
        st.session_state.selected_calendar_day = None
        st.rerun()
with nav_col3:
    if st.button("Next ▶"):
        st.session_state.calendar_month += 1
        if st.session_state.calendar_month > 12:
            st.session_state.calendar_month = 1
            st.session_state.calendar_year += 1
        st.session_state.selected_calendar_day = None
        st.rerun()

cal_year = st.session_state.calendar_year
cal_month = st.session_state.calendar_month

deadline_jobs_by_day: dict[int, list] = {}
for item in jobs_with_match:
    job = item[0]
    if not job["closing_date"]:
        continue
    try:
        d = datetime.date.fromisoformat(job["closing_date"])
    except ValueError:
        continue
    if d.year == cal_year and d.month == cal_month:
        deadline_jobs_by_day.setdefault(d.day, []).append(item)

cal = calendar.Calendar(firstweekday=0)  # Monday first
weeks = cal.monthdayscalendar(cal_year, cal_month)

with nav_col2:
    st.markdown(f"**{calendar.month_name[cal_month]} {cal_year}**")
header_cols = st.columns(7)
for col, day_name in zip(header_cols, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
    col.markdown(f"**{day_name}**")

for week in weeks:
    cols = st.columns(7)
    for col, day in zip(cols, week):
        if day == 0:
            col.write("")
            continue
        jobs_that_day = deadline_jobs_by_day.get(day, [])
        is_today = (cal_year, cal_month, day) == (today.year, today.month, today.day)
        is_selected = st.session_state.selected_calendar_day == day

        if jobs_that_day:
            button_label = f"{'📌 ' if is_today else ''}{day} 🔴{len(jobs_that_day)}"
            if col.button(button_label, key=f"cal-day-{day}",
                          type="primary" if is_selected else "secondary"):
                st.session_state.selected_calendar_day = None if is_selected else day
                st.rerun()
        else:
            col.markdown(f"**{day}**" if is_today else str(day))

st.caption("🔴 = at least one open job closes that day (count shown) · 📌 = today")

selected_day = st.session_state.selected_calendar_day
if selected_day is not None:
    selected_date = datetime.date(cal_year, cal_month, selected_day)
    jobs_that_day = deadline_jobs_by_day.get(selected_day, [])
    st.markdown(f"#### Jobs closing on {selected_date.isoformat()} ({len(jobs_that_day)})")
    if st.button("Clear selection"):
        st.session_state.selected_calendar_day = None
        st.rerun()
    for job, is_match, reasons in jobs_that_day:
        render_job_card(job, is_match, reasons, key_prefix="cal-")
