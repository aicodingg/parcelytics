"""
test_search_filter_delinquent.py — fixture tests for the "Delinquent Only"
filter added to /api/search_filter (NICK-DELINQUENT-1, Aug 2026), extended
under PX-20260828-14 (Aug 2026) for the COUNT(*) OVER() timeout fix.

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

PX-20260828-14 note: the real app.py imports `psycopg2` and `psycopg2.errors`
at module level and the fix's except clause references
`psycopg2.errors.QueryCanceled` -- since only the sliced FUNCTION BODY (not
the whole module) is exec'd here, a fake `psycopg2` stand-in (FakePsycopg2,
below) is injected into the exec namespace exposing the one real thing the
fix needs: a `.errors.QueryCanceled` exception class. This is not a mock of
psycopg2's behavior (it does no real DB work in this sandbox either way) --
it exists purely so `except psycopg2.errors.QueryCanceled:` can resolve the
name and, in the degrade-path tests below, actually catch the fake timeout
`fake_query()` raises.

What these tests check (per NICK-DELINQUENT-1's "Verification required" #1,
plus PX-20260828-14's two hard requirements):
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
  9. PX-20260828-14: no more COUNT(*) OVER() anywhere -- two separate query()
     calls happen (data query, then count query), and the FROM/JOIN/WHERE
     text is byte-for-byte IDENTICAL between them (the "build the predicates
     once, use them in both, do not hand-copy a parallel set" requirement) --
     proven by extracting each query's FROM..WHERE span and comparing them as
     strings, not just eyeballing the source.
  10. Count query invoked with timeout_ms=5000 specifically (its own,
      independent timeout, shorter than get_db()'s connection-wide 8000ms
      default) -- captured via the fake query()'s kwargs.
  11. Count-query timeout degrade path: fake_query raises
      psycopg2.errors.QueryCanceled on the count call only (data call
      succeeds normally) -- response is ok:True (not a 500), total/
      total_pages are None, count_unavailable is True, has_more is computed
      correctly from the data query's own LIMIT+1 fetch (independent of the
      count query's failure) -- proves the endpoint degrades instead of
      taking the whole request down.
  12. has_more via LIMIT+1: normal (non-degraded) path where a fake data
      fetch returns page_size+1 rows the fake DB drives off fetch_limit ==
      SEARCH_FILTER_PAGE_SIZE + 1 -- confirms fetch_limit is actually passed
      as a bind param and results are correctly trimmed back to page_size
      before being returned to the client.

Live-count verification (10,087 parcels / $99,826,342.32 combined total_due,
with NO other filter applied) is explicitly Diego's job against the real
production DB -- these fixture tests only prove the SQL/logic is correct in
the sandbox, they cannot and do not touch a real database. Diego is also
separately verifying the specific PX-20260828-14 failing combination
(Dallas + mv_min=1000000, tax_year=2025 returns instead of 500ing) and
running a live EXPLAIN post-fix -- neither of those is possible from this
sandbox (no live Postgres connection here, confirmed standing limitation).

Run: python3 test_search_filter_delinquent.py
"""
import re
import sys

sys.path.insert(0, ".")
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE, exclude_non_real_property_gap_sql
from tax_logic.classify import label_case_sql


class FakeQueryCanceled(Exception):
    """Stand-in for the real psycopg2.errors.QueryCanceled -- same role as
    every other Fake* class in this file: minimal enough to exercise the
    real app.py source under test, not a reimplementation of psycopg2."""
    pass


class FakeErrorsModule:
    QueryCanceled = FakeQueryCanceled


class FakePsycopg2:
    errors = FakeErrorsModule

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
                           fake_g=None, county_has_data=True,
                           count_query_raises=False):
    """
    Execs the real api_search_filter() body (sliced straight out of app.py)
    against fakes, calls it, and returns (captured, jsonify_payload).

    PX-20260824-06: fake_g (defaults to FakeG(), i.e. Travis) and
    county_has_data (defaults to True) let callers exercise the two new
    gates -- the g.county_slug-derived registration check and the
    _county_has_data() has-data check -- without touching every existing
    call site above.

    PX-20260828-14: the real function now calls query() TWICE -- once for
    the data (fetch_limit = page_size+1 rows, no COUNT), once for the
    separate COUNT(*). fake_query below tells the two calls apart by
    inspecting the SQL text (real code's own shape: the count query's SELECT
    list is exactly "COUNT(*) AS total_count", the data query's is not),
    mirroring `fake_rows` (a list of page rows, WITHOUT a total_count key
    now -- that field doesn't exist in the data query's real SELECT list
    anymore) for the data call and `fake_total_count` for the count call.
    `count_query_raises=True` makes the count call raise FakeQueryCanceled
    (the fake stand-in for psycopg2.errors.QueryCanceled) to exercise the
    degrade path -- the data call is unaffected either way, matching the
    real code's actual sequencing (data query runs and succeeds BEFORE the
    count query is even attempted).
    """
    captured = {"has_data_checked_for": None, "calls": []}

    def fake_query(sql, params=None, timeout_ms=None):
        is_count_call = "COUNT(*) AS total_count" in sql
        captured["calls"].append({"sql": sql, "params": params, "timeout_ms": timeout_ms,
                                   "is_count_call": is_count_call})
        if is_count_call:
            captured["count_sql"] = sql
            captured["count_params"] = params
            captured["count_timeout_ms"] = timeout_ms
            if count_query_raises:
                raise FakeQueryCanceled("simulated statement timeout")
            return [{"total_count": fake_total_count}]
        else:
            captured["data_sql"] = sql
            captured["data_params"] = params
            return fake_rows or []

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
        "psycopg2": FakePsycopg2,
    }
    exec(compile(FUNC_SRC, "app.py (sliced api_search_filter)", "exec"), namespace)
    result = namespace["api_search_filter"]()
    # Route bodies here always return a bare dict (the guard-clause early
    # returns are 2-tuples (dict, status) -- callers under test never hit
    # those paths since every scenario below supplies a real filter).
    if isinstance(result, tuple):
        payload = result[0]
    else:
        payload = result
    # Backward-compat alias: several existing checks below use
    # captured["sql"]/captured["params"] to mean "the query that carries the
    # filter WHERE clause" -- that's the data query now (the count query has
    # no SELECT list to inspect prop_type_label/total_due/etc against).
    if "data_sql" in captured:
        captured["sql"] = captured["data_sql"]
        captured["params"] = captured["data_params"]
    return captured, payload


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


def extract_from_where(sql):
    """Pulls the "FROM parcel p ... WHERE <clause>" span out of either query
    shape (data or count), whitespace-normalized, cutting before ORDER BY if
    present (only the data query has one). Used by Test 9 to prove the two
    queries share IDENTICAL FROM/JOIN/WHERE text, not two independently
    hand-copied versions of it -- Diego's explicit hard requirement."""
    start = sql.index("FROM parcel p")
    end = sql.find("ORDER BY", start)
    if end == -1:
        end = len(sql)
    return " ".join(sql[start:end].split())


# ── 9. PX-20260828-14: shared WHERE/JOIN construction -- the data query and
#      the count query must use the EXACT SAME FROM/JOIN/WHERE text, proving
#      the fix built the predicates once rather than hand-copying a second
#      set (Diego's explicit hard requirement, and the exact failure shape
#      of tonight's other audit findings). ─────────────────────────────────
print("Test 9: data query and count query share identical FROM/JOIN/WHERE text")
captured, payload = run_api_search_filter(
    {"delinquent_only": "1", "prop_type": "Commercial", "mv_min": "1000000", "tax_year": "2025", "page": "1"},
    fake_rows=[{"geo_id": "0006", "situs_address": "6 Main St", "neighborhood_cd": "N3",
                "prop_type_label": "Commercial", "market_value": 1200000, "assessed_value": 1150000,
                "data_source": "certified", "tax_year": 2025, "total_due": 500.00, "first_delinquent_yr": 2024}],
    fake_total_count=1,
)
all_ok &= check("both a data query and a count query were issued",
                "data_sql" in captured and "count_sql" in captured)
all_ok &= check("count query has no COUNT(*) OVER() (window function fully removed)",
                "OVER()" not in captured["data_sql"] and "OVER()" not in captured["count_sql"])
all_ok &= check("count query's SELECT list is a bare COUNT(*), not the wide data SELECT",
                captured["count_sql"].strip().startswith("SELECT") and
                "COUNT(*) AS total_count" in captured["count_sql"] and
                "prop_type_label" not in captured["count_sql"])
all_ok &= check(
    "data query and count query's FROM/JOIN/WHERE text is byte-for-byte identical",
    extract_from_where(captured["data_sql"]) == extract_from_where(captured["count_sql"]),
)
all_ok &= check("shared text includes this request's real filters (mv_min, delinquent JOIN)",
                "mv_min" in extract_from_where(captured["count_sql"]) and
                "tax_delinquent" in extract_from_where(captured["count_sql"]))

# ── 10. PX-20260828-14: count query gets its own, independent timeout
#      (5000ms) distinct from get_db()'s connection-wide 8000ms default --
#      Diego's other explicit hard requirement. ────────────────────────────
print("Test 10: count query invoked with its own timeout_ms=5000")
all_ok &= check("count query call captured a timeout_ms kwarg", captured.get("count_timeout_ms") is not None)
all_ok &= check("count query timeout is 5000ms, shorter than the 8000ms connection default",
                captured.get("count_timeout_ms") == 5000)
data_call = next(c for c in captured["calls"] if not c["is_count_call"])
all_ok &= check("data query call did NOT set a custom timeout_ms (uses the connection default)",
                data_call["timeout_ms"] is None)

# ── 11. PX-20260828-14: count-query timeout degrade path -- if the count
#      query itself times out, the endpoint must NOT 500 the whole request.
#      The data query (already run and succeeded) still returns real
#      results; total/total_pages become None; count_unavailable=True;
#      has_more is still correctly computed from the data query's own
#      LIMIT+1 fetch, independent of the count query's failure. ───────────
print("Test 11: count query times out -> degrades instead of failing the whole request")
# 51 fake rows (page_size 50 + 1) so has_more should come back True.
degrade_rows = [
    {"geo_id": f"0{i:03d}", "situs_address": f"{i} Main St", "neighborhood_cd": "N1",
     "prop_type_label": "Residential", "market_value": 1500000, "assessed_value": 1450000,
     "data_source": "certified", "tax_year": 2025}
    for i in range(51)
]
captured, payload = run_api_search_filter(
    {"mv_min": "1000000", "tax_year": "2025", "page": "1"},
    fake_rows=degrade_rows,
    count_query_raises=True,
)
all_ok &= check("request still succeeds (ok: True), not a 500/error payload", payload.get("ok") is True)
all_ok &= check("data query DID run and its rows are in the response",
                len(payload.get("results", [])) == 50)  # trimmed back from 51 (LIMIT+1) to page_size
all_ok &= check("total is None (count degraded, not silently wrong)", payload.get("total") is None)
all_ok &= check("total_pages is None", payload.get("total_pages") is None)
all_ok &= check("count_unavailable flag is True", payload.get("count_unavailable") is True)
all_ok &= check("has_more is True (51 fetched > 50 page size, independent of count failure)",
                payload.get("has_more") is True)

# ── 12. PX-20260828-14: LIMIT+1 fetch_limit is a real bind param, and
#      results are correctly trimmed back to page_size when count DOES
#      succeed (the ordinary, non-degraded path). ──────────────────────────
print("Test 12: fetch_limit bind param present; results trimmed to page_size on the normal path")
normal_rows = [
    {"geo_id": f"1{i:03d}", "situs_address": f"{i} Oak St", "neighborhood_cd": "N2",
     "prop_type_label": "Residential", "market_value": 400000, "assessed_value": 390000,
     "data_source": "certified", "tax_year": 2025}
    for i in range(51)
]
captured, payload = run_api_search_filter(
    {"mv_min": "300000", "tax_year": "2025", "page": "1"},
    fake_rows=normal_rows,
    fake_total_count=237,
)
all_ok &= check("fetch_limit param passed to the data query", captured["data_params"].get("fetch_limit") == 51)
all_ok &= check("results trimmed back to page_size (50), not the raw 51 fetched",
                len(payload.get("results", [])) == 50)
all_ok &= check("has_more True even on the normal (non-degraded) path when a 51st row exists",
                payload.get("has_more") is True)
all_ok &= check("total/total_pages come from the real count query on the normal path",
                payload.get("total") == 237 and payload.get("count_unavailable") is False)

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
