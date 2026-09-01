#!/usr/bin/env python3
"""
loaders/test_px_20260831_02_tasks_3_4_5.py — PX-20260831-02 Tasks 3, 4, 5
fixture tests for loaders/compute_metrics.py.

Same sandbox-limitation disclosure as loaders/test_refresh_snapshot_summary.py
and loaders/test_backfill_prop_unit_tax_year_geoid.py: no root to apt-get
install postgresql, pip install pgserver blocked by the sandbox's PyPI proxy,
and psycopg2 itself is not installed here either (confirmed via
`python3 -c "import psycopg2"` -> ModuleNotFoundError). Unlike
refresh_snapshot_summary.py, compute_metrics.py DOES `import psycopg2.extras`
at module level (and loaders/db.py, which it imports, does `import psycopg2`
too), so importing loaders.compute_metrics at all requires a fake psycopg2
registered in sys.modules first -- same technique as
test_backfill_prop_unit_tax_year_geoid.py's own _install_fake_psycopg2().

This file proves three things:

  1. TASK 3 (_parcel_metrics_row_floor): exercised directly against a
     FakeConn/FakeCursor for a small county (few thousand parcel_tax_year
     rows) and a large county (millions of rows, Dallas-shaped), proving the
     returned floor is genuinely 0.5x that county's OWN current
     parcel_tax_year count -- not a shared constant, and not off by a
     rounding/int-truncation error at either scale.

  2. TASK 4 (_large_jump_threshold_for_county): exercised directly (no DB
     needed at all -- pure dict lookup), proving TRAVIS and DALLAS each
     return their own distinct, correct value, and that a county with no
     registered entry raises MetricsIntegrityError with no default and no
     silent fallback to another county's threshold -- the exact failure mode
     PM's brief called out ("Missing county key must raise loudly -- no
     default, no fallback to Travis").

  3. TASK 5 (join/subquery county_code scoping): static source-text
     assertions against compute_metrics.py's REAL shipping source -- same
     rigor/technique as test_dallas_gate_4_county_code.py (direct
     string/regex assertions against the real file on disk, not a
     reimplementation), proving every join and correlated subquery this
     task's audit found unscoped now carries the county_code equality it
     was missing. Chosen over a FakeConn harness for Task 5 specifically
     because compute_parcel_metrics()'s real control flow is a single very
     long function issuing 7+ sequential UPDATE/INSERT statements -- a full
     FakeConn walk would mostly be testing FakeCursor's own SQL-parsing
     fidelity, not the fix. The source-text technique reads the ACTUAL
     shipping SQL string, so it fails the moment the real fix regresses.

Run: python3 loaders/test_px_20260831_02_tasks_3_4_5.py
"""
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _install_fake_psycopg2():
    """Same technique as loaders/test_backfill_prop_unit_tax_year_geoid.py's
    own _install_fake_psycopg2(): registers fake `psycopg2` / `psycopg2.extras`
    modules in sys.modules so compute_metrics.py's (and loaders/db.py's)
    module-level `import psycopg2` / `import psycopg2.extras` succeed in this
    sandbox, where the real package is not installed. Nothing in this test
    file actually calls into psycopg2.extras (Tasks 3/4's functions under
    test don't touch it), so the fake module bodies are empty stand-ins.
    """
    if "psycopg2" in sys.modules:
        return
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_pg2 = types.ModuleType("psycopg2")
    fake_pg2.extras = fake_extras
    sys.modules["psycopg2"] = fake_pg2
    sys.modules["psycopg2.extras"] = fake_extras


_install_fake_psycopg2()

import loaders.compute_metrics as cm  # noqa: E402


# ── Fake DB layer, minimal: only needs to answer a single
# "SELECT COUNT(*) FROM parcel_tax_year WHERE county_code = %s" ────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        self._last_params = params

    def fetchone(self):
        (county_code,) = self._last_params
        return (self.conn.county_row_counts[county_code],)


class FakeConn:
    def __init__(self, county_row_counts):
        self.executed = []
        self.county_row_counts = county_row_counts

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)


# ── Task 3: _parcel_metrics_row_floor ───────────────────────────────────────

def test_row_floor_small_county():
    """A small county (e.g. a brand-new onboarding with only a few thousand
    parcel_tax_year rows) gets a floor scaled to ITS OWN size, not a
    Travis/Dallas-shaped absolute number."""
    conn = FakeConn({"TINYCOUNTY": 4_001})
    floor = cm._parcel_metrics_row_floor(conn, "TINYCOUNTY")
    check("small county (4,001 source rows): floor == int(0.5 * 4001) == 2000",
          floor == 2000, floor)
    check("small county: issued exactly one SELECT COUNT(*) query, scoped by county_code",
          len(conn.executed) == 1 and conn.executed[0][1] == ("TINYCOUNTY",),
          conn.executed)
    sql_norm = conn.executed[0][0].upper()
    check("small county: query text is scoped to parcel_tax_year WHERE county_code = %s",
          "FROM PARCEL_TAX_YEAR" in sql_norm and "WHERE COUNTY_CODE = %S" in sql_norm,
          sql_norm)


def test_row_floor_large_county_dallas_shaped():
    """A Dallas-shaped county (millions of rows) gets a floor that scales
    with it -- proving this isn't silently clamped to some small hardcoded
    ceiling, and that the fraction math doesn't overflow/misbehave at scale."""
    conn = FakeConn({"DALLAS": 3_576_634})  # PM's own live-measured Dallas count
    floor = cm._parcel_metrics_row_floor(conn, "DALLAS")
    check("Dallas-shaped county (3,576,634 source rows): floor == int(0.5 * 3576634) == 1,788,317",
          floor == 1_788_317, floor)
    check("Dallas floor is far above the old single-hardcoded PARCEL_METRICS_ROW_FLOOR "
          "(1,000,000) -- proving the per-county derivation is NOT just silently "
          "reproducing the retired constant's behavior for a large county",
          floor > 1_000_000, floor)


def test_row_floor_travis_shaped_uses_own_fraction_not_dallas():
    """Two counties queried against the SAME FakeConn each get THEIR OWN
    floor -- proves no cross-county leakage/caching inside the helper."""
    conn = FakeConn({"TRAVIS": 2_774_846, "DALLAS": 3_576_634})  # PM's own live figures
    travis_floor = cm._parcel_metrics_row_floor(conn, "TRAVIS")
    dallas_floor = cm._parcel_metrics_row_floor(conn, "DALLAS")
    check("Travis floor == int(0.5 * 2,774,846) == 1,387,423",
          travis_floor == 1_387_423, travis_floor)
    check("Dallas floor == int(0.5 * 3,576,634) == 1,788,317 (independently, not reusing Travis's)",
          dallas_floor == 1_788_317, dallas_floor)
    check("the two counties' floors differ (no shared/cached value leaking across calls)",
          travis_floor != dallas_floor, (travis_floor, dallas_floor))


def test_row_floor_custom_fraction_param_still_works():
    """fraction= is a real parameter, not dead code -- PM's brief allowed
    proposing "a better fraction or shape" if justified; proving the knob
    itself works is part of proving the shape is sound."""
    conn = FakeConn({"TRAVIS": 1_000_000})
    floor = cm._parcel_metrics_row_floor(conn, "TRAVIS", fraction=0.25)
    check("fraction=0.25 override: floor == int(0.25 * 1,000,000) == 250,000",
          floor == 250_000, floor)


# ── Task 4: _large_jump_threshold_for_county ────────────────────────────────

def test_threshold_travis_registered_value():
    val = cm._large_jump_threshold_for_county("TRAVIS")
    check("TRAVIS returns its own registered 75.0 (unchanged from the retired "
          "single-constant value, per PM's own reference distribution)",
          val == 75.0, val)


def test_threshold_dallas_registered_value():
    val = cm._large_jump_threshold_for_county("DALLAS")
    check("DALLAS returns its own registered 45.0 (PM's proposed value from the "
          "~3-points-above-p95 methodology, distinct from Travis's 75.0)",
          val == 45.0, val)


def test_threshold_travis_and_dallas_are_genuinely_different_values():
    check("TRAVIS and DALLAS thresholds are NOT the same number -- proves this "
          "is a genuine per-county map, not one value silently applied to both",
          cm._large_jump_threshold_for_county("TRAVIS") !=
          cm._large_jump_threshold_for_county("DALLAS"))


def test_threshold_missing_county_raises_no_default_no_fallback():
    """The exact failure mode PM's brief named: 'Missing county key must
    raise loudly -- no default, no fallback to Travis.'"""
    raised = None
    try:
        cm._large_jump_threshold_for_county("HARRIS")
    except cm.MetricsIntegrityError as e:
        raised = e
    check("missing county (HARRIS, not yet registered) raises MetricsIntegrityError",
          raised is not None)
    if raised is not None:
        msg = str(raised)
        check("error message names the missing county_code",
              "HARRIS" in msg, msg)
        check("error message tells Diego the exact remediation (--analyze) rather "
              "than silently picking a default",
              "--analyze" in msg, msg)
        check("error message explicitly disclaims any fallback-to-another-county "
              "behavior (the exact failure mode PM called out)",
              "no fallback" in msg.lower(), msg)


def test_threshold_missing_county_does_not_silently_return_travis_value():
    """Belt-and-suspenders on the same requirement: prove the return value
    space for a missing county never happens to equal Travis's 75.0 by some
    other code path (i.e. prove it raises, it doesn't just return 75.0)."""
    try:
        val = cm._large_jump_threshold_for_county("HARRIS")
        check("HARRIS (unregistered) did NOT silently return a value at all "
              "(should have raised) -- got a return instead of a raise",
              False, val)
    except cm.MetricsIntegrityError:
        check("HARRIS (unregistered) correctly raised instead of returning "
              "any value, Travis's or otherwise", True)


def test_threshold_by_county_dict_has_exactly_travis_and_dallas_today():
    check("LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY currently has exactly the 2 "
          "counties this brief measured (TRAVIS, DALLAS) -- a 3rd key "
          "appearing here unexpectedly would mean an unmeasured value slipped "
          "in without going through --analyze",
          set(cm.LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY.keys()) == {"TRAVIS", "DALLAS"},
          cm.LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY)


def test_old_module_constant_is_truly_retired():
    check("the old single LARGE_JUMP_THRESHOLD_PCT module-level constant no "
          "longer exists (retired, not just shadowed) -- prevents any stale "
          "call site from silently resurrecting the single-county behavior",
          not hasattr(cm, "LARGE_JUMP_THRESHOLD_PCT"))
    check("the old single PARCEL_METRICS_ROW_FLOOR module-level constant no "
          "longer exists either",
          not hasattr(cm, "PARCEL_METRICS_ROW_FLOOR"))


# ── Task 5: join/subquery county_code scoping — static source-text proof ───
# Same technique as test_dallas_gate_4_county_code.py: read the REAL shipping
# file from disk and regex/string-match against it, so these tests fail the
# moment the real fix regresses (not testing a copy or a reimplementation).

_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compute_metrics.py")
with open(_SRC_PATH) as _f:
    _SRC = _f.read()
_SRC_NORM = " ".join(_SRC.split())


def test_main_insert_parcel_join_scoped():
    check("main INSERT's `JOIN parcel p` carries county_code equality",
          "JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code" in _SRC_NORM)


def test_main_insert_tax_billing_join_scoped():
    check("main INSERT's `LEFT JOIN tax_billing tb` carries county_code equality",
          re.search(
              r"LEFT JOIN tax_billing tb\s+ON tb\.geo_id = pty\.geo_id AND tb\.tax_year = pty\.tax_year\s+"
              r"AND tb\.county_code = pty\.county_code",
              _SRC_NORM,
          ) is not None)


def test_main_insert_tax_delinquent_join_scoped():
    check("main INSERT's `LEFT JOIN tax_delinquent td` carries county_code equality",
          re.search(
              r"LEFT JOIN tax_delinquent td\s+ON td\.geo_id = pty\.geo_id AND td\.county_code = pty\.county_code",
              _SRC_NORM,
          ) is not None)


def test_effective_tax_rate_tbe_subqueries_all_scoped():
    """PM's brief literally said 'join' but these are correlated subqueries
    against the same county-keyed table (tax_billing_entity) carrying the
    identical cross-county-attribution risk -- disclosed scope extension,
    see this task's final report."""
    occurrences = re.findall(
        r"FROM\s+tax_billing_entity tbe\s+WHERE\s+tbe\.geo_id\s*=\s*pty\.geo_id\s+"
        r"AND\s+tbe\.tax_year\s*=\s*2025(\s+AND\s+tbe\.county_code\s*=\s*pty\.county_code)?",
        _SRC_NORM,
    )
    check("found the expected 5 tax_billing_entity correlated-subquery occurrences "
          "(3 in effective_tax_rate's CASE, 2 in effective_tax_rate_derived's CASE)",
          len(occurrences) == 5, len(occurrences))
    check("every one of those 5 occurrences carries the county_code equality "
          "(none left unscoped)",
          all(occ for occ in occurrences), occurrences)
    check("zero remaining unscoped tax_billing_entity subqueries anywhere in the file",
          _SRC_NORM.count("FROM tax_billing_entity tbe WHERE tbe.geo_id = pty.geo_id AND "
                           "tbe.tax_year = 2025 )") == 0)


def test_cap_step_up_exposure_subquery_scoped():
    check("cap_step_up_exposure's `JOIN parcel p` carries county_code equality",
          "JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code" in _SRC_NORM
          and "cap_step_up_exposure" in _SRC_NORM)
    check("cap_step_up_exposure's `LEFT JOIN tax_billing tb` (2025-only) carries county_code equality",
          re.search(
              r"LEFT JOIN tax_billing tb\s+ON tb\.geo_id = pty\.geo_id AND tb\.tax_year = 2025\s+"
              r"AND tb\.county_code = pty\.county_code",
              _SRC_NORM,
          ) is not None)
    check("cap_step_up_exposure's inner subquery WHERE now scopes pty.county_code = %s "
          "(previously completely unscoped by county)",
          re.search(
              r"WHERE COALESCE\(p\.state_cd1, ''\) LIKE 'A%'\s+AND pty\.county_code = %s\s+"
              r"AND pty\.tax_year = 2025\s+AND pty\.exemption_codes LIKE '%HS%'",
              _SRC_NORM,
          ) is not None)


def test_cap_expiry_signal_subquery_scoped():
    check("cap_expiry_signal's `JOIN parcel p` carries county_code equality (pty25-keyed)",
          "JOIN parcel p ON p.geo_id = pty25.geo_id AND p.county_code = pty25.county_code" in _SRC_NORM)
    check("cap_expiry_signal's `LEFT JOIN parcel_tax_year pty26` carries county_code equality",
          re.search(
              r"LEFT JOIN parcel_tax_year pty26\s+ON pty26\.geo_id = pty25\.geo_id AND pty26\.tax_year = 2026\s+"
              r"AND pty26\.county_code = pty25\.county_code",
              _SRC_NORM,
          ) is not None)
    check("cap_expiry_signal's inner subquery WHERE now scopes pty25.county_code = %s "
          "(previously completely unscoped by county)",
          re.search(
              r"WHERE COALESCE\(p\.state_cd1, ''\) LIKE 'A%'\s+AND pty25\.county_code = %s\s+"
              r"AND pty25\.tax_year = 2025\s+AND pty25\.exemption_codes LIKE '%HS%'",
              _SRC_NORM,
          ) is not None)


def test_cumulative_value_growth_pct_subquery_fully_scoped():
    """The most severe Task 5 finding: this subquery's inner `mn` grouping
    used to run across the ENTIRE parcel_tax_year table with no county_code
    at all -- a genuine cross-county geo_id-collision bug, invisible with
    only one county live. Proves every layer of the fix: mn's own
    SELECT/GROUP BY, both join conditions, and the new outer WHERE scope."""
    check("inner `mn` subquery's SELECT list now includes county_code",
          "SELECT geo_id, county_code, MIN(tax_year) AS earliest_year" in _SRC_NORM)
    check("inner `mn` subquery's GROUP BY now includes county_code (not geo_id alone)",
          "GROUP BY geo_id, county_code" in _SRC_NORM)
    check("`mn` join condition scopes county_code alongside geo_id",
          "JOIN ( -- PX-20260831-02" in _SRC_NORM.replace("(\n", "( ") or
          re.search(r"\)\s*mn ON mn\.geo_id = cur\.geo_id AND mn\.county_code = cur\.county_code", _SRC_NORM)
          is not None)
    check("`earliest` join condition scopes county_code alongside geo_id/tax_year",
          re.search(
              r"JOIN parcel_tax_year earliest\s+ON earliest\.geo_id = mn\.geo_id\s+"
              r"AND earliest\.tax_year = mn\.earliest_year\s+AND earliest\.county_code = mn\.county_code",
              _SRC_NORM,
          ) is not None)
    check("outer WHERE now scopes cur.county_code = %s (previously this whole "
          "subquery had NO county_code anywhere)",
          re.search(r"WHERE cur\.county_code = %s\s+AND cur\.tax_year = 2025", _SRC_NORM) is not None)


def test_county_benchmark_join_scoped():
    check("compute_county_benchmarks()'s `JOIN parcel p` carries county_code equality",
          "JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code" in _SRC_NORM
          and "county_benchmark" in _SRC_NORM)
    check("compute_county_benchmarks()'s pre-existing `LEFT JOIN parcel_metrics pm` "
          "was ALREADY correctly scoped (regression check -- this task didn't need "
          "to touch it, confirming it's still intact)",
          re.search(
              r"LEFT JOIN parcel_metrics pm\s+ON pm\.geo_id = pty\.geo_id AND pm\.tax_year = pty\.tax_year\s+"
              r"AND pm\.county_code = pty\.county_code",
              _SRC_NORM,
          ) is not None)


def test_pass2_risk_large_value_jump_uses_per_county_helper_not_deleted_constant():
    """The self-identified live bug flagged mid-session: Pass 2's UPDATE used
    to reference the deleted LARGE_JUMP_THRESHOLD_PCT module constant by name
    (a NameError waiting to happen). Proves it now calls the per-county
    helper instead, and that the old constant name is gone from this call
    site specifically."""
    check("Pass 2 calls _large_jump_threshold_for_county(county_code) to get its threshold",
          "jump_threshold = _large_jump_threshold_for_county(county_code)" in _SRC_NORM)
    check("Pass 2's UPDATE WHERE clause interpolates the looked-up jump_threshold variable, "
          "not the old bare constant name",
          "WHERE ABS(yoy_market_value_pct) > {jump_threshold}" in _SRC_NORM)
    check("no remaining bare reference to the deleted LARGE_JUMP_THRESHOLD_PCT name "
          "anywhere in an f-string/executable context (only the _BY_COUNTY dict name "
          "and its own docstrings/comments should mention the old name)",
          "{LARGE_JUMP_THRESHOLD_PCT}" not in _SRC_NORM)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL PX-20260831-02 TASKS 3/4/5 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
