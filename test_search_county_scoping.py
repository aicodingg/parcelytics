#!/usr/bin/env python3
"""
test_search_county_scoping.py — PX-20260828-03 Task 3.

The brief's own framing: "Today's test showed [the results list] working
correctly for Dallas (7 real Dallas addresses returned) — this is a
verification/regression-proofing step, not a known bug, but worth a
fixture given how many hidden search paths turned up tonight."

Technique: direct string/regex assertions against app.py's REAL, shipping
source text -- same rigor and same reason as test_dallas_gate_4_county_code.py
(these are simple SQL-WHERE-clause/column-list checks, not functions with
branching logic that need to be exercised end-to-end via slice-and-exec).
Every assertion reads the actual file on disk; if the real source drifts,
these tests fail.

Covers:
  Section 1: search_parcels_by_address()'s SQL carries county_code in its
             WHERE clause (DALLAS-GATE-1 Part 2's fix), and its neutral
             (county_code=None) branch loops over _live_counties() rather
             than ever issuing an unscoped query.
  Section 2: resolve_exact_parcel()'s two SQL statements both carry
             county_code in their WHERE clause, with the same neutral-
             branch loop.
  Section 3: PX-20260828-03 Task 2's county_name addition -- stamped
             per-row in search_parcels_by_address() (alongside the
             pre-existing county_slug), and api_address_search() reads it
             off each row rather than deriving its own separate copy for
             the address-match branch.
  Section 4: PX-20260828-03 Task 1's unification -- _resolve_quick_search()
             exists as the one shared three-way resolver, and all three of
             its real callers (_home_search_response(), search_page(),
             search_landing()) actually call it rather than reimplementing
             the resolution logic a second/third/fourth time.

Run: python3 test_search_county_scoping.py
"""
import re
import sys

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


src = open("app.py").read()


def extract_function(name, src=src):
    """Slice out one top-level `def name(...):` body up to (but not
    including) the next top-level `def `/`@app.route` line at column 0 --
    good enough for this file's real, consistent formatting (every
    top-level def/decorator starts at column 0, nothing inside a function
    body does)."""
    m = re.search(rf"\ndef {re.escape(name)}\(.*?\n(?=\ndef |\n@app\.)", src, re.DOTALL)
    if not m:
        raise AssertionError(f"could not locate function {name}() in app.py")
    return m.group(0)


search_parcels_src = extract_function("search_parcels_by_address")
resolve_exact_src = extract_function("resolve_exact_parcel")
resolve_quick_search_src = extract_function("_resolve_quick_search")
home_search_response_src = extract_function("_home_search_response")


# ─────────────────────────────────────────────────────────────────────────
# Section 1: search_parcels_by_address() county scoping
# ─────────────────────────────────────────────────────────────────────────
section("search_parcels_by_address() -- county scoping")

check("main ILIKE query's WHERE clause includes county_code = %(county_code)s",
      "AND  county_code = %(county_code)s" in search_parcels_src)
check("county_code param is bound to target_county in the query() call",
      '"county_code": target_county' in search_parcels_src)
check("neutral (county_code=None) branch loops over _live_counties() rather "
      "than ever issuing an unscoped query",
      "for entry in _live_counties():" in search_parcels_src
      and "search_parcels_by_address(q, limit=limit, county_code=entry[\"county_code\"])" in search_parcels_src)
check("geo_id NOT LIKE 'AJR%%' (real-property-only) still applied alongside "
      "the county_code scoping (D3 convention, unrelated to this brief but "
      "a real regression risk if this WHERE clause is ever touched again)",
      "geo_id NOT LIKE 'AJR%%'" in search_parcels_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 2: resolve_exact_parcel() county scoping
# ─────────────────────────────────────────────────────────────────────────
section("resolve_exact_parcel() -- county scoping")

exact_match_where_clauses = re.findall(
    r"SELECT \* FROM parcel WHERE geo_id = %s AND county_code = %s", resolve_exact_src)
check("both real lookups (direct geo_id match, and the prop_id-fallback "
      "retry) carry AND county_code = %s -- found "
      f"{len(exact_match_where_clauses)} (expected 2)",
      len(exact_match_where_clauses) == 2)
check("neutral (county_code=None) branch loops over _live_counties() rather "
      "than ever issuing an unscoped query",
      "for entry in _live_counties():" in resolve_exact_src
      and "resolve_exact_parcel(q, county_code=entry[\"county_code\"])" in resolve_exact_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 3: PX-20260828-03 Task 2 -- per-row county_name
# ─────────────────────────────────────────────────────────────────────────
section("Task 2 -- per-row county_name on search_parcels_by_address()'s output")

check('county_name derived once per county via COUNTY_PROFILES, same call '
      'shape as the pre-existing county_slug derivation just above it',
      'county_name = COUNTY_PROFILES.get(target_county, COUNTY_PROFILES["TRAVIS"])["county_name"]'
      in search_parcels_src)
check('every returned row is stamped with r["county_name"] = county_name, '
      'right alongside the pre-existing r["county_slug"] = county_slug line',
      'r["county_slug"] = county_slug' in search_parcels_src
      and 'r["county_name"] = county_name' in search_parcels_src)

api_address_search_src = extract_function("api_address_search")
check("api_address_search()'s address-match branch reads county_name off "
      "each row (r.get(\"county_name\", ...)) instead of only ever using "
      "its own separately-derived, request-wide county_name -- the "
      "redundant-derivation cleanup this task called for",
      '"county_name": r.get("county_name", county_name)' in api_address_search_src)
check("api_address_search()'s exact-match branch still returns a county_name "
      "(resolve_exact_parcel()'s dict has no county_name field of its own "
      "to read, unlike search_parcels_by_address()'s rows)",
      api_address_search_src.count('"county_name": county_name') == 1)


# ─────────────────────────────────────────────────────────────────────────
# Section 4: PX-20260828-03 Task 1 -- unification via one shared resolver
# ─────────────────────────────────────────────────────────────────────────
section("Task 1 -- _resolve_quick_search() is the one shared resolver")

check("_resolve_quick_search() exists and still calls resolve_exact_parcel() "
      "then search_parcels_by_address() -- the same two shared functions "
      "audited above, not a reimplementation",
      "resolve_exact_parcel(q, county_code=county_code)" in resolve_quick_search_src
      and "search_parcels_by_address(q, limit=20, county_code=county_code)" in resolve_quick_search_src)
check("_home_search_response() (index()/home()) delegates to "
      "_resolve_quick_search() rather than re-resolving q itself",
      "_resolve_quick_search(q, county_code=county_code)" in home_search_response_src)

search_page_src = extract_function("search_page")
search_landing_src = extract_function("search_landing")
check("search_page() (the anchored /<county_slug>/search route) resolves "
      "q via _resolve_quick_search(q, county_code=g.county_code) -- the "
      "actual PX-20260828-03 Task 1 fix, previously absent entirely",
      "_resolve_quick_search(q, county_code=g.county_code)" in search_page_src)
check("search_landing() (the neutral bare /search route) resolves q via "
      "_resolve_quick_search(q, county_code=None) -- closes the SECOND, "
      "worse Travis-only variant of this same bug found during Task 1's "
      "read-first investigation",
      "_resolve_quick_search(q, county_code=None)" in search_landing_src)
search_html_src = open("templates/search.html").read()
check("search.html's \"Find a Parcel\" form no longer targets the old bare "
      "url_for('index') (the actual live bug -- app.py's own docstrings "
      "quote that old string as explanatory history, which is why this "
      "check reads the template, not app.py)",
      'action="{{ url_for(\'index\') }}"' not in search_html_src)
check("search.html's form now submits back to its own current route "
      "(action=\"{{ request.path }}\"), so both search_page() and "
      "search_landing() resolve it via _resolve_quick_search() themselves",
      'action="{{ request.path }}"' in search_html_src)


print()
print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
sys.exit(0 if all_ok else 1)
