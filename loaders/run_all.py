"""
Master loader — runs all data loaders in the correct order.

Usage:
    python loaders/run_all.py [--schema-only] [--skip-ajr] [--skip-cert] [--skip-tax]
                              [--skip-metrics] [--skip-pir] [--skip-gate]

Order matters:
  1. Schema         — create tables/indexes (including Phase 2 parcel_metrics, county_benchmark)
  2. Tax rates      — small, fast; useful for validation early
  3. Certified 25   — writes parcel (identity) + prop_unit/prop_unit_tax_year (2025 values)
  4. AJR 2021-24    — writes parcel + prop_unit/prop_unit_tax_year for historic years
  5. Rollup         — parcel_rollup.rollup_all_years() + repair_prop_id() (Migration M2,
                      SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.3). Each loader above already
                      calls parcel_rollup.run() for its own tax_year at the end of its own
                      load() — this is a second, explicit, whole-history pass so a fresh
                      `run_all.py` run always leaves parcel_tax_year fully consistent with
                      prop_unit_tax_year regardless of loader-internal call order. Idempotent
                      (re-running is a no-op once already consistent), so this redundancy is
                      free, not wasted work.
  6. Ingest Gate    — loaders/ingest_gate.py's G1-G6 checks (§4.2) against the 2025 Certified
                      and 2026 Preliminary source files (the two EARS-format sources this
                      migration's G1 file-scan check supports). See the gate step below for
                      an explicit disclosure of which sources are NOT gated yet (AJR CSVs,
                      2021 cert, PIR supplemental) — out of this migration's scope.
  7. TaxCurrent     — billing data + backfills owner name
  8. TaxDelinquent  — delinquency flags
  9. compute_metrics — Phase 2 derived insight layer (parcel_metrics, county_benchmark).
                      Per spec §4.1, this step is now GATED: it only runs if every gate
                      check that DID run (step 6) passed. If the gate wasn't run for a
                      source (--skip-gate, or a source outside gate coverage) that source's
                      correctness is simply unverified by this mechanism, not blocking.
 10. PIR TCAD       — Step 5: backfill taxable_value, land_value, imprv_value for 2021-2024
                      (runs only when files are present in config.PIR_TCAD_FILES)
 11. PIR Billing    — Step 5: historical billing for 2021-2024, flips coverage_level to 'full'
                      (runs only when files are present in config.PIR_BILLING_FILES)

KNOWN GAP (disclosed per this migration's AC8 honesty requirement, not
silently fixed): load_cert_2021.py, load_exemptions.py, load_pir_tcad.py,
and validate_coverage_sql.py also write parcel_tax_year's value columns
directly and were NOT refactored by this migration (the brief named only
load_certified_2025.py, load_2026_preliminary.py,
load_certified_historical.py, load_ajr.py, and this file). They are not
called by run_all.py's main path above except load_pir_tcad.py (step 10,
gated on config.PIR_TCAD_FILES being populated, which it is not in this
sandbox). See verify_rollup_canonical.py's ALLOWLIST for the
mechanical record of this gap.
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
from loaders.load_2026_preliminary import PRELIM_DIR  # path only — run_all.py does not call this loader (unchanged from before this migration; it's run standalone)
from loaders.load_tax_current    import load as load_tax, load_delinquent
from loaders.compute_metrics     import (
    analyze_threshold, compute_parcel_metrics, compute_county_benchmarks
)
from loaders import ingest_gate
import parcel_rollup
import tax_billing_rollup
import config


def reset_parcel_tables(conn):
    """Truncate parcel and parcel_tax_year (and the M2 unit-layer tables) so we can reload cleanly.

    TAX-BILLING-REKEY-3: tax_billing_account/_account_entity/_portal_scrape
    added -- these are the real, unit-grain ingestion tables now; truncating
    tax_billing/tax_billing_entity alone (their derived-rollup targets)
    without also truncating the source tables would leave stale
    account-grain rows behind for the next load to (harmlessly, since every
    write is its own ON CONFLICT target) upsert over, but would make a
    --reset run's row counts misleading.
    """
    print("  Truncating parcel, parcel_tax_year, prop_unit, prop_unit_tax_year, "
          "tax_billing_account(_entity), tax_billing_portal_scrape, ingest_audit…")
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE tax_billing_account_entity, tax_billing_account, "
            "tax_billing_portal_scrape, tax_billing_entity, tax_billing, tax_delinquent, "
            "ingest_audit, prop_unit_tax_year, parcel_tax_year, prop_unit, parcel CASCADE"
        )
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
    parser.add_argument("--skip-gate",    action="store_true",
                        help="Skip the M2 Ingestion Conservation Gate (G1-G6) — compute_metrics "
                             "then runs unconditionally, same as before this migration")
    parser.add_argument("--reset",         action="store_true",
                        help="Truncate parcel tables before loading")
    args = parser.parse_args()

    t_start = time.time()
    print("=" * 60)
    print("Travis County Property Tax — Data Loader")
    print("=" * 60)

    conn = get_conn()

    print("\n[1/11] Applying schema…")
    execute_schema(conn)

    if args.schema_only:
        print("Schema-only mode — done.")
        conn.close()
        return

    if args.reset:
        print("\n[reset] Clearing parcel tables…")
        reset_parcel_tables(conn)

    print("\n[2/11] Tax rates…")
    load_rates(conn)

    # Certified MUST load before AJR so prop_unit (identity) is populated —
    # AJR's 2021-format prop_id→geo_id lookup reads prop_unit.
    if not args.skip_cert:
        print("\n[3/11] 2025 Certified Export…")
        load_cert(conn)
    else:
        print("\n[3/11] Certified Export skipped.")

    if not args.skip_ajr:
        print("\n[4/11] AJR files 2021-2024…")
        load_ajr(conn)
    else:
        print("\n[4/11] AJR skipped.")

    print("\n[5/11] parcel_rollup — full-history pass (idempotent safety net; "
          "each loader above already rolled up its own tax_year)…")
    rollup_result = parcel_rollup.run(conn)
    print(f"    → prop_id repaired: {rollup_result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {rollup_result['parcel_tax_year_rows']:,}")

    gate_passed = True
    gate_ran_any = False
    if not args.skip_gate:
        print("\n[6/11] Ingestion Conservation Gate (G1-G6)…")
        cert_prop = os.path.join(config.CERT_DIR, "PROP.TXT") if os.path.isdir(config.CERT_DIR) else None
        cert_prop_ent = os.path.join(config.CERT_DIR, "PROP_ENT.TXT") if os.path.isdir(config.CERT_DIR) else None
        if cert_prop and os.path.exists(cert_prop):
            summary = ingest_gate.gather_and_run(conn, "certified_2025", 2025, cert_prop, cert_prop_ent)
            gate_ran_any = True
            gate_passed = gate_passed and summary["passed"]
            for code, result in summary["checks"].items():
                print(f"    {code}: {'PASS' if result[0] else 'FAIL'} — {result[1]}")
        else:
            print("    2025 Certified source files not found — skipping gate for this source.")

        prelim_prop = os.path.join(PRELIM_DIR, "PROP.TXT") if os.path.isdir(PRELIM_DIR) else None
        prelim_prop_ent = os.path.join(PRELIM_DIR, "PROP_ENT.TXT") if os.path.isdir(PRELIM_DIR) else None
        if prelim_prop and os.path.exists(prelim_prop):
            summary = ingest_gate.gather_and_run(conn, "preliminary_2026", 2026, prelim_prop, prelim_prop_ent)
            gate_ran_any = True
            gate_passed = gate_passed and summary["passed"]
            for code, result in summary["checks"].items():
                print(f"    {code}: {'PASS' if result[0] else 'FAIL'} — {result[1]}")
        else:
            print("    2026 Preliminary source files not found — skipping gate for this source.")

        if not gate_ran_any:
            print("    No gate-able sources found — gate ran zero checks this run.")
        print("    NOTE: AJR (2021-2024), 2021 Certified, and PIR supplemental sources "
              "are NOT covered by this gate yet — see this file's module docstring.")
    else:
        print("\n[6/11] Ingestion Conservation Gate skipped (--skip-gate).")

    if not args.skip_tax:
        print("\n[7/11] TaxCurOpenData (billing)…")
        load_tax(conn)
        # TAX-BILLING-REKEY-3: load_tax() (loaders/load_tax_current.py's
        # load()) now writes tax_billing_account/_account_entity, not
        # tax_billing/tax_billing_entity directly -- roll up here, since
        # run_all.py calls load() directly (not that file's own __main__,
        # which has its own rollup call for standalone runs).
        rollup_result = tax_billing_rollup.run(conn, tax_year=2025)
        print(f"  tax_billing_rollup (2025): {rollup_result['tax_billing_rows']:,} tax_billing rows, "
              f"{rollup_result['tax_billing_entity_rows']:,} tax_billing_entity rows")
        print("\n[8/11] TaxDelqOpenData (delinquent)…")
        load_delinquent(conn)
    else:
        print("\n[7-8/11] Tax billing skipped.")

    if not args.skip_metrics:
        if gate_ran_any and not gate_passed:
            print("\n[9/11] compute_metrics SKIPPED — Ingestion Conservation Gate failed "
                  "(see G1-G6 results above). Fix the underlying data issue and re-run "
                  "before trusting parcel_metrics/county_benchmark. Use --skip-gate to "
                  "force compute_metrics to run anyway (NOT recommended).")
        else:
            print("\n[9/11] Phase 2: compute_metrics…")
            analyze_threshold(conn)
            compute_parcel_metrics(conn)
            compute_county_benchmarks(conn)
    else:
        print("\n[9/11] compute_metrics skipped (--skip-metrics).")

    # ── Step 5: PIR loaders (gated on files being present in config) ──────────
    if not args.skip_pir:
        if config.PIR_TCAD_FILES:
            print("\n[10/11] Step 5: PIR TCAD supplemental fields…")
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
            print("\n[10/11] PIR TCAD: no files configured yet — skipping.")

        if config.PIR_BILLING_FILES:
            print("\n[11/11] Step 5: PIR historical billing…")
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
                # TAX-BILLING-REKEY-3: roll up before update_coverage_level
                # (JOINs tax_billing directly) -- pir_billing_load_file()
                # now writes tax_billing_account/_account_entity only.
                for yr in sorted(all_years):
                    rollup_result = tax_billing_rollup.run(conn, tax_year=yr)
                    print(f"  tax_billing_rollup ({yr}): {rollup_result['tax_billing_rows']:,} "
                          f"tax_billing rows, {rollup_result['tax_billing_entity_rows']:,} "
                          f"tax_billing_entity rows")
                update_coverage_level(conn, all_years)
        else:
            print("\n[11/11] PIR billing: no files configured yet — skipping.")
    else:
        print("\n[10-11/11] PIR loaders skipped (--skip-pir).")

    conn.close()

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"All done in {elapsed/60:.1f} minutes.")
    print(f"{'='*60}")

    # Quick sanity counts
    print("\nRow counts:")
    conn2 = get_conn()
    with conn2.cursor() as cur:
        for tbl in ("parcel", "parcel_tax_year", "prop_unit", "prop_unit_tax_year",
                    "tax_billing", "county_tax_rate", "parcel_metrics", "county_benchmark",
                    "ingest_audit"):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            n = cur.fetchone()[0]
            print(f"  {tbl:30s} {n:>10,}")
    conn2.close()


if __name__ == "__main__":
    main()
