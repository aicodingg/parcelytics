#!/usr/bin/env python3
"""
loaders/test_ears_format.py — AC1 fixture tests for loaders/ears_format.py
(Migration M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §6, AC1/AC2).

Pure in-memory fixtures — no files on disk, no DB. Every iterator under
test accepts a `lines=` iterable directly for exactly this reason.

AC1 coverage: PROP.TXT / PROP_ENT.TXT fixture lines parse to the exact
expected dicts, including a 24-unit collision fixture modeled on geo_id
'0100060237', supplement rows, short lines, and TCO-preference cases.

AC2 coverage: for the 24-unit collision fixture, all 24 units survive
(zero drops) through iter_prop_records() + iter_prop_ent_aggregates() —
this is the direct fixture proof that Mechanism A/B/C's collision-loss
(all three previously stemmed from a geo_id-keyed lookup/upsert/dedup
somewhere downstream of parsing) cannot happen at the parsing layer any
more, since these functions never key anything by geo_id at all.

Run: python3 loaders/test_ears_format.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders import ears_format as ef

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Fixture builders ─────────────────────────────────────────────────────
def _pad(s, slc, value):
    text = "" if value is None else str(value)
    chars = list(s)
    for i, ch in enumerate(text):
        pos = slc.start + i
        if pos < slc.stop and pos < len(chars):
            chars[pos] = ch
    return "".join(chars)


def build_prop_line(prop_id, geo_id, prop_type_cd="R", sup_num=0, owner_id=None, owner_name=None, length=700):
    s = " " * length
    s = _pad(s, ef.PROP_SLICES["prop_id"], prop_id)
    s = _pad(s, ef.PROP_SLICES["prop_type_cd"], prop_type_cd)
    s = _pad(s, ef.PROP_SLICES["sup_num"], sup_num)
    s = _pad(s, ef.PROP_SLICES["geo_id"], geo_id)
    s = _pad(s, ef.PROP_SLICES["owner_id"], owner_id)
    s = _pad(s, ef.PROP_SLICES["owner_name"], owner_name)
    return s


def build_prop_ent_line(prop_id, year=2025, sup_num=0, entity_cd="TCO", assessed=None,
                         taxable=None, market=None, exemptions=None, length=420):
    s = " " * length
    s = _pad(s, ef.PROP_ENT_SLICES["prop_id"], prop_id)
    s = _pad(s, ef.PROP_ENT_SLICES["prop_val_yr"], year)
    s = _pad(s, ef.PROP_ENT_SLICES["sup_num"], sup_num)
    s = _pad(s, ef.PROP_ENT_SLICES["entity_cd"], entity_cd)
    s = _pad(s, ef.PROP_ENT_SLICES["assessed_val"], assessed)
    s = _pad(s, ef.PROP_ENT_SLICES["taxable_val"], taxable)
    s = _pad(s, ef.PROP_ENT_SLICES["market_value"], market)
    for code, sl in ef.EXEMPTION_FIELDS:
        amt = (exemptions or {}).get(code, 0)
        s = _pad(s, sl, amt)
    return s


def build_land_det_line(prop_id, land_val, length=160):
    s = " " * length
    s = _pad(s, ef.LAND_DET_SLICES["prop_id"], prop_id)
    s = _pad(s, ef.LAND_DET_SLICES["land_seg_mkt_val"], land_val)
    return s


# ── AC1: PROP.TXT parsing ────────────────────────────────────────────────
def test_prop_basic():
    line = build_prop_line(prop_id=123456, geo_id="0100030105", prop_type_cd="R",
                            owner_id=99, owner_name="JANE DOE")
    recs = list(ef.iter_prop_records(lines=[line]))
    check("prop basic: one record", len(recs) == 1, f"got {len(recs)}")
    r = recs[0]
    check("prop basic: prop_id", r["prop_id"] == 123456, r)
    check("prop basic: geo_id", r["geo_id"] == "0100030105", r)
    check("prop basic: prop_type_cd", r["prop_type_cd"] == "R", r)
    check("prop basic: owner_id", r["owner_id"] == 99, r)
    check("prop basic: owner_name", r["owner_name"] == "JANE DOE", r)


def test_prop_short_line_skipped():
    short = "0" * 100  # < PROP_MIN_LEN
    lines_out = list(ef.iter_prop_lines(lines=[short]))
    check("prop short line: skip_reason", lines_out[0]["skip_reason"] == "short_line", lines_out[0])
    recs = list(ef.iter_prop_records(lines=[short]))
    check("prop short line: excluded from iter_prop_records", len(recs) == 0, recs)


def test_prop_supplement_skipped():
    line = build_prop_line(prop_id=1, geo_id="0100000001", sup_num=3)
    lines_out = list(ef.iter_prop_lines(lines=[line]))
    check("prop supplement: skip_reason", lines_out[0]["skip_reason"] == "supplement", lines_out[0])
    recs = list(ef.iter_prop_records(lines=[line]))
    check("prop supplement: excluded from iter_prop_records", len(recs) == 0, recs)


def test_prop_no_geo_id_skipped():
    line = build_prop_line(prop_id=1, geo_id="", sup_num=0)
    lines_out = list(ef.iter_prop_lines(lines=[line]))
    check("prop no geo_id: skip_reason", lines_out[0]["skip_reason"] == "no_geo_id", lines_out[0])


def test_prop_conservation_identity():
    lines = [
        build_prop_line(1, "0100000001"),
        "short",
        build_prop_line(2, "0100000002", sup_num=1),
        build_prop_line(3, ""),
    ]
    total = 0
    buckets = {}
    for rec in ef.iter_prop_lines(lines=lines):
        total += 1
        b = rec["skip_reason"] or "accepted"
        buckets[b] = buckets.get(b, 0) + 1
    check("prop conservation identity", sum(buckets.values()) == total == 4, buckets)
    check("prop conservation buckets exact", buckets == {"accepted": 1, "short_line": 1, "supplement": 1, "no_geo_id": 1}, buckets)


# ── AC1: PROP_ENT.TXT parsing + TCO preference ───────────────────────────
def test_prop_ent_basic_aggregate():
    line = build_prop_ent_line(prop_id=500, entity_cd="TCO", assessed=100000,
                                taxable=90000, market=150000, exemptions={"hs": 10000})
    aggs = list(ef.iter_prop_ent_aggregates(lines=[line]))
    check("prop_ent basic: one aggregate", len(aggs) == 1, aggs)
    a = aggs[0]
    check("prop_ent basic: prop_id", a["prop_id"] == 500, a)
    check("prop_ent basic: market_value", a["market_value"] == 150000, a)
    check("prop_ent basic: assessed_value (TCO row)", a["assessed_value"] == 100000, a)
    check("prop_ent basic: exemption_codes", a["exemption_codes"] == "HS", a)


def test_prop_ent_tco_preference():
    # Non-TCO row first (assessed 5), then TCO row (assessed 100) — TCO should win,
    # matching the original loaders' `is_tco or not accum.get("assessed_value")` rule.
    l1 = build_prop_ent_line(prop_id=7, entity_cd="XYZ", assessed=5, taxable=4, market=200)
    l2 = build_prop_ent_line(prop_id=7, entity_cd="TCO", assessed=100, taxable=90, market=200)
    aggs = list(ef.iter_prop_ent_aggregates(lines=[l1, l2]))
    check("tco preference: one aggregate", len(aggs) == 1, aggs)
    check("tco preference: assessed uses TCO row", aggs[0]["assessed_value"] == 100, aggs[0])


def test_prop_ent_tco_widened_code_03():
    # entity_cd == "03" — per ears_format's TCO_ENTITY_CODES superset (the
    # documented, intentional widening from the historical loader's set),
    # this must now count as TCO for ALL callers, including the default.
    l1 = build_prop_ent_line(prop_id=8, entity_cd="XYZ", assessed=5, taxable=4, market=200)
    l2 = build_prop_ent_line(prop_id=8, entity_cd="03", assessed=77, taxable=70, market=200)
    aggs = list(ef.iter_prop_ent_aggregates(lines=[l1, l2]))
    check("TCO widened code '03' recognized", aggs[0]["assessed_value"] == 77, aggs[0])


def test_prop_ent_exemption_union_across_lines():
    l1 = build_prop_ent_line(prop_id=9, entity_cd="A", exemptions={"hs": 1}, market=1)
    l2 = build_prop_ent_line(prop_id=9, entity_cd="B", exemptions={"ov65": 1}, market=1)
    aggs = list(ef.iter_prop_ent_aggregates(lines=[l1, l2]))
    check("exemption union across lines", aggs[0]["exemption_codes"] == "HS,OV65", aggs[0])


def test_prop_ent_supplement_and_short_skipped():
    good = build_prop_ent_line(prop_id=1, market=100)
    supp = build_prop_ent_line(prop_id=2, sup_num=5, market=100)
    short = "x" * 50
    aggs = list(ef.iter_prop_ent_aggregates(lines=[good, supp, short]))
    ids = {a["prop_id"] for a in aggs}
    check("prop_ent supplement/short excluded from aggregates", ids == {1}, ids)


# ── AC1/AC2: 24-unit collision fixture modeled on geo_id 0100060237 ─────
def test_24_unit_collision_fixture():
    """
    Models the real-world case this migration exists to fix: 24 distinct
    prop_ids sharing ONE geo_id. Before the migration, every one of the
    three loaders would have dropped all but one of these 24 units
    (Mechanism A/B/C). Proves that at the PARSING layer (ears_format.py),
    zero drops occur — all 24 prop_ids come through both
    iter_prop_records() and iter_prop_ent_aggregates() intact, since
    neither function is keyed by geo_id at all.
    """
    N = 24
    base_pid = 900000
    geo_id = "0100060237"

    prop_lines = [
        build_prop_line(base_pid + i, geo_id, owner_name=f"UNIT {i}")
        for i in range(N)
    ]
    ent_lines = [
        build_prop_ent_line(base_pid + i, entity_cd="TCO",
                             assessed=10_000 + i, taxable=9_000 + i, market=20_000 + i)
        for i in range(N)
    ]

    records = list(ef.iter_prop_records(lines=prop_lines))
    aggs = list(ef.iter_prop_ent_aggregates(lines=ent_lines))

    check("24-unit fixture: all 24 PROP.TXT records survive", len(records) == N, len(records))
    check("24-unit fixture: all 24 share the same geo_id", all(r["geo_id"] == geo_id for r in records))
    check("24-unit fixture: all 24 have distinct prop_ids", len({r["prop_id"] for r in records}) == N)
    check("24-unit fixture: all 24 PROP_ENT aggregates survive", len(aggs) == N, len(aggs))
    check("24-unit fixture: aggregate market_value sums correctly (spot check)",
          sum(a["market_value"] for a in aggs) == sum(20_000 + i for i in range(N)))


# ── LAND_DET.TXT ──────────────────────────────────────────────────────────
def test_land_totals():
    lines = [
        build_land_det_line(1, 1000),
        build_land_det_line(1, 500),   # same prop_id, second segment — should sum
        build_land_det_line(2, 2000),
        "short",
    ]
    totals = ef.land_totals(lines=lines)
    check("land_totals sums multiple segments per prop_id", totals.get(1) == 1500, totals)
    check("land_totals single segment", totals.get(2) == 2000, totals)
    check("land_totals ignores short lines", len(totals) == 2, totals)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL EARS_FORMAT FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
