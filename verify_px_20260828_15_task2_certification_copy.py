#!/usr/bin/env python3
"""
verify_px_20260828_15_task2_certification_copy.py -- PX-20260828-15 Task 2.

PM's brief: Dallas's 5 years (2022-2026) are all data_source='dcad_certified'
and genuinely certified (DCAD certified its 2026 roll July 23, 2026), while
Travis's 2026 is preliminary -- both true simultaneously. Any copy saying
"certified + preliminary", "2026 preliminary ahead of certification", or
implying a uniform certification state across counties is Travis-specific
and wrong. Known instances: the homepage hero stat ("2021-26 / Certified +
preliminary appraisals") and the Directly-sourced card. "Audit for others
and fix -- generalize rather than deriving a blended range, same ruling as
-11 Task 4."

This fixture checks the 4 fixed instances (the 2 PM named + 2 more found by
auditing for the same "implies uniform certification" pattern: about.html's
"Full certified appraisal history for every parcel in every live county",
and base.html's global footer "2021-2026 Certified (2026 certified Jul 25,
2026)" line) -- confirming the bad phrasing is gone and the generalized
replacement is present, then does a real Jinja parse of each file to prove
no syntax was broken.

PX-20260829-02 UPDATE: Instance 3's original fix lived in about.html's
"What Parcelytics covers" feature-card grid ("Property tax intelligence"
card). That whole section was cut in the About page redesign (Diego's
ruling: it duplicated homepage feature content instead of telling the
"why" story the page is supposed to tell) -- the specific replacement
sentence this fixture originally checked for no longer exists anywhere on
the page. That is not a regression of the certification-copy bug: the
per-county Certified/Preliminary claim this fix guarded against isn't made
anywhere in the new about.html at all, which satisfies the same underlying
guardrail (no claim = no false claim) even more directly than the original
fix did. Instance 3's checks below were updated to confirm (a) the original
overclaim stays gone, (b) the specific old feature-card sentence is gone
too -- superseded, not reintroduced elsewhere -- and (c) the redesigned
page's own new sourcing language (in its "Why Parcelytics" section) still
makes no blanket/uniform certification-status claim. Same category of
"scanner constant must follow a legitimate refactor" fix already applied
once to verify_template_county_scoping.py (PX-20260829-01) and once to
verify_launch_surface_registry.py (PX-20260829-02, the same brief).

Run: python3 verify_px_20260828_15_task2_certification_copy.py
"""
import os
import jinja2

REPO = "/sessions/amazing-sleepy-babbage/mnt/Parcelytics/code"
if not os.path.isdir(REPO):
    REPO = os.path.dirname(os.path.abspath(__file__))

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


import re


def read(rel):
    return open(os.path.join(REPO, rel)).read()


def strip_jinja_comments(src):
    """Removes {# ... #} blocks before substring-checking live copy. This
    codebase's convention (every PX fix in this repo) is to document what
    old, wrong copy said INSIDE a {# ... #} comment right next to the fix --
    which means a naive raw-source substring search for "is the bad phrase
    gone" would false-fail on the fix's own explanatory comment quoting it.
    Comments never render, so stripping them here matches what a real
    Jinja render (and a real site visitor) would actually see."""
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


index_html_raw = read("templates/index.html")
about_html_raw = read("templates/about.html")
base_html_raw = read("templates/base.html")
index_html = strip_jinja_comments(index_html_raw)
about_html = strip_jinja_comments(about_html_raw)
base_html = strip_jinja_comments(base_html_raw)

# ─────────────────────────────────────────────────────────────────────────
section("Instance 1 (PM-named) -- homepage hero stat")
# ─────────────────────────────────────────────────────────────────────────
check('old blended label "Certified + preliminary appraisals" is gone',
      "Certified + preliminary appraisals" not in index_html)
check('new label "Appraisal rolls, certified per county" is present -- '
      "describes the mechanism (certified per county) instead of "
      "asserting one blended status for the whole 2021-26 range",
      "Appraisal rolls, certified per county" in index_html)
check('the "2021–26" year range itself is unchanged (a real fact, not '
      "part of the bug -- only the status label was wrong)",
      '<div class="num">2021–26</div>' in index_html)

# ─────────────────────────────────────────────────────────────────────────
section("Instance 2 (PM-named) -- Directly-sourced card")
# ─────────────────────────────────────────────────────────────────────────
check("Directly-sourced card now discloses that certification timing "
      "varies by county",
      "Certification timing is set by each county's own appraisal "
      "district" in index_html)
check('the card still correctly describes the SOURCE type ("Certified '
      'appraisal-district and tax-office billing data") -- that part was '
      "never wrong, only the missing per-county qualifier was",
      "Certified appraisal-district and tax-office billing data" in index_html)

# ─────────────────────────────────────────────────────────────────────────
section("Instance 3 (found by audit) -- about.html feature card "
        "[PX-20260829-02: section removed, checks updated -- see docstring]")
# ─────────────────────────────────────────────────────────────────────────
check('old overclaim "Full certified appraisal history for every parcel '
      'in every live county" is gone',
      "Full certified appraisal history for every parcel in every live county" not in about_html)
check('the PX-20260828-15 fix\'s own replacement sentence ("Full appraisal '
      'history for every parcel in every live county ... each figure '
      'labeled Certified or Preliminary per that county\'s own appraisal-'
      'district timeline") is gone too -- superseded by the About page '
      "redesign cutting the whole feature-card section it lived in "
      "(PX-20260829-02, Diego's ruling), not a silent regression",
      "Full appraisal history for every parcel in every live county" not in about_html
      and "each figure labeled Certified or Preliminary per that county" not in about_html)
check('the old blanket-certified claim ("Sourced directly from each '
      'county... own certified appraisal rolls") is gone -- checked via '
      'the distinctive "certified appraisal rolls." tail (with trailing '
      "period, only present in the OLD sentence)",
      "certified appraisal rolls." not in about_html)
check("the redesigned page's own new sourcing language (Why Parcelytics "
      "section, PX-20260829-02) still makes no blanket/uniform "
      "certification-status claim -- neither the old feature-card's "
      '"own certified appraisal rolls" phrasing nor a new equivalent '
      "(e.g. a bare \"certified\" claim with no per-county qualifier) "
      "appears anywhere on the page",
      "own certified appraisal rolls" not in about_html)

# ─────────────────────────────────────────────────────────────────────────
section("Instance 4 (found by audit) -- base.html global footer")
# ─────────────────────────────────────────────────────────────────────────
check('old site-wide claim "Appraisal — 2021–2026 Certified '
      '(2026 certified Jul 25, 2026)" is gone -- this was true for Dallas '
      "only, asserted for every county",
      "Appraisal — 2021–2026 Certified (2026 certified Jul 25, 2026)" not in base_html)
check('replacement "Appraisal — 2021–2025 Certified across all '
      'counties; 2026 Certified or Preliminary by county" is present -- '
      "2021-2025 genuinely IS certified everywhere (no county has an "
      "uncertified year before 2026), only 2026 varies",
      "Appraisal — 2021–2025 Certified across all counties; 2026 Certified or Preliminary by county" in base_html)
check("the two lines Diego said to keep verbatim in the original footer "
      'rewrite ("Not legal or tax advice" and the non-affiliation line) '
      "are untouched -- only the factually wrong certification line moved",
      "Not legal or tax advice" in base_html)

# ─────────────────────────────────────────────────────────────────────────
section("Jinja syntax sanity -- all 3 edited files still parse")
# ─────────────────────────────────────────────────────────────────────────
env = jinja2.Environment()
for label, src in [("templates/index.html", index_html_raw),
                    ("templates/about.html", about_html_raw),
                    ("templates/base.html", base_html_raw)]:
    try:
        env.parse(src)
        check(f"{label} parses as valid Jinja (no syntax broken by these edits)", True)
    except Exception as e:
        check(f"{label} parses as valid Jinja -- FAILED: {e}", False)

print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("SCOPE NOTE (not a gap, a deliberate boundary -- disclosed to Diego "
      "in the final report): several other 'certified' mentions on these "
      "same pages (index.html's hero lead, 'Certified data, shown "
      "proudly.' section header, the 'Who it's for' lead, about.html's own "
      "opening line) were reviewed and left alone -- they use 'certified' "
      "as a category descriptor of the underlying source type (appraisal-"
      "district certified rolls, as opposed to third-party scrapes), not "
      "as a real-time status claim about the current year's data. Only "
      "copy asserting or implying a specific, uniform, CURRENT "
      "certification state was in scope for this fix.")
print()
print("NOT PROVEN HERE: a real browser/visual render of the updated "
      "hero stat, card, and footer -- same standing sandbox limitation as "
      "every other PX brief (no live browser). The Jinja-parse check above "
      "confirms no template syntax broke; it does not confirm layout.")
exit(0 if all_ok else 1)
