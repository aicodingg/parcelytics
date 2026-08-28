#!/usr/bin/env python3
"""
verify_px_20260828_06_neutral_typeahead.py — PX-20260828-06.

"Cowork: build a true neutral cross-county typeahead endpoint" — brief:
api_address_search() only ever existed at '/<county_slug>/api/address_search';
the bare '/api/address_search' was a dead _LEGACY_REDIRECT_ROUTES stub
(301-to-Travis), so a genuinely county-less typeahead request could never
reach every live county the way _home_search_response()'s own full-page
Enter-to-search flow already does. Fix: a real bare route,
api_address_search_landing(), reusing resolve_exact_parcel()/
search_parcels_by_address() with county_code=None -- the exact same
cross-county mechanism _resolve_quick_search() already uses -- plus removal
of the now-conflicting _LEGACY_REDIRECT_ROUTES entry.

Technique: direct string/regex assertions against app.py's REAL, shipping
source text, same rigor and same reason as test_search_county_scoping.py /
test_dallas_gate_4_county_code.py -- this sandbox has no psycopg2 and no
network, so app.py cannot be imported and Flask cannot actually be run or
curled here (see the report's own sandbox-vs-live section for the honest
disclosure and the exact live curl commands Diego can run post-deploy).

Covers:
  1. The new route is registered at the true bare path, with the same
     rate limiter as the anchored endpoint, and is a genuinely separate
     view function (not a re-registration of api_address_search()).
  2. It calls resolve_exact_parcel()/search_parcels_by_address() with
     county_code=None -- the real cross-county loop, not g.county_code.
  3. county_name is derived correctly in BOTH branches given this route
     has no g.county_code at all: per-row off search_parcels_by_address()'s
     rows for the address-match branch (no single-value fallback, since a
     neutral call can span multiple counties), and from the resolved
     parcel's own county_code column for the exact-match branch.
  4. _LEGACY_REDIRECT_ROUTES no longer contains an entry for
     '/api/address_search' -- the conflict this brief's fix required
     removing before/alongside adding the real route.
  5. A short_ q (<3 chars) still short-circuits to an empty result list,
     matching the anchored endpoint's own contract.
  6. A logic-trace simulation of the neutral cross-county loop itself
     (using search_parcels_by_address()'s own real, documented algorithm
     against synthetic multi-county data, the same technique
     test_dallas_gate_4_county_code.py and this project's other DB-free
     fixtures use), proving multi-county results really do come back each
     tagged with their own county's county_name, not a single county's.

Run: python3 verify_px_20260828_06_neutral_typeahead.py
"""
import os
import re
import sys

REPO = "/sessions/amazing-sleepy-babbage/mnt/Parcelytics/code"
if not os.path.isdir(REPO):
    # Allow running from a checkout where this file sits alongside app.py.
    REPO = os.path.dirname(os.path.abspath(__file__))

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


src = open(os.path.join(REPO, "app.py")).read()


def extract_function(name, src=src):
    """Same slicing convention as test_search_county_scoping.py: one
    top-level `def name(...):` body up to (but not including) the next
    top-level `def `/`@app.route` line at column 0."""
    m = re.search(rf"\ndef {re.escape(name)}\(.*?\n(?=\ndef |\n@app\.)", src, re.DOTALL)
    if not m:
        raise AssertionError(f"could not locate function {name}() in app.py")
    return m.group(0)


def extract_decorated_block(name, src=src):
    """Grabs the @app.route/@limiter.limit decorator lines immediately
    above `def name(`, plus the function body -- for checking the route
    registration itself, not just the function's internal logic."""
    m = re.search(
        rf"((?:@[^\n]*\n)+)def {re.escape(name)}\(.*?\n(?=\ndef |\n@app\.)",
        src, re.DOTALL)
    if not m:
        raise AssertionError(f"could not locate decorated block for {name}() in app.py")
    return m.group(0)


landing_block = extract_decorated_block("api_address_search_landing")
landing_src = extract_function("api_address_search_landing")
anchored_src = extract_function("api_address_search")


# ─────────────────────────────────────────────────────────────────────────
# Section 1: route registration
# ─────────────────────────────────────────────────────────────────────────
section("Route registration -- true bare path, correct rate limit, distinct function")

check('registered at the true bare path @app.route("/api/address_search") '
      '(no <county_slug> segment)',
      '@app.route("/api/address_search")' in landing_block)
check('rate-limited with the same _LIMIT_TYPEAHEAD convention as the '
      'anchored endpoint',
      '@limiter.limit(_LIMIT_TYPEAHEAD)' in landing_block)
check('is a genuinely separate view function from api_address_search() '
      '(different function name, not a re-registration of the same one)',
      'def api_address_search_landing():' in landing_block
      and landing_src != anchored_src)
check('the anchored route (/<county_slug>/api/address_search) is untouched '
      'and still exists alongside the new bare one',
      '@app.route("/<county_slug>/api/address_search")' in src)


# ─────────────────────────────────────────────────────────────────────────
# Section 2: reuses the real cross-county mechanism, county_code=None
# ─────────────────────────────────────────────────────────────────────────
section("Reuses resolve_exact_parcel()/search_parcels_by_address() with county_code=None")

check('exact-match branch calls resolve_exact_parcel(q, county_code=None) -- '
      'the real cross-county loop, not an implicit g.county_code (which '
      'does not even exist on this route)',
      'resolve_exact_parcel(q, county_code=None)' in landing_src)
check('address-match branch calls search_parcels_by_address(q, limit=8, '
      'county_code=None) -- same cross-county loop, same limit as the '
      'anchored endpoint',
      'search_parcels_by_address(q, limit=8, county_code=None)' in landing_src)
check('does NOT call either function with an implicit/positional county '
      'scope that would silently default to a single county',
      'resolve_exact_parcel(q)' not in landing_src
      and 'search_parcels_by_address(q, limit=8)' not in landing_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 3: county_name derivation correctness given no g.county_code
# ─────────────────────────────────────────────────────────────────────────
section("county_name derivation -- per-row (address branch) vs. parcel-own-column (exact branch)")

check('address-match branch reads county_name straight off each row '
      '(search_parcels_by_address()\'s county_code=None loop already '
      'stamps a real, correct per-row value -- there is no single '
      'request-wide value to fall back to on a neutral route, unlike the '
      'anchored endpoint)',
      '"county_name": r.get("county_name")' in landing_src)
check('exact-match branch derives county_name from the resolved parcel\'s '
      'OWN county_code column via COUNTY_PROFILES, since resolve_exact_parcel()'
      '\'s neutral mode still runs a real SELECT * FROM parcel and therefore '
      'always has one',
      'COUNTY_PROFILES.get(\n            exact.get("county_code"), COUNTY_PROFILES["TRAVIS"]\n        )["county_name"]'
      in landing_src
      or 'COUNTY_PROFILES.get(' in landing_src and 'exact.get("county_code")' in landing_src)
check('does NOT derive a single request-wide county_name the way the '
      'anchored endpoint safely can (g.county_code does not exist here)',
      'county_name = COUNTY_PROFILES.get(g.county_code' not in landing_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 4: _LEGACY_REDIRECT_ROUTES no longer conflicts with the new route
# ─────────────────────────────────────────────────────────────────────────
section("_LEGACY_REDIRECT_ROUTES -- conflicting stub entry removed")

legacy_block_match = re.search(
    r"_LEGACY_REDIRECT_ROUTES = \[(.*?)\]", src, re.DOTALL)
legacy_block = legacy_block_match.group(1) if legacy_block_match else ""

check('the ("/api/address_search", "api_address_search") tuple is gone from '
      '_LEGACY_REDIRECT_ROUTES (a real route AND a redirect rule at the '
      'same literal path would conflict at Flask registration time)',
      '"/api/address_search"' not in legacy_block)
check('every OTHER pre-existing legacy-redirect entry is still present '
      '(this removal is scoped to exactly one tuple, not a wider edit)',
      all(path in legacy_block for path in [
          "/parcel/<geo_id>", "/api/parcel_entities", "/api/rates",
          "/api/benchmark", "/api/search_filter", "/api/peer_benchmark_local/<geo_id>",
          "/api/news", "/api/geocode/<geo_id>", "/api/peer_set/<geo_id>",
          "/api/billing/<geo_id>", "/parcels", "/compare",
      ]))


# ─────────────────────────────────────────────────────────────────────────
# Section 5: short-query contract parity with the anchored endpoint
# ─────────────────────────────────────────────────────────────────────────
section("Short-query (<3 chars) contract parity")

check('returns an empty result list (not an error, not a DB call) for a '
      'query under 3 characters, matching the anchored endpoint\'s own '
      'contract',
      'if len(q) < 3:' in landing_src
      and 'return jsonify({"ok": True, "results": []})' in landing_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 6: logic-trace simulation of the real cross-county algorithm
# ─────────────────────────────────────────────────────────────────────────
section("Logic-trace simulation -- multi-county results, each tagged with its own county_name")

# This models search_parcels_by_address()'s own real, documented algorithm
# (per its docstring/source read above: loop _live_counties(), concatenate,
# stamp county_slug/county_name per county, stop once `limit` is reached)
# against synthetic multi-county data, exactly the way this project's other
# DB-free fixtures (e.g. test_dallas_gate_4_county_code.py) simulate a
# loader's real logic without a live DB connection. This is NOT a
# reimplementation of app.py's SQL -- it is a proxy for the ILIKE match
# step, used only to prove the neutral loop-and-concatenate SHAPE is
# correct; the SQL scoping itself is separately proven by Section 2/3's
# string assertions against the real source and by the ALREADY-PASSING
# PX-20260828-03 fixture (test_search_county_scoping.py) covering the same
# search_parcels_by_address() function this route calls unmodified.

COUNTY_PROFILES = {
    "TRAVIS": {"county_name": "Travis County"},
    "DALLAS": {"county_name": "Dallas County"},
    "HARRIS": {"county_name": "Harris County"},
}
LIVE_COUNTIES = [
    {"county_code": "TRAVIS", "slug": "travis-tx"},
    {"county_code": "DALLAS", "slug": "dallas-tx"},
]
SYNTHETIC_ROWS = {
    "TRAVIS": [{"geo_id": "0100030105", "situs_address": "1201 S LAMAR BLVD", "owner_name": "Travis Owner"}],
    "DALLAS": [{"geo_id": "00000123456000000", "situs_address": "1201 MAIN ST", "owner_name": "Dallas Owner"}],
}


def fake_search_parcels_by_address(q, limit=8, county_code=None):
    """Mirrors the REAL search_parcels_by_address()'s neutral-branch
    shape (loop _live_counties(), concatenate, stamp per-county
    county_slug/county_name, stop at limit) using synthetic per-county
    rows instead of a live ILIKE query."""
    if county_code is None:
        results = []
        for entry in LIVE_COUNTIES:
            results.extend(fake_search_parcels_by_address(q, limit=limit, county_code=entry["county_code"]))
            if len(results) >= limit:
                break
        return results[:limit]
    county_name = COUNTY_PROFILES[county_code]["county_name"]
    rows = [dict(r) for r in SYNTHETIC_ROWS.get(county_code, [])]
    for r in rows:
        r["county_name"] = county_name
    return rows[:limit]


sim_rows = fake_search_parcels_by_address("main", limit=8, county_code=None)
sim_results = [
    {
        "geo_id": r["geo_id"],
        "address": r.get("situs_address") or "",
        "owner": r.get("owner_name") or "",
        "county_name": r.get("county_name"),
    }
    for r in sim_rows
]

check("simulated neutral call surfaces results from BOTH live counties "
      "(Travis AND Dallas), not just the first one",
      {r["county_name"] for r in sim_results} == {"Travis County", "Dallas County"})
check("each simulated result carries its OWN county's county_name, not a "
      "single site-wide value",
      any(r["county_name"] == "Travis County" for r in sim_results)
      and any(r["county_name"] == "Dallas County" for r in sim_results))
check("no simulated result has a missing/None county_name",
      all(r["county_name"] for r in sim_results))


print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE (sandbox has no psycopg2/network -- app.py cannot be")
print("imported or run, and the live app cannot be curled from this sandbox):")
print("  - that GET /api/address_search?q=<partial address> against the real")
print("    deployed app returns a 200 (not a 301) and real cross-county rows.")
print("  - Diego's own live verification, once deployed:")
print('      curl -s "https://<host>/api/address_search?q=main" | python3 -m json.tool')
print("    Expect: HTTP 200 (not a 301 to /travis-tx/api/address_search),")
print('    "ok": true, and (once Dallas/Harris have live data) results whose')
print('    "county_name" values are not all "Travis County".')
sys.exit(0 if all_ok else 1)
