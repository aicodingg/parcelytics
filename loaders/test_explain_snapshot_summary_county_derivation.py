#!/usr/bin/env python3
"""
loaders/test_explain_snapshot_summary_county_derivation.py — PX-20260831-02
delta fixture tests for _county_code_group_key_check() in
loaders/explain_snapshot_summary_county_derivation.py.

That script's checker function decides, from real Postgres EXPLAIN plan
text, whether an aggregation is genuinely scoped by county_code. Its own
live re-run against production is the actual proof Diego trusts (this repo
has no live Postgres or psycopg2 available in the sandbox -- see that
script's own module docstring) -- these fixtures instead prove the pure
plan-text-parsing LOGIC is correct, against synthetic plan text built to
match the exact node shapes Postgres is documented to emit, so a future
change to this function can be checked without a live database.

History this file guards against re-opening:
  v1: only recognized "Group Key:" lines -> false FAIL on single_year_mv_sql
      ('overall'), which planned as HashAggregate ("Hash Key:" lines only).
  v2: added "Hash Key:" recognition, but PASSED on the FIRST matching key
      line -- which would silently miss a plan with TWO grouping-set key
      lines where only one carries county_code (a genuinely-broken partial-
      scoping case, not a false alarm).
  v3 (this fixture set): requires EVERY Group Key / Hash Key / Sort Key line
      to carry county_code, and additionally recognizes "Sort Key:" for
      MixedAggregate (Postgres's hybrid strategy for grouping sets that
      sorts some sets and hashes others -- the planner can choose it over a
      pure HashAggregate depending on statistics/work_mem on a given run).

Run: python3 loaders/test_explain_snapshot_summary_county_derivation.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.explain_snapshot_summary_county_derivation import (
    _county_code_group_key_check, _grouping_key_lines,
)

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Fixture (3): existing plain Group Key shape, unchanged behavior ────────
# part4_agg_sql/cert_agg_sql/neighborhoods_sql's real live-observed PASS
# shape: a plain (non-GROUPING-SETS) GROUP BY renders as GroupAggregate with
# one "Group Key:" line.

def test_plain_group_key_with_county_code_passes():
    plan = """
Aggregate
  Group Key: p.county_code
  ->  Seq Scan on parcel p
"""
    passed, offending = _county_code_group_key_check(plan)
    check("plain Group Key carrying county_code: PASSES", passed is True)
    check("plain Group Key carrying county_code: no offending key reported", offending is None)


def test_finalize_partial_group_aggregate_with_county_code_passes():
    # breakdown_sql's real live-observed PASS shape: parallel
    # Finalize/Partial GroupAggregate, each with its own Group Key line.
    plan = """
Finalize GroupAggregate
  Group Key: p.county_code
  ->  Gather Merge
        ->  Partial GroupAggregate
              Group Key: p.county_code
              ->  Sort
"""
    passed, offending = _county_code_group_key_check(plan)
    check("Finalize/Partial GroupAggregate, both Group Key lines carry county_code: PASSES", passed is True)
    check("Finalize/Partial GroupAggregate: no offending key reported", offending is None)


# ── Fixture (2): MixedAggregate, Hash Key + Sort Key, all carrying
# county_code -- PASSES ───────────────────────────────────────────────────
# Per this brief's explicit instruction: Postgres's MixedAggregate strategy
# (grouping sets where some are hashed and some are sorted) can render both
# "Hash Key:" and "Sort Key:" lines on the same aggregate node. This proves
# the checker recognizes Sort Key as a real grouping-key line, not just
# Group Key / Hash Key -- a MixedAggregate plan the planner might choose on
# a different stats/work_mem day must not silently pass this checker's
# earlier v1/v2 blind spots under a THIRD node-shape name.

def test_mixed_aggregate_hash_and_sort_keys_all_carrying_county_code_passes():
    plan = """
MixedAggregate
  Hash Key: p.county_code, (CASE WHEN ... END)
  Sort Key: p.county_code
  ->  Sort
        Sort Key: p.county_code
        ->  Seq Scan on parcel p
"""
    passed, offending = _county_code_group_key_check(plan)
    check("MixedAggregate: Hash Key + Sort Key lines both carrying county_code: PASSES", passed is True)
    check("MixedAggregate: no offending key reported", offending is None)


# ── Fixture (1): GROUPING SETS ((county_code, ptype), (ptype)) rendered as
# two Hash Key lines, one WITHOUT county_code -- FAILS, naming that key ────
# This is the exact partial-scoping shape v2 of the checker would have
# missed: the first grouping set is correctly scoped, but the second
# (ptype-only) grouping set's rows would blend every county's data
# together. Requiring ALL Hash Key lines to carry county_code is what
# catches this; a checker that stops at the first match (v2's actual bug)
# would report this plan as PASS.

def test_grouping_sets_hash_key_missing_county_code_on_second_set_fails_naming_it():
    plan = """
HashAggregate
  Hash Key: p.county_code, (CASE WHEN ... END)
  Hash Key: (CASE WHEN ... END)
  ->  Hash Join
        Hash Cond: (t.geo_id = p.geo_id)
"""
    passed, offending = _county_code_group_key_check(plan)
    check("GROUPING SETS with one Hash Key missing county_code: correctly FAILS", passed is False)
    check("offending key is the SECOND Hash Key line (the one missing county_code), not the first",
          offending is not None and offending.startswith("Hash Key:") and "county_code" not in offending,
          offending)
    check("offending key is exactly the ptype-only grouping set's Hash Key line",
          offending == "Hash Key: (CASE WHEN ... END)", offending)


# ── Negative controls: genuinely no county_code grouping at all ───────────

def test_hash_aggregate_with_no_county_code_anywhere_fails():
    plan = """
HashAggregate
  Hash Key: (CASE WHEN ... END)
  ->  Seq Scan on parcel p
"""
    passed, offending = _county_code_group_key_check(plan)
    check("HashAggregate with zero county_code in any key: correctly still FAILS", passed is False)
    check("offending key reported is the (only) Hash Key line", offending == "Hash Key: (CASE WHEN ... END)")


def test_plan_with_no_grouping_key_lines_at_all_fails():
    plan = """
Seq Scan on parcel p
  Filter: (market_value > 0)
"""
    passed, offending = _county_code_group_key_check(plan)
    check("plan with no Group/Hash/Sort Key line at all: correctly FAILS (no evidence of any grouping)",
          passed is False)
    check("no offending key to report when there were no key lines at all", offending is None)


# ── _grouping_key_lines() itself: sanity on the extraction helper ─────────

def test_grouping_key_lines_extracts_all_three_shapes_in_one_plan():
    plan = """
MixedAggregate
  Hash Key: p.county_code
  Sort Key: p.county_code
  Group Key: ()
"""
    keys = _grouping_key_lines(plan)
    check("_grouping_key_lines finds all 3 lines (Hash/Sort/Group Key) in one plan",
          len(keys) == 3, keys)
    check("_grouping_key_lines preserves each line verbatim (stripped)",
          keys == ["Hash Key: p.county_code", "Sort Key: p.county_code", "Group Key: ()"], keys)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL EXPLAIN_SNAPSHOT_SUMMARY_COUNTY_DERIVATION FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
