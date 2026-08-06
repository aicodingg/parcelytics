#!/usr/bin/env python3
"""
test_parcel_resolver.py — PARTITION-2-IMPLEMENT, Verification item 4.

Real, isolated tests for resolve_parcel() (parcel_resolver.py) via an
injected stub query_fn — proves the real function's SQL shape, parameter
order, and default-argument behavior, without needing Flask/psycopg2
(neither is importable in this sandbox) and without importing app.py at
all (the injection point exists specifically so this stays possible).

Run: python3 test_parcel_resolver.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from parcel_resolver import resolve_parcel, DEFAULT_COUNTY

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


def test_resolve_parcel_defaults_to_travis():
    calls = []

    def stub_query(sql, params, one=False):
        calls.append((sql, params, one))
        return {"geo_id": params[1], "county_code": params[0], "situs_address": "123 Main St"}

    result = resolve_parcel("0100030105", query_fn=stub_query)

    check("defaults county_code to TRAVIS when not given", calls[0][1][0] == "TRAVIS", calls)
    check("defaults county_code to the module's own DEFAULT_COUNTY constant",
          calls[0][1][0] == DEFAULT_COUNTY, DEFAULT_COUNTY)
    check("passes geo_id through unmodified", calls[0][1][1] == "0100030105", calls)
    check("calls with one=True (a single-row lookup, matching app.py's own query() convention)",
          calls[0][2] is True, calls)
    check("returns whatever query_fn returned, unmodified", result["situs_address"] == "123 Main St", result)


def test_resolve_parcel_honors_explicit_county_code():
    calls = []

    def stub_query(sql, params, one=False):
        calls.append((sql, params, one))
        return None  # simulates "not found in this county"

    result = resolve_parcel("1234567890", county_code="DALLAS", query_fn=stub_query)

    check("an explicit county_code overrides the default", calls[0][1] == ("DALLAS", "1234567890"), calls)
    check("a real 'not found' result (None) passes through untouched", result is None, result)


def test_resolve_parcel_sql_shape():
    captured = {}

    def stub_query(sql, params, one=False):
        captured["sql"] = " ".join(sql.split())
        return None

    resolve_parcel("0100030105", query_fn=stub_query)

    check("real SQL is a plain composite-key equality lookup against parcel",
          captured["sql"] == "SELECT * FROM parcel WHERE county_code = %s AND geo_id = %s", captured)
    check("county_code is checked BEFORE geo_id in the WHERE clause (leading-column "
          "discipline, matching SPEC_COUNTY_PARTITIONING.md finding 9.2(a) -- county_code "
          "leads every composite key/index, no exceptions, including in application-level "
          "WHERE clauses written against that key)",
          captured["sql"].index("county_code") < captured["sql"].index("geo_id"), captured)


def test_resolve_parcel_lazy_imports_app_when_no_query_fn_given():
    """Confirms the lazy-import path exists and fails in exactly the
    expected, honest way in THIS sandbox (Flask/psycopg2 not installed) --
    proves the fallback is real code, not dead/unreachable, without
    requiring Flask to actually be present to test it."""
    try:
        resolve_parcel("0100030105")  # no query_fn -- triggers `from app import query`
        check("lazy import of app.query is reachable code", False,
              "expected an ImportError/ModuleNotFoundError in this Flask-less sandbox, "
              "but no exception was raised -- either Flask is now installed here, or "
              "the lazy-import branch was not actually reached")
    except (ImportError, ModuleNotFoundError) as e:
        check("lazy import of app.query is reachable code, and fails honestly in this "
              "sandbox (no Flask/psycopg2 installed) rather than silently no-op'ing",
              True, str(e))


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL parcel_resolver.py TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
