#!/usr/bin/env python3
"""
test_johnny_feedback_1.py — Task JOHNNY-FEEDBACK-1. Fixture tests for the two
new external-tool rows added to app.py's build_document_sources(): the TCAD
Interactive Map link and the Travis County Clerk deed-history search link.

Same constraint as test_snapshot_correctness_1.py: app.py imports psycopg2 at
module level, which is not installed in this sandbox, so app.py cannot be
imported for a real behavioral test. This reuses the same established
text-inspection technique (_extract_function_body(), copied verbatim from
verify_parcel_filters_coverage.py / test_snapshot_correctness_1.py) to assert
on the real, live source of build_document_sources() instead.

What this proves:
  1. Both new URLs are present verbatim in the real app.py source, inside
     build_document_sources(), and are unconditional (top-level 4-space
     indent, not nested under an `if`) -- both are general county tools
     that apply to every parcel page, not per-parcel conditional data.
  2. Neither new row claims a confidence badge (badge/badge_label are None)
     -- these are external tools, not data this page's own numbers came
     from, so they must not look like a "verified"/"partial"/etc. data
     source in the rendered table.
  3. The CAD map row's copy does NOT claim a direct per-parcel link --
     honesty requirement from the brief, since the map has no
     deep-linking capability (confirmed live, see this task's report).
  4. The deed-history row's copy does NOT claim a working prefill/direct
     lookup -- the brief's fallback case, since a live prefill mechanism
     could not be confirmed working in this session (see report).
  5. A deliberate-corruption case proves this check would actually FAIL if
     a future edit re-added an overclaiming badge or dropped a link.

NOT proven here, and cannot be from this sandbox (no live DB, confirmed):
that the rendered page actually looks right end-to-end. Diego's own live
browser check (already planned per the brief) is required for that -- see
this task's final report.

Run: python3 test_johnny_feedback_1.py
"""
import os
import re
import sys

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


def _extract_function_body(source, func_name):
    """Same technique as verify_parcel_filters_coverage.py's own helper of
    the same name: text from `def func_name(` up to (not including) the
    next top-level `def ` or `@app.route` line."""
    m = re.search(rf"\ndef {re.escape(func_name)}\(", source)
    if not m:
        return None
    start = m.start()
    rest = source[start + 1:]
    end_m = re.search(r"\n(def |@app\.route)", rest)
    end = start + 1 + end_m.start() if end_m else len(source)
    return source[start:end]


REPO_ROOT = os.path.dirname(__file__)


def _read_real_app_py():
    # This test file is run from the outputs scratch dir during drafting,
    # but the real app.py lives in the parcel_app repo -- resolve relative
    # to whichever directory this file is actually placed in when copied
    # into the repo (matching test_snapshot_correctness_1.py's own
    # REPO_ROOT convention, which assumes co-location with app.py).
    path = os.path.join(REPO_ROOT, "app.py")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


CAD_MAP_URL = "https://travis.prodigycad.com/maps"
DEED_PORTAL_URL = "https://travis.tx.publicsearch.us"


def test_both_new_urls_present_in_build_document_sources():
    source = _read_real_app_py()
    body = _extract_function_body(source, "build_document_sources")
    check("build_document_sources() found in app.py", body is not None)
    if body is None:
        return
    check("CAD map URL present verbatim", CAD_MAP_URL in body, CAD_MAP_URL)
    check("Deed portal URL present verbatim", DEED_PORTAL_URL in body, DEED_PORTAL_URL)


def test_both_new_rows_are_unconditional_top_level_appends():
    """Both rows must sit at the function's top 4-space indent level (not
    nested under an `if`/`elif`), since both are general county tools that
    apply to every parcel page -- not conditional per-parcel data."""
    source = _read_real_app_py()
    body = _extract_function_body(source, "build_document_sources")
    check("build_document_sources() found in app.py", body is not None)
    if body is None:
        return
    for url, label in ((CAD_MAP_URL, "CAD map"), (DEED_PORTAL_URL, "Deed portal")):
        # Find the "link": "<url>" line and walk backward to its
        # sources.append({ opener, then check that opener's indent is
        # exactly 4 spaces (top-level statement, not nested under an if).
        link_m = re.search(re.escape(f'"link": "{url}"'), body)
        check(f"{label} row's link line found", link_m is not None)
        if link_m is None:
            continue
        preceding = body[:link_m.start()]
        append_m = list(re.finditer(r"\n( *)sources\.append\(\{", preceding))
        check(f"{label} row: an enclosing sources.append({{ found", len(append_m) > 0)
        if not append_m:
            continue
        indent = append_m[-1].group(1)
        check(f"{label} row's sources.append is at top-level 4-space indent (unconditional)",
              indent == "    ", repr(indent))


def test_neither_new_row_claims_a_confidence_badge():
    source = _read_real_app_py()
    body = _extract_function_body(source, "build_document_sources")
    check("build_document_sources() found in app.py", body is not None)
    if body is None:
        return
    for url, label in ((CAD_MAP_URL, "CAD map"), (DEED_PORTAL_URL, "Deed portal")):
        # Grab the ~10-line window around the link line and confirm badge
        # is explicitly None within that same dict literal.
        link_m = re.search(re.escape(f'"link": "{url}"'), body)
        check(f"{label} row's link line found", link_m is not None)
        if link_m is None:
            continue
        window = body[max(0, link_m.start() - 400):link_m.start() + 200]
        check(f"{label} row: badge is None (no overclaimed confidence tier)",
              '"badge": None' in window, window)
        check(f"{label} row: badge_label is None",
              '"badge_label": None' in window, window)


def test_cad_map_copy_does_not_claim_direct_parcel_link():
    source = _read_real_app_py()
    body = _extract_function_body(source, "build_document_sources")
    check("build_document_sources() found in app.py", body is not None)
    if body is None:
        return
    link_m = re.search(re.escape(f'"link": "{CAD_MAP_URL}"'), body)
    check("CAD map row's link line found", link_m is not None)
    if link_m is None:
        return
    window = body[max(0, link_m.start() - 400):link_m.start() + 200]
    check("CAD map row's coverage text honestly says it does NOT deep-link to this parcel",
          "not this specific parcel" in window or "no way to link directly" in window, window)


def test_deed_portal_copy_does_not_claim_working_prefill():
    source = _read_real_app_py()
    body = _extract_function_body(source, "build_document_sources")
    check("build_document_sources() found in app.py", body is not None)
    if body is None:
        return
    link_m = re.search(re.escape(f'"link": "{DEED_PORTAL_URL}"'), body)
    check("Deed portal row's link line found", link_m is not None)
    if link_m is None:
        return
    window = body[max(0, link_m.start() - 400):link_m.start() + 200]
    check("Deed portal row's coverage text honestly says it's a general search tool "
          "(not a direct/prefilled lookup)",
          "General search tool" in window, window)
    check("Deed portal row's coverage text does not claim an automatic/prefilled result",
          "auto" not in window.lower() and "prefill" not in window.lower(), window)


# ── deliberate-corruption cases, same style as test_snapshot_correctness_1.py
_FIXTURE_CLEAN = '''
def build_document_sources(parcel, history, current, entity_detail, delinquent):
    sources = []
    sources.append({
        "name": "Open County Interactive Map",
        "provides": "TCAD's own interactive parcel map",
        "coverage": "Opens the county's map tool itself, not this specific parcel — no way to link directly to one parcel",
        "badge": None, "badge_label": None,
        "link": "https://travis.prodigycad.com/maps",
        "link_label": "Travis Central Appraisal District — Interactive Map",
    })
    sources.append({
        "name": "Search Deed History (Travis County Clerk)",
        "provides": "Official recorded documents search",
        "coverage": "General search tool, not indexed by parcel",
        "badge": None, "badge_label": None,
        "link": "https://travis.tx.publicsearch.us",
        "link_label": "Travis County Clerk — Official Records Search",
    })
    return sources

@app.route("/about")
def about():
    pass
'''

_FIXTURE_CORRUPT_OVERCLAIMED_BADGE = '''
def build_document_sources(parcel, history, current, entity_detail, delinquent):
    sources = []
    sources.append({
        "name": "Open County Interactive Map",
        "provides": "TCAD's own interactive parcel map",
        "coverage": "Opens the county's map tool itself, not this specific parcel — no way to link directly to one parcel",
        "badge": "verified", "badge_label": "Verified",
        "link": "https://travis.prodigycad.com/maps",
        "link_label": "Travis Central Appraisal District — Interactive Map",
    })
    return sources

@app.route("/about")
def about():
    pass
'''


def test_corruption_case_overclaimed_badge_is_caught():
    body = _extract_function_body(_FIXTURE_CORRUPT_OVERCLAIMED_BADGE, "build_document_sources")
    link_m = re.search(re.escape(f'"link": "{CAD_MAP_URL}"'), body)
    window = body[max(0, link_m.start() - 400):link_m.start() + 200]
    check("CORRUPTION CASE (overclaimed badge): correctly detected as NOT badge:None",
          '"badge": None' not in window, window)


def test_clean_fixture_is_correctly_recognized_as_fixed():
    body = _extract_function_body(_FIXTURE_CLEAN, "build_document_sources")
    check("CLEAN fixture: both URLs present",
          CAD_MAP_URL in body and DEED_PORTAL_URL in body)
    for url in (CAD_MAP_URL, DEED_PORTAL_URL):
        link_m = re.search(re.escape(f'"link": "{url}"'), body)
        window = body[max(0, link_m.start() - 400):link_m.start() + 200]
        check(f"CLEAN fixture: {url} row has badge:None",
              '"badge": None' in window, window)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL JOHNNY_FEEDBACK_1 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
