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

── UPDATE, Task AGGPRECOMP-2 (Aug 2026) ─────────────────────────────────────
_compute_snapshot_data() (app.py) no longer runs live queries at all -- it
was rewired to read the 5 precomputed summary values (breakdown, both
single-year MV totals, the Part 4 aggregate, cert_agg, neighborhoods) out of
snapshot_breakdown/snapshot_totals/snapshot_neighborhood_movers, which
loaders/refresh_snapshot_summary.py now computes ONCE per data load. The
canonical_excl assignment and its 5 query-site references this test
originally checked for INSIDE _compute_snapshot_data() genuinely moved,
in full, to that refresh script (module-level CANONICAL_EXCL constant,
referenced by all 5 of its SQL-builder functions: breakdown_sql,
single_year_mv_sql, part4_agg_sql, cert_agg_sql, neighborhoods_sql) -- this
is the correct, intended effect of that migration (per
SPEC_AGGREGATE_PRECOMPUTATION.md's own "aggregation logic lives only inside
refresh functions" principle), not a regression of this fix. The two real-
source checks below (test_canonical_excl_assignment_includes_l_exclusion,
test_all_five_known_query_sites_still_reference_canonical_excl) are updated
to check loaders/refresh_snapshot_summary.py instead of app.py -- the actual
invariant these tests protect ("the L-exclusion fix reaches every real query
site") is still real and still worth guarding, it just needs to watch the
new location. See loaders/test_refresh_snapshot_summary.py for that
migration's own, more complete fixture-test coverage (all 11 real views, not
just a source-grep count).

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


def _read_real_refresh_snapshot_summary_py():
    path = os.path.join(REPO_ROOT, "loaders", "refresh_snapshot_summary.py")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ── 1-2: real source checks -- AGGPRECOMP-2 (Aug 2026) moved these from
# app.py's _compute_snapshot_data() (which no longer runs any live query at
# all) to loaders/refresh_snapshot_summary.py, where the same 5 query bodies
# now actually live. See this file's module docstring "UPDATE" section. ────
def test_canonical_excl_assignment_includes_l_exclusion():
    source = _read_real_refresh_snapshot_summary_py()

    m = re.search(r"^CANONICAL_EXCL\s*=\s*(.+)$", source, re.MULTILINE)
    check("CANONICAL_EXCL assignment line found in loaders/refresh_snapshot_summary.py",
          m is not None)
    if m is None:
        return
    assignment_line = m.group(1)

    check("CANONICAL_EXCL assignment still references CANONICAL_PARCEL_EXCL",
          "CANONICAL_PARCEL_EXCL" in assignment_line, assignment_line)
    check("CANONICAL_EXCL assignment now also calls exclude_non_real_property_gap_sql()",
          "exclude_non_real_property_gap_sql" in assignment_line, assignment_line)
    check("CANONICAL_EXCL assignment passes p.state_cd1 (the parcel-table alias used throughout these queries)",
          "'p.state_cd1'" in assignment_line or '"p.state_cd1"' in assignment_line, assignment_line)

    # _compute_snapshot_data() itself (app.py) must NOT have re-derived its
    # own copy of this exclusion logic -- it should read precomputed values
    # instead of assembling any canonical_excl-shaped WHERE fragment at all.
    app_source = _read_real_app_py()
    body = _extract_function_body(app_source, "_compute_snapshot_data")
    check("_compute_snapshot_data() found in app.py", body is not None)
    if body is not None:
        check("_compute_snapshot_data() no longer assembles its own canonical_excl "
              "(the exclusion logic lives only in the refresh script now, not duplicated here)",
              "canonical_excl" not in body, body)


def test_all_five_known_query_sites_still_reference_canonical_excl():
    """
    The 5 distinct query-builder functions loaders/refresh_snapshot_summary.py
    now runs ONCE per data load (breakdown_sql, single_year_mv_sql -- one
    query shape, called twice for 2025/2026, part4_agg_sql, cert_agg_sql,
    neighborhoods_sql) must all still reference "{CANONICAL_EXCL}" verbatim
    -- if any query builder stopped referencing the constant (e.g. someone
    re-typed CANONICAL_PARCEL_EXCL directly into one function later), this
    fix would silently stop covering that one query, and this test would
    catch it. Mirrors the original test's intent (all 5 real query sites
    covered, not just the assignment looking correct), updated for where
    those 5 sites actually live post-AGGPRECOMP-2.
    """
    source = _read_real_refresh_snapshot_summary_py()

    occurrences = source.count("{CANONICAL_EXCL}")
    check("CANONICAL_EXCL is referenced at least 5 times in loaders/refresh_snapshot_summary.py "
          "(breakdown_sql, single_year_mv_sql, part4_agg_sql, cert_agg_sql, neighborhoods_sql)",
          occurrences >= 5, f"found {occurrences} occurrences")

    for fn_name in ("breakdown_sql", "single_year_mv_sql", "part4_agg_sql",
                    "cert_agg_sql", "neighborhoods_sql"):
        body = _extract_function_body(source, fn_name)
        check(f"{fn_name}() found in loaders/refresh_snapshot_summary.py", body is not None)
        if body is not None:
            check(f"{fn_name}() references {{CANONICAL_EXCL}}", "{CANONICAL_EXCL}" in body, body)


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
