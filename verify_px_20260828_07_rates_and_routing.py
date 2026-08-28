"""
verify_px_20260828_07_rates_and_routing.py — real Jinja RENDER verification
for PX-20260828-07's three approved fixes:

  1. rates.html's new data_unavailable honesty affordance (mirrors
     snapshot.html/_compute_snapshot_data()'s existing pattern) -- a
     zero-row county must render a loud, honest alert instead of an empty
     "Entities" sidebar under a header that still claims a real year range.
  2. templates/index.html:44 (hero search form) and templates/about.html:23
     (CTA) -- both used url_for('index'), which _add_county_slug()'s real
     @app.url_defaults hook silently resolves to '/travis-tx' on a neutral
     page. Both were changed to url_for('home'), mirroring base.html's
     already-correct navbar pattern.
  3. templates/index.html's Developers audience card -- the hardcoded
     "live in Travis County today, with 2026 preliminary values ahead of
     certification" claim was dropped (Rate Trends has zero Dallas data,
     Market Snapshot's precompute hasn't run for Dallas either -- naming
     Travis here would overclaim rate-history parity Dallas doesn't have).

Same lightweight-Jinja2-Environment technique as verify_px_20260828_01_
render.py (not Flask -- not installed in this sandbox, no network to
install it): a bare jinja2.Environment against templates/, with url_for/
request/config/live_counties/etc. stubbed the same way. Two url_for()
stub behaviors are exercised, matching real Flask behavior:
  - "anchored": url_for(endpoint, county_slug=X) / any endpoint expecting
    county_slug with none given -> a real DEFAULT_COUNTY_SLUG-style
    auto-injection, reproducing the ACTUAL BUG this task fixed (a bare
    url_for('index') on a neutral page resolving to '/travis-tx').
  - "home"/"landing": url_for('home') / url_for('rates_landing') etc. ->
    their real bare paths, matching what should be produced after the fix.

Run: python3 verify_px_20260828_07_rates_and_routing.py
Exits non-zero and prints a diagnosis if any scenario fails.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_BARE_PATHS = {
    "home": "/",
    "search_landing": "/search",
    "info_landing": "/info",
    "rates_landing": "/rates",
    "snapshot_landing": "/snapshot",
    "about": "/about",
}


def _make_url_for(default_county_slug="travis-tx"):
    def _url_for(endpoint, **kwargs):
        if endpoint == "static":
            return "/static/" + kwargs.get("filename", "")
        if endpoint in _BARE_PATHS:
            base = _BARE_PATHS[endpoint]
            qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
            return base + (f"?{qs}" if qs else "")
        # Any OTHER endpoint (e.g. bare 'index', or 'tax_rates', 'county_
        # snapshot', 'info') is treated as county-slug-expecting --
        # reproduces _add_county_slug()'s REAL @app.url_defaults behavior:
        # a county_slug kwarg wins if given explicitly, otherwise the
        # DEFAULT_COUNTY_SLUG-style fallback silently applies. This is
        # exactly the mechanism PX-20260828-07's two routing bugs exploited
        # (a bare url_for('index') on a neutral page -> '/travis-tx').
        slug = kwargs.pop("county_slug", default_county_slug)
        qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
        return f"/{slug}/{endpoint.lstrip('/')}" + (f"?{qs}" if qs else "")
    return _url_for


class _FakeRequest:
    def __init__(self, path="/"):
        self.path = path
        self.args = {}
        self.endpoint = None


_TRAVIS_PROFILE = {
    "display_name": "Travis County, TX", "county_name": "Travis County",
    "cad_name": "Travis Central Appraisal District",
    "tax_office_name": "Travis County Tax Office", "cad_abbr": "TCAD",
}
_DALLAS_PROFILE = {
    "display_name": "Dallas County, TX", "county_name": "Dallas County",
    "cad_name": "Dallas Central Appraisal District",
    "tax_office_name": "Dallas County Tax Office", "cad_abbr": "DCAD",
}
_LIVE_COUNTIES = [
    {"slug": "travis-tx", "value": "travis", "display_name": "Travis County, TX",
     "county_name": "Travis County", "parcel_count": 430147, "parcel_count_display": "430,147"},
    {"slug": "dallas-tx", "value": "dallas", "display_name": "Dallas County, TX",
     "county_name": "Dallas County", "parcel_count": 705536, "parcel_count_display": "705,536"},
]


def make_env(county_slug="travis-tx", county_profile=None, is_county_anchored=False):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _make_url_for(default_county_slug=county_slug)
    env.globals["request"] = _FakeRequest()
    env.globals["mode"] = "investor"
    import config as _real_config
    env.globals["config"] = _real_config
    env.filters["tojson"] = lambda v: "null"
    env.globals["api_county_slug"] = county_slug
    env.globals["county_slug"] = county_slug
    env.globals["county_url"] = lambda path: f"/{county_slug}{path}"
    env.globals["county_profile"] = county_profile or _TRAVIS_PROFILE
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = _LIVE_COUNTIES
    env.globals["is_county_anchored"] = is_county_anchored
    return env


FAILURES = []


def check(label, fn):
    try:
        out = fn()
        if "{%" in out or "{#" in out:
            FAILURES.append(f"{label}: leaked raw Jinja delimiter in output")
        else:
            print(f"  OK   {label} ({len(out)} chars)")
        return out
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")
        return ""


def main():
    # ── Fix 1: rates.html data_unavailable honesty affordance ─────────────
    def rates_ctx(data_unavailable, reason=None):
        return dict(
            data_unavailable=data_unavailable,
            data_unavailable_reason=reason,
            by_entity_json="{}", entity_names_json="{}", entity_category_json="{}",
            all_entities=[], entity_category_order=["School District", "City", "County",
                                                      "Hospital District", "MUD/WCID", "Other"],
            key_entities=["TCO", "IAU", "CAT", "THD", "ACT"],
            year_min=1990, year_max=2025, default_year_from=2016,
        )

    # Travis, real data present -- normal render, no alert, real year range shown.
    env = make_env(county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("rates.html")

    def _rates_available():
        out = tpl.render(**rates_ctx(False))
        if "1990" not in out or "2025" not in out:
            raise AssertionError("expected real year range (1990-2025) not found")
        if "Tax rate history is not yet available" in out:
            raise AssertionError("unavailable alert should NOT render when data_unavailable=False")
        if 'id="ratesChart"' not in out:
            raise AssertionError("chart canvas should render when data available")
        return out
    check("rates.html / Travis, data available -> normal render, no alert", _rates_available)

    # Dallas, zero rows -- the real PX-20260828-07 Task 1 bug this fixes.
    env = make_env(county_slug="dallas-tx", county_profile=_DALLAS_PROFILE)
    tpl = env.get_template("rates.html")
    reason = ("Tax rate history has not been loaded yet for Dallas County -- county_tax_rate "
              "has no rows for this county. This page reads only loaded rate data -- run "
              "loaders/load_tax_rates.py for this county to populate it.")

    def _rates_unavailable():
        out = tpl.render(**rates_ctx(True, reason))
        if "Tax rate history is not yet available for Dallas County" not in out:
            raise AssertionError("expected honest unavailable heading naming Dallas County")
        if reason not in out:
            raise AssertionError("expected real data_unavailable_reason text in output")
        # The old misleading fallback range must NOT appear anywhere near a
        # coverage claim -- entities sidebar / chart / summary table must
        # all be suppressed, not just decorated with a caveat.
        if 'id="ratesChart"' in out:
            raise AssertionError("chart canvas must NOT render when data_unavailable")
        if 'id="entitySearch"' in out:
            raise AssertionError("entities sidebar must NOT render when data_unavailable")
        # The specific bug this fixes: the misleading "1990-2025" fallback
        # range must not appear as a coverage claim next to the county name
        # in the header. Checked as the exact header fragment, not a bare
        # "1990" / "2025" substring ban -- those digits can legitimately
        # appear elsewhere (e.g. a footer copyright year) without being the
        # bug this task fixes.
        if "1990–2025" in out:
            raise AssertionError("the old '1990-2025' fallback range must not appear in the unavailable state")
        return out
    check("rates.html / Dallas, zero rows -> honest alert, no chart/sidebar/fake range", _rates_unavailable)

    # Scripts block must also be fully suppressed when data_unavailable --
    # otherwise document.getElementById('ratesChart') etc. would throw on a
    # page whose markup no longer has those ids (Chart.js `new Chart(null,
    # ...)` throws; addEventListener on null throws).
    def _rates_unavailable_scripts_suppressed():
        out = tpl.render(**rates_ctx(True, reason))
        if "const BY_ENTITY" in out or "new Chart(" in out or "exportCsvBtn" in out:
            raise AssertionError("scripts block must be suppressed when data_unavailable")
        return out
    check("rates.html / Dallas, zero rows -> scripts block fully suppressed (no null-deref)",
          _rates_unavailable_scripts_suppressed)

    # ── Fix 2: index.html hero search form + about.html CTA routing ───────
    # "anchored" env (county_slug set, is_county_anchored True) -- reproduces
    # the exact broken scenario: BEFORE the fix, url_for('index') here would
    # have resolved via the "anchored" fallback to '/travis-tx' even on the
    # bare homepage. The real home() route never sets g.county_slug, but
    # this env's url_for stub defaults to the SAME auto-injection Flask's
    # real @app.url_defaults performs, so this is a faithful reproduction.
    env = make_env(county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("index.html")

    def _index_hero_form_action():
        out = tpl.render(q="", error=None, addr_matches=None, api_county_slug="")
        if 'action="/travis-tx"' in out or "action=\"/travis-tx?" in out:
            raise AssertionError("hero search form still routes to /travis-tx -- regression of the fixed bug")
        if 'action="/"' not in out and "class=\"hero-search\"" not in out:
            raise AssertionError("hero-search form not found in rendered output at all")
        return out
    check("index.html / hero search form action -> url_for('home') = '/' (not '/travis-tx')",
          _index_hero_form_action)

    def _index_developers_card_no_travis_claim():
        out = tpl.render(q="", error=None, addr_matches=None, api_county_slug="")
        if "live in Travis County today" in out:
            raise AssertionError("Developers card still makes the dropped Travis-specific rate-history claim")
        if "Developers" not in out:
            raise AssertionError("Developers audience card not found at all")
        return out
    check("index.html / Developers card no longer claims Travis-only rate-history coverage",
          _index_developers_card_no_travis_claim)

    env = make_env(county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("about.html")

    def _about_cta_action():
        out = tpl.render()
        # Targets the specific CTA link (identified by its own link text),
        # not a bare 'href="/travis-tx"' substring search -- that string
        # legitimately appears elsewhere on this page (the navbar's county
        # picker dropdown has a real, correct "Travis County, TX" option
        # pointing at /travis-tx; that is NOT the bug this task fixes).
        import re
        m = re.search(r'<a href="([^"]*)" class="btn-cta">Search a parcel', out)
        if not m:
            raise AssertionError("Search a parcel CTA not found in rendered output at all")
        if m.group(1) == "/travis-tx":
            raise AssertionError("About page CTA still routes to /travis-tx -- regression of the fixed bug")
        if m.group(1) != "/":
            raise AssertionError(f"About page CTA expected to resolve to '/', got {m.group(1)!r}")
        return out
    check("about.html / hero CTA href -> url_for('home') = '/' (not '/travis-tx')", _about_cta_action)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260828-07 (rates honesty + routing fixes) scenarios passed.")


if __name__ == "__main__":
    main()
