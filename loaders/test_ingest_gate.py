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
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


# ── G3 ────────────────────────────────────────────────────────────────────
def test_g3_pass_all_equal():
    passed, detail = gate.g3_dollar_conservation_check(500, 500, 500)
    check("G3 pass: all three equal", passed, detail)


def test_g3_pass_all_none():
    passed, detail = gate.g3_dollar_conservation_check(None, None, None)
    check("G3 pass: all-None is a valid equal match", passed, detail)


def test_g3_fail_mismatch():
    passed, detail = gate.g3_dollar_conservation_check(500, 500, 499)
    check("G3 fail: one value off by $1 correctly fails (zero tolerance)", passed is False, detail)


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
