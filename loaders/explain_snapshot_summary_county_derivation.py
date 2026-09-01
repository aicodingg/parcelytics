#!/usr/bin/env python3
"""
loaders/explain_snapshot_summary_county_derivation.py — PX-20260831-02 Task 1,
live proof script for Diego.

Read-only. Issues ZERO writes and ZERO row-returning executions of the real
query bodies -- `EXPLAIN` alone (no `ANALYZE`) only asks Postgres to PLAN each
of the five refresh_snapshot_summary.py query builders' SQL for one
representative view (default: "overall", matching this task's own report),
never actually runs them. Every statement below is wrapped in a transaction
that is always ROLLBACK'd, never COMMIT'd -- belt-and-suspenders on top of
EXPLAIN's own no-execution guarantee, same posture as every other read-only
live-proof script in this repo (e.g. verify_index_coverage.py --index-source
live).

What this proves, and what it does NOT prove:
  - PROVES: each of the five builders' SQL, as it will actually run against
    real production statistics and indexes, produces a plan whose grouping
    step includes county_code as a group key -- the structural signature of
    "this query genuinely derives and carries county_code per row" rather
    than "this query still secretly computes one blended cross-county
    number." This is the live-Postgres-planner complement to
    test_refresh_snapshot_summary.py's own static source-text assertions
    (which check the SQL *strings* for "GROUP BY p.county_code" etc. without
    ever asking a real planner to look at them).
  - DOES NOT PROVE: that a real refresh_snapshot_summary.py run completes
    correctly end to end, that the row counts it would produce are sane, or
    that the post-refresh consistency assertion passes on real data -- see
    this task's final report for the separate real-refresh verification
    commands (--dry-run first, then a real run, both against a target Diego
    names explicitly).

Usage (per this repo's standing convention -- always /usr/bin/python3, run
from the repo root, DATABASE_URL already exported to point at the intended
target):

    /usr/bin/python3 loaders/explain_snapshot_summary_county_derivation.py
    /usr/bin/python3 loaders/explain_snapshot_summary_county_derivation.py --view retail
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.refresh_snapshot_summary import (
    breakdown_sql, single_year_mv_sql, part4_agg_sql, cert_agg_sql, neighborhoods_sql,
)
from snapshot_taxonomy import _SNAPSHOT_VALID_VIEWS

# (label, sql, needs_nestloop_off) -- needs_nestloop_off mirrors exactly what
# _compute_one_view() itself passes to each builder's _fetch() call
# (refresh_snapshot_summary.py: breakdown/part4/cert/neighborhoods all set
# nestloop_off=True; single_year_mv_sql does not) -- an EXPLAIN of the SAME
# plan shape production will actually use, not a hypothetical default-planner
# variant.
def _builders_for_view(view):
    return [
        ("breakdown_sql",      breakdown_sql(view),             True),
        ("single_year_mv_sql", single_year_mv_sql(view, 2025),  False),
        ("part4_agg_sql",      part4_agg_sql(view),             True),
        ("cert_agg_sql",       cert_agg_sql(view),               True),
        ("neighborhoods_sql",  neighborhoods_sql(view),          True),
    ]


def _plan_text(cur, sql, nestloop_off):
    """Runs EXPLAIN (COSTS OFF) on `sql` inside the caller's already-open
    transaction and returns the plan as one newline-joined string. COSTS OFF
    keeps the output deterministic/diffable across runs (no cost estimates
    that shift with autovacuum/ANALYZE timing) -- the group-key structural
    check below doesn't need cost numbers anyway."""
    if nestloop_off:
        cur.execute("SET LOCAL enable_nestloop = off")
    cur.execute(f"EXPLAIN (COSTS OFF) {sql}")
    return "\n".join(row[0] for row in cur.fetchall())


# Every line shape Postgres can render for an aggregate node's own grouping
# key(s):
#   - "Group Key:"  -- plain GROUP BY, or a GroupAggregate's single pass over
#                      one grouping set (also the shape a plain,
#                      non-GROUPING-SETS HashAggregate over one column uses).
#   - "Hash Key:"   -- one line PER GROUPING SET under a HashAggregate that's
#                      hashing all sets in a single pass.
#   - "Sort Key:"   -- one line per grouping set under a MixedAggregate, the
#                      planner's hybrid strategy that sorts some grouping
#                      sets and hashes others; the planner can choose this
#                      over a pure HashAggregate on a different stats/
#                      work_mem day, so a checker that only recognizes
#                      Hash Key would reopen the exact same class of blind
#                      spot this fix exists to close, just under a third
#                      node shape.
_GROUP_KEY_PREFIXES = ("Group Key:", "Hash Key:", "Sort Key:")


def _grouping_key_lines(plan_text):
    """Every grouping-key line belonging to an aggregate node in this plan
    (see _GROUP_KEY_PREFIXES above for the three shapes). A single plan can
    have more than one -- e.g. a HashAggregate over GROUPING SETS with two
    sets renders two separate "Hash Key:" lines, one per set -- and EVERY
    one of them describes a real, independently-computed slice of the
    aggregation's output rows."""
    return [line.strip() for line in plan_text.splitlines() if line.strip().startswith(_GROUP_KEY_PREFIXES)]


def _county_code_group_key_check(plan_text):
    """Structural check: does EVERY grouping-key line in this plan's
    aggregation step carry county_code? Returns (passed, offending_key)
    where offending_key is the first key line missing county_code (or None
    if passed).

    This is deliberately stricter than "at least one key line mentions
    county_code" (that was this function's original, buggy behavior): a
    query planned as GROUPING SETS ((county_code, ptype), (ptype)) would
    render two Hash Key lines -- one correctly carrying county_code, one
    NOT -- and every row produced under that second grouping set would
    blend every county together into one blended number. A checker that
    stops at the first matching line would miss that second, broken set
    entirely. Requiring ALL key lines to carry county_code is what actually
    proves the WHOLE aggregation is county-scoped, not just one pass of it.

    If the plan has no grouping-key lines at all, this returns
    (False, None) -- no evidence of any county_code grouping is exactly as
    much a FAIL as evidence of an ungrouped one.

    PX-20260831-02 history: v1 of this function only checked for
    "Group Key:" and produced a false FAIL against production for
    single_year_mv_sql('overall'), which the live planner executed as a
    top-level HashAggregate with "Hash Key: p.county_code, ..." /
    "Hash Key: p.county_code" lines (GROUPING SETS ((county_code, ptype),
    (county_code)) -- narrower than breakdown_sql's 3-column grouping sets,
    which the planner instead ran as GroupAggregate). v2 added Hash Key
    recognition but still passed on the FIRST matching line, which would
    have missed a genuinely-partial-scoping bug (one grouping set correct,
    another not). This version (v3) requires every key line to pass, and
    additionally recognizes "Sort Key:" for MixedAggregate, Postgres's
    hybrid strategy for grouping sets that sorts some and hashes others.
    The builder's own SQL text was never wrong in the incident that
    triggered this fix -- county_code was already present in both SELECT
    and every GROUPING SETS tuple (verified directly against
    refresh_snapshot_summary.py) -- this has always been a checker-side
    blind spot, not a builder bug.

    This is a plan-shape check, not a string-match against the SQL source
    (that's what test_refresh_snapshot_summary.py's static assertions
    already cover) -- it proves the PLANNER, not just the SQL text, treats
    county_code as part of every slice of the grouping."""
    keys = _grouping_key_lines(plan_text)
    if not keys:
        return False, None
    for key in keys:
        if "county_code" not in key:
            return False, key
    return True, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--view", default="overall",
                     help="Representative /snapshot view to EXPLAIN all five builders for "
                          "(default: overall). Any value from snapshot_taxonomy._SNAPSHOT_VALID_VIEWS.")
    args = ap.parse_args()

    if args.view not in _SNAPSHOT_VALID_VIEWS:
        print(f"FATAL: '{args.view}' is not a real /snapshot view. Valid: {sorted(_SNAPSHOT_VALID_VIEWS)}")
        return 1

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}\n"
          f"Representative view: {args.view!r}\n"
          f"Every statement below is EXPLAIN only (no ANALYZE, no execution) inside a "
          f"transaction that is always ROLLBACK'd -- zero writes, zero row-returning "
          f"execution of the real query bodies.\n")

    all_pass = True
    try:
        with conn.cursor() as cur:
            for label, sql, nestloop_off in _builders_for_view(args.view):
                plan = _plan_text(cur, sql, nestloop_off)
                passed, offending_key = _county_code_group_key_check(plan)
                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_pass = False
                print(f"── {label}({args.view!r}) ── [{status}: county_code in every Group/Hash/Sort Key]")
                if not passed and offending_key is not None:
                    print(f"    FAIL: grouping key line missing county_code: {offending_key!r}")
                elif not passed:
                    print("    FAIL: no Group Key / Hash Key / Sort Key line found at all -- "
                          "no evidence this aggregation is grouped on anything.")
                print(plan)
                print()
    finally:
        conn.rollback()  # belt-and-suspenders: EXPLAIN alone never executes, but never COMMIT regardless.

    print(f"{'ALL 5 PLANS' if all_pass else 'NOT ALL PLANS'} carry county_code as a group key "
          f"for view={args.view!r}.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
