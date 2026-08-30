"""
verify_px_20260828_09_provenance_and_hints.py — real Jinja RENDER
verification for PX-20260828-09 Task 3 (homepage provenance panel now
loops live_counties' real per-county sources instead of the current
request's single county_profile) and Task 4 (hero/search hints no longer
hardcode Travis-specific account-number digit lengths).

Same lightweight-Jinja2-Environment technique as verify_px_20260828_07_
rates_and_routing.py (not Flask -- not installed in this sandbox). The
mock live_counties list below matches the REAL, extended shape
_live_counties() (app.py) now returns as of this task -- each entry
carries cad_name/cad_abbr/tax_office_name alongside slug/display_name/
county_name/parcel_count, not a narrower ad-hoc shape.

PX-20260829-03 UPDATE: Task 3's own per-county provenance panel (the
"Provenance · N+ parcels..." block this task built, looping live_counties
to print every county's CAD/tax-office name by name) was itself removed
from index.html by PX-20260829-03 Task 4 -- replaced with The Standard
(moved verbatim from about.html), which folds sourcing into ONE general
line ("directly from that county's own appraisal district and tax
office, never a third-party aggregator") rather than enumerating each
county, per Diego's explicit ruling that per-county enumeration "doesn't
scale." This is the same kind of legitimate supersession already
documented in verify_launch_surface_registry.py and
verify_px_20260829_02_about_redesign.py -- the two checks below are
rewritten to assert the NEW reality (general sourcing line present, old
per-county enumeration gone) rather than the original per-county-loop
behavior, which no longer exists by design. Task 4's hint-genericization
checks are untouched -- unaffected by the panel's removal.

Run: python3 verify_px_20260828_09_provenance_and_hints.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

_BARE_PATHS = {
    "home": "/", "search_landing": "/search", "info_landing": "/info",
    "rates_landing": "/rates", "snapshot_landing": "/snapshot", "about": "/about",
    "terms": "/terms", "privacy": "/privacy", "disclaimer": "/disclaimer",
}


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


_TRAVIS_PROFILE = {
    "display_name": "Travis County, TX", "county_name": "Travis County",
    "cad_name": "Travis Central Appraisal District",
    "tax_office_name": "Travis County Tax Office", "cad_abbr": "TCAD",
}

# Matches the REAL, extended _live_counties() shape (app.py) as of this
# task -- cad_name/cad_abbr/tax_office_name now travel with every entry.
_LIVE_COUNTIES = [
    {"slug": "travis-tx", "value": "travis", "display_name": "Travis County, TX",
     "county_name": "Travis County", "parcel_count": 430147, "parcel_count_display": "430,147",
     "cad_name": "Travis Central Appraisal District", "cad_abbr": "TCAD",
     "tax_office_name": "Travis County Tax Office"},
    {"slug": "dallas-tx", "value": "dallas", "display_name": "Dallas County, TX",
     "county_name": "Dallas County", "parcel_count": 705536, "parcel_count_display": "705,536",
     "cad_name": "Dallas Central Appraisal District", "cad_abbr": "DCAD",
     "tax_office_name": "Dallas County Tax Office"},
]


def make_env(county_slug="travis-tx", county_profile=None):
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
    env.globals["is_county_anchored"] = False
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
    # ── Task 3 (superseded by PX-20260829-03 Task 4): homepage sourcing ────
    env = make_env(county_slug="travis-tx")
    tpl = env.get_template("index.html")

    def _standard_folds_in_one_general_sourcing_line():
        out = tpl.render(q="", error=None, addr_matches=None, api_county_slug="")
        # The per-county provenance panel this task originally built is gone
        # (PX-20260829-03 Task 4) -- replaced by The Standard's ONE general
        # sourcing line. Assert the new line is present, and that the old
        # per-county enumeration pattern it replaced is genuinely absent,
        # not just relocated.
        if "directly from that county's own appraisal district and tax office" not in out:
            raise AssertionError("The Standard's general sourcing line is missing from index.html")
        for needle in ("Travis Central Appraisal District (TCAD)", "Travis County Tax Office",
                       "Dallas Central Appraisal District (DCAD)", "Dallas County Tax Office"):
            if needle in out:
                raise AssertionError(
                    f"old per-county source enumeration ({needle!r}) still present -- "
                    "should be folded into one general line, not enumerated per county")
        return out
    check("index.html / sourcing is ONE general line on The Standard, not a per-county enumeration",
          _standard_folds_in_one_general_sourcing_line)

    def _provenance_drops_travis_only_year_claims():
        out = tpl.render(q="", error=None, addr_matches=None, api_county_slug="")
        # The old hardcoded "2021-2025 verified, 2026 preliminary" / "1990-
        # 2025" claims must not survive anywhere on the homepage -- Dallas
        # has zero rate-history rows loaded (PX-20260828-07 Task 1 finding),
        # so repeating those year-range claims would overclaim coverage
        # Dallas doesn't have. Still a valid check post-Task-4: this is a
        # general homepage assertion, not scoped to the now-removed panel.
        if "2021–2025 verified, 2026 preliminary" in out:
            raise AssertionError("stale Travis-only certified-year claim still present on the homepage")
        if "Tax rates by entity (1990" in out:
            raise AssertionError("stale Travis-only '1990-2025' rate-history claim still present on the homepage")
        return out
    check("index.html / homepage no longer makes county-specific year-coverage claims",
          _provenance_drops_travis_only_year_claims)

    # ── Task 4: hero + search-page hints no longer hardcode Travis format ──
    def _hero_hint_generic():
        out = tpl.render(q="", error=None, addr_matches=None, api_county_slug="")
        if "10-digit" in out or "14-digit" in out or "TCAD account" in out:
            raise AssertionError("hero search hint still hardcodes Travis-specific account digit-lengths")
        if "Accepts a parcel ID" not in out:
            raise AssertionError("expected generic hero search hint text not found")
        return out
    check("index.html / hero search hint is generic, no Travis-specific account format",
          _hero_hint_generic)

    env2 = make_env(county_slug="travis-tx")
    tpl2 = env2.get_template("search.html")

    def _search_hint_generic():
        out = tpl2.render(q="", results=[], live_counties=_LIVE_COUNTIES)
        if "10-digit" in out or "14-digit" in out or "TCAD account" in out:
            raise AssertionError("search page hint still hardcodes Travis-specific account digit-lengths")
        if "Accepts a parcel ID" not in out:
            raise AssertionError("expected generic search page hint text not found")
        return out
    check("search.html / hint is generic, no Travis-specific account format", _search_hint_generic)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260828-09 (provenance loop + generic hints) scenarios passed.")


if __name__ == "__main__":
    main()
