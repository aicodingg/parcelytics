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

# PARCEL-ROLLUP-HOTFIX-1: DEFAULT_COUNTY imported from the same real source
# every other real writer in this codebase uses (mirrors load_tax_current.py's
# own DALLAS-GATE-4 import), not redeclared locally.
from loaders.scrape_billing_history import DEFAULT_COUNTY  # noqa: E402


# ── Production SQL (executed against Postgres; not exercised by fixture
#    tests in this sandbox — see loaders/test_parcel_rollup.py's module
#    docstring for the AC8 disclosure on why, and compute_rollup() /
#    compute_prop_id_repair() below for the hand-verified pure-Python
#    mirror of this same logic that IS fixture-tested). ──────────────────
#
# Task M5-PERYEAR-GEOID: this used to GROUP BY u.geo_id (prop_unit's
# single "latest-known" account assignment, via an inner JOIN), which
# meant every year's rollup — including old years like 2022 — used
# whatever geo_id was true as of the MOST RECENT year ever loaded for
# that prop_id, not the geo_id that was actually true for the year being
# rolled up. When TCAD reissues/replats an account between an old year
# and now, the old year's rollup silently grouped under the wrong,
# LATER account number. Confirmed production scale: ~1,380-1,442
# properties/year for 2022-2024 (~4,250 total).
#
# Fix: GROUP BY COALESCE(y.geo_id, u.geo_id) — y.geo_id is the new,
# real, as-of-that-year column on prop_unit_tax_year itself (populated
# by every loader now, per this same task's Step 2). u.geo_id is kept
# ONLY as a fallback for rows where y.geo_id is still NULL.
#
# NULL-fallback design decision (brief's explicit open question):
# Fall back to prop_unit.geo_id (current behavior) rather than exclude
# NULL-geo_id rows from the rollup entirely. Reasoning: this column is
# being added additively (Step 1) and is only populated going forward
# by loaders (Step 2) plus a one-time historical backfill (Step 5) that
# runs AFTER this code ships, not atomically with it — there WILL be a
# window, per environment, where existing prop_unit_tax_year rows have
# geo_id = NULL until the backfill script runs. Excluding those rows
# from the rollup during that window would make real parcels vanish
# from parcel_tax_year (and immediately fail G1's source-conservation
# check) the moment this code deploys, before the backfill has had a
# chance to run — a strictly worse, more sudden regression than the
# "old years use a possibly-later geo_id" bug being fixed here, which
# has been silently present all along. The fallback path reintroduces
# that exact same latest-known-geo_id behavior for a row until the
# backfill sets its real value, then never applies again for that row
# (the NULL only exists until it's backfilled) — a transient migration
# safety net, not a permanent second code path.
#
# The join is LEFT JOIN (not INNER, as it was before) so a
# prop_unit_tax_year row whose geo_id is already populated directly
# (the normal case, post-backfill) is never dropped merely because its
# prop_id happens to have no matching prop_unit row — COALESCE only
# needs u.geo_id when y.geo_id is NULL, so a missing prop_unit row is
# harmless once y.geo_id is real. Rows where COALESCE is still NULL
# (no per-year value AND no prop_unit fallback) are excluded via the
# WHERE clause — grouping them together under a NULL geo_id key would
# silently merge unrelated properties into one bogus "NULL" parcel_tax_year
# row, which is worse than omitting them.
#
# PARCEL-ROLLUP-HOTFIX-1 (real, urgent -- found live by verify_county_
# scoping.py's own MC-2 audit): county_code added throughout. This module
# had ZERO county_code awareness despite prop_unit / prop_unit_tax_year /
# parcel / parcel_tax_year all being real, already-migrated, county_code-
# leading-PK tables in production (migrate_county_partitioning.py's own
# TABLE_SPECS) -- every real INSERT here was still the pre-migration
# shape. Confirmed NOT (yet) silently corrupting data: county_code is
# NOT NULL with no default on all 4 tables, and zero live rows have a
# NULL county_code, meaning this module hasn't actually run to completion
# since the migration finished (a loaded gun, not yet fired) -- but the
# very next real run_all.py run would hard-fail on the NOT NULL
# violation. Both CTEs are scoped to ONE county_code per real invocation
# (y.county_code = %(county_code)s), matching every other real writer's
# one-county-per-call convention -- since prop_unit_tax_year's own
# geo_id is only unique WITHIN a county post-migration, scoping the CTEs
# this way keeps COALESCE(y.geo_id, u.geo_id) collision-free exactly as
# it was before any second county's data existed. The u/y join also now
# carries county_code (u.county_code = y.county_code) so a future second
# county's prop_unit rows can never be joined against this county's
# prop_unit_tax_year rows by a coincidentally-matching prop_id.
ROLLUP_SQL = """
    WITH base AS (
        SELECT COALESCE(y.geo_id, u.geo_id) AS geo_id, y.tax_year,
               SUM(y.market_value)   AS market_value,
               SUM(y.assessed_value) AS assessed_value,
               SUM(y.taxable_value)  AS taxable_value,
               SUM(y.hs_cap_loss)    AS hs_cap_loss,
               SUM(y.land_value)     AS land_value,
               SUM(y.imprv_value)    AS imprv_value,
               MIN(y.data_source)    AS data_source,
               COUNT(*)              AS unit_count
        FROM prop_unit_tax_year y
        LEFT JOIN prop_unit u ON u.prop_id = y.prop_id AND u.county_code = y.county_code
        WHERE y.tax_year = %(tax_year)s
          AND y.county_code = %(county_code)s
          AND COALESCE(y.geo_id, u.geo_id) IS NOT NULL
        GROUP BY COALESCE(y.geo_id, u.geo_id), y.tax_year
    ),
    codes AS (
        SELECT COALESCE(y.geo_id, u.geo_id) AS geo_id, y.tax_year,
               string_agg(DISTINCT code, ',' ORDER BY code) AS exemption_codes
        FROM prop_unit_tax_year y
        LEFT JOIN prop_unit u ON u.prop_id = y.prop_id AND u.county_code = y.county_code
        LEFT JOIN LATERAL unnest(string_to_array(y.exemption_codes, ',')) AS code ON TRUE
        WHERE y.tax_year = %(tax_year)s
          AND y.county_code = %(county_code)s
          AND COALESCE(y.geo_id, u.geo_id) IS NOT NULL
        GROUP BY COALESCE(y.geo_id, u.geo_id), y.tax_year
    )
    INSERT INTO parcel_tax_year
        (county_code, geo_id, tax_year, market_value, assessed_value, taxable_value,
         hs_cap_loss, land_value, imprv_value, exemption_codes, data_source, unit_count)
    SELECT %(county_code)s, base.geo_id, base.tax_year, base.market_value, base.assessed_value,
           base.taxable_value, base.hs_cap_loss, base.land_value, base.imprv_value,
           codes.exemption_codes, base.data_source, base.unit_count
    FROM base JOIN codes ON codes.geo_id = base.geo_id AND codes.tax_year = base.tax_year
    ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE
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

# PARCEL-ROLLUP-HOTFIX-1: scoped by county_code -- without this, rollup_
# all_years() would iterate over tax_years present for ANY county, not
# just the one being rolled up, and attempt (harmless but wasteful, and
# semantically wrong) rollups for years that don't exist for this county.
DISTINCT_YEARS_SQL = (
    "SELECT DISTINCT tax_year FROM prop_unit_tax_year "
    "WHERE county_code = %(county_code)s ORDER BY tax_year"
)

# PARCEL-ROLLUP-HOTFIX-1: county_code added to both the subquery's own
# GROUP BY (so MIN(prop_id) is computed within one county, never across
# counties once a second county's prop_unit rows exist) and the outer
# UPDATE's WHERE (so this never touches another county's parcel rows).
PROP_ID_REPAIR_SQL = """
    UPDATE parcel p
    SET prop_id = rep.min_prop_id
    FROM (
        SELECT geo_id, MIN(prop_id) AS min_prop_id
        FROM prop_unit
        WHERE county_code = %(county_code)s
        GROUP BY geo_id
    ) rep
    WHERE p.geo_id = rep.geo_id
      AND p.county_code = %(county_code)s
      AND (p.prop_id IS DISTINCT FROM rep.min_prop_id)
"""


# ── DB-facing wrappers (production code path — requires a live conn) ────
def rollup_tax_year(conn, tax_year, county_code=DEFAULT_COUNTY):
    """(Re)compute every parcel_tax_year row for one tax_year, scoped to one
    county_code per call (PARCEL-ROLLUP-HOTFIX-1). Idempotent."""
    with conn.cursor() as cur:
        cur.execute(ROLLUP_SQL, {"tax_year": tax_year, "county_code": county_code})
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def distinct_tax_years(conn, county_code=DEFAULT_COUNTY):
    with conn.cursor() as cur:
        cur.execute(DISTINCT_YEARS_SQL, {"county_code": county_code})
        return [r[0] for r in cur.fetchall()]


def rollup_all_years(conn, county_code=DEFAULT_COUNTY):
    """(Re)compute parcel_tax_year for every tax_year present in
    prop_unit_tax_year for one county_code."""
    total = 0
    for year in distinct_tax_years(conn, county_code=county_code):
        total += rollup_tax_year(conn, year, county_code=county_code)
    return total


def repair_prop_id(conn, county_code=DEFAULT_COUNTY):
    """
    Replace parcel.prop_id's winner-contaminated value with the stable
    MIN(prop_id) representative across all prop_unit rows sharing that
    geo_id, scoped to one county_code. Idempotent — a second run updates
    0 rows once already correct.
    """
    with conn.cursor() as cur:
        cur.execute(PROP_ID_REPAIR_SQL, {"county_code": county_code})
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def run(conn, tax_year=None, county_code=DEFAULT_COUNTY):
    """
    Full rollup entry point used by loaders/run_all.py after all source
    loaders finish. Repairs parcel.prop_id's identity FIRST, then
    (re)computes parcel_tax_year's values, so there's no window where one
    is refreshed and the other still reflects stale/contaminated state.
    PARCEL-ROLLUP-HOTFIX-1: county_code threaded through both steps,
    default DEFAULT_COUNTY ("TRAVIS") matching every other real writer's
    one-county-per-call convention.
    """
    repaired = repair_prop_id(conn, county_code=county_code)
    if tax_year is not None:
        rolled = rollup_tax_year(conn, tax_year, county_code=county_code)
    else:
        rolled = rollup_all_years(conn, county_code=county_code)
    return {"prop_id_repaired": repaired, "parcel_tax_year_rows": rolled}


# ── Pure-Python mirror of the SQL above — no DB required. Used by
#    loaders/test_parcel_rollup.py to fixture-test NULL-semantics and
#    idempotency in this sandbox (see that file's docstring for why the
#    SQL itself can't be executed-verified here). Kept in hand-verified
#    lockstep with ROLLUP_SQL / PROP_ID_REPAIR_SQL above — any change to
#    one must be mirrored in the other. ────────────────────────────────
def compute_rollup(unit_rows, tax_year):
    """
    unit_rows: iterable of dicts, each a prop_unit_tax_year row — keys:
    prop_id, tax_year, geo_id, market_value, assessed_value,
    taxable_value, hs_cap_loss, land_value, imprv_value, exemption_codes,
    data_source, and OPTIONALLY prop_unit_geo_id.

    Task M5-PERYEAR-GEOID: `geo_id` here is meant to be the row's own
    real, as-of-that-year prop_unit_tax_year.geo_id (may be None for
    legacy rows not yet backfilled — see ROLLUP_SQL's comment above for
    the full NULL-fallback rationale). `prop_unit_geo_id`, if present, is
    prop_unit's latest-known geo_id for that prop_id, used ONLY as a
    fallback when `geo_id` is None — mirrors ROLLUP_SQL's
    COALESCE(y.geo_id, u.geo_id). Callers with no prop_unit backing at
    all (e.g. snapshot_2026_preliminary.py, which resolves geo_id itself
    directly from that year's own PROP.TXT before calling this function)
    simply omit prop_unit_geo_id — old callers unmodified need no change,
    since `row.get(...)` is None when the key is absent, and `geo_id` is
    already non-None for that caller's rows.

    A row where BOTH geo_id and prop_unit_geo_id are None/absent is
    excluded entirely (mirrors ROLLUP_SQL's WHERE ... IS NOT NULL filter)
    rather than being silently grouped together under one bogus NULL key.

    Returns a list of dicts (one per geo_id present for `tax_year`),
    matching exactly what ROLLUP_SQL would INSERT: geo_id, tax_year,
    market_value, assessed_value, taxable_value, hs_cap_loss, land_value,
    imprv_value, exemption_codes, data_source, unit_count.
    """
    groups = {}
    for row in unit_rows:
        if row["tax_year"] != tax_year:
            continue
        effective_geo_id = row.get("geo_id")
        if effective_geo_id is None:
            effective_geo_id = row.get("prop_unit_geo_id")
        if effective_geo_id is None:
            continue
        groups.setdefault(effective_geo_id, []).append(row)

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
    ap.add_argument(
        "--county", default=DEFAULT_COUNTY,
        help=f"county_code scoping every real query/write in this run (default: {DEFAULT_COUNTY}). "
             "PARCEL-ROLLUP-HOTFIX-1: mirrors scrape_billing_history.py's own --county convention.",
    )
    args = ap.parse_args()

    if not args.all_years and args.year is None:
        ap.error("pass --year YYYY or --all-years")

    conn = get_conn()
    result = run(conn, tax_year=args.year, county_code=args.county)
    print(f"prop_id repaired: {result['prop_id_repaired']:,} rows")
    print(f"parcel_tax_year rolled up: {result['parcel_tax_year_rows']:,} rows")
    conn.close()
