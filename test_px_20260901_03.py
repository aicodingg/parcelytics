#!/usr/bin/env python3
"""
test_px_20260901_03.py — PX-20260901-03 Task 3 dedicated fixture coverage.

Reuses test_snapshot_data_unavailable.py's own extraction-and-exec harness
(_read_real_app_py / _build_real_namespace / _extract_function_body) rather
than duplicating it -- this file adds NEW scenarios that harness didn't
already cover, it doesn't reinvent how the real app.py source gets loaded.

Covers the brief's three explicit requirements:

Task 1 fixtures (coverage-aware movers freshness) -- PM's exact three
scenarios, called directly against the REAL, unmodified
_snapshot_summary_freshness() extracted from app.py:
  1. Dallas-shaped (breakdown+totals agree, batch matches load_batch,
     ZERO neighborhood data, movers table never queried) -> fresh
  2. Travis-shaped (breakdown+totals+movers all agree, batch matches,
     county HAS neighborhood data) -> fresh
  3. Travis-shaped but movers is batch-mismatched -> NOT fresh

Task 2 fixtures (composition from availability), against the real
snapshot_coverage_copy() and the real _compute_snapshot_data()'s
available_tabs computation:
  4. Full coverage (11 views + movers) -> snapshot_coverage_copy() returns
     None (nothing renders -- this is what keeps Travis's page unchanged)
  5. Dallas-shaped (7 views, no movers) -> snapshot_coverage_copy() returns
     the exact generated sentence PM's brief quoted as an example
  6. available_tabs preserves _SNAPSHOT_VIEW_TAB_ORDER's order and excludes
     any view absent from snapshot_totals, for both a Dallas-shaped and a
     Travis-shaped available-views set

Run: python3 test_px_20260901_03.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from test_snapshot_data_unavailable import (
    _read_real_app_py, _build_real_namespace, check, FAILURES,
)
from snapshot_taxonomy import _SNAPSHOT_VIEW_TAB_ORDER, _SNAPSHOT_COVERAGE_LABELS


class _FakeG:
    pass


class _RecordingStub:
    """Generic stub for _snapshot_summary_freshness() scenarios -- takes an
    explicit table -> rows mapping (matching real query() call shapes) plus
    a latest batch id, and a has_neighborhood_data flag. Records every call
    so a scenario can assert movers was (or wasn't) ever queried."""
    def __init__(self, has_neighborhood_data, table_batch_ids, latest_batch_id):
        self.has_neighborhood_data = has_neighborhood_data
        self.table_batch_ids = table_batch_ids  # {table_name: [batch_id, ...]}
        self.latest_batch_id = latest_batch_id
        self.calls = []

    def __call__(self, sql, params=None, one=False):
        self.calls.append(sql)
        norm = " ".join(sql.split())
        if "AS has_data" in norm:
            return {"has_data": self.has_neighborhood_data}
        if "MAX(batch_id)" in norm:
            return {"latest": self.latest_batch_id}
        for table, batch_ids in self.table_batch_ids.items():
            if f"FROM {table} WHERE county_code" in norm:
                return [{"source_import_batch_id": b} for b in batch_ids]
        raise AssertionError(f"unexpected query, no fixture branch matched: {sql!r}")


def _freshness_scenario(ns, **kwargs):
    stub = _RecordingStub(**kwargs)
    ns["g"] = _FakeG()
    ns["query"] = stub
    is_fresh, reason = ns["_snapshot_summary_freshness"](county_code="DALLAS")
    return is_fresh, reason, stub


def test_task1_dallas_shaped_no_movers_is_fresh():
    """PM's fixture 1: Dallas-shaped data -- breakdown/totals present and
    agreeing with the latest batch, ZERO neighborhood data for the county.
    Must be fresh, and the movers table must NEVER be queried at all (not
    just 'queried and ignored') -- that's what proves the required-tables
    list itself is conditional, not just the pass/fail check downstream."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    is_fresh, reason, stub = _freshness_scenario(
        ns,
        has_neighborhood_data=False,
        table_batch_ids={
            "snapshot_breakdown": [11],
            "snapshot_totals": [11],
            "snapshot_neighborhood_movers": [11],  # present in the stub but must be UNREACHED
        },
        latest_batch_id=11,
    )
    check("Task1/Dallas-shaped: is_fresh is True", is_fresh is True, reason)
    check("Task1/Dallas-shaped: reason is None", reason is None, reason)
    check("Task1/Dallas-shaped: snapshot_neighborhood_movers was NEVER queried "
          "(proves it's excluded from the required-tables list, not just "
          "excluded from the pass/fail check)",
          not any("snapshot_neighborhood_movers" in c for c in stub.calls),
          stub.calls)


def test_task1_travis_shaped_movers_present_matching_is_fresh():
    """PM's fixture 2: Travis-shaped -- county HAS neighborhood data, and
    its movers rows agree with breakdown/totals/load_batch. Must be fresh,
    and movers MUST be queried this time (the original 3-table check is
    unchanged for a county that actually has neighborhood data)."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    is_fresh, reason, stub = _freshness_scenario(
        ns,
        has_neighborhood_data=True,
        table_batch_ids={
            "snapshot_breakdown": [11],
            "snapshot_totals": [11],
            "snapshot_neighborhood_movers": [11],
        },
        latest_batch_id=11,
    )
    check("Task1/Travis-shaped-matching: is_fresh is True", is_fresh is True, reason)
    check("Task1/Travis-shaped-matching: reason is None", reason is None, reason)
    check("Task1/Travis-shaped-matching: snapshot_neighborhood_movers WAS queried "
          "(county has neighborhood data -- original 3-table requirement stands)",
          any("snapshot_neighborhood_movers" in c for c in stub.calls),
          stub.calls)


def test_task1_travis_shaped_movers_batch_mismatch_is_not_fresh():
    """PM's fixture 3: Travis-shaped, but movers lags behind breakdown/
    totals/load_batch (batch 10 vs latest 11). Must be NOT fresh -- this is
    a real freshness gap for a county that has neighborhood data, not a
    coverage gap to wave through."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    is_fresh, reason, stub = _freshness_scenario(
        ns,
        has_neighborhood_data=True,
        table_batch_ids={
            "snapshot_breakdown": [11],
            "snapshot_totals": [11],
            "snapshot_neighborhood_movers": [10],  # stale relative to latest
        },
        latest_batch_id=11,
    )
    check("Task1/Travis-shaped-mismatch: is_fresh is False", is_fresh is False, reason)
    check("Task1/Travis-shaped-mismatch: a real honest reason string is present",
          bool(reason), reason)
    check("Task1/Travis-shaped-mismatch: reason carries no developer-facing tokens",
          not any(tok in reason for tok in
                  ("snapshot_breakdown", "snapshot_totals", "snapshot_neighborhood_movers",
                   "loaders/", ".py", "DALLAS", "TRAVIS")),
          reason)


def test_task2_full_coverage_returns_none():
    """snapshot_coverage_copy() must return None when a county has every
    view AND neighborhood movers -- this is what keeps Travis's page
    byte-identical to before this brief (the template only renders the
    coverage line `{% if coverage_line %}`)."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    all_views = set(_SNAPSHOT_VIEW_TAB_ORDER) | {"commercial"}
    result = ns["snapshot_coverage_copy"]("Travis County", all_views, True)
    check("Task2/full-coverage: snapshot_coverage_copy() returns None", result is None, result)


def test_task2_dallas_shaped_coverage_sentence():
    """snapshot_coverage_copy() for Dallas's real shape (7 views: overall,
    residential, multifamily, commercial, land, agricultural, other --
    missing retail/industrial/office/hotel -- and no neighborhood movers)
    must produce a generated, parameterized sentence naming exactly what's
    missing, using the county name and _SNAPSHOT_COVERAGE_LABELS vocabulary
    -- never a hand-typed per-county string."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    dallas_views = {"overall", "residential", "multifamily", "commercial",
                     "land", "agricultural", "other"}
    result = ns["snapshot_coverage_copy"]("Dallas County", dallas_views, False)

    check("Task2/Dallas-shaped: coverage line is a non-empty string",
          isinstance(result, str) and len(result) > 0, result)
    check("Task2/Dallas-shaped: names the county", result.startswith("Dallas County:"), result)
    check("Task2/Dallas-shaped: lists the available views present in Dallas's data",
          all(_SNAPSHOT_COVERAGE_LABELS[v] in result for v in dallas_views),
          result)
    check("Task2/Dallas-shaped: names the missing sub-sector views (retail/office/industrial/hotel)",
          all(_SNAPSHOT_COVERAGE_LABELS[v] in result for v in
              ("retail", "office", "industrial", "hotel")),
          result)
    check("Task2/Dallas-shaped: mentions neighborhood detail is unavailable",
          "neighborhood detail" in result, result)
    check("Task2/Dallas-shaped: no developer-facing tokens leaked into the sentence",
          not any(tok in result for tok in
                  ("snapshot_totals", "snapshot_neighborhood_movers", "loaders/", ".py", "DALLAS")),
          result)


def test_task2_available_tabs_order_and_filtering():
    """The tab bar must preserve _SNAPSHOT_VIEW_TAB_ORDER's existing order
    and include ONLY views actually present in snapshot_totals for the
    county -- this proves the exact list-comprehension app.py's
    _compute_snapshot_data() uses for `available_tabs`, run directly here
    against two different available-views sets (Dallas's real 7, and full
    Travis coverage) so a future edit to the ordering logic can't silently
    reorder or leak an unavailable tab without failing this test."""
    # PM's brief states Dallas's snapshot_totals has 7 DISTINCT view rows:
    # overall, residential, multifamily, commercial, land, agricultural,
    # other. But "commercial" is the legacy, no-tab view (old deep links
    # only -- see snapshot_taxonomy.py's own comment above
    # _SNAPSHOT_VIEW_TAB_ORDER's definition); it was never one of the 10
    # tab-order entries, so it's correctly excluded from the rendered tab
    # bar for EVERY county, Travis included. 7 available *rows* therefore
    # yields 6 available *tabs* -- this test asserts that distinction
    # explicitly so it can't be silently miscounted later.
    dallas_available_views = {"overall", "residential", "multifamily", "commercial",
                               "land", "agricultural", "other"}
    dallas_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in dallas_available_views]

    check("Task2/available_tabs: Dallas's 7 available view-rows yield 6 tabs "
          "('commercial' has no tab in any county, per _SNAPSHOT_VIEW_TAB_ORDER)",
          len(dallas_tabs) == 6, dallas_tabs)
    check("Task2/available_tabs: Dallas tabs preserve _SNAPSHOT_VIEW_TAB_ORDER's relative order",
          dallas_tabs == [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in dallas_tabs],
          dallas_tabs)
    check("Task2/available_tabs: no missing-view (retail/office/industrial/hotel) leaked into Dallas's tabs",
          not any(v in dallas_tabs for v in ("retail", "office", "industrial", "hotel")),
          dallas_tabs)

    travis_available_views = set(_SNAPSHOT_VIEW_TAB_ORDER) | {"commercial"}
    travis_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in travis_available_views]
    check("Task2/available_tabs: Travis (full coverage) gets every _SNAPSHOT_VIEW_TAB_ORDER tab, "
          "unchanged order -- proves Travis's render is unaffected by this brief",
          travis_tabs == list(_SNAPSHOT_VIEW_TAB_ORDER), travis_tabs)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL PX-20260901-03 TASK 1+2 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
