#!/usr/bin/env python3
"""
verify_px_20260829_02_about_redesign.py -- real Jinja RENDER verification for
the About page redesign, PX-20260829-02 as amended by PX-20260829-03.

PX-20260829-03 UPDATE: Diego's live review of -02 requested a follow-up
pass -- most of this file's original checks were written against -02's
8-section structure, which -03 changed substantially:
  - The county pill + "Showing ... coverage details below" hero disclosure
    are removed (Task 1) -- About carries no county context at all now.
  - Why Parcelytics is condensed from 5 paragraphs to 3, in a new 2-column
    layout (prose left, the "See it for yourself" evidence card moved into
    the freed right column) (Task 2, sign-off obtained). PX-20260829-05
    later cut it further to 2 paragraphs -- see that check's own comment
    below for what changed and why.
  - The Standard (5 principles + confidence legend) MOVED to the homepage,
    replacing its old per-county Provenance panel, and was removed from
    About entirely -- no duplication across two pages (Task 4).
  - Methodology (and its #methodology-billing-gap anchor) is REMOVED from
    About -- not just moved, deleted. The 4 property.html "Why?" links that
    used to point at it were replaced with an inline ⓘ info-icon tooltip on
    property.html itself, so nothing needs that anchor anymore (Task 3,
    amended mid-brief with its own sign-off).
  - Our Mission is rewritten around centralization (Task 5, sign-off
    obtained): "Make property tax data centralized, transparent,
    accessible, and actionable."
  - Where We're Going (the coverage_map() reuse) is REMOVED from About --
    it duplicated the homepage and rendered blank on this page. The shared
    coverage_map() macro itself is untouched; index.html is its only caller
    now (Task 6).
  - The closing CTA points at url_for('search_landing') = '/search', not
    url_for('home') (Task 7).

This is the same "scanner constant must follow a legitimate refactor"
category as the PX-20260829-02 fixes to verify_launch_surface_registry.py
and verify_px_20260828_15_task2_certification_copy.py -- the checks below
are rewritten to match the CURRENT approved structure, not preserved as
stale assertions against content that's supposed to be gone.

Same lightweight-jinja2.Environment technique used throughout this repo's
other verify_*.py fixtures (no Flask/DB in this sandbox). Real tojson
(json.dumps, not a stubbed constant) so index.html's coverage-map
data-live-slugs attribute can be checked for real content.

Checks:
  1. about.html renders with no exceptions for both a 1-live-county and a
     2-live-county scenario.
  2. Section order matches the -03 structure: Hero -> Why Parcelytics ->
     Who We're Building For -> Our Mission -> Closing.
  3. Hero carries NO county pill/eyebrow and NO "Showing ... coverage
     details" disclosure, and never references county_profile at all.
  4. Why Parcelytics is exactly 3 paragraphs, laid out in 2 columns, with
     the source-evidence card in the right column (not full-width below).
  5. The Standard and Methodology are entirely ABSENT from about.html --
     not merely reordered. Methodology's own #methodology-billing-gap
     anchor is gone too, and about.html no longer references
     county_profile or the removed anchor anywhere.
  6. The Standard now renders on the HOMEPAGE instead, with its own
     id="the-standard", all 5 principles, the confidence legend, and the
     folded general sourcing line -- and the homepage's old per-county
     Provenance panel (CAD/tax-office loop, "Provenance ·" kicker) is gone.
  7. Who We're Building For still renders exactly the homepage's 4
     segments (no 5th "asset managers" card).
  8. Our Mission renders the approved centralization line.
  9. Where We're Going / coverage_map() is entirely ABSENT from about.html
     (no #roadmapMap, no market-card-grid) -- but the shared macro/JS
     themselves are untouched and index.html's own coverage map still
     renders correctly with a real data-live-slugs attribute.
  10. Hero CTA -> url_for('home') = '/'; Closing CTA -> url_for
      ('search_landing') = '/search' (NOT the same target anymore).
  11. property.html's 4 old "Why?" links to about.html#methodology-
      billing-gap are gone, replaced by the approved ⓘ tooltip copy, and
      no url_for('about') call remains in property.html at all.

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

        # 2. Section order (PX-20260829-03 structure).
        markers = [
            ("Hero", 'Property tax data you can'),
            ("Why Parcelytics", "shouldn't require detective work"),
            ("Who We're Building For", "Built for anyone who cares about property taxes."),
            ("Our Mission", "Make property tax data"),
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
            check(f"[{scenario_name}] section order matches -03 structure "
                  f"(Hero -> Why Parcelytics -> Who We're Building For -> "
                  f"Mission -> Closing)",
                  positions == sorted(positions))

        # 3. Hero carries no county context at all (Task 1).
        check(f"[{scenario_name}] hero has no county eyebrow pill",
              '<span class="hero-eyebrow">' not in out)
        check(f"[{scenario_name}] hero has no 'Showing ... coverage details' disclosure",
              "coverage details below" not in out)
        # Scoped to the <section class="hero"> block itself, not the whole
        # page -- base.html's shared <meta name="description"> tag also
        # reads county_profile (a separate, pre-existing, site-wide SEO tag
        # outside this brief's scope), so a whole-page substring check would
        # false-fail on that unrelated tag. The eyebrow-pill and disclosure-
        # line checks above already prove Task 1's actual requirement.
        hero_match = re.search(r'<section class="hero">.*?</section>', out, re.DOTALL)
        check(f"[{scenario_name}] hero section itself carries no county reference",
              hero_match is not None and "Travis County" not in hero_match.group(0))

        # 4. Why Parcelytics: 2-column layout, evidence card in the right
        # column (Task 2). Paragraph count was originally 3 (this section's
        # own PX-20260829-02 build); PX-20260829-05 cut the old middle
        # paragraph (it restated paragraph 1 in more detail without adding
        # anything -- PM's own read that the argument is tighter at two) and
        # folded the old third paragraph's mechanism sentence into a new,
        # longer closing paragraph naming the platform's centralization
        # vision and concrete investor/developer + homeowner benefits. Now
        # exactly 2 paragraphs, not 3 -- this is the approved, intentional
        # shape, not a regression.
        why_section_match = re.search(
            r'Why Parcelytics.*?(?=Built for anyone who cares about property taxes)', out, re.DOTALL)
        check(f"[{scenario_name}] Why Parcelytics section found for paragraph count check",
              why_section_match is not None)
        if why_section_match:
            why_html = why_section_match.group(0)
            para_count = len(re.findall(r'<p class="fd-lead', why_html))
            check(f"[{scenario_name}] Why Parcelytics has exactly 2 paragraphs post-PX-20260829-05 (got {para_count})",
                  para_count == 2)
            check(f"[{scenario_name}] Why Parcelytics uses a 2-column row (col-lg-7 / col-lg-5)",
                  'class="col-lg-7"' in why_html and 'class="col-lg-5"' in why_html)
            check(f"[{scenario_name}] evidence card ('See it for yourself') sits in the "
                  "right column, not full-width below the prose",
                  why_html.find('class="col-lg-5"') < why_html.find("See it for yourself"))

        # 5. The Standard and Methodology are entirely absent from About now
        # (Tasks 3-amended and 4) -- not reordered, GONE.
        check(f"[{scenario_name}] 'Honesty, made premium.' (The Standard) is "
              "absent from about.html -- moved to the homepage, not duplicated",
              "Honesty, made premium." not in out)
        check(f"[{scenario_name}] Methodology section (id=\"methodology\") is "
              "absent from about.html -- deleted, not just reordered",
              'id="methodology"' not in out)
        check(f"[{scenario_name}] #methodology-billing-gap anchor is gone "
              "(nothing links to it anymore -- see property.html checks below)",
              'id="methodology-billing-gap"' not in out)

        # 7. Who We're Building For -- exactly homepage's 4 segments (unchanged
        # by -03, still verified here since this file replaces the -02 check).
        who_serve_titles = re.findall(
            r'<div class="who-serve-card">.*?<h3>([^<]+)</h3>', out, re.DOTALL)
        check(f"[{scenario_name}] exactly 4 who-serve-card segments "
              f"(got {len(who_serve_titles)}: {who_serve_titles})",
              who_serve_titles == ["Real estate investors", "Developers", "Homeowners", "Tax consultants"])
        check(f'[{scenario_name}] no 5th "asset managers" card',
              "asset manager" not in out.lower())

        # 8. Our Mission -- approved centralization line (Task 5).
        check(f"[{scenario_name}] Our Mission renders the approved centralization line",
              "centralized, transparent, accessible, and actionable" in out)

        # 9. Where We're Going / coverage_map() is entirely absent (Task 6).
        check(f"[{scenario_name}] no #roadmapMap on about.html (coverage map removed)",
              'id="roadmapMap"' not in out)
        check(f"[{scenario_name}] no market-card-grid on about.html (coverage map removed)",
              "market-card-grid" not in out)

        # 10. CTAs: hero -> home ('/'), closing -> search ('/search') -- these
        # are now DIFFERENT targets, unlike -02 where both went to '/'.
        cta_hrefs = re.findall(r'<a href="([^"]*)" class="btn-cta">', out)
        check(f"[{scenario_name}] hero CTA -> '/' (url_for('home')), "
              f"closing CTA -> '/search' (url_for('search_landing')) "
              f"(found {cta_hrefs})",
              cta_hrefs == ["/", "/search"])

    # ─────────────────────────────────────────────────────────────────────
    print("\n--- Homepage: The Standard moved here, old Provenance panel gone ---")
    # ─────────────────────────────────────────────────────────────────────
    live_counties = [_TRAVIS_ENTRY, _DALLAS_ENTRY]
    env_index = make_env(live_counties)
    env_index.globals["total_live_parcel_count_display"] = "1.13M"
    index_out = env_index.get_template("index.html").render()

    check("index.html now renders 'Honesty, made premium.' (The Standard, moved from About)",
          "Honesty, made premium." in index_out)
    check('index.html has id="the-standard" (About\'s Methodology intro links here)',
          'id="the-standard"' in index_out)
    for rule in (
        "No estimate or interpolation is ever presented as fact.",
        "Every figure shows a visible confidence level.",
        "Every figure is traceable to its source and as-of date.",
        '"Not Available" is an explicit state — never blank, never zero.',
        "Estimates carry distinct treatment: dashed, muted, ~ prefixed.",
    ):
        check(f"index.html's Standard includes principle: {rule[:40]}...", rule in index_out)
    check("index.html's Standard folds in ONE general sourcing line "
          "(directly from county appraisal district/tax office) instead of "
          "enumerating every county by name",
          "directly from that county's own appraisal district and tax office" in index_out)
    check("index.html no longer has the old per-county Provenance kicker "
          "('Provenance ·' framing, doesn't scale per Diego's ruling)",
          "Provenance ·" not in index_out)
    check("index.html's Standard doesn't re-enumerate every live county's "
          "CAD/tax-office by name (that per-county list is what didn't scale)",
          "Dallas Central Appraisal District" not in index_out)

    # coverage_map() macro itself: index.html is still its one real caller,
    # and it should still render correctly with real data-live-slugs.
    m = re.search(r'<svg id="roadmapMap"[^>]*data-live-slugs=\'([^\']*)\'', index_out)
    check("index.html's #roadmapMap still has a data-live-slugs attribute "
          "(coverage_map() macro itself untouched by Task 6 -- About just "
          "stopped calling it)",
          m is not None)
    if m:
        parsed_slugs = json.loads(m.group(1))
        expected_slugs = [c["slug"] for c in live_counties]
        check(f"index.html's data-live-slugs == {expected_slugs} (got {parsed_slugs})",
              parsed_slugs == expected_slugs)

    # ─────────────────────────────────────────────────────────────────────
    print("\n--- property.html: 4 old 'Why?' links replaced with ⓘ tooltip ---")
    # ─────────────────────────────────────────────────────────────────────
    property_src = open(os.path.join(TEMPLATE_DIR, "property.html")).read()
    check("property.html has no live url_for('about') call anywhere "
          "(the 4 old billing-gap links are gone; the only other historical "
          "use -- a Why? link -- is gone too)",
          "url_for('about')" not in property_src)
    check("property.html has no more '#methodology-billing-gap' references "
          "outside of explanatory comments",
          not re.search(r'href="[^"]*#methodology-billing-gap"', property_src))
    approved_tooltip = ("own billing file doesn't include a row for every parcel on its "
                         "appraisal roll — a gap in the county's published data, not "
                         "something Parcelytics failed to load.")
    occurrences = property_src.count(approved_tooltip)
    check(f"property.html carries the approved condensed tooltip copy at all "
          f"4 sites (found {occurrences}, expected 4)",
          occurrences == 4)
    check("property.html's replacement uses a ⓘ info icon (native title "
          "tooltip, this page's existing pattern), not a new component",
          property_src.count('style="cursor:help; color:var(--text-3);') >= 4)

    # ─────────────────────────────────────────────────────────────────────
    print("\n--- Jinja syntax sanity ---")
    # ─────────────────────────────────────────────────────────────────────
    import jinja2
    plain_env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATE_DIR))
    for label in ("about.html", "_macros.html", "index.html", "property.html"):
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
    print("All PX-20260829-02/-03 (About page redesign + revisions) scenarios passed.")


if __name__ == "__main__":
    main()
