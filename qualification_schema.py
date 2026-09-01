"""
Shared qualification classification schema — the single source of truth for
the 7 degree-level buckets, used by:
  - ai_filter.py's HTML-based extraction prompt
  - pdf_extract.py's PPSC (text) and FPSC (vision) extraction prompts
  - dashboard.py's "Browse by Degree" grouping/ordering

Keeping this in one place means all three extraction/display paths classify
and order qualifications identically instead of drifting apart.
"""

# Ascending order for the 6 ordinal levels, then the two non-ordinal buckets.
# "Professional/Specialist" and "Not Specified" are deliberately NOT part of
# the Matric->PhD ladder — see ai_filter.py's match_job() for why each is
# handled as a special case instead of being slotted into the hierarchy.
QUALIFICATION_CATEGORIES = [
    "Matric", "Intermediate", "Bachelor", "Master", "MPhil", "PhD",
    "Professional/Specialist", "Not Specified",
]

# Embedded verbatim into every extraction prompt (ai_filter.py, pdf_extract.py)
# so all three extraction paths apply the exact same classification rules.
QUALIFICATION_PROMPT_SNIPPET = """\
- "min_qualification": the minimum education level required, normalized to \
  EXACTLY one of these 8 values — never invent a different label:
  - "Matric" (10 years of education / SSC / Matriculation)
  - "Intermediate" (12 years of education / FA / FSc / ICS / I.Com / HSSC)
  - "Bachelor" (a Bachelor's degree — 14 years under the older 2-year system \
    OR 16 years under the newer 4-year system; both map to this ONE bucket, \
    but if the ad states an explicit years-of-education number, ALSO fill \
    "qualification_raw_text" with that exact wording so the distinction \
    isn't lost)
  - "Master" (16-18 years of education, a Master's degree)
  - "MPhil"
  - "PhD"
  - "Professional/Specialist" — a professional or specialist qualification \
    that is NOT a standard academic degree level: FCPS, FRCS, MRCP, CFA, \
    CPA, ACCA, bar-at-law, board certification, etc. Use this INSTEAD OF \
    guessing which academic level it's "closest to" — do not flatten a \
    professional qualification into "Bachelor" or "Master" just because \
    it's roughly that many years of study. Always fill \
    "qualification_raw_text" with the original wording when you use this.
  - "Not Specified" — the ad genuinely states no minimum qualification at \
    all (some technical/trade/labor posts). Do NOT use this as a fallback \
    when you're merely unsure; use it only when the ad truly omits it.

  Years-of-education-only phrasing (no degree name given): map "14 years of \
  education" -> "Bachelor", "16 years of education" -> "Bachelor" (still — \
  16 years is the standard 4-year Bachelor's in Pakistan; put the raw \
  wording in qualification_raw_text if you're unsure whether it means \
  Bachelor's or Master's for that program), "18 years of education" -> \
  "Master".

  Do NOT default upward: a Matric-level clerical/technical post (Junior \
  Clerk, Naib Tehsildar, Sub-Inspector, driver, technician) must be \
  classified as "Matric" or "Intermediate", not bumped to "Bachelor" \
  because you're extracting from a mixed batch that's mostly Bachelor's \
  posts. Read each entry's own qualification line independently.
- "qualification_raw_text": the original qualification wording verbatim, \
  when min_qualification is "Professional/Specialist" or when the ad's \
  phrasing doesn't cleanly map to one of the standard levels (e.g. an \
  explicit years-of-education number). null/omit otherwise — don't repeat \
  ordinary "Bachelor's degree" wording here, only the ambiguous/special cases.\
"""
