"""
verify_px_20260829_01_search_result_county.py -- PX-20260829-01.

PM's live bug report: on /about (a neutral page, COUNTY_BASE === ""),
typing "2626 cartwright" correctly returns and tags the Dallas parcel, but
clicking it navigated to /travis-tx/parcel/<id> -- the PAGE's county, not
the RESULT's -- because parcel-typeahead.js built the navigate target from
global.COUNTY_BASE, and the API response carried county_name (for the
display tag) but never county_slug, leaving the JS no way to navigate to
the result's own county even if it had wanted to.

Fix (this file verifies the BACKEND half; see
verify_px_20260829_01_typeahead_result_county.js, run under Node, for the
FRONTEND half -- same real-source-extraction technique this session's
other app.py fixtures already use, since this sandbox has no Flask):
  1. api_address_search() (anchored /<county_slug>/api/address_search) now
     stamps county_slug on every result -- g.county_slug on the exact-match
     branch (this endpoint's resolve_exact_parcel(q) call is always scoped
     to g.county_code, so it can never resolve to a different county), and
     r.get("county_slug", g.county_slug) on the address-match branch
     (search_parcels_by_address() already stamps county_slug per row).
  2. api_address_search_landing() (neutral /api/address_search) now stamps
     county_slug too -- derived via a COUNTY_SLUGS reverse lookup from the
     resolved parcel's own county_code on the exact-match branch (this is
     the endpoint that can genuinely span multiple counties per call, so
     there's no single g.county_slug to fall back to), and r.get(
     "county_slug") on the address-match branch (search_parcels_by_address()
     with county_code=None already stamps a real per-row value).

Also verifies (Task 2 of this same brief): the three occurrences of the
Travis-specific "10-digit TCAD account number" error copy (found across
_resolve_quick_search()'s two error branches AND property_detail()'s own
404 branch -- the third was found DURING this fix, not previously audited)
are gone from the real shipping source, replaced by county-agnostic
"parcel ID (from your county's appraisal district or tax office)" phrasing
consistent with index.html/search.html's own PX-20260828-09 Task 4 hint
text.

Technique: regex-slice the real function bodies out of app.py (same
markers-based extraction this session's other fixtures use), exec() each
against fakes for g/COUNTY_PROFILES/COUNTY_SLUGS/DEFAULT_COUNTY_SLUG/
resolve_exact_parcel/search_parcels_by_address/search_logic/request/
jsonify/app.route/limiter.limit -- proving the REAL interpolation/lookup
logic runs, not a reimplementation of it.

Run: python3 verify_px_20260829_01_search_result_county.py
"""
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
APP_PY = open(os.path.join(REPO, "app.py")).read()

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ─────────────────────────────────────────────────────────────────────────
section("Part 1: extract the real api_address_search()/api_address_search_landing() source")
# ─────────────────────────────────────────────────────────────────────────

start = APP_PY.index('@app.route("/<county_slug>/api/address_search")')
end = APP_PY.index('@app.route("/<county_slug>/api/peer_benchmark_local/<geo_id>")')
SRC = APP_PY[start:end]

check("slice contains api_address_search()'s real body", "def api_address_search():" in SRC)
check("slice contains api_address_search_landing()'s real body", "def api_address_search_landing():" in SRC)
check("anchored endpoint's exact-match branch stamps county_slug from g.county_slug",
      '"county_slug": g.county_slug,' in SRC)
check("anchored endpoint's address-match branch reads county_slug off the row, falling back to g.county_slug",
      'r.get("county_slug", g.county_slug)' in SRC)
check("neutral endpoint's exact-match branch derives county_slug via a COUNTY_SLUGS reverse lookup",
      "exact_county_slug = next(" in SRC and "for slug, code in COUNTY_SLUGS.items()" in SRC)
check("neutral endpoint's address-match branch reads county_slug straight off the row",
      '"county_slug": r.get("county_slug"),' in SRC)

# ─────────────────────────────────────────────────────────────────────────
section("Part 2: exec() the real source against fakes -- anchored endpoint (Dallas page)")
# ─────────────────────────────────────────────────────────────────────────


class FakeApp:
    def route(self, *a, **k):
        return lambda fn: fn


class FakeLimiter:
    def limit(self, *a, **k):
        return lambda fn: fn


class FakeG:
    county_code = "DALLAS"
    county_slug = "dallas-tx"


class FakeArgs(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


class FakeRequest:
    def __init__(self, q):
        self.args = FakeArgs(q=q)


class FakeSearchLogic:
    @staticmethod
    def is_account_number_query(q):
        return q.isdigit() or q.startswith("ACCT")


_jsonify_calls = []


def fake_jsonify(payload):
    _jsonify_calls.append(payload)
    return payload  # this fixture reads the dict directly, no real Flask Response needed


COUNTY_PROFILES = {
    "TRAVIS": {"county_name": "Travis County"},
    "DALLAS": {"county_name": "Dallas County"},
}
COUNTY_SLUGS = {"travis-tx": "TRAVIS", "dallas-tx": "DALLAS"}
DEFAULT_COUNTY_SLUG = "travis-tx"


def make_ns(resolve_exact_parcel_fn, search_parcels_by_address_fn):
    return {
        "app": FakeApp(),
        "limiter": FakeLimiter(),
        "_LIMIT_TYPEAHEAD": None,
        "g": FakeG(),
        "request": None,  # set per-call below
        "jsonify": fake_jsonify,
        "COUNTY_PROFILES": COUNTY_PROFILES,
        "COUNTY_SLUGS": COUNTY_SLUGS,
        "DEFAULT_COUNTY_SLUG": DEFAULT_COUNTY_SLUG,
        "search_logic": FakeSearchLogic(),
        "resolve_exact_parcel": resolve_exact_parcel_fn,
        "search_parcels_by_address": search_parcels_by_address_fn,
        # PX-20260829-04 added real time.perf_counter()-based timing
        # instrumentation (_log_typeahead_timing) directly inside these two
        # endpoint bodies, guarded by has_request_context(). This fixture
        # tests county_slug/county_name plumbing, not timing instrumentation,
        # so `time` is the real module (harmless, just a clock) and the other
        # two are no-op stubs: has_request_context() -> False so the timing
        # branch's own internals don't need a real Flask app context, and
        # _log_typeahead_timing is a no-op so its print() diagnostic doesn't
        # fire during this unrelated test's output.
        "time": time,
        "has_request_context": lambda: False,
        "_log_typeahead_timing": lambda *a, **k: None,
    }


# ── Scenario A: anchored endpoint, address-text match (the PM's own real
#    repro shape once this endpoint is reached via /dallas-tx/...) ────────
def _search_parcels_dallas_match(q, limit=8):
    return [{
        "geo_id": "32130500090190000", "situs_address": "2626 CARTWRIGHT RD",
        "owner_name": "SOMEOWNER LLC", "county_name": "Dallas County",
        "county_slug": "dallas-tx",
    }]


ns = make_ns(resolve_exact_parcel_fn=lambda q: None,
             search_parcels_by_address_fn=_search_parcels_dallas_match)
ns["request"] = FakeRequest("2626 cartwright")
exec(SRC, ns)
result = ns["api_address_search"]()
check("Scenario A: anchored endpoint address-match result carries county_slug='dallas-tx'",
      result["results"][0]["county_slug"] == "dallas-tx")
check("Scenario A: county_name still present alongside county_slug (no regression)",
      result["results"][0]["county_name"] == "Dallas County")

# ── Scenario B: anchored endpoint, exact account-number match ─────────────
def _resolve_exact_dallas(q):
    return {"geo_id": "32130500090190000", "situs_address": "2626 CARTWRIGHT RD",
            "owner_name": "SOMEOWNER LLC", "county_code": "DALLAS"}


ns2 = make_ns(resolve_exact_parcel_fn=_resolve_exact_dallas,
              search_parcels_by_address_fn=lambda q, limit=8: [])
ns2["request"] = FakeRequest("ACCT12345670000000")
exec(SRC, ns2)
result_b = ns2["api_address_search"]()
check("Scenario B: anchored endpoint exact-match result carries county_slug='dallas-tx' (from g.county_slug)",
      result_b["results"][0]["county_slug"] == "dallas-tx")

# ─────────────────────────────────────────────────────────────────────────
section("Part 3: exec() the real source against fakes -- neutral landing endpoint")
# ─────────────────────────────────────────────────────────────────────────


# ── Scenario C (the confirmed live bug's real shape): neutral endpoint,
#    address-text match finds a DALLAS parcel from a page with no county
#    context at all -- this is exactly /about's own real call. ───────────
def _search_parcels_neutral_dallas(q, limit=8, county_code=None):
    assert county_code is None, "neutral endpoint must call with county_code=None"
    return [{
        "geo_id": "32130500090190000", "situs_address": "2626 CARTWRIGHT RD",
        "owner_name": "SOMEOWNER LLC", "county_name": "Dallas County",
        "county_slug": "dallas-tx",
    }]


ns3 = make_ns(resolve_exact_parcel_fn=lambda q, county_code=None: None,
              search_parcels_by_address_fn=_search_parcels_neutral_dallas)
ns3["request"] = FakeRequest("2626 cartwright")
exec(SRC, ns3)
result_c = ns3["api_address_search_landing"]()
check("Scenario C (the live bug's exact repro shape): neutral endpoint address-match "
      "result carries county_slug='dallas-tx', not Travis and not absent",
      result_c["results"][0]["county_slug"] == "dallas-tx")

# ── Scenario D: neutral endpoint, exact account-number match resolves to
#    Dallas -- county_slug must be DERIVED from the parcel's own
#    county_code, not any caller-side default. ────────────────────────────
def _resolve_exact_neutral_dallas(q, county_code=None):
    assert county_code is None, "neutral endpoint must call resolve_exact_parcel with county_code=None"
    return {"geo_id": "32130500090190000", "situs_address": "2626 CARTWRIGHT RD",
            "owner_name": "SOMEOWNER LLC", "county_code": "DALLAS"}


ns4 = make_ns(resolve_exact_parcel_fn=_resolve_exact_neutral_dallas,
              search_parcels_by_address_fn=lambda q, limit=8, county_code=None: [])
ns4["request"] = FakeRequest("ACCT12345670000000")
exec(SRC, ns4)
result_d = ns4["api_address_search_landing"]()
check("Scenario D: neutral endpoint exact-match result carries county_slug='dallas-tx' "
      "(reverse-derived from the resolved parcel's own county_code, not a caller default)",
      result_d["results"][0]["county_slug"] == "dallas-tx")

# ── Scenario E: neutral endpoint, exact match resolves to TRAVIS -- proves
#    the derivation isn't hardcoded to always return Dallas or always fall
#    back to DEFAULT_COUNTY_SLUG regardless of the real county_code. ──────
def _resolve_exact_neutral_travis(q, county_code=None):
    return {"geo_id": "0100030109", "situs_address": "123 MAIN ST",
            "owner_name": "SOMEONE", "county_code": "TRAVIS"}


ns5 = make_ns(resolve_exact_parcel_fn=_resolve_exact_neutral_travis,
              search_parcels_by_address_fn=lambda q, limit=8, county_code=None: [])
ns5["request"] = FakeRequest("0100030109")
exec(SRC, ns5)
result_e = ns5["api_address_search_landing"]()
check("Scenario E: neutral endpoint exact-match result correctly derives county_slug='travis-tx' "
      "for a Travis parcel (not a hardcoded Dallas value)",
      result_e["results"][0]["county_slug"] == "travis-tx")

# ─────────────────────────────────────────────────────────────────────────
section("Part 4: real shipping source no longer has the three Travis-specific error strings")
# ─────────────────────────────────────────────────────────────────────────

_STALE_ERROR_STRINGS = [
    "Try a shorter street name or use the 10-digit TCAD account number.",
    "the 10-digit TCAD account number works most reliably.",
]
check("app.py's live error-message strings no longer contain the stale Travis-specific "
      "copy (the two exact strings this fix replaced) -- 'TCAD account number' still "
      "appears in this fix's own explanatory comments, which is expected and fine",
      all(s not in APP_PY for s in _STALE_ERROR_STRINGS))

# NOTE: APP_PY is the raw SOURCE TEXT of app.py, not a runtime-evaluated
# string -- adjacent string-literal fragments (e.g. two lines that Python
# concatenates at parse time into one string) are NOT textually contiguous
# in the source itself (there's a real newline/indentation/quote-char gap
# between them). So each check below matches one literal FRAGMENT as it
# actually appears on its own source line, not the runtime-concatenated
# sentence -- Part 2/3's exec()-and-call scenarios above are what actually
# prove the runtime-concatenated strings are correct end-to-end for the
# two fields that matter operationally (results[].county_slug); these
# three checks exist only to confirm the copy fix's wording landed, which
# a per-fragment source match already does honestly.
check("_resolve_quick_search()'s address-miss branch now uses the generic parcel-ID phrasing",
      "search by your parcel ID (from your county's" in APP_PY
      and "appraisal district or tax office) instead." in APP_PY)
check("_resolve_quick_search()'s numeric-miss branch now uses the generic parcel-ID phrasing",
      "this accepts a parcel ID from your county's appraisal" in APP_PY
      and "district or tax office, not just a street address." in APP_PY)
check("property_detail()'s 404 branch (a third, previously-unaudited occurrence, found during this fix) "
      "now uses the generic parcel-ID phrasing too",
      APP_PY.count("this accepts a parcel ID from your county's") >= 2  # both _resolve_quick_search AND property_detail
      and 'appraisal district or tax office."' in APP_PY)

print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE: the frontend half (parcel-typeahead.js's select() actually using "
      "result.county_slug to navigate, and its explicit no-silent-fallback behavior when "
      "county_slug is absent) -- see verify_px_20260829_01_typeahead_result_county.js, run "
      "under Node's vm module, for that half. Also not proven: a live browser end-to-end "
      "click-through (/about -> type -> click -> lands on the real Dallas parcel page) -- "
      "same standing sandbox limitation (no live browser) as every other PX brief; this is "
      "the live check Diego's own brief already commits to doing post-deploy.")
print()
print("INVESTIGATION FINDING (not fixed, per the brief's own 'don't fix unless clearly "
      "wrong' instruction): /travis-tx/parcel/<a-real-but-different-county's-geo_id> -- "
      "property_detail()'s `parcel WHERE geo_id = %s AND county_code = %s` query returns no "
      "row (county_code mismatch), which is INDISTINGUISHABLE, in this route, from the "
      "geo_id not existing in ANY county -- both hit the same 404 branch, rendering "
      "index.html with a real HTTP 404 status and a real, now-generic explanatory error "
      "message (this is already a genuine 404, not a raw crash or a blank page). What it "
      "does NOT do is try other live counties and offer/redirect to the correct one, the way "
      "_resolve_quick_search()'s own search-box path already does for a TYPED query. "
      "Judgment call: this is a real gap (a nicer UX is possible) but not a CLEARLY WRONG "
      "one -- the current behavior is honest (real 404, real explanation) rather than "
      "misleading, and adding a cross-county probe to every failed direct parcel-URL request "
      "is a real feature addition (extra queries on the most likely-malformed-URL path, plus "
      "a UX decision about silent-redirect vs. suggestion-link) rather than a one-line fix. "
      "Left unbuilt, flagged here for Diego's own call on priority -- consistent with the "
      "brief's explicit instruction not to fix this unless it's clearly wrong.")
exit(0 if all_ok else 1)
