"""
verify_px_20260828_01_render.py — real Jinja RENDER verification for
PX-20260828-01 (Home/Search/Info/Rates/Snapshot neutral-URL decoupling).

Covers the three templates that gained a real, wired county filter this
brief (rates.html, snapshot.html, info.html) plus base.html's own new
api_county_slug-driven COUNTY_BASE line -- every template extends
base.html, so any of these renders exercises that line too. Same
lightweight-Undefined technique as verify_m4_part1_other_pages_render.py
(not a fully-strict harness); this still catches template syntax errors,
undefined-attribute crashes on the NEW variables this brief introduced
(api_county_slug, and county_profile/county_slug now being passed as
EXPLICIT render_template() kwargs from the *_landing() routes instead of
only ever coming from the context processor), and leaked raw Jinja
delimiters.

Two url_for() stub behaviors are exercised deliberately:
  - "anchored" stub: url_for(endpoint, county_slug=X) mimics
    _add_county_slug()'s real auto-injection -- i.e. every endpoint that
    takes a county_slug gets one prefixed, matching a request that arrived
    on a real /<county_slug>/... page (tax_rates()/county_snapshot()/
    info()).
  - "landing" stub: url_for('rates_landing'/'snapshot_landing'/
    'info_landing') resolves to the real bare path with NO county
    prefix -- matching what these three new routes' own url_for() calls
    inside their shared templates actually produce.
Both stubs also thread api_county_slug through the county_slug kwarg of
url_for('index', ...) the same way base.html's real COUNTY_BASE line does,
so a wrong api_county_slug would show up in the rendered output.

Run: python3 verify_px_20260828_01_render.py
Exits non-zero and prints a diagnosis if any scenario fails.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

_LANDING_BARE_PATHS = {
    "info_landing": "/info",
    "rates_landing": "/rates",
    "snapshot_landing": "/snapshot",
    "home": "/",
    "search_landing": "/search",
}


class _FakeRequest:
    def __init__(self, path="/", args=None):
        self.path = path
        self.args = args or {}
        self.endpoint = None


def _make_url_for(api_county_slug):
    def _url_for(endpoint, **kwargs):
        if endpoint == "static":
            return "/static/" + kwargs.get("filename", "")
        if endpoint in _LANDING_BARE_PATHS:
            base = _LANDING_BARE_PATHS[endpoint]
            qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
            return base + (f"?{qs}" if qs else "")
        # county-anchored endpoint: mimic _add_county_slug()'s real
        # auto-injection using whatever county_slug kwarg was explicitly
        # passed (index()'s own case, since base.html calls
        # url_for('index', county_slug=api_county_slug) directly), falling
        # back to api_county_slug for any other anchored endpoint that
        # didn't pass one explicitly (matching the real DEFAULT_COUNTY_SLUG
        # fallback shape closely enough for a render-only check).
        slug = kwargs.pop("county_slug", api_county_slug)
        qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
        return f"/{slug}/{endpoint.lstrip('/')}" + (f"?{qs}" if qs else "")
    return _url_for


def make_env(api_county_slug="travis-tx", county_slug="travis-tx", county_profile=None,
             live_counties=None, mode="investor"):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _make_url_for(api_county_slug)
    env.globals["request"] = _FakeRequest()
    env.globals["mode"] = mode
    import config as _real_config
    env.globals["config"] = _real_config
    env.filters["tojson"] = lambda v: "null"

    env.globals["api_county_slug"] = api_county_slug
    env.globals["county_slug"] = county_slug
    env.globals["county_url"] = lambda path: f"/{county_slug}{path}"
    env.globals["county_profile"] = county_profile or _TRAVIS_PROFILE
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = live_counties if live_counties is not None else _LIVE_COUNTIES
    env.globals["is_county_anchored"] = False
    return env


_TRAVIS_PROFILE = {
    "display_name": "Travis County, TX",
    "county_name": "Travis County",
    "cad_name": "Travis Central Appraisal District",
    "tax_office_name": "Travis County Tax Office",
}
_DALLAS_PROFILE = {
    "display_name": "Dallas County, TX",
    "county_name": "Dallas County",
    "cad_name": "Dallas Central Appraisal District",
    "tax_office_name": "Dallas County Tax Office",
}
_LIVE_COUNTIES = [
    {"slug": "travis-tx", "county_code": "TRAVIS", "value": "travis",
     "display_name": "Travis County, TX", "county_name": "Travis County",
     "parcel_count": 430147, "parcel_count_display": "430,147"},
    {"slug": "dallas-tx", "county_code": "DALLAS", "value": "dallas",
     "display_name": "Dallas County, TX", "county_name": "Dallas County",
     "parcel_count": 705536, "parcel_count_display": "705,536"},
]

FAILURES = []


def check(label, fn):
    try:
        out = fn()
        if "{%" in out or "{#" in out:
            FAILURES.append(f"{label}: leaked raw Jinja delimiter in output")
        else:
            print(f"  OK   {label} ({len(out)} chars)")
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")


def main():
    # ── rates.html ──────────────────────────────────────────────────────
    def rates_ctx():
        return dict(
            by_entity_json="{}", entity_names_json="{}", entity_category_json="{}",
            all_entities=[], entity_category_order=["School District", "City", "County",
                                                      "Hospital District", "MUD/WCID", "Other"],
            key_entities=["TCO", "IAU", "CAT", "THD", "ACT"],
            year_min=1990, year_max=2025, default_year_from=2016,
        )

    env = make_env(api_county_slug="travis-tx", county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("rates.html")
    check("rates.html / anchored render (tax_rates(), Travis)", lambda: tpl.render(**rates_ctx()))

    env = make_env(api_county_slug="dallas-tx", county_slug="dallas-tx", county_profile=_DALLAS_PROFILE)
    tpl = env.get_template("rates.html")
    check("rates.html / bare landing render (rates_landing(), ?county=dallas-tx)",
          lambda: tpl.render(**rates_ctx()))

    # ── snapshot.html ────────────────────────────────────────────────────
    def snapshot_ctx(status_2026="certified", mode="investor"):
        def _bd_row(ptype, mv25_b, mv26_b, med_pct=5.0):
            return {"ptype": ptype, "sort_key": ptype, "n_parcels": 100, "n_up": 60, "n_down": 30,
                    "n_flat": 10, "median_pct": med_pct, "p25_pct": med_pct - 2, "p75_pct": med_pct + 2,
                    "total_mv25_b": mv25_b, "total_mv26_b": mv26_b}
        return dict(
            view="overall", mode=mode, status_2026=status_2026,
            data_unavailable=False, data_unavailable_reason=None,
            rows=[_bd_row("Residential", 100.0, 106.0)],
            totals={"n_total": 1000, "n_up": 600, "n_down": 300, "n_flat": 100,
                    "total_mv25_b": 180.0, "total_mv26_b": 190.0, "median_pct": 5.5},
            bench_trends=[], new_construction_count=42, risk_flagged_count=7,
            subtype_cap=8, top_neighborhoods=[], bottom_neighborhoods=[],
        )

    env = make_env(api_county_slug="travis-tx", county_slug="travis-tx", county_profile=_TRAVIS_PROFILE, mode="investor")
    tpl = env.get_template("snapshot.html")
    check("snapshot.html / anchored render (county_snapshot(), Travis)",
          lambda: tpl.render(**snapshot_ctx()))

    env = make_env(api_county_slug="dallas-tx", county_slug="dallas-tx", county_profile=_DALLAS_PROFILE, mode="homeowner")
    tpl = env.get_template("snapshot.html")
    check("snapshot.html / bare landing render (snapshot_landing(), ?county=dallas-tx, homeowner)",
          lambda: tpl.render(**snapshot_ctx(mode="homeowner")))

    # ── info.html ────────────────────────────────────────────────────────
    env = make_env(api_county_slug="travis-tx", county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("info.html")
    check("info.html / anchored render, content available (info(), Travis)",
          lambda: tpl.render(info_content_available=True))

    env = make_env(api_county_slug="dallas-tx", county_slug="dallas-tx", county_profile=_DALLAS_PROFILE)
    tpl = env.get_template("info.html")

    def _check_info_not_available():
        out = tpl.render(info_content_available=False)
        if "Not available for Dallas County yet" not in out:
            raise AssertionError("expected honest not-available heading not found in output")
        return out
    check("info.html / bare landing render, content NOT available (info_landing(), ?county=dallas-tx)",
          _check_info_not_available)

    env = make_env(api_county_slug="travis-tx", county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("info.html")
    check("info.html / bare landing render, content available (info_landing(), ?county=travis-tx)",
          lambda: tpl.render(info_content_available=True))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260828-01 scenarios rendered cleanly, no leaked Jinja delimiters.")


if __name__ == "__main__":
    main()
