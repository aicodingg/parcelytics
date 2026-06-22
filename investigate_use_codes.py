"""
investigate_use_codes.py — Task 4: query distinct use code fields in the DB.

Checks:
  1. Distinct values / counts in state_cd1 (Texas Comptroller classification)
  2. Distinct values / counts in prop_type_cd  (coarse R/P/MH/MN)
  3. Whether state_cd1 ever contains numeric values (i.e., TCAD internal use codes)
  4. Whether any column in the parcel table looks like a numeric use code field

Run from:  cd ~/Desktop/Claude\ Files/parcel_app && python3 investigate_use_codes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(
    host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("\n" + "="*80)
print("  USE CODE FIELD INVESTIGATION")
print("="*80)

# 1. state_cd1 — distinct values + counts, sorted by frequency
print("\n── 1. state_cd1 — distinct values (sorted by count desc) ──────────────────")
cur.execute("""
    SELECT state_cd1, COUNT(*) AS n
    FROM parcel
    GROUP BY state_cd1
    ORDER BY n DESC
    LIMIT 40
""")
rows = cur.fetchall()
total = sum(r["n"] for r in rows)
print(f"  {'state_cd1':<15} {'count':>9}  {'%':>6}")
print(f"  {'-'*40}")
for r in rows:
    val = r["state_cd1"] if r["state_cd1"] is not None else "(NULL)"
    pct = r["n"] / total * 100 if total else 0
    print(f"  {val:<15} {r['n']:>9,}  {pct:>5.1f}%")

# Check if any state_cd1 value looks numeric (would indicate TCAD internal use codes)
numeric_cd1 = [r for r in rows if r["state_cd1"] and r["state_cd1"].strip().isdigit()]
print(f"\n  → Numeric state_cd1 values found: {len(numeric_cd1)}")
if numeric_cd1:
    print("  *** IMPORTANT: state_cd1 contains numeric values — TCAD use codes are here!")
    for r in numeric_cd1[:10]:
        print(f"       {r['state_cd1']}  ({r['n']:,} parcels)")
else:
    print("  → state_cd1 is purely Comptroller alpha codes (A, B, F1, etc.)")
    print("    TCAD internal numeric use codes are NOT in this field.")

# 2. state_cd2 — any values at all?
print("\n── 2. state_cd2 — distinct values (secondary Comptroller code) ─────────────")
cur.execute("""
    SELECT state_cd2, COUNT(*) AS n
    FROM parcel
    WHERE state_cd2 IS NOT NULL AND state_cd2 <> ''
    GROUP BY state_cd2
    ORDER BY n DESC
    LIMIT 20
""")
rows2 = cur.fetchall()
if rows2:
    for r in rows2:
        print(f"  {r['state_cd2']:<15} {r['n']:>9,}")
else:
    print("  (all NULL or empty — field not populated)")

# 3. prop_type_cd
print("\n── 3. prop_type_cd — distinct values ───────────────────────────────────────")
cur.execute("""
    SELECT prop_type_cd, COUNT(*) AS n
    FROM parcel
    GROUP BY prop_type_cd
    ORDER BY n DESC
""")
for r in cur.fetchall():
    val = r["prop_type_cd"] if r["prop_type_cd"] is not None else "(NULL)"
    print(f"  {val:<15} {r['n']:>9,}")

# 4. Check all column names in parcel for anything that might be a use code
print("\n── 4. Column names in parcel table ─────────────────────────────────────────")
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'parcel'
    ORDER BY ordinal_position
""")
cols = cur.fetchall()
print(f"  {'column_name':<30} {'data_type':<20}")
print(f"  {'-'*55}")
for c in cols:
    flag = "  ← use code?" if any(kw in c["column_name"].lower()
                                   for kw in ["use", "class", "cd", "code", "type"]) else ""
    print(f"  {c['column_name']:<30} {c['data_type']:<20}{flag}")

# 5. Same check on parcel_tax_year
print("\n── 5. Column names in parcel_tax_year table ────────────────────────────────")
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'parcel_tax_year'
    ORDER BY ordinal_position
""")
for c in cur.fetchall():
    flag = "  ← use code?" if any(kw in c["column_name"].lower()
                                   for kw in ["use", "class", "cd", "code", "type"]) else ""
    print(f"  {c['column_name']:<30} {c['data_type']:<20}{flag}")

print("\n" + "="*80)
print("  SUMMARY / CONCLUSIONS")
print("="*80)
print("""
  The TCAD numeric use codes (30=Convenience Store, 32=Office, 48=Restaurant, etc.)
  are NOT currently loaded into the database. They come from PROP.TXT but no loader
  field was mapped for them.

  The most granular property type field currently available is state_cd1 (Texas
  Comptroller classification codes: A=SFR, F1=Commercial, D1=Agricultural, etc.).

  To enable the USE_CODE_LOOKUP matrix (Task 5), one of the following is needed:
    Option A: Identify the fixed-width byte position of the TCAD use code in PROP.TXT
              and add a column to the parcel or parcel_tax_year table.
    Option B: Use state_cd1 as a proxy to infer valuation method (less granular).

  This script's output will confirm which fields are in the DB and whether
  any numeric codes are already present in state_cd1.
""")

conn.close()
print("Done.\n")
