"""
migrate_add_sqft.py — Additive schema migration: add living_area_sqft to parcel.

Safe to run multiple times (IF NOT EXISTS guard).
Run BEFORE load_imp_det_sqft.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders.db import get_conn

MIGRATION = """
ALTER TABLE parcel
    ADD COLUMN IF NOT EXISTS living_area_sqft NUMERIC(10, 2);

COMMENT ON COLUMN parcel.living_area_sqft IS
    'Total heated/cooled living area in square feet, summed from '
    'IMP_DET.TXT component codes: 1ST, 2ND, 3RD, 1/2, RSBLW, FBSMT. '
    'NULL if the parcel has no improvement detail (vacant land, '
    'exempt-only parcels, or IMP_DET not yet loaded).';
"""


def run():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(MIGRATION)
    conn.commit()
    conn.close()
    print("Migration complete: parcel.living_area_sqft column added (if not already present).")


if __name__ == "__main__":
    run()

