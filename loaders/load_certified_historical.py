#!/usr/bin/env python3
"""
load_certified_historical.py — Load 2022, 2023, or 2024 Certified Appraisal Export.

Usage:
    cd ~/Desktop/Claude\ Files/parcel_app
    python3 loaders/load_certified_historical.py --year 2022
    python3 loaders/load_certified_historical.py --year 2023
    python3 loaders/load_certified_historical.py --year 2024

Behavior:
  - Reads PROP.TXT, PROP_ENT.TXT, LAND_DET.TXT from the year's Certified Export folder
  - Does NOT write to the `parcel` table — that table holds the 2025+
    public-identity data and we still don't overwrite it with historical
    values (unchanged policy from before this migration).
  - DOES upsert `prop_unit` (Migration M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md
    §3.4) — prop_unit tracks a unit's first_seen_year/last_seen_year across
    EVERY year we load, independent of the `parcel` identity policy above;
    a prop_id that only ever appears in, say, 2022-2023 data still needs a
    prop_unit row so its 2022/2023 prop_unit_tax_year rows have somewhere
    to point.
  - Writes prop_unit_tax_year for the given tax_year, keyed by prop_id
    directly (PRIMARY KEY (prop_id, tax_year) — no geo_id collision is
    possible here, unlike the pre-migration version of this file, which
    wrote parcel_tax_year via `ON CONFLICT (geo_id, tax_year) DO UPDATE`
    and silently let later units in the file overwrite earlier ones
    sharing a geo_id — Mechanism B, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §1).
  - Calls parcel_rollup.py at the end to (re)derive parcel_tax_year for
    this tax_year from the prop_unit_tax_year rows just written.
  - Reports: rows inserted vs updated, land/imprv null rates, elapsed time

Field positions (same across 2022-2026 exports): see loaders/ears_format.py.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders import ears_format
from loaders import ingest_gate
from loaders.scrape_billing_history import DEFAULT_COUNTY  # PARCEL-ROLLUP-HOTFIX-1
import parcel_rollup
import psycopg2
import psycopg2.extras

# PX-20260824-03 Task 2: gate wiring, real correction of an inaccurate
# premise. Every other refactored loader's own comment (load_certified_2025.py,
# load_2026_preliminary.py) says "the real gate enforcement point is
# loaders/run_all.py's explicit step" -- checked directly against run_all.py's
# real source rather than trusted at face value (this brief's own standing
# rule): run_all.py's gate step (its step 6) only ever calls
# ingest_gate.gather_and_run() for two hardcoded sources, "certified_2025"
# (config.CERT_DIR) and "preliminary_2026" (PRELIM_DIR). run_all.py never
# calls this loader at all, for any year -- it's designed to be run
# standalone (see this module's own docstring's Usage section). So the
# "real enforcement point" those other loaders defer to genuinely does not
# exist for this one; deferring the same way here would mean the gate runs
# zero times for a historical-year load, silently. Fixed here, not by
# routing through run_all.py (this loader isn't in its pipeline and adding
# it there is a bigger, separate change than this brief's two named
# blockers), but by calling gather_and_run() directly at the end of this
# loader's own main(), below -- the same real call shape run_all.py uses
# for its two sources, just invoked from the one place that actually runs
# for this loader's years.
#
# Accepted, disclosed tradeoff: gather_and_run() re-scans PROP.TXT and
# PROP_ENT.TXT a second time (G1/G2/G3's own file scans, independent of the
# reads load_prop_unit()/load_prop_ent() already did above) -- the exact
# "second full read of a multi-gigabyte file" cost load_certified_2025.py's
# own comment cites as its reason NOT to do this inline. That reasoning is
# sound for a loader that might run routinely; less costly here, since a
# historical-year load is an occasional, one-off operation, not a nightly
# job -- flagged explicitly as a real, accepted tradeoff, not a silently
# copied decision.
BATCH_SIZE = 5000

# PX-20260824-03: rewritten from a plain module-level dict pointing at
# config.DATA_DIR (a stale, pre-FILE-ARCH-2 grammar -- "2022_Certified_Export"
# etc. haven't existed under DATA_DIR since FILE-ARCH-2 moved what DATA_DIR
# means; see PX-20260824-02's findings report for the full path-resolution
# audit) to a small function that resolves through config.py's own canonical,
# now-fixed CERT_DIR_2022/2023/2024/2026 constants -- the same real, verified
# archive-drive locations backfill_prop_unit_tax_year_geoid.py's own
# CERT_SOURCE_DIRS already uses this exact pattern for.
#
# Deliberately a function, not a module-level dict: config.CERT_DIR_2022 (and
# siblings) are themselves now lazy, mount-check-guarded attributes (see
# config.py's own CERT_DIR family comment) -- resolving all 4 into a plain
# dict at THIS module's import time would just move the same "import
# shouldn't require the archive drive mounted" problem up one level. A
# function called only from main(), after argument parsing, defers the
# mount check to the one real place it should fire: right before this
# loader is actually about to read from the resolved directory.
def _cert_dir_for_year(year):
    return {
        2022: config.CERT_DIR_2022,
        2023: config.CERT_DIR_2023,
        2024: config.CERT_DIR_2024,
        2026: config.CERT_DIR_2026,
    }[year]


# ── Step 1: PROP.TXT → prop_unit (+ prop_id → geo_id map, side-effect) ──────
def load_prop_unit(conn, cert_dir, year, county_code=DEFAULT_COUNTY):
    """
    Upsert prop_unit for every unit seen in this year's PROP.TXT — extends
    first_seen_year/last_seen_year via the shared PROP_UNIT_UPSERT_SQL's
    LEAST()/GREATEST(). Does NOT touch the `parcel` table.

    Task M5-PERYEAR-GEOID: also builds and returns {prop_id: geo_id} for
    every accepted row in this same PROP.TXT read (no second file read) —
    this IS this year's real, as-of-that-year account assignment, needed
    by load_prop_ent() below since PROP_ENT.TXT itself carries no geo_id
    field at all. This supersedes the old build_pid_geo_map() function,
    which was dead code (never called) that would have done a wasteful
    SECOND full read of PROP.TXT (up to ~9.65 GB) to get the same map —
    now deleted.
    """
    path = os.path.join(cert_dir, "PROP.TXT")
    unit_rows = []
    total = 0
    pid_to_geo = {}
    for rec in ears_format.iter_prop_records(path):
        # PARCEL-ROLLUP-HOTFIX-1: county_code first, matching PROP_UNIT_UPSERT_SQL's
        # real column order.
        unit_rows.append((county_code, rec["prop_id"], rec["geo_id"], rec["prop_type_cd"], None,
                           rec["owner_id"], rec["owner_name"], year, year))
        if rec["prop_id"] and rec["geo_id"]:
            pid_to_geo[rec["prop_id"]] = rec["geo_id"]
        if len(unit_rows) >= BATCH_SIZE:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_UPSERT_SQL, unit_rows, page_size=2000)
            conn.commit()
            total += len(unit_rows)
            unit_rows = []
    if unit_rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_UPSERT_SQL, unit_rows, page_size=2000)
        conn.commit()
        total += len(unit_rows)
    print(f"    → {total:,} prop_unit rows upserted for {year}")
    return total, pid_to_geo


# ── Step 2: PROP_ENT.TXT → prop_unit_tax_year ────────────────────────────────
def load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo, county_code=DEFAULT_COUNTY):
    path = os.path.join(cert_dir, "PROP_ENT.TXT")
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.2f} GB)…")
    t0 = time.time()

    rows_to_insert = []
    total = 0
    n_no_geo = 0

    def commit_batch():
        nonlocal total
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, rows_to_insert, page_size=2000)
        conn.commit()
        total += len(rows_to_insert)
        rows_to_insert.clear()

    for agg in ears_format.iter_prop_ent_aggregates(path):
        # Task M5-PERYEAR-GEOID: PROP_ENT.TXT has no geo_id field -- pid_to_geo
        # (built from this same year's PROP.TXT in load_prop_unit(), above)
        # is the real, as-of-that-year value.
        geo_id = pid_to_geo.get(agg["prop_id"])
        if geo_id is None:
            n_no_geo += 1
        rows_to_insert.append((
            county_code,  # PARCEL-ROLLUP-HOTFIX-1: matching PROP_UNIT_TAX_YEAR_UPSERT_SQL's real column order
            agg["prop_id"], agg.get("year") or year, geo_id,
            agg["market_value"], agg["assessed_value"], agg["taxable_value"],
            None, None, None,
            agg["exemption_codes"], data_source,
        ))
        if len(rows_to_insert) >= BATCH_SIZE:
            commit_batch()
            if total % 100_000 == 0:
                print(f"    … {total:,} rows committed", flush=True)

    if rows_to_insert:
        commit_batch()

    print(f"    → {total:,} unit-year rows upserted  [{time.time()-t0:.1f}s] "
          f"({n_no_geo:,} with no resolvable geo_id)")
    return total


# ── Step 3: LAND_DET.TXT → land_value + imprv_value (by prop_id) ────────────
def load_land_imprv(conn, cert_dir, year, county_code=DEFAULT_COUNTY):
    path = os.path.join(cert_dir, "LAND_DET.TXT")
    print(f"  Loading LAND_DET.TXT…")
    t0 = time.time()

    land_totals = ears_format.land_totals(path)
    print(f"    {len(land_totals):,} units with land segments")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",
            (year, county_code),
        )
        market_by_pid = {r[0]: r[1] for r in cur.fetchall()}

    # PX-20260823-02: county_code added to the WHERE.
    update_sql = """
        UPDATE prop_unit_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE prop_id = %s AND tax_year = %s AND county_code = %s
    """
    updates = []
    for prop_id, land_val in land_totals.items():
        market_val = market_by_pid.get(prop_id)
        if market_val is None:
            continue
        imprv_val = max(0, market_val - land_val)
        updates.append((land_val, imprv_val, prop_id, year, county_code))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} units  [{time.time()-t0:.1f}s]")
    return len(updates)


# ── Post-load summary ─────────────────────────────────────────────────────────
def post_load_summary(conn, year, data_source, rows_before, ajr_before, county_code=DEFAULT_COUNTY):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s",
            (year, county_code)
        )
        rows_after = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",
            (year, data_source, county_code)
        )
        cert_count = cur.fetchone()[0]

        cur.execute("""
            SELECT
                COUNT(*)           AS total,
                COUNT(land_value)  AS lv_non_null,
                COUNT(imprv_value) AS iv_non_null
            FROM parcel_tax_year
            WHERE tax_year = %s AND data_source = %s AND county_code = %s
        """, (year, data_source, county_code))
        total, lv_nn, iv_nn = cur.fetchone()

    inserted = rows_after - rows_before
    updated  = cert_count - inserted

    print(f"\n{'='*65}")
    print(f"  LOAD COMPLETE — {year} Certified Roll  (data_source='{data_source}')")
    print(f"{'='*65}")
    print(f"  AJR rows before load     : {ajr_before:>10,}")
    print(f"  Cert rows after load     : {cert_count:>10,}")
    print(f"  Inserted (new parcels)   : {inserted:>10,}  ← in cert, not in AJR")
    print(f"  Updated  (AJR→cert)      : {updated:>10,}  ← replaced AJR values")
    print(f"\n  land_value  non-null     : {lv_nn:>10,} / {total:,}  ({lv_nn/max(total,1)*100:.1f}%)")
    print(f"  imprv_value non-null     : {iv_nn:>10,} / {total:,}  ({iv_nn/max(total,1)*100:.1f}%)")
    print(f"\n  NOTE (M2): data_source on a parcel_tax_year row is now a")
    print(f"  MIN() representative across every unit summed into that row")
    print(f"  (parcel_rollup.py), not a single ground-truth value per row —")
    print(f"  this count is exact for single-unit parcels (the large")
    print(f"  majority) and a best-effort label for true multi-unit ones.")
    print(f"{'='*65}\n")

    return inserted, updated


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--year', type=int, required=True, choices=[2022, 2023, 2024, 2026],
                    help='Tax year to load (2022, 2023, 2024, or 2026)')
    ap.add_argument(
        '--county', default=DEFAULT_COUNTY,
        help=f"county_code written to every real prop_unit/prop_unit_tax_year row "
             f"(default: {DEFAULT_COUNTY}). PARCEL-ROLLUP-HOTFIX-1: mirrors "
             f"scrape_billing_history.py's own --county convention.",
    )
    ap.add_argument(
        '--published-total', type=float, default=None,
        help="TCAD's published county total for this tax_year, for the gate's G6 "
             "external-reconciliation check (PX-20260824-03). Optional -- if "
             "omitted, G6 is reported as an explicit SKIPPED result (not silently "
             "absent), since no historical-year published total is on file "
             "anywhere in this repo as of this writing.",
    )
    ap.add_argument(
        '--skip-gate', action='store_true',
        help="Skip the Ingestion Conservation Gate (G1-G6) after loading. Not "
             "recommended -- matches run_all.py's own --skip-gate escape hatch "
             "naming for consistency, same rationale (an explicit opt-out, not "
             "a silent one).",
    )
    args = ap.parse_args()

    year        = args.year
    data_source = f"cert_{year}"
    ajr_source  = f"ajr_{year}"

    # PX-20260824-03: resolving cert_dir can now raise ArchiveNotMountedError
    # (the external vault drive isn't attached) -- caught here and reported
    # the same way this loader already reports "dir not found" below, since
    # both are the same real precondition ("the source data isn't reachable
    # right now") surfaced through two different, equally legitimate causes.
    try:
        cert_dir = _cert_dir_for_year(year)
    except config.ArchiveNotMountedError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not os.path.isdir(cert_dir):
        print(f"ERROR: Cert dir not found: {cert_dir}")
        sys.exit(1)

    print(f"\n{'─'*65}")
    print(f"  Loading {year} Certified Appraisal Export")
    print(f"  Source dir : {cert_dir}")
    print(f"  data_source: {data_source}")
    print(f"{'─'*65}\n")

    conn = psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER,
        password=config.DB_PASS
    )

    # Snapshot counts before load
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s",
            (year, args.county),
        )
        rows_before = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",
            (year, ajr_source, args.county)
        )
        ajr_before = cur.fetchone()[0]

    print(f"  Rows in parcel_tax_year[{year}] before load : {rows_before:,}")
    print(f"  Of which data_source='{ajr_source}'         : {ajr_before:,}\n")

    t_total = time.time()

    _, pid_to_geo = load_prop_unit(conn, cert_dir, year, county_code=args.county)
    load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo, county_code=args.county)
    load_land_imprv(conn, cert_dir, year, county_code=args.county)

    print(f"  Rolling up prop_unit_tax_year → parcel_tax_year for {year}…")
    result = parcel_rollup.run(conn, tax_year=year, county_code=args.county)
    print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    print(f"\n  Total elapsed: {time.time()-t_total:.1f}s")

    # PX-20260824-03 Task 2: wire the Ingestion Conservation Gate for real,
    # at the end of this loader's own run. See the module-level comment
    # block above (right after the imports) for why this call lives HERE
    # rather than mirroring load_certified_2025.py verbatim -- that loader
    # does NOT call gather_and_run() itself; run_all.py's own step 6 is its
    # real enforcement point, and run_all.py never calls this loader at all
    # for any year. Without an inline call here, historical-year loads
    # would have zero gate coverage, silently. --skip-gate exists for the
    # same reason run_all.py exposes its own --skip-gate: local iteration
    # without paying the cost of a second full PROP.TXT/PROP_ENT.TXT scan.
    if args.skip_gate:
        print(f"\n  ⚠ Gate SKIPPED (--skip-gate passed) — no G1-G6 checks ran, "
              f"no ingest_audit row written for this run.")
    else:
        prop_path = os.path.join(cert_dir, "PROP.TXT")
        prop_ent_path = os.path.join(cert_dir, "PROP_ENT.TXT")
        print(f"\n  Running Ingestion Conservation Gate (G1-G6) for {data_source}…")
        gate_summary = ingest_gate.gather_and_run(
            conn,
            source_tag=data_source,
            tax_year=year,
            prop_path=prop_path,
            prop_ent_path=prop_ent_path,
            published_total=args.published_total,
            county_code=args.county,
            # PX-20260824-04: explicit, even though it equals source_tag
            # here (this loader's data_source IS its own source_tag,
            # unlike run_all.py's two calls -- see gather_and_run()'s own
            # docstring) -- explicit is safer than relying on the
            # source_tag-equals-data_source default holding forever.
            data_source=data_source,
        )
        for code, gate_result in gate_summary["checks"].items():
            passed, detail = gate_result[0], gate_result[1]
            print(f"    {code}: {'PASS' if passed else 'FAIL'} — {detail}")
        print(f"  GATE OVERALL: {'PASS' if gate_summary['passed'] else 'FAIL'}")
        if not gate_summary["passed"]:
            print(f"  ⚠ Gate reported a FAILURE for {data_source}. Data has already "
                  f"been written and rolled up -- this is a loud post-hoc signal, "
                  f"not a pre-write block (matching run_all.py's own gate-after-load "
                  f"ordering). Investigate ingest_audit before trusting this year's "
                  f"figures.")

    post_load_summary(conn, year, data_source, rows_before, ajr_before, county_code=args.county)

    conn.close()
    print(f"Done. Run compute_metrics.py after loading all three years.")


if __name__ == "__main__":
    main()
