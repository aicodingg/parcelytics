"""
2026 vs 2025 county-wide market value comparison by property type.
Run: cd ~/Desktop/Claude\ Files/parcel_app && python3 query_2026_vs_2025.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 90)
print("  2026 Preliminary vs 2025 Certified — County-Wide Market Value Comparison")
print("=" * 90)

cur.execute("""
WITH y25 AS (
    SELECT p.geo_id, p.state_cd1,
           t.market_value AS mv25
    FROM parcel p
    JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
    WHERE t.market_value IS NOT NULL AND t.market_value > 0
      AND p.geo_id NOT LIKE 'AJR%'   -- exclude personal property supplement accounts
      AND p.state_cd1 NOT LIKE 'X%'  -- exclude tax-exempt accounts
),
y26 AS (
    SELECT geo_id, market_value AS mv26
    FROM parcel_tax_year
    WHERE tax_year = 2026 AND market_value IS NOT NULL AND market_value > 0
      AND geo_id NOT LIKE 'AJR%'
),
joined AS (
    SELECT
        y25.state_cd1,
        y25.mv25,
        y26.mv26,
        (y26.mv26 - y25.mv25)::float / y25.mv25 AS pct_chg,
        CASE
            WHEN LEFT(y25.state_cd1,1) = 'A'        THEN 'Residential (A)'
            WHEN LEFT(y25.state_cd1,1) = 'B'        THEN 'Multi-Family (B)'
            WHEN LEFT(y25.state_cd1,1) IN ('F','L') THEN 'Commercial (F/L)'
            WHEN LEFT(y25.state_cd1,1) = 'C'        THEN 'Land/Vacant (C)'
            WHEN LEFT(y25.state_cd1,1) = 'D'        THEN 'Agricultural (D)'
            WHEN LEFT(y25.state_cd1,1) = 'E'        THEN 'Rural/Open Space (E)'
            ELSE 'Other (' || LEFT(COALESCE(y25.state_cd1,'?'),2) || ')'
        END AS ptype
    FROM y25 JOIN y26 USING (geo_id)
)
SELECT
    ptype,
    COUNT(*)                                                                            AS n_parcels,
    SUM(CASE WHEN mv26 > mv25 THEN 1 ELSE 0 END)                                       AS n_up,
    SUM(CASE WHEN mv26 < mv25 THEN 1 ELSE 0 END)                                       AS n_down,
    SUM(CASE WHEN mv26 = mv25 THEN 1 ELSE 0 END)                                       AS n_flat,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pct_chg)::numeric * 100, 2)      AS median_pct,
    ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY pct_chg)::numeric * 100, 2)     AS p25_pct,
    ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY pct_chg)::numeric * 100, 2)     AS p75_pct,
    ROUND(AVG(pct_chg)::numeric * 100, 2)                                              AS mean_pct,
    ROUND(SUM(mv26)::numeric / 1e9, 2)                                                 AS total_mv26_b,
    ROUND(SUM(mv25)::numeric / 1e9, 2)                                                 AS total_mv25_b
FROM joined
GROUP BY ptype
ORDER BY n_parcels DESC
""")
rows = cur.fetchall()

print(f"\n  {'Property Type':<22} {'N':>7} {'↑Up':>7} {'↓Down':>7} {'=Flat':>6} "
      f"{'Med%':>7} {'P25%':>6} {'P75%':>6} {'Mean%':>7} {'25→26 ΔTotal':>14}")
print("  " + "-" * 95)
for r in rows:
    total_chg = (r['total_mv26_b'] - r['total_mv25_b'])
    sign = "+" if total_chg >= 0 else ""
    print(f"  {r['ptype']:<22} {r['n_parcels']:>7,} {r['n_up']:>7,} {r['n_down']:>7,} "
          f"{r['n_flat']:>6,} {r['median_pct']:>7} {r['p25_pct']:>6} {r['p75_pct']:>6} "
          f"{r['mean_pct']:>7} {sign}{total_chg:.2f}B")

# County total
cur.execute("""
WITH y25 AS (
    SELECT geo_id, market_value AS mv25 FROM parcel_tax_year
    WHERE tax_year=2025 AND market_value>0
),
y26 AS (
    SELECT geo_id, market_value AS mv26 FROM parcel_tax_year
    WHERE tax_year=2026 AND market_value>0
)
SELECT
    COUNT(*)                                                                            AS n,
    SUM(CASE WHEN mv26>mv25 THEN 1 ELSE 0 END)                                         AS n_up,
    SUM(CASE WHEN mv26<mv25 THEN 1 ELSE 0 END)                                         AS n_down,
    SUM(CASE WHEN mv26=mv25 THEN 1 ELSE 0 END)                                         AS n_flat,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY (mv26-mv25)::float/mv25)::numeric*100, 2)
                                                                                        AS median_pct,
    ROUND(SUM(mv25)::numeric/1e9, 2)                                                   AS total_mv25_b,
    ROUND(SUM(mv26)::numeric/1e9, 2)                                                   AS total_mv26_b
FROM y25 JOIN y26 USING(geo_id)
""")
t = cur.fetchone()
delta = t['total_mv26_b'] - t['total_mv25_b']
print(f"\n  {'COUNTY TOTAL':<22} {t['n']:>7,} {t['n_up']:>7,} {t['n_down']:>7,} "
      f"{t['n_flat']:>6,} {t['median_pct']:>7}")
print(f"\n  Total assessed value: ${t['total_mv25_b']:.2f}B (2025) → ${t['total_mv26_b']:.2f}B (2026 prelim) "
      f"  ({'+' if delta>=0 else ''}{delta:.2f}B)")

conn.close()
print("\nDone.")
