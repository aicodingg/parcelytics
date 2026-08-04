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

# Same construction as app.py's _compute_snapshot_data() `canonical_excl`
# local variable (SNAPSHOT-CORRECTNESS-1, Aug 2026) -- module-level here
# since it doesn't depend on `view`, unlike view_where.
CANONICAL_EXCL = CANONICAL_PARCEL_EXCL + f" AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"


# ── Per-view SQL builders (byte-identical in shape to app.py's PRE-migration
# _compute_snapshot_data() query bodies -- only the view-dependent fragments
# come from snapshot_taxonomy.py instead of a local if/elif block) ─────────

def breakdown_sql(view):
    ptype_case, sort_case, _bench_labels, _order_by, _fallback = ptype_and_sort_case_for_view(view)
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
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
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY GROUPING SETS ((({ptype_case}), ({sort_case})), ())
    """


def single_year_mv_sql(view, year):
    ptype_case, _sort_case, _bench_labels, _order_by, _fallback = ptype_and_sort_case_for_view(view)
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            ({ptype_case})                                  AS ptype,
            ROUND(SUM(t.market_value)::NUMERIC / 1e9, 3)     AS total_mv_b
        FROM parcel p
        JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = {year}
        WHERE t.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
        GROUP BY GROUPING SETS ((({ptype_case})), ())
    """


def part4_agg_sql(view):
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            COUNT(*) FILTER (WHERE p.year_built >= 2025)              AS n_new_construction,
            COUNT(*) FILTER (WHERE pm.risk_large_value_jump = TRUE)   AS n_risk_flagged
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        LEFT JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
    """


def cert_agg_sql(view):
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            COUNT(*) FILTER (WHERE t26.data_source = 'preliminary') AS n_preliminary,
            COUNT(*)                                                AS n_total
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          {view_where}
    """


def neighborhoods_sql(view):
    view_where = _snapshot_view_where(view)
    return f"""
        SELECT
            p.neighborhood_cd,
            COUNT(*) AS n_parcels,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2) AS median_pct
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {CANONICAL_EXCL}
          AND p.neighborhood_cd IS NOT NULL AND p.neighborhood_cd != ''
          {view_where}
        GROUP BY p.neighborhood_cd
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

    Returns (rows, totals_row_or_None) -- `rows` are the per-ptype rows
    (UNCAPPED -- capping is read-time, not here), `totals_row_or_None` is
    the grand-total row's fields, or None if there is no total row (empty
    view/no qualifying parcels).
    """
    mv25_by_ptype = {r["ptype"]: r["total_mv_b"] for r in mv25_rows}
    mv26_by_ptype = {r["ptype"]: r["total_mv_b"] for r in mv26_rows}

    rows = [dict(r) for r in breakdown_rows if r["ptype"] is not None]
    for r in rows:
        r["total_mv25_b"] = mv25_by_ptype.get(r["ptype"], r["total_mv25_b"])
        r["total_mv26_b"] = mv26_by_ptype.get(r["ptype"], r["total_mv26_b"])

    total_row_raw = next((r for r in breakdown_rows if r["ptype"] is None), None)
    totals_row = None
    if total_row_raw:
        totals_row = {
            "n_total": total_row_raw["n_parcels"],
            "n_up": total_row_raw["n_up"],
            "n_down": total_row_raw["n_down"],
            "n_flat": total_row_raw["n_flat"],
            "total_mv25_b": mv25_by_ptype.get(None, total_row_raw["total_mv25_b"]),
            "total_mv26_b": mv26_by_ptype.get(None, total_row_raw["total_mv26_b"]),
            "median_pct": total_row_raw["median_pct"],
        }
    return rows, totals_row


def _mint_batch(conn, note):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO load_batch (note) VALUES (%s) RETURNING batch_id", (note,))
        batch_id = cur.fetchone()[0]
    conn.commit()
    return batch_id


def _compute_one_view(conn, view, verbose=True):
    """
    Runs all 5 query bodies for one view against a live conn, returns
    (breakdown_rows, totals_row_or_None, new_construction_count,
    risk_flagged_count, n_preliminary_2026, n_total_2026, neighborhood_rows).

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
    rows, totals_row = merge_breakdown_rows(breakdown_rows, mv25_rows, mv26_rows)

    new_construction_count = 0
    risk_flagged_count = 0
    n_preliminary_2026 = 0
    n_total_2026 = 0
    if totals_row:
        agg = _fetch(part4_agg_sql(view), nestloop_off=True, one=True)
        if agg:
            new_construction_count = int(agg["n_new_construction"] or 0)
            risk_flagged_count = int(agg["n_risk_flagged"] or 0)
        cert_agg = _fetch(cert_agg_sql(view), nestloop_off=True, one=True)
        if cert_agg and cert_agg["n_total"]:
            n_preliminary_2026 = int(cert_agg["n_preliminary"] or 0)
            n_total_2026 = int(cert_agg["n_total"])

    neighborhood_rows = []
    if totals_row:
        neighborhood_rows = _fetch(neighborhoods_sql(view), nestloop_off=True)

    return rows, totals_row, new_construction_count, risk_flagged_count, n_preliminary_2026, n_total_2026, neighborhood_rows


def build_shadow(conn, batch_id, verbose=True):
    """
    Phase 1: build all three shadow tables fresh, across all 11 views. Does
    NOT touch the live snapshot_breakdown/snapshot_totals/
    snapshot_neighborhood_movers tables at all -- safe to run while those are
    being read by live traffic, however long it takes.
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
        (rows, totals_row, new_construction_count, risk_flagged_count,
         n_preliminary_2026, n_total_2026, neighborhood_rows) = _compute_one_view(conn, view, verbose=verbose)

        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO snapshot_breakdown_shadow
                        (view, ptype, sort_key, n_parcels, n_up, n_down, n_flat,
                         median_pct, p25_pct, p75_pct, total_mv25_b, total_mv26_b,
                         source_import_batch_id, refreshed_at)
                    VALUES (%(view)s, %(ptype)s, %(sort_key)s, %(n_parcels)s, %(n_up)s,
                            %(n_down)s, %(n_flat)s, %(median_pct)s, %(p25_pct)s, %(p75_pct)s,
                            %(total_mv25_b)s, %(total_mv26_b)s, %(batch_id)s, NOW())
                    """,
                    {
                        "view": view, "ptype": r["ptype"], "sort_key": str(r["sort_key"]) if r["sort_key"] is not None else None,
                        "n_parcels": r["n_parcels"], "n_up": r["n_up"], "n_down": r["n_down"], "n_flat": r["n_flat"],
                        "median_pct": r["median_pct"], "p25_pct": r["p25_pct"], "p75_pct": r["p75_pct"],
                        "total_mv25_b": r["total_mv25_b"], "total_mv26_b": r["total_mv26_b"],
                        "batch_id": batch_id,
                    },
                )
                breakdown_row_count += 1

            if totals_row:
                cur.execute(
                    """
                    INSERT INTO snapshot_totals_shadow
                        (view, n_total, n_up, n_down, n_flat, median_pct, total_mv25_b, total_mv26_b,
                         new_construction_count, risk_flagged_count, n_preliminary_2026, n_total_2026,
                         source_import_batch_id, refreshed_at)
                    VALUES (%(view)s, %(n_total)s, %(n_up)s, %(n_down)s, %(n_flat)s, %(median_pct)s,
                            %(total_mv25_b)s, %(total_mv26_b)s, %(new_construction_count)s,
                            %(risk_flagged_count)s, %(n_preliminary_2026)s, %(n_total_2026)s,
                            %(batch_id)s, NOW())
                    """,
                    {
                        "view": view, "n_total": totals_row["n_total"], "n_up": totals_row["n_up"],
                        "n_down": totals_row["n_down"], "n_flat": totals_row["n_flat"],
                        "median_pct": totals_row["median_pct"], "total_mv25_b": totals_row["total_mv25_b"],
                        "total_mv26_b": totals_row["total_mv26_b"],
                        "new_construction_count": new_construction_count, "risk_flagged_count": risk_flagged_count,
                        "n_preliminary_2026": n_preliminary_2026, "n_total_2026": n_total_2026,
                        "batch_id": batch_id,
                    },
                )
                totals_row_count += 1

            for nb in neighborhood_rows:
                cur.execute(
                    """
                    INSERT INTO snapshot_neighborhood_movers_shadow
                        (view, neighborhood_cd, n_parcels, median_pct, source_import_batch_id, refreshed_at)
                    VALUES (%(view)s, %(neighborhood_cd)s, %(n_parcels)s, %(median_pct)s, %(batch_id)s, NOW())
                    """,
                    {
                        "view": view, "neighborhood_cd": nb["neighborhood_cd"],
                        "n_parcels": nb["n_parcels"], "median_pct": nb["median_pct"],
                        "batch_id": batch_id,
                    },
                )
                nb_row_count += 1

        _log(f"    view={view:14s} breakdown={len(rows):4d} rows  "
             f"totals={'yes' if totals_row else 'no ':3s}  neighborhoods={len(neighborhood_rows):4d} rows")

    conn.commit()
    _log(f"    shadow tables built: {breakdown_row_count:,} breakdown / {totals_row_count:,} totals / "
         f"{nb_row_count:,} neighborhood rows  [{time.time()-t0:.1f}s]")
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
    refresh_group_stats.refresh_group_stats().
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
            (rows, totals_row, *_rest, neighborhood_rows) = _compute_one_view(conn, view, verbose=False)
            total_breakdown += len(rows)
            total_totals += 1 if totals_row else 0
            total_nb += len(neighborhood_rows)
            if sample is None and rows:
                sample = {"view": view, "rows": rows[:3]}
        _log(f"[DRY RUN] {total_breakdown:,} breakdown rows / {total_totals:,} totals rows / "
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
             f"(standalone mode -- no pipeline caller passed one in)")
    else:
        _log(f"  Using caller-supplied batch_id={used_batch_id}")

    breakdown_row_count, totals_row_count, nb_row_count = build_shadow(conn, used_batch_id, verbose=verbose)
    swap_shadow_in(conn, verbose=verbose)

    return {
        "dry_run": False, "breakdown_row_count": breakdown_row_count,
        "totals_row_count": totals_row_count, "neighborhood_row_count": nb_row_count,
        "batch_id": used_batch_id,
    }


def assert_snapshot_summary_fresh(conn):
    """
    Staleness assertion across all three Tier 1 tables. Modeled directly on
    refresh_group_stats.assert_group_stats_fresh() -- extended to require
    all three tables agree with EACH OTHER as well as with the latest
    load_batch, since a genuinely atomic swap (see swap_shadow_in() above)
    means they can never legitimately disagree; if they do, that's proof the
    swap was NOT atomic (a real bug), not just staleness.

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
            cur.execute(f"SELECT DISTINCT source_import_batch_id FROM {tbl}")
            batch_ids_by_table[tbl] = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT MAX(batch_id) FROM load_batch")
        row = cur.fetchone()
        latest_batch_id = row[0] if row else None

    detail = {
        "latest_batch_id": latest_batch_id,
        "batch_ids_by_table": {k: sorted(v) for k, v in batch_ids_by_table.items()},
    }

    for tbl in tables:
        if not batch_ids_by_table[tbl]:
            detail["reason"] = f"{tbl} is empty -- cannot be fresh (nothing to check)"
            return False, detail
        if len(batch_ids_by_table[tbl]) > 1:
            detail["reason"] = (f"{tbl} contains rows from more than one batch_id -- "
                                 f"a partial/failed refresh; should be impossible if the "
                                 f"shadow-swap is genuinely atomic")
            return False, detail

    if latest_batch_id is None:
        detail["reason"] = "load_batch is empty -- no known batch to compare against"
        return False, detail

    table_batch_ids = {tbl: next(iter(batch_ids_by_table[tbl])) for tbl in tables}
    distinct_table_batches = set(table_batch_ids.values())
    if len(distinct_table_batches) > 1:
        detail["reason"] = (f"the three Tier 1 tables disagree with each other on "
                             f"source_import_batch_id ({table_batch_ids}) -- the atomic "
                             f"swap did not actually keep them in sync; this should be "
                             f"impossible and indicates a real bug, not ordinary staleness")
        return False, detail

    common_batch_id = next(iter(distinct_table_batches))
    if common_batch_id != latest_batch_id:
        detail["reason"] = (f"Tier 1 tables reflect batch {common_batch_id}, but the latest "
                             f"known batch is {latest_batch_id} -- STALE")
        return False, detail

    detail["reason"] = "all three Tier 1 tables match the latest known batch"
    return True, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Compute + report row counts only; no writes")
    ap.add_argument("--check-staleness", action="store_true", help="Run the staleness assertion only; no refresh")
    ap.add_argument("--batch-id", type=int, default=None,
                    help="Tag this refresh with an existing load_batch.batch_id "
                         "(future pipeline use; standalone runs normally omit this)")
    args = ap.parse_args()

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}  — confirm this is the environment you intend BEFORE any write commits.\n")

    if args.check_staleness:
        is_fresh, detail = assert_snapshot_summary_fresh(conn)
        print(f"snapshot summary fresh: {is_fresh}")
        for k, v in detail.items():
            print(f"  {k}: {v}")
        conn.close()
        sys.exit(0 if is_fresh else 1)

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
