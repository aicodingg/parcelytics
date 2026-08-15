#!/usr/bin/env python3
"""
loaders/test_snapshot_2026_preliminary.py — fixture tests for
loaders/snapshot_2026_preliminary.py (Task M4-2026-PRELIM-SNAPSHOT, Part 2).

Pure in-memory fixtures — no files on disk, no DB. Reuses
loaders/test_ears_format.py's existing build_prop_line() /
build_prop_ent_line() / build_land_det_line() fixture-line builders
directly (imported, not re-implemented) per the brief's "reuse existing
fixtures if usable, don't build from scratch" instruction — the whole
point of build_unit_rows()'s path/lines duality (mirroring ears_format.py
itself) is to make this possible without ever touching a real file.

Covers:
  - single-unit parcel: build_unit_rows() + compute_rollup() round-trip
    produces exactly the source PROP_ENT/LAND_DET values, unit_count=1.
  - multi-unit parcel (2 prop_ids sharing one geo_id): values SUM()
    correctly, unit_count=2 — the exact scenario parcel_rollup.py's
    "collision" fix exists for, now proven for this snapshot path too.
  - a prop_id present in PROP_ENT.TXT but ABSENT from PROP.TXT (no
    geo_id) is correctly dropped, not crashed on or silently mis-attributed
    — same "not every prop_id survives / resolves" pattern documented in
    Part 1's investigation.
  - imprv_value = max(0, market_value - land_value) derivation, including
    the case where land_value is missing (imprv stays None, not 0 or a
    wrong number).
  - INSERT_SQL contains exactly the columns schema.sql defines for
    parcel_2026_preliminary_snapshot, plus county_code (DALLAS-GATE-2,
    added post-migration -- see that test's own comment) (a drift guard —
    if someone edits the schema or the SQL string without updating the
    other, this test catches the mismatch without needing a live DB).

Run: python3 loaders/test_snapshot_2026_preliminary.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders.test_ears_format import build_prop_line, build_prop_ent_line, build_land_det_line
from loaders import snapshot_2026_preliminary as snap
import parcel_rollup

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Single-unit parcel ───────────────────────────────────────────────────
def test_single_unit_round_trip():
    geo_id = "0101140329"
    prop_lines = [build_prop_line(1001, geo_id)]
    ent_lines = [build_prop_ent_line(1001, entity_cd="TCO", assessed=480_000,
                                      taxable=460_000, market=500_000,
                                      exemptions={"hs": 40_000})]
    land_lines = [build_land_det_line(1001, 150_000)]

    unit_rows = snap.build_unit_rows(prop_lines=prop_lines, ent_lines=ent_lines,
                                      land_lines=land_lines, verbose=False)
    check("single unit: exactly 1 unit row built", len(unit_rows) == 1, len(unit_rows))
    ur = unit_rows[0]
    check("single unit: geo_id attributed correctly", ur["geo_id"] == geo_id, ur["geo_id"])
    check("single unit: market_value", ur["market_value"] == 500_000, ur["market_value"])
    check("single unit: land_value", ur["land_value"] == 150_000, ur["land_value"])
    check("single unit: imprv_value = market - land", ur["imprv_value"] == 350_000, ur["imprv_value"])
    check("single unit: exemption_codes", ur["exemption_codes"] == "HS", ur["exemption_codes"])
    check("single unit: tax_year is 2026", ur["tax_year"] == 2026, ur["tax_year"])

    rolled = parcel_rollup.compute_rollup(unit_rows, tax_year=2026)
    check("single unit: rollup produces exactly 1 geo_id row", len(rolled) == 1, len(rolled))
    r = rolled[0]
    check("single unit: rollup unit_count == 1", r["unit_count"] == 1, r["unit_count"])
    check("single unit: rollup market_value unchanged", r["market_value"] == 500_000, r["market_value"])
    check("single unit: rollup taxable_value unchanged", r["taxable_value"] == 460_000, r["taxable_value"])


# ── Multi-unit parcel (the collision case) ──────────────────────────────
def test_multi_unit_parcel_sums_correctly():
    geo_id = "0100060237"
    prop_lines = [
        build_prop_line(2001, geo_id, owner_name="UNIT A"),
        build_prop_line(2002, geo_id, owner_name="UNIT B"),
    ]
    ent_lines = [
        build_prop_ent_line(2001, entity_cd="TCO", assessed=200_000, taxable=190_000, market=210_000),
        build_prop_ent_line(2002, entity_cd="TCO", assessed=300_000, taxable=280_000, market=320_000),
    ]
    land_lines = [
        build_land_det_line(2001, 50_000),
        build_land_det_line(2002, 80_000),
    ]

    unit_rows = snap.build_unit_rows(prop_lines=prop_lines, ent_lines=ent_lines,
                                      land_lines=land_lines, verbose=False)
    check("multi-unit: 2 unit rows built", len(unit_rows) == 2, len(unit_rows))

    rolled = parcel_rollup.compute_rollup(unit_rows, tax_year=2026)
    check("multi-unit: rollup produces exactly 1 geo_id row", len(rolled) == 1, len(rolled))
    r = rolled[0]
    check("multi-unit: unit_count == 2", r["unit_count"] == 2, r["unit_count"])
    check("multi-unit: market_value SUMs (210k + 320k)", r["market_value"] == 530_000, r["market_value"])
    check("multi-unit: taxable_value SUMs (190k + 280k)", r["taxable_value"] == 470_000, r["taxable_value"])
    check("multi-unit: land_value SUMs (50k + 80k)", r["land_value"] == 130_000, r["land_value"])


# ── prop_id in PROP_ENT.TXT but missing from PROP.TXT ───────────────────
def test_orphan_prop_id_dropped_not_crashed():
    prop_lines = [build_prop_line(3001, "0100000001")]  # only 3001 has a PROP.TXT row
    ent_lines = [
        build_prop_ent_line(3001, entity_cd="TCO", assessed=100_000, taxable=90_000, market=110_000),
        build_prop_ent_line(3002, entity_cd="TCO", assessed=200_000, taxable=190_000, market=210_000),  # orphan
    ]
    land_lines = [build_land_det_line(3001, 30_000)]

    unit_rows = snap.build_unit_rows(prop_lines=prop_lines, ent_lines=ent_lines,
                                      land_lines=land_lines, verbose=False)
    check("orphan prop_id: only the resolvable prop_id survives", len(unit_rows) == 1, len(unit_rows))
    check("orphan prop_id: the surviving row is 3001, not 3002",
          unit_rows[0]["prop_id"] == 3001, unit_rows[0]["prop_id"])


# ── imprv_value derivation edge case: no LAND_DET row for this prop_id ──
def test_imprv_value_none_when_land_missing():
    prop_lines = [build_prop_line(4001, "0100000002")]
    ent_lines = [build_prop_ent_line(4001, entity_cd="TCO", assessed=100_000, taxable=90_000, market=110_000)]
    land_lines = []  # no LAND_DET.TXT row for prop_id 4001 at all

    unit_rows = snap.build_unit_rows(prop_lines=prop_lines, ent_lines=ent_lines,
                                      land_lines=land_lines, verbose=False)
    check("no land row: land_value is None, not 0", unit_rows[0]["land_value"] is None, unit_rows[0]["land_value"])
    check("no land row: imprv_value is None (can't be derived), not market_value or 0",
          unit_rows[0]["imprv_value"] is None, unit_rows[0]["imprv_value"])


# ── SQL/schema drift guard ────────────────────────────────────────────────
def test_insert_sql_matches_schema_columns():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        schema_sql = f.read()

    m = re.search(
        r"CREATE TABLE IF NOT EXISTS parcel_2026_preliminary_snapshot \((.*?)\);",
        schema_sql, re.DOTALL,
    )
    check("schema.sql defines parcel_2026_preliminary_snapshot", m is not None)
    if not m:
        return
    schema_cols = set(re.findall(r"^\s*(\w+)\s+\w", m.group(1), re.MULTILINE))

    insert_cols = set(re.findall(r"%\((\w+)\)s", snap.INSERT_SQL))
    insert_target_cols = set(
        re.search(r"INSERT INTO parcel_2026_preliminary_snapshot\s*\((.*?)\)",
                  snap.INSERT_SQL, re.DOTALL).group(1).replace("\n", " ").split(", ")
    )
    insert_target_cols = {c.strip() for c in insert_target_cols}

    # DALLAS-GATE-2: county_code is now written by INSERT_SQL (the table
    # gained it as a real live PK column via migrate_county_partitioning.py)
    # but schema.sql's own CREATE TABLE text for this table was
    # deliberately left unedited -- same "stale inline text, live DB is
    # authoritative" convention already established for the other 14
    # migrated tables (see schema.sql's own comment on this table, and
    # POST-PARTITION-INCIDENT-1-AUDIT's stale-PK-vs-real-index distinction).
    # Excluded from the schema-column comparison below for that reason --
    # this is a known, deliberate mismatch, not drift.
    schema_cols_for_comparison = schema_cols | {"county_code"}

    check("INSERT_SQL's placeholder names are all real schema columns (plus county_code, "
          "deliberately added post-migration -- see comment above)",
          insert_cols <= schema_cols_for_comparison, insert_cols - schema_cols_for_comparison)
    check("INSERT_SQL's target column list matches its own placeholders",
          insert_cols == insert_target_cols, (insert_cols, insert_target_cols))
    # snapshotted_at is DEFAULT NOW() in schema.sql and deliberately NOT in
    # INSERT_SQL's column list (every row gets the same load-time
    # timestamp via the column default, not a per-row Python value) --
    # every OTHER schema column (plus county_code) should be written
    # explicitly.
    check("every schema column except snapshotted_at (plus county_code) is written by INSERT_SQL",
          (schema_cols_for_comparison - {"snapshotted_at"}) == insert_target_cols,
          (schema_cols_for_comparison - {"snapshotted_at"}, insert_target_cols))


def main():
    test_single_unit_round_trip()
    test_multi_unit_parcel_sums_correctly()
    test_orphan_prop_id_dropped_not_crashed()
    test_imprv_value_none_when_land_missing()
    test_insert_sql_matches_schema_columns()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
