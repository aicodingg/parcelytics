"""
Report all state_cd1 prefix groups not in the standard A-F/J/L/M-O/S/X set.
Used to identify mineral accounts, personal property, or other non-real-property codes
that should be excluded from real-estate benchmark calculations.

Run:
    cd ~/Desktop/Claude\ Files/parcel_app && python3 query_state_cd1_prefixes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 70)
print("  All state_cd1 prefix groups in parcel table")
print("=" * 70)

# All prefixes, sorted by count
cur.execute("""
    SELECT
        LEFT(COALESCE(state_cd1, ''), 1) AS prefix,
        COUNT(*) AS cnt,
        COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS pct
    FROM parcel
    GROUP BY prefix
    ORDER BY cnt DESC
""")
rows = cur.fetchall()

KNOWN_REAL_ESTATE = set('ABCDEFGJLMNOSX')
print(f"\n  {'Prefix':<10} {'Count':>8}  {'%':>6}  Known?")
print("  " + "-" * 40)
for r in rows:
    pfx = r['prefix'] or '(null)'
    known = "Real estate" if pfx in KNOWN_REAL_ESTATE else "*** CHECK ***"
    print(f"  {pfx:<10} {r['cnt']:>8,}  {r['pct']:>5.2f}%  {known}")

# Specifically find the "unrecognized" codes from the query_2026_vs_2025 report
print(f"\n\n  Unrecognized prefixes (not in standard Comptroller real-estate set):")
cur.execute("""
    SELECT
        state_cd1,
        COUNT(*) AS cnt
    FROM parcel
    WHERE LEFT(state_cd1, 1) NOT IN ('A','B','C','D','E','F','G','H','J','L','M','N','O','S','X')
      AND state_cd1 IS NOT NULL AND state_cd1 != ''
    GROUP BY state_cd1
    ORDER BY cnt DESC
    LIMIT 30
""")
rows2 = cur.fetchall()
if rows2:
    print(f"  {'state_cd1':<12} {'Count':>8}")
    print("  " + "-" * 25)
    for r in rows2:
        print(f"  {r['state_cd1']:<12} {r['cnt']:>8,}")
else:
    print("  None found.")

# Check null / empty
cur.execute("SELECT COUNT(*) AS n FROM parcel WHERE state_cd1 IS NULL OR state_cd1 = ''")
n_null = cur.fetchone()['n']
print(f"\n  Parcels with NULL/empty state_cd1: {n_null:,}")

# Check neighborhood_cd null rate
cur.execute("""
    SELECT
        COUNT(*) AS total,
        COUNT(neighborhood_cd) AS non_null,
        COUNT(*) - COUNT(neighborhood_cd) AS null_count
    FROM parcel
""")
nb = cur.fetchone()
print(f"\n  neighborhood_cd coverage:")
print(f"    total parcels : {nb['total']:,}")
print(f"    non-null      : {nb['non_null']:,}  ({100*nb['non_null']/nb['total']:.1f}%)")
print(f"    null          : {nb['null_count']:,}")

# Sample neighborhood_cd values
cur.execute("""
    SELECT neighborhood_cd, COUNT(*) AS cnt
    FROM parcel
    WHERE neighborhood_cd IS NOT NULL AND neighborhood_cd != ''
    GROUP BY neighborhood_cd
    ORDER BY cnt DESC
    LIMIT 20
""")
nbs = cur.fetchall()
if nbs:
    print(f"\n  Top 20 neighborhood_cd values:")
    for r in nbs:
        print(f"    {r['neighborhood_cd']:<20} {r['cnt']:>8,}")

conn.close()
print("\nDone.")
