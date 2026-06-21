"""
Sanity check script — run after reloading data to verify fixes.
Usage: python3 review_check.py
"""
import psycopg2, psycopg2.extras
conn = psycopg2.connect(dbname='parcel_tax', user='diegog')
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print('=== ROW COUNTS BY YEAR ===')
cur.execute("""
  SELECT tax_year,
    COUNT(*) as parcels,
    COUNT(market_value) as has_market,
    COUNT(assessed_value) as has_assessed,
    COUNT(taxable_value) as has_taxable,
    COUNT(land_value) as has_land,
    COUNT(imprv_value) as has_imprv,
    COUNT(hs_cap_loss) as has_hs_cap
  FROM parcel_tax_year GROUP BY tax_year ORDER BY tax_year
""")
for r in cur.fetchall(): print(dict(r))

print()
print('=== hs_cap_loss FIX: SFR with homestead cap in ANY year ===')
cur.execute("""
  SELECT p.geo_id, p.situs_address, p.state_cd1, pty.tax_year, pty.hs_cap_loss
  FROM parcel p
  JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id
  WHERE p.state_cd1 = 'A' AND pty.hs_cap_loss > 0
  ORDER BY pty.hs_cap_loss DESC
  LIMIT 3
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  FOUND: geo={r['geo_id']} addr={r['situs_address']} year={r['tax_year']} hs_cap={r['hs_cap_loss']:,}")
    # Full history for the top one
    geo = rows[0]['geo_id']
    print(f"\n  Full history for {geo}:")
    cur.execute("""
      SELECT tax_year, market_value, assessed_value, taxable_value, hs_cap_loss
      FROM parcel_tax_year WHERE geo_id=%s ORDER BY tax_year
    """, (geo,))
    for r in cur.fetchall(): print(f"    {dict(r)}")
else:
    print('  WARNING: No SFR homestead parcels found — check data load')

print()
print('=== land_value / imprv_value FIX: 2025 non-zero counts ===')
cur.execute("""
  SELECT COUNT(*) as total_2025,
    COUNT(land_value) as has_land,
    COUNT(imprv_value) as has_imprv,
    AVG(land_value::numeric) as avg_land,
    AVG(imprv_value::numeric) as avg_imprv
  FROM parcel_tax_year WHERE tax_year = 2025
""")
r = cur.fetchone()
print(f"  2025 parcels: {r['total_2025']:,}")
print(f"  has land_value: {r['has_land']:,}")
print(f"  has imprv_value: {r['has_imprv']:,}")
if r['avg_land']: print(f"  avg land_value: ${float(r['avg_land']):,.0f}")
if r['avg_imprv']: print(f"  avg imprv_value: ${float(r['avg_imprv']):,.0f}")

print()
print('=== SANITY CHECK: Commercial 0100030105 (1201 S Lamar) ===')
cur.execute("""
  SELECT tax_year, market_value, assessed_value, taxable_value, land_value, imprv_value, hs_cap_loss
  FROM parcel_tax_year WHERE geo_id='0100030105' ORDER BY tax_year
""")
for r in cur.fetchall(): print(f"  {dict(r)}")

# Verify land + imprv ~ market for 2025
cur.execute("""
  SELECT market_value, land_value, imprv_value,
    (land_value + imprv_value) as sum_lv_iv
  FROM parcel_tax_year WHERE geo_id='0100030105' AND tax_year=2025
""")
r = cur.fetchone()
if r and r['land_value']:
    print(f"  2025 check: market={r['market_value']:,}  land={r['land_value']:,}  imprv={r['imprv_value']:,}  sum={r['sum_lv_iv']:,}")

print()
print('=== SANITY CHECK: Multi-family 0100030109 (1219 S Lamar) ===')
cur.execute("""
  SELECT tax_year, market_value, assessed_value, taxable_value, land_value, imprv_value
  FROM parcel_tax_year WHERE geo_id='0100030109' ORDER BY tax_year
""")
for r in cur.fetchall(): print(f"  {dict(r)}")

print()
print('Done.')
conn.close()
