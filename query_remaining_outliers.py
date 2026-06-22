"""
Identify the 27 non-AJR* commercial (F/L) parcels with >500% MV increase 2025→2026.
These are driving the residual mean of 19,085% after AJR* exclusion.

Run:
    cd ~/Desktop/Claude\ Files/parcel_app && python3 query_remaining_outliers.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 110)
print("  Non-AJR* Commercial (F/L) parcels with >500% MV increase 2025→2026")
print("=" * 110)

cur.execute("""
    SELECT
        p.geo_id,
        p.situs_address,
        p.state_cd1,
        p.classi_cd,
        pty25.market_value   AS mv_2025,
        pty25.assessed_value AS av_2025,
        pty26.market_value   AS mv_2026,
        pty26.assessed_value AS av_2026,
        ROUND(100.0 * (pty26.market_value - pty25.market_value)
              / NULLIF(pty25.market_value, 0), 1) AS pct_change
    FROM parcel p
    JOIN parcel_tax_year pty25 ON pty25.geo_id = p.geo_id AND pty25.tax_year = 2025
    JOIN parcel_tax_year pty26 ON pty26.geo_id = p.geo_id AND pty26.tax_year = 2026
    WHERE LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND p.geo_id NOT LIKE 'AJR%%'
      AND pty25.market_value > 0 AND pty26.market_value > 0
      AND 100.0 * (pty26.market_value - pty25.market_value)
          / NULLIF(pty25.market_value, 0) > 500
    ORDER BY pct_change DESC
""")
rows = cur.fetchall()

print(f"\n  {'geo_id':<14} {'state_cd1':<10} {'classi':<7} {'MV 2025':>10} {'MV 2026':>14} {'AV 2026':>12} {'Pct Chg':>14}  Address")
print("  " + "-" * 105)
for r in rows:
    print(f"  {r['geo_id']:<14} {(r['state_cd1'] or ''):<10} {(r['classi_cd'] or ''):<7} "
          f"${r['mv_2025']:>9,.0f} ${r['mv_2026']:>13,.0f} ${r['av_2026']:>11,.0f} "
          f"{r['pct_change']:>13.1f}%  {(r['situs_address'] or '')[:35]}")

# How many have mv_2025 <= 100?
low_base = [r for r in rows if r['mv_2025'] <= 100]
print(f"\n  Total: {len(rows)} non-AJR* outliers")
print(f"  With mv_2025 ≤ $100 (placeholder values): {len(low_base)}")
print(f"  With mv_2025 > $100 (genuine reappraisals): {len(rows) - len(low_base)}")

# Aggregate contribution to mean
cur.execute("""
    SELECT
        ROUND(AVG(100.0 * (pty26.market_value - pty25.market_value)
              / NULLIF(pty25.market_value, 0))::NUMERIC, 2) AS mean_without_outliers
    FROM parcel p
    JOIN parcel_tax_year pty25 ON pty25.geo_id = p.geo_id AND pty25.tax_year = 2025
    JOIN parcel_tax_year pty26 ON pty26.geo_id = p.geo_id AND pty26.tax_year = 2026
    WHERE LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND p.geo_id NOT LIKE 'AJR%%'
      AND pty25.market_value > 100   -- exclude $1/$10/$100 placeholder bases
      AND pty26.market_value > 0
""")
r = cur.fetchone()
print(f"\n  Commercial mean excluding mv_2025 ≤ $100 placeholders: {r['mean_without_outliers']:+.2f}%")

conn.close()
print("\nDone.")
