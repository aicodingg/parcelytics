#!/usr/bin/env python3
"""
snapshot_2026_preliminary.py — Task M4-2026-PRELIM-SNAPSHOT, Part 2.

One-time loader that permanently retains the ORIGINAL 2026 Preliminary
Export values into parcel_2026_preliminary_snapshot, BEFORE they're gone
for good. Today's load_certified_historical.py --year 2026 run overwrote
the live preliminary values in parcel_tax_year / prop_unit_tax_year with
certified values (same upsert-on-(geo_id,tax_year) pattern as 2022-2024's
AJR->certified transition) -- this is intentional and correct (certified
should be authoritative going forward), but it means the ORIGINAL
preliminary numbers no longer exist anywhere in the live database. The
original source files are untouched on disk at config.PRELIM_2026_DIR
(confirmed present: PROP.TXT 4.84GB, PROP_ENT.TXT 9.65GB, LAND_DET.TXT
89.5MB, unmodified since their June 12, 2026 extraction).

Design, per the M4-2026-PRELIM-SNAPSHOT brief:
  - Reuses ears_format.py's parsing/aggregation (iter_prop_records,
    iter_prop_ent_aggregates, land_totals) -- no parallel re-implementation
    of the fixed-width slicing.
  - Reuses parcel_rollup.compute_rollup() for the SUM()-per-geo_id rollup
    -- the exact same pure-Python mirror of ROLLUP_SQL that
    load_certified_historical.py's real ROLLUP_SQL run would produce, so
    this snapshot's aggregation logic can never silently drift from the
    canonical rollup (see parcel_rollup.py's module docstring for why that
    single-source-of-truth property matters). compute_rollup() also
    returns hs_cap_loss/data_source fields that this table's schema
    (schema.sql) doesn't have columns for -- those are computed but simply
    not included in the INSERT below; not an oversight, see the schema
    comment for why the brief's proposed column set omits them.
  - Writes ONLY to the new, standalone parcel_2026_preliminary_snapshot
    table (schema.sql). Never touches parcel_tax_year, prop_unit,
    prop_unit_tax_year, or any other live table -- this script has no
    UPDATE/INSERT statement against any table but the new one.
  - TRUNCATE + fresh INSERT, not ON CONFLICT DO NOTHING: this table is
    meant to hold exactly one snapshot (a photograph of a single moment --
    the pre-overwrite 2026 preliminary state), so "run it twice" should
    mean "replace the photo", not "silently keep whatever rows happened to
    load first and skip the rest if a prior run was interrupted partway
    through". ON CONFLICT DO NOTHING would leave a partial/inconsistent
    snapshot un-repairable by simply re-running the script; TRUNCATE +
    fresh INSERT inside one transaction makes every run either fully
    replace the snapshot or leave the previous good one untouched (any
    error during the INSERT rolls the TRUNCATE back too, since both run
    under the same connection with autocommit off until the final commit).

Usage:
    cd ~/Desktop/Claude\ Files/parcel_app
    python3 loaders/snapshot_2026_preliminary.py --dry-run   # parse + roll up only, no DB writes
    python3 loaders/snapshot_2026_preliminary.py             # writes parcel_2026_preliminary_snapshot

IMPORTANT (sandbox-vs-live disclosure): this script was written and
fixture-tested (loaders/test_snapshot_2026_preliminary.py) against small
synthetic fixture files in this sandbox, which has neither a live database
connection nor access to the real multi-gigabyte source files at
config.PRELIM_2026_DIR (confirmed: that directory does not exist in this
sandbox). Diego needs to run this script himself, live, against the real
files and database, per the brief's own "Run today (or when Cowork
finishes it)" instruction.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders import ears_format
import parcel_rollup
# psycopg2 / get_conn are imported lazily inside write_snapshot()/main(), not
# at module load time -- same pattern as loaders/ingest_gate.py's own
# from loaders.db import get_conn (inside its function, not top-level) --
# so this module (and its fixture tests) can be imported and exercised in
# any environment without psycopg2 installed and with no DB reachable,
# same as every other pure-parsing/rollup module in this codebase.

TAX_YEAR = 2026

TRUNCATE_SQL = "TRUNCATE TABLE parcel_2026_preliminary_snapshot"

INSERT_SQL = """
    INSERT INTO parcel_2026_preliminary_snapshot
        (geo_id, market_value, assessed_value, taxable_value,
         land_value, imprv_value, exemption_codes, unit_count)
    VALUES (%(geo_id)s, %(market_value)s, %(assessed_value)s, %(taxable_value)s,
            %(land_value)s, %(imprv_value)s, %(exemption_codes)s, %(unit_count)s)
"""


def build_unit_rows(prop_path=None, ent_path=None, land_path=None,
                     prop_lines=None, ent_lines=None, land_lines=None, verbose=True):
    """
    Parse the three 2026 Preliminary Export source files and return a list
    of dicts shaped exactly as parcel_rollup.compute_rollup() expects
    (prop_id, geo_id, tax_year, market_value, assessed_value,
    taxable_value, hs_cap_loss, land_value, imprv_value, exemption_codes,
    data_source) -- i.e. a synthetic prop_unit_tax_year-shaped row per
    prop_id, built entirely from this one export rather than a live JOIN,
    since this snapshot has no live prop_unit table backing it.

    Mirrors load_certified_historical.py's three-file read order and its
    imprv_value = max(0, market_value - land_value) derivation (same
    "land/imprv only meaningfully known once we have both LAND_DET.TXT and
    a market_value" logic as load_land_imprv() there).

    Each of the three sources accepts EITHER a `*_path` (production, real
    files) OR `*_lines` (an in-memory iterable — fixture tests), passed
    straight through to ears_format's own path/lines duality — same
    pattern as every ears_format.py iterator, so this function is directly
    fixture-testable with loaders/test_ears_format.py's existing
    build_prop_line()/build_prop_ent_line()/build_land_det_line() helpers,
    per the brief's "reuse existing fixtures if usable" instruction.
    """
    def _log(msg):
        if verbose:
            print(msg)

    _log(f"  Reading PROP.TXT" + (f" ({os.path.getsize(prop_path)/1e9:.2f} GB)…" if prop_path else "…"))
    t0 = time.time()
    pid_to_geo = {}
    for rec in ears_format.iter_prop_records(path=prop_path, lines=prop_lines):
        if rec["prop_id"] and rec["geo_id"]:
            pid_to_geo[rec["prop_id"]] = rec["geo_id"]
    _log(f"    {len(pid_to_geo):,} prop_id→geo_id mappings  [{time.time()-t0:.1f}s]")

    _log(f"  Reading LAND_DET.TXT…")
    t0 = time.time()
    land_by_pid = ears_format.land_totals(path=land_path, lines=land_lines)
    _log(f"    {len(land_by_pid):,} units with land segments  [{time.time()-t0:.1f}s]")

    _log(f"  Reading PROP_ENT.TXT" + (f" ({os.path.getsize(ent_path)/1e9:.2f} GB)…" if ent_path else "…"))
    t0 = time.time()
    unit_rows = []
    n_no_geo = 0
    for agg in ears_format.iter_prop_ent_aggregates(path=ent_path, lines=ent_lines):
        geo_id = pid_to_geo.get(agg["prop_id"])
        if not geo_id:
            # Same "not every prop_id survives / resolves" pattern
            # documented repeatedly elsewhere in this codebase (see Part 1's
            # investigation into remaining data_source='preliminary' rows,
            # and KNOWN_LIMITATIONS.md) -- a prop_id present in PROP_ENT.TXT
            # but missing (or supplement/no-geo_id-skipped) from PROP.TXT
            # simply can't be attributed to a geo_id and is dropped, same as
            # every other loader in this codebase does silently today.
            n_no_geo += 1
            continue
        land_val = land_by_pid.get(agg["prop_id"])
        market_val = agg["market_value"]
        imprv_val = max(0, market_val - land_val) if (market_val is not None and land_val is not None) else None
        unit_rows.append({
            "prop_id": agg["prop_id"],
            "geo_id": geo_id,
            "tax_year": TAX_YEAR,
            "market_value": market_val,
            "assessed_value": agg["assessed_value"],
            "taxable_value": agg["taxable_value"],
            "hs_cap_loss": None,
            "land_value": land_val,
            "imprv_value": imprv_val,
            "exemption_codes": agg["exemption_codes"],
            "data_source": "preliminary_2026_snapshot",
        })
    _log(f"    {len(unit_rows):,} unit rows built, {n_no_geo:,} prop_ids skipped (no geo_id)  [{time.time()-t0:.1f}s]")
    return unit_rows


def write_snapshot(conn, rolled_rows):
    import psycopg2.extras
    with conn.cursor() as cur:
        cur.execute(TRUNCATE_SQL)
        rows = [
            {
                "geo_id": r["geo_id"],
                "market_value": r["market_value"],
                "assessed_value": r["assessed_value"],
                "taxable_value": r["taxable_value"],
                "land_value": r["land_value"],
                "imprv_value": r["imprv_value"],
                "exemption_codes": r["exemption_codes"],
                "unit_count": r["unit_count"],
            }
            for r in rolled_rows
        ]
        psycopg2.extras.execute_batch(cur, INSERT_SQL, rows, page_size=2000)
    conn.commit()
    return len(rows)


def print_summary(rolled_rows, written=None):
    total = len(rolled_rows)
    mv_nn = sum(1 for r in rolled_rows if r["market_value"] is not None)
    tv_nn = sum(1 for r in rolled_rows if r["taxable_value"] is not None)
    lv_nn = sum(1 for r in rolled_rows if r["land_value"] is not None)
    iv_nn = sum(1 for r in rolled_rows if r["imprv_value"] is not None)
    multi_unit = sum(1 for r in rolled_rows if r["unit_count"] and r["unit_count"] > 1)

    print(f"\n{'='*65}")
    print(f"  2026 PRELIMINARY SNAPSHOT — parse/rollup summary")
    print(f"{'='*65}")
    print(f"  geo_id rows (parcels)     : {total:>10,}")
    print(f"  market_value  non-null    : {mv_nn:>10,} / {total:,}  ({mv_nn/max(total,1)*100:.1f}%)")
    print(f"  taxable_value non-null    : {tv_nn:>10,} / {total:,}  ({tv_nn/max(total,1)*100:.1f}%)")
    print(f"  land_value    non-null    : {lv_nn:>10,} / {total:,}  ({lv_nn/max(total,1)*100:.1f}%)")
    print(f"  imprv_value   non-null    : {iv_nn:>10,} / {total:,}  ({iv_nn/max(total,1)*100:.1f}%)")
    print(f"  multi-unit parcels        : {multi_unit:>10,}")
    if written is not None:
        print(f"\n  Rows written to parcel_2026_preliminary_snapshot: {written:,}")
    print(f"{'='*65}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and roll up only; do not connect to the DB or write anything")
    ap.add_argument("--prelim-dir", default=config.PRELIM_2026_DIR,
                    help="Override the 2026 Preliminary Export source directory")
    args = ap.parse_args()

    if not os.path.isdir(args.prelim_dir):
        print(f"ERROR: Preliminary export dir not found: {args.prelim_dir}")
        sys.exit(1)

    print(f"\n{'─'*65}")
    print(f"  2026 Preliminary Snapshot — {'DRY RUN (parse/rollup only)' if args.dry_run else 'LIVE WRITE'}")
    print(f"  Source dir: {args.prelim_dir}")
    print(f"{'─'*65}\n")

    t_total = time.time()
    unit_rows = build_unit_rows(
        prop_path=os.path.join(args.prelim_dir, "PROP.TXT"),
        ent_path=os.path.join(args.prelim_dir, "PROP_ENT.TXT"),
        land_path=os.path.join(args.prelim_dir, "LAND_DET.TXT"),
    )

    print(f"  Rolling up {len(unit_rows):,} unit rows by geo_id…")
    rolled_rows = parcel_rollup.compute_rollup(unit_rows, tax_year=TAX_YEAR)
    print(f"    → {len(rolled_rows):,} geo_id (parcel) rows")

    if args.dry_run:
        print_summary(rolled_rows)
        print(f"  Total elapsed: {time.time()-t_total:.1f}s")
        print("Dry run complete. No DB connection made, nothing written.")
        return

    from loaders.db import get_conn, execute_schema
    conn = get_conn()
    execute_schema(conn)
    try:
        written = write_snapshot(conn, rolled_rows)
    finally:
        conn.close()

    print(f"  Total elapsed: {time.time()-t_total:.1f}s")
    print_summary(rolled_rows, written=written)
    print("Done.")


if __name__ == "__main__":
    main()
