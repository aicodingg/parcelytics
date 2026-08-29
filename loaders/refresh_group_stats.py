#!/usr/bin/env python3
"""
loaders/refresh_group_stats.py — Task AGGPRECOMP-1, Step 1 of
SPEC_AGGREGATE_PRECOMPUTATION.md ("Compute-at-Write, Serve-at-Read").

Builds group_stats: one row per distinct
(neighborhood_cd, state_cd1_class, classi_cd, tax_year) combination, holding
count/min/p25/median/p75/max for market_value, assessed_value, and
total_tax. This is the ONE place this aggregation logic lives, per the
spec's own principle ("aggregation logic lives only inside refresh
functions") — no live request handler should ever re-derive these numbers;
they should SELECT the row this script already computed.

Explicitly OUT OF SCOPE for this brief (see Task Log / AGGPRECOMP-1 brief):
  - Rewiring any existing endpoint (api_peer_set, api_peer_benchmark_local,
    county_snapshot) to actually READ from group_stats. That's Migration
    Order step 4 in the spec — a separate, later brief. This script only
    WRITES group_stats; nothing in the live app changes behavior because of
    this file existing.
  - Wiring this script into parcel_rollup.py / run_all.py's real call
    chain. This script is built standalone + dry-run-capable, with a
    batch_id parameter designed so a future pipeline caller can pass in a
    real batch id minted at actual load time (see "batch_id" note below) —
    but nothing calls this script automatically yet.

── Grain-key normalization ──────────────────────────────────────────────────
Matches EXISTING, already-indexed conventions in this codebase exactly
(investigated before writing this, per this task's brief) rather than
inventing new ones:
  neighborhood_cd_key = COALESCE(neighborhood_cd, '')
  state_cd1_class     = parcel_filters.state_cd1_class_sql()
                         -> LEFT(UPPER(COALESCE(state_cd1,'')), 1)
  classi_cd_key       = UPPER(TRIM(COALESCE(classi_cd, '')))
                         -> matches idx_parcel_use_code_exact's existing
                            UPPER(TRIM(classi_cd)) expression, api_peer_set's
                            own filter shape, made NULL-safe via COALESCE
                            the same way the other two grain keys are.
All three are COALESCE'd to '' because group_stats' primary key is a
composite NOT NULL key (see schema.sql) — an unknown neighborhood/class/
use-code is still a real, countable group, not a row this table can't
represent.

── total_tax metric: what it is and why total_tax_rate is NOT here ─────────
Investigated before deciding (per this task's brief, "state your reasoning
either way, don't assume without checking"):

  api_peer_benchmark_local's `_effective_tax()` (app.py, ~line 5435):
  tb.total_tax when it's a real nonzero figure, else tbe.entity_tax_sum
  (SUM(amount_due) from tax_billing_entity). This is a per-parcel DOLLAR
  total — the same kind of quantity as market_value/assessed_value, and
  exactly the metric the spec means by "total_tax or the entity-tax-sum
  equivalent."

  api_peer_set's `total_tax_rate` (app.py, ~line 5904): SUM(ctr.rate) from
  county_tax_rate, joined per taxing entity that bills this specific
  parcel. This is a SUM OF RATES, not a dollar total — a fundamentally
  different quantity driven by which entities happen to overlap a parcel's
  jurisdiction, not by the parcel's own value. Percentile-banding a rate
  answers a different question than percentile-banding a dollar figure, and
  the spec's own metric list never mentions rate percentiles.

  Decision: this table's tax metric is the EFFECTIVE TAX DOLLAR figure
  (total_tax_* columns below), computed with the exact same
  COALESCE(NULLIF(total_tax,0), entity_tax_sum) fallback already proven in
  api_peer_benchmark_local — NOT total_tax_rate. If a future brief wants
  group-level rate percentiles, that should be a deliberate, separate
  addition, not folded in here speculatively.

  count_total_tax is tracked SEPARATELY from count (the group's overall
  parcel count) because not every parcel has a resolvable effective tax
  figure — the same distinction api_peer_set's own `peer_tax_n` already
  draws from `n` (see app.py, ~line 5443).

── batch_id / staleness ─────────────────────────────────────────────────────
Investigated before writing this (per this task's brief): no real
per-load-run identifier exists anywhere in this codebase today
(ingest_audit.id is per-CHECK-ROW, not per-load-run; parcel_rollup.py's
run() and ingest_gate.py's gather_and_run() take no batch parameter). This
is a genuine, separate gap — closed here via a new, minimal load_batch
table (schema.sql), NOT by inventing an incompatible parallel identifier
scoped only to this script.

refresh_group_stats(conn, batch_id=...): if batch_id is given, tags the
refreshed rows with that EXISTING load_batch.batch_id — the shape a future
pipeline caller will use once parcel_rollup.py/run_all.py mints its own
batch row at real load time. If batch_id is None (this brief's only real
caller — standalone CLI use), this function mints its own fresh load_batch
row via _mint_batch().

HONEST DISCLOSURE: since nothing else currently writes to load_batch except
this script, assert_group_stats_fresh() will TRIVIALLY pass immediately
after any standalone run — there's no independent "a real load happened"
signal yet to go stale against. That's a correctly-disclosed limitation of
NOT being wired into the real load pipeline yet (explicitly out of scope
for this brief), not a flaw in the assertion's own logic. A genuine
staleness scenario (data loads, mints a new batch_id, but this refresh
hasn't run yet) only becomes representable once a later brief wires the
real load pipeline to mint load_batch rows independently of this script.

── Shadow-table-then-atomic-swap ───────────────────────────────────────────
Two separate phases, deliberately NOT combined into one long transaction:

  1. Build phase (can take minutes on real production-scale data, per the
     spec's own cost estimate): DROP + CREATE group_stats_shadow, then
     INSERT INTO it via one aggregation query. This touches ONLY the
     shadow table — the live group_stats table is fully untouched and
     fully readable by every live request for this entire phase.
  2. Swap phase: a single, separate, short transaction —
     ALTER TABLE group_stats RENAME TO group_stats_old;
     ALTER TABLE group_stats_shadow RENAME TO group_stats;
     DROP TABLE group_stats_old;
     COMMIT — three DDL statements, each metadata-only, so the ACCESS
     EXCLUSIVE lock window on the live group_stats name is milliseconds,
     not minutes. Postgres DDL is transactional: either all three succeed
     and commit together, or none do — readers only ever see the fully-old
     or fully-new table, never a half-refreshed one.

This two-phase split (vs. one giant transaction wrapping the whole build)
is a deliberate improvement over the naive reading of "build into a shadow
table, then swap" — it minimizes the real lock window on the table live
readers actually hit, which is the whole point of doing a shadow-swap
instead of an in-place rebuild in the first place.

IMPORTANT (sandbox-vs-live disclosure, same pattern as every other loader
this week): this sandbox has neither a live Postgres connection nor network
access to install one (confirmed: apt-get install postgresql fails with a
permission error — no root in this sandbox — and pip installs to PyPI are
blocked by the sandbox's network proxy, confirmed via a direct attempt).
The AGGREGATION QUERY's correctness (grouping keys, percentile definition,
effective-tax fallback priority) is verified here via a pure-Python
reference re-implementation of Postgres's PERCENTILE_CONT linear-
interpolation formula, exercised against small known-answer fixtures in
loaders/test_refresh_group_stats.py — this proves the LOGIC is right, but
does NOT prove the actual SQL string below executes correctly against real
Postgres, or that the shadow-swap is genuinely atomic under real concurrent
traffic. Diego needs to verify both live (see this task's final report for
exact commands), per this week's own hard-won lesson that reasoning alone
has twice been wrong this week even when it sounded sound.

Usage:
    cd ~/Desktop/Claude\\ Files/parcel_app
    python3 loaders/refresh_group_stats.py --dry-run       # compute + report row count, no writes
    python3 loaders/refresh_group_stats.py                 # real refresh, mints its own batch id
    python3 loaders/refresh_group_stats.py --check-staleness   # run the staleness assertion only
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parcel_filters import (
    state_cd1_class_sql,
    CANONICAL_PARCEL_EXCL_BARE,
    exclude_non_real_property_gap_sql,
)

# ── Grain-key expressions (module-level so tests can assert on them without
# re-typing the SQL) ─────────────────────────────────────────────────────────
NEIGHBORHOOD_CD_KEY_EXPR = "COALESCE(p.neighborhood_cd, '')"
STATE_CD1_CLASS_EXPR = state_cd1_class_sql("p.state_cd1")
CLASSI_CD_KEY_EXPR = "UPPER(TRIM(COALESCE(p.classi_cd, '')))"

# Real-property-only scoping (Task AGGPRECOMP-1-FIX, Aug 2026). Two parts,
# deliberately kept as two separate, independently-sourced fragments rather
# than merged into one new blob, so each keeps referencing its own single
# source of truth:
#   CANONICAL_PARCEL_EXCL_BARE       -- excludes 'X' (tax-exempt), 'N'
#       (personal property, 3 parcels), and AJR-prefixed synthetic BPP
#       placeholder geo_ids. Already correct, already tested
#       (verify_parcel_filters_coverage.py) -- reused as-is, not
#       reimplemented, closing group_stats' exposure to these same classes
#       for free (group_stats had NO exclusion at all before this fix).
#   exclude_non_real_property_gap_sql() -- excludes 'L' (Business Personal
#       Property, 42,563 real parcels) specifically -- the gap this task
#       exists to close. See parcel_filters.py's own comment above
#       NON_REAL_PROPERTY_GAP_CLASSES for the full investigation of why
#       this is separate from CANONICAL_PARCEL_EXCL rather than folded into
#       it.
# Applying BOTH here (not just the L-specific one) was a judgment call
# beyond this task's literal ask -- flagged explicitly in this task's final
# report, not silently expanded: group_stats had zero exclusion scoping
# before this fix, and it would leave a known, already-elsewhere-fixed
# contamination class (X-exempt, AJR-placeholder) unaddressed in the one
# new place that doesn't have it, for no added risk (CANONICAL_PARCEL_EXCL
# is reused unchanged, not modified).
REAL_PROPERTY_ONLY_WHERE = (
    f"({CANONICAL_PARCEL_EXCL_BARE}) AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"
)

# Effective-tax dollar figure, per api_peer_benchmark_local's own proven
# fallback (app.py ~line 5435-5440): tb.total_tax when real/nonzero, else
# tbe.entity_tax_sum. tbe_sum is aggregated ONCE, globally, grouped by
# (geo_id, tax_year) -- this is the one place a full-table GROUP BY over
# tax_billing_entity is the RIGHT shape (refresh-time, ~10x/year), unlike
# the live per-request case Task M6-PEER-QUERY-PERF fixed, where the same
# full-table aggregation was being redone on every web request.
REFRESH_GROUP_STATS_SQL = f"""
    WITH tbe_sum AS (
        SELECT geo_id, tax_year, SUM(amount_due) AS entity_tax_sum
        FROM   tax_billing_entity
        GROUP  BY geo_id, tax_year
    ),
    effective AS (
        SELECT
            p.county_code               AS county_code,
            {NEIGHBORHOOD_CD_KEY_EXPR}  AS neighborhood_cd_key,
            {STATE_CD1_CLASS_EXPR}      AS state_cd1_class,
            {CLASSI_CD_KEY_EXPR}        AS classi_cd_key,
            pty.tax_year                AS tax_year,
            pty.market_value            AS market_value,
            pty.assessed_value          AS assessed_value,
            COALESCE(NULLIF(tb.total_tax, 0), tbe.entity_tax_sum) AS effective_tax
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.county_code = p.county_code
        LEFT JOIN tax_billing tb  ON tb.geo_id  = p.geo_id AND tb.tax_year  = pty.tax_year AND tb.county_code  = p.county_code
        LEFT JOIN tbe_sum     tbe ON tbe.geo_id = p.geo_id AND tbe.tax_year = pty.tax_year AND tbe.county_code = p.county_code
        WHERE  pty.market_value > 0
          AND  {REAL_PROPERTY_ONLY_WHERE}
    )
    SELECT
        county_code,
        neighborhood_cd_key,
        state_cd1_class,
        classi_cd_key,
        tax_year,

        COUNT(*)                                                                        AS count,
        MIN(market_value)                                                               AS min_market_value,
        ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY market_value))::BIGINT       AS p25_market_value,
        ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY market_value))::BIGINT       AS median_market_value,
        ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY market_value))::BIGINT       AS p75_market_value,
        MAX(market_value)                                                               AS max_market_value,

        MIN(assessed_value)                                                             AS min_assessed_value,
        ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY assessed_value))::BIGINT     AS p25_assessed_value,
        ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY assessed_value))::BIGINT     AS median_assessed_value,
        ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY assessed_value))::BIGINT     AS p75_assessed_value,
        MAX(assessed_value)                                                             AS max_assessed_value,

        COUNT(effective_tax)                                                            AS count_total_tax,
        MIN(effective_tax)                                                              AS min_total_tax,
        ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY effective_tax)::NUMERIC, 2)  AS p25_total_tax,
        ROUND(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY effective_tax)::NUMERIC, 2)  AS median_total_tax,
        ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY effective_tax)::NUMERIC, 2)  AS p75_total_tax,
        MAX(effective_tax)                                                              AS max_total_tax
    FROM   effective
    GROUP  BY county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year
"""

# batch_id and refreshed_at are appended as literal SELECT-list columns at
# execution time (see _build_insert_sql() below) rather than baked into
# REFRESH_GROUP_STATS_SQL itself, so that constant stays a plain,
# independently-runnable read-only SELECT (used as-is for --dry-run and for
# ad hoc inspection) with no bound parameters of its own.


def _build_insert_sql():
    """
    INSERT INTO group_stats_shadow (...) SELECT ..., %(batch_id)s, NOW()
    FROM effective GROUP BY ... -- built by inserting two extra SELECT-list
    columns into REFRESH_GROUP_STATS_SQL's tail, rather than hand-
    duplicating the whole query a second time.

    county_code (PX-20260828-13, Stage 4 MISSING_TENANT_SCOPE follow-up,
    supersedes PARTITION-2-FIX-1's approach on this file): NO LONGER an
    externally-injected %(county_code)s literal stamped onto every row.
    That approach was itself the real, undisclosed bug Diego's Stage 4
    grouping report caught: REFRESH_GROUP_STATS_SQL's own FROM/JOIN chain
    had ZERO county_code filter, so it silently aggregated every county's
    parcels into the SAME percentile groups, then mislabeled the whole
    blended result with whatever single county_code the caller happened to
    pass -- a real, silent cross-county contamination risk the instant a
    second county's data exists, not just a cosmetic default-value gap.

    Fixed at the SOURCE: REFRESH_GROUP_STATS_SQL's `effective` CTE now
    selects `p.county_code` as a genuine column, carried through the GROUP
    BY, so each output row's county_code is DERIVED from that row's own
    parcels, not asserted from outside. One refresh run now correctly
    computes every county's group_stats simultaneously, in the same shadow-
    table build -- required by this table's full-table shadow-swap
    architecture (see module docstring): a naive `WHERE county_code = %s`
    added to a per-county-parameterized version of this script would have
    WIPED OUT every other county's rows on the next swap, since the swap
    replaces the whole table, not just one county's slice.

    county_code is therefore no longer a parameter build_shadow() threads
    in -- see build_shadow()'s own docstring for what changed on that side.
    """
    select_sql = REFRESH_GROUP_STATS_SQL.rstrip()
    # Insert the two extra SELECT-list expressions right before the
    # closing "FROM effective" of the final SELECT.
    marker = "    FROM   effective"
    assert marker in select_sql, "REFRESH_GROUP_STATS_SQL shape changed -- update _build_insert_sql()"
    head, tail = select_sql.split(marker, 1)
    head = head.rstrip()
    # Robust to whitespace/padding changes in the SELECT list above --
    # only asserts the SEMANTIC invariant (max_total_tax is the last
    # column before "FROM effective"), not exact column spacing.
    assert head.rstrip().endswith("AS max_total_tax"), \
        "REFRESH_GROUP_STATS_SQL's SELECT list changed shape -- update _build_insert_sql()"
    select_with_batch = (
        head + ",\n"
        "        %(batch_id)s::BIGINT                                                  AS source_import_batch_id,\n"
        "        NOW()                                                                  AS refreshed_at\n"
        + marker + tail
    )
    columns = """
        county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year,
        count, min_market_value, p25_market_value, median_market_value, p75_market_value, max_market_value,
        min_assessed_value, p25_assessed_value, median_assessed_value, p75_assessed_value, max_assessed_value,
        count_total_tax, min_total_tax, p25_total_tax, median_total_tax, p75_total_tax, max_total_tax,
        source_import_batch_id, refreshed_at
    """
    return f"INSERT INTO group_stats_shadow ({columns}) {select_with_batch}"


# PX-20260828-13: load_batch.county_code is NOT NULL with no default, but a
# refresh_group_stats.py run now covers EVERY county in one batch (see
# build_shadow()'s docstring) -- there is no longer one real county_code to
# stamp on this batch row. 'ALL' is a deliberate, documented sentinel, not a
# real county_code value -- it will never collide with a real registered
# county code (all of which are real county names/short-codes, e.g.
# 'TRAVIS', 'DALLAS'). Confirmed safe to introduce: grepped every live
# reader of load_batch.county_code before choosing this -- the only ones
# are this same file's _mint_batch() (writer) and assert_group_stats_fresh()
# (which reads group_stats.county_code, a real per-row value derived from
# parcel data, NOT load_batch.county_code -- the two columns serve
# different purposes and were never the same value in the first place).
# app.py's own only load_batch read (`SELECT MAX(batch_id) FROM load_batch`,
# ~line 4268) never touches county_code at all. Nothing live depends on
# load_batch.county_code meaning a real, single county.
GROUP_STATS_BATCH_COUNTY_SENTINEL = "ALL"


def _mint_batch(conn, note, county_code=GROUP_STATS_BATCH_COUNTY_SENTINEL):
    # county_code: see GROUP_STATS_BATCH_COUNTY_SENTINEL comment above for
    # why this is 'ALL', not a real county, as of PX-20260828-13.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO load_batch (note, county_code) VALUES (%s, %s) RETURNING batch_id",
            (note, county_code),
        )
        batch_id = cur.fetchone()[0]
    conn.commit()
    return batch_id


def build_shadow(conn, batch_id, verbose=True):
    """
    Phase 1: build group_stats_shadow fresh. Does NOT touch the live
    group_stats table at all -- safe to run while group_stats is being
    read by live traffic, however long it takes.

    county_code (PX-20260828-13): no longer a parameter here -- see
    _build_insert_sql()'s docstring for why. Every county's rows are built
    in this ONE pass, each stamped with ITS OWN real county_code (derived
    from parcel.county_code inside REFRESH_GROUP_STATS_SQL), not a single
    external value applied uniformly. This is required, not just simpler:
    group_stats_shadow is swapped in as a full-table replace (see module
    docstring's shadow-swap section) -- a per-county-scoped build would
    have to either wipe out every OTHER county's rows on swap, or abandon
    the shadow-swap pattern entirely for a slower in-place per-county
    delete+insert. Building every county at once, in one shadow table,
    keeps the atomic full-table swap intact and correct.
    """
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS group_stats_shadow")
        cur.execute("CREATE TABLE group_stats_shadow (LIKE group_stats INCLUDING ALL)")
        cur.execute(_build_insert_sql(), {"batch_id": batch_id})
        row_count = cur.rowcount
    conn.commit()
    _log(f"    group_stats_shadow built: {row_count:,} rows (all counties)  [{time.time()-t0:.1f}s]")
    return row_count


def swap_shadow_in(conn, verbose=True):
    """
    Phase 2: atomic swap. Three metadata-only DDL statements in ONE
    transaction -- either all three commit together or none do. This is
    the moment live readers ever see a change, and it's a near-instant
    ACCESS EXCLUSIVE lock on the table name, not the whole build.
    """
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE group_stats RENAME TO group_stats_old")
        cur.execute("ALTER TABLE group_stats_shadow RENAME TO group_stats")
        cur.execute("DROP TABLE group_stats_old")
    conn.commit()
    _log(f"    swap committed  [{time.time()-t0:.3f}s]")


def refresh_group_stats(conn, batch_id=None, dry_run=False, verbose=True):
    """
    Full refresh entry point.

    batch_id: pass an EXISTING load_batch.batch_id to tag this refresh with
        it -- the call shape a future pipeline caller (parcel_rollup.py /
        run_all.py, once it mints its own batch at real load time) will
        use. If None (this brief's standalone CLI usage), this function
        mints its own fresh load_batch row via _mint_batch() and uses that
        (tagged with GROUP_STATS_BATCH_COUNTY_SENTINEL -- see that
        constant's comment).

    dry_run: if True, runs the read-only aggregation SELECT (no
        %(batch_id)s / no shadow table / no load_batch row minted) and
        returns row count + a few sample groups, without writing anything
        -- same "--dry-run: parse/compute only, no writes" contract as
        every other loader touched this week.

    county_code (PX-20260828-13): REMOVED as a parameter here -- see
    build_shadow()'s docstring. This function now always refreshes every
    county's group_stats rows in one pass; there is no single county_code
    left to parameterize a "real refresh" run with (only
    assert_group_stats_fresh(), a genuinely different, still per-county
    check, keeps its own county_code parameter below).

    Returns a dict summary.
    """
    def _log(msg):
        if verbose:
            print(msg)

    if dry_run:
        t0 = time.time()
        with conn.cursor() as cur:
            cur.execute(REFRESH_GROUP_STATS_SQL)
            rows = cur.fetchall()
            colnames = [d[0] for d in cur.description]
        _log(f"[DRY RUN] {len(rows):,} group_stats rows would be computed  [{time.time()-t0:.1f}s]")
        sample = [dict(zip(colnames, r)) for r in rows[:5]]
        return {"dry_run": True, "row_count": len(rows), "sample": sample, "batch_id": None}

    used_batch_id = batch_id
    if used_batch_id is None:
        used_batch_id = _mint_batch(conn, note="refresh_group_stats.py standalone run")
        _log(f"  Minted new load_batch row: batch_id={used_batch_id} "
             f"(standalone mode -- no pipeline caller passed one in; "
             f"county_code='{GROUP_STATS_BATCH_COUNTY_SENTINEL}' -- this batch covers every county)")
    else:
        _log(f"  Using caller-supplied batch_id={used_batch_id}")

    row_count = build_shadow(conn, used_batch_id, verbose=verbose)
    swap_shadow_in(conn, verbose=verbose)

    return {"dry_run": False, "row_count": row_count, "batch_id": used_batch_id}


def assert_group_stats_fresh(conn, county_code="TRAVIS"):
    """
    Staleness assertion. Modeled on loaders/db.py's is_valid_tax_year() --
    a real, callable invariant, not a passive comment. Verifies every
    group_stats row FOR county_code's source_import_batch_id matches the
    latest known load_batch row.

    county_code (PARTITION-2-IMPLEMENT, Part 3, SPEC_COUNTY_PARTITIONING.md
    finding 9.7): scoped per county, not table-wide -- same real bug this
    fixes as app.py's _snapshot_summary_freshness() (see that function's
    own docstring for the full concrete scenario): once group_stats holds
    multiple counties' rows, an unscoped "SELECT DISTINCT
    source_import_batch_id FROM group_stats" would see >1 distinct batch_id
    the moment ANY two counties' rows were refreshed at different times --
    which, after county-scoped reloads (loaders/reload_county_scope.py,
    §9.2(c)) become the normal refresh mechanism, is the ORDINARY case, not
    a failure. That would make this assertion permanently, falsely report
    "not fresh" for the whole table forever. Scoping by county_code asks
    the honest, answerable question instead: is county_code's OWN data
    current.

    Defaults to 'TRAVIS' -- same single hardcoded seam as every other
    PARTITION-2-IMPLEMENT Part 3 call site (see app.py's
    _snapshot_summary_freshness() docstring for the full reasoning) --
    until Diego's real per-county operational tooling exists to pass a
    real value here.

    REAL DEPLOYMENT-SEQUENCING WARNING (same as app.py's twin function):
    this references a county_code column that does not exist on
    group_stats until migrate_county_partitioning.py's migration of that
    table has actually run. Do not deploy/run this against a database
    where that hasn't happened yet -- it will fail with "column
    county_code does not exist."

    Returns (is_fresh: bool, detail: dict) -- usable both as a standalone
    diagnostic (Diego can call this directly / via --check-staleness) and
    as something ingest_gate.py could plausibly call in the future (not
    wired in yet, per this brief's scope).

    HONEST LIMITATION (see module docstring): in this brief's standalone-
    only mode, this assertion will trivially PASS right after any refresh,
    since refresh_group_stats.py is currently the ONLY writer of
    load_batch. It only becomes a meaningful staleness check once a later
    brief wires the real load pipeline to mint load_batch rows
    independently of this script.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source_import_batch_id FROM group_stats WHERE county_code = %s",
            (county_code,),
        )
        batch_ids_in_table = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT MAX(batch_id) FROM load_batch")
        row = cur.fetchone()
        latest_batch_id = row[0] if row else None

    detail = {
        "county_code": county_code,
        "latest_batch_id": latest_batch_id,
        "batch_ids_in_group_stats": sorted(batch_ids_in_table),
    }

    if not batch_ids_in_table:
        detail["reason"] = f"group_stats has no rows for county_code={county_code!r} -- cannot be fresh (nothing to check)"
        return False, detail
    if len(batch_ids_in_table) > 1:
        detail["reason"] = (f"group_stats contains rows from more than one batch_id for "
                             f"county_code={county_code!r} -- a partial/failed refresh; should "
                             f"be impossible if the shadow-swap/county-scoped reload is "
                             f"genuinely atomic")
        return False, detail
    if latest_batch_id is None:
        detail["reason"] = "load_batch is empty -- no known batch to compare against"
        return False, detail

    table_batch_id = next(iter(batch_ids_in_table))
    if table_batch_id != latest_batch_id:
        detail["reason"] = (f"group_stats for county_code={county_code!r} reflects batch "
                             f"{table_batch_id}, but the latest known batch is "
                             f"{latest_batch_id} -- STALE")
        return False, detail

    detail["reason"] = f"group_stats for county_code={county_code!r} matches the latest known batch"
    return True, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="Compute + report row count only; no writes")
    ap.add_argument("--check-staleness", action="store_true", help="Run the staleness assertion only; no refresh")
    ap.add_argument("--batch-id", type=int, default=None,
                    help="Tag this refresh with an existing load_batch.batch_id "
                         "(future pipeline use; standalone runs normally omit this)")
    ap.add_argument("--county", default="TRAVIS",
                    help="county_code to check with --check-staleness ONLY (default: TRAVIS). "
                         "As of PX-20260828-13, a real refresh (no flag / --dry-run) always "
                         "computes EVERY county's group_stats rows in one pass -- county_code "
                         "is now derived per-row from parcel.county_code inside the aggregation "
                         "itself, not an external value you choose per run. This flag has no "
                         "effect on a real refresh or --dry-run; it only selects which county's "
                         "freshness --check-staleness reports on.")
    args = ap.parse_args()

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}  — confirm this is the environment you intend BEFORE any write commits.\n")

    if args.check_staleness:
        is_fresh, detail = assert_group_stats_fresh(conn, county_code=args.county)
        print(f"group_stats fresh (county_code={args.county!r}): {is_fresh}")
        for k, v in detail.items():
            print(f"  {k}: {v}")
        conn.close()
        sys.exit(0 if is_fresh else 1)

    result = refresh_group_stats(conn, batch_id=args.batch_id, dry_run=args.dry_run)
    conn.close()

    print(f"\n{'='*65}")
    print("  SUMMARY")
    print(f"{'='*65}")
    if result["dry_run"]:
        print(f"  [DRY RUN] {result['row_count']:,} group_stats rows would be computed")
    else:
        print(f"  {result['row_count']:,} group_stats rows written, batch_id={result['batch_id']}")


if __name__ == "__main__":
    main()
