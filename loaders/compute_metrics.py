"""
compute_metrics.py — Phase 2 computed insight layer.

Populates parcel_metrics and county_benchmark from Phase 1 source data.
Run after any data load that updates parcel_tax_year or tax_billing.

Usage:
    python3 loaders/compute_metrics.py            # full compute + brief analysis
    python3 loaders/compute_metrics.py --analyze  # distribution analysis only, no compute

Data Integrity Standard (Part 2 — binding for all phases):
  - NULL in a parcel_metrics field = "Not Available" — never a zero or blank
  - coverage_level = 'full'       → 2025 row: market + assessed + real billing (Verified)
  - coverage_level = 'value_only' → 2021–2024: market + assessed only; tax fields Not Available
  - has_tax_data = FALSE          → never show tax metrics for that row in UI
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
import psycopg2.extras

COMPUTATION_VERSION = "2.0"

# ── Risk threshold ──────────────────────────────────────────────────────────────
# Set at 75% based on actual distribution across 1,401,316 parcel-year pairs:
#   p50=7.1%  p75=32.3%  p90=59.4%  p95=72.0%  p99=292.9%
#   > 50%: 15.9% of pairs  > 75%: 4.5%  > 100%: 2.6%
#
# 75% sits just above the p90 — flags genuinely unusual moves without drowning
# investors in noise. 50% (p90) flagged 1 in 6 parcel-years, too broad to be
# actionable. 100% was considered but is only 2.6% and misses real 75-99% jumps.
LARGE_JUMP_THRESHOLD_PCT = 75.0

# State code prefix → benchmark property type label
# Matches the display mapping used in property.html
TYPE_GROUPS = [
    (["A"],      "Residential",  "A"),
    (["B"],      "Multi-Family", "B"),
    (["C"],      "Land/Vacant",  "C"),
    (["D", "E"], "Agricultural", "D/E"),
    (["F"],      "Commercial",   "F"),
]


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _like_clause(prefixes):
    """Build a LIKE ANY(...) clause for state_cd1 prefix matching."""
    patterns = ", ".join(f"'{p}%'" for p in prefixes)
    return f"p.state_cd1 LIKE ANY(ARRAY[{patterns}])"


# ── Step 4: Threshold distribution analysis ─────────────────────────────────────
def analyze_threshold(conn):
    print("\n" + "=" * 60)
    print("  Step 4 — Risk Threshold Distribution Analysis")
    print("=" * 60)
    print("  Computing YoY market value changes across all parcel-years…")
    t0 = time.time()

    with conn.cursor() as cur:
        cur.execute("""
            WITH yoy AS (
                SELECT
                    a.geo_id,
                    a.tax_year,
                    CASE
                        WHEN b.market_value > 0
                        THEN ROUND(
                            100.0 * (a.market_value - b.market_value) / b.market_value, 2)
                        ELSE NULL
                    END AS yoy_pct
                FROM parcel_tax_year a
                JOIN parcel_tax_year b
                  ON b.geo_id = a.geo_id AND b.tax_year = a.tax_year - 1
                WHERE a.market_value > 0 AND b.market_value > 0
            )
            SELECT
                COUNT(*)                                                             AS total_pairs,
                SUM(CASE WHEN ABS(yoy_pct) > 10  THEN 1 ELSE 0 END)                AS flag_10,
                SUM(CASE WHEN ABS(yoy_pct) > 20  THEN 1 ELSE 0 END)                AS flag_20,
                SUM(CASE WHEN ABS(yoy_pct) > 30  THEN 1 ELSE 0 END)                AS flag_30,
                SUM(CASE WHEN ABS(yoy_pct) > 40  THEN 1 ELSE 0 END)                AS flag_40,
                SUM(CASE WHEN ABS(yoy_pct) > 50  THEN 1 ELSE 0 END)                AS flag_50,
                SUM(CASE WHEN ABS(yoy_pct) > 75  THEN 1 ELSE 0 END)                AS flag_75,
                SUM(CASE WHEN ABS(yoy_pct) > 100 THEN 1 ELSE 0 END)                AS flag_100,
                ROUND(MIN(yoy_pct), 1)                                               AS min_yoy,
                ROUND(MAX(yoy_pct), 1)                                               AS max_yoy,
                ROUND(AVG(yoy_pct)::NUMERIC, 2)                                      AS avg_yoy,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p50,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p75,
                ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p90,
                ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p95,
                ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p99
            FROM yoy
        """)
        r = cur.fetchone()

    total = r[0]
    print(f"\n  Total YoY parcel-year pairs:  {total:,}")
    print(f"\n  Distribution of YoY market value changes:")
    print(f"    Median (p50):  {r[11]}%")
    print(f"    p75:           {r[12]}%")
    print(f"    p90:           {r[13]}%")
    print(f"    p95:           {r[14]}%")
    print(f"    p99:           {r[15]}%")
    print(f"    Min:           {r[8]}%   Max: {r[9]}%   Avg: {r[10]}%")

    print(f"\n  Parcels flagged (|YoY| > threshold):")
    thresholds = [10, 20, 30, 40, 50, 75, 100]
    counts = [r[1], r[2], r[3], r[4], r[5], r[6], r[7]]
    for t, c in zip(thresholds, counts):
        pct_flagged = 100.0 * c / total if total else 0
        marker = "  ← current LARGE_JUMP_THRESHOLD_PCT" if t == LARGE_JUMP_THRESHOLD_PCT else ""
        print(f"    > {t:3d}%:  {c:>8,}  ({pct_flagged:.1f}%){marker}")

    print(f"\n  Elapsed: {time.time()-t0:.1f}s")
    print("=" * 60)


# ── Step 2: Compute parcel_metrics ──────────────────────────────────────────────
def compute_parcel_metrics(conn):
    print("\n[1] Computing parcel_metrics…")
    t0 = time.time()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM parcel_metrics")
    conn.commit()

    # Main insert: YoY metrics via SQL window functions
    # yoy_tax_amount_pct is NULL for all years — no historical billing exists yet
    # effective_tax_rate populated for 2025 only (real billing available)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO parcel_metrics (
                geo_id, tax_year,
                coverage_level, has_tax_data,
                yoy_market_value_pct,
                yoy_assessed_value_pct,
                yoy_tax_amount_pct,
                assessment_ratio,
                effective_tax_rate,
                risk_delinquent,
                risk_data_incomplete,
                computation_version
            )
            SELECT
                pty.geo_id,
                pty.tax_year,

                CASE WHEN pty.tax_year = 2025 THEN 'full' ELSE 'value_only' END,
                (pty.tax_year = 2025),

                -- YoY market value pct
                CASE
                    WHEN LAG(pty.market_value) OVER w > 0
                    THEN ROUND(
                        100.0 * (pty.market_value - LAG(pty.market_value) OVER w)
                        / LAG(pty.market_value) OVER w, 4)
                END,

                -- YoY assessed value pct
                CASE
                    WHEN LAG(pty.assessed_value) OVER w > 0
                    THEN ROUND(
                        100.0 * (pty.assessed_value - LAG(pty.assessed_value) OVER w)
                        / LAG(pty.assessed_value) OVER w, 4)
                END,

                -- yoy_tax_amount_pct: Not Available — no prior-year billing data exists yet
                -- Will be updated when historical billing arrives via PIR
                NULL,

                -- Assessment ratio: assessed / market (NULL if market = 0)
                CASE
                    WHEN pty.market_value > 0
                    THEN ROUND(pty.assessed_value::NUMERIC / pty.market_value, 4)
                END,

                -- Effective tax rate: real billing for 2025 only; Not Available otherwise
                CASE
                    WHEN pty.tax_year = 2025
                     AND COALESCE(tb.total_tax, 0) > 0
                     AND pty.market_value > 0
                    THEN ROUND(tb.total_tax::NUMERIC / pty.market_value, 6)
                END,

                -- Delinquency flag
                COALESCE(td.total_due > 0, FALSE),

                -- Data incomplete: market_value = 0 or NULL (known AJR anomaly)
                COALESCE(pty.market_value, 0) = 0,

                '{COMPUTATION_VERSION}'

            FROM parcel_tax_year pty
            JOIN parcel p ON p.geo_id = pty.geo_id
            LEFT JOIN tax_billing tb
              ON tb.geo_id = pty.geo_id AND tb.tax_year = pty.tax_year
            LEFT JOIN tax_delinquent td
              ON td.geo_id = pty.geo_id
            WINDOW w AS (PARTITION BY pty.geo_id ORDER BY pty.tax_year)
        """)
        n = cur.rowcount
    conn.commit()
    print(f"    Inserted {n:,} rows  ({time.time()-t0:.1f}s)")

    # Pass 2: large value jump flag
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE parcel_metrics
            SET risk_large_value_jump     = TRUE,
                risk_large_value_jump_pct = ABS(yoy_market_value_pct)
            WHERE ABS(yoy_market_value_pct) > {LARGE_JUMP_THRESHOLD_PCT}
        """)
        n_jump = cur.rowcount
    conn.commit()
    print(f"    risk_large_value_jump: {n_jump:,} rows flagged (>{LARGE_JUMP_THRESHOLD_PCT}%)  ({time.time()-t1:.1f}s)")

    # Pass 3: homestead cap expiry risk — residential only, consistent with Phase 1 guard
    # Fires when hs_cap_loss > 0 in ANY year for the parcel AND assessed < 90% of market
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE parcel_metrics pm
            SET risk_homestead_cap_expiry = TRUE
            FROM (
                SELECT DISTINCT pty.geo_id
                FROM parcel_tax_year pty
                JOIN parcel p ON p.geo_id = pty.geo_id
                WHERE p.state_cd1 LIKE 'A%'
                  AND pty.hs_cap_loss > 0
                  AND pty.market_value > 0
                  AND pty.assessed_value < pty.market_value * 0.90
            ) hs
            WHERE pm.geo_id = hs.geo_id
        """)
        n_cap = cur.rowcount
    conn.commit()
    print(f"    risk_homestead_cap_expiry: {n_cap:,} rows flagged  ({time.time()-t1:.1f}s)")

    # Pass 4: cumulative value growth (on 2025 row, from each parcel's earliest valid year)
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE parcel_metrics pm
            SET cumulative_value_growth_pct = sub.cum_pct
            FROM (
                SELECT
                    cur.geo_id,
                    ROUND(
                        100.0 * (cur.market_value - earliest.market_value)
                        / earliest.market_value, 4
                    ) AS cum_pct
                FROM parcel_tax_year cur
                JOIN (
                    -- Find earliest year with valid (non-zero, non-null) market_value per parcel
                    SELECT geo_id, MIN(tax_year) AS earliest_year
                    FROM parcel_tax_year
                    WHERE market_value > 0
                    GROUP BY geo_id
                ) mn ON mn.geo_id = cur.geo_id
                JOIN parcel_tax_year earliest
                  ON earliest.geo_id = mn.geo_id
                 AND earliest.tax_year = mn.earliest_year
                WHERE cur.tax_year = 2025
                  AND cur.market_value > 0
                  AND earliest.market_value > 0
                  AND cur.tax_year != mn.earliest_year   -- need at least 2 data points
            ) sub
            WHERE pm.geo_id = sub.geo_id AND pm.tax_year = 2025
        """)
        n_cum = cur.rowcount
    conn.commit()
    print(f"    cumulative_value_growth_pct: {n_cum:,} rows updated  ({time.time()-t1:.1f}s)")

    print(f"  → parcel_metrics done in {time.time()-t0:.1f}s total")


# ── Step 2b: Compute county_benchmark ──────────────────────────────────────────
def compute_county_benchmarks(conn):
    print("\n[2] Computing county_benchmark…")
    t0 = time.time()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM county_benchmark")
    conn.commit()

    with conn.cursor() as cur:
        for prefixes, label, prefix_key in TYPE_GROUPS:
            like_cond = _like_clause(prefixes)
            cur.execute(f"""
                INSERT INTO county_benchmark (
                    county_code, tax_year, property_type_label, state_cd1_prefix,
                    parcel_count,
                    median_market_value, p25_market_value, p75_market_value,
                    median_assessed_value, median_assessment_ratio,
                    median_yoy_value_change_pct
                )
                SELECT
                    'TRAVIS',
                    pty.tax_year,
                    %s,
                    %s,
                    COUNT(*),
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY pty.assessed_value)::BIGINT,
                    ROUND(
                        PERCENTILE_CONT(0.50) WITHIN GROUP (
                            ORDER BY CASE WHEN pty.market_value > 0
                                THEN pty.assessed_value::NUMERIC / pty.market_value
                                ELSE NULL END
                        )::NUMERIC, 4),
                    ROUND(
                        PERCENTILE_CONT(0.50) WITHIN GROUP (
                            ORDER BY pm.yoy_market_value_pct
                        )::NUMERIC, 4)
                FROM parcel_tax_year pty
                JOIN parcel p ON p.geo_id = pty.geo_id
                LEFT JOIN parcel_metrics pm
                  ON pm.geo_id = pty.geo_id AND pm.tax_year = pty.tax_year
                WHERE {like_cond}
                  AND pty.market_value > 0
                GROUP BY pty.tax_year
                ON CONFLICT (county_code, tax_year, property_type_label) DO UPDATE
                    SET parcel_count                = EXCLUDED.parcel_count,
                        median_market_value         = EXCLUDED.median_market_value,
                        p25_market_value            = EXCLUDED.p25_market_value,
                        p75_market_value            = EXCLUDED.p75_market_value,
                        median_assessed_value       = EXCLUDED.median_assessed_value,
                        median_assessment_ratio     = EXCLUDED.median_assessment_ratio,
                        median_yoy_value_change_pct = EXCLUDED.median_yoy_value_change_pct,
                        computed_at                 = NOW()
            """, (label, prefix_key))
            n = cur.rowcount
            print(f"    {label}: {n} year rows")
    conn.commit()
    print(f"  → county_benchmark done in {time.time()-t0:.1f}s")


# ── Sample verification output ──────────────────────────────────────────────────
def print_sample(conn):
    sanity_parcels = [
        ("0100030105", "Commercial F1 — 1201 S Lamar"),
        ("0100030109", "Multi-family B — 1219 S Lamar"),
        ("0284460113", "Residential A — Abbeyglen Castle Dr"),
    ]
    print("\n=== Sample: sanity-check parcels ===")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for geo_id, label in sanity_parcels:
            print(f"\n  {label} ({geo_id})")
            cur.execute("""
                SELECT tax_year, coverage_level,
                       yoy_market_value_pct,
                       assessment_ratio,
                       effective_tax_rate,
                       cumulative_value_growth_pct,
                       risk_large_value_jump,
                       risk_large_value_jump_pct,
                       risk_homestead_cap_expiry,
                       risk_delinquent,
                       risk_data_incomplete
                FROM parcel_metrics WHERE geo_id = %s ORDER BY tax_year
            """, (geo_id,))
            for r in cur.fetchall():
                d = dict(r)
                print(f"    {d['tax_year']} [{d['coverage_level']}]"
                      f"  yoy_mkt={d['yoy_market_value_pct']}"
                      f"  ratio={d['assessment_ratio']}"
                      f"  eff_rate={d['effective_tax_rate']}"
                      f"  cum={d['cumulative_value_growth_pct']}"
                      f"  jump={d['risk_large_value_jump']}"
                      f"  cap={d['risk_homestead_cap_expiry']}")

        print("\n  County benchmark — Residential 2025:")
        cur.execute("""
            SELECT parcel_count, median_market_value, p25_market_value, p75_market_value,
                   median_assessment_ratio, median_yoy_value_change_pct
            FROM county_benchmark
            WHERE property_type_label = 'Residential' AND tax_year = 2025
        """)
        r = cur.fetchone()
        if r:
            d = dict(r)
            print(f"    n={d['parcel_count']:,}  "
                  f"median=${d['median_market_value']:,}  "
                  f"p25=${d['p25_market_value']:,}  "
                  f"p75=${d['p75_market_value']:,}  "
                  f"ratio={d['median_assessment_ratio']}  "
                  f"yoy={d['median_yoy_value_change_pct']}%")


# ── Entrypoint ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Phase 2 metric computation")
    parser.add_argument("--analyze", action="store_true",
                        help="Print threshold distribution analysis only; skip compute")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.analyze:
            analyze_threshold(conn)
            return

        # Apply any new schema additions (parcel_metrics, county_benchmark, rate_trend view)
        print("Applying schema…")
        execute_schema(conn)

        # Threshold analysis runs first so you can see what the current setting flags
        analyze_threshold(conn)

        # Compute
        compute_parcel_metrics(conn)
        compute_county_benchmarks(conn)
        print_sample(conn)

        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
