#!/usr/bin/env python3
"""
test_snapshot_correctness_1.py — Task SNAPSHOT-CORRECTNESS-1. Fixture tests
for the L-class (Business Personal Property) exclusion fix applied to
app.py's _compute_snapshot_data().

REAL, HONEST GAP DISCLOSED FIRST (per this task's brief, item 3): before
this task, _compute_snapshot_data() had ZERO fixture/behavioral test
coverage anywhere in this codebase. test_verify_parcel_filters_coverage.py
references a function named `_compute_snapshot_data` only inside its own
SYNTHETIC fixture source strings (_FIXTURE_APP_PY_CLEAN /
_FIXTURE_APP_PY_CORRUPT_1) -- those exist to test whether
verify_parcel_filters_coverage.py's STATIC CHECKER correctly detects
canonical-vs-retyped exclusion fragments in an app.py-shaped file; they do
not execute app.py's real _compute_snapshot_data() or assert anything
about its actual query output. No test anywhere runs this function against
real or synthetic parcel data.

Why a full behavioral harness isn't built here either: _compute_snapshot_data()
calls query()/query_no_nestloop() directly against a real Postgres
connection with no dependency-injection seam, across 5 different SQL query
bodies -- faking that realistically (grouped rows, GROUPING SETS totals,
etc.) for one exclusion-condition check would be a large new test-harness
investment, which this task's own brief says not to take on ("a minimal
test... if feasible without a large new test-harness investment").

What this file does instead, reusing this codebase's own established
pattern for testing app.py-level SQL wiring without importing app.py at
all (app.py imports psycopg2 at module level, and psycopg2 is not
installed in this sandbox -- confirmed via `import psycopg2` raising
ModuleNotFoundError, the same constraint noted throughout this week's other
loader/test files): read app.py as plain TEXT and inspect the real source
around canonical_excl's assignment and its 5 query-site references, the
exact technique verify_parcel_filters_coverage.py's own
_extract_function_body() already uses and already trusts. This proves:

  1. canonical_excl's assignment in the real, live app.py source now
     includes the L-exclusion helper (not a hand-wavy "should be there").
  2. All 5 query sites inside _compute_snapshot_data() that this task's
     investigation found (breakdown, both _single_year_mv_totals() calls,
     the Part 4 aggregate, the status_2026/cert_agg query, and the
     neighborhoods query) still reference the SAME canonical_excl variable
     -- so the one-point fix genuinely reaches all of them, not just the
     ones a human skimmed.
  3. A deliberate-corruption case (same style as
     test_verify_parcel_filters_coverage.py) proves this check would
     actually FAIL if a future edit narrowed the fix back down or dropped
     a query site's reference -- not just that it passes today.
  4. parcel_filters.exclude_non_real_property_gap_sql() -- imported and
     genuinely callable, producing the exact fragment this fix relies on.

NOT proven here, and cannot be from this sandbox (no live DB, confirmed):
that the real SQL actually executes correctly against Postgres, or that
the live county-wide total actually drops by the measured $9,969,617,448.
Diego's own live re-run is required for both -- see this task's final
report for exact before/after numbers to check.

Run: python3 test_snapshot_correctness_1.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from parcel_filters import exclude_non_real_property_gap_sql, NON_REAL_PROPERTY_GAP_CLASSES

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
    path = os.path.join(REPO_ROOT, "app.py")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ── 1-2: real app.py source checks ───────────────────────────────────────
def test_canonical_excl_assignment_includes_l_exclusion():
    source = _read_real_app_py()
    body = _extract_function_body(source, "_compute_snapshot_data")
    check("_compute_snapshot_data() found in app.py", body is not None)
    if body is None:
        return

    m = re.search(r"canonical_excl\s*=\s*(.+)", body)
    check("canonical_excl assignment line found", m is not None)
    if m is None:
        return
    assignment_line = m.group(1)

    check("canonical_excl assignment still references CANONICAL_PARCEL_EXCL",
          "CANONICAL_PARCEL_EXCL" in assignment_line, assignment_line)
    check("canonical_excl assignment now also calls exclude_non_real_property_gap_sql()",
          "exclude_non_real_property_gap_sql" in assignment_line, assignment_line)
    check("canonical_excl assignment passes p.state_cd1 (the parcel-table alias used throughout this function)",
          "'p.state_cd1'" in assignment_line or '"p.state_cd1"' in assignment_line, assignment_line)


def test_all_five_known_query_sites_still_reference_canonical_excl():
    """
    The 5 distinct query bodies this task's investigation confirmed exist
    inside _compute_snapshot_data() (not the 3 the function's own stale
    comment names -- see this task's report): the breakdown query,
    _single_year_mv_totals() (one query body, called twice), the Part 4
    aggregate, the status_2026/cert_agg query, and the neighborhoods query.
    All must still say "{canonical_excl}" verbatim -- if any query site
    stopped referencing the variable (e.g. someone re-typed
    CANONICAL_PARCEL_EXCL directly into one query later), this fix would
    silently stop covering that one query, and this test would catch it.
    """
    source = _read_real_app_py()
    body = _extract_function_body(source, "_compute_snapshot_data")
    check("_compute_snapshot_data() found in app.py", body is not None)
    if body is None:
        return

    occurrences = body.count("{canonical_excl}")
    check("canonical_excl is referenced at least 5 times in _compute_snapshot_data() "
          "(breakdown, 1x _single_year_mv_totals body, Part 4 aggregate, "
          "status_2026/cert_agg, neighborhoods)",
          occurrences >= 5, f"found {occurrences} occurrences")


# ── 3: deliberate-corruption case, same style as
# test_verify_parcel_filters_coverage.py's own corruption tests ──────────
_FIXTURE_FUNC_CLEAN = '''
def _compute_snapshot_data(view):
    canonical_excl = CANONICAL_PARCEL_EXCL + f" AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"
    breakdown = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl} {view_where}")
    agg = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl} {view_where}")
    nb_rows = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl}")

@app.route("/snapshot/neighborhood/<code>")
def snapshot_neighborhood(code):
    pass
'''

_FIXTURE_FUNC_CORRUPT_MISSING_L_EXCLUSION = '''
def _compute_snapshot_data(view):
    canonical_excl = CANONICAL_PARCEL_EXCL
    breakdown = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl} {view_where}")
    agg = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl} {view_where}")
    nb_rows = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl}")

@app.route("/snapshot/neighborhood/<code>")
def snapshot_neighborhood(code):
    pass
'''

_FIXTURE_FUNC_CORRUPT_DROPPED_QUERY_SITE = '''
def _compute_snapshot_data(view):
    canonical_excl = CANONICAL_PARCEL_EXCL + f" AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"
    breakdown = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl} {view_where}")
    agg = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {CANONICAL_PARCEL_EXCL} {view_where}")
    nb_rows = query_no_nestloop(f"SELECT 1 FROM parcel p WHERE 1=1 {canonical_excl}")

@app.route("/snapshot/neighborhood/<code>")
def snapshot_neighborhood(code):
    pass
'''


def test_corruption_case_missing_l_exclusion_is_caught():
    body = _extract_function_body(_FIXTURE_FUNC_CORRUPT_MISSING_L_EXCLUSION, "_compute_snapshot_data")
    m = re.search(r"canonical_excl\s*=\s*(.+)", body)
    has_l_exclusion = m is not None and "exclude_non_real_property_gap_sql" in m.group(1)
    check("CORRUPTION CASE (missing L-exclusion): correctly detected as NOT fixed",
          has_l_exclusion is False, m.group(1) if m else None)


def test_clean_fixture_is_correctly_recognized_as_fixed():
    body = _extract_function_body(_FIXTURE_FUNC_CLEAN, "_compute_snapshot_data")
    m = re.search(r"canonical_excl\s*=\s*(.+)", body)
    has_l_exclusion = m is not None and "exclude_non_real_property_gap_sql" in m.group(1)
    check("CLEAN fixture: correctly recognized as fixed",
          has_l_exclusion is True, m.group(1) if m else None)
    check("CLEAN fixture: all 3 query sites in this small fixture reference canonical_excl",
          body.count("{canonical_excl}") == 3, body.count("{canonical_excl}"))


def test_corruption_case_dropped_query_site_is_caught():
    body = _extract_function_body(_FIXTURE_FUNC_CORRUPT_DROPPED_QUERY_SITE, "_compute_snapshot_data")
    # This fixture's canonical_excl assignment IS fixed, but one query site
    # (agg) was quietly reverted to CANONICAL_PARCEL_EXCL directly -- the
    # per-site occurrence count must reflect that, not just the assignment
    # line looking correct.
    check("CORRUPTION CASE (dropped query site): only 2 of 3 sites still reference canonical_excl",
          body.count("{canonical_excl}") == 2, body.count("{canonical_excl}"))


# ── 4: the helper itself, as used by this specific call site ─────────────
def test_exclude_non_real_property_gap_sql_produces_expected_fragment():
    frag = exclude_non_real_property_gap_sql("p.state_cd1")
    check("fragment excludes 'L'", "'L'" in frag, frag)
    check("fragment is NULL-safe (COALESCE)", "COALESCE(p.state_cd1" in frag, frag)
    check("fragment uses the same LEFT(UPPER(...),1) convention as the grain-key helpers",
          frag.startswith("LEFT(UPPER("), frag)
    check("NON_REAL_PROPERTY_GAP_CLASSES currently contains exactly ('L',) "
          "(if this ever grows, this test's expected substrings above should be revisited)",
          NON_REAL_PROPERTY_GAP_CLASSES == ("L",), NON_REAL_PROPERTY_GAP_CLASSES)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL SNAPSHOT_CORRECTNESS_1 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
