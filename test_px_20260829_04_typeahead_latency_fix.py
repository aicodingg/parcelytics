"""
test_px_20260829_04_typeahead_latency_fix.py -- PX-20260829-04 Task 1
(measured-diagnosis fix, sign-off given by Diego).

Sandbox has no Flask/psycopg2/live Postgres (confirmed unavailable all
session, same standing constraint as every other PX brief's own test
files -- see test_search_filter_delinquent.py's own header for the
established precedent). So this cannot measure real wall-clock milliseconds
-- that requires Diego's own live deploy, which is exactly what the new
_log_typeahead_timing() instrumentation in app.py (api_address_search() /
api_address_search_landing()) now provides on every real request.

What CAN be proven here, and is the actual root-cause evidence for this
brief's diagnosis, is the DB CALL COUNT the real, shipping
search_parcels_by_address()/_live_counties() source issues per typeahead
request -- each call is one query(), and every query() call opens its own
brand-new physical DB connection (get_db() has no pooling, confirmed by
reading it directly -- no psycopg2.pool anywhere in this file). Call count
is a direct, deterministic proxy for "how many serial connection round
trips does one keystroke cost," independent of any particular DB's
latency -- and it is exactly what changed between the old code (git
history) and the new code below.

Technique: the same real-source slice-and-exec approach established
throughout this codebase's test suite (test_search_filter_delinquent.py,
test_dallas_gate_4_county_code.py, etc.) -- extract the REAL function
bodies out of app.py by line range and exec() them against a minimal fake
`query()` that counts calls and returns synthetic rows, plus real
COUNTY_SLUGS (small, extracted verbatim) and a trimmed, real-shaped
COUNTY_PROFILES stand-in (only the fields these functions actually read).
This tests the ACTUAL function bodies shipping in app.py, not a
reimplementation of them.

Run: python3 test_px_20260829_04_typeahead_latency_fix.py
"""
import os
import re
import time

APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
SRC_LINES = open(APP_PY, "r", encoding="utf-8").read().splitlines(keepends=True)


def _slice(start_marker, end_marker):
    """Extract source text from the first line containing start_marker up
    to (not including) the first later line containing end_marker."""
    start_i = next(i for i, l in enumerate(SRC_LINES) if start_marker in l)
    end_i = next(i for i in range(start_i + 1, len(SRC_LINES)) if end_marker in SRC_LINES[i])
    return "".join(SRC_LINES[start_i:end_i])


SEARCH_FN_SRC = _slice("def search_parcels_by_address(", "# ── County-in-URL routing")
COUNTY_SLUGS_SRC = _slice("COUNTY_SLUGS = {", "DEFAULT_COUNTY_SLUG = ") + 'DEFAULT_COUNTY_SLUG = "travis-tx"\n'
COUNTY_HAS_DATA_SRC = _slice("def _county_has_data(county_code):", "# ── PX-20260828-15 Task 3")
LIVE_COUNTIES_SRC = _slice("_LIVE_COUNTIES_CACHE = {", "def _live_counties_with_counts()")

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}  --  {detail}")
        FAILURES.append(label)


def make_namespace(call_log, fake_rows_by_pattern=None, county_has_data=None):
    """Builds the exec() namespace: real COUNTY_SLUGS/DEFAULT_COUNTY_SLUG/
    _county_has_data/_live_counties/search_parcels_by_address source (all
    sliced verbatim from app.py above), a trimmed real-shaped
    COUNTY_PROFILES stand-in, a fake `g` with no county context (the
    neutral-request case this brief's own finding is about), a fake
    `query()` that logs every call and returns synthetic rows, and real
    search_logic (already DB-free, imported directly, no faking needed)."""
    import search_logic

    class FakeG:
        pass

    ns = {
        "search_logic": search_logic,
        "g": FakeG(),
        "time": time,
        "COUNTY_PROFILES": {
            "TRAVIS": {"county_name": "Travis County"},
            "DALLAS": {"county_name": "Dallas County"},
            "HARRIS": {"county_name": "Harris County"},
        },
    }

    def fake_query(sql, params=None, one=False, timeout_ms=None):
        call_log.append({"sql": " ".join(sql.split()), "params": params})
        if "_county_has_data" in call_log[-1]["sql"]:
            pass  # unreachable, county_has_data is faked separately below
        return []  # overridden per-scenario below via monkeypatch of the compiled fn

    ns["query"] = fake_query
    exec(COUNTY_SLUGS_SRC, ns)
    exec(COUNTY_HAS_DATA_SRC, ns)
    exec(LIVE_COUNTIES_SRC, ns)
    exec(SEARCH_FN_SRC, ns)
    return ns


def scenario_a_match_on_first_attempt_both_counties_live():
    """Travis and Dallas both live. Query is a 4-token address that matches
    immediately (first, most-specific attempt) -- the common, fast case.
    OLD behavior: 3 calls for _live_counties() (_county_has_data x3,
    uncached) + up to 2 calls for the address search (one per county, each
    stopping at its own first successful attempt) = up to 5.
    NEW behavior: 3 calls for _live_counties() (still uncached on this
    first call) + exactly 1 call for the address search (one combined
    query across both live counties' county_code) = 4 total, and critically
    the address-search part no longer multiplies by county count."""
    call_log = []
    ns = make_namespace(call_log)

    # _county_has_data() real body does `query(...)["has_data"]` -- fake
    # query() must return a dict with that key. Travis + Dallas "have
    # data," Harris does not (matches real live state, PX-20260828-15).
    def fake_query(sql, params=None, one=False, timeout_ms=None):
        call_log.append({"sql": " ".join(sql.split()), "params": params})
        if "EXISTS" in sql:
            county_code = params["county_code"]
            return {"has_data": county_code in ("TRAVIS", "DALLAS")}
        # The address-search SELECT: return one Travis row and one Dallas
        # row in the SAME call, proving the combined query really does span
        # both counties in one round trip.
        return [
            {"geo_id": "0100030105", "situs_address": "123 CARTWRIGHT AVE", "owner_name": "A", "county_code": "TRAVIS"},
            {"geo_id": "0200030105", "situs_address": "456 CARTWRIGHT AVE", "owner_name": "B", "county_code": "DALLAS"},
        ]

    ns["query"] = fake_query
    exec(COUNTY_SLUGS_SRC, ns)
    exec(COUNTY_HAS_DATA_SRC, ns)
    exec(LIVE_COUNTIES_SRC, ns)
    exec(SEARCH_FN_SRC, ns)

    results = ns["search_parcels_by_address"]("2626 CARTWRIGHT AVE DALLAS", limit=8, county_code=None)

    address_search_calls = [c for c in call_log if "EXISTS" not in c["sql"]]
    liveness_calls = [c for c in call_log if "EXISTS" in c["sql"]]

    check("Scenario A: exactly 1 combined address-search query() call (was up to 2, one per county)",
          len(address_search_calls) == 1, address_search_calls)
    check("Scenario A: that one call scopes county_code via county_code = ANY(...)",
          "county_code = any(" in address_search_calls[0]["sql"].lower(), address_search_calls[0])
    check("Scenario A: _live_counties() still issues 3 liveness checks on an uncached (first) call",
          len(liveness_calls) == 3, liveness_calls)
    check("Scenario A: total DB calls for this whole request = 4 (3 liveness + 1 address search)",
          len(call_log) == 4, call_log)
    check("Scenario A: results carry BOTH counties' rows from the one combined query",
          {r["county_code"] for r in results} == {"TRAVIS", "DALLAS"}, results)
    check("Scenario A: each row's county_slug is correctly derived per-row (not a single site-wide value)",
          {r["geo_id"]: r["county_slug"] for r in results} ==
          {"0100030105": "travis-tx", "0200030105": "dallas-tx"}, results)
    check("Scenario A: each row's county_name is correctly derived per-row",
          {r["geo_id"]: r["county_name"] for r in results} ==
          {"0100030105": "Travis County", "0200030105": "Dallas County"}, results)


def scenario_b_live_counties_cache_hit():
    """A second neutral typeahead request within the 60s TTL window must
    NOT re-issue the 3 liveness queries -- this is the other half of Task
    1's fix (the TTL cache), and it is the part that most directly targets
    "one query() per REGISTERED slug on every single keystroke," which was
    true regardless of whether the address-search fan-out fix above had
    landed."""
    call_log = []

    def fake_query(sql, params=None, one=False, timeout_ms=None):
        call_log.append({"sql": " ".join(sql.split()), "params": params})
        if "EXISTS" in sql:
            return {"has_data": params["county_code"] in ("TRAVIS", "DALLAS")}
        return []

    import search_logic
    ns = {"search_logic": search_logic, "g": type("G", (), {})(), "time": time,
          "COUNTY_PROFILES": {"TRAVIS": {"county_name": "Travis County"},
                               "DALLAS": {"county_name": "Dallas County"},
                               "HARRIS": {"county_name": "Harris County"}},
          "query": fake_query}
    exec(COUNTY_SLUGS_SRC, ns)
    exec(COUNTY_HAS_DATA_SRC, ns)
    exec(LIVE_COUNTIES_SRC, ns)

    first = ns["_live_counties"]()
    calls_after_first = len(call_log)
    second = ns["_live_counties"]()
    calls_after_second = len(call_log)

    check("Scenario B: first _live_counties() call issues 3 real liveness queries (cache cold)",
          calls_after_first == 3, calls_after_first)
    check("Scenario B: second call within the TTL window issues ZERO additional queries (cache hit)",
          calls_after_second == calls_after_first, (calls_after_first, calls_after_second))
    check("Scenario B: cached result is identical to the freshly-queried one",
          first == second, (first, second))
    check("Scenario B: Harris (no data) correctly excluded from the live list",
          {e["county_code"] for e in first} == {"TRAVIS", "DALLAS"}, first)


def scenario_c_zero_match_caps_at_token_attempts_not_multiplied_by_counties():
    """A query that matches at NO specificity level in ANY county. OLD
    behavior: up to (tokens-1) attempts PER county, i.e. up to
    2 counties x 3 attempts = 6 address-search calls. NEW behavior: up to
    (tokens-1) attempts TOTAL, shared across every live county in one
    combined query per attempt -- caps at 3 regardless of live-county
    count, and that cap does not grow as more counties come online."""
    call_log = []

    def fake_query(sql, params=None, one=False, timeout_ms=None):
        call_log.append({"sql": " ".join(sql.split()), "params": params})
        if "EXISTS" in sql:
            return {"has_data": params["county_code"] in ("TRAVIS", "DALLAS")}
        return []  # zero matches at every attempt, every time

    import search_logic
    ns = {"search_logic": search_logic, "g": type("G", (), {})(), "time": time,
          "COUNTY_PROFILES": {"TRAVIS": {"county_name": "Travis County"},
                               "DALLAS": {"county_name": "Dallas County"},
                               "HARRIS": {"county_name": "Harris County"}},
          "query": fake_query}
    exec(COUNTY_SLUGS_SRC, ns)
    exec(COUNTY_HAS_DATA_SRC, ns)
    exec(LIVE_COUNTIES_SRC, ns)
    exec(SEARCH_FN_SRC, ns)

    results = ns["search_parcels_by_address"]("2626 NONEXISTENT FAKE STREET", limit=8, county_code=None)
    address_search_calls = [c for c in call_log if "EXISTS" not in c["sql"]]

    check("Scenario C: zero-match 4-token query issues exactly 3 address-search calls (tokens-1, not x2 counties)",
          len(address_search_calls) == 3, address_search_calls)
    check("Scenario C: returns empty results (genuinely no match anywhere)",
          results == [], results)


if __name__ == "__main__":
    print("--- Scenario A: match on first attempt, both counties live ---")
    scenario_a_match_on_first_attempt_both_counties_live()
    print("\n--- Scenario B: _live_counties() TTL cache hit ---")
    scenario_b_live_counties_cache_hit()
    print("\n--- Scenario C: zero-match caps at token attempts, not multiplied by county count ---")
    scenario_c_zero_match_caps_at_token_attempts_not_multiplied_by_counties()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("All PX-20260829-04 Task 1 (typeahead latency fix) scenarios passed.")
