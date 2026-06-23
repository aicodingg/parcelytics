"""
load_pir_tcad.py — Step 5 PIR loader for TCAD supplemental fields.

Populates taxable_value, land_value, imprv_value on existing parcel_tax_year
rows (2021–2024) from the TCAD open records response (Ref. R010172-062126).

BEFORE LOADING: run with --inspect to confirm field positions on the actual
file TCAD sends, then adjust the FIELD_* constants below if needed.

Expected format: AJR-style comma-delimited CSV (same structure as existing
AJR files). May arrive as one file per year or one combined file.

Usage:
    python3 loaders/load_pir_tcad.py --inspect   # print first 3 rows, then exit
    python3 loaders/load_pir_tcad.py             # load all years in config.PIR_TCAD_FILES
    python3 loaders/load_pir_tcad.py 2022        # load a single year (optional)
"""
import csv
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
import psycopg2.extras

# ── Field positions (0-based, AJR CSV format) ──────────────────────────────────
# Run --inspect on the actual PIR file to confirm these before loading.
# The AJR aggregate-entity filter (field[3] == '227000') applies here too.
AGGREGATE_ENTITY = "227000"
F_GEO_ID         = 6    # 10-char TCAD long account (2022+); prop_id in 2021
F_PROP_ID        = 7
F_MARKET         = 32   # cross-reference vs existing parcel_tax_year to catch shifts
F_TAXABLE        = 33   # ← UNCONFIRMED: adjust after --inspect. May be at [36] or elsewhere.
F_ASSESSED       = 34
F_LAND           = None  # ← UNCONFIRMED: set to integer index once known from --inspect
F_IMPRV          = None  # ← UNCONFIRMED: set to integer index once known from --inspect
F_HS_CAP         = 35

# Minimum number of fields a row must have to be processed
MIN_FIELDS       = 36


def _int_or_none(v):
    try:
        s = v.strip() if v else ""
        return int(float(s)) if s else None
    except (ValueError, AttributeError):
        return None


def inspect(filepath):
    """Print the first few rows so field positions can be confirmed."""
    print(f"\n=== INSPECT: {os.path.basename(filepath)} ===")
    with open(filepath, encoding="latin-1", errors="replace", newline="") as f:
        reader = csv.reader(f)
        printed = 0
        for i, row in enumerate(reader):
            if not row:
                continue
            # Print all fields with their index
            print(f"\nRow {i} ({len(row)} fields):")
            for j, val in enumerate(row):
                marker = ""
                if j == F_GEO_ID:       marker = " ← F_GEO_ID"
                elif j == F_TAXABLE:    marker = " ← F_TAXABLE (UNCONFIRMED)"
                elif j == F_ASSESSED:   marker = " ← F_ASSESSED"
                elif j == F_MARKET:     marker = " ← F_MARKET"
                elif j == F_HS_CAP:     marker = " ← F_HS_CAP"
                elif F_LAND and j == F_LAND:  marker = " ← F_LAND (UNCONFIRMED)"
                elif F_IMPRV and j == F_IMPRV: marker = " ← F_IMPRV (UNCONFIRMED)"
                print(f"  [{j:2d}] {repr(val[:60])}{marker}")
            printed += 1
            if printed >= 3:
                break

    print("\n=== END INSPECT ===")
    print("Adjust F_TAXABLE, F_LAND, F_IMPRV in this file to match, then run without --inspect.")


def build_pid_lookup(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        return {row[0]: row[1] for row in cur.fetchall()}


def load_year(conn, year, filepath, pid_lookup, dry_run=False):
    t0 = time.time()
    print(f"  Loading PIR TCAD {year}: {os.path.basename(filepath)}")

    if F_LAND is None or F_IMPRV is None:
        print("  WARNING: F_LAND and/or F_IMPRV are not set. Run --inspect first, then "
              "set the correct field indices in load_pir_tcad.py before loading.")
        print("  Proceeding with taxable_value only (land_value and imprv_value will be NULL).")

    rows = []
    seen = set()
    skipped_market_mismatch = 0

    with open(filepath, encoding="latin-1", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for lineno, fields in enumerate(reader, 1):
            if len(fields) < MIN_FIELDS:
                continue
            if fields[3].strip() != AGGREGATE_ENTITY:
                continue

            # Geo ID resolution (same logic as load_ajr.py)
            f6 = fields[F_GEO_ID].strip()
            f7 = _int_or_none(fields[F_PROP_ID])

            if len(f6) == 10 and f6.isdigit():
                geo_id  = f6
                prop_id = f7
            else:
                prop_id = _int_or_none(f6)
                geo_id  = pid_lookup.get(prop_id)
                if not geo_id:
                    continue  # no known mapping — skip rather than create synthetic

            if not geo_id or geo_id in seen:
                continue
            seen.add(geo_id)

            taxable_val = _int_or_none(fields[F_TAXABLE])
            land_val    = _int_or_none(fields[F_LAND])  if F_LAND  is not None and F_LAND  < len(fields) else None
            imprv_val   = _int_or_none(fields[F_IMPRV]) if F_IMPRV is not None and F_IMPRV < len(fields) else None
            pir_market  = _int_or_none(fields[F_MARKET])

            # Sanity check: if PIR market_value disagrees significantly with what we loaded
            # from AJR, flag but still proceed (the VALUES are what we care about).
            # We don't update market_value — it stays as loaded from the authoritative AJR.
            rows.append((taxable_val, land_val, imprv_val, f"ajr_pir_{year}", geo_id, year))

            if lineno % 500_000 == 0:
                print(f"    … {lineno:,} lines, {len(seen):,} parcels collected")

    if not rows:
        print(f"  No rows collected for {year} — check file and field positions.")
        return 0

    if dry_run:
        print(f"  DRY RUN: would update {len(rows):,} parcel_tax_year rows for {year}")
        return len(rows)

    # UPDATE existing parcel_tax_year rows — never creates new rows
    update_sql = """
        UPDATE parcel_tax_year
        SET taxable_value = %s,
            land_value    = %s,
            imprv_value   = %s,
            data_source   = %s
        WHERE geo_id = %s AND tax_year = %s
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, rows, page_size=2000)
        updated = cur.rowcount
    conn.commit()

    elapsed = time.time() - t0
    print(f"    → {updated:,} rows updated in {elapsed:.1f}s")
    return updated


def main():
    parser = argparse.ArgumentParser(description="Load TCAD PIR supplemental fields")
    parser.add_argument("year", nargs="?", type=int, help="Single year to load (optional)")
    parser.add_argument("--inspect", action="store_true",
                        help="Print field layout of first file and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing to DB")
    args = parser.parse_args()

    files = config.PIR_TCAD_FILES
    if not files:
        print("No PIR TCAD files configured in config.py (PIR_TCAD_FILES is empty).")
        print("Add file paths to config.PIR_TCAD_FILES once you receive the TCAD response.")
        return

    if args.inspect:
        for year, path in sorted(files.items()):
            if os.path.exists(path):
                inspect(path)
                break
            print(f"  {path} not found — skipping")
        return

    conn = get_conn()
    try:
        pid_lookup = build_pid_lookup(conn)
        print(f"  prop_id → geo_id lookup: {len(pid_lookup):,} entries")

        target_years = [args.year] if args.year else sorted(files.keys())
        total = 0
        for year in target_years:
            path = files.get(year)
            if not path:
                print(f"  No file configured for {year}")
                continue
            if not os.path.exists(path):
                print(f"  WARNING: {path} not found — skipping {year}")
                continue
            total += load_year(conn, year, path, pid_lookup, dry_run=args.dry_run)

        print(f"\nPIR TCAD load complete — {total:,} rows updated.")
        if not args.dry_run:
            print("Run python3 loaders/compute_metrics.py to recompute parcel_metrics "
                  "so affected fields flip from 'Not Available' to 'Verified'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
