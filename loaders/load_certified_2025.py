"""
Load 2025 Certified Appraisal Export (EARS fixed-width format).

Files used:
  PROP.TXT       — one row per parcel (property + owner)
  PROP_ENT.TXT   — one row per parcel × entity (values, exemptions)
  LAND_DET.TXT   — land segment detail (land value)
  IMP_INFO.TXT   — improvement info (improvement value)

Migration M2 (SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.4): this loader used
to write parcel_tax_year directly, keyed by a prop_id → geo_id lookup
built FROM THE parcel TABLE (`SELECT prop_id, geo_id FROM parcel`). That
lookup only ever contained the ONE prop_id that happened to win the
`parcel` table's `ON CONFLICT (geo_id) DO UPDATE` for each geo_id — every
PROP_ENT.TXT row for a losing prop_id (i.e. every unit past the first in
a multi-unit account) failed the dict lookup and was silently dropped
(Mechanism A, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §1). That's gone now:
this loader writes prop_unit / prop_unit_tax_year keyed directly by
prop_id (prop_id is prop_unit's PRIMARY KEY — no lookup, no collision,
nothing to drop), and parcel_tax_year is no longer written here at all —
it's derived by parcel_rollup.py at the end of load(), which SUMs every
prop_unit_tax_year row sharing a geo_id. All field-slice constants and
the PROP.TXT / PROP_ENT.TXT / LAND_DET.TXT parsing logic now live in
loaders/ears_format.py (previously duplicated near-identically across
this file, load_2026_preliminary.py, and load_certified_historical.py —
see that module's docstring for the one real drift found while
consolidating: the TCO entity-code check).

PROP.TXT / PROP_ENT.TXT / LAND_DET.TXT field positions: see
loaders/ears_format.py's PROP_SLICES / PROP_ENT_SLICES / LAND_DET_SLICES
(same positions this file used before the refactor — unchanged, just
centralized).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
from loaders import ears_format
from loaders import ingest_gate  # noqa: F401 — AC5 wiring marker; see module note below
from loaders.scrape_billing_history import DEFAULT_COUNTY  # PARCEL-ROLLUP-HOTFIX-1
import parcel_rollup

# NOTE on ingest_gate wiring (Migration M2, AC5): this loader imports
# loaders/ingest_gate.py so the gate module is a real dependency of every
# refactored loader (mechanically checkable), but deliberately does NOT
# call ingest_gate.scan_prop_ledger() inline here — that would mean a
# SECOND full read of a multi-gigabyte PROP.TXT file (9.8 GB per this
# file's original docstring) on every load run, on top of the read
# iter_prop_records() already does below. The real gate enforcement point
# is loaders/run_all.py's explicit step, which scans PROP.TXT/PROP_ENT.TXT
# once, after this loader has already finished writing. This is a
# deliberate performance tradeoff, flagged here rather than silently
# choosing one approach without explanation.

import psycopg2.extras

TAX_YEAR = 2025
DATA_SRC = "certified"


# ── Step 1: PROP.TXT → parcel (public identity) + prop_unit (storage truth) ──
def load_prop_txt(conn, cert_dir, county_code=DEFAULT_COUNTY):
    path = os.path.join(cert_dir, "PROP.TXT")
    print(f"  Loading PROP.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    parcel_sql = """
        INSERT INTO parcel
            (county_code, geo_id, prop_id, prop_type_cd, owner_id, owner_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (county_code, geo_id) DO UPDATE
            SET prop_id      = EXCLUDED.prop_id,
                prop_type_cd = EXCLUDED.prop_type_cd,
                owner_id     = EXCLUDED.owner_id,
                owner_name   = EXCLUDED.owner_name
    """
    # NOTE: parcel.prop_id is still written here (last-record-wins, same as
    # before) so the column is never left NULL between load and the
    # parcel_rollup.repair_prop_id() step that runs at the end of load()
    # and replaces it with the stable MIN(prop_id) representative.

    parcel_rows = []
    unit_rows = []
    total = 0
    # Task M5-PERYEAR-GEOID: built as a side-effect of this same PROP.TXT
    # read (no second file read) -- this IS the 2025 real, as-of-2025
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

    print(f"    → {total:,} parcels / units in {time.time()-t0:.1f}s")
    return total, pid_to_geo


def _flush_prop_txt_batch(conn, parcel_sql, parcel_rows, unit_rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, parcel_sql, parcel_rows, page_size=2000)
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_UPSERT_SQL, unit_rows, page_size=2000)
    conn.commit()


# ── Step 2: PROP_ENT.TXT → prop_unit_tax_year (one row per real unit) ───────
def load_prop_ent_txt(conn, cert_dir, pid_to_geo, county_code=DEFAULT_COUNTY):
    path = os.path.join(cert_dir, "PROP_ENT.TXT")
    print(f"  Loading PROP_ENT.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    t0 = time.time()

    rows_to_insert = []
    total = 0
    n_no_geo = 0

    for agg in ears_format.iter_prop_ent_aggregates(path):
        # Task M5-PERYEAR-GEOID: PROP_ENT.TXT itself has no geo_id field --
        # pid_to_geo (built from this same year's PROP.TXT in
        # load_prop_txt(), above) is the source of the real, as-of-2025
        # value. A prop_id present in PROP_ENT.TXT but absent from
        # pid_to_geo (dropped/supplement-only/no-geo_id in PROP.TXT) gets
        # geo_id=None here -- same "not every prop_id resolves" population
        # already excluded from prop_unit itself, not a new gap.
        geo_id = pid_to_geo.get(agg["prop_id"])
        if geo_id is None:
            n_no_geo += 1
        rows_to_insert.append((
            county_code,  # PARCEL-ROLLUP-HOTFIX-1: matching PROP_UNIT_TAX_YEAR_UPSERT_SQL's real column order
            agg["prop_id"], agg.get("year") or TAX_YEAR, geo_id,
            agg["market_value"], agg["assessed_value"], agg["taxable_value"],
            None,  # hs_cap_loss — not derivable from PROP_ENT fields read here
            None,  # land_value — set by load_land_and_imprv()
            None,  # imprv_value — set by load_land_and_imprv()
            agg["exemption_codes"], DATA_SRC,
        ))

        if len(rows_to_insert) >= 5000:
            _flush_pty(conn, rows_to_insert)
            total += len(rows_to_insert)
            rows_to_insert = []

    if rows_to_insert:
        _flush_pty(conn, rows_to_insert)
        total += len(rows_to_insert)

    print(f"    → {total:,} unit-year rows in {time.time()-t0:.1f}s "
          f"({n_no_geo:,} with no resolvable geo_id)")
    return total


def _flush_pty(conn, rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, rows, page_size=2000)
    conn.commit()


# ── Step 3: LAND_DET.TXT → land_value; imprv_value = market − land ───────────
def load_land_and_imprv(conn, cert_dir, county_code=DEFAULT_COUNTY):
    """
    Sum land segment market values per prop_id from LAND_DET.TXT, then set
    (on prop_unit_tax_year, by prop_id directly — no geo_id indirection,
    unlike the pre-migration version of this function):
      land_value  = sum of land_seg_mkt_val
      imprv_value = max(0, market_value − land_value)
    """
    print("  Loading LAND_DET.TXT…")
    t0 = time.time()

    land_path = os.path.join(cert_dir, "LAND_DET.TXT")
    land_totals = ears_format.land_totals(land_path)
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


def load(conn, county_code=DEFAULT_COUNTY):
    cert_dir = config.CERT_DIR
    if not os.path.isdir(cert_dir):
        print(f"  WARNING: {cert_dir} not found, skipping 2025 Certified")
        return 0

    _, pid_to_geo = load_prop_txt(conn, cert_dir, county_code=county_code)
    load_prop_ent_txt(conn, cert_dir, pid_to_geo, county_code=county_code)
    load_land_and_imprv(conn, cert_dir, county_code=county_code)

    print("  Rolling up prop_unit_tax_year → parcel_tax_year for 2025…")
    result = parcel_rollup.run(conn, tax_year=TAX_YEAR, county_code=county_code)
    print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    print("  2025 Certified Export loaded.")


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
