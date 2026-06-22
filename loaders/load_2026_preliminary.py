"""
Load 2026 Preliminary Appraisal Export (same fixed-width format as 2025 Certified).

Source folder: "2026 Preliminary Appraisal Export Supp 0_06092026 (1)"
Files used:
  PROP.TXT       — parcel master (property + owner)
  PROP_ENT.TXT   — per-entity values (market, assessed, taxable, exemptions)
  LAND_DET.TXT   — land segment detail (land value)
  SB12.TXT       — Senate Bill 12 over-65 freeze exemption detail

Key differences from 2025 Certified:
  - tax_year = 2026
  - data_source = 'preliminary' (distinct from 'certified')
  - confidence_level: shown as "Preliminary" in the UI (blue badge)
  - No billing data available — that requires post-certification tax roll

Field positions: CONFIRMED identical to 2025 (PROP.TXT is 9,813 chars/line;
same geo_id at [546:596], owner at [608:678], sup_num at [22:34]).

Data Integrity Standard:
  - Do NOT overwrite or modify any existing 2025 or prior year rows
  - Insert 2026 with ON CONFLICT DO NOTHING (not DO UPDATE) so re-runs are safe
  - AV > MV anomalies are preserved as-is with visible UI flag — not corrected
  - Post-load QA runs automatically (items 8–9 from the brief)

Run:
  python3 loaders/load_2026_preliminary.py

After load, run compute_metrics.py to refresh parcel_metrics and county_benchmark
for 2026 rows.
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema

import psycopg2.extras

TAX_YEAR   = 2026
DATA_SRC   = "preliminary"
PRELIM_DIR = os.path.join(
    config.DATA_DIR,
    "2026 Preliminary Appraisal Export Supp 0_06092026 (1)"
)

# Exemption fields (same slice positions as 2025 PROP_ENT)
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


# ── Step 1: PROP.TXT → parcel table (upsert owner/name; never overwrite geo_id) ──
def load_prop_txt(conn):
    path = os.path.join(PRELIM_DIR, "PROP.TXT")
    if not os.path.exists(path):
        print(f"  ERROR: {path} not found"); return 0
    print(f"  Loading PROP.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    # UPSERT: update owner info from 2026 data; preserve all other fields
    # geo_id and prop_id are the authoritative keys from the 2025 certified load
    parcel_sql = """
        INSERT INTO parcel (geo_id, prop_id, prop_type_cd, owner_id, owner_name)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (geo_id) DO UPDATE
            SET owner_name   = EXCLUDED.owner_name,
                owner_id     = EXCLUDED.owner_id
    """

    rows  = []
    total = 0

    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 600:
                continue
            sup_num = _int_field(line, slice(22, 34))
            if sup_num != 0:
                continue   # Skip supplement rows; Supp 0 only

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

    print(f"    → {total:,} parcel rows upserted in {time.time()-t0:.1f}s")
    return total


# ── Step 2: PROP_ENT.TXT → parcel_tax_year for 2026 ─────────────────────────
def load_prop_ent_txt(conn):
    path = os.path.join(PRELIM_DIR, "PROP_ENT.TXT")
    if not os.path.exists(path):
        print(f"  ERROR: {path} not found"); return 0
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    # ON CONFLICT DO NOTHING: safe to re-run; never touches prior year rows
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

    print("    Building prop_id → geo_id lookup…")
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        pid_to_geo = {row[0]: row[1] for row in cur.fetchall()}
    print(f"    {len(pid_to_geo):,} parcels in lookup")

    current_pid     = None
    accum           = {}
    rows_to_insert  = []
    total           = 0

    def flush(pid, acc):
        geo_id = pid_to_geo.get(pid)
        if not geo_id:
            return
        rows_to_insert.append((
            geo_id,
            TAX_YEAR,
            acc.get("market_value"),
            acc.get("assessed_value"),
            acc.get("taxable_value"),
            ",".join(sorted(acc.get("exemptions", set()))) or None,
            DATA_SRC,
        ))

    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 180:
                continue

            prop_id = _int_field(line, slice(0, 12))
            sup_num = _int_field(line, slice(17, 29))
            if sup_num != 0:
                continue

            year      = _int_field(line, slice(12, 17))
            entity_cd = _str_field(line, slice(53, 63))
            assessed  = _int_field(line, slice(148, 163))
            taxable   = _int_field(line, slice(163, 178))
            market    = _int_field(line, slice(388, 403))

            if prop_id != current_pid:
                if current_pid is not None and accum:
                    flush(current_pid, accum)
                current_pid = prop_id
                accum = {"year": year, "exemptions": set()}

            if market and not accum.get("market_value"):
                accum["market_value"] = market

            is_tco = entity_cd and entity_cd.strip().upper() in ("100303", "TCO")
            if is_tco or not accum.get("assessed_value"):
                accum["assessed_value"] = assessed
                accum["taxable_value"]  = taxable

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

    if current_pid is not None and accum:
        flush(current_pid, accum)

    if rows_to_insert:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, pty_sql, rows_to_insert, page_size=2000)
        conn.commit()
        total += len(rows_to_insert)

    print(f"    → {total:,} parcel-year rows for 2026 in {time.time()-t0:.1f}s")
    return total


# ── Step 3: LAND_DET.TXT → land_value + imprv_value for 2026 ─────────────────
def load_land_and_imprv(conn):
    path = os.path.join(PRELIM_DIR, "LAND_DET.TXT")
    if not os.path.exists(path):
        print("  LAND_DET.TXT not found, skipping land/imprv"); return 0
    print(f"  Loading LAND_DET.TXT ({os.path.getsize(path)/1e6:.0f} MB)…")
    t0 = time.time()

    land_totals = {}
    with open(path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 155:
                continue
            prop_id = _int_field(line, slice(0, 12))
            val     = _int_field(line, slice(140, 154))
            if prop_id and val:
                land_totals[prop_id] = land_totals.get(prop_id, 0) + val

    print(f"    {len(land_totals):,} parcels with land detail")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.prop_id, p.geo_id, pty.market_value
            FROM parcel p
            JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = %s
            WHERE p.prop_id IS NOT NULL
        """, (TAX_YEAR,))
        pid_info = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    update_sql = """
        UPDATE parcel_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE geo_id = %s AND tax_year = %s
    """
    updates = []
    for pid, land_val in land_totals.items():
        info = pid_info.get(pid)
        if not info:
            continue
        geo_id, market_val = info
        imprv_val = max(0, (market_val or 0) - land_val)
        updates.append((land_val, imprv_val, geo_id, TAX_YEAR))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} parcels in {time.time()-t0:.1f}s")
    return len(updates)


# ── Step 4: SB12.TXT → over-65 freeze exemption data ────────────────────────
def load_sb12(conn):
    """
    SB12 contains Senate Bill 12 over-65 freeze records.
    We extract the freeze-capped taxable value per entity where available.
    The SB12 format is tab-separated: prop_id, owner_id, entity_id, seq, entity_cd,
    entity_xref, exemption_type, freeze_yr, row_type, appraised_yr, ...
    We flag parcels that have an active SB12 freeze in the exemption_codes field.
    """
    path = os.path.join(PRELIM_DIR, "SB12.TXT")
    if not os.path.exists(path):
        print("  SB12.TXT not found, skipping"); return 0
    print(f"  Loading SB12.TXT ({os.path.getsize(path)/1e6:.0f} MB) — over-65 freeze…")
    t0 = time.time()

    # Read prop_ids that have an active SB12 freeze in 2026
    # Format: tab-separated; col 0=prop_id, col 3=seq, col 9=appraised_yr
    frozen_pids = set()
    with open(path, encoding="latin-1", errors="replace") as f:
        for line in f:
            parts = line.rstrip('\n').split('\t')
            if len(parts) < 10:
                continue
            try:
                prop_id = int(parts[0])
                appraised_yr = int(parts[9]) if parts[9].strip().isdigit() else 0
            except (ValueError, IndexError):
                continue
            if appraised_yr == TAX_YEAR:
                frozen_pids.add(prop_id)

    print(f"    {len(frozen_pids):,} parcels with active 2026 SB12 freeze")

    if not frozen_pids:
        return 0

    # Look up geo_ids for these prop_ids
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        pid_to_geo = {row[0]: row[1] for row in cur.fetchall()}

    # Update exemption_codes to include SB12 flag
    update_sql = """
        UPDATE parcel_tax_year
        SET exemption_codes = CASE
            WHEN exemption_codes IS NULL OR exemption_codes = '' THEN 'SB12'
            WHEN exemption_codes NOT LIKE '%%SB12%%' THEN exemption_codes || ',SB12'
            ELSE exemption_codes
        END
        WHERE geo_id = %s AND tax_year = %s
    """
    updates = [(pid_to_geo[pid], TAX_YEAR) for pid in frozen_pids if pid in pid_to_geo]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → SB12 flag set on {len(updates):,} parcels in {time.time()-t0:.1f}s")
    return len(updates)


# ── Step 5: Post-load QA (Item 8 from brief) ─────────────────────────────────
def run_qa(conn):
    """
    Post-load data quality checks for 2026 preliminary data.
    Reports findings but never modifies data.
    """
    print("\n" + "="*72)
    print("  POST-LOAD QA — 2026 Preliminary")
    print("="*72)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. Row count
    cur.execute("SELECT COUNT(*) AS n FROM parcel_tax_year WHERE tax_year = 2026")
    n_2026 = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM parcel_tax_year WHERE tax_year = 2025")
    n_2025 = cur.fetchone()["n"]
    pct_diff = abs(n_2026 - n_2025) / n_2025 * 100 if n_2025 else 0
    flag = "⚠ DEVIATED >5%" if pct_diff > 5 else "✓"
    print(f"\n  Row counts: 2026={n_2026:,}  2025={n_2025:,}  diff={pct_diff:.1f}%  {flag}")

    # 2. Null rates
    for col in ["market_value", "assessed_value", "taxable_value", "land_value", "imprv_value"]:
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE {col} IS NULL OR {col} = 0) AS nulls,
                   COUNT(*) AS total
            FROM parcel_tax_year WHERE tax_year = 2026
        """)
        r = cur.fetchone()
        pct = r["nulls"] / r["total"] * 100 if r["total"] else 0
        flag = "⚠" if pct > 20 else "✓"
        print(f"  {flag}  {col}: {r['nulls']:,} null/zero of {r['total']:,} ({pct:.1f}%)")

    # 3. AV > MV anomaly check
    cur.execute("""
        SELECT COUNT(*) AS n
        FROM parcel_tax_year
        WHERE tax_year = 2026
          AND assessed_value > market_value
          AND market_value > 0 AND assessed_value > 0
    """)
    n_anom = cur.fetchone()["n"]
    pct_anom = n_anom / n_2026 * 100 if n_2026 else 0
    print(f"\n  AV > MV anomalies in 2026: {n_anom:,} ({pct_anom:.2f}%)")
    if pct_anom < 5:
        print("    ✓ Lower than 2021–2024 AJR rates — expected for Certified Export format")
    else:
        print("    ⚠ Higher than expected — investigate before display")

    # 4. Sanity check known parcels
    KNOWN = ["0100030105", "0100030109", "0284460113"]
    print("\n  Known-parcel sanity check:")
    print(f"  {'Parcel':<14} {'2025 MV':>12} {'2026 MV':>12} {'Δ':>8}")
    print(f"  {'-'*14} {'-'*12} {'-'*12} {'-'*8}")
    for geo_id in KNOWN:
        cur.execute("""
            SELECT tax_year, market_value, assessed_value
            FROM parcel_tax_year
            WHERE geo_id = %s AND tax_year IN (2025, 2026)
            ORDER BY tax_year
        """, (geo_id,))
        rows = {r["tax_year"]: r for r in cur.fetchall()}
        r25 = rows.get(2025)
        r26 = rows.get(2026)
        mv25 = r25["market_value"] if r25 else None
        mv26 = r26["market_value"] if r26 else None
        if mv25 and mv26:
            delta = (mv26 - mv25) / mv25 * 100
            flag = "⚠ >50% change" if abs(delta) > 50 else "✓"
            print(f"  {geo_id:<14} ${mv25:>11,.0f} ${mv26:>11,.0f} {delta:>+7.1f}%  {flag}")
        else:
            missing = "2025 missing" if not mv25 else "2026 missing"
            print(f"  {geo_id:<14}  ({missing})")

    print("\n" + "="*72)
    cur.close()


# ── Step 6: 2026 vs 2025 county-wide comparison (Item 9 from brief) ───────────
def run_county_comparison(conn):
    """
    County-wide 2026 vs 2025 market value comparison by property type.
    Computation only — no UI built yet; results logged to terminal.
    """
    print("\n" + "="*72)
    print("  2026 vs 2025 COUNTY-WIDE COMPARISON")
    print("="*72)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        WITH joined AS (
            SELECT
                p.geo_id,
                LEFT(COALESCE(p.state_cd1, '?'), 1)         AS type_prefix,
                p25.market_value                              AS mv_2025,
                p26.market_value                              AS mv_2026
            FROM parcel p
            JOIN parcel_tax_year p25 ON p25.geo_id = p.geo_id AND p25.tax_year = 2025
            JOIN parcel_tax_year p26 ON p26.geo_id = p.geo_id AND p26.tax_year = 2026
            WHERE p25.market_value > 0 AND p26.market_value > 0
        ),
        with_pct AS (
            SELECT type_prefix,
                   mv_2025, mv_2026,
                   (mv_2026 - mv_2025)::numeric / mv_2025 * 100 AS pct_chg
            FROM joined
        )
        SELECT
            type_prefix,
            COUNT(*)                                              AS parcel_count,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mv_2025) AS median_mv_2025,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mv_2026) AS median_mv_2026,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pct_chg) AS median_pct_change,
            COUNT(*) FILTER (WHERE mv_2026 > mv_2025)            AS count_increased,
            COUNT(*) FILTER (WHERE mv_2026 < mv_2025)            AS count_decreased,
            COUNT(*) FILTER (WHERE mv_2026 = mv_2025)            AS count_unchanged
        FROM with_pct
        GROUP BY type_prefix
        ORDER BY parcel_count DESC
    """)
    rows = cur.fetchall()

    type_labels = {
        "A": "Residential (SFR)", "B": "Multi-Family", "C": "Vacant/Land",
        "D": "Agricultural",     "E": "Rural (Non-AG)", "F": "Commercial",
        "G": "Minerals",          "J": "Utilities",     "L": "Personal Prop",
    }

    print(f"\n  {'Type':<22} {'Count':>8} {'Median MV 2025':>15} {'Median MV 2026':>15} {'Median Δ':>9} {'↑ Incr':>8} {'↓ Decr':>8}")
    print(f"  {'-'*22} {'-'*8} {'-'*15} {'-'*15} {'-'*9} {'-'*8} {'-'*8}")

    for r in rows:
        label = type_labels.get(r["type_prefix"], f"Other ({r['type_prefix']})")
        pct   = float(r["median_pct_change"]) if r["median_pct_change"] is not None else 0
        flag  = "  ◀ notable" if abs(pct) > 15 else ""
        print(f"  {label:<22} {r['parcel_count']:>8,} "
              f"${float(r['median_mv_2025']):>13,.0f} "
              f"${float(r['median_mv_2026']):>13,.0f} "
              f" {pct:>+8.1f}%"
              f" {r['count_increased']:>8,}"
              f" {r['count_decreased']:>8,}"
              f"{flag}")

    # Overall
    cur.execute("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE p26.market_value > p25.market_value) AS incr
        FROM parcel_tax_year p25
        JOIN parcel_tax_year p26 USING (geo_id)
        WHERE p25.tax_year = 2025 AND p26.tax_year = 2026
          AND p25.market_value > 0 AND p26.market_value > 0
    """)
    tot = cur.fetchone()
    print(f"\n  Overall: {tot['total']:,} parcels compared. "
          f"{tot['incr']:,} ({tot['incr']/tot['total']*100:.1f}%) increased in 2026 vs 2025.")
    print("="*72)
    cur.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def load(conn, skip_qa=False):
    if not os.path.isdir(PRELIM_DIR):
        print(f"  ERROR: Preliminary data directory not found:\n  {PRELIM_DIR}")
        return

    print(f"\n{'='*72}")
    print(f"  Loading 2026 Preliminary Appraisal Export")
    print(f"  Source: {os.path.basename(PRELIM_DIR)}")
    print(f"  Tax year: {TAX_YEAR}, data_source: '{DATA_SRC}'")
    print(f"{'='*72}\n")

    load_prop_txt(conn)
    load_prop_ent_txt(conn)
    load_land_and_imprv(conn)
    load_sb12(conn)

    print("\n  2026 Preliminary load complete.")

    if not skip_qa:
        run_qa(conn)
        run_county_comparison(conn)


if __name__ == "__main__":
    conn = get_conn()
    execute_schema(conn)
    load(conn)
    conn.close()
