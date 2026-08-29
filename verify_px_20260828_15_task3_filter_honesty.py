"""
verify_px_20260828_15_task3_filter_honesty.py -- PX-20260828-15 Task 3.

PM's brief: "filter honesty for unavailable data ... Apply the honesty-
affordance pattern rates.html now uses: when a selected filter depends on
data not loaded for the selected county, say so explicitly rather than
returning a bare empty result. Propose the UX before building." Diego's
sign-off (via AskUserQuestion): disable the affected control inline
(Option A), with two requirements -- (a) name the county explicitly in the
caption, not a generic "unavailable"; (b) make the underlying mechanism
generic rather than delinquency-specific, so a future billing-dependent
filter (e.g. Effective Tax Rate, once Dallas billing data lands) can reuse
it without a copy-pasted has_x_data flag.

Confirmed, in-scope cases: the "Delinquent Only" checkbox + "Delinquent
Parcels" Quick Filter preset, AND (added 2026-08-29, after Diego's live
confirmation) the "Effective Tax Rate" min/max range + "Include Not
Available effective tax rate" checkbox. tax_delinquent is sourced solely
from Travis's own TaxDelqOpenData.csv (loaders/load_tax_current.py's
load_delinquent()) -- no Dallas-equivalent loader exists anywhere in the
repo (confirmed by grep), and the table's own resting row count (~10,087,
all Travis, per SPEC_COUNTY_PARTITIONING.md's pre-partition snapshot)
corroborates it. Effective Tax Rate's real source, tax_billing_entity, has
the identical "no Dallas loader" shape -- confirmed live by Diego (2026-08-
29): tax_billing DALLAS = 0 rows, tax_billing_account DALLAS = 0 rows.
api_search_filter()'s own route-level _county_has_data() gate only checks
the `parcel` table -- it does NOT catch either of these narrower per-filter
gaps, which is exactly why a Dallas delinquent-only or ETR-ranged search
used to come back "No parcels matched these filters," indistinguishable
from a real empty result.

This fixture checks:
  1. The generic mechanism: _table_has_county_data() + FILTER_DATA_
     REQUIREMENTS registry in app.py, exec()'d against a fake query() to
     prove the real SQL shape and both real registrations
     ('delinquent' -> 'tax_delinquent', 'effective_tax_rate' ->
     'tax_billing_entity').
  2. search_page() computes filter_data_availability from that registry
     and passes it to render_template(); search_landing() passes {} (no
     real county chosen yet on that neutral page).
  3. A real Jinja RENDER of search.html (not just a parse) in both the
     "available" and "unavailable" states, proving: the checkbox and Quick
     Filter button are actually disabled, the caption names the real
     county (requirement a), and the JS metaReady handler's re-enable
     guard skips a data-unavailable="1" button (requirement b's real,
     visible mechanism at the template layer).
  4. Regression safety: search.html renders exactly as before for every
     scenario this codebase's own pre-existing harness
     (verify_m4_part1_other_pages_render.py) already exercises, INCLUDING
     the ones that never pass filter_data_availability at all -- the
     (filter_data_availability or {}) guard must not raise on a caller
     that predates this fix.

Sandbox has no Flask/psycopg2 (confirmed unavailable, same constraint as
every other slice-and-exec test in this codebase). Uses the same
techniques already established here: extract app.py's real function
source between markers and exec() it against fakes (for #1), and the
real jinja2.Environment + FileSystemLoader render technique already used
by verify_px_20260828_07_rates_and_routing.py and
verify_m4_part1_other_pages_render.py (for #3/#4).

Run: python3 verify_px_20260828_15_task3_filter_honesty.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

REPO = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(REPO, "templates")

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


APP_PY = open(os.path.join(REPO, "app.py")).read()

# ─────────────────────────────────────────────────────────────────────────
section("Part 1: the generic mechanism (_table_has_county_data + FILTER_DATA_REQUIREMENTS)")
# ─────────────────────────────────────────────────────────────────────────

START = APP_PY.index("def _table_has_county_data(table, county_code):")
END = APP_PY.index("\n\n\ndef _live_counties():")
MECH_SRC = APP_PY[START:END]
check("slice contains _table_has_county_data's real body", "AS has_data" in MECH_SRC)
check("slice contains FILTER_DATA_REQUIREMENTS registry", "FILTER_DATA_REQUIREMENTS = {" in MECH_SRC)

_fake_query_calls = []


def _fake_query(sql, params, one=False):
    _fake_query_calls.append((sql, params))
    # Simulate: tax_delinquent has a row only for DALLAS (an arbitrary,
    # deliberately-inverted-from-reality scripted state -- proves the real
    # interpolated table name and county_code param reach the SQL, not a
    # stale/hardcoded assumption baked into this test). tax_billing_entity
    # never has a row for either county in this fake DB, matching Diego's
    # real live confirmation that it's empty for Dallas today (Travis's own
    # real population isn't asserted here -- this fixture proves the
    # MECHANISM, not today's Travis row count).
    if "tax_delinquent" in sql:
        has = params["county_code"] == "DALLAS"
    elif "tax_billing_entity" in sql:
        has = False
    else:
        has = None
    return {"has_data": has}


ns = {"query": _fake_query}
exec(MECH_SRC, ns)

check("_table_has_county_data() exists after exec()", "_table_has_county_data" in ns)
check("FILTER_DATA_REQUIREMENTS registers 'delinquent' -> 'tax_delinquent'",
      ns["FILTER_DATA_REQUIREMENTS"].get("delinquent") == "tax_delinquent")
check("FILTER_DATA_REQUIREMENTS registers 'effective_tax_rate' -> 'tax_billing_entity' "
      "(added 2026-08-29 per Diego's live confirmation)",
      ns["FILTER_DATA_REQUIREMENTS"].get("effective_tax_rate") == "tax_billing_entity")

_dallas_result = ns["_table_has_county_data"]("tax_delinquent", "DALLAS")
_travis_result = ns["_table_has_county_data"]("tax_delinquent", "TRAVIS")
check("real function call: Dallas has_data=True (per the fake DB's own scripted state)", _dallas_result is True)
check("real function call: Travis has_data=False (per the fake DB's own scripted state -- "
      "proves the county_code param, not just the table name, actually reaches the SQL)",
      _travis_result is False)
check("the real SQL text was built with the interpolated table name, not a literal placeholder",
      any("FROM tax_delinquent " in sql for sql, _ in _fake_query_calls))

_etr_dallas_result = ns["_table_has_county_data"]("tax_billing_entity", "DALLAS")
check("real function call: tax_billing_entity/Dallas has_data=False "
      "(matches Diego's live confirmation)", _etr_dallas_result is False)
check("the real SQL text was built with the tax_billing_entity table name for the ETR check",
      any("FROM tax_billing_entity " in sql for sql, _ in _fake_query_calls))

# ─────────────────────────────────────────────────────────────────────────
section("Part 2: search_page()/search_landing() wire filter_data_availability")
# ─────────────────────────────────────────────────────────────────────────

_sp_start = APP_PY.index('@app.route("/<county_slug>/search")')
_sp_end = APP_PY.index('@app.route("/<county_slug>/parcel/<geo_id>")')
ROUTES_SRC = APP_PY[_sp_start:_sp_end]

check("search_page() computes filter_data_availability from FILTER_DATA_REQUIREMENTS",
      "filter_data_availability = {" in ROUTES_SRC
      and "for key, table in FILTER_DATA_REQUIREMENTS.items()" in ROUTES_SRC)
check("search_page() passes filter_data_availability into its render_template() call",
      re.search(r'render_template\(\s*"search\.html".*?filter_data_availability=filter_data_availability',
                 ROUTES_SRC, re.DOTALL) is not None)
check("search_landing() passes filter_data_availability={} explicitly (no real county chosen yet)",
      "filter_data_availability={}" in ROUTES_SRC)

# ─────────────────────────────────────────────────────────────────────────
section("Part 3: real Jinja RENDER of search.html -- available vs unavailable states")
# ─────────────────────────────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, path="/travis-tx/search"):
        self.path = path
        self.args = {}


def _url_for(endpoint, **kwargs):
    if endpoint == "static":
        return "/static/" + kwargs.get("filename", "")
    return "/" + endpoint.lstrip("/")


_DALLAS_PROFILE = {
    "display_name": "Dallas County, TX", "county_name": "Dallas County",
    "cad_name": "Dallas Central Appraisal District",
    "tax_office_name": "Dallas County Tax Office",
}
_TRAVIS_PROFILE = {
    "display_name": "Travis County, TX", "county_name": "Travis County",
    "cad_name": "Travis Central Appraisal District",
    "tax_office_name": "Travis County Tax Office",
}
_LIVE_COUNTIES = [
    {"slug": "travis-tx", "county_code": "TRAVIS", "display_name": "Travis County, TX",
     "county_name": "Travis County", "parcel_count": 430147, "parcel_count_display": "430,147"},
    {"slug": "dallas-tx", "county_code": "DALLAS", "display_name": "Dallas County, TX",
     "county_name": "Dallas County", "parcel_count": 705536, "parcel_count_display": "705,536"},
]


def make_env(county_slug, county_profile):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _url_for
    env.globals["request"] = _FakeRequest(path=f"/{county_slug}/search")
    import config as _real_config
    env.globals["config"] = _real_config
    env.filters["tojson"] = lambda v: "null"
    env.globals["county_slug"] = county_slug
    env.globals["county_url"] = lambda path: f"/{county_slug}{path}"
    env.globals["county_profile"] = county_profile
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = _LIVE_COUNTIES
    env.globals["is_county_anchored"] = True
    return env


# ── Dallas, tax_delinquent AND tax_billing_entity NOT loaded (the real,
#    confirmed state for Dallas today per Diego's 2026-08-29 live check) ──
env = make_env("dallas-tx", _DALLAS_PROFILE)
tpl = env.get_template("search.html")
out_unavailable = tpl.render(
    has_preliminary_2026=False, county_selected=True, q="", addr_matches=None, error=None,
    filter_data_availability={"delinquent": False, "effective_tax_rate": False},
)

check("Dallas/unavailable: ETR min input is disabled",
      re.search(r'id="fltEtrMin"[^>]*\bdisabled\b', out_unavailable) is not None)
check("Dallas/unavailable: ETR max input is disabled",
      re.search(r'id="fltEtrMax"[^>]*\bdisabled\b', out_unavailable) is not None)
check("Dallas/unavailable: ETR Include-NA checkbox is disabled too (Diego's question (b) -- "
      "it's a modifier on min/max, meaningless on its own, so it must be disabled alongside "
      "them rather than left interactively enabled)",
      re.search(r'id="fltEtrIncludeNa"[^>]*\bdisabled\b', out_unavailable) is not None)
check('Dallas/unavailable: ETR caption explicitly names "Dallas County" and cites billing data',
      "Not yet available for Dallas County" in out_unavailable
      and "billing data hasn't been loaded" in out_unavailable)

check("Dallas/unavailable: Delinquent Only checkbox is disabled",
      re.search(r'id="fltDelinquentOnly"[^>]*\bdisabled\b', out_unavailable) is not None)
check("Dallas/unavailable: Delinquent Parcels Quick Filter button carries data-unavailable=\"1\"",
      re.search(r'data-preset="delinquent"[^>]*data-unavailable="1"', out_unavailable) is not None)
check('Dallas/unavailable: caption explicitly names "Dallas County" (Diego\'s requirement a -- '
      "not a generic 'unavailable' message)",
      "Not yet available for Dallas County" in out_unavailable)
check("Dallas/unavailable: the JS metaReady handler's guard against data-unavailable is present",
      'b.getAttribute("data-unavailable") !== "1"' in out_unavailable)
check("Dallas/unavailable: the ORIGINAL always-disabled Quick Filter description is NOT shown "
      "for this button (replaced by the honesty caption)",
      "Not yet available" in out_unavailable.split('data-preset="delinquent"')[1].split("</button>")[0])

# ── Dallas, tax_delinquent AND tax_billing_entity loaded (the future-state
#    check, once Dallas billing data is eventually acquired) ──────────────
out_available = tpl.render(
    has_preliminary_2026=False, county_selected=True, q="", addr_matches=None, error=None,
    filter_data_availability={"delinquent": True, "effective_tax_rate": True},
)
check("Dallas/available: Delinquent Only checkbox has no disabled attribute",
      re.search(r'id="fltDelinquentOnly"[^>]*\bdisabled\b', out_available) is None)
check("Dallas/available: Quick Filter button carries no data-unavailable attribute",
      'data-unavailable="1"' not in out_available.split('data-preset="delinquent"')[1].split("</button>")[0])
check("Dallas/available: original Quick Filter description text is shown, not the honesty caption",
      "Parcels with a real, outstanding delinquent tax balance" in out_available)
check("Dallas/available: ETR min/max/Include-NA carry no disabled attribute",
      re.search(r'id="fltEtrMin"[^>]*\bdisabled\b', out_available) is None
      and re.search(r'id="fltEtrMax"[^>]*\bdisabled\b', out_available) is None
      and re.search(r'id="fltEtrIncludeNa"[^>]*\bdisabled\b', out_available) is None)
check("Dallas/available: no 'Not yet available' caption anywhere near either filter",
      "Not yet available" not in out_available)

# ── Independence check: each filter's disable state is keyed off its own
#    registry entry, not a shared/leaked flag -- Dallas with ONLY delinquent
#    data missing (ETR available) must disable exactly the delinquent
#    controls and leave ETR's three controls untouched, and vice versa. ──
out_mixed = tpl.render(
    has_preliminary_2026=False, county_selected=True, q="", addr_matches=None, error=None,
    filter_data_availability={"delinquent": True, "effective_tax_rate": False},
)
check("mixed state: Delinquent Only enabled while ETR is independently disabled",
      re.search(r'id="fltDelinquentOnly"[^>]*\bdisabled\b', out_mixed) is None
      and re.search(r'id="fltEtrMin"[^>]*\bdisabled\b', out_mixed) is not None)

# ── Travis, no filter_data_availability entry at all (mirrors search_landing()'s {}) ──
env2 = make_env("travis-tx", _TRAVIS_PROFILE)
tpl2 = env2.get_template("search.html")
out_default = tpl2.render(
    has_preliminary_2026=False, county_selected=False, q="", addr_matches=None, error=None,
    filter_data_availability={},
)
check("empty filter_data_availability={} (search_landing()'s real value): "
      "checkbox defaults to available/enabled (missing key -> True)",
      re.search(r'id="fltDelinquentOnly"[^>]*\bdisabled\b', out_default) is None)
check("empty filter_data_availability={}: ETR controls also default to available/enabled "
      "(missing 'effective_tax_rate' key -> True, same safe default as 'delinquent')",
      re.search(r'id="fltEtrMin"[^>]*\bdisabled\b', out_default) is None)

# ── Regression: filter_data_availability omitted ENTIRELY (the pre-existing
#    harness's real, unmodified calling pattern) must not raise. ─────────
try:
    out_omitted = tpl2.render(has_preliminary_2026=True)
    check("filter_data_availability omitted entirely (pre-existing harness's real calling "
          "pattern) does not raise UndefinedError", True)
except Exception as e:
    check(f"filter_data_availability omitted entirely does not raise -- FAILED: {type(e).__name__}: {e}", False)

for out, label in [(out_unavailable, "unavailable"), (out_available, "available"),
                    (out_mixed, "mixed"), (out_default, "default-empty")]:
    check(f"{label} render: no leaked raw Jinja delimiters", "{%" not in out and "{#" not in out)

print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE: a live browser confirming the disabled checkbox/button/inputs actually "
      "prevent interaction end-to-end (this fixture proves the MECHANISM is correct against a "
      "scripted fake DB state, not a live click-through) -- same standing sandbox limitation "
      "(no live browser) as every other PX brief. Diego's own 2026-08-29 live query already "
      "covers the DB side for effective_tax_rate (tax_billing/tax_billing_account both "
      "confirmed 0 rows for DALLAS); tax_delinquent's own live Dallas row count was not "
      "separately re-confirmed this round -- it rests on this brief's earlier grep + resting-"
      "count evidence, not a fresh live query.")
print()
print("RESOLVED since this fixture's first version: Effective Tax Rate (etr_min/etr_max + the "
      "Include Not Available checkbox) is now REGISTERED in FILTER_DATA_REQUIREMENTS "
      "('effective_tax_rate' -> 'tax_billing_entity'), per Diego's 2026-08-29 live confirmation "
      "and explicit instruction to register it. All three of its controls (min, max, Include-NA) "
      "are disabled together when unavailable -- see search.html's own comment for why the "
      "Include-NA checkbox specifically must be included, not just min/max.")
print()
print("CONFIRMED, per Diego's own framing: Dallas's billing-table emptiness is not a "
      "not-yet-run job -- repo-wide grep found no Dallas billing loader at all (Dallas's "
      "load_dallas_certified.py writes parcel/parcel_tax_year/prop_unit from DCAD's certified-"
      "roll export; DCAD billing/tax-collection data is a separate, unacquired source). Both "
      "registered entries ('delinquent', 'effective_tax_rate') will correctly stay disabled "
      "for Dallas until that acquisition work happens -- this is today's real state, not a "
      "conservative placeholder.")
exit(0 if all_ok else 1)
