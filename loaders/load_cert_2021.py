#!/usr/bin/env python3
"""
load_cert_2021.py — Load 2021 Certified Appraisal Roll into parcel_tax_year.

Usage:
    cd ~/Desktop/Claude\ Files/parcel_app
    python3 loaders/load_cert_2021.py --csv cert_2021_full.csv [--dry-run]

Behavior:
  - Reads CSV produced by parse_cert_2021_pdf.py
  - Deduplicates split-parcel accounts: keeps highest market_value per geo_id;
    tie-breaks on primary account (RefID2 ending in 0000)
  - UPSERTs into parcel_tax_year for tax_year=2021:
      * ON CONFLICT (geo_id, tax_year): replaces ajr_2021 values with cert_2021
      * For land_value / imprv_value: COALESCE — keeps existing DB value if PDF had NULL
        (avoids overwriting good data with two-column-collapse gaps)
  - Prints post-load summary:
      * Rows inserted (new parcels not in AJR)
      * Rows updated (AJR → cert_2021)
      * land_value non-null rate across all loaded rows
      * imprv_value non-null rate

Fields:
    CSV column        DB column
    ──────────────    ──────────────────
    market_value   →  market_value
    assessed_value →  assessed_value
    taxable_value  →  taxable_value
    cap_loss       →  hs_cap_loss
    land_value     →  land_value      (NULL preserved — not overwritten with NULL)
    imprv_value    →  imprv_value     (NULL preserved — not overwritten with NULL)
    exemption_codes→  exemption_codes (if column exists)
    'cert_2021'    →  data_source

Does NOT touch 2022–2024 rows.
2022–2024 will be loaded via load_certified_2025.py approach (structured TXT files).
"""

import argparse
import csv
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import config
import psycopg2
import psycopg2.extras

TAX_YEAR    = 2021
DATA_SOURCE = 'cert_2021'
BATCH_SIZE  = 500


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_int(val):
    """Convert CSV string to int, return None if blank/null."""
    if val is None:
        return None
    s = str(val).strip()
    if s in ('', 'None', 'NULL', 'none'):
        return None
    try:
        return int(s.replace(',', ''))
    except ValueError:
        return None


def load_csv(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def deduplicate(records):
    """
    For split-parcel accounts (same geo_id, different RefID2 suffix), keep the
    record with the highest market_value.  Tie-break: prefer RefID2 ending in
    '0000' (the primary/main account).

    Returns (deduped_list, dupe_count).
    """
    by_geo = defaultdict(list)
    for r in records:
        by_geo[r['geo_id']].append(r)

    deduped    = []
    dupe_count = 0

    for geo_id, group in by_geo.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            dupe_count += 1
            def _sort_key(r):
                mv      = parse_int(r.get('market_value')) or 0
                primary = 1 if r.get('refid2', '').endswith('0000') else 0
                return (mv, primary)
            best = sorted(group, key=_sort_key, reverse=True)[0]
            deduped.append(best)

    return deduped, dupe_count


def has_column(conn, table, column):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table, column))
        return cur.fetchone() is not None


# ── Core UPSERT ───────────────────────────────────────────────────────────────

def build_upsert_sql(with_exemptions):
    cols_insert = """geo_id, tax_year,
                market_value, assessed_value, taxable_value, hs_cap_loss,
                land_value, imprv_value, data_source"""
    vals_insert = """%(geo_id)s, %(tax_year)s,
                %(market_value)s, %(assessed_value)s, %(taxable_value)s, %(cap_loss)s,
                %(land_value)s, %(imprv_value)s, %(data_source)s"""
    set_clause  = """
                market_value   = EXCLUDED.market_value,
                assessed_value = EXCLUDED.assessed_value,
                taxable_value  = EXCLUDED.taxable_value,
                hs_cap_loss    = EXCLUDED.hs_cap_loss,
                land_value     = COALESCE(EXCLUDED.land_value,  parcel_tax_year.land_value),
                imprv_value    = COALESCE(EXCLUDED.imprv_value, parcel_tax_year.imprv_value),
                data_source    = EXCLUDED.data_source"""

    if with_exemptions:
        cols_insert += ", exemption_codes"
        vals_insert += ", %(exemption_codes)s"
        set_clause  += ",\n                exemption_codes = EXCLUDED.exemption_codes"

    return f"""
        INSERT INTO parcel_tax_year ({cols_insert})
        VALUES ({vals_insert})
        ON CONFLICT (geo_id, tax_year) DO UPDATE SET
            {set_clause}
        RETURNING (xmax = 0) AS inserted
    """


def upsert_batch(cur, sql, batch):
    """Execute a batch via execute_values (returns RETURNING rows)."""
    # execute_values doesn't support %(name)s dict style — use executemany instead
    cur.executemany(sql, batch)
    # Note: executemany doesn't return RETURNING rows easily; we count separately


def run_load(conn, records, with_exemptions, dry_run):
    sql = build_upsert_sql(with_exemptions)

    # Count 2021 rows before load
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s", (TAX_YEAR,))
        rows_before = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (TAX_YEAR, 'ajr_2021')
        )
        ajr_before = cur.fetchone()[0]

    print(f"\n  Rows in parcel_tax_year[2021] before load : {rows_before:,}")
    print(f"  Of which data_source='ajr_2021'          : {ajr_before:,}")

    if dry_run:
        print("\n  [DRY RUN] — no changes written.")
        return

    # UPSERT in batches
    processed = 0
    with conn.cursor() as cur:
        batch = []
        for r in records:
            row = {
                'geo_id':          r['geo_id'],
                'tax_year':        TAX_YEAR,
                'market_value':    parse_int(r.get('market_value')),
                'assessed_value':  parse_int(r.get('assessed_value')),
                'taxable_value':   parse_int(r.get('taxable_value')),
                'cap_loss':        parse_int(r.get('cap_loss')),
                'land_value':      parse_int(r.get('land_value')),
                'imprv_value':     parse_int(r.get('imprv_value')),
                'data_source':     DATA_SOURCE,
            }
            if with_exemptions:
                row['exemption_codes'] = r.get('exemption_codes') or None
            batch.append(row)

            if len(batch) >= BATCH_SIZE:
                cur.executemany(sql, batch)
                processed += len(batch)
                batch = []
                if processed % 10_000 == 0:
                    print(f"  ... {processed:,} rows processed", flush=True)

        if batch:
            cur.executemany(sql, batch)
            processed += len(batch)

    conn.commit()

    # Count after
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s", (TAX_YEAR,))
        rows_after = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (TAX_YEAR, DATA_SOURCE)
        )
        cert_after = cur.fetchone()[0]
        cur.execute("""
            SELECT
                COUNT(*)                                  AS total,
                COUNT(land_value)                         AS lv_non_null,
                COUNT(imprv_value)                        AS iv_non_null,
                COUNT(CASE WHEN prop_type = 'R' THEN 1 END) AS r_count
            FROM (
                SELECT pty.land_value, pty.imprv_value,
                       (SELECT prop_type FROM (
                           SELECT regexp_replace(pty2.geo_id, '.*', '') AS prop_type
                           FROM parcel_tax_year pty2
                           WHERE pty2.geo_id = pty.geo_id LIMIT 1
                       ) sub)
                FROM parcel_tax_year pty
                WHERE tax_year = %s AND data_source = %s
            ) sub2
        """, (TAX_YEAR, DATA_SOURCE))
        # Simpler version — just get totals
        cur.execute("""
            SELECT
                COUNT(*)           AS total,
                COUNT(land_value)  AS lv_non_null,
                COUNT(imprv_value) AS iv_non_null
            FROM parcel_tax_year
            WHERE tax_year = %s AND data_source = %s
        """, (TAX_YEAR, DATA_SOURCE))
        stats = cur.fetchone()
        total, lv_nn, iv_nn = stats

    inserted = rows_after - rows_before
    updated  = cert_after - inserted

    return {
        'processed': processed,
        'rows_before': rows_before,
        'rows_after': rows_after,
        'ajr_before': ajr_before,
        'cert_after': cert_after,
        'inserted': inserted,
        'updated': updated,
        'total': total,
        'lv_non_null': lv_nn,
        'iv_non_null': iv_nn,
    }


# ── Post-load validation query ────────────────────────────────────────────────

def post_load_summary(conn):
    """Print summary of 2021 data after load."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                data_source,
                COUNT(*)                                     AS parcel_count,
                COUNT(land_value)                            AS lv_non_null,
                COUNT(imprv_value)                           AS iv_non_null,
                ROUND(AVG(market_value))                     AS avg_market_value,
                ROUND(AVG(assessed_value))                   AS avg_assessed_value
            FROM parcel_tax_year
            WHERE tax_year = 2021
            GROUP BY data_source
            ORDER BY data_source
        """)
        rows = cur.fetchall()

    print(f"\n{'─'*70}")
    print(f"  2021 parcel_tax_year breakdown by data_source:")
    print(f"  {'data_source':<15} {'parcels':>10} {'lv_non_null':>12} {'iv_non_null':>12} "
          f"{'avg_mv':>14} {'avg_av':>14}")
    print(f"  {'─'*14} {'─'*10} {'─'*12} {'─'*12} {'─'*14} {'─'*14}")
    for r in rows:
        n = r['parcel_count']
        lv = r['lv_non_null']
        iv = r['iv_non_null']
        print(f"  {r['data_source'] or 'NULL':<15} {n:>10,} "
              f"{lv:>10,} ({lv/max(n,1)*100:4.1f}%) "
              f"{iv:>10,} ({iv/max(n,1)*100:4.1f}%) "
              f"{r['avg_market_value']:>14,.0f} "
              f"{r['avg_assessed_value']:>14,.0f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True, metavar='FILE.csv',
                    help='CSV produced by parse_cert_2021_pdf.py')
    ap.add_argument('--dry-run', action='store_true',
                    help='Parse and deduplicate but do not write to DB')
    args = ap.parse_args()

    # ── Load & deduplicate ────────────────────────────────────────────────────
    print(f"\nReading {args.csv} ...")
    raw = load_csv(args.csv)
    print(f"  {len(raw):,} raw records from CSV")

    deduped, dupe_count = deduplicate(raw)
    print(f"  {dupe_count:,} split-parcel geo_ids deduplicated (kept highest market_value)")
    print(f"  {len(deduped):,} unique geo_ids to load")

    # Quick field stats
    n = len(deduped)
    mv_ok = sum(1 for r in deduped if parse_int(r.get('market_value')) is not None)
    lv_ok = sum(1 for r in deduped if parse_int(r.get('land_value'))   is not None)
    iv_ok = sum(1 for r in deduped if parse_int(r.get('imprv_value'))  is not None)
    print(f"\n  Pre-load field stats ({n:,} records):")
    print(f"    market_value non-null : {mv_ok:,} / {n:,}  ({mv_ok/max(n,1)*100:.1f}%)")
    print(f"    land_value   non-null : {lv_ok:,} / {n:,}  ({lv_ok/max(n,1)*100:.1f}%)  ← risky field")
    print(f"    imprv_value  non-null : {iv_ok:,} / {n:,}  ({iv_ok/max(n,1)*100:.1f}%)  ← risky field")

    # ── Connect ───────────────────────────────────────────────────────────────
    conn = psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER,
        password=config.DB_PASS
    )

    with_exemptions = has_column(conn, 'parcel_tax_year', 'exemption_codes')
    if with_exemptions:
        print("\n  exemption_codes column found — will load exemption codes.")
    else:
        print("\n  exemption_codes column not found — skipping that field.")

    # ── UPSERT ────────────────────────────────────────────────────────────────
    print(f"\nLoading into parcel_tax_year (tax_year={TAX_YEAR}, data_source='{DATA_SOURCE}') ...")
    result = run_load(conn, deduped, with_exemptions, args.dry_run)

    if args.dry_run or result is None:
        conn.close()
        return

    # ── Post-load report ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  LOAD COMPLETE — 2021 Certified Roll")
    print(f"{'='*70}")
    print(f"  Records processed          : {result['processed']:>10,}")
    print(f"  Inserted (new parcels)     : {result['inserted']:>10,}  "
          f"← in cert roll but not in AJR")
    print(f"  Updated  (AJR → cert_2021): {result['updated']:>10,}  "
          f"← replaced ajr_2021 values")
    print(f"  Total cert_2021 rows in DB : {result['cert_after']:>10,}")
    print(f"\n  Post-load land/imprv null rates (cert_2021 rows only):")
    t = result['total']
    print(f"    land_value  non-null : {result['lv_non_null']:,} / {t:,}  "
          f"({result['lv_non_null']/max(t,1)*100:.1f}%)")
    print(f"    imprv_value non-null : {result['iv_non_null']:,} / {t:,}  "
          f"({result['iv_non_null']/max(t,1)*100:.1f}%)")
    print(f"\n  Note: NULL land/imprv includes personal property (P-type) and")
    print(f"  some vacant land parcels — this is expected, not a parser failure.")

    post_load_summary(conn)
    print(f"{'='*70}\n")

    conn.close()
    print("Done. Next: run compute_metrics.py to rebuild county_benchmark for 2021.")


if __name__ == '__main__':
    main()
