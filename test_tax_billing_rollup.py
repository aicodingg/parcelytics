#!/usr/bin/env python3
"""
test_tax_billing_rollup.py — fixture tests for tax_billing_rollup.py
(TAX-BILLING-REKEY-3, SPEC_TAX_BILLING_REKEY.md §7.5). Mirrors
test_parcel_rollup.py's exact pattern for the billing side.

GAP DISCLOSURE: tax_billing_rollup.py's own module comments reference this
file by name ("Used by loaders/test_tax_billing_rollup.py to fixture-test
NULL-semantics, the account-always-wins portal policy, and idempotency in
this sandbox") as if it already existed when that module was built (M1,
task #653). It did not -- confirmed by a direct file-existence check before
writing this one. Built now, during M0-M4 final verification, closing that
gap before compiling the final report rather than letting a described-but-
never-built test file slip through the same way
verify_parcel_filters_canonical.py once did (see verify_rollup_canonical.py's
own honesty note for that precedent).

AC8 disclosure: these tests exercise compute_rollup() / compute_entity_
rollup() / compute_portal_merge() -- the pure-Python mirror of ROLLUP_SQL /
ENTITY_ROLLUP_SQL / PORTAL_MERGE_SQL (see tax_billing_rollup.py's own module
docstring for why that split exists: no live Postgres is reachable in this
sandbox, so the actual SQL strings themselves are reviewed but NOT
executed-verified here). rollup_tax_year() / merge_portal_scrape_year() /
run() -- the thin DB-facing wrappers that execute the real SQL -- are NOT
covered by these tests and remain live-DB-verify-only (see the M0-M4 final
report for the exact commands to run them).

Run: python3 test_tax_billing_rollup.py
"""
import sys

from tax_billing_rollup import compute_rollup, compute_entity_rollup, compute_portal_merge

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def account(account_id, geo_id, tax_year=2025, county_code="TRAVIS",
            billing_num=None, owner_name=None, total_tax=None, total_paid=None,
            total_due=None, is_delinquent=False, exemption_codes=None,
            data_source="load_tax_current", confidence_level="certified"):
    return {
        "county_code": county_code, "account_id": account_id, "tax_year": tax_year,
        "geo_id": geo_id, "billing_num": billing_num, "owner_name": owner_name,
        "total_tax": total_tax, "total_paid": total_paid, "total_due": total_due,
        "is_delinquent": is_delinquent, "exemption_codes": exemption_codes,
        "data_source": data_source, "confidence_level": confidence_level,
    }


# ── compute_rollup() ────────────────────────────────────────────────────
def test_single_account_sum_equals_itself():
    rows = [account("A1", "G1", total_tax=1000, total_paid=800, total_due=200)]
    out = compute_rollup(rows, 2025)
    check("single account: one output row", len(out) == 1, out)
    r = out[0]
    check("single account: total_tax passthrough", r["total_tax"] == 1000, r)
    check("single account: account_count == 1", r["account_count"] == 1, r)


def test_multi_account_sum():
    rows = [
        account("A1", "G1", total_tax=1000, total_paid=800, total_due=200),
        account("A2", "G1", total_tax=2000, total_paid=2000, total_due=0),
        account("A3", "G1", total_tax=500, total_paid=250, total_due=250),
    ]
    out = compute_rollup(rows, 2025)
    r = out[0]
    check("multi account: total_tax summed", r["total_tax"] == 3500, r)
    check("multi account: total_paid summed", r["total_paid"] == 3050, r)
    check("multi account: total_due summed", r["total_due"] == 450, r)
    check("multi account: account_count == 3", r["account_count"] == 3, r)


def test_null_semantics_all_null_stays_null():
    """SUM() of all-NULL inputs is NULL, not 0 — matching real SQL SUM()."""
    rows = [account("A1", "G1", total_tax=None), account("A2", "G1", total_tax=None)]
    out = compute_rollup(rows, 2025)
    check("all-NULL total_tax -> NULL (not 0)", out[0]["total_tax"] is None, out[0])


def test_null_semantics_mixed_partial_sum():
    rows = [
        account("A1", "G1", total_tax=1000),
        account("A2", "G1", total_tax=None),
        account("A3", "G1", total_tax=500),
    ]
    out = compute_rollup(rows, 2025)
    check("mixed NULL/real -> partial sum ignores NULLs", out[0]["total_tax"] == 1500, out[0])


def test_is_delinquent_bool_or():
    rows = [
        account("A1", "G1", is_delinquent=False),
        account("A2", "G1", is_delinquent=True),
        account("A3", "G1", is_delinquent=False),
    ]
    out = compute_rollup(rows, 2025)
    check("is_delinquent BOOL_OR: any true -> true", out[0]["is_delinquent"] is True, out[0])


def test_is_delinquent_all_false():
    rows = [account("A1", "G1", is_delinquent=False), account("A2", "G1", is_delinquent=False)]
    out = compute_rollup(rows, 2025)
    check("is_delinquent BOOL_OR: all false -> false", out[0]["is_delinquent"] is False, out[0])


def test_exemption_union():
    rows = [
        account("A1", "G1", exemption_codes="HS,OV65"),
        account("A2", "G1", exemption_codes="HS,DV"),
        account("A3", "G1", exemption_codes=None),
    ]
    out = compute_rollup(rows, 2025)
    check("exemption union across accounts, deduped+sorted",
          out[0]["exemption_codes"] == "DV,HS,OV65", out[0])


def test_exemption_union_all_none():
    rows = [account("A1", "G1", exemption_codes=None), account("A2", "G1", exemption_codes=None)]
    out = compute_rollup(rows, 2025)
    check("exemption union all-None -> None", out[0]["exemption_codes"] is None, out[0])


def test_billing_num_and_data_source_min_representative():
    rows = [
        account("A1", "G1", billing_num="B002", data_source="scrape", confidence_level="partial"),
        account("A2", "G1", billing_num="B001", data_source="load_tax_current", confidence_level="certified"),
    ]
    out = compute_rollup(rows, 2025)
    r = out[0]
    check("billing_num uses MIN() representative", r["billing_num"] == "B001", r)
    check("data_source uses MIN() representative", r["data_source"] == "load_tax_current", r)
    check("confidence_level uses MIN() representative", r["confidence_level"] == "certified", r)


def test_tax_year_filter():
    rows = [
        account("A1", "G1", tax_year=2025, total_tax=1000),
        account("A1", "G1", tax_year=2024, total_tax=900),
    ]
    out2025 = compute_rollup(rows, 2025)
    out2024 = compute_rollup(rows, 2024)
    check("tax_year filter: 2025 sees only 2025 row", out2025[0]["total_tax"] == 1000, out2025)
    check("tax_year filter: 2024 sees only 2024 row", out2024[0]["total_tax"] == 900, out2024)


def test_multiple_geo_ids_independent():
    rows = [
        account("A1", "G1", total_tax=1000),
        account("A2", "G2", total_tax=5000),
        account("A3", "G2", total_tax=2500),
    ]
    out = compute_rollup(rows, 2025)
    by_geo = {r["geo_id"]: r for r in out}
    check("multiple geo_ids: G1 independent total", by_geo["G1"]["total_tax"] == 1000, by_geo)
    check("multiple geo_ids: G2 summed independently", by_geo["G2"]["total_tax"] == 7500, by_geo)


def test_multiple_counties_independent():
    """county_code leads the grouping key from creation (§7.1/§7.2) -- two
    different counties sharing the same geo_id string (not expected in
    practice, but the grouping must not silently merge them) stay separate."""
    rows = [
        account("A1", "G1", county_code="TRAVIS", total_tax=1000),
        account("A2", "G1", county_code="DALLAS", total_tax=2000),
    ]
    out = compute_rollup(rows, 2025)
    check("two output rows -- one per county_code, not merged", len(out) == 2, out)
    by_county = {r["county_code"]: r for r in out}
    check("TRAVIS row unaffected by DALLAS row", by_county["TRAVIS"]["total_tax"] == 1000, by_county)
    check("DALLAS row unaffected by TRAVIS row", by_county["DALLAS"]["total_tax"] == 2000, by_county)


def test_rollup_idempotent():
    rows = [account("A1", "G1", total_tax=1000), account("A2", "G1", total_tax=2000)]
    out1 = compute_rollup(rows, 2025)
    out2 = compute_rollup(rows, 2025)
    check("rollup idempotent: identical output on re-run", out1 == out2, (out1, out2))


def test_required_last_write_wins_reproduction_via_rollup():
    """
    Same REQUIRED real-mechanism reproduction as
    loaders/test_billing_gate.py's own required fixture, exercised here
    directly against compute_rollup() itself (the actual production
    aggregation logic, not just the gate that checks it): two accounts
    sharing one geo_id must both survive and sum correctly, recovering the
    exact $1,000 the OLD (geo_id, tax_year)-keyed last-write-wins path
    would have destroyed (see tax_billing_rollup.py's own module docstring
    for the real, measured $5,794,968.90 / $170,061,400.28 figures this
    mechanism caused).
    """
    rows = [account("A1", "G1", total_tax=1000), account("A2", "G1", total_tax=2000)]
    out = compute_rollup(rows, 2025)
    check("last-write-wins reproduction: exactly one rolled-up G1 row", len(out) == 1, out)
    check("last-write-wins reproduction: BOTH accounts' dollars survive (3000, not 2000)",
          out[0]["total_tax"] == 3000, out[0])
    check("last-write-wins reproduction: account_count reflects both accounts",
          out[0]["account_count"] == 2, out[0])


# ── compute_entity_rollup() ─────────────────────────────────────────────
def entity(account_id, geo_id, entity_code, tax_year=2025, county_code="TRAVIS",
           amount_due=None, amount_paid=None):
    return {
        "county_code": county_code, "account_id": account_id, "tax_year": tax_year,
        "geo_id": geo_id, "entity_code": entity_code,
        "amount_due": amount_due, "amount_paid": amount_paid,
    }


def test_entity_rollup_sums_by_geo_and_entity_code():
    rows = [
        entity("A1", "G1", "SCH", amount_due=500, amount_paid=500),
        entity("A2", "G1", "SCH", amount_due=300, amount_paid=100),
        entity("A1", "G1", "CTY", amount_due=200, amount_paid=200),
    ]
    out = compute_entity_rollup(rows, 2025)
    by_key = {(r["geo_id"], r["entity_code"]): r for r in out}
    check("entity rollup: 2 distinct (geo_id, entity_code) groups", len(out) == 2, out)
    check("entity rollup: SCH amount_due summed across 2 accounts",
          by_key[("G1", "SCH")]["amount_due"] == 800, by_key)
    check("entity rollup: SCH account_count == 2", by_key[("G1", "SCH")]["account_count"] == 2, by_key)
    check("entity rollup: CTY unaffected by SCH", by_key[("G1", "CTY")]["amount_due"] == 200, by_key)


def test_entity_rollup_null_semantics():
    rows = [entity("A1", "G1", "SCH", amount_due=None), entity("A2", "G1", "SCH", amount_due=None)]
    out = compute_entity_rollup(rows, 2025)
    check("entity rollup: all-NULL amount_due -> NULL", out[0]["amount_due"] is None, out[0])


# ── compute_portal_merge() ───────────────────────────────────────────────
def portal(geo_id, tax_year=2025, county_code="TRAVIS", total_paid=None,
           data_source="portal_scrape", confidence_level="partial"):
    return {
        "county_code": county_code, "geo_id": geo_id, "tax_year": tax_year,
        "total_paid": total_paid, "data_source": data_source,
        "confidence_level": confidence_level,
    }


def test_portal_merge_applies_when_no_existing_row():
    """A portal row for a key with no existing tax_billing row always
    applies -- mirrors a real ON CONFLICT INSERT with nothing to conflict
    against."""
    out = compute_portal_merge({}, [portal("G1", total_paid=750)], 2025)
    key = ("TRAVIS", "G1", 2025)
    check("portal merge: new key applies", key in out, out)
    check("portal merge: new key gets portal's total_paid", out[key]["total_paid"] == 750, out)
    check("portal merge: total_tax mirrors total_paid (only figure this source has)",
          out[key]["total_tax"] == 750, out)


def test_portal_merge_blocked_by_account_grain_data():
    """THE account-always-wins design decision (module docstring point 2):
    an existing tax_billing row with a REAL data_source (from the
    account-grain rollup, e.g. 'load_tax_current') must NEVER be
    overwritten by a portal-scrape row for the same key."""
    key = ("TRAVIS", "G1", 2025)
    existing = {key: {"data_source": "load_tax_current"}}
    out = compute_portal_merge(existing, [portal("G1", total_paid=750)], 2025)
    check("portal merge: account-grain row is NOT overwritten", key not in out, out)


def test_portal_merge_allowed_when_existing_is_portal_or_null():
    """A prior portal-only row (data_source='portal_scrape') or a NULL
    data_source row IS allowed to be updated by a newer portal fetch --
    the guard only blocks account-grain data specifically."""
    key = ("TRAVIS", "G1", 2025)
    existing_portal = {key: {"data_source": "portal_scrape"}}
    out1 = compute_portal_merge(existing_portal, [portal("G1", total_paid=900)], 2025)
    check("portal merge: prior portal-scrape row CAN be refreshed", key in out1, out1)
    check("portal merge: refreshed value applied", out1[key]["total_paid"] == 900, out1)

    existing_null = {key: {"data_source": None}}
    out2 = compute_portal_merge(existing_null, [portal("G1", total_paid=900)], 2025)
    check("portal merge: NULL-data_source row CAN be filled by portal", key in out2, out2)


def test_portal_merge_tax_year_filter():
    out = compute_portal_merge({}, [portal("G1", tax_year=2024, total_paid=500)], 2025)
    check("portal merge: wrong-year row excluded", len(out) == 0, out)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL TAX_BILLING_ROLLUP FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
