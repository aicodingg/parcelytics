"""
Load 2025 Certified Appraisal Export (EARS fixed-width format).

Files used:
  PROP.TXT       — one row per parcel (property + owner)
  PROP_ENT.TXT   — one row per parcel × entity (values, exemptions)
  LAND_DET.TXT   — land segment detail (land value)
  IMP_INFO.TXT   — improvement info (improvement value)

PROP.TXT field positions (1-based from layout, converted to 0-based slices):
  prop_id      [0:12]   int
  prop_type_cd [12:17]  char
  prop_val_yr  [17:22]  int
  sup_num      [22:34]  int   — 0 = certified, skip supplements
  geo_id       [546:596] char (50) — trim to actual account
  owner_id     [596:608] int
  owner_name   [608:678] char (70)
  addr_line1   [693:753] char (60)  — mailing address (situs often embedded in earlier fields)

PROP_ENT.TXT field positions (0-based):
  prop_id      [0:12]
  prop_val_yr  [12:17]
  sup_num      [17:29]
  owner_id     [29:41]
  entity_id    [41:53]
  entity_cd    [53:63]   — 'A       ' = TCAD aggregate; others = individual entities
  entity_name  [63:113]
  entity_xref  [113:133]
  filler       [133:148]
  assessed_val [148:163]
  taxable_val  [163:178]
  ab_amt       [178:193]
  en_amt       [193:208]
  fr_amt       [208:223]
  ht_amt       [223:238]
  pro_amt      [238:253]
  pc_amt       [253:268]
  so_amt       [268:283]
  ex366_amt    [283:298]
  hs_amt       [298:313]
  ov65_amt     [313:328]
  dp_amt       [328:343]
  dv_amt       [343:358]
  ex_amt       [358:373]
  ch_amt       [373:388]
  market_value [388:403]
  appraised_val[403:418]

LAND_DET.TXT field positions (0-based):
  prop_id      [0:12]
  prop_val_yr  [12:16]
  land_seg_id  [16:28]
  …
  land_seg_mkt_val [112:126]  numeric(14)

IMP_INFO.TXT field positions (0-based):
  prop_id      [0:12]
  prop_val_yr  [12:16]
  imprv_id     [16:28]
  …
  imprv_val    [68:82]   numeric(14)
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema

import psycopg2.extras


# ── Exemption codes derived from non-zero fields ───────────────────────────────
EXEMPTION_FIELDS = [
    ("hs",    slice(298, 313)),
    ("ov65",  slice(313, 328)),
    ("dp",    slice(328, 343)),
    ("dv",    slice(343, 358)),
    ("ab",    slice(178, 193)),
    ("fr",    slice(208, 223)),
    ("ht",    slice(223, 238)),
    ("ch",    slice(373, 388)),
    ("ex366", slice(283, 298)),
]


def _int_field(line, s):
    try:
        return int(line[s].strip()) if line[s].strip() else None
    except (ValueError, IndexError):
        return None


def _str_field(line, s):
    try:
        return line[s].strip() or None
    except IndexError:
        return None


# ── Step 1: PROP.TXT → parcel table ──────────────────────────────────────────
def load_prop_txt(conn, cert_dir):
    path = os.path.join(cert_dir, "PROP.TXT")
    print(f"  Loading PROP.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    parcel_sql = """
        INSERT INTO parcel
            (geo_id, prop_id, prop_type_cd, owner_id, owner_name)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (geo_id) DO UPDATE
            SET prop_id      = EXCLUDED.prop_id,
                prop_type_cd = EXCLUDED.prop_type_cd,
                owner_id     = EXCLUDED.owner_id,
                owner_name   = EXCLUDED.owner_name
    """

    rows  = []
    total = 0

    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 600:
                continue
            sup_num = _int_field(line, slice(22, 34))
            if sup_num != 0:           # skip supplement rows
                continue

            geo_id       = (_str_field(line, slice(546, 596)) or "")[:10].strip() or None
            prop_id      = _int_field(line, slice(0, 12))
            prop_type_cd = _str_field(line, slice(12, 17))
            owner_id     = _int_field(line, slice(596, 608))
            owner_name   = _str_field(line, slice(608, 678))

            if not geo_id:
                continue

            rows.append((geo_id, prop_id, prop_type_cd, owner_id, owner_name))

            if len(rows) >= 5000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, parcel_sql, rows, page_size=2000)
                conn.commit()
                total += len(rows)
                rows = []

            if lineno % 100_000 == 0:
                print(f"    … {lineno:,} lines, {total:,} committed")

    if rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, parcel_sql, rows, page_size=2000)
        conn.commit()
        total += len(rows)

    print(f"    → {total:,} parcels in {time.time()-t0:.1f}s")
    return total


# ── Step 2: PROP_ENT.TXT → parcel_tax_year + entity values ──────────────────
def load_prop_ent_txt(conn, cert_dir):
    path = os.path.join(cert_dir, "PROP_ENT.TXT")
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    # We aggregate across all entity rows per parcel:
    #   market_value   = from any row (same for all entities)
    #   assessed_value = from TCO (Travis County) entity, else first seen
    #   taxable_value  = from TCO entity
    #   exemption_codes = union of non-zero exemption fields

    # Build per-prop_id accumulator in memory (prop_id → dict)
    # PROP_ENT is sorted by prop_id, so we can stream
    pty_sql = """
        INSERT INTO parcel_tax_year
            (geo_id, tax_year, market_value, assessed_value, taxable_value,
             exemption_codes, data_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year) DO UPDATE
            SET market_value   = EXCLUDED.market_value,
                assessed_value = EXCLUDED.assessed_value,
                taxable_value  = EXCLUDED.taxable_value,
                exemption_codes= EXCLUDED.exemption_codes,
                data_source    = EXCLUDED.data_source
    """

    # Also maintain a lookup prop_id → geo_id from the parcel table
    print("    Building prop_id → geo_id lookup…")
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        pid_to_geo = {row[0]: row[1] for row in cur.fetchall()}
    print(f"    {len(pid_to_geo):,} parcels in lookup")

    current_pid  = None
    accum        = {}
    rows_to_insert = []
    total        = 0

    def flush(pid, acc):
        geo_id = pid_to_geo.get(pid)
        if not geo_id:
            return
        rows_to_insert.append((
            geo_id,
            acc.get("year", 2025),
            acc.get("market_value"),
            acc.get("assessed_value"),
            acc.get("taxable_value"),
            ",".join(sorted(acc.get("exemptions", set()))) or None,
            "certified",
        ))

    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 180:
                continue

            prop_id = _int_field(line, slice(0, 12))
            sup_num = _int_field(line, slice(17, 29))
            if sup_num != 0:
                continue

            year        = _int_field(line, slice(12, 17))
            entity_cd   = _str_field(line, slice(53, 63))
            assessed    = _int_field(line, slice(148, 163))
            taxable     = _int_field(line, slice(163, 178))
            market      = _int_field(line, slice(388, 403))

            # Flush when prop_id changes
            if prop_id != current_pid:
                if current_pid is not None and accum:
                    flush(current_pid, accum)
                current_pid = prop_id
                accum = {"year": year, "exemptions": set()}

            # market_value is the same regardless of entity
            if market and not accum.get("market_value"):
                accum["market_value"] = market

            # Prefer TCO (Travis County) or first entity for assessed/taxable
            is_tco = entity_cd and entity_cd.strip().upper() in ("100303", "TCO")
            if is_tco or not accum.get("assessed_value"):
                accum["assessed_value"] = assessed
                accum["taxable_value"]  = taxable

            # Collect exemption codes from non-zero amounts
            for code, sl in EXEMPTION_FIELDS:
                amt = _int_field(line, sl)
                if amt and amt > 0:
                    accum["exemptions"].add(code.upper())

            if len(rows_to_insert) >= 5000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, pty_sql, rows_to_insert, page_size=2000)
                conn.commit()
                total += len(rows_to_insert)
                rows_to_insert = []

            if lineno % 500_000 == 0:
                print(f"    … {lineno:,} lines, {total:,} committed")

    # Flush last parcel
    if current_pid is not None and accum:
        flush(current_pid, accum)

    if rows_to_insert:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, pty_sql, rows_to_insert, page_size=2000)
        conn.commit()
        total += len(rows_to_insert)

    print(f"    → {total:,} parcel-year rows in {time.time()-t0:.1f}s")
    return total


# ── Step 3: LAND_DET.TXT + IMP_INFO.TXT → land_value / imprv_value ──────────
def load_land_and_imprv(conn, cert_dir):
    """Sum land and improvement values per parcel and update parcel_tax_year."""
    print("  Loading LAND_DET.TXT…")
    t0 = time.time()

    # land_seg_mkt_val at [112:126] per layout
    land_totals = {}  # prop_id → total land value
    land_path = os.path.join(cert_dir, "LAND_DET.TXT")
    with open(land_path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 128:
                continue
            prop_id = _int_field(line, slice(0, 12))
            val     = _int_field(line, slice(112, 126))
            if prop_id and val:
                land_totals[prop_id] = land_totals.get(prop_id, 0) + val

    print(f"    {len(land_totals):,} parcels with land detail")

    print("  Loading IMP_INFO.TXT…")
    imprv_totals = {}  # prop_id → total improvement value
    imprv_path = os.path.join(cert_dir, "IMP_INFO.TXT")
    with open(imprv_path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 84:
                continue
            prop_id = _int_field(line, slice(0, 12))
            val     = _int_field(line, slice(68, 82))
            if prop_id and val:
                imprv_totals[prop_id] = imprv_totals.get(prop_id, 0) + val

    print(f"    {len(imprv_totals):,} parcels with improvement detail")

    # Fetch prop_id → geo_id
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        pid_to_geo = {r[0]: r[1] for r in cur.fetchall()}

    update_sql = """
        UPDATE parcel_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE geo_id = %s AND tax_year = 2025
    """
    updates = []
    all_pids = set(land_totals) | set(imprv_totals)
    for pid in all_pids:
        geo_id = pid_to_geo.get(pid)
        if geo_id:
            updates.append((land_totals.get(pid), imprv_totals.get(pid), geo_id))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} parcels in {time.time()-t0:.1f}s")
    return len(updates)


def load(conn):
    cert_dir = config.CERT_DIR
    if not os.path.isdir(cert_dir):
        print(f"  WARNING: {cert_dir} not found, skipping 2025 Certified")
        return 0

    load_prop_txt(conn, cert_dir)
    load_prop_ent_txt(conn, cert_dir)
    load_land_and_imprv(conn, cert_dir)
    print("  2025 Certified Export loaded.")


if __name__ == "__main__":
    conn = get_conn()
    execute_schema(conn)
    load(conn)
    conn.close()
