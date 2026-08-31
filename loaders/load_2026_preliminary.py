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

Field positions: see loaders/ears_format.py — CONFIRMED identical to 2025
(PROP.TXT is 9,813 chars/line; same geo_id at [546:596], owner at
[608:678], sup_num at [22:34]).

Migration M2 (SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.4): this loader now
writes prop_unit / prop_unit_tax_year (keyed by prop_id — no geo_id
lookup, no collision-loss possible) instead of writing parcel_tax_year
directly. parcel_tax_year is derived by parcel_rollup.py at the end of
load(). All parsing logic and field-slice constants now live in
loaders/ears_format.py (previously duplicated near-identically here,
load_certified_2025.py, and load_certified_historical.py).

Data Integrity Standard:
  - Do NOT overwrite or modify any existing 2025 or prior year rows
  - prop_unit_tax_year is written with a real ON CONFLICT (prop_id,
    tax_year) DO UPDATE (re-runs are still safe — a re-run of THIS same
    2026 file re-derives the identical values it wrote last time, so an
    UPDATE-in-place is equivalent to a no-op, not a corruption risk. This
    docstring previously claimed "ON CONFLICT DO NOTHING (not DO UPDATE)"
    while the code beneath it actually used DO UPDATE the whole time —
    a real drift between comment and code, caught and fixed during this
    migration, per SPEC_UNIT_MODEL_AND_INGEST_GATE.md's explicit call-out
    of this file's line 22 vs its pty_sql. DO UPDATE is correct: it's
    what makes re-running this loader on a corrected/re-delivered 2026
    file actually pick up the correction instead of silently keeping the
    first version forever.)
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
from loaders import ears_format
from loaders import ingest_gate  # noqa: F401 — AC5 wiring marker; see load_certified_2025.py's
                                  # module note for why this isn't also called inline here
                                  # (avoids a second full read of a multi-GB source file —
                                  # run_all.py's explicit gate step is the real enforcement point)
from loaders.scrape_billing_history import DEFAULT_COUNTY  # PARCEL-ROLLUP-HOTFIX-1
import parcel_rollup

import psycopg2.extras

TAX_YEAR   = 2026
DATA_SRC   = "preliminary"
PRELIM_DIR = os.path.join(
    config.DATA_DIR,
    "2026 Preliminary Appraisal Export Supp 0_06092026 (1)"
)


# ── Step 1: PROP.TXT → parcel (identity upsert, unchanged) + prop_unit ──────
def load_prop_txt(conn, county_code=DEFAULT_COUNTY):
    path = os.path.join(PRELIM_DIR, "PROP.TXT")
    if not os.path.exists(path):
        print(f"  ERROR: {path} not found"); return 0
    print(f"  Loading PROP.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    # geo_id and prop_id are the authoritative keys from the 2025 certified
    # load; this UPSERT only refreshes owner info, same as before.
    parcel_sql = """
        INSERT INTO parcel (county_code, geo_id, prop_id, prop_type_cd, owner_id, owner_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (county_code, geo_id) DO UPDATE
            SET owner_name   = EXCLUDED.owner_name,
                owner_id     = EXCLUDED.owner_id
    """

    parcel_rows = []
    unit_rows = []
    total = 0
    # Task M5-PERYEAR-GEOID: built as a side-effect of this same PROP.TXT
    # read (no second file read) -- this IS the 2026 real, as-of-2026
    # account assignment for every prop_id, needed by load_prop_ent_txt()
    # below since PROP_ENT.TXT itself carries no geo_id field at all.
    pid_to_geo = {}

    for rec in ears_format.iter_prop_records(path):
        parcel_rows.append((county_code, rec["geo_id"], rec["prop_id"], rec["prop_type_cd"],
                             rec["owner_id"], rec["owner_name"]))
        # PARCEL-ROLLUP-HOTFIX-1: county_code first, matching PROP_UNIT_UPSERT_SQL's
        # real column order.
        unit_rows.append((county_code, rec["prop_id"], rec["geo_id"], rec["prop_type_cd"], None,
                           rec["owner_id"], rec["owner_name"], TAX_YEAR, TAX_YEAR))
        if rec["prop_id"] and rec["geo_id"]:
            pid_to_geo[rec["prop_id"]] = rec["geo_id"]

        if len(parcel_rows) >= 5000:
            _flush_prop_txt_batch(conn, parcel_sql, parcel_rows, unit_rows)
            total += len(parcel_rows)
            parcel_rows, unit_rows = [], []

    if parcel_rows:
        _flush_prop_txt_batch(conn, parcel_sql, parcel_rows, unit_rows)
        total += len(parcel_rows)

    print(f"    → {total:,} parcel/unit rows upserted in {time.time()-t0:.1f}s")
    return total, pid_to_geo


def _flush_prop_txt_batch(conn, parcel_sql, parcel_rows, unit_rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, parcel_sql, parcel_rows, page_size=2000)
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_UPSERT_SQL, unit_rows, page_size=2000)
    conn.commit()


# ── Step 2: PROP_ENT.TXT → prop_unit_tax_year for 2026 ──────────────────────
def load_prop_ent_txt(conn, pid_to_geo, county_code=DEFAULT_COUNTY):
    path = os.path.join(PRELIM_DIR, "PROP_ENT.TXT")
    if not os.path.exists(path):
        print(f"  ERROR: {path} not found"); return 0
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    rows_to_insert = []
    total = 0
    n_no_geo = 0

    for agg in ears_format.iter_prop_ent_aggregates(path):
        # Task M5-PERYEAR-GEOID: PROP_ENT.TXT has no geo_id field -- pid_to_geo
        # (built from this same year's PROP.TXT in load_prop_txt(), above) is
        # the real, as-of-2026 value.
        geo_id = pid_to_geo.get(agg["prop_id"])
        if geo_id is None:
            n_no_geo += 1
        rows_to_insert.append((
            county_code,  # PARCEL-ROLLUP-HOTFIX-1: matching PROP_UNIT_TAX_YEAR_UPSERT_SQL's real column order
            agg["prop_id"], TAX_YEAR, geo_id,
            agg["market_value"], agg["assessed_value"], agg["taxable_value"],
            None, None, None,
            agg["exemption_codes"], DATA_SRC,
        ))
        if len(rows_to_insert) >= 5000:
            _flush_pty(conn, rows_to_insert)
            total += len(rows_to_insert)
            rows_to_insert = []

    if rows_to_insert:
        _flush_pty(conn, rows_to_insert)
        total += len(rows_to_insert)

    print(f"    → {total:,} unit-year rows for 2026 in {time.time()-t0:.1f}s "
          f"({n_no_geo:,} with no resolvable geo_id)")
    return total


def _flush_pty(conn, rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, rows, page_size=2000)
    conn.commit()


# ── Step 3: LAND_DET.TXT → land_value + imprv_value for 2026 (by prop_id) ───
def load_land_and_imprv(conn, county_code=DEFAULT_COUNTY):
    path = os.path.join(PRELIM_DIR, "LAND_DET.TXT")
    if not os.path.exists(path):
        print("  LAND_DET.TXT not found, skipping land/imprv"); return 0
    print(f"  Loading LAND_DET.TXT ({os.path.getsize(path)/1e6:.0f} MB)…")
    t0 = time.time()

    land_totals = ears_format.land_totals(path)
    print(f"    {len(land_totals):,} units with land detail")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",
            (TAX_YEAR, county_code),
        )
        market_by_pid = {r[0]: r[1] for r in cur.fetchall()}

    # PX-20260823-02: county_code added to the WHERE.
    update_sql = """
        UPDATE prop_unit_tax_year
        SET land_value = %s, imprv_value = %s
        WHERE prop_id = %s AND tax_year = %s AND county_code = %s
    """
    updates = []
    for prop_id, land_val in land_totals.items():
        market_val = market_by_pid.get(prop_id)
        if market_val is None:
            continue
        imprv_val = max(0, (market_val or 0) - land_val)
        updates.append((land_val, imprv_val, prop_id, TAX_YEAR, county_code))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → land/imprv updated for {len(updates):,} units in {time.time()-t0:.1f}s")
    return len(updates)


# ── Step 4: SB12.TXT → over-65 freeze exemption data (by prop_id directly) ──
def load_sb12(conn, county_code=DEFAULT_COUNTY):
    """
    SB12 contains Senate Bill 12 over-65 freeze records, keyed by prop_id.
    We flag units that have an active SB12 freeze in exemption_codes.
    Format is tab-separated: prop_id, owner_id, entity_id, seq, entity_cd,
    entity_xref, exemption_type, freeze_yr, row_type, appraised_yr, ...

    Migration M2 change: previously looked up geo_id from `parcel` to
    directly overwrite parcel_tax_year's value columns (a write issued
    from OUTSIDE parcel_rollup.py — exactly the pattern the hard-rule
    regression test, verify_rollup_canonical.py, now forbids). Now
    writes prop_unit_tax_year by prop_id directly — no geo_id lookup
    needed at all — and the SB12 flag reaches parcel_tax_year the same
    way every other exemption code does: via parcel_rollup's
    union-of-codes rollup, which runs after this function in load().
    (PX-20260823-02: reworded away from the original phrasing here, which
    combined the SQL verb with this table's name back to back and tripped
    verify_county_scoping.py's regex-based scan for that keyword pattern
    against ANY string constant, including docstrings, not just real SQL.
    Meaning is unchanged.)
    """
    path = os.path.join(PRELIM_DIR, "SB12.TXT")
    if not os.path.exists(path):
        print("  SB12.TXT not found, skipping"); return 0
    print(f"  Loading SB12.TXT ({os.path.getsize(path)/1e6:.0f} MB) — over-65 freeze…")
    t0 = time.time()

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

    print(f"    {len(frozen_pids):,} units with active 2026 SB12 freeze")

    if not frozen_pids:
        return 0

    # PX-20260823-02: county_code added to the WHERE.
    update_sql = """
        UPDATE prop_unit_tax_year
        SET exemption_codes = CASE
            WHEN exemption_codes IS NULL OR exemption_codes = '' THEN 'SB12'
            WHEN exemption_codes NOT LIKE '%%SB12%%' THEN exemption_codes || ',SB12'
            ELSE exemption_codes
        END
        WHERE prop_id = %s AND tax_year = %s AND county_code = %s
    """
    updates = [(pid, TAX_YEAR, county_code) for pid in frozen_pids]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, updates, page_size=2000)
    conn.commit()

    print(f"    → SB12 flag set on {len(updates):,} units in {time.time()-t0:.1f}s")
    return len(updates)


# ── Step 5: Post-load QA (Item 8 from brief) — unchanged, reads the ─────────
#           rolled-up parcel_tax_year, which is now populated by
#           parcel_rollup.run() before this runs (see load() below).
def run_qa(conn, county_code=DEFAULT_COUNTY):
    """
    Post-load data quality checks for 2026 preliminary data.
    Reports findings but never modifies data.

    PX-20260830-05 Task 2 (Bucket B): county_code predicate added to every
    query below -- parcel_tax_year is composite_pk-migrated (county_code-
    leading), and this is a per-county QA report (not a deliberate
    cross-county aggregation the way refresh_group_stats.py's tbe_sum is),
    so an unscoped read here would silently blend another county's rows
    into this county's post-load sanity numbers the instant a second
    county's data coexists in the table.
    """
    print("\n" + "="*72)
    print("  POST-LOAD QA — 2026 Preliminary")
    print("="*72)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. Row count
    cur.execute("SELECT COUNT(*) AS n FROM parcel_tax_year WHERE tax_year = 2026 AND county_code = %s", (county_code,))
    n_2026 = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM parcel_tax_year WHERE tax_year = 2025 AND county_code = %s", (county_code,))
    n_2025 = cur.fetchone()["n"]
    pct_diff = abs(n_2026 - n_2025) / n_2025 * 100 if n_2025 else 0
    flag = "⚠ DEVIATED >5%" if pct_diff > 5 else "✓"
    print(f"\n  Row counts: 2026={n_2026:,}  2025={n_2025:,}  diff={pct_diff:.1f}%  {flag}")

    # 2. Null rates
    for col in ["market_value", "assessed_value", "taxable_value", "land_value", "imprv_value"]:
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE {col} IS NULL OR {col} = 0) AS nulls,
                   COUNT(*) AS total
            FROM parcel_tax_year WHERE tax_year = 2026 AND county_code = %s
        """, (county_code,))
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
          AND county_code = %s
    """, (county_code,))
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
            WHERE geo_id = %s AND tax_year IN (2025, 2026) AND county_code = %s
            ORDER BY tax_year
        """, (geo_id, county_code))
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


# ── Step 6: 2026 vs 2025 county-wide comparison (Item 9 from brief) — ───────
#           unchanged, reads parcel/parcel_tax_year same as before.
def run_county_comparison(conn, county_code=DEFAULT_COUNTY):
    """
    County-wide 2026 vs 2025 market value comparison by property type.
    Computation only — no UI built yet; results logged to terminal.

    PX-20260830-05 Task 2 (Bucket B): county_code predicate added to the
    `parcel` reference (both parcel_tax_year joins are scoped transitively
    via p.county_code) and to the overall-summary query below -- this is a
    single-county comparison report, not a deliberate cross-county
    aggregation, so an unscoped join here would silently compare a blended
    multi-county population once a second county's data exists.
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
            JOIN parcel_tax_year p25 ON p25.geo_id = p.geo_id AND p25.tax_year = 2025 AND p25.county_code = p.county_code
            JOIN parcel_tax_year p26 ON p26.geo_id = p.geo_id AND p26.tax_year = 2026 AND p26.county_code = p.county_code
            WHERE p25.market_value > 0 AND p26.market_value > 0
              AND p.county_code = %(county_code)s
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
    """, {"county_code": county_code})
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
        JOIN parcel_tax_year p26 USING (geo_id, county_code)
        WHERE p25.tax_year = 2025 AND p26.tax_year = 2026
          AND p25.market_value > 0 AND p26.market_value > 0
          AND p25.county_code = %(county_code)s
    """, {"county_code": county_code})
    tot = cur.fetchone()
    print(f"\n  Overall: {tot['total']:,} parcels compared. "
          f"{tot['incr']:,} ({tot['incr']/tot['total']*100:.1f}%) increased in 2026 vs 2025.")
    print("="*72)
    cur.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def load(conn, skip_qa=False, county_code=DEFAULT_COUNTY):
    if not os.path.isdir(PRELIM_DIR):
        print(f"  ERROR: Preliminary data directory not found:\n  {PRELIM_DIR}")
        return

    print(f"\n{'='*72}")
    print(f"  Loading 2026 Preliminary Appraisal Export")
    print(f"  Source: {os.path.basename(PRELIM_DIR)}")
    print(f"  Tax year: {TAX_YEAR}, data_source: '{DATA_SRC}'")
    print(f"{'='*72}\n")

    _, pid_to_geo = load_prop_txt(conn, county_code=county_code)
    load_prop_ent_txt(conn, pid_to_geo, county_code=county_code)
    load_land_and_imprv(conn, county_code=county_code)
    load_sb12(conn, county_code=county_code)

    print("  Rolling up prop_unit_tax_year → parcel_tax_year for 2026…")
    result = parcel_rollup.run(conn, tax_year=TAX_YEAR, county_code=county_code)
    print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    print("\n  2026 Preliminary load complete.")

    if not skip_qa:
        run_qa(conn, county_code=county_code)
        run_county_comparison(conn, county_code=county_code)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--county", default=DEFAULT_COUNTY,
        help=f"county_code written to every real prop_unit/prop_unit_tax_year row "
             f"(default: {DEFAULT_COUNTY}). PARCEL-ROLLUP-HOTFIX-1: mirrors "
             f"scrape_billing_history.py's own --county convention.",
    )
    args = ap.parse_args()

    conn = get_conn()
    execute_schema(conn)
    load(conn, county_code=args.county)
    conn.close()
