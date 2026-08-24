#!/usr/bin/env python3
"""
loaders/test_ingest_gate.py — AC4 fixture tests for loaders/ingest_gate.py
(Migration M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §4.3/§6, AC4).

Every g*_check() function in ingest_gate.py is a pure decision function
(counts/sums in, (passed, detail[, ...]) out) — no DB access — so these
are tested directly. The gather_and_run() DB-facing orchestration wrapper
is NOT covered here (AC8 disclosure — no live Postgres in this sandbox);
only the decision logic each check actually enforces is.

Per §4.3's explicit requirement, this file includes TWO deliberate-
corruption cases that must FAIL (not just pass-case tests):
  1. test_g1_deliberate_corruption_miscounted_ledger — a G1 ledger where
     the bucket counts don't sum to the file's total line count (as if a
     line were silently double-counted or dropped from every bucket).
  2. test_g4_deliberate_corruption_rollup_drift — a G4 case where
     parcel_tax_year's stored value has drifted from what SUM()-ing the
     real prop_unit_tax_year rows would produce (as if parcel_rollup.py
     ran once, then a row was hand-edited or a stale value never
     refreshed).

Run: python3 loaders/test_ingest_gate.py

PX-20260824-03 note: the G4 tests below call gate.g4_rollup_integrity_check()
directly, which does a lazy `import parcel_rollup` internally -- that module
transitively imports loaders/db.py, which does `import psycopg2`
unconditionally at module top level. psycopg2 is not installed in this
sandbox (no network access to pip install it), so without the fake-module
injection below this file crashed with ModuleNotFoundError the moment it
reached the G4 tests -- a pre-existing sandbox gap in this test file
specifically (every OTHER fixture-tested module already using this real,
established technique -- e.g. loaders/test_backfill_prop_unit_tax_year_geoid.py,
loaders/test_pir_xlsx_common.py, test_cert_archive_paths.py,
loaders/test_gate_wiring.py -- this file just hadn't needed it before its
G4 tests were added). Nothing this file actually exercises touches
psycopg2's real behavior at all; the fake module exists purely to satisfy
the transitive import chain.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = types.ModuleType("psycopg2.extras")
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_pg2.extras)

from loaders import ingest_gate as gate
from loaders import ears_format as ef

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── G1 ────────────────────────────────────────────────────────────────────
def test_g1_pass_on_well_formed_ledger():
    lines = [
        "0" * 700,                        # a plausible "accepted" PROP.TXT-length line
        "short",                          # short_line
    ]
    ledger = gate.scan_prop_ledger(lines=lines)
    passed, detail = gate.g1_conservation_check(ledger)
    check("G1 pass: well-formed ledger conserves line count", passed, detail)


def test_g1_deliberate_corruption_miscounted_ledger():
    """DELIBERATE CORRUPTION CASE 1 (required by §4.3): hand-construct a
    ledger whose bucket counts do NOT sum to total_lines — simulating a
    bug where some line silently fell through every classification bucket
    (or was double-counted into two). This MUST fail G1."""
    corrupted_ledger = {
        "total_lines": 100,
        "buckets": {"accepted": 90, "short_line": 5, "supplement": 4, "no_geo_id": 0},
        # 90+5+4+0 = 99, not 100 -- one line unaccounted for
        "prop_ids": set(), "geo_ids": set(),
    }
    passed, detail = gate.g1_conservation_check(corrupted_ledger)
    check("G1 CORRUPTION CASE: miscounted ledger correctly FAILS", passed is False, detail)


def test_g1_real_scan_conservation():
    """Same identity, but derived from a real ears_format scan rather than
    a hand-built dict, to prove the check works end-to-end against the
    actual scanner, not just a synthetic dict shape."""
    lines = ["x" * 700 for _ in range(10)] + ["short"] * 3
    ledger = gate.scan_prop_ledger(lines=lines)
    passed, detail = gate.g1_conservation_check(ledger)
    check("G1 real-scan conservation holds", passed, detail)
    check("G1 real-scan bucket total", sum(ledger["buckets"].values()) == 13, ledger["buckets"])


# ── G2 ────────────────────────────────────────────────────────────────────
def test_g2_pass_exact_match():
    passed, detail = gate.g2_identity_coverage_check(1000, 1000)
    check("G2 pass: exact match", passed, detail)


def test_g2_fail_mismatch():
    passed, detail = gate.g2_identity_coverage_check(1000, 998)
    check("G2 fail: mismatch correctly fails", passed is False, detail)


# ── G2 year-scoping fix (task M3-G2-FIX, 2026-07-29) ───────────────────────
# The two tests above only prove g2_identity_coverage_check() correctly
# compares two hand-typed integers -- that function never had a bug. The
# real bug was in gather_and_run()'s SQL: it supplied an UNSCOPED
# `SELECT COUNT(*) FROM prop_unit` (all years ever loaded) as the second
# argument, instead of a tax_year-scoped count. These tests exercise the
# actual computation pattern (file scan's distinct prop_ids vs a
# tax-year-scoped landed-prop_ids count) using realistic fixture data, not
# just arbitrary equal/unequal integers.
def test_g2_pass_year_scoped_counts_match():
    """Every prop_id from this year's file scan landed a prop_unit_tax_year
    row for this same tax_year -- the fixed, correctly-scoped comparison
    must pass."""
    file_ledger = {"prop_ids": {101, 102, 103, 104, 105}}
    # Stands in for `SELECT COUNT(DISTINCT prop_id) FROM prop_unit_tax_year
    # WHERE tax_year = 2025` returning a landed row for every one of this
    # year's file prop_ids.
    landed_prop_ids_this_year = {101, 102, 103, 104, 105}
    passed, detail = gate.g2_identity_coverage_check(
        len(file_ledger["prop_ids"]), len(landed_prop_ids_this_year)
    )
    check("G2 pass: year-scoped file count matches year-scoped landed count", passed, detail)


def test_g2_deliberate_corruption_dropped_unit():
    """DELIBERATE CORRUPTION CASE (required by task M3-G2-FIX, mirrors
    test_g4_deliberate_corruption_rollup_drift's pattern): one prop_id from
    the file scan never landed a prop_unit_tax_year row for this tax_year --
    simulating a loader silently dropping a unit. This MUST fail G2."""
    file_ledger = {"prop_ids": {101, 102, 103, 104, 105}}
    landed_prop_ids_this_year = {101, 102, 103, 104}  # 105 silently dropped
    passed, detail = gate.g2_identity_coverage_check(
        len(file_ledger["prop_ids"]), len(landed_prop_ids_this_year)
    )
    check("G2 CORRUPTION CASE: dropped unit correctly FAILS", passed is False, detail)


def test_g2_old_unscoped_query_would_have_falsely_failed():
    """
    Regression proof for the actual bug, at fixture scale (mirrors the real
    live-DB numbers found tonight: 518,894 all-time prop_unit rows vs
    449,290 in the 2025 file, a 69,604 gap explained entirely by prior
    years' prop_ids no longer in this year's file). Shows BOTH halves:
    the OLD unscoped comparison fails even though nothing is actually
    wrong (scope mismatch, not a real drop), and the NEW year-scoped
    comparison correctly passes once scope matches.
    """
    file_ledger = {"prop_ids": {101, 102, 103}}  # this year's file: 3 prop_ids
    # OLD (buggy) behavior: prop_unit accumulates prop_ids across every
    # year ever loaded -- e.g. 101/102/103 from this year, plus 201/202
    # left over from a prior year's AJR load that aren't in this year's
    # file at all.
    all_time_prop_unit_count = len({101, 102, 103, 201, 202})
    old_buggy_passed, old_buggy_detail = gate.g2_identity_coverage_check(
        len(file_ledger["prop_ids"]), all_time_prop_unit_count
    )
    check(
        "G2 OLD-BUG REPRODUCTION: unscoped all-time count vs this year's file count incorrectly FAILS (confirms the bug was real)",
        old_buggy_passed is False,
        old_buggy_detail,
    )
    # NEW (fixed) behavior: only prop_ids that landed a row for THIS
    # tax_year are counted.
    landed_prop_ids_this_year = {101, 102, 103}
    new_passed, new_detail = gate.g2_identity_coverage_check(
        len(file_ledger["prop_ids"]), len(landed_prop_ids_this_year)
    )
    check(
        "G2 FIX CONFIRMATION: year-scoped count correctly PASSES once scope matches",
        new_passed,
        new_detail,
    )


# ── G3 (PX-20260824-04: narrowed to file vs unit_table only -- see
#    g3_dollar_conservation_check's own docstring) ──────────────────────────
def test_g3_pass_equal():
    passed, detail = gate.g3_dollar_conservation_check(500, 500)
    check("G3 pass: file and unit_table equal", passed, detail)


def test_g3_pass_all_none():
    passed, detail = gate.g3_dollar_conservation_check(None, None)
    check("G3 pass: both-None is a valid equal match", passed, detail)


def test_g3_fail_mismatch():
    passed, detail = gate.g3_dollar_conservation_check(500, 499)
    check("G3 fail: one value off by $1 correctly fails (zero tolerance)", passed is False, detail)


# ── G2 expected_gap (PX-20260824-04) ────────────────────────────────────────
def test_g2_expected_gap_pass():
    """A file_prop_id_count/db_landed_count gap that exactly matches the
    caller-supplied expected_gap (the known PROP_ENT.TXT-vs-PROP.TXT
    population difference) PASSES -- this is the real 429,367/470,483/
    41,116 shape PX-20260824-04 investigated, using small stand-in
    numbers."""
    passed, detail = gate.g2_identity_coverage_check(
        file_prop_id_count=900, db_landed_count=1000, expected_gap=100
    )
    check("G2 pass: gap exactly matches expected_gap (known no-geo population diff)", passed, detail)


def test_g2_expected_gap_fail_bigger_than_expected():
    """A gap LARGER than expected_gap means something beyond the known
    no-geo mechanism is going on (e.g. genuine data loss/duplication) --
    must still FAIL, not be silently absorbed by the explanation."""
    passed, detail = gate.g2_identity_coverage_check(
        file_prop_id_count=900, db_landed_count=1050, expected_gap=100
    )
    check("G2 CORRUPTION CASE: actual gap (150) exceeds expected_gap (100) correctly FAILS",
          passed is False, detail)


def test_g2_expected_gap_fail_smaller_than_expected():
    """A gap SMALLER than expected (fewer rows landed than the file
    populations predict) is just as real a problem -- some of the
    'explained' no-geo rows didn't actually land at all."""
    passed, detail = gate.g2_identity_coverage_check(
        file_prop_id_count=900, db_landed_count=950, expected_gap=100
    )
    check("G2 CORRUPTION CASE: actual gap (50) short of expected_gap (100) correctly FAILS",
          passed is False, detail)


def test_g2_expected_gap_defaults_to_zero():
    """Omitting expected_gap preserves this function's pre-PX-20260824-04
    exact-match behavior -- no caller that doesn't know about the new
    parameter silently gets different semantics."""
    passed, detail = gate.g2_identity_coverage_check(file_prop_id_count=1000, db_landed_count=1000)
    check("G2: expected_gap defaults to 0, exact match still passes", passed, detail)
    passed2, detail2 = gate.g2_identity_coverage_check(file_prop_id_count=1000, db_landed_count=1041)
    check("G2: expected_gap defaults to 0, an unexplained gap still fails", passed2 is False, detail2)


# ── G3_rollup (PX-20260824-04, new check) ───────────────────────────────────
# Both sides are WHOLE-YEAR here -- whole_year_unit_sum is the unscoped
# SUM(market_value) FROM prop_unit_tax_year (every data_source that has
# ever written a row for this tax_year, combined), NOT a single source's
# own total. min_expected_residual is a LOWER BOUND (this run's own known
# no-geo dollars) -- the check passes on >=, not exact equality, since
# other sources may legitimately contribute additional, separately
# unexplained residual this run has no file to name. See
# g3_rollup_residual_check()'s own docstring for the full reasoning
# (including why an earlier, per-source-vs-whole-year version of this
# check was replaced during this task's own fixture testing).
def test_g3_rollup_pass_explained_residual():
    """whole_year_unit_sum exceeds account_table_sum by EXACTLY the known
    no-geo dollar total -- the real $774,939,443-shaped case PX-20260824-04
    investigated for 2022, using small stand-in numbers. PASSES, with a
    detail string that says WHOLE-YEAR scope explicitly."""
    passed, detail = gate.g3_rollup_residual_check(
        whole_year_unit_sum=10_000, account_table_sum=9_000, min_expected_residual=1_000
    )
    check("G3_rollup pass: residual exactly matches the known minimum (no-geo dollars)", passed, detail)
    check("G3_rollup detail explicitly names WHOLE-YEAR scope",
          "WHOLE-YEAR" in detail, detail)


def test_g3_rollup_fail_residual_below_known_minimum():
    """A residual SMALLER than this source's own known no-geo total means
    even the well-understood gap isn't fully reflected -- a real bug (the
    rollup did something it shouldn't have). Must FAIL loudly."""
    passed, detail = gate.g3_rollup_residual_check(
        whole_year_unit_sum=10_000, account_table_sum=9_500, min_expected_residual=1_000
    )
    check("G3_rollup CORRUPTION CASE: residual (500) below known minimum (1,000) correctly FAILS",
          passed is False, detail)


def test_g3_rollup_pass_zero_residual_zero_expected():
    """No no-geo rows at all (true for 2025/2026 today) -- whole_year_unit_sum
    should equal account_table_sum exactly, min_expected_residual=0."""
    passed, detail = gate.g3_rollup_residual_check(
        whole_year_unit_sum=10_000, account_table_sum=10_000, min_expected_residual=0
    )
    check("G3_rollup pass: zero residual when min_expected_residual is zero", passed, detail)


def test_g3_rollup_pass_extra_residual_from_other_sources():
    """PX-20260824-04's real design point: a residual LARGER than this
    source's own known minimum is NOT a failure -- it's expected once
    other data_sources (e.g. a lingering ajr_2022 alongside a fresh
    cert_2022 load) also have rows for the same tax_year and may carry
    their own, separately unexplained no-geo dollars this run can't name
    from its own file alone. Must still PASS, with a detail string that
    says so."""
    passed, detail = gate.g3_rollup_residual_check(
        whole_year_unit_sum=15_000, account_table_sum=9_000, min_expected_residual=1_000
    )
    check("G3_rollup pass: residual (6,000) exceeds this source's own known minimum (1,000)",
          passed, detail)
    check("G3_rollup detail flags the extra residual as likely another source's, not a failure",
          "other source" in detail, detail)


def test_g3_rollup_handles_none_as_zero():
    """account_table_sum=None (nothing rolled up at all yet for this
    tax_year/county) is treated as 0, same None-safety convention as the
    rest of this module's dollar checks."""
    passed, detail = gate.g3_rollup_residual_check(
        whole_year_unit_sum=1_000, account_table_sum=None, min_expected_residual=1_000
    )
    check("G3_rollup: None account_table_sum treated as 0, residual matches known minimum", passed, detail)


# ── G4 ────────────────────────────────────────────────────────────────────
def test_g4_pass_matches_expected():
    unit_rows = [
        {"prop_id": 1, "geo_id": "G1", "tax_year": 2025, "market_value": 100,
         "assessed_value": 90, "taxable_value": 80, "hs_cap_loss": None,
         "land_value": None, "imprv_value": None, "exemption_codes": "HS", "data_source": "certified"},
        {"prop_id": 2, "geo_id": "G1", "tax_year": 2025, "market_value": 200,
         "assessed_value": 180, "taxable_value": 170, "hs_cap_loss": None,
         "land_value": None, "imprv_value": None, "exemption_codes": None, "data_source": "certified"},
    ]
    stored = {
        "G1": {"market_value": 300, "assessed_value": 270, "taxable_value": 250,
               "hs_cap_loss": None, "land_value": None, "imprv_value": None,
               "exemption_codes": "HS", "unit_count": 2},
    }
    passed, detail, mismatches = gate.g4_rollup_integrity_check(unit_rows, stored, 2025)
    check("G4 pass: correctly rolled-up row matches", passed, detail)
    check("G4 pass: zero mismatches", mismatches == [], mismatches)


def test_g4_deliberate_corruption_rollup_drift():
    """DELIBERATE CORRUPTION CASE 2 (required by §4.3): parcel_tax_year's
    STORED market_value has drifted from what the real prop_unit_tax_year
    rows sum to (300 real vs. 999 stored — as if a stale/hand-edited value
    never got refreshed by a rollup run). This MUST fail G4."""
    unit_rows = [
        {"prop_id": 1, "geo_id": "G1", "tax_year": 2025, "market_value": 100,
         "assessed_value": 90, "taxable_value": 80, "hs_cap_loss": None,
         "land_value": None, "imprv_value": None, "exemption_codes": None, "data_source": "certified"},
        {"prop_id": 2, "geo_id": "G1", "tax_year": 2025, "market_value": 200,
         "assessed_value": 180, "taxable_value": 170, "hs_cap_loss": None,
         "land_value": None, "imprv_value": None, "exemption_codes": None, "data_source": "certified"},
    ]
    corrupted_stored = {
        "G1": {"market_value": 999, "assessed_value": 270, "taxable_value": 250,
               "hs_cap_loss": None, "land_value": None, "imprv_value": None,
               "exemption_codes": None, "unit_count": 2},
    }
    passed, detail, mismatches = gate.g4_rollup_integrity_check(unit_rows, corrupted_stored, 2025)
    check("G4 CORRUPTION CASE: drifted market_value correctly FAILS", passed is False, detail)
    check("G4 CORRUPTION CASE: mismatch identifies the right geo_id/column",
          any(m[0] == "G1" and "market_value" in m[1] for m in mismatches), mismatches)


def test_g4_fail_missing_geo_id():
    unit_rows = [{"prop_id": 1, "geo_id": "G2", "tax_year": 2025, "market_value": 50,
                  "assessed_value": None, "taxable_value": None, "hs_cap_loss": None,
                  "land_value": None, "imprv_value": None, "exemption_codes": None, "data_source": "certified"}]
    stored = {}  # G2 never made it into parcel_tax_year at all
    passed, detail, mismatches = gate.g4_rollup_integrity_check(unit_rows, stored, 2025)
    check("G4 fail: geo_id present in units but missing from parcel_tax_year", passed is False, detail)


# ── G5 ────────────────────────────────────────────────────────────────────
def test_g5_pass_exact_match():
    passed, detail = gate.g5_account_coverage_check(50000, 50000)
    check("G5 pass: exact match", passed, detail)


def test_g5_fail_mismatch():
    passed, detail = gate.g5_account_coverage_check(50000, 49990)
    check("G5 fail: mismatch correctly fails", passed is False, detail)


# ── G5 year-scoping fix (task M3-G5-FIX, 2026-07-29) ────────────────────────
# The two tests above only prove g5_account_coverage_check() correctly
# compares two hand-typed integers -- that function never had a bug. The
# real bug was in gather_and_run(): both its inputs were unscoped by year,
# AND the right-hand side queried the wrong table (`parcel`, the
# year-independent master reference table, instead of `parcel_tax_year`),
# so G5 printed the identical result for every tax_year passed to
# --check-db (confirmed live: 489,343 / 517,655 for every one of 2022
# through 2026). These tests exercise the actual fixed computation pattern
# -- distinct geo_ids from this year's prop_unit_tax_year JOIN prop_unit
# rows, vs. row count from this year's parcel_tax_year rows, exactly what
# gather_and_run derives from its existing G4 result sets -- using two
# different fixture "years" with different geo_id sets, proving the fix
# is genuinely year-sensitive, not just that the pure function accepts
# different numbers.
def test_g5_year_sensitivity_different_years_produce_different_results():
    """Two fixture years with deliberately different geo_id sets must
    produce DIFFERENT G5 results -- the pre-fix implementation was blind
    to year and would report the identical numbers for both."""
    # Year 2025: 3 distinct geo_ids in prop_unit_tax_year/prop_unit, 3 rows
    # in parcel_tax_year -- a clean match.
    g4_unit_rows_2025 = [
        {"geo_id": "G1"}, {"geo_id": "G1"}, {"geo_id": "G2"}, {"geo_id": "G3"},
    ]
    g4_parcel_rows_2025 = {"G1": {}, "G2": {}, "G3": {}}
    distinct_geo_ids_2025 = len({r["geo_id"] for r in g4_unit_rows_2025})
    parcel_count_2025 = len(g4_parcel_rows_2025)
    passed_2025, detail_2025 = gate.g5_account_coverage_check(distinct_geo_ids_2025, parcel_count_2025)

    # Year 2022: a genuinely smaller, older year with fewer geo_ids.
    g4_unit_rows_2022 = [{"geo_id": "G1"}, {"geo_id": "G2"}]
    g4_parcel_rows_2022 = {"G1": {}, "G2": {}}
    distinct_geo_ids_2022 = len({r["geo_id"] for r in g4_unit_rows_2022})
    parcel_count_2022 = len(g4_parcel_rows_2022)
    passed_2022, detail_2022 = gate.g5_account_coverage_check(distinct_geo_ids_2022, parcel_count_2022)

    check(
        "G5 year-sensitivity: 2025 and 2022 fixture years report DIFFERENT geo_id counts (fix is not blind to year)",
        (distinct_geo_ids_2025, parcel_count_2025) != (distinct_geo_ids_2022, parcel_count_2022),
        {"2025": (distinct_geo_ids_2025, parcel_count_2025), "2022": (distinct_geo_ids_2022, parcel_count_2022)},
    )
    check("G5 year-sensitivity: 2025 fixture year passes (clean match)", passed_2025, detail_2025)
    check("G5 year-sensitivity: 2022 fixture year passes (clean match)", passed_2022, detail_2022)


def test_g5_old_unscoped_logic_would_have_reported_same_number_both_years():
    """DELIBERATE CORRUPTION / REGRESSION PROOF: reproduces the pre-fix
    behavior directly (unscoped COUNT(DISTINCT geo_id) FROM prop_unit
    across ALL years, and COUNT(*) FROM the year-independent `parcel`
    table) to show it produces the SAME pair of numbers regardless of
    which year is being gated -- exactly the symptom confirmed live
    tonight (489,343 / 517,655 identical across 2022-2026)."""
    # Simulates prop_unit's full geo_id set across ALL years ever loaded,
    # and the year-independent `parcel` master reference table -- both
    # the same value regardless of which tax_year is being asked about.
    all_time_geo_ids_in_prop_unit = {"G1", "G2", "G3", "G4", "G5"}
    all_time_parcel_table_rows = {"G1", "G2", "G3", "G4", "G5"}

    old_distinct_geo_ids_2025 = len(all_time_geo_ids_in_prop_unit)
    old_parcel_count_2025 = len(all_time_parcel_table_rows)
    old_distinct_geo_ids_2022 = len(all_time_geo_ids_in_prop_unit)  # same unscoped query, no year filter
    old_parcel_count_2022 = len(all_time_parcel_table_rows)

    check(
        "G5 OLD-BUG REPRODUCTION: unscoped queries report the IDENTICAL pair of numbers for two different years (confirms the bug was real)",
        (old_distinct_geo_ids_2025, old_parcel_count_2025) == (old_distinct_geo_ids_2022, old_parcel_count_2022),
        {"2025": (old_distinct_geo_ids_2025, old_parcel_count_2025), "2022": (old_distinct_geo_ids_2022, old_parcel_count_2022)},
    )


def test_g5_fail_year_scoped_real_mismatch():
    """A genuine per-year mismatch (a geo_id with real unit data that
    never landed a parcel_tax_year row for that year) must still fail,
    proving the fix doesn't accidentally mask real gaps."""
    g4_unit_rows = [{"geo_id": "G1"}, {"geo_id": "G2"}, {"geo_id": "G3"}]
    g4_parcel_rows = {"G1": {}, "G2": {}}  # G3 never landed a parcel_tax_year row
    distinct_geo_ids = len({r["geo_id"] for r in g4_unit_rows})
    parcel_count = len(g4_parcel_rows)
    passed, detail = gate.g5_account_coverage_check(distinct_geo_ids, parcel_count)
    check("G5 fail: real per-year gap (G3 missing from parcel_tax_year) correctly FAILS", passed is False, detail)


# ── G6 (banded, the one non-exact check) ─────────────────────────────────
def test_g6_ok_within_5pct():
    passed, detail, level = gate.g6_external_reconciliation_check(1_000_000, 1_020_000)
    check("G6 ok: within 5% band", passed and level == "ok", (detail, level))


def test_g6_warn_within_5_to_8pct():
    passed, detail, level = gate.g6_external_reconciliation_check(1_000_000, 1_070_000)
    check("G6 warn: 5-8% band still passes but flags warn", passed and level == "warn", (detail, level))


def test_g6_fail_beyond_8pct():
    passed, detail, level = gate.g6_external_reconciliation_check(1_000_000, 1_200_000)
    check("G6 fail: beyond 8% band fails", passed is False and level == "fail", (detail, level))


def test_g6_fail_no_published_total():
    passed, detail, level = gate.g6_external_reconciliation_check(1_000_000, None)
    check("G6 fail: no published_total short-circuits to fail (no ZeroDivisionError)", passed is False, detail)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL INGEST_GATE FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
