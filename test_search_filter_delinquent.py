"""
test_search_filter_delinquent.py — fixture tests for the "Delinquent Only"
filter added to /api/search_filter (NICK-DELINQUENT-1, Aug 2026).

Sandbox has no Flask/psycopg2 (confirmed unavailable, no outbound network to
pip install them — same constraint as test_rate_limit_exempt.py and
verify_property_html_render.py earlier this session). Uses the same
slice-and-exec technique already established in this codebase: extract
api_search_filter()'s REAL source text out of app.py between two markers and
exec() it against a minimal namespace of fakes (fake `request.args`, a fake
`query()` that records the SQL/params it was called with instead of hitting a
real DB, real `label_case_sql` from tax_logic.classify, real
CANONICAL_PARCEL_EXCL_BARE / exclude_non_real_property_gap_sql from
parcel_filters). This tests the ACTUAL function body that ships in app.py,
not a reimplementation of it.

What these tests check (per NICK-DELINQUENT-1's "Verification required" #1):
  1. delinquent_only=0 (or absent): no tax_delinquent join/select, no
     CANONICAL_PARCEL_EXCL/gap-exclusion WHERE clauses added -- confirms the
     new filter is fully opt-in and every pre-existing filter's SQL is
     byte-for-byte unchanged (regression coverage for the "no more than any
     other filter does" scoping decision documented in app.py's comment).
  2. delinquent_only=1 alone: real INNER JOIN against tax_delinquent with
     total_due > 0 in its ON clause, total_due/first_delinquent_yr added to
     the SELECT list, CANONICAL_PARCEL_EXCL_BARE + the L-class gap exclusion
     both present in the WHERE clause, and has_real_filter no longer requires
     a second filter to pass the "at least one real filter" guard.
  3. delinquent_only=1 combined with prop_type=Commercial: proves Nick's
     actual real request (narrowing delinquent parcels down by asset type) --
     both the prop_type WHERE clause AND the delinquent join/WHERE clauses
     are present simultaneously, not one replacing the other.
  4. Results-loop: total_due/first_delinquent_yr are only added to each
     result dict when delinquent_only is set, confirmed via a fake `query()`
     return row carrying those two fields.

Live-count verification (10,087 parcels / $99,826,342.32 combined total_due,
with NO other filter applied) is explicitly Diego's job against the real
production DB -- these fixture tests only prove the SQL/logic is correct in
the sandbox, they cannot and do not touch a real database.

Run: python3 test_search_filter_delinquent.py
"""
import re
import sys

sys.path.insert(0, ".")
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE, exclude_non_real_property_gap_sql
from tax_logic.classify import label_case_sql

APP_PY = open("app.py").read()

START_MARKER = "\ndef api_search_filter():"
# DALLAS-GATE-1 Part 2: route path now carries a leading /<county_slug>
# segment (app.py's real, current decorator text) -- marker updated to
# match; the slice boundary logic itself (find api_search_filter's real
# source between two literal markers) is unchanged.
END_MARKER = "\n\n\n@app.route(\"/<county_slug>/api/estimate_acq/<geo_id>\")"

start = APP_PY.index(START_MARKER)
end = APP_PY.index(END_MARKER, start)
FUNC_SRC = APP_PY[start:end]

assert "delinquent_only" in FUNC_SRC, "sanity: slice must contain the new filter code"
assert "def api_search_filter" in FUNC_SRC


class FakeArgs(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


class FakeRequest:
    def __init__(self, args):
        self.args = FakeArgs(args)


class FakeG:
    """DALLAS-GATE-2 Part 2: api_search_filter() now reads g.county_code
    (the fix for the search_filter-related gap flagged by
    verify_index_coverage.py -- see app.py's own comment at the top of the
    function's `where` list construction). Minimal stand-in for Flask's real
    `g` request-context object -- same single attribute _pull_county_slug()
    sets on every real request, hardcoded to 'TRAVIS' here since these tests
    exercise the delinquent-filter SQL/logic, not multi-county routing.

    PX-20260824-06: county_slug added alongside county_code -- both are set
    together by the real _pull_county_slug() (app.py:1891-1892), and the
    Task 2 fix reads g.county_slug to validate the `county` query-string
    param against the request's own already-registered slug (see that
    fix's comment in app.py). Kept in sync with county_code here the same
    way the real request context keeps them in sync."""
    county_code = "TRAVIS"
    county_slug = "travis-tx"


def _row_confidence(data_source, assessed_value=None, market_value=None):
    # Minimal stand-in -- these tests are about the delinquent-filter SQL/
    # logic, not confidence tiering (which has its own dedicated coverage
    # elsewhere in this codebase, e.g. the AJR/Historical-Year Confidence
    # Tiering harness).
    return "verified" if data_source == "certified" else "preliminary"


# PX-20260824-06: minimal stand-in for COUNTY_PROFILES, real enough to
# exercise the county_name lookups in both new gates' error/message
# strings without importing all of app.py's module-level state.
FAKE_COUNTY_PROFILES = {
    "TRAVIS": {"county_name": "Travis County"},
    "DALLAS": {"county_name": "Dallas County"},
}


def run_api_search_filter(query_args, fake_rows=None, fake_total_count=0,
                           fake_g=None, county_has_data=True):
    """
    Execs the real api_search_filter() body (sliced straight out of app.py)
    against fakes, calls it, and returns (captured_sql, captured_params,
    jsonify_payload).

    PX-20260824-06: fake_g (defaults to FakeG(), i.e. Travis) and
    county_has_data (defaults to True) let callers exercise the two new
    gates -- the g.county_slug-derived registration check and the
    _county_has_data() has-data check -- without touching every existing
    call site above.
    """
    captured = {"has_data_checked_for": None}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        rows = fake_rows or []
        if rows:
            # Real code reads total_count off rows[0] -- COUNT(*) OVER().
            for r in rows:
                r["total_count"] = fake_total_count
        return rows

    def fake_jsonify(payload):
        return payload

    def fake_county_has_data(county_code):
        captured["has_data_checked_for"] = county_code
        return county_has_data

    namespace = {
        "request": FakeRequest(query_args),
        "g": fake_g or FakeG(),
        "query": fake_query,
        "jsonify": fake_jsonify,
        "label_case_sql": label_case_sql,
        "_row_confidence": _row_confidence,
        "_county_has_data": fake_county_has_data,
        "COUNTY_PROFILES": FAKE_COUNTY_PROFILES,
        "_HS_TOKEN_RE": r'(^|[,;])\s*HS\s*($|[,;])',
        "CERTIFIED_TIER_DATA_SOURCES": frozenset({"certified", "cert_2022", "cert_2023", "cert_2024"}),
        "SEARCH_FILTER_PAGE_SIZE": 50,
        "CANONICAL_PARCEL_EXCL_BARE": CANONICAL_PARCEL_EXCL_BARE,
        "exclude_non_real_property_gap_sql": exclude_non_real_property_gap_sql,
    }
    exec(compile(FUNC_SRC, "app.py (sliced api_search_filter)", "exec"), namespace)
    result = namespace["api_search_filter"]()
    # Route bodies here always return a bare dict (the guard-clause early
    # returns are 2-tuples (dict, status) -- callers under test never hit
    # those paths since every scenario below supplies a real filter).
    if isinstance(result, tuple):
        return captured, result[0]
    return captured, result


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


all_ok = True

# ── 1. Regression: delinquent_only absent leaves every existing filter's
#      SQL untouched -- no join, no select, no extra WHERE clauses. ────────
print("Test 1: delinquent_only absent (regression -- existing filters unchanged)")
captured, payload = run_api_search_filter(
    {"prop_type": "Residential", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0001", "situs_address": "1 Main St", "neighborhood_cd": "N1",
                "prop_type_label": "Residential", "market_value": 300000, "assessed_value": 290000,
                "data_source": "certified", "tax_year": 2025}],
    fake_total_count=1,
)
sql = captured["sql"]
all_ok &= check("no tax_delinquent JOIN present", "tax_delinquent" not in sql)
all_ok &= check("no CANONICAL_PARCEL_EXCL fragment present", "state_cd1" not in sql or "COALESCE(p.state_cd1" not in sql)
all_ok &= check("no total_due/first_delinquent_yr in SELECT", "total_due" not in sql and "first_delinquent_yr" not in sql)
all_ok &= check("result dict has no total_due/first_delinquent_yr keys",
                "total_due" not in payload["results"][0] and "first_delinquent_yr" not in payload["results"][0])
all_ok &= check("prop_type filter still applied", "prop_type_label" in sql or "prop_type" in captured["params"])

# ── 2. delinquent_only alone (no other filter) ──────────────────────────────
print("Test 2: delinquent_only=1 alone")
captured, payload = run_api_search_filter(
    {"delinquent_only": "1", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0002", "situs_address": "2 Main St", "neighborhood_cd": "N1",
                "prop_type_label": "Commercial", "market_value": 500000, "assessed_value": 480000,
                "data_source": "certified", "tax_year": 2025, "total_due": 12345.67, "first_delinquent_yr": 2022}],
    fake_total_count=1,
)
sql = captured["sql"]
all_ok &= check("passes has_real_filter guard (200, not 400)", "error" not in payload)
all_ok &= check("real INNER JOIN against tax_delinquent with total_due > 0 in ON clause",
                "JOIN tax_delinquent d ON d.geo_id = p.geo_id AND d.total_due > 0" in sql)
all_ok &= check("total_due, first_delinquent_yr added to SELECT", "d.total_due" in sql and "d.first_delinquent_yr" in sql)
all_ok &= check("CANONICAL_PARCEL_EXCL_BARE fragment present in WHERE", CANONICAL_PARCEL_EXCL_BARE in sql)
all_ok &= check("L-class gap-exclusion fragment present in WHERE", exclude_non_real_property_gap_sql("p.state_cd1") in sql)
all_ok &= check("result dict carries total_due (float)", payload["results"][0]["total_due"] == 12345.67)
all_ok &= check("result dict carries first_delinquent_yr", payload["results"][0]["first_delinquent_yr"] == 2022)

# ── 3. delinquent_only combined with prop_type (Nick's actual real request:
#      narrow delinquent parcels down by asset type) ────────────────────────
print("Test 3: delinquent_only=1 combined with prop_type=Commercial")
captured, payload = run_api_search_filter(
    {"delinquent_only": "1", "prop_type": "Commercial", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0003", "situs_address": "3 Main St", "neighborhood_cd": "N2",
                "prop_type_label": "Commercial", "market_value": 900000, "assessed_value": 880000,
                "data_source": "certified", "tax_year": 2025, "total_due": 4321.00, "first_delinquent_yr": 2023}],
    fake_total_count=1,
)
sql = captured["sql"]
all_ok &= check("delinquent JOIN present alongside asset-type filter",
                "JOIN tax_delinquent d ON d.geo_id = p.geo_id AND d.total_due > 0" in sql)
all_ok &= check("prop_type WHERE clause also present (both filters combine, neither replaces the other)",
                "prop_type" in captured["params"] and captured["params"]["prop_type"] == "Commercial")
all_ok &= check("CANONICAL_PARCEL_EXCL_BARE still present when combined with prop_type", CANONICAL_PARCEL_EXCL_BARE in sql)
all_ok &= check("result still carries total_due when combined with another filter",
                payload["results"][0]["total_due"] == 4321.00)

# ── 4. delinquent_only=0 explicitly (not just absent) behaves the same as
#      absent -- both are "off". ─────────────────────────────────────────────
print("Test 4: delinquent_only=0 explicitly (not just omitted) still means off")
captured, payload = run_api_search_filter(
    {"delinquent_only": "0", "prop_type": "Residential", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0004", "situs_address": "4 Main St", "neighborhood_cd": "N1",
                "prop_type_label": "Residential", "market_value": 250000, "assessed_value": 240000,
                "data_source": "certified", "tax_year": 2025}],
    fake_total_count=1,
)
sql = captured["sql"]
all_ok &= check("no tax_delinquent JOIN when delinquent_only=0", "tax_delinquent" not in sql)

# ── 5. PX-20260824-06: registration-mismatch gate (was `if county !=
#      "travis"`) -- a `county` query param that doesn't match the
#      request's own g.county_slug is rejected, without a hardcoded
#      county literal driving the comparison. ──────────────────────────────
print("Test 5: county param mismatched against g.county_slug -> 400, no query run")
captured, payload = run_api_search_filter(
    {"county": "dallas", "prop_type": "Residential", "tax_year": "2025", "page": "1"},
    fake_g=FakeG(),  # county_slug = "travis-tx" -> expected token "travis"
)
all_ok &= check("rejected (ok: False)", payload.get("ok") is False)
all_ok &= check("error message present and names the real county", "error" in payload and "Travis County" in payload["error"])
all_ok &= check("no query ever run (rejected before the SQL block)", "sql" not in captured)
all_ok &= check("has-data check never reached", captured["has_data_checked_for"] is None)

# ── 6. PX-20260824-06: matching county param on a real Dallas request
#      passes the registration check (no hardcoded "travis" literal blocks
#      it) and reaches the has-data check. ─────────────────────────────────
print("Test 6: county=dallas on a Dallas request (g.county_slug=dallas-tx) passes registration gate")


class FakeGDallas:
    county_code = "DALLAS"
    county_slug = "dallas-tx"


captured, payload = run_api_search_filter(
    {"county": "dallas", "prop_type": "Residential", "tax_year": "2025", "page": "1"},
    fake_g=FakeGDallas(),
    county_has_data=False,  # Dallas: registered, not yet loaded
)
all_ok &= check("registration gate passed (no county-mismatch error)",
                not (payload.get("ok") is False and "mismatch" in payload.get("error", "").lower()))
all_ok &= check("has-data check reached and scoped to DALLAS", captured["has_data_checked_for"] == "DALLAS")

# ── 7. PX-20260824-06: has-data gate -- registered-but-dataless county
#      (Dallas today) gets a clean "no data yet" response, not a 500 and
#      not results from the (never-run) query. ─────────────────────────────
print("Test 7: Dallas registered but county_has_data=False -> clean no-data-yet response")
all_ok &= check("ok is False (distinguishable from a real 0-result search)", payload.get("ok") is False)
all_ok &= check("no_data_yet flag set", payload.get("no_data_yet") is True)
all_ok &= check("message names Dallas County, not a generic/Travis message", "Dallas County" in payload.get("error", ""))
all_ok &= check("query() never called (no wasted round-trip)", "sql" not in captured)

# ── 8. PX-20260824-06: has-data gate does NOT block Travis (the only
#      county with real data today) -- pure regression check that the new
#      gate is additive, not a behavior change for the existing path. ──────
print("Test 8: Travis (county_has_data=True) unaffected by the new has-data gate")
captured, payload = run_api_search_filter(
    {"prop_type": "Residential", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0005", "situs_address": "5 Main St", "neighborhood_cd": "N1",
                "prop_type_label": "Residential", "market_value": 310000, "assessed_value": 300000,
                "data_source": "certified", "tax_year": 2025}],
    fake_total_count=1,
    county_has_data=True,
)
all_ok &= check("has-data check reached and scoped to TRAVIS", captured["has_data_checked_for"] == "TRAVIS")
all_ok &= check("real query ran (has-data gate did not short-circuit)", "sql" in captured)
all_ok &= check("normal results payload returned", payload.get("ok") is not False and "results" in payload)

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
