"""
Load AJR (Annual Jurisdiction Roll) CSV files for tax years 2021–2024.

AJR format: comma-delimited, no header, one row per parcel × taxing entity.
We read only the aggregate entity rows (field[3] == '227000') to get one row
per unique parcel per year.

Confirmed field positions (0-based):
  [1]  tax_year
  [3]  entity_code      ('227000' = Travis County aggregate)
  [6]  geo_id           TCAD long account (10 chars, e.g. '0100030105')
  [7]  prop_id          TCAD short integer ID
  [9]  situs_address
  [11] legal_desc
  [16] neighborhood_cd
  [24] ptd_state_cd     property class code (A=SFR, F1=commercial, D1=ag …)
  [29] owner_id
  [30] state_cd1
  [31] state_cd2
  [32] market_value     (confirmed for 2022/2024/2025 cross-refs)
  [34] assessed_value   (market minus HS cap; anomalous in some 2021–2023 rows —
                         see NOTE in README; stored as-is for review)
  [35] hs_cap_loss
  [-- 2025 AJR only --]
  [-4] zip_code         (second-to-last meaningful fields added in 2025 format)
  [-3] latitude
  [-2] longitude

NOTE: 2021 file has two copies of data (_PTD.csv and _PTD_AJR_RECORDS.csv).
      We use the _PTD.csv (slightly larger) as the canonical source.
"""
import csv
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema

import psycopg2.extras


AGGREGATE_ENTITY = "227000"

# NOTE: AJR field[6] is prop_id (not geo_id). We resolve geo_id by joining
# to the parcel table (populated from the 2025 Certified Export first).
# For parcels not in the certified export, we synthesise a geo_id as
# "AJR" + str(prop_id).zfill(7) so they don't collide with real accounts.

PARCEL_SQL = """
    INSERT INTO parcel (geo_id, prop_id, situs_address, legal_desc,
                        neighborhood_cd, state_cd1, state_cd2, owner_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (geo_id) DO UPDATE
        SET situs_address  = COALESCE(parcel.situs_address,  EXCLUDED.situs_address),
            legal_desc     = COALESCE(parcel.legal_desc,     EXCLUDED.legal_desc),
            neighborhood_cd= COALESCE(parcel.neighborhood_cd,EXCLUDED.neighborhood_cd),
            state_cd1      = COALESCE(parcel.state_cd1,      EXCLUDED.state_cd1),
            state_cd2      = COALESCE(parcel.state_cd2,      EXCLUDED.state_cd2),
            owner_id       = COALESCE(parcel.owner_id,       EXCLUDED.owner_id)
"""

PTY_SQL = """
    INSERT INTO parcel_tax_year
        (geo_id, tax_year, market_value, assessed_value, hs_cap_loss, data_source)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (geo_id, tax_year) DO UPDATE
        SET market_value   = EXCLUDED.market_value,
            assessed_value = EXCLUDED.assessed_value,
            hs_cap_loss    = EXCLUDED.hs_cap_loss,
            data_source    = EXCLUDED.data_source
"""


def _int_or_none(v):
    try:
        s = v.strip() if v else ""
        return int(float(s)) if s else None
    except (ValueError, AttributeError):
        return None


def _clean_geo_id(v):
    """Return the 10-char TCAD long account, stripped of whitespace."""
    return v.strip()[:14] if v else None


def build_pid_lookup(conn):
    """Return {prop_id: geo_id} from parcels already in the DB (from certified export)."""
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        return {row[0]: row[1] for row in cur.fetchall()}


def load_year(conn, year, filepath, pid_lookup):
    t0 = time.time()
    print(f"  Loading {year} AJR: {os.path.basename(filepath)}")

    parcel_rows = []
    pty_rows    = []
    seen        = set()

    with open(filepath, encoding="latin-1", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for lineno, fields in enumerate(reader, 1):
            if len(fields) < 36:
                continue
            # Only aggregate entity rows
            if fields[3].strip() != AGGREGATE_ENTITY:
                continue

            # 2022+: field[6] = geo_id (10-char account), field[7] = prop_id
            # 2021:  field[6] = prop_id, field[7] = prop_id (no geo_id in file)
            f6 = fields[6].strip()
            f7 = _int_or_none(fields[7])

            if len(f6) == 10 and f6.isdigit():
                # 2022+ format: geo_id is directly available
                geo_id  = f6
                prop_id = f7
            else:
                # 2021 format: field[6] is prop_id, look up geo_id
                prop_id = _int_or_none(f6)
                geo_id  = pid_lookup.get(prop_id) or f"AJR{prop_id}"

            if not geo_id or geo_id in seen:
                continue
            seen.add(geo_id)

            address      = fields[9].strip()
            legal        = fields[11].strip()
            # 2021: neighborhood at [17]; 2022+: at [16]
            nbhd         = (fields[16].strip() or fields[17].strip()) if len(fields) > 17 else ""
            state_cd1    = fields[30].strip()
            state_cd2    = fields[31].strip()
            owner_id     = _int_or_none(fields[29])
            market_val   = _int_or_none(fields[32])
            assessed_val = _int_or_none(fields[34])
            hs_cap       = _int_or_none(fields[35])

            parcel_rows.append((geo_id, prop_id, address, legal,
                                nbhd, state_cd1, state_cd2, owner_id))
            pty_rows.append((geo_id, year, market_val, assessed_val,
                             hs_cap, f"ajr_{year}"))

            if lineno % 500_000 == 0:
                print(f"    … {lineno:,} lines scanned, {len(seen):,} parcels")

    # Bulk insert
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, PARCEL_SQL, parcel_rows, page_size=2000)
        psycopg2.extras.execute_batch(cur, PTY_SQL,    pty_rows,    page_size=2000)
    conn.commit()

    elapsed = time.time() - t0
    print(f"    → {len(seen):,} parcels loaded in {elapsed:.1f}s")
    return len(seen)


def load(conn):
    print("  Building prop_id → geo_id lookup from certified data…")
    pid_lookup = build_pid_lookup(conn)
    print(f"  {len(pid_lookup):,} certified parcels in lookup")

    total = 0
    for year, filepath in sorted(config.AJR_FILES.items()):
        if not os.path.exists(filepath):
            print(f"  WARNING: {filepath} not found, skipping {year}")
            continue
        total += load_year(conn, year, filepath, pid_lookup)
    print(f"  AJR total: {total:,} parcel-year rows")
    return total


if __name__ == "__main__":
    conn = get_conn()
    execute_schema(conn)
    load(conn)
    conn.close()
