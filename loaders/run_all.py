"""
Master loader — runs all data loaders in the correct order.

Usage:
    python loaders/run_all.py [--schema-only] [--skip-ajr] [--skip-cert] [--skip-tax]

Order matters:
  1. Schema       — create tables/indexes
  2. Tax rates    — small, fast; useful for validation early
  3. AJR 2021-24  — populates parcel + parcel_tax_year for historic years
  4. Certified 25 — upserts parcel (adds prop_type_cd, owner) + 2025 values
  5. TaxCurrent   — billing data + backfills owner name
  6. TaxDelinquent— delinquency flags
"""
import sys
import os
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.db             import get_conn, execute_schema
from loaders.load_tax_rates  import load as load_rates
from loaders.load_ajr        import load as load_ajr
from loaders.load_certified_2025 import load as load_cert
from loaders.load_tax_current    import load as load_tax, load_delinquent


def reset_parcel_tables(conn):
    """Truncate parcel and parcel_tax_year so we can reload cleanly."""
    print("  Truncating parcel and parcel_tax_year…")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE tax_billing_entity, tax_billing, tax_delinquent, parcel_tax_year, parcel CASCADE")
    conn.commit()
    print("  Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-only",  action="store_true")
    parser.add_argument("--skip-ajr",     action="store_true")
    parser.add_argument("--skip-cert",    action="store_true")
    parser.add_argument("--skip-tax",     action="store_true")
    parser.add_argument("--reset",        action="store_true",
                        help="Truncate parcel tables before loading")
    args = parser.parse_args()

    t_start = time.time()
    print("=" * 60)
    print("Travis County Property Tax — Data Loader")
    print("=" * 60)

    conn = get_conn()

    print("\n[1/6] Applying schema…")
    execute_schema(conn)

    if args.schema_only:
        print("Schema-only mode — done.")
        conn.close()
        return

    if args.reset:
        print("\n[reset] Clearing parcel tables…")
        reset_parcel_tables(conn)

    print("\n[2/6] Tax rates…")
    load_rates(conn)

    # Certified MUST load before AJR so prop_id → geo_id lookup works
    if not args.skip_cert:
        print("\n[3/6] 2025 Certified Export…")
        load_cert(conn)
    else:
        print("\n[3/6] Certified Export skipped.")

    if not args.skip_ajr:
        print("\n[4/6] AJR files 2021-2024…")
        load_ajr(conn)
    else:
        print("\n[4/6] AJR skipped.")

    if not args.skip_tax:
        print("\n[5/6] TaxCurOpenData (billing)…")
        load_tax(conn)
        print("\n[6/6] TaxDelqOpenData (delinquent)…")
        load_delinquent(conn)
    else:
        print("\n[5-6/6] Tax billing skipped.")

    conn.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"All done in {elapsed/60:.1f} minutes.")
    print(f"{'='*60}")

    # Quick sanity counts
    print("\nRow counts:")
    conn2 = get_conn()
    with conn2.cursor() as cur:
        for tbl in ("parcel", "parcel_tax_year", "tax_billing", "county_tax_rate"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            n = cur.fetchone()[0]
            print(f"  {tbl:30s} {n:>10,}")
    conn2.close()


if __name__ == "__main__":
    main()
