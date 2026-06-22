"""
Identify commercial parcels with largest 2025→2026 market value increases.
Flags parcels with >500% increase as potential data anomalies.

Run:
    cd ~/Desktop/Claude\ Files/parcel_app && python3 query_commercial_outliers.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 100)
print("  Commercial (F/L) parcels — Top 20 by 2025→2026 market value increase")
print("=" * 100)

cur.execute("""
    SELECT p.geo_id, p.situs_address,
           pty25.market_value  AS mv_2025,
           pty26.market_value  AS mv_2026,
           ROUND(100.0 * (pty26.market_value - pty25.market_value)
                 / NULLIF(pty25.market_value, 0), 1) AS pct_change
    FROM parcel p
    JOIN parcel_tax_year pty25 ON pty25.geo_id = p.geo_id AND pty25.tax_year = 2025
    JOIN parcel_tax_year pty26 ON pty26.geo_id = p.geo_id AND pty26.tax_year = 2026
    WHERE LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND pty25.market_value > 0
      AND pty26.market_value > 0
    ORDER BY pct_change DESC
    LIMIT 20
""")
rows = cur.fetchall()

print(f"\n  {'geo_id':<14} {'MV 2025':>14} {'MV 2026':>14} {'Pct Chg':>10}  Address")
print("  " + "-" * 90)
for r in rows:
    flag = " *** ANOMALY" if r['pct_change'] and r['pct_change'] > 500 else ""
    print(f"  {r['geo_id']:<14} ${r['mv_2025']:>13,.0f} ${r['mv_2026']:>13,.0f} "
          f"{r['pct_change']:>9.1f}%  {(r['situs_address'] or '')[:40]}{flag}")

# Count anomaly parcels
cur.execute("""
    SELECT COUNT(*) AS n_anomaly,
           SUM(pty26.market_value - pty25.market_value) AS total_excess_mv
    FROM parcel p
    JOIN parcel_tax_year pty25 ON pty25.geo_id = p.geo_id AND pty25.tax_year = 2025
    JOIN parcel_tax_year pty26 ON pty26.geo_id = p.geo_id AND pty26.tax_year = 2026
    WHERE LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND pty25.market_value > 0 AND pty26.market_value > 0
      AND 100.0 * (pty26.market_value - pty25.market_value)
          / NULLIF(pty25.market_value, 0) > 500
""")
agg = cur.fetchone()
print(f"\n  Parcels with >500% increase: {agg['n_anomaly']:,}")
if agg['total_excess_mv']:
    print(f"  Combined excess MV contribution: ${float(agg['total_excess_mv'])/1e9:.2f}B")

conn.close()
print("\nDone.")
