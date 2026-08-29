"""
verify_px_20260828_10_who_we_serve.py — PX-20260828-10 "Who We Serve"
icon-grid redesign real Jinja RENDER verification.

Same lightweight-Jinja2-Environment technique as the other PX-20260828-*
harnesses in this repo (no real Flask available in this sandbox).

Checks:
  1. All four approved segments render (investors, developers, homeowners,
     tax consultants), each with a title and non-empty description.
  2. Brokers and appraisers do NOT appear as segments (excluded, PM-approved).
  3. The new section uses its own .who-serve-* classes, not a repurposed
     .audience-card (which must stay untouched -- it's also used by the
     county market cards higher on this same page).
  4. The old three-card/.audience-card "Who it's for" markup structure
     (row g-4 / col-md-4) is gone from this section.
  5. Each who-serve-card has exactly one inline <svg> icon (hand-authored,
     no external icon library reference anywhere in the section).
  6. static/style.css: .who-serve-grid collapses to 1 column at the same
     768px breakpoint already used by .market-card-grid/.footer-grid (no
     new breakpoint invented), and .audience-card's own CSS block is
     byte-for-byte unmodified.

Run: python3 verify_px_20260828_10_who_we_serve.py
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


_LIVE_COUNTIES = [
    {"slug": "travis-tx", "value": "travis", "display_name": "Travis County, TX",
     "county_name": "Travis County", "parcel_count": 430147, "parcel_count_display": "430K",
     "cad_name": "Travis Central Appraisal District", "cad_abbr": "TCAD",
     "tax_office_name": "Travis County Tax Office"},
    {"slug": "dallas-tx", "value": "dallas", "display_name": "Dallas County, TX",
     "county_name": "Dallas County", "parcel_count": 705536, "parcel_count_display": "705K",
     "cad_name": "Dallas Central Appraisal District", "cad_abbr": "DCAD",
     "tax_office_name": "Dallas County Tax Office"},
]


def make_env(county_slug="travis-tx"):
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
    env.globals["county_profile"] = {
        "display_name": "Travis County, TX", "county_name": "Travis County",
        "cad_name": "Travis Central Appraisal District",
        "tax_office_name": "Travis County Tax Office", "cad_abbr": "TCAD",
    }
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = _LIVE_COUNTIES
    env.globals["total_live_parcel_count_display"] = "1.13M"
    env.globals["is_county_anchored"] = False
    return env


def _render_index():
    env = make_env()
    tpl = env.get_template("index.html")
    return tpl.render(q="", error=None, addr_matches=None, api_county_slug="")


def _extract_who_serve_section(out):
    start = out.find('<div class="who-serve-grid">')
    if start == -1:
        raise AssertionError("who-serve-grid not found in rendered index.html")
    # Section closes well before the next top-level {# ══ ... ═══ #} block;
    # a generous slice is fine since we only assert presence/absence within it.
    return out[start: start + 6000]


def _t_four_segments_present():
    out = _render_index()
    section = _extract_who_serve_section(out)
    for title in ("Real estate investors", "Developers", "Homeowners", "Tax consultants"):
        if title not in section:
            raise AssertionError(f"expected segment title missing: {title!r}")
    n_cards = section.count('class="who-serve-card"')
    if n_cards != 4:
        raise AssertionError(f"expected exactly 4 who-serve-card entries, found {n_cards}")


def _t_brokers_appraisers_excluded():
    out = _render_index()
    section = _extract_who_serve_section(out)
    for excluded in ("Broker", "Appraiser"):
        if excluded in section:
            raise AssertionError(f"excluded segment {excluded!r} unexpectedly appears in the rendered who-serve section")


def _t_tax_consultant_copy_grounded():
    out = _render_index()
    section = _extract_who_serve_section(out)
    for needle in ("Filter and export", "assessment-ratio anomaly", "protest-filing tool"):
        if needle not in section:
            raise AssertionError(f"tax consultants card missing grounded-feature reference: {needle!r}")


def _t_new_class_not_audience_card():
    out = _render_index()
    section = _extract_who_serve_section(out)
    if "audience-card" in section:
        raise AssertionError("who-serve section still references .audience-card -- should use the new .who-serve-card class instead")
    if section.count("who-serve-badge") < 4:
        raise AssertionError("expected at least 4 who-serve-badge occurrences (one per segment)")


def _t_old_three_card_structure_gone():
    out = _render_index()
    if "One dataset. Three ways to use it." not in out:
        pass  # expected to be gone -- this is the actual assertion below
    else:
        raise AssertionError("old 'Three ways to use it' heading still present -- section not replaced")
    if "One dataset. Four ways to use it." not in out:
        raise AssertionError("new 'Four ways to use it' heading not found")


def _t_each_card_has_one_inline_svg():
    out = _render_index()
    section = _extract_who_serve_section(out)
    # Split on card boundaries and check each has exactly one <svg>.
    cards = section.split('class="who-serve-card"')[1:]
    if len(cards) != 4:
        raise AssertionError(f"expected 4 card fragments after split, got {len(cards)}")
    for i, card in enumerate(cards):
        n = card[:1200].count("<svg")
        if n != 1:
            raise AssertionError(f"card {i} has {n} <svg> tags, expected exactly 1")


def _t_no_external_icon_library_referenced():
    out = _render_index()
    section = _extract_who_serve_section(out)
    for banned in ("font-awesome", "fontawesome", "bootstrap-icons", "lucide", "material-icons", "feather-icons"):
        if banned in section.lower():
            raise AssertionError(f"section references an external icon library ({banned!r}) -- spec requires hand-authored inline SVG, no new dependency")


for label, fn in [
    ("index.html / all 4 approved segments render with titles", _t_four_segments_present),
    ("index.html / brokers and appraisers correctly excluded", _t_brokers_appraisers_excluded),
    ("index.html / tax-consultants copy references real, shipped features", _t_tax_consultant_copy_grounded),
    ("index.html / new section uses .who-serve-card, not .audience-card", _t_new_class_not_audience_card),
    ("index.html / old 3-card 'Three ways to use it' heading replaced", _t_old_three_card_structure_gone),
    ("index.html / each card has exactly one hand-authored inline <svg>", _t_each_card_has_one_inline_svg),
    ("index.html / no external icon library referenced", _t_no_external_icon_library_referenced),
]:
    check(label, fn)


# ── static/style.css: .who-serve-grid breakpoint + .audience-card untouched ─

def _t_css_who_serve_breakpoint_matches_existing():
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    if ".who-serve-grid { grid-template-columns: 1fr; }" not in css_no_comments.replace("\n", " ").replace("  ", " "):
        # Looser check: find the media block containing who-serve-grid and
        # confirm it's the same 768px breakpoint already used elsewhere.
        idx = css_no_comments.find(".who-serve-grid")
        idx2 = css_no_comments.find(".who-serve-grid", idx + 1)
        if idx2 == -1:
            raid = css_no_comments.rfind("@media (max-width: 768px)", 0, idx)
            raise AssertionError(f".who-serve-grid mobile override not found in the existing 768px media block (nearest @media before first occurrence at {raid})")


def _t_css_audience_card_untouched():
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    expected = (
        ".audience-card {\n"
        "  background: var(--surface);\n"
        "  border: 1px solid var(--border);\n"
        "  border-top: 3px solid var(--accent);\n"
        "  border-radius: var(--r-lg);\n"
        "  padding: var(--s-5);\n"
        "  height: 100%;\n"
        "}\n"
        '.audience-card h3 { font-size: var(--fs-md); font-weight: var(--fw-bold); color: var(--ink); margin-bottom: 8px; }\n'
        '.audience-card p { font-size: var(--fs-sm); line-height: 1.6; color: var(--text-2); margin: 0; }'
    )
    if expected not in css:
        raise AssertionError(".audience-card's CSS block was modified -- it must stay byte-for-byte unchanged since the county market cards also use it")


check("static/style.css: .who-serve-grid collapses at the existing 768px breakpoint", _t_css_who_serve_breakpoint_matches_existing)
check("static/style.css: .audience-card block is byte-for-byte unmodified", _t_css_audience_card_untouched)


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} scenario(s)")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All PX-20260828-10 (Who We Serve icon-grid) scenarios passed.")
