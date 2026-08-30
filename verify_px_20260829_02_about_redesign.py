#!/usr/bin/env python3
"""
verify_px_20260829_02_about_redesign.py -- real Jinja RENDER verification for
PX-20260829-02's About page redesign: the section reorder/rewrite
(templates/about.html) and the Coverage Map extraction into a shared
coverage_map() macro (templates/_macros.html) + static/coverage-map.js so
about.html and index.html reuse the exact same component instead of a second
hand-copied instance (same drift-avoidance precedent as
static/parcel-typeahead.js and _macros.html's addr_match_results()).

Same lightweight-jinja2.Environment technique used throughout this repo's
other verify_*.py fixtures (no Flask/DB in this sandbox). Unlike some of
those fixtures, this one stubs `tojson` with real json.dumps (not a
constant "null") so the coverage_map() macro's data-live-slugs attribute
can actually be checked for content, not just for not crashing.

Checks:
  1. about.html renders with no exceptions for both a 1-live-county and a
     2-live-county scenario.
  2. Section order matches Diego's approved structure: Hero -> Why
     Parcelytics -> The Standard -> Methodology -> Who We're Building For
     -> Our Mission -> Where We're Going -> Closing.
  3. id="methodology-billing-gap" is still present verbatim -- property.html
     has 4 live url_for('about')+'#methodology-billing-gap' links into it.
  4. Who We're Building For renders exactly the homepage's 4 segments (no
     5th "asset managers" card): Real estate investors, Developers,
     Homeowners, Tax consultants.
  5. The folded-in "See it for yourself" data-sources table has one row per
     live_counties entry, reading real cad_name/cad_abbr/tax_office_name
     (not a single hardcoded county).
  6. coverage_map()'s data-live-slugs attribute on about.html's #roadmapMap
     contains exactly the slugs passed in, in order -- proves the macro is
     actually being called with the real live_counties, not a stale copy.
  7. The strongest drift-avoidance proof: rendering index.html and
     about.html with the IDENTICAL live_counties list produces a
     byte-identical market-card-grid block on both pages -- true shared-
     component reuse, not two implementations that merely look alike today.
  8. Hero and Closing CTAs both resolve via url_for('home') = '/' (neutral
     page, not hardcoded to a county).

Run: python3 verify_px_20260829_02_about_redesign.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

REPO = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(REPO, "templates")

FAILURES = []


def check(label, cond):
    status = "OK  " if cond else "FAIL"
    print(f"  {status} {label}")
    if not cond:
        FAILURES.append(label)
    return cond


_BARE_PATHS = {"home": "/", "about": "/about", "search_landing": "/search"}


def _make_url_for(default_county_slug="travis-tx"):
    def _url_for(endpoint, **kwargs):
        if endpoint == "static":
            return "/static/" + kwargs.get("filename", "")
        if endpoint in _BARE_PATHS:
            base = _BARE_PATHS[endpoint]
            qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
            return base + (f"?{qs}" if qs else "")
        slug = kwargs.pop("county_slug", default_county_slug)
        qs = "&".join(f"{k}={v}" for k, v in kwargs.items())
        return f"/{slug}/{endpoint.lstrip('/')}" + (f"?{qs}" if qs else "")
    return _url_for


class _FakeRequest:
    def __init__(self, path="/"):
        self.path = path
        self.args = {}
        self.endpoint = None


_TRAVIS_PROFILE = {"county_name": "Travis County", "display_name": "Travis County, TX"}

_TRAVIS_ENTRY = {
    "slug": "travis-tx", "county_code": "travis", "value": "travis",
    "display_name": "Travis County, TX", "county_name": "Travis County",
    "cad_name": "Travis Central Appraisal District", "cad_abbr": "TCAD",
    "tax_office_name": "Travis County Tax Office",
    "parcel_count": 430147, "parcel_count_display": "430K",
}
_DALLAS_ENTRY = {
    "slug": "dallas-tx", "county_code": "dallas", "value": "dallas",
    "display_name": "Dallas County, TX", "county_name": "Dallas County",
    "cad_name": "Dallas Central Appraisal District", "cad_abbr": "DCAD",
    "tax_office_name": "Dallas County Tax Office",
    "parcel_count": 705536, "parcel_count_display": "705K",
}


def make_env(live_counties):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _make_url_for()
    env.globals["request"] = _FakeRequest()
    env.globals["mode"] = "investor"
    import config as _real_config
    env.globals["config"] = _real_config
    # Real tojson (not a stubbed constant) so data-live-slugs' actual
    # content can be verified, matching what Flask/Jinja would really emit.
    env.filters["tojson"] = lambda v: json.dumps(v)
    env.globals["api_county_slug"] = "travis-tx"
    env.globals["county_slug"] = "travis-tx"
    env.globals["county_url"] = lambda path: f"/travis-tx{path}"
    env.globals["county_profile"] = _TRAVIS_PROFILE
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = live_counties
    env.globals["is_county_anchored"] = False
    return env


def main():
    for scenario_name, live_counties in [
        ("1 live county (Travis only)", [_TRAVIS_ENTRY]),
        ("2 live counties (Travis + Dallas)", [_TRAVIS_ENTRY, _DALLAS_ENTRY]),
    ]:
        print(f"\n--- Scenario: {scenario_name} ---")
        env = make_env(live_counties)
        tpl = env.get_template("about.html")
        out = tpl.render()
        check(f"[{scenario_name}] renders with no exceptions ({len(out)} chars)", len(out) > 1000)

        # 2. Section order.
        markers = [
            ("Hero", 'Property tax data you can'),
            ("Why Parcelytics", "shouldn't require detective work"),
            ("The Standard", "Honesty, made premium."),
            ("Methodology", 'id="methodology"'),
            ("Who We're Building For", "Built for anyone who cares about property taxes."),
            ("Our Mission", "Make property tax data"),
            ("Where We're Going", "One platform. Every county. Better analysis."),
            ("Closing", "Better data. Better understanding. Better decisions."),
        ]
        positions = []
        for name, needle in markers:
            idx = out.find(needle)
            if idx == -1:
                FAILURES.append(f"[{scenario_name}] section marker missing: {name} ({needle!r})")
                print(f"  FAIL section marker missing: {name} ({needle!r})")
            positions.append(idx)
        if all(p != -1 for p in positions):
            check(f"[{scenario_name}] section order matches approved structure "
                  f"(Hero -> Why Parcelytics -> Standard -> Methodology -> "
                  f"Who We're Building For -> Mission -> Where We're Going -> Closing)",
                  positions == sorted(positions))

        # 3. Methodology cross-link anchor still present verbatim.
        check(f'[{scenario_name}] id="methodology-billing-gap" present '
              "(property.html has 4 live links into this anchor)",
              'id="methodology-billing-gap"' in out)

        # 4. Who We're Building For -- exactly homepage's 4 segments.
        who_serve_titles = re.findall(
            r'<div class="who-serve-card">.*?<h3>([^<]+)</h3>', out, re.DOTALL)
        check(f"[{scenario_name}] exactly 4 who-serve-card segments "
              f"(got {len(who_serve_titles)}: {who_serve_titles})",
              who_serve_titles == ["Real estate investors", "Developers", "Homeowners", "Tax consultants"])
        check(f'[{scenario_name}] no 5th "asset managers" card (no distinct '
              "shipped capability backs it, per Diego's ruling)",
              "asset manager" not in out.lower())

        # 5. Folded data-sources table -- one row per live county, real fields.
        for c in live_counties:
            check(f"[{scenario_name}] data-sources evidence table includes "
                  f"{c['county_name']} ({c['cad_abbr']} / {c['tax_office_name']})",
                  c["cad_name"] in out and c["cad_abbr"] in out and c["tax_office_name"] in out)

        # 6. coverage_map() macro's data-live-slugs reflects the real input.
        m = re.search(r'<svg id="roadmapMap"[^>]*data-live-slugs=\'([^\']*)\'', out)
        check(f"[{scenario_name}] #roadmapMap has a data-live-slugs attribute at all",
              m is not None)
        if m:
            parsed_slugs = json.loads(m.group(1))
            expected_slugs = [c["slug"] for c in live_counties]
            check(f"[{scenario_name}] data-live-slugs == {expected_slugs} "
                  f"(got {parsed_slugs})",
                  parsed_slugs == expected_slugs)

        # 8. CTAs resolve via url_for('home'), not a hardcoded county path.
        cta_hrefs = re.findall(r'<a href="([^"]*)" class="btn-cta">', out)
        check(f"[{scenario_name}] both CTAs (hero + closing) resolve to '/' "
              f"via url_for('home') (found {cta_hrefs})",
              cta_hrefs == ["/", "/"])

    # ─────────────────────────────────────────────────────────────────────
    print("\n--- Drift-avoidance proof: shared coverage_map() macro ---")
    # ─────────────────────────────────────────────────────────────────────
    # 7. Same live_counties fed to both pages -> byte-identical
    # market-card-grid block. This is the actual proof the extraction did
    # what it was supposed to: about.html isn't a second hand-copied
    # instance of the coverage map, it's the SAME macro output.
    live_counties = [_TRAVIS_ENTRY, _DALLAS_ENTRY]
    env_about = make_env(live_counties)
    env_index = make_env(live_counties)
    # index.html needs a couple more globals the homepage template reads
    # that about.html does not -- stub the minimum to get a clean render.
    import config as _real_config
    env_index.globals["total_live_parcel_count_display"] = "1.13M"

    def _extract_grid(html):
        m = re.search(r'<div class="market-card-grid mt-4">.*?</div>\s*\n</div>', html, re.DOTALL)
        return m.group(0) if m else None

    about_out = env_about.get_template("about.html").render()
    try:
        index_out = env_index.get_template("index.html").render()
        about_grid = _extract_grid(about_out)
        index_grid = _extract_grid(index_out)
        check("both about.html and index.html's market-card-grid blocks were found",
              about_grid is not None and index_grid is not None)
        if about_grid and index_grid:
            check("about.html and index.html render a BYTE-IDENTICAL "
                  "market-card-grid from the same live_counties input "
                  "(true shared-macro reuse, not a second hand-copied "
                  "implementation)",
                  about_grid == index_grid)
    except Exception as e:
        FAILURES.append(f"index.html render for drift-avoidance comparison raised: {e}")
        print(f"  FAIL index.html render for drift-avoidance comparison raised: {e}")

    # ─────────────────────────────────────────────────────────────────────
    print("\n--- Jinja syntax sanity ---")
    # ─────────────────────────────────────────────────────────────────────
    import jinja2
    plain_env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATE_DIR))
    for label in ("about.html", "_macros.html", "index.html"):
        try:
            src = plain_env.loader.get_source(plain_env, label)[0]
            plain_env.parse(src)
            check(f"{label} parses as valid Jinja", True)
        except Exception as e:
            check(f"{label} parses as valid Jinja (error: {e})", False)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260829-02 (About page redesign) scenarios passed.")


if __name__ == "__main__":
    main()
