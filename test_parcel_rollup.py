#!/usr/bin/env python3
"""
test_parcel_rollup.py — AC3 fixture tests for parcel_rollup.py (Migration
M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §6, AC3).

AC8 disclosure: these tests exercise compute_rollup() / compute_prop_id_
repair() — the pure-Python mirror of ROLLUP_SQL / PROP_ID_REPAIR_SQL (see
parcel_rollup.py's own module docstring for why that split exists: no
live Postgres is reachable in this sandbox, so the actual SQL strings
themselves are reviewed but NOT executed-verified here). rollup_tax_year()
/ repair_prop_id() / run() — the thin DB-facing wrappers that execute the
real SQL — are NOT covered by these tests and remain live-DB-verify-only
(see this migration's final report for the exact commands to run them).

Run: python3 test_parcel_rollup.py
"""
import sys

from parcel_rollup import compute_rollup, compute_prop_id_repair

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def unit(prop_id, geo_id, tax_year=2025, market_value=None, assessed_value=None,
         taxable_value=None, hs_cap_loss=None, land_value=None, imprv_value=None,
         exemption_codes=None, data_source="certified"):
    return {
        "prop_id": prop_id, "geo_id": geo_id, "tax_year": tax_year,
        "market_value": market_value, "assessed_value": assessed_value,
        "taxable_value": taxable_value, "hs_cap_loss": hs_cap_loss,
        "land_value": land_value, "imprv_value": imprv_value,
        "exemption_codes": exemption_codes, "data_source": data_source,
    }


def test_single_unit_sum_equals_itself():
    rows = [unit(1, "G1", market_value=100, assessed_value=90)]
    out = compute_rollup(rows, 2025)
    check("single unit: one output row", len(out) == 1, out)
    r = out[0]
    check("single unit: market_value passthrough", r["market_value"] == 100, r)
    check("single unit: unit_count == 1", r["unit_count"] == 1, r)


def test_multi_unit_sum():
    rows = [
        unit(1, "G1", market_value=100, assessed_value=90),
        unit(2, "G1", market_value=200, assessed_value=180),
        unit(3, "G1", market_value=50, assessed_value=45),
    ]
    out = compute_rollup(rows, 2025)
    r = out[0]
    check("multi unit: market_value summed", r["market_value"] == 350, r)
    check("multi unit: assessed_value summed", r["assessed_value"] == 315, r)
    check("multi unit: unit_count == 3", r["unit_count"] == 3, r)


def test_null_semantics_all_null_stays_null():
    """SUM() of all-NULL inputs is NULL, not 0 — matching real SQL SUM()."""
    rows = [
        unit(1, "G1", market_value=None),
        unit(2, "G1", market_value=None),
    ]
    out = compute_rollup(rows, 2025)
    check("all-NULL market_value -> NULL (not 0)", out[0]["market_value"] is None, out[0])


def test_null_semantics_mixed_partial_sum():
    """A mix of NULL and real values sums only the non-NULL ones."""
    rows = [
        unit(1, "G1", market_value=100),
        unit(2, "G1", market_value=None),
        unit(3, "G1", market_value=50),
    ]
    out = compute_rollup(rows, 2025)
    check("mixed NULL/real -> partial sum ignores NULLs", out[0]["market_value"] == 150, out[0])


def test_exemption_union():
    rows = [
        unit(1, "G1", exemption_codes="HS,OV65"),
        unit(2, "G1", exemption_codes="HS,DV"),
        unit(3, "G1", exemption_codes=None),
    ]
    out = compute_rollup(rows, 2025)
    check("exemption union across units, deduped+sorted",
          out[0]["exemption_codes"] == "DV,HS,OV65", out[0])


def test_exemption_union_all_none():
    rows = [unit(1, "G1", exemption_codes=None), unit(2, "G1", exemption_codes=None)]
    out = compute_rollup(rows, 2025)
    check("exemption union all-None -> None", out[0]["exemption_codes"] is None, out[0])


def test_data_source_min_representative():
    rows = [unit(1, "G1", data_source="preliminary"), unit(2, "G1", data_source="certified")]
    out = compute_rollup(rows, 2025)
    check("data_source uses MIN() representative", out[0]["data_source"] == "certified", out[0])


def test_tax_year_filter():
    rows = [
        unit(1, "G1", tax_year=2025, market_value=100),
        unit(1, "G1", tax_year=2026, market_value=200),
    ]
    out2025 = compute_rollup(rows, 2025)
    out2026 = compute_rollup(rows, 2026)
    check("tax_year filter: 2025 sees only 2025 row", out2025[0]["market_value"] == 100, out2025)
    check("tax_year filter: 2026 sees only 2026 row", out2026[0]["market_value"] == 200, out2026)


def test_multiple_geo_ids_independent():
    rows = [
        unit(1, "G1", market_value=100),
        unit(2, "G2", market_value=500),
        unit(3, "G2", market_value=250),
    ]
    out = compute_rollup(rows, 2025)
    by_geo = {r["geo_id"]: r for r in out}
    check("multiple geo_ids: G1 independent total", by_geo["G1"]["market_value"] == 100, by_geo)
    check("multiple geo_ids: G2 summed independently", by_geo["G2"]["market_value"] == 750, by_geo)


def test_rollup_idempotent():
    rows = [unit(1, "G1", market_value=100), unit(2, "G1", market_value=200)]
    out1 = compute_rollup(rows, 2025)
    out2 = compute_rollup(rows, 2025)
    check("rollup idempotent: identical output on re-run", out1 == out2, (out1, out2))


def test_prop_id_repair_min_representative():
    units = [
        {"prop_id": 500, "geo_id": "G1"},
        {"prop_id": 100, "geo_id": "G1"},   # should win (MIN)
        {"prop_id": 300, "geo_id": "G1"},
        {"prop_id": 42, "geo_id": "G2"},
    ]
    reps = compute_prop_id_repair(units)
    check("prop_id repair: MIN across multi-unit geo_id", reps["G1"] == 100, reps)
    check("prop_id repair: single-unit geo_id unaffected", reps["G2"] == 42, reps)


def test_prop_id_repair_idempotent():
    units = [{"prop_id": 5, "geo_id": "G1"}, {"prop_id": 9, "geo_id": "G1"}]
    r1 = compute_prop_id_repair(units)
    r2 = compute_prop_id_repair(units)
    check("prop_id repair idempotent", r1 == r2, (r1, r2))


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL PARCEL_ROLLUP FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
