#!/usr/bin/env python3
"""
loaders/refresh_snapshot_summary.py — Task AGGPRECOMP-2, Step 2 (Tier 1) of
SPEC_AGGREGATE_PRECOMPUTATION.md ("Compute-at-Write, Serve-at-Read").

Builds the three Tier 1 Market Snapshot summary tables (snapshot_breakdown,
snapshot_totals, snapshot_neighborhood_movers -- see schema.sql's own
comments for the exact grain of each) by running, ONCE per refresh for ALL
11 real /snapshot ?view= values, the same 5 query bodies
app.py's _compute_snapshot_data() used to run LIVE on every request:
breakdown (GROUPING SETS), the two single-year market-value-total queries
(the INNER JOIN suppression fix), the Part 4 aggregate (new construction /
risk-flag counts), the cert_agg query (2026 preliminary-vs-certified split),
and the top/bottom moving neighborhoods query.

This is the ACTUAL fix for /snapshot's real, live 500 errors -- not a query
optimization. Once this script has run and app.py's _compute_snapshot_data()
is rewired to read these tables (same task, see app.py's diff), the
/snapshot request path performs ZERO live aggregation. Per the spec's own
explicit principle: NO LIVE FALLBACK, ever -- if these tables are missing or
stale, the route shows an honest "data temporarily unavailable" state, never
silently recomputes live (which would just resurrect the exact timeout class
this migration exists to retire).

Reuses, does not reinvent:
  - The exact same load_batch / shadow-table-then-atomic-swap /
    provenance-stamping pattern loaders/refresh_group_stats.py already
    proved live (Step 1, AGGPRECOMP-1) -- see that script's own module
    docstring for the full reasoning (shadow-swap lock-window minimization,
    batch_id staleness semantics, sandbox-vs-live disclosure convention).
  - snapshot_taxonomy.py for every view-scoping SQL-fragment builder
    (ptype_and_sort_case_for_view(), _snapshot_view_where(), the taxonomy
    CASE expressions) -- extracted from app.py specifically so this script
    and the live route build byte-identical SQL from ONE place, not two
    independently-maintained copies of the same branching logic. See that
    module's docstring for why it does NOT import app.py itself.
  - parcel_filters.CANONICAL_PARCEL_EXCL + exclude_non_real_property_gap_sql()
    for the exact same real-property scoping (canonical_excl) app.py's
    _compute_snapshot_data() already applies to all five query bodies
    (confirmed via SNAPSHOT-CORRECTNESS-1, Aug 2026 -- re-confirmed fresh
    during this task's own investigation step, see this task's final report).

── PX-20260831-02 (2026-08-31): single-county build-time seam RETIRED ──────
Every prior version of this script (AGGPRECOMP-2 through PARTITION-2-FIX-1)
took an externally-passed `county_code` parameter (default 'TRAVIS') and
stamped that ONE value onto every row a real refresh wrote, while the five
query bodies below aggregated across EVERY county's parcels with no
county_code filter at all. That was a correct, explicitly-scoped-out
simplification at the time it was written (Travis was the only county with
real data) -- it stopped being correct the moment Dallas's parcels landed in
`parcel`/`parcel_tax_year` (confirmed live, 2026-08-31:
`parcel_tax_year` has 3,576,634 DALLAS rows and 2,774,846 TRAVIS rows).
Running the old code against that data would not add Dallas rows alongside
untouched Travis rows -- it would blend both counties' parcels into one
aggregate, mislabel the result with whichever single county_code the caller
passed, and atomically replace the ENTIRE live table via the shadow-swap,
destroying the other county's real rows in the process. Flagged and blocked
by PX-20260831-01's runbook (R6) before any live run was attempted.

Fixed here at the SOURCE, mirroring the exact same fix `refresh_group_stats.py`
already proved live for `group_stats` (PX-20260828-13, commits `8f9ebdc` +
`5bfe005`): every one of the five query builders now derives `county_code`
directly from `parcel.county_code` and carries it through every GROUP BY /
GROUPING SETS clause, so a single refresh pass correctly computes every
county's rows simultaneously, each genuinely derived from that county's own
parcels. `build_shadow()` / `refresh_snapshot_summary()` no longer take a
`county_code` write-path parameter at all -- there is no longer one county to
parameterize a real refresh with, same as `refresh_group_stats.py`. `--county`
survives on the CLI ONLY for `--check-staleness` reporting (per-county
freshness is still a meaningful, distinct question from "did the one-pass
build derive every county correctly").

Explicitly OUT OF SCOPE for this brief (see AGGPRECOMP-2 brief, "Out of
scope"):
  - group_stats itself (Step 1, already live and stable) -- untouched.
  - Tier 3 peer/benchmark endpoints (api_peer_set, api_peer_benchmark_local,
    and api_benchmark() -- Tier-3-shaped for the same reason even though not
    explicitly named in the brief, see this task's final report) -- Step 4,
    a separate future brief.
  - County-partitioning (Step 5) -- separate, Dallas-prerequisite brief.
  - Wiring this script into the real load pipeline (parcel_rollup.py /
    run_all.py's actual call chain) -- same "standalone + dry-run-capable,
    batch_id parameter ready for a future pipeline caller" posture
    refresh_group_stats.py already established. Nothing calls this script
    automatically yet.

── query_no_nestloop() retirement (per the spec's own explicit instruction:
"Each query migrated into Tier 1 or Tier 3 should have its
query_no_nestloop() call site removed as part of that migration") ──────────
The 4 queries below (breakdown, Part 4 aggregate, cert_agg, neighborhoods)
were app.py's ONLY 4 real call sites of query_no_nestloop() (confirmed via
grep before this migration -- every other hit was a comment referencing it).
This script re-applies the exact same `SET LOCAL enable_nestloop = off`
override to these same 4 query shapes, for the exact same measured reason
(app.py's query_no_nestloop() docstring has the full on/off EXPLAIN ANALYZE
evidence) -- just once per refresh run (~10x/year) instead of once per live
request. app.py's query_no_nestloop() function itself is REMOVED as part of
this migration (see app.py's diff) -- it now has zero remaining callers, so
leaving a zero-caller function in the live request-handling module would be
dead code, not a real safety net.

── Sandbox-vs-live disclosure (same pattern as refresh_group_stats.py) ─────
This sandbox has neither a live Postgres connection nor network access to
install one. The five query bodies below are verified here to be
byte-identical in shape to app.py's PRE-migration live queries (see
test_refresh_snapshot_summary.py's SQL-shape assertions) and the Python-side
merge/override logic (INNER JOIN suppression fix, total-row split, HAVING
>=10 neighborhood filter) is verified against small, known-answer synthetic
fixtures (same discipline as refresh_group_stats.py's PERCENTILE_CONT
reference reimplementation) -- this proves the LOGIC is right, but does NOT
prove the actual SQL strings execute correctly against real Postgres, or
that all 11 views' worth of shadow-build genuinely completes within the
spec's "minutes of pipeline time" cost estimate on real production-scale
data. Diego needs to verify both live (see this task's final report for
exact commands).

Usage:
    cd ~/Desktop/Claude\\ Files/parcel_app
    python3 loaders/refresh_snapshot_summary.py --dry-run       # compute + report row counts, no writes
    python3 loaders/refresh_snapshot_summary.py                 # real refresh, mints its own batch id
    python3 loaders/refresh_snapshot_summary.py --check-staleness   # run the staleness assertion only
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parcel_filters import CANONICAL_PARCEL_EXCL, exclude_non_real_property_gap_sql
from snapshot_taxonomy import _SNAPSHOT_VALID_VIEWS, _snapshot_view_where, ptype_and_sort_case_for_view
# PX-20260831-02 Task 1: reuse refresh_group_stats.py's own "one refresh now
# covers every county" batch-county sentinel rather than inventing a second,
# differently-named one for the identical situation this script now shares
# with that script -- both files live in loaders/, so this is a same-
# directory import (no path games needed beyond the sys.path.insert() above,
# which is already in place for the repo-root imports below).
from refresh_group_stats import GROUP_STATS_BATCH_COUNTY_SENTINEL as _ALL_COUNTIES_BATCH_SENTINEL

# Same construction as app.py's _compute_snapshot_data() `canonical_excl`
# local variable (SNAPSHOT-CORRECTNESS-1, Aug 2026) -- module-level here
# since it doesn't depend on `view`, unlike view_where.
CANONICAL_EXCL = CANONICAL_PARCEL_EXCL + f" AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"


class SnapshotConsistencyError(RuntimeError):
    """Raised by refresh_snapshot_summary() when the post-refresh
    (view, county_code) consistency assertion (see
    assert_snapshot_breakdown_totals_consistent()) finds a real mismatch
    immediately after a refresh has already swapped in. Unlike
    compute_metrics.py's MetricsIntegrityError, raising this does NOT roll
    anything back -- the shadow-swap has no single enclosing transaction to
    unwind (see refresh_snapshot_summary()'s own docstring for why) -- this
    is a loud, immediate post-hoc alarm, not a preventative gate."""


# ── Per-view SQL builders (byte-identical in shape to app.py's PRE-migration
# _compute_snapshot_data() query bodies -- only the view-dependent fragments
# come from snapshot_taxonomy.py instead of a local if/elif block) ─────────

def breakdown_sql(view):
    """
    PX-20260831-02 Task 1: county_code is now a genuine SELECT/GROUP BY
    column, derived from p.county_code -- NOT an externally-stamped literal
    (see module docstring's "single-county build-time seam RETIRED" section).
    Both parcel_tax_year joins are now also equality-scoped on county_code,
    not just geo_id/tax_year -- matching refresh_group_stats.py's own
    REFRESH_GROUP_STATS_SQL join shape exactly (see that file's `effective`
    CTE). The GROUPING SETS clause carries p.county_code in BOTH grouping
    sets, so the grand-total row (ptype/sort_key NULL) is now produced once
    PER COUNTY, not once per view -- matching this table's real, live
    composite PK (county_code, view, ptype), confirmed via
    migrate_county_partitioning.py's own TABLE_SPECS entry for this table.
    """
    ptype_case, sort_case, _bench_labels, _order_by, _fallback = ptype_and_sort_case_for_view(view)
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.county_code                                                                   AS county_code,
            ({ptype_case})                                                                  AS ptype,
            ({sort_case})                                                                    AS sort_key,
            COUNT(*)                                                                        AS n_parcels,
            SUM(CASE WHEN t26.market_value > t25.market_value THEN 1 ELSE 0 END)            AS n_up,
            SUM(CASE WHEN t26.market_value < t25.market_value THEN 1 ELSE 0 END)            AS n_down,
            SUM(CASE WHEN t26.market_value = t25.market_value THEN 1 ELSE 0 END)            AS n_flat,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS median_pct,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS p25_pct,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS p75_pct,
            ROUND(SUM(t25.market_value)::NUMERIC / 1e9, 3)                                  AS total_mv25_b,
            ROUND(SUM(t26.market_value)::NUMERIC / 1e9, 3)                                  AS total_mv26_b
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025 AND t25.county_code = p.county_code
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026 AND t26.county_code = p.county_code
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY GROUPING SETS ((p.county_code, ({ptype_case}), ({sort_case})), (p.county_code))
    """


def single_year_mv_sql(view, year):
    """PX-20260831-02 Task 1: same county_code derivation/join-scoping/
    GROUPING SETS treatment as breakdown_sql() above -- see that function's
    docstring."""
    ptype_case, _sort_case, _bench_labels, _order_by, _fallback = ptype_and_sort_case_for_view(view)
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.county_code                                    AS county_code,
            ({ptype_case})                                  AS ptype,
            ROUND(SUM(t.market_value)::NUMERIC / 1e9, 3)     AS total_mv_b
        FROM parcel p
        JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = {year} AND t.county_code = p.county_code
        WHERE t.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY GROUPING SETS ((p.county_code, ({ptype_case})), (p.county_code))
    """


def part4_agg_sql(view):
    """PX-20260831-02 Task 1: was a single view-wide aggregate row (implicitly
    Travis-only in effect, since Travis was the only county with data when
    written); now GROUP BY p.county_code produces one row per county. The
    LEFT JOIN to parcel_metrics is now also county_code-scoped -- the same
    ungated-join class Task 5 fixes in compute_metrics.py itself; parcel_metrics
    carries a real, live county_code column (confirmed via compute_metrics.py's
    own comment: live PK is (county_code, geo_id, tax_year))."""
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.county_code                                              AS county_code,
            COUNT(*) FILTER (WHERE p.year_built >= 2025)              AS n_new_construction,
            COUNT(*) FILTER (WHERE pm.risk_large_value_jump = TRUE)   AS n_risk_flagged
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025 AND t25.county_code = p.county_code
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026 AND t26.county_code = p.county_code
        LEFT JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = 2026 AND pm.county_code = p.county_code
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY p.county_code
    """


def cert_agg_sql(view):
    """PX-20260831-02 Task 1: same per-county GROUP BY treatment as
    part4_agg_sql() above."""
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.county_code                                            AS county_code,
            COUNT(*) FILTER (WHERE t26.data_source = 'preliminary') AS n_preliminary,
            COUNT(*)                                                AS n_total
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025 AND t25.county_code = p.county_code
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026 AND t26.county_code = p.county_code
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY p.county_code
    """


def neighborhoods_sql(view):
    """PX-20260831-02 Task 1: GROUP BY now (county_code, neighborhood_cd), not
    neighborhood_cd alone -- neighborhood codes are short, locally-assigned
    strings with no guarantee of cross-county uniqueness (the same class of
    risk geo_id carried before this project's composite-PK convention), so
    grouping by neighborhood_cd alone would have silently blended two
    counties' same-coded neighborhoods into one row the instant both had
    real data. Matches this table's real, live composite PK
    (county_code, view, neighborhood_cd) per migrate_county_partitioning.py's
    TABLE_SPECS."""
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.county_code AS county_code,
            p.neighborhood_cd,
            COUNT(*) AS n_parcels,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2) AS median_pct
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025 AND t25.county_code = p.county_code
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026 AND t26.county_code = p.county_code
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          AND p.neighborhood_cd IS NOT NULL AND p.neighborhood_cd != ''
          {view_where}
        GROUP BY p.county_code, p.neighborhood_cd
        HAVING COUNT(*) >= 10
    """


# ── Pure Python merge logic (the part fixture-tested against synthetic data
# -- no DB required) ─────────────────────────────────────────────────────────

def merge_breakdown_rows(breakdown_rows, mv25_rows, mv26_rows):
    """
    Reproduces _compute_snapshot_data()'s exact merge: split the GROUPING
    SETS result into per-ptype rows + the grand-total row (ptype IS NULL),
    then overwrite total_mv25_b/total_mv26_b from the independent single-year
    queries (the INNER JOIN suppression fix -- a parcel present in only one
    of the two years must not be silently dropped from EITHER year's dollar
    total by the paired-JOIN breakdown query).

    PX-20260831-02 Task 1: every row (breakdown, mv25, mv26) now carries a
    real county_code column (see the three builders' own docstrings) -- the
    matching key for the mv25/mv26 lookup is (county_code, ptype), not ptype
    alone, and the grand-total row is now per (county_code), not per view.
    Two counties' grand-total rows both have ptype IS NULL but DIFFERENT
    county_code values -- matching them by ptype alone would have collapsed
    both counties' totals into whichever one the dict lookup happened to
    keep last, a silent blend of exactly the kind this whole brief exists to
    close off.

    Returns (rows, totals_rows) -- `rows` are the per-(county_code, ptype)
    rows (UNCAPPED -- capping is read-time, not here), `totals_rows` is a
    LIST of one dict per county that has any qualifying parcels for this
    view (empty list if no county qualifies) -- this is a real signature
    change from the prior single-county version (which returned one dict or
    None), required because a single view can now genuinely have more than
    one county's grand-total row.
    """
    mv25_by_key = {(r["county_code"], r["ptype"]): r["total_mv_b"] for r in mv25_rows}
    mv26_by_key = {(r["county_code"], r["ptype"]): r["total_mv_b"] for r in mv26_rows}

    rows = [dict(r) for r in breakdown_rows if r["ptype"] is not None]
    for r in rows:
        key = (r["county_code"], r["ptype"])
        r["total_mv25_b"] = mv25_by_key.get(key, r["total_mv25_b"])
        r["total_mv26_b"] = mv26_by_key.get(key, r["total_mv26_b"])

    totals_rows = []
    for total_row_raw in (r for r in breakdown_rows if r["ptype"] is None):
        cc = total_row_raw["county_code"]
        key = (cc, None)
        totals_rows.append({
            "county_code": cc,
            "n_total": total_row_raw["n_parcels"],
            "n_up": total_row_raw["n_up"],
            "n_down": total_row_raw["n_down"],
            "n_flat": total_row_raw["n_flat"],
            "total_mv25_b": mv25_by_key.get(key, total_row_raw["total_mv25_b"]),
            "total_mv26_b": mv26_by_key.get(key, total_row_raw["total_mv26_b"]),
            "median_pct": total_row_raw["median_pct"],
        })
    return rows, totals_rows


def _mint_batch(conn, note, county_code=_ALL_COUNTIES_BATCH_SENTINEL):
    # county_code (PX-20260831-02 Task 1): since a real refresh now covers
    # EVERY county in one pass (see module docstring's "single-county
    # build-time seam RETIRED" section), there is no longer one real
    # county_code to stamp on this batch row -- same situation
    # refresh_group_stats.py already solved via GROUP_STATS_BATCH_COUNTY_
    # SENTINEL = "ALL", imported here and reused rather than reinvented (a
    # second, differently-spelled sentinel for the identical situation would
    # be its own footgun). load_batch.county_code is NOT NULL with no
    # default (migrate_county_partitioning.py, add_column mode) -- 'ALL' is
    # a documented sentinel, never a real registered county code.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO load_batch (note, county_code) VALUES (%s, %s) RETURNING batch_id",
            (note, county_code),
        )
        batch_id = cur.fetchone()[0]
    conn.commit()
    return batch_id


def _compute_one_view(conn, view, verbose=True):
    """
    Runs all 5 query bodies for one view against a live conn, returns
    (rows, totals_rows, neighborhoods_by_county).

    PX-20260831-02 Task 1: `totals_rows` is now a LIST (one dict per county
    with qualifying parcels for this view, each already carrying its own
    new_construction_count/risk_flagged_count/n_preliminary_2026/
    n_total_2026 keys attached below), and `neighborhood_rows` is now
    grouped into a {county_code: [rows]} dict -- both are real signature
    changes from the single-county version (which returned a single
    totals_row-or-None and a flat neighborhood_rows list), required because
    one view can now genuinely produce more than one county's worth of
    results in a single pass. part4_agg_sql()/cert_agg_sql()/
    neighborhoods_sql() are only run when at least one county has
    qualifying parcels for this view (mirrors the old `if totals_row:`
    short-circuit, just checked against "any county qualifies" instead of
    "the one implicit county qualifies").

    Requires psycopg2.extras.RealDictCursor semantics (dict-like rows,
    matching app.py's query()/query_no_nestloop() cursor_factory) so
    merge_breakdown_rows() above can index by column name identically to
    the live app's own Python-side merge.
    """
    import psycopg2.extras

    def _fetch(sql, nestloop_off=False, one=False):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if nestloop_off:
                cur.execute("SET LOCAL enable_nestloop = off")
            cur.execute(sql)
            return cur.fetchone() if one else cur.fetchall()

    breakdown_rows = _fetch(breakdown_sql(view), nestloop_off=True)
    mv25_rows = _fetch(single_year_mv_sql(view, 2025))
    mv26_rows = _fetch(single_year_mv_sql(view, 2026))
    rows, totals_rows = merge_breakdown_rows(breakdown_rows, mv25_rows, mv26_rows)

    counties_with_data = {t["county_code"] for t in totals_rows}

    part4_by_county = {}
    cert_by_county = {}
    neighborhoods_by_county = {}
    if counties_with_data:
        for r in _fetch(part4_agg_sql(view), nestloop_off=True):
            part4_by_county[r["county_code"]] = r
        for r in _fetch(cert_agg_sql(view), nestloop_off=True):
            cert_by_county[r["county_code"]] = r
        for nb in _fetch(neighborhoods_sql(view), nestloop_off=True):
            neighborhoods_by_county.setdefault(nb["county_code"], []).append(nb)

    for t in totals_rows:
        cc = t["county_code"]
        agg = part4_by_county.get(cc)
        t["new_construction_count"] = int(agg["n_new_construction"] or 0) if agg else 0
        t["risk_flagged_count"] = int(agg["n_risk_flagged"] or 0) if agg else 0
        cagg = cert_by_county.get(cc)
        if cagg and cagg["n_total"]:
            t["n_preliminary_2026"] = int(cagg["n_preliminary"] or 0)
            t["n_total_2026"] = int(cagg["n_total"])
        else:
            t["n_preliminary_2026"] = 0
            t["n_total_2026"] = 0

    return rows, totals_rows, neighborhoods_by_county


def build_shadow(conn, batch_id, verbose=True):
    """
    Phase 1: build all three shadow tables fresh, across all 11 views AND
    every county present in the data, in one pass. Does NOT touch the live
    snapshot_breakdown/snapshot_totals/snapshot_neighborhood_movers tables
    at all -- safe to run while those are being read by live traffic,
    however long it takes.

    PX-20260831-02 Task 1 (2026-08-31): county_code is NO LONGER a parameter
    here -- this is the retirement of the seam the paragraph below describes.
    Every row this function writes now carries a county_code DERIVED from
    that row's own parcels (see the five query builders' own docstrings),
    exactly mirroring refresh_group_stats.py's build_shadow() (PX-20260828-13).
    Required, not merely simpler, for the identical reason that fix gave:
    these three tables are swapped in as a full-table replace (see module
    docstring's shadow-swap section) -- a per-county-scoped build would have
    to either wipe out every OTHER county's rows on swap, or abandon the
    shadow-swap pattern for a slower in-place per-county delete+insert.
    Building every county at once, in one shadow table, keeps the atomic
    full-table swap intact and correct.

    ── Retired paragraph, correct when written (PARTITION-2-FIX-1), kept
    verbatim below for history ──────────────────────────────────────────
    "county_code (PARTITION-2-FIX-1): every row this function writes now
    stamps county_code explicitly. Required, not cosmetic --
    migrate_county_partitioning.py's real, already-run migration made
    county_code a NOT NULL column with no default on all three of these
    tables (finding 9.4: the default is deliberately dropped post-migration
    to prevent silent contamination), so the INSERTs below would fail
    outright without this. Defaults to 'TRAVIS' -- same single hardcoded
    seam as every other PARTITION-2-IMPLEMENT Part 3 call site; per this
    brief's explicit scope boundary, this is NOT an attempt to make this
    script multi-county-aware (that's reload_county_scope.py's job, for the
    future) -- every value written here is 'TRAVIS', matching today's real,
    single-county production data." -- correct on 2026-08-28 when Travis was
    the only county with real data; retired 2026-08-31 (PX-20260831-02 Task 1)
    once Dallas's parcels made the single-county assumption actively wrong.
    """
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    with conn.cursor() as cur:
        for tbl in ("snapshot_breakdown", "snapshot_totals", "snapshot_neighborhood_movers"):
            cur.execute(f"DROP TABLE IF EXISTS {tbl}_shadow")
            cur.execute(f"CREATE TABLE {tbl}_shadow (LIKE {tbl} INCLUDING ALL)")

    breakdown_row_count = 0
    totals_row_count = 0
    nb_row_count = 0

    for view in sorted(_SNAPSHOT_VALID_VIEWS):
        rows, totals_rows, neighborhoods_by_county = _compute_one_view(conn, view, verbose=verbose)

        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO snapshot_breakdown_shadow
                        (county_code, view, ptype, sort_key, n_parcels, n_up, n_down, n_flat,
                         median_pct, p25_pct, p75_pct, total_mv25_b, total_mv26_b,
                         source_import_batch_id, refreshed_at)
                    VALUES (%(county_code)s, %(view)s, %(ptype)s, %(sort_key)s, %(n_parcels)s, %(n_up)s,
                            %(n_down)s, %(n_flat)s, %(median_pct)s, %(p25_pct)s, %(p75_pct)s,
                            %(total_mv25_b)s, %(total_mv26_b)s, %(batch_id)s, NOW())
                    """,
                    {
                        "county_code": r["county_code"],
                        "view": view, "ptype": r["ptype"], "sort_key": str(r["sort_key"]) if r["sort_key"] is not None else None,
                        "n_parcels": r["n_parcels"], "n_up": r["n_up"], "n_down": r["n_down"], "n_flat": r["n_flat"],
                        "median_pct": r["median_pct"], "p25_pct": r["p25_pct"], "p75_pct": r["p75_pct"],
                        "total_mv25_b": r["total_mv25_b"], "total_mv26_b": r["total_mv26_b"],
                        "batch_id": batch_id,
                    },
                )
                breakdown_row_count += 1

            for t in totals_rows:
                cur.execute(
                    """
                    INSERT INTO snapshot_totals_shadow
                        (county_code, view, n_total, n_up, n_down, n_flat, median_pct, total_mv25_b, total_mv26_b,
                         new_construction_count, risk_flagged_count, n_preliminary_2026, n_total_2026,
                         source_import_batch_id, refreshed_at)
                    VALUES (%(county_code)s, %(view)s, %(n_total)s, %(n_up)s, %(n_down)s, %(n_flat)s, %(median_pct)s,
                            %(total_mv25_b)s, %(total_mv26_b)s, %(new_construction_count)s,
                            %(risk_flagged_count)s, %(n_preliminary_2026)s, %(n_total_2026)s,
                            %(batch_id)s, NOW())
                    """,
                    {
                        "county_code": t["county_code"],
                        "view": view, "n_total": t["n_total"], "n_up": t["n_up"],
                        "n_down": t["n_down"], "n_flat": t["n_flat"],
                        "median_pct": t["median_pct"], "total_mv25_b": t["total_mv25_b"],
                        "total_mv26_b": t["total_mv26_b"],
                        "new_construction_count": t["new_construction_count"],
                        "risk_flagged_count": t["risk_flagged_count"],
                        "n_preliminary_2026": t["n_preliminary_2026"], "n_total_2026": t["n_total_2026"],
                        "batch_id": batch_id,
                    },
                )
                totals_row_count += 1

            for cc, nb_rows in neighborhoods_by_county.items():
                for nb in nb_rows:
                    cur.execute(
                        """
                        INSERT INTO snapshot_neighborhood_movers_shadow
                            (county_code, view, neighborhood_cd, n_parcels, median_pct, source_import_batch_id, refreshed_at)
                        VALUES (%(county_code)s, %(view)s, %(neighborhood_cd)s, %(n_parcels)s, %(median_pct)s, %(batch_id)s, NOW())
                        """,
                        {
                            "county_code": cc,
                            "view": view, "neighborhood_cd": nb["neighborhood_cd"],
                            "n_parcels": nb["n_parcels"], "median_pct": nb["median_pct"],
                            "batch_id": batch_id,
                        },
                    )
                    nb_row_count += 1

        nb_total_this_view = sum(len(v) for v in neighborhoods_by_county.values())
        _log(f"    view={view:14s} breakdown={len(rows):4d} rows  "
             f"totals={len(totals_rows):2d} counties  neighborhoods={nb_total_this_view:4d} rows")

    conn.commit()
    _log(f"    shadow tables built: {breakdown_row_count:,} breakdown / {totals_row_count:,} totals / "
         f"{nb_row_count:,} neighborhood rows (all counties, one pass)  [{time.time()-t0:.1f}s]")
    return breakdown_row_count, totals_row_count, nb_row_count


def swap_shadow_in(conn, verbose=True):
    """
    Phase 2: atomic swap of ALL THREE tables together, in ONE transaction --
    either all nine DDL statements commit together or none do. This is
    stronger than swapping each table independently: _compute_snapshot_data()
    reads snapshot_breakdown + snapshot_totals + snapshot_neighborhood_movers
    together for one response, so a reader must never see (e.g.) a
    just-refreshed snapshot_breakdown alongside a still-old
    snapshot_neighborhood_movers mid-swap.
    """
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    with conn.cursor() as cur:
        for tbl in ("snapshot_breakdown", "snapshot_totals", "snapshot_neighborhood_movers"):
            cur.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_old")
            cur.execute(f"ALTER TABLE {tbl}_shadow RENAME TO {tbl}")
            cur.execute(f"DROP TABLE {tbl}_old")
    conn.commit()
    _log(f"    swap committed (3 tables)  [{time.time()-t0:.3f}s]")


def refresh_snapshot_summary(conn, batch_id=None, dry_run=False, verbose=True):
    """
    Full refresh entry point. Same signature/behavior contract as
    refresh_group_stats.refresh_group_stats() -- including, as of
    PX-20260831-02 Task 1, the ABSENCE of a county_code parameter: a real
    refresh now always computes every county's rows in one pass (see
    build_shadow()'s docstring), so there is no longer one county to
    parameterize a run with.

    PX-20260831-02 Task 1: after a real (non-dry-run) refresh swaps in, this
    function now ALSO runs assert_snapshot_breakdown_totals_consistent()
    immediately and raises SnapshotConsistencyError if it finds any real
    mismatch -- not gated behind a separate --check-consistency flag
    anymore. A refresh that silently produces internally-inconsistent
    numbers for any (view, county_code) pair is exactly the "completed
    successfully but wrong" failure class this codebase treats as
    unacceptable elsewhere (see compute_metrics.py's MetricsIntegrityError).
    This check runs for EVERY county present in the just-refreshed data
    automatically, since assert_snapshot_breakdown_totals_consistent() is
    not itself county-scoped -- it already compares every (view, county_code)
    key found in either table.
    """
    def _log(msg):
        if verbose:
            print(msg)

    if dry_run:
        t0 = time.time()
        total_breakdown = 0
        total_totals = 0
        total_nb = 0
        sample = None
        for view in sorted(_SNAPSHOT_VALID_VIEWS):
            rows, totals_rows, neighborhoods_by_county = _compute_one_view(conn, view, verbose=False)
            total_breakdown += len(rows)
            total_totals += len(totals_rows)
            total_nb += sum(len(v) for v in neighborhoods_by_county.values())
            if sample is None and rows:
                sample = {"view": view, "rows": rows[:3]}
        _log(f"[DRY RUN] {total_breakdown:,} breakdown rows / {total_totals:,} totals rows (all counties) / "
             f"{total_nb:,} neighborhood rows would be computed across "
             f"{len(_SNAPSHOT_VALID_VIEWS)} views  [{time.time()-t0:.1f}s]")
        return {
            "dry_run": True, "breakdown_row_count": total_breakdown,
            "totals_row_count": total_totals, "neighborhood_row_count": total_nb,
            "sample": sample, "batch_id": None,
        }

    used_batch_id = batch_id
    if used_batch_id is None:
        used_batch_id = _mint_batch(conn, note="refresh_snapshot_summary.py standalone run")
        _log(f"  Minted new load_batch row: batch_id={used_batch_id} "
             f"(standalone mode -- no pipeline caller passed one in; "
             f"county_code='{_ALL_COUNTIES_BATCH_SENTINEL}' -- this batch covers every county)")
    else:
        _log(f"  Using caller-supplied batch_id={used_batch_id}")

    breakdown_row_count, totals_row_count, nb_row_count = build_shadow(
        conn, used_batch_id, verbose=verbose
    )
    swap_shadow_in(conn, verbose=verbose)

    # PX-20260831-02 Task 1: post-refresh consistency alarm. Runs AFTER the
    # swap has already committed -- unlike compute_metrics.py's row-count
    # sanity check, this table's shadow-swap architecture has no single
    # enclosing transaction to roll back into, so this is a loud, immediate
    # post-hoc alarm, not a preventative rollback (see
    # SnapshotConsistencyError's own docstring).
    is_consistent, consistency_detail = assert_snapshot_breakdown_totals_consistent(conn)
    _log(f"  Post-refresh (view, county_code) consistency check: {is_consistent}")
    if not is_consistent:
        raise SnapshotConsistencyError(
            f"refresh completed and swapped, but "
            f"{len(consistency_detail['mismatches'])} cross-table mismatch(es) "
            f"found immediately after -- see detail['mismatches']: "
            f"{consistency_detail['mismatches']}"
        )

    return {
        "dry_run": False, "breakdown_row_count": breakdown_row_count,
        "totals_row_count": totals_row_count, "neighborhood_row_count": nb_row_count,
        "batch_id": used_batch_id, "consistency_detail": consistency_detail,
    }


def assert_snapshot_summary_fresh(conn, county_code="TRAVIS"):
    """
    Staleness assertion across all three Tier 1 tables. Modeled directly on
    refresh_group_stats.assert_group_stats_fresh() -- extended to require
    all three tables agree with EACH OTHER as well as with the latest
    load_batch, since a genuinely atomic swap (see swap_shadow_in() above)
    means they can never legitimately disagree; if they do, that's proof the
    swap was NOT atomic (a real bug), not just staleness.

    county_code (PARTITION-2-IMPLEMENT, Part 3): scoped per county, same
    real reasoning as app.py's _snapshot_summary_freshness() (this
    function's own direct real-world twin -- that function's docstring
    literally names this one as the loader-side original it was modeled
    on) and refresh_group_stats.assert_group_stats_fresh(). SCOPE NOTE:
    this specific function wasn't named in PARTITION-2-IMPLEMENT's brief
    text (which named _snapshot_summary_freshness() and
    refresh_group_stats.py's --check-staleness explicitly) -- extended to
    this one too as a deliberate, disclosed judgment call, not silently:
    leaving this function table-wide while its intentionally-mirrored
    app.py twin became per-county-scoped would immediately reintroduce
    finding 9.7's exact bug, just discovered from the loader side (a
    --check-staleness run) instead of the request side. See this task's
    final report for this call being flagged explicitly, per this
    project's standing practice of naming judgment calls rather than
    deciding them silently.

    Defaults to 'TRAVIS' -- same hardcoded seam as every other
    PARTITION-2-IMPLEMENT Part 3 call site.

    REAL DEPLOYMENT-SEQUENCING WARNING (same as every other Part 3 change):
    references a county_code column that does not exist on
    snapshot_breakdown/snapshot_totals/snapshot_neighborhood_movers until
    migrate_county_partitioning.py's migration of those three tables has
    actually run. Do not deploy/run against a database where that hasn't
    happened yet.

    NOT extended in this brief: assert_snapshot_breakdown_totals_consistent()
    (AGGPRECOMP-2-FIX-2, Fix 3) -- a genuinely different, data-CORRECTNESS
    check (not a freshness/staleness check), out of PARTITION-2-IMPLEMENT's
    named scope ("no changes to anything not named in §4.3's table list or
    this brief's six parts"). It would likely need the same per-county
    scoping eventually, for the same class of reason -- flagged as a real,
    separate, un-actioned follow-up in this task's final report, not
    silently left unaddressed.

    Returns (is_fresh: bool, detail: dict).

    HONEST LIMITATION (same as assert_group_stats_fresh()): in standalone-
    only mode, this assertion trivially PASSES right after any refresh,
    since this script and refresh_group_stats.py are currently the ONLY
    writers of load_batch. It only becomes a meaningful staleness check once
    a later brief wires the real load pipeline to mint load_batch rows
    independently.
    """
    tables = ("snapshot_breakdown", "snapshot_totals", "snapshot_neighborhood_movers")
    batch_ids_by_table = {}
    with conn.cursor() as cur:
        for tbl in tables:
            cur.execute(
                f"SELECT DISTINCT source_import_batch_id FROM {tbl} WHERE county_code = %s",
                (county_code,),
            )
            batch_ids_by_table[tbl] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT MAX(batch_id) FROM load_batch")
        row = cur.fetchone()
        latest_batch_id = row[0] if row else None

    detail = {
        "county_code": county_code,
        "latest_batch_id": latest_batch_id,
        "batch_ids_by_table": {k: sorted(v) for k, v in batch_ids_by_table.items()},
    }

    for tbl in tables:
        if not batch_ids_by_table[tbl]:
            detail["reason"] = f"{tbl} has no rows for county_code={county_code!r} -- cannot be fresh (nothing to check)"
            return False, detail
        if len(batch_ids_by_table[tbl]) > 1:
            detail["reason"] = (f"{tbl} contains rows from more than one batch_id for "
                                 f"county_code={county_code!r} -- a partial/failed refresh; "
                                 f"should be impossible if the shadow-swap/county-scoped "
                                 f"reload is genuinely atomic")
            return False, detail

    if latest_batch_id is None:
        detail["reason"] = "load_batch is empty -- no known batch to compare against"
        return False, detail

    table_batch_ids = {tbl: next(iter(batch_ids_by_table[tbl])) for tbl in tables}
    distinct_table_batches = set(table_batch_ids.values())
    if len(distinct_table_batches) > 1:
        detail["reason"] = (f"the three Tier 1 tables disagree with each other on "
                             f"source_import_batch_id for county_code={county_code!r} "
                             f"({table_batch_ids}) -- the atomic swap/reload did not actually "
                             f"keep them in sync; this should be impossible and indicates a "
                             f"real bug, not ordinary staleness")
        return False, detail

    common_batch_id = next(iter(distinct_table_batches))
    if common_batch_id != latest_batch_id:
        detail["reason"] = (f"Tier 1 tables for county_code={county_code!r} reflect batch "
                             f"{common_batch_id}, but the latest known batch is "
                             f"{latest_batch_id} -- STALE")
        return False, detail

    detail["reason"] = f"all three Tier 1 tables match the latest known batch for county_code={county_code!r}"
    return True, detail


def assert_snapshot_breakdown_totals_consistent(conn, tolerance_b=0.01):
    """
    AGGPRECOMP-2-FIX-2, Fix 3 -- cross-table DATA consistency assertion, a
    genuinely different check from assert_snapshot_summary_fresh() above.
    That function proves the atomic swap kept all three tables on the same
    batch_id (no partial-swap corruption); THIS one proves the data itself
    is internally consistent -- that snapshot_totals' per-view aggregate
    numbers actually equal the real, independently-computed sums over that
    same view's own snapshot_breakdown rows. A refresh can complete
    successfully, swap atomically, and STILL produce numbers that don't
    add up if there's ever drift between breakdown_sql()'s and
    single_year_mv_sql()'s WHERE/GROUP BY logic for a given view -- exactly
    the class of bug SNAPSHOT-CORRECTNESS-1/AGGPRECOMP-1-FIX found and
    fixed once already in the pre-migration live queries (a canonical_excl
    fragment applied to one query site but not a sibling one). This
    assertion is the harness's standing guard against that exact class of
    bug recurring silently inside the refresh script.

    n_total/n_up/n_down/n_flat are checked for EXACT equality -- they come
    from the SAME breakdown_sql() GROUPING SETS query as the per-ptype
    rows (just a different grouping-set level within ONE query execution),
    so any real mismatch here means rows were lost/corrupted somewhere
    between compute and write, not merely query-logic drift between two
    different queries -- a stronger, more surprising failure mode.

    total_mv25_b/total_mv26_b are checked within `tolerance_b` (default
    0.01, i.e. $10K on a billions-denominated column) rather than exact
    equality -- these DO come from a structurally separate query
    (single_year_mv_sql()), independently ROUND()ed to 3 decimals at both
    the per-ptype and grand-total aggregation levels, so a few cents of
    independent-rounding drift accumulated across potentially hundreds of
    ptype rows is expected, real, benign behavior -- not a bug worth
    failing the assertion over.

    median_pct is DELIBERATELY NOT checked here -- a percentile has no
    valid mathematical identity relating a grand-total median to its
    per-group medians (it is not a sum or an average of the parts), so
    "checking" it would either be a meaningless no-op or, worse, a false
    assertion of a relationship that doesn't actually hold. Deliberate
    scope decision, not an oversight.

    PX-20260830-05 Task 3 (Bucket C): grouped by (view, county_code) on
    BOTH sides, not just view. snapshot_breakdown/snapshot_totals/
    snapshot_neighborhood_movers are all composite_pk-migrated live
    (county_code added as the leading PK column by
    migrate_county_partitioning.py -- confirmed via that script's own
    TABLE_SPECS entries for all three tables -- new_pk
    (county_code, view, ptype) / (county_code, view) /
    (county_code, view, neighborhood_cd) respectively). schema.sql's
    bootstrap CREATE TABLE text for all three is stale relative to that
    live migration -- a disclosure comment for this was added directly to
    schema.sql as part of PX-20260831-02 Task 1, matching the same
    disclosure convention already used there for parcel_metrics/
    ingest_audit/load_batch's own stale bootstrap DDL. Grouping by view
    alone here would silently blend every county's breakdown rows into one
    sum and compare it against whichever single county's snapshot_totals
    row happened to exist for that view -- exactly the kind of false-pass
    this assertion exists to prevent.

    Returns (is_consistent: bool, detail: dict). detail["mismatches"] is a
    list of every real discrepancy found (empty when fully consistent).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT view, county_code,
                   SUM(n_parcels)    AS sum_n_parcels,
                   SUM(n_up)         AS sum_n_up,
                   SUM(n_down)       AS sum_n_down,
                   SUM(n_flat)       AS sum_n_flat,
                   SUM(total_mv25_b) AS sum_mv25,
                   SUM(total_mv26_b) AS sum_mv26
            FROM snapshot_breakdown
            GROUP BY view, county_code
        """)
        breakdown_sums = {
            (r[0], r[1]): {"n_parcels": r[2], "n_up": r[3], "n_down": r[4], "n_flat": r[5],
                           "total_mv25_b": r[6], "total_mv26_b": r[7]}
            for r in cur.fetchall()
        }

        cur.execute("""
            SELECT view, county_code, n_total, n_up, n_down, n_flat, total_mv25_b, total_mv26_b
            FROM snapshot_totals
        """)
        totals_by_view = {
            (r[0], r[1]): {"n_total": r[2], "n_up": r[3], "n_down": r[4], "n_flat": r[5],
                           "total_mv25_b": r[6], "total_mv26_b": r[7]}
            for r in cur.fetchall()
        }

    mismatches = []
    all_keys = sorted(set(breakdown_sums) | set(totals_by_view))
    for view, county_code in all_keys:
        bsum = breakdown_sums.get((view, county_code))
        tot = totals_by_view.get((view, county_code))

        if bsum is None:
            mismatches.append({"view": view, "county_code": county_code,
                                "reason": "snapshot_totals has a row for this (view, county_code) but "
                                          "snapshot_breakdown has ZERO rows -- should be impossible"})
            continue
        if tot is None:
            mismatches.append({"view": view, "county_code": county_code,
                                "reason": "snapshot_breakdown has rows for this (view, county_code) but "
                                          "snapshot_totals has NO row -- should be impossible"})
            continue

        if bsum["n_parcels"] != tot["n_total"]:
            mismatches.append({"view": view, "county_code": county_code, "field": "n_total",
                                "breakdown_sum": bsum["n_parcels"], "totals_value": tot["n_total"],
                                "reason": "SUM(snapshot_breakdown.n_parcels) != snapshot_totals.n_total"})
        for f in ("n_up", "n_down", "n_flat"):
            if bsum[f] != tot[f]:
                mismatches.append({"view": view, "county_code": county_code, "field": f,
                                    "breakdown_sum": bsum[f], "totals_value": tot[f],
                                    "reason": f"SUM(snapshot_breakdown.{f}) != snapshot_totals.{f}"})

        for f in ("total_mv25_b", "total_mv26_b"):
            b_val = float(bsum[f] or 0)
            t_val = float(tot[f] or 0)
            if abs(b_val - t_val) > tolerance_b:
                mismatches.append({"view": view, "county_code": county_code, "field": f,
                                    "breakdown_sum": b_val, "totals_value": t_val,
                                    "diff": round(b_val - t_val, 6),
                                    "reason": f"SUM(snapshot_breakdown.{f}) vs snapshot_totals.{f} "
                                              f"differ by more than tolerance ({tolerance_b})"})

    is_consistent = len(mismatches) == 0
    detail = {
        "views_and_counties_checked": all_keys,
        "tolerance_b": tolerance_b,
        "mismatches": mismatches,
        "reason": ("all (view, county_code) pairs' snapshot_totals match the real computed sums "
                   "over their own snapshot_breakdown rows" if is_consistent else
                   f"{len(mismatches)} real cross-table mismatch(es) found -- see detail['mismatches']"),
    }
    return is_consistent, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Compute + report row counts only; no writes")
    ap.add_argument("--check-staleness", action="store_true",
                     help="Run BOTH the staleness assertion AND the cross-table consistency "
                          "assertion (AGGPRECOMP-2-FIX-2, Fix 3); no refresh. Two genuinely "
                          "different checks -- swap atomicity vs. data correctness -- reported "
                          "separately below even though this one flag runs both.")
    ap.add_argument("--check-consistency", action="store_true",
                     help="Run ONLY the cross-table consistency assertion (Fix 3); no refresh, "
                          "no staleness check.")
    ap.add_argument("--batch-id", type=int, default=None,
                    help="Tag this refresh with an existing load_batch.batch_id "
                         "(future pipeline use; standalone runs normally omit this)")
    ap.add_argument("--county", default="TRAVIS",
                    help="county_code to check with --check-staleness ONLY (default: TRAVIS). "
                         "As of PX-20260831-02 Task 1 (mirrors refresh_group_stats.py's own "
                         "--county flag exactly), a real refresh (no flag / --dry-run) always "
                         "computes EVERY county's snapshot summary rows in one pass -- "
                         "county_code is now derived per-row from parcel.county_code inside "
                         "each of the five query builders, not an external value you choose "
                         "per run. This flag has no effect on a real refresh, --dry-run, or "
                         "--check-consistency (which was never county-scoped -- see "
                         "assert_snapshot_breakdown_totals_consistent()'s docstring); it only "
                         "selects which county's freshness --check-staleness reports on.")
    args = ap.parse_args()

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}  — confirm this is the environment you intend BEFORE any write commits.\n")

    if args.check_consistency:
        is_consistent, detail = assert_snapshot_breakdown_totals_consistent(conn)
        print(f"snapshot cross-table consistency: {is_consistent}")
        for k, v in detail.items():
            print(f"  {k}: {v}")
        conn.close()
        sys.exit(0 if is_consistent else 1)

    if args.check_staleness:
        is_fresh, fresh_detail = assert_snapshot_summary_fresh(conn, county_code=args.county)
        print(f"snapshot summary fresh (county_code={args.county!r}, swap-atomicity/provenance check): {is_fresh}")
        for k, v in fresh_detail.items():
            print(f"  {k}: {v}")
        print()
        is_consistent, consistency_detail = assert_snapshot_breakdown_totals_consistent(conn)
        print(f"snapshot cross-table consistency (data-correctness check): {is_consistent}")
        for k, v in consistency_detail.items():
            print(f"  {k}: {v}")
        conn.close()
        sys.exit(0 if (is_fresh and is_consistent) else 1)

    result = refresh_snapshot_summary(conn, batch_id=args.batch_id, dry_run=args.dry_run)
    conn.close()

    print(f"\n{'='*65}")
    print("  SUMMARY")
    print(f"{'='*65}")
    if result["dry_run"]:
        print(f"  [DRY RUN] {result['breakdown_row_count']:,} breakdown / "
              f"{result['totals_row_count']:,} totals / "
              f"{result['neighborhood_row_count']:,} neighborhood rows would be computed")
    else:
        print(f"  {result['breakdown_row_count']:,} breakdown / {result['totals_row_count']:,} totals / "
              f"{result['neighborhood_row_count']:,} neighborhood rows written, batch_id={result['batch_id']}")


if __name__ == "__main__":
    main()
