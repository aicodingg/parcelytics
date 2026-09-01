#!/usr/bin/env python3
"""
test_snapshot_data_unavailable.py — AGGPRECOMP-2-FIX-2, Evidence checklist
item 3: "Proof the 'data unavailable' branch actually triggers ... a real
test showing the /snapshot route renders the honest unavailable state, not
a fallback."

verify_m4_part1_other_pages_render.py already proves templates/snapshot.html
renders correctly GIVEN data_unavailable=True/False as hand-set context
variables -- but that only proves the TEMPLATE is correct, not that the
real Python logic in app.py's _snapshot_summary_freshness() /
_compute_snapshot_data() actually PRODUCES data_unavailable=True under a
real stale/missing-table condition. This file closes that gap.

Same technique test_snapshot_correctness_1.py already established
(_extract_function_body(): regex from `\ndef {name}(` to the next top-level
`\ndef ` or `@app.route` line) -- extended here one step further: instead
of just grep-checking the extracted text for a substring, this actually
EXECs the real, unmodified extracted source into a controlled namespace
(with a stub query() standing in for the real DB call, plus the real,
unmodified snapshot_taxonomy.py imports and the real, unmodified
_cap_subtype_rows()/SNAPSHOT_SUBTYPE_CAP also extracted from app.py) and
CALLS the real _compute_snapshot_data() function object with that stub.
This proves the actual, real app.py source code -- not a re-typed copy of
it -- produces data_unavailable=True with a real reason string when the
freshness gate fails, and data_unavailable=False with real row data when it
doesn't. Flask/psycopg2 are not importable in this sandbox (confirmed:
`python3 -c "import flask"` / `import psycopg2` both ModuleNotFoundError),
so app.py cannot be imported directly -- this extraction-and-exec technique
is what makes testing the REAL function possible without either dependency.

Run: python3 test_snapshot_data_unavailable.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from snapshot_taxonomy import (
    ptype_and_sort_case_for_view, _SNAPSHOT_SECTOR_VIEWS,
    _SNAPSHOT_VIEW_TAB_ORDER, _SNAPSHOT_TAB_BUTTON_LABEL, _SNAPSHOT_COVERAGE_LABELS,
)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


REPO_ROOT = os.path.dirname(__file__)


def _read_real_app_py():
    with open(os.path.join(REPO_ROOT, "app.py"), encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_function_body(source, func_name):
    """Identical technique to test_snapshot_correctness_1.py's own helper of
    the same name."""
    m = re.search(rf"\ndef {re.escape(func_name)}\(", source)
    if not m:
        return None
    start = m.start()
    rest = source[start + 1:]
    end_m = re.search(r"\n(def |@app\.route)", rest)
    end = start + 1 + end_m.start() if end_m else len(source)
    return source[start:end]


def _extract_dict_literal(source, name):
    """PX-20260830-04 Task 1: same extraction technique as
    _extract_function_body(), for a top-level `NAME = { ... }` dict literal
    instead of a `def`. app.py's own style always closes a top-level dict
    literal with a bare "}" at column 0 on its own line (confirmed against
    COUNTY_PROFILES specifically) -- that's the boundary used here."""
    m = re.search(rf"\n{re.escape(name)} = \{{", source)
    if not m:
        return None
    start = m.start() + 1
    end_m = re.search(r"\n\}\n", source[start:])
    if not end_m:
        return None
    end = start + end_m.start() + 2
    return source[start:end]


def _build_real_namespace(source):
    """Extracts the REAL, unmodified source of _snapshot_summary_freshness()
    and _compute_snapshot_data() from app.py and execs it into a fresh
    namespace alongside the real snapshot_taxonomy imports and a stub
    query(). Returns that namespace so tests can call the real function
    objects directly.

    _cap_subtype_rows() is deliberately NOT extracted/execed here -- it's
    only reachable for the 10 sector/commercial views (see
    _compute_snapshot_data()'s `if sector_or_commercial:` guard), and every
    scenario this file exercises uses view="overall" or returns via the
    freshness gate before that guard is ever reached, so it's genuinely
    unused here. (It's also immediately followed in app.py by top-level
    module init code -- Sentry setup referencing `config` -- rather than
    another `def `/`@app.route` boundary, which the shared
    _extract_function_body() heuristic doesn't stop at; extracting it would
    need a smarter boundary detector for no benefit to what this file
    actually proves.)"""
    freshness_src = _extract_function_body(source, "_snapshot_summary_freshness")
    compute_src = _extract_function_body(source, "_compute_snapshot_data")
    # PX-20260901-03 Task 1/2: _snapshot_summary_freshness() and
    # _compute_snapshot_data() now both call two more real app.py functions
    # -- _county_has_neighborhood_data() (Task 1's coverage-derived movers
    # requirement) and snapshot_coverage_copy() (Task 2's coverage line).
    # Both must be extracted and execed here too, or the real extracted
    # function bodies above raise NameError the moment they're called.
    neighborhood_data_src = _extract_function_body(source, "_county_has_neighborhood_data")
    assert neighborhood_data_src, "_county_has_neighborhood_data() not found in app.py -- extraction broke"
    # snapshot_coverage_copy() is immediately followed by
    # "@app.context_processor" (same shape as unavailable_copy() right
    # above it) -- the shared _extract_function_body() boundary regex only
    # stops at "\ndef " or "@app\.route", so it would silently over-capture
    # into _inject_county_helpers()'s own @app.context_processor decorator
    # line, which raises NameError on exec (no real `app` object in this
    # stub namespace). Same manual widened-boundary technique as
    # unavailable_copy_src above.
    _scc_m = re.search(r"\ndef snapshot_coverage_copy\(", source)
    assert _scc_m, "snapshot_coverage_copy() not found in app.py -- extraction broke"
    _scc_start = _scc_m.start() + 1
    _scc_end_m = re.search(r"\n(def |@app\.)", source[_scc_start + 1:])
    coverage_copy_src = source[_scc_start:_scc_start + 1 + _scc_end_m.start()] if _scc_end_m else source[_scc_start:]
    # unavailable_copy() is immediately followed by "@app.context_processor"
    # (decorating _inject_county_helpers() right after it) -- the shared
    # _extract_function_body() boundary regex only stops at "\ndef " or
    # "@app.route", so it would silently over-capture past unavailable_copy()
    # through that decorator line. Widened locally to also stop at any
    # "@app." decorator, rather than loosening the shared helper (used
    # identically, and correctly, elsewhere for def-only boundaries).
    m = re.search(r"\ndef unavailable_copy\(", source)
    assert m, "unavailable_copy() not found in app.py -- extraction broke"
    _start = m.start() + 1
    _end_m = re.search(r"\n(def |@app\.)", source[_start + 1:])
    unavailable_copy_src = source[_start:_start + 1 + _end_m.start()] if _end_m else source[_start:]
    county_profiles_src = _extract_dict_literal(source, "COUNTY_PROFILES")
    assert freshness_src, "_snapshot_summary_freshness() not found in app.py -- extraction broke"
    assert compute_src, "_compute_snapshot_data() not found in app.py -- extraction broke"
    # PX-20260830-04 Task 1: _snapshot_summary_freshness() now calls the
    # real unavailable_copy() (COUNTY_PROFILES lookup + parameterized
    # sentence) instead of building its own raw f-string -- both must be
    # extracted and execed into this namespace too, or every real call
    # inside the extracted function body raises NameError. This proves the
    # REAL production wiring (not a stub) actually produces the honest
    # copy end-to-end.
    assert unavailable_copy_src, "unavailable_copy() not found in app.py -- extraction broke"
    assert county_profiles_src, "COUNTY_PROFILES not found in app.py -- extraction broke"

    # PX-20260901-03 Task 1: _county_has_neighborhood_data() reads/writes
    # flask.g (a per-request cache) -- a plain object stands in fine here,
    # since getattr(g, ..., None) / g.attr = ... don't need a real Flask
    # app context, just something that supports arbitrary attributes.
    class _FakeG:
        pass

    namespace = {
        "ptype_and_sort_case_for_view": ptype_and_sort_case_for_view,
        "_SNAPSHOT_SECTOR_VIEWS": _SNAPSHOT_SECTOR_VIEWS,
        "_SNAPSHOT_VIEW_TAB_ORDER": _SNAPSHOT_VIEW_TAB_ORDER,
        "_SNAPSHOT_TAB_BUTTON_LABEL": _SNAPSHOT_TAB_BUTTON_LABEL,
        "_SNAPSHOT_COVERAGE_LABELS": _SNAPSHOT_COVERAGE_LABELS,
        "SNAPSHOT_SUBTYPE_CAP": 7,
        "g": _FakeG(),
        "query": None,  # set per-scenario by the caller before invoking
    }
    exec(compile(county_profiles_src, "<extracted COUNTY_PROFILES>", "exec"), namespace)
    exec(compile(unavailable_copy_src, "<extracted unavailable_copy>", "exec"), namespace)
    exec(compile(coverage_copy_src, "<extracted snapshot_coverage_copy>", "exec"), namespace)
    exec(compile(neighborhood_data_src, "<extracted _county_has_neighborhood_data>", "exec"), namespace)
    exec(compile(freshness_src, "<extracted _snapshot_summary_freshness>", "exec"), namespace)
    exec(compile(compute_src, "<extracted _compute_snapshot_data>", "exec"), namespace)
    return namespace


class _StaleQueryStub:
    """Simulates the real DB shape for a STALE scenario: snapshot_breakdown
    reflects an older batch (3) than the latest known load_batch (5) --
    exactly _snapshot_summary_freshness()'s 4th real failure branch (the
    same one confirmed live in loaders/refresh_snapshot_summary.py's own
    fixture tests). All three Tier-1 tables agree with EACH OTHER (batch 3)
    but lag the latest batch -- ordinary staleness, not corruption."""
    def __init__(self, table_batch_id=3, latest_batch_id=5):
        self.table_batch_id = table_batch_id
        self.latest_batch_id = latest_batch_id
        self.calls = []

    def __call__(self, sql, params=None, one=False):
        self.calls.append(sql)
        norm = " ".join(sql.split())
        # PX-20260901-03 Task 1: _county_has_neighborhood_data()'s coverage
        # check now runs FIRST, as part of the freshness gate itself (it
        # decides whether snapshot_neighborhood_movers is even a required
        # table). Travis has real neighborhood data, so this stays True --
        # movers remains a required table for this stub, matching this
        # scenario's pre-existing 3-table-required behavior exactly.
        if "AS has_data" in norm:
            return {"has_data": True}
        if "source_import_batch_id FROM" in norm:
            return [{"source_import_batch_id": self.table_batch_id}]
        if "MAX(batch_id)" in norm:
            return {"latest": self.latest_batch_id}
        raise AssertionError(
            f"stale scenario: _compute_snapshot_data() issued a query PAST the "
            f"freshness gate -- it should have returned immediately with "
            f"data_unavailable=True and never reached here. SQL: {sql!r}"
        )


class _FreshQueryStub:
    """Simulates a real, fresh, fully-populated scenario for view='overall'
    -- both tables agree with each other and match the latest batch (5),
    plus real breakdown/totals/neighborhood rows so _compute_snapshot_data()
    can be proven to ALSO return real data (not just the unavailable
    branch) when the gate passes, matching production shape."""
    def __init__(self):
        self.calls = []

    def __call__(self, sql, params=None, one=False):
        self.calls.append(sql)
        norm = " ".join(sql.split())
        if "source_import_batch_id FROM" in norm:
            return [{"source_import_batch_id": 5}]
        if "MAX(batch_id)" in norm:
            return {"latest": 5}
        # PX-20260901-03: Travis has real neighborhood-code data -- matches
        # this stub's overall "fully-populated, full-coverage" shape.
        if "AS has_data" in norm:
            return {"has_data": True}
        # PX-20260901-03: checked BEFORE the generic "FROM snapshot_totals"
        # branch below since both share that substring -- this is the new
        # DISTINCT-view availability query, which returns a LIST of view
        # rows, not the single per-view totals dict the generic branch
        # returns. Travis has full coverage: all 10 tab-order views plus
        # the legacy "commercial" bucket (PM's evidence: "Travis: 11").
        if "SELECT DISTINCT view FROM snapshot_totals" in norm:
            return [{"view": v} for v in _SNAPSHOT_VIEW_TAB_ORDER] + [{"view": "commercial"}]
        if "FROM snapshot_breakdown" in norm:
            return [
                {"ptype": "Residential", "sort_key": "1", "n_parcels": 900, "n_up": 500, "n_down": 300,
                 "n_flat": 100, "median_pct": 5.1, "p25_pct": 2.0, "p75_pct": 8.0,
                 "total_mv25_b": 150.0, "total_mv26_b": 158.0},
            ]
        if "FROM snapshot_totals" in norm:
            return {"n_total": 900, "n_up": 500, "n_down": 300, "n_flat": 100, "median_pct": 5.1,
                    "total_mv25_b": 150.0, "total_mv26_b": 158.0, "new_construction_count": 12,
                    "risk_flagged_count": 3, "n_preliminary_2026": 0, "n_total_2026": 900}
        if "FROM county_benchmark" in norm:
            return []
        if "FROM snapshot_neighborhood_movers" in norm:
            return [{"neighborhood_cd": "NB1", "n_parcels": 15, "median_pct": 6.0}]
        raise AssertionError(f"fresh scenario: unexpected query not covered by stub: {sql!r}")


def test_real_compute_snapshot_data_returns_data_unavailable_when_stale():
    """THE core proof for evidence-checklist item 3: the REAL, extracted
    _compute_snapshot_data() source from app.py, called for view='overall'
    against a stale-data stub, returns the honest unavailable shape -- not
    a live-fallback recompute, not a crash, not a half-populated dict."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    stub = _StaleQueryStub(table_batch_id=3, latest_batch_id=5)
    ns["query"] = stub

    # DALLAS-GATE-1 Part 2: _compute_snapshot_data() now takes county_code
    # as a real 2nd param (no longer a hardcoded "TRAVIS" literal inside the
    # function) -- test call sites updated to match the real new signature.
    result = ns["_compute_snapshot_data"]("overall", "TRAVIS")

    check("stale scenario: data_unavailable is True", result["data_unavailable"] is True, result)
    check("stale scenario: a real, honest reason string is present",
          bool(result.get("data_unavailable_reason")), result.get("data_unavailable_reason"))
    # PX-20260830-04 Task 1: Diego's standing ruling -- user-facing
    # data_unavailable_reason strings carry NO table names, file paths,
    # function names, or internal county codes. This used to assert the
    # OPPOSITE (that "refresh_snapshot_summary.py" WAS present) -- that
    # assertion was itself testing for the developer-facing bug this brief
    # was required to remove. Replaced with the real denylist check plus a
    # positive check that the new parameterized copy actually landed.
    check("stale scenario: reason contains NO developer-facing tokens "
          "(file paths, table names, raw county code)",
          not any(tok in result["data_unavailable_reason"] for tok in
                  ("refresh_snapshot_summary.py", "snapshot_breakdown", "snapshot_totals",
                   "snapshot_neighborhood_movers", "loaders/", ".py", "TRAVIS", "DALLAS")),
          result["data_unavailable_reason"])
    check("stale scenario: reason uses the real county name from COUNTY_PROFILES "
          "('Travis County'), not the raw code",
          "Travis County" in result["data_unavailable_reason"], result["data_unavailable_reason"])
    check("stale scenario: reason follows the 'being prepared' honest shape "
          "(parcel/appraisal data live, summary view not ready)",
          "is being prepared" in result["data_unavailable_reason"]
          and "will be available soon" in result["data_unavailable_reason"],
          result["data_unavailable_reason"])
    check("stale scenario: rows is the empty-list placeholder, not a partial/crashed result",
          result["rows"] == [], result["rows"])
    check("stale scenario: totals is None", result["totals"] is None, result)
    check("stale scenario: bench_trends is empty (no live fallback query attempted)",
          result["bench_trends"] == [], result)
    check("stale scenario: status_2026 defaults to 'none'", result["status_2026"] == "none", result)
    check("stale scenario: NEVER issued a query past the freshness gate (proves no live fallback)",
          # PX-20260901-03: _county_has_neighborhood_data()'s "AS has_data"
          # check is now PART OF the freshness gate itself (it runs first,
          # to decide whether snapshot_neighborhood_movers even belongs in
          # the required-tables list) -- not a live-fallback query issued
          # AFTER the gate has already failed. The no-live-fallback
          # guarantee this assertion protects is still intact: every query
          # in stub.calls belongs to the gate, none of them is a real
          # aggregate/rows query for the page itself.
          all("source_import_batch_id FROM" in " ".join(c.split())
              or "MAX(batch_id)" in " ".join(c.split())
              or "AS has_data" in " ".join(c.split())
              for c in stub.calls),
          stub.calls)


def test_real_compute_snapshot_data_returns_real_rows_when_fresh():
    """Contrast case: the SAME real, extracted function, called against a
    fresh/fully-populated stub, returns data_unavailable=False with real
    row data -- proves the gate doesn't just always fail closed, it
    genuinely branches on the real freshness check."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)
    ns["query"] = _FreshQueryStub()

    result = ns["_compute_snapshot_data"]("overall", "TRAVIS")

    check("fresh scenario: data_unavailable is False", result["data_unavailable"] is False, result)
    check("fresh scenario: real breakdown row came through", len(result["rows"]) == 1, result["rows"])
    check("fresh scenario: rows[0].ptype is the real stubbed value",
          result["rows"][0]["ptype"] == "Residential", result["rows"][0])
    check("fresh scenario: totals populated from the real stubbed totals row",
          result["totals"] is not None and result["totals"]["n_total"] == 900, result["totals"])
    check("fresh scenario: status_2026 derived correctly (n_preliminary=0, n_total=900 -> certified)",
          result["status_2026"] == "certified", result["status_2026"])


def test_real_freshness_gate_empty_table_case():
    """A second real stale-shape scenario, distinct from the lag case above
    -- an EMPTY table (nothing refreshed yet at all). Proves the gate's
    other real failure branch also fires through the real extracted code,
    not just the one already covered above."""
    source = _read_real_app_py()
    ns = _build_real_namespace(source)

    calls = []

    def empty_table_stub(sql, params=None, one=False):
        calls.append(sql)
        norm = " ".join(sql.split())
        # PX-20260901-03: _snapshot_summary_freshness() now calls
        # _county_has_neighborhood_data() FIRST, before any batch-id query,
        # to decide whether snapshot_neighborhood_movers belongs in the
        # required-tables list at all -- Travis (this scenario's county)
        # has real neighborhood data, so the original 3-table requirement
        # is unchanged here.
        if "AS has_data" in norm:
            return {"has_data": True}
        if "source_import_batch_id FROM" in norm:
            return []  # empty table -- nothing refreshed yet
        if "MAX(batch_id)" in norm:
            return {"latest": None}
        raise AssertionError(f"unexpected query: {sql!r}")

    ns["query"] = empty_table_stub
    result = ns["_compute_snapshot_data"]("residential", "TRAVIS")

    check("empty-table scenario: data_unavailable is True", result["data_unavailable"] is True, result)
    check("empty-table scenario: reason follows the honest 'being prepared' shape, "
          "no developer-facing tokens",
          "is being prepared" in result["data_unavailable_reason"]
          and "Travis County" in result["data_unavailable_reason"]
          and not any(tok in result["data_unavailable_reason"] for tok in
                      ("refresh_snapshot_summary.py", "snapshot_breakdown", "TRAVIS", "loaders/")),
          result["data_unavailable_reason"])


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL SNAPSHOT DATA_UNAVAILABLE (REAL-CODE, EXTRACTED-AND-EXECUTED) TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
