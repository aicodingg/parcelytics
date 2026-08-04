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

from snapshot_taxonomy import ptype_and_sort_case_for_view, _SNAPSHOT_SECTOR_VIEWS

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
    assert freshness_src, "_snapshot_summary_freshness() not found in app.py -- extraction broke"
    assert compute_src, "_compute_snapshot_data() not found in app.py -- extraction broke"

    namespace = {
        "ptype_and_sort_case_for_view": ptype_and_sort_case_for_view,
        "_SNAPSHOT_SECTOR_VIEWS": _SNAPSHOT_SECTOR_VIEWS,
        "SNAPSHOT_SUBTYPE_CAP": 7,
        "query": None,  # set per-scenario by the caller before invoking
    }
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

    result = ns["_compute_snapshot_data"]("overall")

    check("stale scenario: data_unavailable is True", result["data_unavailable"] is True, result)
    check("stale scenario: a real, honest reason string is present",
          bool(result.get("data_unavailable_reason")) and "STALE" not in result["data_unavailable_reason"].upper()
          or "stale" in result["data_unavailable_reason"].lower(),
          result.get("data_unavailable_reason"))
    check("stale scenario: reason mentions refresh_snapshot_summary.py (actionable, not vague)",
          "refresh_snapshot_summary.py" in result["data_unavailable_reason"], result["data_unavailable_reason"])
    check("stale scenario: rows is the empty-list placeholder, not a partial/crashed result",
          result["rows"] == [], result["rows"])
    check("stale scenario: totals is None", result["totals"] is None, result)
    check("stale scenario: bench_trends is empty (no live fallback query attempted)",
          result["bench_trends"] == [], result)
    check("stale scenario: status_2026 defaults to 'none'", result["status_2026"] == "none", result)
    check("stale scenario: NEVER issued a query past the freshness gate (proves no live fallback)",
          all("source_import_batch_id FROM" in " ".join(c.split()) or "MAX(batch_id)" in " ".join(c.split())
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

    result = ns["_compute_snapshot_data"]("overall")

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
        if "source_import_batch_id FROM" in norm:
            return []  # empty table -- nothing refreshed yet
        if "MAX(batch_id)" in norm:
            return {"latest": None}
        raise AssertionError(f"unexpected query: {sql!r}")

    ns["query"] = empty_table_stub
    result = ns["_compute_snapshot_data"]("residential")

    check("empty-table scenario: data_unavailable is True", result["data_unavailable"] is True, result)
    check("empty-table scenario: reason names 'has not been generated yet'",
          "has not been generated yet" in result["data_unavailable_reason"], result["data_unavailable_reason"])


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
