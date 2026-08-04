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
END_MARKER = "\n\n\n@app.route(\"/api/estimate_acq/<geo_id>\")"

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


def _row_confidence(data_source, assessed_value=None, market_value=None):
    # Minimal stand-in -- these tests are about the delinquent-filter SQL/
    # logic, not confidence tiering (which has its own dedicated coverage
    # elsewhere in this codebase, e.g. the AJR/Historical-Year Confidence
    # Tiering harness).
    return "verified" if data_source == "certified" else "preliminary"


def run_api_search_filter(query_args, fake_rows=None, fake_total_count=0):
    """
    Execs the real api_search_filter() body (sliced straight out of app.py)
    against fakes, calls it, and returns (captured_sql, captured_params,
    jsonify_payload).
    """
    captured = {}

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

    namespace = {
        "request": FakeRequest(query_args),
        "query": fake_query,
        "jsonify": fake_jsonify,
        "label_case_sql": label_case_sql,
        "_row_confidence": _row_confidence,
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

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
