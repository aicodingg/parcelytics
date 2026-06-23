"""
load_imp_det_sqft.py — Populate parcel.living_area_sqft from IMP_DET.TXT.

IMP_DET.TXT field layout (0-based byte offsets, CRLF-terminated fixed-width):
  [0:12]   prop_id        — internal TCAD sequential property ID (NOT the geo_id)
  [12:16]  year           — appraisal year
  [16:28]  imp_id         — improvement number
  [28:40]  det_id         — detail number
  [40:50]  component_code — 10-char component type (e.g. '1ST       ', '2ND       ')
  [50:75]  component_desc — 25-char description
  [75:85]  quality_class  — 10-char quality rating
  [85:89]  year_built
  [89:93]  eff_year
  [93:103] area_sqft      — 10-char float string (e.g. '00002986.0')
  [103:116] value         — 13-char float string

Living-area component codes included in total (industry standard for TCAD):
  1ST    — 1st floor living area
  2ND    — 2nd floor living area
  3RD    — 3rd floor living area
  1/2    — half floor
  RSBLW  — residence below grade (walk-out)
  FBSMT  — finished basement

Excluded (not heated/cooled living area):
  UBSMT  — unfinished basement
  GAR    — garage
  CPRT   — carport
  DECK   — deck/patio
  (all others)

Mapping: IMP_DET.TXT prop_id → geo_id requires a PROP.TXT pass first,
because the certified export uses internal sequential prop_id while the
app's parcel table PK (geo_id) is the TCAD account number from PROP.TXT[546:556].

Migration: ALTER TABLE parcel ADD COLUMN IF NOT EXISTS living_area_sqft NUMERIC(10,2)
Run migrate_add_sqft.py first, or include the ALTER in setup.sql.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn

import psycopg2.extras


# Component codes that count as heated/cooled living area
LIVING_AREA_CODES = frozenset({"1ST", "2ND", "3RD", "1/2", "RSBLW", "FBSMT"})


def _build_prop_id_map(cert_dir):
    """
    Read PROP.TXT and return a dict mapping prop_id_str -> geo_id.

    PROP.TXT layout used here:
      [0:12]    prop_id (12-char zero-padded int)
      [22:34]   sup_num  — skip non-zero (supplements)
      [546:556] geo_id   (first 10 chars of the 50-char geo_id field)
    """
    prop_txt = os.path.join(cert_dir, "PROP.TXT")
    print(f"  Building prop_id → geo_id map from PROP.TXT "
          f"({os.path.getsize(prop_txt)/1e9:.1f} GB)…")
    t0 = time.time()

    prop_map = {}  # prop_id_str -> geo_id
    with open(prop_txt, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 556:
                continue
            try:
                sup_num = int(line[22:34].strip())
            except ValueError:
                continue
            if sup_num != 0:
                continue  # skip supplement rows

            prop_id_str = line[0:12].strip()
            geo_id = line[546:556].strip()
            if prop_id_str and geo_id:
                prop_map[prop_id_str] = geo_id

    print(f"    → {len(prop_map):,} parcels mapped in {time.time()-t0:.1f}s")
    return prop_map


def load(conn, cert_dir=None):
    """
    Scan IMP_DET.TXT, sum living-area sqft per geo_id, upsert into parcel.

    Pre-condition: parcel.living_area_sqft column must exist
    (run migrate_add_sqft.py or setup.sql migration first).
    """
    if cert_dir is None:
        cert_dir = config.CERT_DIR

    imp_det = os.path.join(cert_dir, "IMP_DET.TXT")
    if not os.path.exists(imp_det):
        print(f"  WARNING: {imp_det} not found, skipping")
        return 0

    # Step 1: build prop_id → geo_id mapping
    prop_map = _build_prop_id_map(cert_dir)

    # Step 2: scan IMP_DET.TXT, accumulate area per prop_id
    print(f"  Scanning IMP_DET.TXT ({os.path.getsize(imp_det)/1e9:.1f} GB)…")
    t0 = time.time()

    area_by_prop = {}   # prop_id_str -> float (total sqft)
    skipped_area = 0    # rows where area parse fails
    found_codes  = {}   # for reporting

    with open(imp_det, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 103:
                continue

            component_cd = line[40:50].strip()
            if component_cd not in LIVING_AREA_CODES:
                continue

            prop_id_str = line[0:12].strip()
            area_raw    = line[93:103].strip()

            try:
                area = float(area_raw)
            except ValueError:
                skipped_area += 1
                continue

            if area <= 0:
                continue

            area_by_prop[prop_id_str] = area_by_prop.get(prop_id_str, 0.0) + area
            found_codes[component_cd] = found_codes.get(component_cd, 0) + 1

            if lineno % 1_000_000 == 0:
                print(f"    … {lineno/1e6:.1f}M rows scanned, "
                      f"{len(area_by_prop):,} parcels with living area")

    elapsed = time.time() - t0
    print(f"    → Scanned in {elapsed:.1f}s  |  "
          f"{len(area_by_prop):,} parcels  |  "
          f"{skipped_area} unparseable area values")
    print(f"    Component counts: "
          + ", ".join(f"{k}={v:,}" for k, v in sorted(found_codes.items())))

    # Step 3: join with prop_map to get geo_id → sqft
    area_by_geo = {}
    missing_map = 0
    for prop_id_str, sqft in area_by_prop.items():
        geo_id = prop_map.get(prop_id_str)
        if geo_id:
            area_by_geo[geo_id] = sqft
        else:
            missing_map += 1

    if missing_map:
        print(f"    WARNING: {missing_map:,} prop_ids had no geo_id match "
              f"(supplements or out-of-scope parcels)")

    # Step 4: upsert into parcel.living_area_sqft
    print(f"  Upserting {len(area_by_geo):,} parcel sqft values…")
    t1 = time.time()

    sql = """
        UPDATE parcel
           SET living_area_sqft = %s
         WHERE geo_id = %s
    """

    rows = [(round(sqft, 2), geo_id) for geo_id, sqft in area_by_geo.items()]

    BATCH = 5000
    updated = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH):
            psycopg2.extras.execute_batch(cur, sql, rows[i:i+BATCH], page_size=2000)
            updated += min(BATCH, len(rows) - i)
        conn.commit()

    print(f"    → {updated:,} parcels updated with living_area_sqft in "
          f"{time.time()-t1:.1f}s")

    # Sanity-check a few known parcels
    _sanity_check(conn)

    return updated


def _sanity_check(conn):
    """Print living_area_sqft for a few well-known parcels."""
    print("\n  Sanity check — sample parcels:")
    sql = """
        SELECT geo_id, living_area_sqft
          FROM parcel
         WHERE living_area_sqft IS NOT NULL
         ORDER BY living_area_sqft DESC
         LIMIT 5
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            print(f"    geo_id={row[0]}  living_area_sqft={row[1]:,.0f} sqft")

    # Also check a few specific residential parcels
    test_geos = ["0100030105", "0204063005"]
    sql2 = """
        SELECT geo_id, living_area_sqft
          FROM parcel
         WHERE geo_id = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql2, (test_geos,))
        for row in cur.fetchall():
            sqft_str = f"{float(row[1]):,.0f}" if row[1] is not None else "NULL"
            print(f"    geo_id={row[0]}  living_area_sqft={sqft_str} sqft")


if __name__ == "__main__":
    conn = get_conn()
    load(conn)
    conn.close()
