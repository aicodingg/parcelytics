"""
Projection Anomaly Investigation — assessed_value > market_value in AJR years
==============================================================================
Context
-------
build_projections() computes value CAGR from earliest→2025 market values.
When assessed_value > market_value exists in the AJR history for a parcel,
the assessment ratio calculation in compute_metrics is skewed (assessment
ratio > 100%), and the parcel's individual CAGR-based projection may be
slightly off if that anomalous year happens to be the "earliest" year in the
span calculation.

This is INVESTIGATION ONLY. No calculation logic is changed.
Run:  python3 investigate_av_anomaly.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
import config
import psycopg2
import psycopg2.extras


def get_db():
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS,
    )


def run():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("=" * 72)
    print("AJR Anomaly: assessed_value > market_value (2021–2024)")
    print("=" * 72)

    # ── 1. Count by year ──────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            tax_year,
            COUNT(DISTINCT geo_id) AS affected_parcels,
            ROUND(AVG(assessed_value - market_value)::numeric, 0) AS avg_excess,
            MAX(assessed_value - market_value) AS max_excess
        FROM parcel_tax_year
        WHERE tax_year BETWEEN 2021 AND 2024
          AND assessed_value > market_value
          AND market_value  > 0
          AND assessed_value > 0
        GROUP BY tax_year
        ORDER BY tax_year
    """)
    rows = cur.fetchall()

    print("\nBy year:")
    print(f"  {'Year':<8} {'Parcels':>10} {'Avg excess':>14} {'Max excess':>14}")
    print(f"  {'-'*8} {'-'*10} {'-'*14} {'-'*14}")
    for r in rows:
        print(f"  {r['tax_year']:<8} {r['affected_parcels']:>10,} "
              f"${r['avg_excess']:>12,.0f} ${r['max_excess']:>12,.0f}")

    # ── 2. Total unique parcels affected across any AJR year ─────────────────
    cur.execute("""
        SELECT COUNT(DISTINCT geo_id) AS total_affected
        FROM parcel_tax_year
        WHERE tax_year BETWEEN 2021 AND 2024
          AND assessed_value > market_value
          AND market_value  > 0
          AND assessed_value > 0
    """)
    total = cur.fetchone()["total_affected"]

    cur.execute("SELECT COUNT(DISTINCT geo_id) FROM parcel")
    total_parcels = cur.fetchone()["count"]

    pct = total / total_parcels * 100 if total_parcels else 0
    print(f"\nTotal unique parcels with AV > MV in any AJR year: {total:,}")
    print(f"Total parcels in dataset:                           {total_parcels:,}")
    print(f"Share affected:                                     {pct:.2f}%")

    # ── 3. Property type breakdown ────────────────────────────────────────────
    cur.execute("""
        SELECT
            LEFT(p.state_cd1, 1) AS type_prefix,
            COUNT(DISTINCT pty.geo_id) AS affected
        FROM parcel_tax_year pty
        JOIN parcel p ON p.geo_id = pty.geo_id
        WHERE pty.tax_year BETWEEN 2021 AND 2024
          AND pty.assessed_value > pty.market_value
          AND pty.market_value  > 0
          AND pty.assessed_value > 0
        GROUP BY LEFT(p.state_cd1, 1)
        ORDER BY affected DESC
    """)
    type_rows = cur.fetchall()

    type_labels = {
        "A": "Residential (SFR)", "B": "Multi-Family",
        "C": "Land/Vacant",      "D": "Agricultural",
        "E": "Agricultural",     "F": "Commercial",
    }
    print("\nBy property type:")
    for r in type_rows:
        label = type_labels.get(r["type_prefix"], f"Other ({r['type_prefix']})")
        print(f"  {label:<25} {r['affected']:>8,}")

    # ── 4. Worst examples ─────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            pty.geo_id,
            p.situs_address,
            p.state_cd1,
            pty.tax_year,
            pty.market_value,
            pty.assessed_value,
            (pty.assessed_value - pty.market_value) AS excess,
            ROUND((pty.assessed_value::numeric / pty.market_value - 1) * 100, 1) AS excess_pct
        FROM parcel_tax_year pty
        JOIN parcel p ON p.geo_id = pty.geo_id
        WHERE pty.tax_year BETWEEN 2021 AND 2024
          AND pty.assessed_value > pty.market_value
          AND pty.market_value  > 0
          AND pty.assessed_value > 0
        ORDER BY excess DESC
        LIMIT 10
    """)
    examples = cur.fetchall()

    print("\nTop 10 by absolute excess (AV − MV):")
    print(f"  {'geo_id':<12} {'Year':<6} {'Market':>12} {'Assessed':>12} {'Excess':>12} {'%':>7}  Address")
    print(f"  {'-'*12} {'-'*6} {'-'*12} {'-'*12} {'-'*12} {'-'*7}  {'-'*30}")
    for r in examples:
        addr = (r["situs_address"] or "—")[:35]
        print(f"  {r['geo_id']:<12} {r['tax_year']:<6} "
              f"${r['market_value']:>10,.0f} ${r['assessed_value']:>10,.0f} "
              f"${r['excess']:>10,.0f} {r['excess_pct']:>6.1f}%  {addr}")

    # ── 5. Projection impact: does AV>MV year happen to be the "earliest" year?
    # build_projections() uses the earliest year in hist (sorted by tax_year)
    # where market_value is not null. If the anomaly is in 2021 (the usual
    # earliest), it doesn't directly affect the CAGR (CAGR uses market_value,
    # not assessed_value). But if market_value is also wrong in that year, CAGR
    # is skewed. Let's check parcels where the EARLIEST AJR year with market_value
    # also has AV>MV — those are the ones most likely to have skewed CAGRs.
    cur.execute("""
        WITH earliest AS (
            SELECT geo_id, MIN(tax_year) AS earliest_year
            FROM parcel_tax_year
            WHERE market_value IS NOT NULL AND market_value > 0
              AND tax_year BETWEEN 2021 AND 2024
            GROUP BY geo_id
        )
        SELECT COUNT(DISTINCT pty.geo_id) AS n_earliest_anomaly
        FROM parcel_tax_year pty
        JOIN earliest e ON e.geo_id = pty.geo_id AND e.earliest_year = pty.tax_year
        WHERE pty.assessed_value > pty.market_value
          AND pty.market_value  > 0
    """)
    n_earliest = cur.fetchone()["n_earliest_anomaly"]
    print(f"\nParcels where anomaly is in the earliest AJR year")
    print(f"(directly affects CAGR baseline in build_projections):")
    print(f"  {n_earliest:,} parcels")
    print(f"  Note: CAGR uses market_value only, not assessed_value.")
    print(f"  An AV>MV anomaly skews CAGR only if the market_value itself is wrong")
    print(f"  in that year (a separate concern from the AV field).")

    print("\n" + "=" * 72)
    print("Summary for future projection-validation pass:")
    print(f"  - {total:,} parcels ({pct:.1f}%) have AV > MV in at least one AJR year")
    print(f"  - {n_earliest:,} of those have the anomaly in their CAGR-baseline year")
    print(f"  - The anomaly is most prevalent in {max(rows, key=lambda r: r['affected_parcels'])['tax_year']}")
    print(f"    ({max(rows, key=lambda r: r['affected_parcels'])['affected_parcels']:,} parcels that year)")
    print("  - No calculation logic was changed by this script.")
    print("=" * 72)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
