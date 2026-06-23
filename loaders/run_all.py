"""
Master loader — runs all data loaders in the correct order.

Usage:
    python loaders/run_all.py [--schema-only] [--skip-ajr] [--skip-cert] [--skip-tax]
                              [--skip-metrics] [--skip-pir]

Order matters:
  1. Schema         — create tables/indexes (including Phase 2 parcel_metrics, county_benchmark)
  2. Tax rates      — small, fast; useful for validation early
  3. Certified 25   — upserts parcel (adds prop_type_cd, owner) + 2025 values
  4. AJR 2021-24    — populates parcel + parcel_tax_year for historic years
  5. TaxCurrent     — billing data + backfills owner name
  6. TaxDelinquent  — delinquency flags
  7. compute_metrics — Phase 2 derived insight layer (parcel_metrics, county_benchmark)
  8. PIR TCAD       — Step 5: backfill taxable_value, land_value, imprv_value for 2021-2024
                      (runs only when files are present in config.PIR_TCAD_FILES)
  9. PIR Billing    — Step 5: historical billing for 2021-2024, flips coverage_level to 'full'
                      (runs only when files are present in config.PIR_BILLING_FILES)
"""
import sys
import os
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.db                  import get_conn, execute_schema
from loaders.load_tax_rates      import load as load_rates
from loaders.load_ajr            import load as load_ajr
from loaders.load_certified_2025 import load as load_cert
from loaders.load_tax_current    import load as load_tax, load_delinquent
from loaders.compute_metrics     import (
    analyze_threshold, compute_parcel_metrics, compute_county_benchmarks
)
import config


def reset_parcel_tables(conn):
    """Truncate parcel and parcel_tax_year so we can reload cleanly."""
    print("  Truncating parcel and parcel_tax_year…")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE tax_billing_entity, tax_billing, tax_delinquent, parcel_tax_year, parcel CASCADE")
    conn.commit()
    print("  Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-only",   action="store_true")
    parser.add_argument("--skip-ajr",      action="store_true")
    parser.add_argument("--skip-cert",     action="store_true")
    parser.add_argument("--skip-tax",      action="store_true")
    parser.add_argument("--skip-metrics",  action="store_true",
                        help="Skip Phase 2 compute_metrics step")
    parser.add_argument("--skip-pir",     action="store_true",
                        help="Skip Step 5 PIR loaders (TCAD supplemental + historical billing)")
    parser.add_argument("--reset",         action="store_true",
                        help="Truncate parcel tables before loading")
    args = parser.parse_args()

    t_start = time.time()
    print("=" * 60)
    print("Travis County Property Tax — Data Loader")
    print("=" * 60)

    conn = get_conn()

    print("\n[1/9] Applying schema…")
    execute_schema(conn)

    if args.schema_only:
        print("Schema-only mode — done.")
        conn.close()
        return

    if args.reset:
        print("\n[reset] Clearing parcel tables…")
        reset_parcel_tables(conn)

    print("\n[2/9] Tax rates…")
    load_rates(conn)

    # Certified MUST load before AJR so prop_id → geo_id lookup works
    if not args.skip_cert:
        print("\n[3/9] 2025 Certified Export…")
        load_cert(conn)
    else:
        print("\n[3/9] Certified Export skipped.")

    if not args.skip_ajr:
        print("\n[4/9] AJR files 2021-2024…")
        load_ajr(conn)
    else:
        print("\n[4/9] AJR skipped.")

    if not args.skip_tax:
        print("\n[5/9] TaxCurOpenData (billing)…")
        load_tax(conn)
        print("\n[6/9] TaxDelqOpenData (delinquent)…")
        load_delinquent(conn)
    else:
        print("\n[5-6/9] Tax billing skipped.")

    if not args.skip_metrics:
        print("\n[7/9] Phase 2: compute_metrics…")
        analyze_threshold(conn)
        compute_parcel_metrics(conn)
        compute_county_benchmarks(conn)
    else:
        print("\n[7/9] compute_metrics skipped (--skip-metrics).")

    # ── Step 5: PIR loaders (gated on files being present in config) ──────────
    if not args.skip_pir:
        if config.PIR_TCAD_FILES:
            print("\n[8/9] Step 5: PIR TCAD supplemental fields…")
            # Import lazily so missing files don't error on every run
            from loaders.load_pir_tcad import load_year as pir_tcad_load_year, build_pid_lookup
            pid_lookup = build_pid_lookup(conn)
            for year, path in sorted(config.PIR_TCAD_FILES.items()):
                import os
                if os.path.exists(path):
                    pir_tcad_load_year(conn, year, path, pid_lookup)
                else:
                    print(f"  WARNING: PIR TCAD {year} file not found: {path}")
        else:
            print("\n[8/9] PIR TCAD: no files configured yet — skipping.")

        if config.PIR_BILLING_FILES:
            print("\n[9/9] Step 5: PIR historical billing…")
            from loaders.load_pir_billing import load_file as pir_billing_load_file, update_coverage_level
            all_years = set()
            for key, path in sorted(config.PIR_BILLING_FILES.items()):
                import os
                if os.path.exists(path):
                    _, years = pir_billing_load_file(conn, path)
                    all_years |= years
                else:
                    print(f"  WARNING: PIR billing file not found: {path}")
            if all_years:
                update_coverage_level(conn, all_years)
        else:
            print("\n[9/9] PIR billing: no files configured yet — skipping.")
    else:
        print("\n[8-9/9] PIR loaders skipped (--skip-pir).")

    conn.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"All done in {elapsed/60:.1f} minutes.")
    print(f"{'='*60}")

    # Quick sanity counts
    print("\nRow counts:")
    conn2 = get_conn()
    with conn2.cursor() as cur:
        for tbl in ("parcel", "parcel_tax_year", "tax_billing", "county_tax_rate",
                    "parcel_metrics", "county_benchmark"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            n = cur.fetchone()[0]
            print(f"  {tbl:30s} {n:>10,}")
    conn2.close()


if __name__ == "__main__":
    main()
