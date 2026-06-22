"""
2026 vs 2025 county-wide market value comparison by property type.
Run: python3 compare_2026_vs_2025.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("""
    WITH joined AS (
        SELECT p.geo_id,
               LEFT(COALESCE(p.state_cd1,'?'),1) AS tp,
               p25.market_value AS mv25,
               p26.market_value AS mv26
        FROM parcel p
        JOIN parcel_tax_year p25 ON p25.geo_id=p.geo_id AND p25.tax_year=2025
        JOIN parcel_tax_year p26 ON p26.geo_id=p.geo_id AND p26.tax_year=2026
        WHERE p25.market_value>0 AND p26.market_value>0
    ),
    with_pct AS (
        SELECT tp, mv25, mv26,
               (mv26-mv25)::numeric/mv25*100 AS pct
        FROM joined
    )
    SELECT tp,
           COUNT(*)                                              AS n,
           PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY mv25)     AS med25,
           PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY mv26)     AS med26,
           PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY pct)      AS med_pct,
           COUNT(*) FILTER(WHERE mv26>mv25)                     AS up,
           COUNT(*) FILTER(WHERE mv26<mv25)                     AS down,
           COUNT(*) FILTER(WHERE mv26=mv25)                     AS flat
    FROM with_pct GROUP BY tp ORDER BY n DESC
""")
rows = cur.fetchall()

LABELS = {
    'A':'Residential (SFR)','B':'Multi-Family','C':'Land/Vacant',
    'D':'Agricultural (D)','E':'Agricultural (E)','F':'Commercial',
    'G':'Minerals','J':'Utilities','L':'Personal Prop',
    'M':'Mobile Home','O':'Other','X':'Exempt',
}

print(f"\n{'='*108}")
print(f"  2026 vs 2025 — County-Wide Market Value Comparison by Property Type")
print(f"{'='*108}")
print(f"  {'Type':<22} {'N':>8}  {'Med MV 2025':>13} {'Med MV 2026':>13} {'Med Δ':>8}  {'↑ Up':>8} {'↓ Down':>8} {'= Flat':>7}")
print(f"  {'-'*100}")
for r in rows:
    lbl = LABELS.get(r['tp'], f"Other({r['tp']})")
    pct = float(r['med_pct'])
    flag = "  ◀ notable" if abs(pct) > 10 else ""
    print(f"  {lbl:<22} {r['n']:>8,}  ${float(r['med25']):>11,.0f} ${float(r['med26']):>11,.0f} {pct:>+8.1f}%  {r['up']:>8,} {r['down']:>8,} {r['flat']:>7,}{flag}")

cur.execute("""
    SELECT COUNT(*) AS n,
           COUNT(*) FILTER(WHERE p26.market_value>p25.market_value)  AS up,
           COUNT(*) FILTER(WHERE p26.market_value<p25.market_value)  AS down,
           COUNT(*) FILTER(WHERE p26.market_value=p25.market_value)  AS flat,
           PERCENTILE_CONT(0.5) WITHIN GROUP(
               ORDER BY (p26.market_value-p25.market_value)::numeric/p25.market_value*100
           ) AS med_pct
    FROM parcel_tax_year p25
    JOIN parcel_tax_year p26 USING(geo_id)
    WHERE p25.tax_year=2025 AND p26.tax_year=2026
      AND p25.market_value>0 AND p26.market_value>0
""")
t = cur.fetchone()
print(f"  {'-'*100}")
print(f"  {'ALL TYPES':<22} {t['n']:>8,}  {'':>13} {'':>13} {float(t['med_pct']):>+8.1f}%  {t['up']:>8,} {t['down']:>8,} {t['flat']:>7,}")
print(f"{'='*108}\n")
conn.close()
