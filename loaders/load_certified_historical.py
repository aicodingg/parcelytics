#!/usr/bin/env python3
"""
load_certified_historical.py — Load 2022, 2023, or 2024 Certified Appraisal Export.

Usage:
    cd ~/Desktop/Claude\ Files/parcel_app
    python3 loaders/load_certified_historical.py --year 2022
    python3 loaders/load_certified_historical.py --year 2023
    python3 loaders/load_certified_historical.py --year 2024

Behavior:
  - Reads PROP.TXT, PROP_ENT.TXT, LAND_DET.TXT from the year's Certified Export folder
  - Builds prop_id → geo_id lookup from PROP.TXT (does NOT update parcel table —
    the parcel table holds 2025 data and we don't overwrite it with historical values)
  - UPSERTs parcel_tax_year for the given tax_year:
      ON CONFLICT (geo_id, tax_year): replaces ajr_YYYY with cert_YYYY values
  - Updates land_value and derives imprv_value = max(0, market_value - land_value)
  - Reports: rows inserted vs updated, land/imprv null rates, elapsed time
  - Does NOT touch any other tax_year rows

Field positions (same across 2022–2025 exports, confirmed by file inspection):
  PROP.TXT      geo_id       [546:596]
  PROP_ENT.TXT  prop_val_yr  [12:17]   entity_cd   [53:63]
                assessed     [148:163]  taxable     [163:178]  market [388:403]
                exemptions   [178:388] (various fields)
  LAND_DET.TXT  prop_val_yr  [12:16]   land_seg_mkt_val [140:154]
"""

import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
import psycopg2
import psycopg2.extras

BATCH_SIZE = 5000

CERT_DIRS = {
    2022: os.path.join(config.DATA_DIR, "2022_Certified_Export"),
    2023: os.path.join(config.DATA_DIR, "2023_Certified_Export"),
    2024: os.path.join(config.DATA_DIR, "2024_Certified_Export"),
}

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


def _int(line, s):
    try:
        v = line[s].strip()
        return int(v) if v else None
    except (ValueError, IndexError):
        return None


def _str(line, s):
    try:
        return line[s].strip() or None
    except IndexError:
        return None


# ── Step 1: PROP.TXT → prop_id → geo_id mapping (no DB write) ────────────────
def build_pid_geo_map(cert_dir, year):
    """
    Read PROP.TXT and return {prop_id: geo_id} for sup_num=0 rows.
    Does NOT write to the parcel table — we don't overwrite 2025 parcel data.
    """
    path = os.path.join(cert_dir, "PROP.TXT")
    print(f"  Reading PROP.TXT ({os.path.getsize(path)/1e9:.2f} GB)…")
    t0 = time.time()
    pid_to_geo = {}
    skipped = 0

    with open(path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 600:
                continue
            sup_num = _int(line, slice(22, 34))
            if sup_num != 0:
                skipped += 1
                continue
            prop_id = _int(line, slice(0, 12))
            geo_id  = (_str(line, slice(546, 596)) or "")[:10].strip() or None
            if prop_id and geo_id:
                pid_to_geo[prop_id] = geo_id

    print(f"    {len(pid_to_geo):,} prop_id→geo_id mappings "
          f"({skipped:,} supplement rows skipped)  [{time.time()-t0:.1f}s]")
    return pid_to_geo


# ── Step 2: PROP_ENT.TXT → parcel_tax_year ────────────────────────────────────
def load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo):
    path = os.path.join(cert_dir, "PROP_ENT.TXT")
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.2f} GB)…")
    t0 = time.time()

    pty_sql = """
        INSERT INTO parcel_tax_year
            (geo_id, tax_year, market_value, assessed_value, taxable_value,
             exemption_codes, data_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year) DO UPDATE
            SET market_value    = EXCLUDED.market_value,
                assessed_value  = EXCLUDED.assessed_value,
                taxable_value   = EXCLUDED.taxable_value,
                exemption_codes = EXCLUDED.exemption_codes,
                data_source     = EXCLUDED.data_source
    """

    current_pid    = None
    accum          = {}
    rows_to_insert = []
    total          = 0
    parse_errors   = 0

    def flush(pid, acc):
        geo_id = pid_to_geo.get(pid)
        if not geo_id:
            return
        rows_to_insert.append((
            geo_id,
            acc.get("year", year),
            acc.get("market_value"),
            acc.get("assessed_value"),
            acc.get("taxable_value"),
            ",".join(sorted(acc.get("exemptions", set()))) or None,
            data_source,
        ))

    def commit_batch():
        nonlocal total
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, pty_sql, rows_to_insert, page_size=2000)
        conn.commit()
        total += len(rows_to_insert)
        rows_to_insert.clear()

    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 180:
                continue
            try:
                prop_id = _int(line, slice(0, 12))
                sup_num = _int(line, slice(17, 29))
                if sup_num != 0:
                    continue

                row_year  = _int(line, slice(12, 17))
                entity_cd = _str(line, slice(53, 63))
                assessed  = _int(line, slice(148, 163))
                taxable   = _int(line, slice(163, 178))
                market    = _int(line, slice(388, 403))

                if prop_id != current_pid:
                    if current_pid is not None and accum:
                        flush(current_pid, accum)
                    current_pid = prop_id
                    accum = {"year": row_year or year, "exemptions": set()}

                if market and not accum.get("market_value"):
                    accum["market_value"] = market

                # Prefer Travis County entity for assessed/taxable
                is_tco = entity_cd and entity_cd.strip().upper() in ("100303", "TCO", "03")
                if is_tco or not accum.get("assessed_value"):
                    accum["assessed_value"] = assessed
                    accum["taxable_value"]  = taxable

                for code, sl in EXEMPTION_FIELDS:
                    amt = _int(line, sl)
                    if amt and amt > 0:
                        accum["exemptions"].add(code.upper())

            except Exception:
                parse_errors += 1
                continue

            if len(rows_to_insert) >= BATCH_SIZE:
                commit_batch()
                if total % 100_000 == 0:
                    print(f"    … {total:,} rows committed", flush=True)

    # Flush last parcel
    if current_pid is not None and accum:
        flush(current_pid, accum)
    if rows_to_insert:
        commit_batch()

    if parse_errors:
        print(f"    ⚠️  {parse_errors:,} lines skipped due to parse errors")
    print(f"    → {total:,} parcel-year rows upserted  [{time.time()-t0:.1f}s]")
    return total


# ── Step 3: LAND_DET.TXT → land_value + imprv_value ──────────────────────────
def load_land_imprv(conn, cert_dir, year, data_source, pid_to_geo):
    path = os.path.join(cert_dir, "LAND_DET.TXT")
    print(f"  Loading LAND_DET.TXT…")
    t0 = time.time()

    # Sum land segment market values per prop_id
    land_totals = defaultdict(int)
    with open(path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 155:
                continue
            prop_id = _int(line, slice(0, 12))
            val     = _int(line, slice(140, 154))
            if prop_id and val:
                land_totals[prop_id] += val

    print(f"    {len(land_totals):,} parcels with land segments")

    # Fetch market_value for this year to derive imprv_value = market - land
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geo_id, market_value FROM parcel_tax_year WHERE tax_year = %s",
            (year,)
        )
        geo_to_market = {r[0]: r[1] for r in cur.fetchall()}

    update_sql = """
        UPDATE parcel_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE geo_id = %s AND tax_year = %s
    """
    updates = []
    for prop_id, land_val in land_totals.items():
        geo_id = pid_to_geo.get(prop_id)
        if not geo_id:
            continue
        market_val = geo_to_market.get(geo_id) or 0
        imprv_val  = max(0, market_val - land_val)
        updates.append((land_val, imprv_val, geo_id, year))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} parcels  [{time.time()-t0:.1f}s]")
    return len(updates)


# ── Post-load summary ─────────────────────────────────────────────────────────
def post_load_summary(conn, year, data_source, rows_before, ajr_before):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s",
            (year,)
        )
        rows_after = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (year, data_source)
        )
        cert_count = cur.fetchone()[0]

        cur.execute("""
            SELECT
                COUNT(*)           AS total,
                COUNT(land_value)  AS lv_non_null,
                COUNT(imprv_value) AS iv_non_null
            FROM parcel_tax_year
            WHERE tax_year = %s AND data_source = %s
        """, (year, data_source))
        total, lv_nn, iv_nn = cur.fetchone()

    inserted = rows_after - rows_before
    updated  = cert_count - inserted

    print(f"\n{'='*65}")
    print(f"  LOAD COMPLETE — {year} Certified Roll  (data_source='{data_source}')")
    print(f"{'='*65}")
    print(f"  AJR rows before load     : {ajr_before:>10,}")
    print(f"  Cert rows after load     : {cert_count:>10,}")
    print(f"  Inserted (new parcels)   : {inserted:>10,}  ← in cert, not in AJR")
    print(f"  Updated  (AJR→cert)      : {updated:>10,}  ← replaced AJR values")
    print(f"\n  land_value  non-null     : {lv_nn:>10,} / {total:,}  ({lv_nn/max(total,1)*100:.1f}%)")
    print(f"  imprv_value non-null     : {iv_nn:>10,} / {total:,}  ({iv_nn/max(total,1)*100:.1f}%)")
    print(f"{'='*65}\n")

    return inserted, updated


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--year', type=int, required=True, choices=[2022, 2023, 2024],
                    help='Tax year to load (2022, 2023, or 2024)')
    args = ap.parse_args()

    year        = args.year
    data_source = f"cert_{year}"
    cert_dir    = CERT_DIRS[year]
    ajr_source  = f"ajr_{year}"

    if not os.path.isdir(cert_dir):
        print(f"ERROR: Cert dir not found: {cert_dir}")
        sys.exit(1)

    print(f"\n{'─'*65}")
    print(f"  Loading {year} Certified Appraisal Export")
    print(f"  Source dir : {cert_dir}")
    print(f"  data_source: {data_source}")
    print(f"{'─'*65}\n")

    conn = psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER,
        password=config.DB_PASS
    )

    # Snapshot counts before load
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s", (year,))
        rows_before = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s",
            (year, ajr_source)
        )
        ajr_before = cur.fetchone()[0]

    print(f"  Rows in parcel_tax_year[{year}] before load : {rows_before:,}")
    print(f"  Of which data_source='{ajr_source}'         : {ajr_before:,}\n")

    t_total = time.time()

    pid_to_geo = build_pid_geo_map(cert_dir, year)
    load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo)
    load_land_imprv(conn, cert_dir, year, data_source, pid_to_geo)

    print(f"\n  Total elapsed: {time.time()-t_total:.1f}s")

    post_load_summary(conn, year, data_source, rows_before, ajr_before)

    conn.close()
    print(f"Done. Run compute_metrics.py after loading all three years.")


if __name__ == "__main__":
    main()
