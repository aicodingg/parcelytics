#!/bin/bash
# run_cert_load.command
# Double-click this file to run the full 2022–2024 certified load pipeline.
# Output is logged to run_cert_load.log in the same directory.
# Safe to re-run — all steps are UPSERT (idempotent).

set -e
cd "$(dirname "$0")"

LOG="run_cert_load.log"
exec > >(tee -a "$LOG") 2>&1

echo ""
echo "========================================================"
echo "  Parcelytics — 2022–2024 Certified Load Pipeline"
echo "  Started: $(date)"
echo "========================================================"

echo ""
echo "─── STEP 1: Load 2022 Certified Export ─────────────────"
python3 loaders/load_certified_historical.py --year 2022

echo ""
echo "─── STEP 2: Load 2023 Certified Export ─────────────────"
python3 loaders/load_certified_historical.py --year 2023

echo ""
echo "─── STEP 3: Load 2024 Certified Export ─────────────────"
python3 loaders/load_certified_historical.py --year 2024

echo ""
echo "─── STEP 4: Rebuild county_benchmark + parcel_metrics ──"
python3 loaders/compute_metrics.py

echo ""
echo "─── STEP 5: Sanity check — 3 parcels × 2022–2024 ───────"
python3 - <<'PYEOF'
import sys, os
sys.path.insert(0, '.')
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(
    host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER,
    password=config.DB_PASS
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

parcels = [
    ('0100030105', '1201 S Lamar / Odd Duck (Commercial F1)'),
    ('0100030109', '1219 S Lamar (Multi-Family B)'),
    ('0284460113', 'Abbeyglen Castle Dr (Residential A)'),
]

for geo_id, label in parcels:
    print(f"\n  {label}  [{geo_id}]")
    cur.execute("""
        SELECT tax_year, market_value, assessed_value, taxable_value,
               land_value, imprv_value, data_source
        FROM parcel_tax_year
        WHERE geo_id = %s AND tax_year IN (2022, 2023, 2024)
        ORDER BY tax_year
    """, (geo_id,))
    rows = cur.fetchall()
    if not rows:
        print("    (no rows)")
    for r in rows:
        print(f"    {r['tax_year']}  MV={r['market_value']:>12,}  "
              f"AV={r['assessed_value']:>12,}  "
              f"TV={str(r['taxable_value'] or 'None'):>12}  "
              f"LV={str(r['land_value'] or 'None'):>12}  "
              f"IV={str(r['imprv_value'] or 'None'):>12}  "
              f"src={r['data_source']}")

conn.close()
PYEOF

echo ""
echo "─── STEP 6: County benchmark — Commercial 2022–2024 ─────"
python3 - <<'PYEOF'
import sys
sys.path.insert(0, '.')
import config, psycopg2, psycopg2.extras

conn = psycopg2.connect(
    host=config.DB_HOST, port=config.DB_PORT,
    dbname=config.DB_NAME, user=config.DB_USER,
    password=config.DB_PASS
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT tax_year, property_type_label, parcel_count,
           median_market_value, median_assessment_ratio
    FROM county_benchmark
    WHERE tax_year IN (2022, 2023, 2024)
    ORDER BY property_type_label, tax_year
""")
rows = cur.fetchall()
print(f"  {'type':<16} {'year':>6} {'parcels':>10} {'median_mv':>14} {'ratio':>8}")
print(f"  {'─'*16} {'─'*6} {'─'*10} {'─'*14} {'─'*8}")
for r in rows:
    print(f"  {r['property_type_label']:<16} {r['tax_year']:>6} "
          f"{r['parcel_count']:>10,} "
          f"{r['median_market_value']:>14,.0f} "
          f"{float(r['median_assessment_ratio'] or 0):>8.4f}")
conn.close()
PYEOF

echo ""
echo "========================================================"
echo "  ALL STEPS COMPLETE"
echo "  Finished: $(date)"
echo "  Full log saved to: $(pwd)/$LOG"
echo "========================================================"
