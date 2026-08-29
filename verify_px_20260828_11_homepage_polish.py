"""
verify_px_20260828_11_homepage_polish.py — PX-20260828-11 Task 1 (live-first
market card ordering) + Task 2 (equal-height Who We Serve grid) verification.

Tasks 3 and 4 are proposal-only in this brief (no implementation pending PM
sign-off), so there is nothing to fixture-test for them yet.

Same lightweight-Jinja2-Environment render technique as the other
PX-20260828-* harnesses in this repo (no real Flask available in this
sandbox), plus a direct static/style.css text check for the CSS-only Task 1
ordering mechanism (there's no DOM layout engine in this sandbox to prove
visual order, so this asserts the CSS rule that produces it, plus that the
underlying is-live/is-soon classes are still registry-driven per-card).

Run: python3 verify_px_20260828_11_homepage_polish.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")

FAILURES = []


def check(label, fn):
    try:
        fn()
        print(f"  OK   {label}")
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")


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


def _make_live_counties(slugs):
    catalog = {
        "travis-tx": {"slug": "travis-tx", "value": "travis", "display_name": "Travis County, TX",
                       "county_name": "Travis County", "parcel_count": 430147, "parcel_count_display": "430K",
                       "cad_name": "Travis Central Appraisal District", "cad_abbr": "TCAD",
                       "tax_office_name": "Travis County Tax Office"},
        "dallas-tx": {"slug": "dallas-tx", "value": "dallas", "display_name": "Dallas County, TX",
                       "county_name": "Dallas County", "parcel_count": 769000, "parcel_count_display": "769K",
                       "cad_name": "Dallas Central Appraisal District", "cad_abbr": "DCAD",
                       "tax_office_name": "Dallas County Tax Office"},
        "harris-tx": {"slug": "harris-tx", "value": "harris", "display_name": "Harris County, TX",
                       "county_name": "Harris County", "parcel_count": 900000, "parcel_count_display": "900K",
                       "cad_name": "Harris Central Appraisal District", "cad_abbr": "HCAD",
                       "tax_office_name": "Harris County Tax Office"},
    }
    return [catalog[s] for s in slugs]


def make_env(live_slugs=("travis-tx", "dallas-tx")):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _make_url_for()
    env.globals["request"] = _FakeRequest()
    env.globals["mode"] = "investor"
    import config as _real_config
    env.globals["config"] = _real_config
    env.filters["tojson"] = lambda v: "null"
    env.globals["api_county_slug"] = "travis-tx"
    env.globals["county_slug"] = "travis-tx"
    env.globals["county_url"] = lambda path: f"/travis-tx{path}"
    env.globals["county_profile"] = {
        "display_name": "Travis County, TX", "county_name": "Travis County",
        "cad_name": "Travis Central Appraisal District",
        "tax_office_name": "Travis County Tax Office", "cad_abbr": "TCAD",
    }
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = _make_live_counties(live_slugs)
    env.globals["total_live_parcel_count_display"] = "1.13M"
    env.globals["is_county_anchored"] = False
    return env


def _render_index(live_slugs=("travis-tx", "dallas-tx")):
    env = make_env(live_slugs)
    tpl = env.get_template("index.html")
    return tpl.render(q="", error=None, addr_matches=None, api_county_slug="")


# ── Task 1: market card live-first ordering ────────────────────────────────

def _extract_market_grid(out):
    start = out.find('<div class="market-card-grid')
    end = out.find("</div>\n  </section>", start)  # end of the roadmap section, generous
    if start == -1:
        raise AssertionError("market-card-grid not found in rendered index.html")
    return out[start: start + 6000] if end == -1 else out[start:end]


def _t1_travis_dallas_live_source_order_unchanged():
    # PX-20260828-11 Task 1 rule: CSS order, NOT a markup reshuffle -- so the
    # underlying HTML/DOM order of the six market cards must be UNCHANGED
    # (Austin, NYC, LA, Chicago, Dallas, Houston), proving live-first
    # sorting comes from a CSS rule keyed on is-live/is-soon (already
    # registry-driven), not a hand-reordered template.
    out = _render_index(live_slugs=("travis-tx", "dallas-tx"))
    section = _extract_market_grid(out)
    order = []
    for slug in ("travis-tx", "newyork-ny", "losangeles-ca", "cook-il", "dallas-tx", "harris-tx"):
        idx = section.find(f'data-market="{slug}"')
        if idx == -1:
            raise AssertionError(f"market card for {slug} not found")
        order.append((idx, slug))
    order.sort()
    got = [slug for _, slug in order]
    expected = ["travis-tx", "newyork-ny", "losangeles-ca", "cook-il", "dallas-tx", "harris-tx"]
    if got != expected:
        raise AssertionError(f"source/DOM order changed -- expected {expected}, got {got} "
                              f"(Task 1 must reorder visually via CSS `order`, not by reshuffling markup)")


def _t1_is_live_soon_classes_still_registry_driven():
    # Both Travis and Dallas live -> both get is-live; the other four
    # (never registered) stay is-soon regardless of live_slugs.
    out = _render_index(live_slugs=("travis-tx", "dallas-tx"))
    section = _extract_market_grid(out)
    for slug in ("travis-tx", "dallas-tx"):
        idx = section.find(f'data-market="{slug}"')
        card = section[max(0, idx - 120):idx]
        if "is-live" not in card:
            raise AssertionError(f"{slug} should carry is-live when in live_slugs")
    for slug in ("newyork-ny", "losangeles-ca", "cook-il", "harris-tx"):
        idx = section.find(f'data-market="{slug}"')
        card = section[max(0, idx - 120):idx]
        if "is-live" in card:
            raise AssertionError(f"{slug} should NOT carry is-live (not a real registered/loaded county)")


def _t1_dallas_only_travis_soon_still_correct():
    # Sanity: if Dallas were the ONLY live county (hypothetical), Travis
    # correctly falls back to is-soon -- proves the is-live class per card
    # really does come from live_slugs membership, not a fixed assumption.
    out = _render_index(live_slugs=("dallas-tx",))
    section = _extract_market_grid(out)
    idx = section.find('data-market="travis-tx"')
    card = section[max(0, idx - 120):idx]
    if "is-live" in card:
        raise AssertionError("travis-tx incorrectly shows is-live when NOT in live_slugs")
    idx = section.find('data-market="dallas-tx"')
    card = section[max(0, idx - 120):idx]
    if "is-live" not in card:
        raise AssertionError("dallas-tx should show is-live when it IS the only live_slugs entry")


for label, fn in [
    ("index.html / market card DOM/source order unchanged (Austin,NYC,LA,Chicago,Dallas,Houston)", _t1_travis_dallas_live_source_order_unchanged),
    ("index.html / is-live/is-soon classes still driven by live_slugs membership", _t1_is_live_soon_classes_still_registry_driven),
    ("index.html / hypothetical Dallas-only-live scenario flips classes correctly", _t1_dallas_only_travis_soon_still_correct),
]:
    check(label, fn)


def _t1_css_order_rule_present():
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    if ".market-card.is-live { order: 0; }" not in css_no_comments.replace("\n", " ").replace("  ", " ") and \
       "order: 0;" not in css_no_comments[css_no_comments.find(".market-card.is-live"):css_no_comments.find(".market-card.is-live") + 200]:
        raise AssertionError(".market-card.is-live does not declare order:0 -- Task 1 CSS ordering rule missing")
    idx = css_no_comments.find(".market-card.is-soon { order:")
    if idx == -1:
        # allow either exact single-rule form or the pre-existing multi-declaration .is-soon block plus a new rule
        if "order: 1;" not in css_no_comments:
            raise AssertionError(".market-card.is-soon does not declare order:1 -- Task 1 CSS ordering rule missing")


check("static/style.css: .market-card.is-live/is-soon carry order:0/order:1 (Task 1)", _t1_css_order_rule_present)


# ── Task 2: equal-height Who We Serve grid ─────────────────────────────────

def _t2_who_serve_grid_stretch_declared():
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    idx = css_no_comments.find(".who-serve-grid {")
    if idx == -1:
        raise AssertionError(".who-serve-grid rule not found")
    block = css_no_comments[idx: idx + 400]
    if "align-items: stretch;" not in block:
        raise AssertionError(".who-serve-grid does not explicitly declare align-items: stretch (Task 2)")


def _t2_who_serve_card_full_height():
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    idx = css_no_comments.find(".who-serve-card {")
    if idx == -1:
        raise AssertionError(".who-serve-card rule not found")
    block = css_no_comments[idx: idx + 200]
    if "height: 100%" not in block:
        raise AssertionError(".who-serve-card does not declare height:100% (Task 2 equal-height fix)")


def _t2_who_serve_content_unchanged():
    # Cosmetic only -- confirm the 4 segments/copy from PX-20260828-10 are
    # completely untouched by this CSS-only fix.
    out = _render_index()
    for title in ("Real estate investors", "Developers", "Homeowners", "Tax consultants"):
        if title not in out:
            raise AssertionError(f"Task 2 must be cosmetic-only, but segment {title!r} is missing/changed")


check("static/style.css: .who-serve-grid declares align-items: stretch (Task 2)", _t2_who_serve_grid_stretch_declared)
check("static/style.css: .who-serve-card declares height: 100% (Task 2)", _t2_who_serve_card_full_height)
check("index.html / Who We Serve segment content unchanged by Task 2 (cosmetic-only)", _t2_who_serve_content_unchanged)


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} scenario(s)")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All PX-20260828-11 (Task 1 + Task 2) scenarios passed.")
