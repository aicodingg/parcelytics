"""
parcel_rollup.py — single source of truth for computing parcel_tax_year
from prop_unit_tax_year (Migration M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md
§3.3). Mirrors parcel_filters.py's "one canonical module" pattern: before
this fix, every loader independently wrote parcel_tax_year's value columns
directly, computed straight from whatever single PROP_ENT.TXT/AJR row won
a geo_id collision (see loaders' original `flush()` functions and the
three collision-loss mechanisms in SPEC_UNIT_MODEL_AND_INGEST_GATE.md §1).
Now: loaders write prop_unit / prop_unit_tax_year (the real per-prop_id
grain, no geo_id collision possible since prop_id is the primary key
there), and ONLY this module writes parcel_tax_year's value columns, by
SUM()-aggregating every unit that shares a geo_id.

Two responsibilities, both idempotent (safe to re-run any number of times
with no drift — re-running produces byte-identical output given unchanged
inputs):
  1. rollup_tax_year() / rollup_all_years() — (re)compute parcel_tax_year
     rows from prop_unit_tax_year via SUM(), grouped by (geo_id, tax_year).
  2. repair_prop_id() — parcel.prop_id was historically "winner
     contamination": whichever prop_id's PROP.TXT row happened to load
     last for a geo_id under the old `ON CONFLICT (geo_id) DO UPDATE`
     scheme. This UPDATE replaces it with a stable, deterministic
     representative: MIN(prop_id) across all units sharing that geo_id.

unit_count semantics on the written parcel_tax_year row (see schema.sql):
  NULL = row hasn't been rolled up yet (pre-migration legacy state — never
         written by this module, only by not running it)
  1    = simple single-unit parcel; the row's values are that one unit's
         own values (a SUM() of one row still equals that row, so no
         special-casing is needed for the single-unit case, but the
         COUNT(*) = 1 is what a caller checks to know it's not a sum)
  >1   = true multi-unit account; every value column is a SUM() across
         that many prop_unit_tax_year rows for the year

NULL-value semantics of SUM(): Postgres's SUM() ignores NULL inputs and
returns NULL only if EVERY input row was NULL (not zero) — this is the
correct behavior here: a unit with a genuinely-unknown market_value
(NULL) should not silently be treated as $0 and given veto power over an
otherwise-real total; it should simply not contribute to the sum, the
same as it not existing. A geo_id whose only unit(s) all have NULL
market_value correctly rolls up to NULL (still unknown), not 0.

    from parcel_rollup import rollup_all_years, repair_prop_id
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402  (repo-root import, same pattern as parcel_filters.py callers)


# ── Production SQL (executed against Postgres; not exercised by fixture
#    tests in this sandbox — see loaders/test_parcel_rollup.py's module
#    docstring for the AC8 disclosure on why, and compute_rollup() /
#    compute_prop_id_repair() below for the hand-verified pure-Python
#    mirror of this same logic that IS fixture-tested). ──────────────────
ROLLUP_SQL = """
    WITH base AS (
        SELECT u.geo_id, y.tax_year,
               SUM(y.market_value)   AS market_value,
               SUM(y.assessed_value) AS assessed_value,
               SUM(y.taxable_value)  AS taxable_value,
               SUM(y.hs_cap_loss)    AS hs_cap_loss,
               SUM(y.land_value)     AS land_value,
               SUM(y.imprv_value)    AS imprv_value,
               MIN(y.data_source)    AS data_source,
               COUNT(*)              AS unit_count
        FROM prop_unit_tax_year y
        JOIN prop_unit u ON u.prop_id = y.prop_id
        WHERE y.tax_year = %(tax_year)s
        GROUP BY u.geo_id, y.tax_year
    ),
    codes AS (
        SELECT u.geo_id, y.tax_year,
               string_agg(DISTINCT code, ',' ORDER BY code) AS exemption_codes
        FROM prop_unit_tax_year y
        JOIN prop_unit u ON u.prop_id = y.prop_id
        LEFT JOIN LATERAL unnest(string_to_array(y.exemption_codes, ',')) AS code ON TRUE
        WHERE y.tax_year = %(tax_year)s
        GROUP BY u.geo_id, y.tax_year
    )
    INSERT INTO parcel_tax_year
        (geo_id, tax_year, market_value, assessed_value, taxable_value,
         hs_cap_loss, land_value, imprv_value, exemption_codes, data_source, unit_count)
    SELECT base.geo_id, base.tax_year, base.market_value, base.assessed_value,
           base.taxable_value, base.hs_cap_loss, base.land_value, base.imprv_value,
           codes.exemption_codes, base.data_source, base.unit_count
    FROM base JOIN codes ON codes.geo_id = base.geo_id AND codes.tax_year = base.tax_year
    ON CONFLICT (geo_id, tax_year) DO UPDATE
        SET market_value    = EXCLUDED.market_value,
            assessed_value  = EXCLUDED.assessed_value,
            taxable_value   = EXCLUDED.taxable_value,
            hs_cap_loss     = EXCLUDED.hs_cap_loss,
            land_value      = EXCLUDED.land_value,
            imprv_value     = EXCLUDED.imprv_value,
            exemption_codes = EXCLUDED.exemption_codes,
            data_source     = EXCLUDED.data_source,
            unit_count      = EXCLUDED.unit_count
"""

DISTINCT_YEARS_SQL = "SELECT DISTINCT tax_year FROM prop_unit_tax_year ORDER BY tax_year"

PROP_ID_REPAIR_SQL = """
    UPDATE parcel p
    SET prop_id = rep.min_prop_id
    FROM (
        SELECT geo_id, MIN(prop_id) AS min_prop_id
        FROM prop_unit
        GROUP BY geo_id
    ) rep
    WHERE p.geo_id = rep.geo_id
      AND (p.prop_id IS DISTINCT FROM rep.min_prop_id)
"""


# ── DB-facing wrappers (production code path — requires a live conn) ────
def rollup_tax_year(conn, tax_year):
    """(Re)compute every parcel_tax_year row for one tax_year. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(ROLLUP_SQL, {"tax_year": tax_year})
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def distinct_tax_years(conn):
    with conn.cursor() as cur:
        cur.execute(DISTINCT_YEARS_SQL)
        return [r[0] for r in cur.fetchall()]


def rollup_all_years(conn):
    """(Re)compute parcel_tax_year for every tax_year present in prop_unit_tax_year."""
    total = 0
    for year in distinct_tax_years(conn):
        total += rollup_tax_year(conn, year)
    return total


def repair_prop_id(conn):
    """
    Replace parcel.prop_id's winner-contaminated value with the stable
    MIN(prop_id) representative across all prop_unit rows sharing that
    geo_id. Idempotent — a second run updates 0 rows once already correct.
    """
    with conn.cursor() as cur:
        cur.execute(PROP_ID_REPAIR_SQL)
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def run(conn, tax_year=None):
    """
    Full rollup entry point used by loaders/run_all.py after all source
    loaders finish. Repairs parcel.prop_id's identity FIRST, then
    (re)computes parcel_tax_year's values, so there's no window where one
    is refreshed and the other still reflects stale/contaminated state.
    """
    repaired = repair_prop_id(conn)
    if tax_year is not None:
        rolled = rollup_tax_year(conn, tax_year)
    else:
        rolled = rollup_all_years(conn)
    return {"prop_id_repaired": repaired, "parcel_tax_year_rows": rolled}


# ── Pure-Python mirror of the SQL above — no DB required. Used by
#    loaders/test_parcel_rollup.py to fixture-test NULL-semantics and
#    idempotency in this sandbox (see that file's docstring for why the
#    SQL itself can't be executed-verified here). Kept in hand-verified
#    lockstep with ROLLUP_SQL / PROP_ID_REPAIR_SQL above — any change to
#    one must be mirrored in the other. ────────────────────────────────
def compute_rollup(unit_rows, tax_year):
    """
    unit_rows: iterable of dicts, each a prop_unit_tax_year row joined to
    its prop_unit's geo_id — keys: prop_id, geo_id, tax_year, market_value,
    assessed_value, taxable_value, hs_cap_loss, land_value, imprv_value,
    exemption_codes, data_source.

    Returns a list of dicts (one per geo_id present for `tax_year`),
    matching exactly what ROLLUP_SQL would INSERT: geo_id, tax_year,
    market_value, assessed_value, taxable_value, hs_cap_loss, land_value,
    imprv_value, exemption_codes, data_source, unit_count.
    """
    groups = {}
    for row in unit_rows:
        if row["tax_year"] != tax_year:
            continue
        groups.setdefault(row["geo_id"], []).append(row)

    out = []
    for geo_id, rows in groups.items():
        out.append({
            "geo_id": geo_id,
            "tax_year": tax_year,
            "market_value": _sql_sum(rows, "market_value"),
            "assessed_value": _sql_sum(rows, "assessed_value"),
            "taxable_value": _sql_sum(rows, "taxable_value"),
            "hs_cap_loss": _sql_sum(rows, "hs_cap_loss"),
            "land_value": _sql_sum(rows, "land_value"),
            "imprv_value": _sql_sum(rows, "imprv_value"),
            "exemption_codes": _sql_code_union(rows),
            "data_source": _sql_min(rows, "data_source"),
            "unit_count": len(rows),
        })
    return out


def compute_prop_id_repair(prop_units):
    """
    prop_units: iterable of dicts with keys prop_id, geo_id (mirrors the
    prop_unit table). Returns {geo_id: min_prop_id} — the exact mapping
    PROP_ID_REPAIR_SQL's subquery computes.
    """
    reps = {}
    for u in prop_units:
        geo_id, prop_id = u["geo_id"], u["prop_id"]
        if geo_id not in reps or (prop_id is not None and prop_id < reps[geo_id]):
            if reps.get(geo_id) is None:
                reps[geo_id] = prop_id
            else:
                reps[geo_id] = min(reps[geo_id], prop_id)
    return reps


def _sql_sum(rows, key):
    """Mirror of Postgres SUM(): ignores NULLs; NULL only if every value was NULL."""
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return sum(vals)


def _sql_min(rows, key):
    """Mirror of Postgres MIN(): ignores NULLs; NULL only if every value was NULL."""
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return min(vals)


def _sql_code_union(rows):
    """Mirror of the codes CTE: union of every comma-split code, NULLs excluded, sorted."""
    codes = set()
    for r in rows:
        raw = r.get("exemption_codes")
        if not raw:
            continue
        codes.update(c for c in raw.split(",") if c)
    return ",".join(sorted(codes)) if codes else None


if __name__ == "__main__":
    import argparse
    from loaders.db import get_conn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=None, help="Roll up a single tax_year only")
    ap.add_argument("--all-years", action="store_true", help="Roll up every tax_year present in prop_unit_tax_year")
    args = ap.parse_args()

    if not args.all_years and args.year is None:
        ap.error("pass --year YYYY or --all-years")

    conn = get_conn()
    result = run(conn, tax_year=args.year)
    print(f"prop_id repaired: {result['prop_id_repaired']:,} rows")
    print(f"parcel_tax_year rolled up: {result['parcel_tax_year_rows']:,} rows")
    conn.close()
