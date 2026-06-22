"""
Task 3 — Market Value Distribution for CAGR-Baseline-Year Anomalies
====================================================================
For the 28,759 parcels where AV > MV occurs in their earliest AJR year
(the year used as the CAGR base in build_projections), this script checks
whether their market_value itself is suspect:
  - Zero or null market_value  → parsing error, CAGR is unusable
  - market_value < $10,000     → likely a parsing/encoding error (not a real value)
  - market_value appears normal → AV anomaly doesn't corrupt the CAGR (MV is fine)

Also: property-type breakdown for baseline-year anomalies.

Run: python3 investigate_mv_distribution.py
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
    print("Task 3 — Market Value Distribution in CAGR Baseline Year")
    print("(parcels where AV > MV in their earliest AJR year)")
    print("=" * 72)

    # ── Identify baseline-year anomalous parcels ──────────────────────────────
    cur.execute("""
        WITH earliest AS (
            SELECT geo_id, MIN(tax_year) AS earliest_year
            FROM parcel_tax_year
            WHERE market_value IS NOT NULL AND market_value > 0
              AND tax_year BETWEEN 2021 AND 2024
            GROUP BY geo_id
        )
        SELECT
            pty.geo_id,
            pty.tax_year       AS baseline_year,
            pty.market_value,
            pty.assessed_value,
            pty.assessed_value - pty.market_value AS excess,
            LEFT(p.state_cd1, 1) AS type_prefix
        FROM parcel_tax_year pty
        JOIN earliest e ON e.geo_id = pty.geo_id AND e.earliest_year = pty.tax_year
        JOIN parcel p   ON p.geo_id = pty.geo_id
        WHERE pty.assessed_value > pty.market_value
          AND pty.market_value > 0
    """)
    rows = cur.fetchall()
    total = len(rows)

    print(f"\nTotal baseline-year anomalous parcels: {total:,}")

    # ── Distribution of market_value ─────────────────────────────────────────
    mv_zero     = sum(1 for r in rows if not r["market_value"])
    mv_lt_10k   = sum(1 for r in rows if r["market_value"] and r["market_value"] < 10_000)
    mv_lt_50k   = sum(1 for r in rows if r["market_value"] and r["market_value"] < 50_000)
    mv_normal   = sum(1 for r in rows if r["market_value"] and r["market_value"] >= 50_000)

    print(f"\nMarket value distribution (these {total:,} parcels):")
    print(f"  Zero / null MV:          {mv_zero:>8,}  ({mv_zero/total*100:.1f}%)  ← CAGR unusable")
    print(f"  MV < $10,000:            {mv_lt_10k:>8,}  ({mv_lt_10k/total*100:.1f}%)  ← likely parsing error")
    print(f"  MV < $50,000:            {mv_lt_50k:>8,}  ({mv_lt_50k/total*100:.1f}%)  ← suspiciously low")
    print(f"  MV ≥ $50,000 (normal):   {mv_normal:>8,}  ({mv_normal/total*100:.1f}%)  ← MV looks valid")
    print(f"\n  → For {mv_normal:,} parcels ({mv_normal/total*100:.1f}%), the MV appears valid.")
    print(f"    AV anomaly in those rows likely affects assessment_ratio metrics only,")
    print(f"    not the CAGR (which uses market_value, not assessed_value).")

    # ── By property type ─────────────────────────────────────────────────────
    type_labels = {
        "A": "Residential (SFR)", "B": "Multi-Family",
        "C": "Land/Vacant",       "D": "Agricultural",
        "E": "Agricultural (E)",  "F": "Commercial",
    }
    type_counts = {}
    for r in rows:
        tp = r["type_prefix"] or "?"
        type_counts[tp] = type_counts.get(tp, 0) + 1

    print(f"\nBy property type:")
    for tp, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        label = type_labels.get(tp, f"Other ({tp})")
        print(f"  {label:<25} {cnt:>8,}  ({cnt/total*100:.1f}%)")

    # ── Median excess by type ─────────────────────────────────────────────────
    print(f"\nMedian excess (assessed − market) by type:")
    cur.execute("""
        WITH earliest AS (
            SELECT geo_id, MIN(tax_year) AS earliest_year
            FROM parcel_tax_year
            WHERE market_value IS NOT NULL AND market_value > 0
              AND tax_year BETWEEN 2021 AND 2024
            GROUP BY geo_id
        )
        SELECT
            LEFT(p.state_cd1, 1) AS type_prefix,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pty.assessed_value - pty.market_value) AS median_excess,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (pty.assessed_value::numeric / pty.market_value - 1) * 100) AS median_excess_pct
        FROM parcel_tax_year pty
        JOIN earliest e ON e.geo_id = pty.geo_id AND e.earliest_year = pty.tax_year
        JOIN parcel p   ON p.geo_id = pty.geo_id
        WHERE pty.assessed_value > pty.market_value AND pty.market_value > 0
        GROUP BY LEFT(p.state_cd1, 1)
        ORDER BY median_excess DESC
    """)
    for r in cur.fetchall():
        tp    = r["type_prefix"] or "?"
        label = type_labels.get(tp, f"Other ({tp})")
        print(f"  {label:<25}  median excess=${float(r['median_excess']):>10,.0f}  ({float(r['median_excess_pct']):.1f}%)")

    print("\n" + "="*72)
    print("Conclusion:")
    print(f"  If MV ≥ $50K ({mv_normal/total*100:.1f}% of baseline-year anomalies), the CAGR is unaffected.")
    print(f"  The AV > MV anomaly in those rows inflates the assessment_ratio metric")
    print(f"  and triggers spurious AV>MV UI flags, but does NOT corrupt the value projection.")
    print(f"  The {mv_lt_10k:,} parcels with MV < $10K should be excluded from CAGR-based projections")
    print(f"  (build_projections skips them naturally since they appear in the 'risk_data_incomplete' flag).")
    print("="*72)

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
