#!/usr/bin/env python3
"""
loaders/test_per_county_freshness.py — PARTITION-2-IMPLEMENT, Verification
item 3.

Proves the real bug SPEC_COUNTY_PARTITIONING.md finding 9.7 named --
"refreshing Travis alone would incorrectly mark Dallas's data fresh too" --
does NOT happen with the real, modified code, in both places Part 3 touched:

  1. loaders/refresh_group_stats.assert_group_stats_fresh(conn, county_code)
     -- a plain, directly-importable function (no Flask/psycopg2 import
     chain), tested here directly against a fake connection.
  2. loaders/refresh_snapshot_summary.assert_snapshot_summary_fresh(conn,
     county_code) -- same technique, same file's real function.
  3. app.py's _snapshot_summary_freshness(county_code) -- CANNOT be
     imported directly (Flask/psycopg2 are not installed in this sandbox --
     confirmed, same as every other app.py test this project has built).
     Proven instead via the same extraction-and-exec technique
     test_snapshot_data_unavailable.py already established this session:
     regex-extract the REAL, unmodified function body out of the REAL
     app.py source, exec() it into a controlled namespace with a stub
     query(), and call the real function object -- this proves the actual
     shipped code, not a re-typed copy of it, genuinely scopes by
     county_code.

Run: python3 loaders/test_per_county_freshness.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.refresh_group_stats import assert_group_stats_fresh
from loaders.refresh_snapshot_summary import assert_snapshot_summary_fresh

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


# ═══════════════════════════════════════════════════════════════════════════
# 1 & 2 — the two loaders/ functions, tested directly (plain Python, no
# Flask/psycopg2 dependency at all)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self._rows = self.conn.handler(" ".join(sql.split()), params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, handler):
        self.handler = handler

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)


def _two_county_group_stats_handler(travis_batch=5, dallas_batch=3, latest_batch=5):
    """Simulates the real post-migration shape: group_stats holds rows for
    BOTH TRAVIS (freshly reloaded, batch 5 -- matches latest) and DALLAS
    (stale, still batch 3 -- an older county-scoped reload hasn't re-run
    yet) at the same time -- exactly the real scenario finding 9.7
    describes."""
    def handler(sql, params):
        if "SELECT DISTINCT source_import_batch_id FROM group_stats WHERE county_code" in sql:
            county = params[0]
            if county == "TRAVIS":
                return [(travis_batch,)]
            if county == "DALLAS":
                return [(dallas_batch,)]
            return []
        if "SELECT MAX(batch_id) FROM load_batch" in sql:
            return [(latest_batch,)]
        raise AssertionError(f"unexpected SQL: {sql!r}")
    return handler


def test_group_stats_travis_fresh_dallas_stale_reported_distinctly():
    conn = _FakeConn(_two_county_group_stats_handler(travis_batch=5, dallas_batch=3, latest_batch=5))

    travis_fresh, travis_detail = assert_group_stats_fresh(conn, county_code="TRAVIS")
    dallas_fresh, dallas_detail = assert_group_stats_fresh(conn, county_code="DALLAS")

    check("THE core proof (finding 9.7): Travis reports fresh", travis_fresh is True, travis_detail)
    check("THE core proof (finding 9.7): Dallas is correctly reported NOT fresh -- "
          "a Travis-only refresh does NOT mark Dallas fresh too",
          dallas_fresh is False, dallas_detail)
    check("Dallas's failure reason names Dallas and the real stale batch",
          "DALLAS" in dallas_detail["reason"] and "3" in dallas_detail["reason"], dallas_detail)
    check("each call's detail carries its own real county_code", travis_detail["county_code"] == "TRAVIS"
          and dallas_detail["county_code"] == "DALLAS", (travis_detail, dallas_detail))


def _two_county_snapshot_handler(travis_batch=5, dallas_batch=None, latest_batch=5):
    """dallas_batch=None simulates Dallas having NO rows at all yet (the
    real state before Dallas's first-ever refresh, not just a stale one)."""
    def handler(sql, params):
        if "FROM snapshot_breakdown WHERE county_code" in sql \
           or "FROM snapshot_totals WHERE county_code" in sql \
           or "FROM snapshot_neighborhood_movers WHERE county_code" in sql:
            county = params[0]
            if county == "TRAVIS":
                return [(travis_batch,)]
            if county == "DALLAS" and dallas_batch is not None:
                return [(dallas_batch,)]
            return []
        if "SELECT MAX(batch_id) FROM load_batch" in sql:
            return [(latest_batch,)]
        raise AssertionError(f"unexpected SQL: {sql!r}")
    return handler


def test_snapshot_summary_travis_fresh_dallas_empty_reported_distinctly():
    conn = _FakeConn(_two_county_snapshot_handler(travis_batch=5, dallas_batch=None, latest_batch=5))

    travis_fresh, travis_detail = assert_snapshot_summary_fresh(conn, county_code="TRAVIS")
    dallas_fresh, dallas_detail = assert_snapshot_summary_fresh(conn, county_code="DALLAS")

    check("Tier 1 tables: Travis reports fresh", travis_fresh is True, travis_detail)
    check("Tier 1 tables: Dallas (no rows yet) correctly reported NOT fresh, not silently 'fresh by "
          "virtue of Travis being fresh'", dallas_fresh is False, dallas_detail)
    check("Dallas's reason names the empty-for-this-county case",
          "no rows for county_code" in dallas_detail["reason"], dallas_detail)


# ═══════════════════════════════════════════════════════════════════════════
# 3 — app.py's _snapshot_summary_freshness(), via extraction-and-exec
# (same real technique test_snapshot_data_unavailable.py already
# established this session, reused here rather than reinvented)
# ═══════════════════════════════════════════════════════════════════════════

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read_real_app_py():
    with open(os.path.join(REPO_ROOT, "app.py"), encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_function_body(source, func_name):
    """Identical technique to test_snapshot_correctness_1.py's /
    test_snapshot_data_unavailable.py's own helper of the same name."""
    m = re.search(rf"\ndef {re.escape(func_name)}\(", source)
    if not m:
        return None
    start = m.start()
    rest = source[start + 1:]
    end_m = re.search(r"\n(def |@app\.route)", rest)
    end = start + 1 + end_m.start() if end_m else len(source)
    return source[start:end]


def test_real_app_py_snapshot_summary_freshness_scopes_by_county():
    source = _read_real_app_py()
    freshness_src = _extract_function_body(source, "_snapshot_summary_freshness")
    assert freshness_src, "_snapshot_summary_freshness() not found in app.py -- extraction broke"

    calls = []

    def stub_query(sql, params=None, one=False):
        norm = " ".join(sql.split())
        calls.append((norm, params))
        if "source_import_batch_id FROM" in norm and "WHERE county_code" in norm:
            county = params[0]
            if county == "TRAVIS":
                return [{"source_import_batch_id": 5}]
            if county == "DALLAS":
                return [{"source_import_batch_id": 3}]
            return []
        if "MAX(batch_id)" in norm:
            return {"latest": 5}
        raise AssertionError(f"unexpected query in real app.py extraction: {sql!r}")

    namespace = {"query": stub_query}
    exec(compile(freshness_src, "<extracted _snapshot_summary_freshness>", "exec"), namespace)
    real_fn = namespace["_snapshot_summary_freshness"]

    travis_fresh, travis_reason = real_fn(county_code="TRAVIS")
    dallas_fresh, dallas_reason = real_fn(county_code="DALLAS")

    check("REAL, extracted app.py code: Travis is fresh", travis_fresh is True, travis_reason)
    check("REAL, extracted app.py code: THE core finding-9.7 proof -- Dallas (stale, batch 3) is "
          "correctly reported NOT fresh even though Travis (batch 5) is fresh in the SAME table",
          dallas_fresh is False, dallas_reason)
    check("Dallas's reason string names Dallas specifically",
          "DALLAS" in dallas_reason, dallas_reason)
    check("every real query issued during both calls was scoped with a WHERE county_code = %s "
          "clause -- proves the real, shipped SQL text, not just this test's stub, is scoped",
          all("WHERE county_code" in sql for sql, _ in calls if "source_import_batch_id FROM" in sql),
          calls)

    default_fresh, _ = real_fn()  # no county_code passed -- must default to 'TRAVIS', not crash
    check("calling with no county_code arg defaults to the real 'TRAVIS' seam (finding 9.5's pattern)",
          default_fresh is True, default_fresh)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL per-county freshness TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
