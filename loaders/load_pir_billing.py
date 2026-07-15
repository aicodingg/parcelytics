"""
load_pir_billing.py — Step 5 PIR loader for historical tax billing 2021–2024.

Loads historical TaxCurOpenData-format files from the Travis County Tax Office
open records response (sent Jun 21, 2026) into tax_billing and tax_billing_entity.

The Travis County billing format is the same CSV structure as TaxCurOpenData (1).csv
(the existing 2025 file). The TAXYEAR column in the file determines which year each
row belongs to; rows outside 2021–2024 are filtered out.

The Tax Office may respond with:
  (a) One file per year — list each in config.PIR_BILLING_FILES keyed by year
  (b) One combined multi-year file — add it once with any key, e.g. {0: "path.csv"}

Usage:
    python3 loaders/load_pir_billing.py             # load all files in config
    python3 loaders/load_pir_billing.py --inspect   # print header + first row, then exit
    python3 loaders/load_pir_billing.py --dry-run   # count rows without writing
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

# Only accept historical years; refuse to overwrite 2025 data from this loader
VALID_YEARS = {2021, 2022, 2023, 2024}


def _f(v):
    try:
        return float(v.strip().replace(",", "")) if v and v.strip() else None
    except ValueError:
        return None


def _i(v):
    try:
        return int(v.strip()) if v and v.strip() else None
    except ValueError:
        return None


def inspect(filepath):
    """Print header and first row so column names can be confirmed."""
    print(f"\n=== INSPECT: {os.path.basename(filepath)} ===")
    with open(filepath, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        print(f"Columns ({len(headers)}): {headers}")
        for i, row in enumerate(reader):
            print(f"\nRow 1: {dict(row)}")
            break
    print("=== END INSPECT ===\n")
    print("Key columns to verify: PARCEL, TAXYEAR, TOTAL_TAX, TOTAL_DUE, ENTITY1…ENTITYn")


def load_file(conn, filepath, dry_run=False):
    """Load one billing file. Returns (rows_written, years_seen)."""
    print(f"  Loading {os.path.basename(filepath)} ({os.path.getsize(filepath)/1e6:.0f} MB)…")
    t0 = time.time()

    billing_sql = """
        INSERT INTO tax_billing
            (geo_id, tax_year, billing_num, owner_name,
             total_tax, total_paid, total_due,
             is_delinquent, exemption_codes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year) DO UPDATE
            SET billing_num     = EXCLUDED.billing_num,
                owner_name      = EXCLUDED.owner_name,
                total_tax       = EXCLUDED.total_tax,
                total_paid      = EXCLUDED.total_paid,
                total_due       = EXCLUDED.total_due,
                is_delinquent   = EXCLUDED.is_delinquent,
                exemption_codes = EXCLUDED.exemption_codes
    """

    entity_sql = """
        INSERT INTO tax_billing_entity (geo_id, tax_year, entity_code, amount_due, amount_paid)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year, entity_code) DO UPDATE
            SET amount_due  = EXCLUDED.amount_due,
                amount_paid = EXCLUDED.amount_paid
    """

    billing_rows = []
    entity_rows  = []
    total_written = 0
    skipped_year  = 0
    years_seen    = set()

    with open(filepath, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        max_entity = 0
        for h in headers:
            if h.startswith("ENTITY") and h[6:].isdigit():
                max_entity = max(max_entity, int(h[6:]))

        for lineno, row in enumerate(reader, 1):
            raw_parcel = row.get("PARCEL", "").strip().strip('"')
            geo_id = raw_parcel[:10].strip() if raw_parcel else None
            if not geo_id:
                continue

            tax_year = _i(row.get("TAXYEAR", ""))
            if tax_year not in VALID_YEARS:
                skipped_year += 1
                continue
            years_seen.add(tax_year)

            billing_num  = row.get("BILLING", "").strip().strip('"') or None
            owner_name   = row.get("NAMELF", "").strip().strip('"') or None
            exemptions   = row.get("EXEMPTION", "").strip().strip('"') or None
            total_tax    = _f(row.get("TOTAL_TAX", ""))
            total_due    = _f(row.get("TOTAL_DUE", ""))

            # For historical data: total_paid ≈ total_tax if not delinquent
            # The Tax Office data may not break this out the same way as current data
            total_paid   = _f(row.get("TOTAL_PAID", "")) or (total_tax if not total_due else None)
            is_delinquent = bool(total_due and total_due > 0.01)

            billing_rows.append((
                geo_id, tax_year, billing_num, owner_name,
                total_tax, total_paid, total_due, is_delinquent, exemptions
            ))

            for i in range(1, max_entity + 1):
                ent_code = row.get(f"ENTITY{i}", "").strip().strip('"')
                due      = _f(row.get(f"DUE{i}", ""))
                paid     = _f(row.get(f"PAID{i}", ""))
                if ent_code and (due or paid):
                    entity_rows.append((geo_id, tax_year, ent_code, due, paid))

            if not dry_run and len(billing_rows) >= 3000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
                    if entity_rows:
                        psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
                conn.commit()
                total_written += len(billing_rows)
                billing_rows = []
                entity_rows  = []

            if lineno % 100_000 == 0:
                print(f"    … {lineno:,} rows scanned, {total_written:,} committed, "
                      f"years seen: {sorted(years_seen)}")

    if not dry_run and billing_rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
            if entity_rows:
                psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
        conn.commit()
        total_written += len(billing_rows)

    if dry_run:
        total_written = len(billing_rows)
        print(f"  DRY RUN: {total_written:,} billing rows would be written")

    elapsed = time.time() - t0
    if skipped_year:
        print(f"  ({skipped_year:,} rows skipped — outside 2021-2024)")
    print(f"    → {total_written:,} rows in {elapsed:.1f}s, years: {sorted(years_seen)}")
    return total_written, years_seen


def update_coverage_level(conn, years):
    """
    After billing data is loaded for a set of years, update parcel_metrics
    coverage_level from 'value_only' to 'full' for rows that now have billing.
    Also compute yoy_tax_amount_pct and effective_tax_rate for those years.

    Real fix (July 2026, per Diego's "Property Page Small Bugs Batch" item 3,
    same round as compute_metrics.py's coverage_level fix): this used to flip
    coverage_level to 'full' whenever ANY tax_billing row existed for that
    (geo_id, tax_year) -- not checking confidence_level at all, same
    year-blind gap as compute_parcel_metrics()'s old `tax_year = 2025` check,
    just triggered by "a row exists" instead of "the year is 2025". Left
    unfixed, this function would have silently re-introduced the exact bug
    compute_metrics.py's fix just closed the next time any PIR loader ran (all
    four call this after their own upsert -- see load_pir_billing_2021_full.py
    and pir_xlsx_common.run_cli()). Now gated on tb.confidence_level =
    'verified', matching compute_metrics.py's own condition exactly, so a
    derived/reconstructed or portal-scrape-partial row loaded for 2021-2024
    correctly stays 'value_only' instead of being upgraded to 'full' just for
    existing.
    """
    if not years:
        return

    print(f"\n  Updating parcel_metrics coverage for years {sorted(years)}…")
    year_list = ", ".join(str(y) for y in years)

    with conn.cursor() as cur:
        # Flip coverage_level and has_tax_data where billing now exists AND
        # is genuinely verified (not just present) -- see docstring above.
        cur.execute(f"""
            UPDATE parcel_metrics pm
            SET coverage_level = 'full',
                has_tax_data   = TRUE,
                effective_tax_rate = CASE
                    WHEN tb.total_tax > 0 AND pty.market_value > 0
                     AND tb.total_tax::NUMERIC / pty.market_value <= 1
                    THEN ROUND(tb.total_tax::NUMERIC / pty.market_value, 6)
                END
            FROM tax_billing tb
            JOIN parcel_tax_year pty
              ON pty.geo_id = tb.geo_id AND pty.tax_year = tb.tax_year
            WHERE pm.geo_id = tb.geo_id
              AND pm.tax_year = tb.tax_year
              AND pm.tax_year IN ({year_list})
              AND tb.confidence_level = 'verified'
        """)
        updated = cur.rowcount
    conn.commit()
    print(f"    coverage_level → 'full' for {updated:,} rows (verified billing only)")

    # Compute yoy_tax_amount_pct for the newly-billed years
    # YoY = (this_year_tax - prior_year_tax) / prior_year_tax * 100
    # Only valid when BOTH years now have billing
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE parcel_metrics pm
            SET yoy_tax_amount_pct = CASE
                WHEN tb_prev.total_tax > 0
                THEN ROUND(
                    100.0 * (tb_cur.total_tax - tb_prev.total_tax) / tb_prev.total_tax,
                    4)
            END
            FROM tax_billing tb_cur
            JOIN tax_billing tb_prev
              ON tb_prev.geo_id   = tb_cur.geo_id
             AND tb_prev.tax_year = tb_cur.tax_year - 1
            WHERE pm.geo_id   = tb_cur.geo_id
              AND pm.tax_year = tb_cur.tax_year
              AND pm.tax_year IN ({year_list})
              AND tb_cur.total_tax > 0
        """)
        n_yoy = cur.rowcount
    conn.commit()
    print(f"    yoy_tax_amount_pct computed for {n_yoy:,} rows")


def main():
    parser = argparse.ArgumentParser(description="Load Travis County historical billing (PIR)")
    parser.add_argument("--inspect", action="store_true",
                        help="Print header and first row of first file, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without writing to DB")
    parser.add_argument("--skip-metrics", action="store_true",
                        help="Skip updating parcel_metrics coverage after load")
    args = parser.parse_args()

    files = config.PIR_BILLING_FILES
    if not files:
        print("No billing PIR files configured in config.py (PIR_BILLING_FILES is empty).")
        print("Add file paths to config.PIR_BILLING_FILES once you receive the Tax Office response.")
        return

    if args.inspect:
        for path in files.values():
            if os.path.exists(path):
                inspect(path)
                break
            print(f"  {path} not found")
        return

    conn = get_conn()
    try:
        all_years = set()
        total = 0
        for key, path in sorted(files.items()):
            if not os.path.exists(path):
                print(f"  WARNING: {path} not found — skipping")
                continue
            n, years = load_file(conn, path, dry_run=args.dry_run)
            total += n
            all_years |= years

        print(f"\nPIR billing load complete — {total:,} rows, years: {sorted(all_years)}")

        if not args.dry_run and not args.skip_metrics and all_years:
            update_coverage_level(conn, all_years)
            print("\nRun python3 loaders/compute_metrics.py to recompute all derived metrics.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
