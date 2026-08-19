#!/usr/bin/env python3
"""
loaders/test_billing_gate.py — fixture tests for loaders/billing_gate.py
(TAX-BILLING-REKEY-3, SPEC_TAX_BILLING_REKEY.md §7.5 point 1).

Every bg*_check() function in billing_gate.py is a pure decision function
(counts/sums/rows in, (passed, detail[, ...]) out) — no DB access — so
these are tested directly, mirroring loaders/test_ingest_gate.py's own
G1-G6 discipline exactly. The gather_and_run() DB-facing orchestration
wrapper is NOT covered here (AC8 disclosure — no live Postgres in this
sandbox); only the decision logic each check actually enforces is.

Per §7.5's explicit requirement, this file includes deliberate-corruption
cases that must FAIL, PLUS the one REQUIRED specific fixture:

  test_required_last_write_wins_loss_reproduction — hand-reproduces the
  REAL bug this whole migration exists to fix (measured, real loss:
  $5,794,968.90 / $170,061,400.28 — see tax_billing_rollup.py's module
  docstring and KNOWN_LIMITATIONS.md). Two synthetic accounts (A1, A2)
  share one geo_id (G1) for tax_year 2025. Under the OLD write path
  (direct INSERT into tax_billing keyed by (geo_id, tax_year), which is
  what every writer did before this migration), processing A1 then A2
  under `ON CONFLICT (geo_id, tax_year) DO UPDATE` means A2's write
  silently clobbers A1's — the old table only ever had ONE row for G1,
  holding A2's numbers, with A1's $1,000 gone with no error. This test
  proves BG2 and BG3 both catch that mechanism (comparing the file's real
  2-account/$3,000 truth against the old scheme's collapsed 0-account/
  $2,000-only state), then proves the NEW unit-layer write path (both
  accounts land distinctly in tax_billing_account, then
  tax_billing_rollup.py SUMs them) makes the identical scenario pass
  cleanly under both checks.

Run: python3 loaders/test_billing_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders import billing_gate as gate
import tax_billing_rollup

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── BG1 ──────────────────────────────────────────────────────────────────
def test_bg1_pass_on_well_formed_ledger():
    rows = [
        {"raw_account": "12345678901234", "tax_year": 2025, "expected_tax_year": 2025},
        {"raw_account": None, "tax_year": 2025, "expected_tax_year": 2025},  # skipped_no_account
        {"raw_account": "98765432109876", "tax_year": 2024, "expected_tax_year": 2025},  # skipped_wrong_year
    ]
    ledger = gate.scan_billing_ledger(rows)
    passed, detail = gate.bg1_conservation_check(ledger)
    check("BG1 pass: well-formed ledger conserves line count", passed, detail)
    check("BG1 pass: buckets classified correctly",
          ledger["buckets"] == {"accepted": 1, "skipped_no_account": 1, "skipped_wrong_year": 1},
          ledger["buckets"])


def test_bg1_deliberate_corruption_miscounted_ledger():
    """DELIBERATE CORRUPTION CASE (mirrors test_ingest_gate.py's G1 case):
    hand-construct a ledger whose bucket counts do NOT sum to total_lines —
    simulating a bug where some source row silently fell through every
    classification bucket (or was double-counted into two). This MUST
    fail BG1."""
    corrupted_ledger = {
        "total_lines": 100,
        "buckets": {"accepted": 90, "skipped_no_account": 5, "skipped_wrong_year": 4},
        # 90+5+4 = 99, not 100 -- one row unaccounted for
        "account_ids": set(),
    }
    passed, detail = gate.bg1_conservation_check(corrupted_ledger)
    check("BG1 CORRUPTION CASE: miscounted ledger correctly FAILS", passed is False, detail)


def test_bg1_real_scan_conservation():
    """Same identity, derived from scan_billing_ledger() end-to-end rather
    than a hand-built dict, to prove the check works against the actual
    scanner, not just a synthetic dict shape."""
    rows = ([{"raw_account": f"ACC{i}", "tax_year": 2025, "expected_tax_year": 2025} for i in range(10)]
            + [{"raw_account": None, "tax_year": 2025, "expected_tax_year": 2025} for _ in range(3)])
    ledger = gate.scan_billing_ledger(rows)
    passed, detail = gate.bg1_conservation_check(ledger)
    check("BG1 real-scan conservation holds", passed, detail)
    check("BG1 real-scan bucket total", sum(ledger["buckets"].values()) == 13, ledger["buckets"])


# ── BG2 ──────────────────────────────────────────────────────────────────
def test_bg2_pass_exact_match():
    passed, detail = gate.bg2_account_coverage_check(5000, 5000)
    check("BG2 pass: exact match", passed, detail)


def test_bg2_fail_mismatch():
    passed, detail = gate.bg2_account_coverage_check(5000, 4998)
    check("BG2 fail: mismatch correctly fails", passed is False, detail)


def test_bg2_deliberate_corruption_dropped_account():
    """DELIBERATE CORRUPTION CASE (mirrors test_ingest_gate.py's G2 dropped-
    unit case): one account_id from the file scan never landed a
    tax_billing_account row for this tax_year -- simulating a loader
    silently dropping an account. This MUST fail BG2."""
    file_account_ids = {"A1", "A2", "A3", "A4", "A5"}
    landed_account_ids = {"A1", "A2", "A3", "A4"}  # A5 silently dropped
    passed, detail = gate.bg2_account_coverage_check(len(file_account_ids), len(landed_account_ids))
    check("BG2 CORRUPTION CASE: dropped account correctly FAILS", passed is False, detail)


# ── BG3 ──────────────────────────────────────────────────────────────────
def test_bg3_pass_all_equal():
    passed, detail = gate.bg3_dollar_conservation_check(50000, 50000, 50000)
    check("BG3 pass: all three equal", passed, detail)


def test_bg3_pass_all_none():
    passed, detail = gate.bg3_dollar_conservation_check(None, None, None)
    check("BG3 pass: all-None is a valid equal match", passed, detail)


def test_bg3_fail_mismatch():
    passed, detail = gate.bg3_dollar_conservation_check(50000, 50000, 49999)
    check("BG3 fail: one value off by $1 correctly fails (zero tolerance)", passed is False, detail)


# ── BG4 ──────────────────────────────────────────────────────────────────
def test_bg4_pass_matches_expected():
    account_rows = [
        {"county_code": "TRAVIS", "account_id": "A1", "tax_year": 2025, "geo_id": "G1",
         "billing_num": "B1", "owner_name": "Smith", "total_tax": 1000, "total_paid": 800,
         "total_due": 200, "is_delinquent": False, "exemption_codes": "HS",
         "data_source": "load_tax_current", "confidence_level": "certified"},
        {"county_code": "TRAVIS", "account_id": "A2", "tax_year": 2025, "geo_id": "G1",
         "billing_num": "B2", "owner_name": "Smith", "total_tax": 2000, "total_paid": 2000,
         "total_due": 0, "is_delinquent": False, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
    ]
    stored = {
        ("TRAVIS", "G1"): {"total_tax": 3000, "total_paid": 2800, "total_due": 200,
                            "is_delinquent": False, "account_count": 2},
    }
    passed, detail, mismatches = gate.bg4_rollup_integrity_check(account_rows, stored, 2025)
    check("BG4 pass: correctly rolled-up row matches", passed, detail)
    check("BG4 pass: zero mismatches", mismatches == [], mismatches)


def test_bg4_deliberate_corruption_rollup_drift():
    """DELIBERATE CORRUPTION CASE (mirrors test_ingest_gate.py's G4 rollup-
    drift case): tax_billing's STORED total_tax has drifted from what the
    real tax_billing_account rows sum to (3000 real vs. 9999 stored — as
    if a stale value never got refreshed by tax_billing_rollup.py). This
    MUST fail BG4."""
    account_rows = [
        {"county_code": "TRAVIS", "account_id": "A1", "tax_year": 2025, "geo_id": "G1",
         "billing_num": "B1", "owner_name": "Smith", "total_tax": 1000, "total_paid": 800,
         "total_due": 200, "is_delinquent": False, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
        {"county_code": "TRAVIS", "account_id": "A2", "tax_year": 2025, "geo_id": "G1",
         "billing_num": "B2", "owner_name": "Smith", "total_tax": 2000, "total_paid": 2000,
         "total_due": 0, "is_delinquent": False, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
    ]
    corrupted_stored = {
        ("TRAVIS", "G1"): {"total_tax": 9999, "total_paid": 2800, "total_due": 200,
                            "is_delinquent": False, "account_count": 2},
    }
    passed, detail, mismatches = gate.bg4_rollup_integrity_check(account_rows, corrupted_stored, 2025)
    check("BG4 CORRUPTION CASE: drifted total_tax correctly FAILS", passed is False, detail)
    check("BG4 CORRUPTION CASE: mismatch identifies the right key/column",
          any(m[0] == ("TRAVIS", "G1") and "total_tax" in m[1] for m in mismatches), mismatches)


def test_bg4_fail_missing_geo_id():
    account_rows = [
        {"county_code": "TRAVIS", "account_id": "A9", "tax_year": 2025, "geo_id": "G9",
         "billing_num": None, "owner_name": None, "total_tax": 500, "total_paid": None,
         "total_due": 500, "is_delinquent": True, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
    ]
    stored = {}  # G9 never made it into tax_billing at all
    passed, detail, mismatches = gate.bg4_rollup_integrity_check(account_rows, stored, 2025)
    check("BG4 fail: geo_id present in accounts but missing from tax_billing", passed is False, detail)


# ── REQUIRED: last-write-wins loss reproduction ─────────────────────────
def test_required_last_write_wins_loss_reproduction():
    """
    REQUIRED (§7.5): reproduces the real, already-measured last-write-wins
    loss mechanism ($5,794,968.90 / $170,061,400.28 real dollars destroyed
    -- see tax_billing_rollup.py's module docstring). Two real taxing
    sub-accounts, A1 ($1,000 total_tax) and A2 ($2,000 total_tax), share
    one geo_id G1 for tax_year 2025. The source file's ground truth: 2
    distinct accounts, $3,000 total.

    OLD PATH (pre-migration, every writer before this rekey): loader
    processes A1, INSERTs INTO tax_billing (geo_id, tax_year, total_tax)
    VALUES ('G1', 2025, 1000) -- table now has one row, $1,000. Loader
    then processes A2 for the SAME geo_id, hits
    `ON CONFLICT (geo_id, tax_year) DO UPDATE SET total_tax = EXCLUDED.total_tax`
    -- the row's total_tax is silently OVERWRITTEN to $2,000. A1's $1,000
    is gone; no error, no warning, nothing in any log. This is modeled
    below as: zero rows ever land in tax_billing_account (that table
    didn't exist pre-migration -- every writer bypassed the unit layer
    entirely), and the surviving tax_billing row holds only A2's $2,000.
        - BG2 (file: 2 distinct accounts vs landed: 0 tax_billing_account
          rows) -- MUST FAIL.
        - BG3 (file sum $3,000 vs the OLD scheme's only observable number,
          the collapsed tax_billing total of $2,000) -- MUST FAIL.

    NEW PATH (post-migration): both A1 and A2 land their own row in
    tax_billing_account (account_id, not geo_id, is the primary key --
    no collision possible), then tax_billing_rollup.py SUMs both into
    tax_billing.total_tax = $3,000, the correct, undamaged total.
        - BG2 (file: 2 distinct accounts vs landed: 2 tax_billing_account
          rows) -- MUST PASS.
        - BG3 (file sum $3,000 == tax_billing_account sum $3,000 ==
          rolled-up tax_billing sum $3,000) -- MUST PASS.
    """
    file_account_ids = {"A1", "A2"}
    file_sum = 1000 + 2000  # = 3000, the real, undamaged truth

    # ── OLD (pre-migration) path ────────────────────────────────────────
    old_landed_account_count = 0  # tax_billing_account never existed; every writer bypassed it
    old_tax_billing_total = 2000  # only A2 survived the ON CONFLICT (geo_id, tax_year) clobber

    old_bg2_passed, old_bg2_detail = gate.bg2_account_coverage_check(
        len(file_account_ids), old_landed_account_count
    )
    check(
        "REQUIRED FIXTURE — OLD PATH: BG2 correctly FAILS "
        "(file says 2 accounts, old scheme landed 0 in the unit layer)",
        old_bg2_passed is False, old_bg2_detail,
    )

    old_bg3_passed, old_bg3_detail = gate.bg3_dollar_conservation_check(
        file_sum, None, old_tax_billing_total
    )
    check(
        "REQUIRED FIXTURE — OLD PATH: BG3 correctly FAILS "
        "($3,000 real vs $2,000 survived the last-write-wins clobber -- "
        "this is the exact mechanism that destroyed the real "
        "$5,794,968.90 / $170,061,400.28)",
        old_bg3_passed is False, old_bg3_detail,
    )

    # ── NEW (post-migration) path ───────────────────────────────────────
    new_account_rows = [
        {"county_code": "TRAVIS", "account_id": "A1", "tax_year": 2025, "geo_id": "G1",
         "billing_num": None, "owner_name": None, "total_tax": 1000, "total_paid": None,
         "total_due": None, "is_delinquent": False, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
        {"county_code": "TRAVIS", "account_id": "A2", "tax_year": 2025, "geo_id": "G1",
         "billing_num": None, "owner_name": None, "total_tax": 2000, "total_paid": None,
         "total_due": None, "is_delinquent": False, "exemption_codes": None,
         "data_source": "load_tax_current", "confidence_level": "certified"},
    ]
    new_landed_account_count = len(new_account_rows)  # both land distinctly, no collision possible
    new_account_table_sum = sum(r["total_tax"] for r in new_account_rows)  # = 3000

    # tax_billing_rollup.py is the ONLY module that writes tax_billing --
    # use its own real compute_rollup() here, not a hand-typed number, so
    # this fixture proves the actual rollup module produces the correct
    # figure, not just that a human did the arithmetic correctly.
    rolled_up = tax_billing_rollup.compute_rollup(new_account_rows, 2025)
    new_tax_billing_total = rolled_up[0]["total_tax"]

    new_bg2_passed, new_bg2_detail = gate.bg2_account_coverage_check(
        len(file_account_ids), new_landed_account_count
    )
    check(
        "REQUIRED FIXTURE — NEW PATH: BG2 correctly PASSES "
        "(both accounts land distinctly in tax_billing_account)",
        new_bg2_passed, new_bg2_detail,
    )

    new_bg3_passed, new_bg3_detail = gate.bg3_dollar_conservation_check(
        file_sum, new_account_table_sum, new_tax_billing_total
    )
    check(
        "REQUIRED FIXTURE — NEW PATH: BG3 correctly PASSES "
        "(file=$3,000 == tax_billing_account=$3,000 == rolled-up tax_billing=$3,000, "
        "via tax_billing_rollup.compute_rollup() itself, not a hand-typed number)",
        new_bg3_passed, new_bg3_detail,
    )

    check(
        "REQUIRED FIXTURE: new-path rollup total exactly recovers the $1,000 the old path destroyed",
        new_tax_billing_total - old_tax_billing_total == 1000,
        f"new={new_tax_billing_total} old={old_tax_billing_total}",
    )


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL BILLING_GATE FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
