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
from loaders import ingest_gate  # noqa: F401 — AC5 wiring marker; see load_certified_2025.py's
                                  # module note for why this isn't also called inline here
import parcel_rollup
import psycopg2
import psycopg2.extras

BATCH_SIZE = 5000

CERT_DIRS = {
    2022: os.path.join(config.DATA_DIR, "2022_Certified_Export"),
    2023: os.path.join(config.DATA_DIR, "2023_Certified_Export"),
    2024: os.path.join(config.DATA_DIR, "2024_Certified_Export"),
}


# ── Step 1: PROP.TXT → prop_id → geo_id map (no `parcel` write) + prop_unit ──
def build_pid_geo_map(cert_dir, year):
    """
    Read PROP.TXT and return {prop_id: geo_id} for accepted (sup_num=0,
    has geo_id) rows via ears_format.iter_prop_records() — the shared
    parser, not a locally re-typed copy.
    """
    path = os.path.join(cert_dir, "PROP.TXT")
    print(f"  Reading PROP.TXT ({os.path.getsize(path)/1e9:.2f} GB)…")
    t0 = time.time()
    pid_to_geo = {}
    for rec in ears_format.iter_prop_records(path):
        if rec["prop_id"] and rec["geo_id"]:
            pid_to_geo[rec["prop_id"]] = rec["geo_id"]
    print(f"    {len(pid_to_geo):,} prop_id→geo_id mappings  [{time.time()-t0:.1f}s]")
    return pid_to_geo


def load_prop_unit(conn, cert_dir, year):
    """
    Upsert prop_unit for every unit seen in this year's PROP.TXT — extends
    first_seen_year/last_seen_year via the shared PROP_UNIT_UPSERT_SQL's
    LEAST()/GREATEST(). Does NOT touch the `parcel` table.
    """
    path = os.path.join(cert_dir, "PROP.TXT")
    unit_rows = []
    total = 0
    for rec in ears_format.iter_prop_records(path):
        unit_rows.append((rec["prop_id"], rec["geo_id"], rec["prop_type_cd"], None,
                           rec["owner_id"], rec["owner_name"], year, year))
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
    return total


# ── Step 2: PROP_ENT.TXT → prop_unit_tax_year ────────────────────────────────
def load_prop_ent(conn, cert_dir, year, data_source):
    path = os.path.join(cert_dir, "PROP_ENT.TXT")
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.2f} GB)…")
    t0 = time.time()

    rows_to_insert = []
    total = 0

    def commit_batch():
        nonlocal total
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, rows_to_insert, page_size=2000)
        conn.commit()
        total += len(rows_to_insert)
        rows_to_insert.clear()

    for agg in ears_format.iter_prop_ent_aggregates(path):
        rows_to_insert.append((
            agg["prop_id"], agg.get("year") or year,
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

    print(f"    → {total:,} unit-year rows upserted  [{time.time()-t0:.1f}s]")
    return total


# ── Step 3: LAND_DET.TXT → land_value + imprv_value (by prop_id) ────────────
def load_land_imprv(conn, cert_dir, year):
    path = os.path.join(cert_dir, "LAND_DET.TXT")
    print(f"  Loading LAND_DET.TXT…")
    t0 = time.time()

    land_totals = ears_format.land_totals(path)
    print(f"    {len(land_totals):,} units with land segments")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s",
            (year,),
        )
        market_by_pid = {r[0]: r[1] for r in cur.fetchall()}

    update_sql = """
        UPDATE prop_unit_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE prop_id = %s AND tax_year = %s
    """
    updates = []
    for prop_id, land_val in land_totals.items():
        market_val = market_by_pid.get(prop_id)
        if market_val is None:
            continue
        imprv_val = max(0, market_val - land_val)
        updates.append((land_val, imprv_val, prop_id, year))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} units  [{time.time()-t0:.1f}s]")
    return len(updates)


# ── Post-load summary ─────────────────────────────────────────────────────────
def post_load_summary(conn, year, data_source, rows_before, ajr_before):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s",
            (year,)
        )
        rows_after = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (year, data_source)
        )
        cert_count = cur.fetchone()[0]

        cur.execute("""
            SELECT
                COUNT(*)           AS total,
                COUNT(land_value)  AS lv_non_null,
                COUNT(imprv_value) AS iv_non_null
            FROM parcel_tax_year
            WHERE tax_year = %s AND data_source = %s
        """, (year, data_source))
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
    ap.add_argument('--year', type=int, required=True, choices=[2022, 2023, 2024],
                    help='Tax year to load (2022, 2023, or 2024)')
    args = ap.parse_args()

    year        = args.year
    data_source = f"cert_{year}"
    cert_dir    = CERT_DIRS[year]
    ajr_source  = f"ajr_{year}"

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
        cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s", (year,))
        rows_before = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (year, ajr_source)
        )
        ajr_before = cur.fetchone()[0]

    print(f"  Rows in parcel_tax_year[{year}] before load : {rows_before:,}")
    print(f"  Of which data_source='{ajr_source}'         : {ajr_before:,}\n")

    t_total = time.time()

    load_prop_unit(conn, cert_dir, year)
    load_prop_ent(conn, cert_dir, year, data_source)
    load_land_imprv(conn, cert_dir, year)

    print(f"  Rolling up prop_unit_tax_year → parcel_tax_year for {year}…")
    result = parcel_rollup.run(conn, tax_year=year)
    print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    print(f"\n  Total elapsed: {time.time()-t_total:.1f}s")

    post_load_summary(conn, year, data_source, rows_before, ajr_before)

    conn.close()
    print(f"Done. Run compute_metrics.py after loading all three years.")


if __name__ == "__main__":
    main()
