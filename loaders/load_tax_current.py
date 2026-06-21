"""
Load TaxCurOpenData (1).csv into tax_billing and tax_billing_entity.

Columns (confirmed from inspection):
  PARCEL          — 14-char tax office account (long account + 0000)
  BILLING         — billing number
  TAXYEAR
  OWNID           — owner ID
  NAMELF          — owner name
  EXEMPTION       — exemption codes (H=homestead, W=OV65, etc.)
  CAUSE           — cause/lawsuit number
  TOTAL_TAX       — total levy
  TOTAL_PI        — penalty & interest
  TOTAL_DUE       — total amount due
  PAYDTP / PAYDTE — payment dates (if paid)
  ENTITY1…ENTITY20, DUE1…DUE20, PAID1…PAID20  — per-entity amounts

The PARCEL field is the 14-digit tax office account. To join to TCAD geo_id:
  geo_id = PARCEL[:10].strip()  (the 14-digit = 10-char geo_id + '0000')
"""
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema

import psycopg2.extras


def _f(v):
    """Parse a numeric string, returning None if blank/non-numeric."""
    try:
        return float(v.strip().replace(",", "")) if v and v.strip() else None
    except ValueError:
        return None


def _i(v):
    try:
        return int(v.strip()) if v and v.strip() else None
    except ValueError:
        return None


def load(conn):
    path = config.TAX_CUR_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found, skipping")
        return 0

    print(f"  Loading TaxCurOpenData ({os.path.getsize(path)/1e6:.0f} MB)…")
    t0 = time.time()

    billing_sql = """
        INSERT INTO tax_billing
            (geo_id, tax_year, billing_num, owner_name,
             total_tax, total_paid, total_due,
             is_delinquent, exemption_codes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year) DO UPDATE
            SET billing_num   = EXCLUDED.billing_num,
                owner_name    = EXCLUDED.owner_name,
                total_tax     = EXCLUDED.total_tax,
                total_paid    = EXCLUDED.total_paid,
                total_due     = EXCLUDED.total_due,
                is_delinquent = EXCLUDED.is_delinquent,
                exemption_codes = EXCLUDED.exemption_codes
    """

    entity_sql = """
        INSERT INTO tax_billing_entity (geo_id, tax_year, entity_code, amount_due, amount_paid)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year, entity_code) DO UPDATE
            SET amount_due  = EXCLUDED.amount_due,
                amount_paid = EXCLUDED.amount_paid
    """

    owner_update_sql = """
        UPDATE parcel SET owner_name = %s, owner_id = %s
        WHERE geo_id = %s AND owner_name IS NULL
    """

    billing_rows = []
    entity_rows  = []
    owner_rows   = []
    total_billing = 0

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # Discover entity columns dynamically (ENTITY1..ENTITY30)
        max_entity = 0
        for h in headers:
            if h.startswith("ENTITY") and h[6:].isdigit():
                max_entity = max(max_entity, int(h[6:]))

        for lineno, row in enumerate(reader, 1):
            # Derive geo_id: first 10 chars of the 14-digit PARCEL field
            raw_parcel = row.get("PARCEL", "").strip().strip('"')
            geo_id = raw_parcel[:10].strip() if raw_parcel else None
            if not geo_id:
                continue

            tax_year   = _i(row.get("TAXYEAR", ""))
            billing_num = row.get("BILLING", "").strip().strip('"')
            owner_name = row.get("NAMELF", "").strip().strip('"') or None
            owner_id   = _i(row.get("OWNID", ""))
            exemptions = row.get("EXEMPTION", "").strip().strip('"') or None
            cause      = row.get("CAUSE", "").strip() or None
            total_tax  = _f(row.get("TOTAL_TAX", ""))
            total_pi   = _f(row.get("TOTAL_PI", ""))
            total_due  = _f(row.get("TOTAL_DUE", ""))

            # Is delinquent if total_due > 0 and not fully paid
            is_delinquent = bool(total_due and total_due > 0.01)

            billing_rows.append((
                geo_id, tax_year, billing_num, owner_name,
                total_tax, total_tax,  # total_paid ≈ total_tax if not delinquent
                total_due, is_delinquent, exemptions
            ))

            # Backfill owner name on parcel table
            if owner_name:
                owner_rows.append((owner_name, owner_id, geo_id))

            # Parse entity columns
            for i in range(1, max_entity + 1):
                ent_code = row.get(f"ENTITY{i}", "").strip().strip('"')
                due      = _f(row.get(f"DUE{i}", ""))
                paid     = _f(row.get(f"PAID{i}", ""))
                if ent_code and (due or paid):
                    entity_rows.append((geo_id, tax_year, ent_code, due, paid))

            if len(billing_rows) >= 3000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
                    if entity_rows:
                        psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
                    if owner_rows:
                        psycopg2.extras.execute_batch(cur, owner_update_sql, owner_rows, page_size=2000)
                conn.commit()
                total_billing += len(billing_rows)
                billing_rows = []
                entity_rows  = []
                owner_rows   = []

            if lineno % 100_000 == 0:
                print(f"    … {lineno:,} rows, {total_billing:,} committed")

    # Final flush
    if billing_rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
            if entity_rows:
                psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
            if owner_rows:
                psycopg2.extras.execute_batch(cur, owner_update_sql, owner_rows, page_size=2000)
        conn.commit()
        total_billing += len(billing_rows)

    elapsed = time.time() - t0
    print(f"    → {total_billing:,} billing rows in {elapsed:.1f}s")
    return total_billing


def load_delinquent(conn):
    """Load TaxDelqOpenData.csv into tax_delinquent."""
    path = config.TAX_DELQ_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found, skipping delinquent load")
        return 0

    print(f"  Loading TaxDelqOpenData ({os.path.getsize(path)/1e6:.1f} MB)…")

    sql = """
        INSERT INTO tax_delinquent
            (geo_id, tax_year, delinquent_total, current_year_total, total_due,
             first_delinquent_yr, cause_number)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id) DO UPDATE
            SET delinquent_total    = EXCLUDED.delinquent_total,
                current_year_total  = EXCLUDED.current_year_total,
                total_due           = EXCLUDED.total_due,
                first_delinquent_yr = EXCLUDED.first_delinquent_yr,
                cause_number        = EXCLUDED.cause_number
    """

    rows = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("Account #", "").strip()
            geo_id = raw[:10].strip() if raw else None
            if not geo_id:
                continue
            rows.append((
                geo_id,
                _i(row.get("Last Tax Roll Year", "")),
                _f(row.get("Delinquent Total", "")),
                _f(row.get("Current Year Total", "")),
                _f(row.get("Total Due", "")),
                _i(row.get("1st Year Delinquent", "")),
                row.get("Cause #", "").strip() or None,
            ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=2000)
    conn.commit()
    print(f"    → {len(rows):,} delinquent rows")
    return len(rows)


if __name__ == "__main__":
    conn = get_conn()
    execute_schema(conn)
    load(conn)
    load_delinquent(conn)
    conn.close()
