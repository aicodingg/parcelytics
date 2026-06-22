"""
Post-fix verification:
1. Re-compute commercial mean/median after AJR* exclusion (corrected figures for KNOWN_LIMITATIONS)
2. Check Howard Ln parcel (0275010202) for correct data, no AV>MV flag, 2026 benchmark inclusion
3. Delete any AJR* rows from county_benchmark (cleanup from prior compute_metrics runs)

Run:
    cd ~/Desktop/Claude\ Files/parcel_app && python3 verify_ajr_fix.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# ── 1. Corrected commercial mean/median after AJR* exclusion ──────────────────
print("=" * 80)
print("  1. Commercial (F/L) mean/median change — AJR* excluded")
print("=" * 80)

cur.execute("""
    WITH y25 AS (
        SELECT p.geo_id, t.market_value AS mv25
        FROM parcel p
        JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
        WHERE t.market_value > 0
          AND LEFT(p.state_cd1, 1) IN ('F', 'L')
          AND p.geo_id NOT LIKE 'AJR%%'
          AND p.state_cd1 NOT LIKE 'X%%'
    ),
    y26 AS (
        SELECT geo_id, market_value AS mv26
        FROM parcel_tax_year
        WHERE tax_year = 2026 AND market_value > 0
          AND geo_id NOT LIKE 'AJR%%'
    ),
    joined AS (
        SELECT
            100.0 * (mv26 - mv25) / mv25 AS pct_chg,
            mv25, mv26
        FROM y25 JOIN y26 USING (geo_id)
    )
    SELECT
        COUNT(*)                                                              AS n_parcels,
        ROUND(AVG(pct_chg)::NUMERIC, 2)                                      AS mean_pct,
        ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pct_chg)::NUMERIC, 2) AS median_pct,
        ROUND(SUM(mv25)::NUMERIC / 1e9, 2)                                   AS total_mv25_b,
        ROUND(SUM(mv26)::NUMERIC / 1e9, 2)                                   AS total_mv26_b,
        COUNT(CASE WHEN pct_chg > 500 THEN 1 END)                            AS still_over_500pct
    FROM joined
""")
row = cur.fetchone()
print(f"  Parcels:       {row['n_parcels']:,}")
print(f"  Mean change:   {row['mean_pct']:+.2f}%  (was 6,084.02% before AJR* exclusion)")
print(f"  Median change: {row['median_pct']:+.2f}%")
print(f"  Total MV 2025: ${row['total_mv25_b']:.2f}B")
print(f"  Total MV 2026: ${row['total_mv26_b']:.2f}B")
print(f"  Still >500%:   {row['still_over_500pct']} parcels (should be 0 or just Howard Ln)")

# ── 2. Howard Ln parcel check ─────────────────────────────────────────────────
print()
print("=" * 80)
print("  2. Howard Ln parcel — 0275010202")
print("=" * 80)

GEO_ID = "0275010202"

# Parcel master
cur.execute("SELECT geo_id, situs_address, state_cd1, classi_cd FROM parcel WHERE geo_id = %s", (GEO_ID,))
p = cur.fetchone()
if p:
    print(f"  geo_id:       {p['geo_id']}")
    print(f"  address:      {p['situs_address']}")
    print(f"  state_cd1:    {p['state_cd1']}")
    print(f"  classi_cd:    {p['classi_cd']}")
else:
    print("  *** PARCEL NOT FOUND ***")

# Tax year rows
cur.execute("""
    SELECT tax_year, market_value, assessed_value, taxable_value, data_source
    FROM parcel_tax_year
    WHERE geo_id = %s
    ORDER BY tax_year
""", (GEO_ID,))
rows = cur.fetchall()
print(f"\n  Tax year records ({len(rows)} rows):")
for r in rows:
    av_flag = " *** AV > MV" if r['assessed_value'] and r['market_value'] and r['assessed_value'] > r['market_value'] else ""
    print(f"    {r['tax_year']}: MV=${r['market_value'] or 0:>12,.0f}  AV=${r['assessed_value'] or 0:>12,.0f}  "
          f"TV={r['taxable_value'] or 0:>12,.0f}  [{r['data_source'] or 'certified'}]{av_flag}")

# Parcel metrics
cur.execute("""
    SELECT tax_year, yoy_market_value_pct, risk_large_value_jump, risk_large_value_jump_pct
    FROM parcel_metrics
    WHERE geo_id = %s
    ORDER BY tax_year
""", (GEO_ID,))
metrics = cur.fetchall()
print(f"\n  Parcel metrics ({len(metrics)} rows):")
for m in metrics:
    jump_flag = " [LARGE JUMP FLAGGED]" if m['risk_large_value_jump'] else ""
    print(f"    {m['tax_year']}: yoy_mv_pct={m['yoy_market_value_pct'] or 'NULL'}  "
          f"jump_pct={m['risk_large_value_jump_pct'] or 'NULL'}{jump_flag}")

# Check if geo_id would match AJR exclusion
is_ajr = GEO_ID.startswith("AJR")
print(f"\n  Is AJR* account: {is_ajr} — {'SHOULD NOT' if not is_ajr else 'WILL'} be excluded from benchmarks")

# Check 2026 benchmark inclusion — does it appear in F/L live aggregation window?
print(f"\n  2026 live benchmark check (would this parcel contribute to Commercial benchmark?):")
cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM parcel p
    JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2026
    WHERE p.geo_id = %s
      AND LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND p.geo_id NOT LIKE 'AJR%%'
      AND pty.market_value > 0
""", (GEO_ID,))
r = cur.fetchone()
print(f"    Rows matching 2026 Commercial benchmark filter: {r['cnt']} ({'YES - included' if r['cnt'] else 'NO - excluded'})")

# ── 3. Delete AJR* rows from county_benchmark ─────────────────────────────────
print()
print("=" * 80)
print("  3. AJR* rows in county_benchmark (cleanup)")
print("=" * 80)

# county_benchmark doesn't have a geo_id column; AJR* accounts map to property_type_label via state_cd1 LIKE patterns
# But AJR* accounts have commercial state_cd1 codes (F/L), so they'd inflate the Commercial row.
# We can't delete by geo_id here — we need to rebuild with the corrected compute_metrics.py.
# Instead, check if any AJR* accounts appear in the source data used by county_benchmark:
cur.execute("""
    SELECT COUNT(*) AS ajr_count_commercial
    FROM parcel p
    JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id
    WHERE p.geo_id LIKE 'AJR%%'
      AND LEFT(p.state_cd1, 1) IN ('F', 'L')
      AND pty.market_value > 0
      AND (pty.data_source IS NULL OR pty.data_source = 'certified')
""")
r = cur.fetchone()
print(f"  AJR* F/L accounts in certified data (would have been in old county_benchmark): {r['ajr_count_commercial']:,}")
print()

# Check current county_benchmark for Commercial to see if n_parcels looks inflated
cur.execute("""
    SELECT tax_year, property_type_label, parcel_count, median_market_value, median_yoy_value_change_pct
    FROM county_benchmark
    WHERE property_type_label IN ('Commercial', 'Residential')
    ORDER BY property_type_label, tax_year
""")
bench_rows = cur.fetchall()
print("  Current county_benchmark (Commercial + Residential rows):")
for r in bench_rows:
    print(f"    {r['property_type_label']:<15} {r['tax_year']}: {r['parcel_count']:>8,} parcels  "
          f"median_mv=${r['median_market_value'] or 0:>12,.0f}  "
          f"median_yoy={r['median_yoy_value_change_pct'] or 'NULL'}%")

print()
print("  → If AJR* accounts inflated Commercial parcel_count, re-run compute_metrics.py to rebuild.")
print("    The updated compute_metrics.py now includes AND p.geo_id NOT LIKE 'AJR%%' in the INSERT.")

conn.close()
print("\nDone.")
