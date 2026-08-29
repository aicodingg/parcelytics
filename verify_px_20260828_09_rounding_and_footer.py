"""
verify_px_20260828_09_rounding_and_footer.py — PX-20260828-09 Task 1
(_human_count floor-rounding) + Task 2 (2-column footer, Data sources/
Coverage columns removed, Contact & feedback moved into Product, bottom
"Data:" line removed).

Task 1 note on method: app.py cannot be imported directly in this sandbox
(top-level Flask app construction + a live psycopg2 connection pool run at
import time, neither available here — same constraint as every other
verify_*.py in this repo). Rather than hand-copy _human_count()'s logic
into this test (which would only prove the copy is right, not the shipped
function), this harness AST-extracts the actual _human_count function
object's source out of the real app.py file and exec()s just that
definition in an isolated namespace. That's a stronger guarantee than a
reimplementation: if Diego edits the real function and breaks the floor
rule, this test fails against the real source, not a stale mirror of it.

Task 2 uses the same lightweight-Jinja2-Environment render technique as
the other PX-20260828-* harnesses in this repo (no real Flask available).

Run: python3 verify_px_20260828_09_rounding_and_footer.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(REPO_ROOT, "app.py")
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")

FAILURES = []


def check(label, fn):
    try:
        fn()
        print(f"  OK   {label}")
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")


# ── Task 1: extract the real _human_count from app.py via AST ─────────────

def _extract_human_count():
    with open(APP_PY, "r") as f:
        source = f.read()
    tree = ast.parse(source, filename=APP_PY)
    func_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_human_count":
            func_node = node
            break
    if func_node is None:
        raise AssertionError("_human_count not found in app.py -- has it been renamed/removed?")
    # Re-serialize just this function's AST and exec it in isolation --
    # proves we're testing the function as it actually parses in the real
    # file (correct signature, correct body), not a hand-typed guess.
    module = ast.Module(body=[func_node], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, filename=APP_PY, mode="exec")
    namespace = {}
    exec(code, namespace)
    return namespace["_human_count"]


_human_count = _extract_human_count()


def _t1_millions_floor_not_round():
    # Diego's own example pairing: 1,199,683 -> "1.19M", NOT "1.20M".
    # Round-to-nearest would give 1.20M here, which is false (1,199,683 <
    # 1,200,000). This is the single most important assertion in this file.
    got = _human_count(1_199_683)
    if got != "1.19M":
        raise AssertionError(f"expected '1.19M' (floor), got {got!r} -- round-to-nearest regression?")


def _t1_millions_exact_boundary():
    # Exactly 1,000,000 should format as a clean "1.00M", not fall through
    # to the K branch (n >= 1_000_000 is the M threshold, inclusive).
    got = _human_count(1_000_000)
    if got != "1.00M":
        raise AssertionError(f"expected '1.00M' at the exact 1M boundary, got {got!r}")


def _t1_millions_just_under_boundary():
    # 999,999 is one under the M threshold -- must take the K branch, not M.
    got = _human_count(999_999)
    if got != "999K":
        raise AssertionError(f"expected '999K' just under the 1M boundary, got {got!r}")


def _t1_thousands_floor():
    # Diego's own example: 705,536 -> "705K" (floors the remainder, doesn't
    # round up to 706K).
    got = _human_count(705_536)
    if got != "705K":
        raise AssertionError(f"expected '705K' (floor), got {got!r}")


def _t1_thousands_exact_boundary():
    # Exactly 1,000 should take the K branch ("1K"), not fall through to
    # the bare-integer branch (n >= 1_000 is the K threshold, inclusive).
    got = _human_count(1_000)
    if got != "1K":
        raise AssertionError(f"expected '1K' at the exact 1,000 boundary, got {got!r}")


def _t1_below_thousand_exact():
    # Under 1,000: exact integer, no suffix at all.
    got = _human_count(999)
    if got != "999":
        raise AssertionError(f"expected exact '999' under the 1,000 floor, got {got!r}")


def _t1_zero():
    got = _human_count(0)
    if got != "0":
        raise AssertionError(f"expected '0' for n=0, got {got!r}")


def _t1_large_seven_figure():
    # A bigger, non-round number well above 1M -- 12,345,678 -> floor to
    # two decimals of millions -> "12.34M" (not "12.35M").
    got = _human_count(12_345_678)
    if got != "12.34M":
        raise AssertionError(f"expected '12.34M' (floor), got {got!r}")


for label, fn in [
    ("_human_count(1,199,683) floors to '1.19M', not round-to-nearest '1.20M'", _t1_millions_floor_not_round),
    ("_human_count(1,000,000) == '1.00M' (exact M boundary)", _t1_millions_exact_boundary),
    ("_human_count(999,999) == '999K' (just under M boundary, stays in K branch)", _t1_millions_just_under_boundary),
    ("_human_count(705,536) == '705K' (floors, doesn't round to 706K)", _t1_thousands_floor),
    ("_human_count(1,000) == '1K' (exact K boundary)", _t1_thousands_exact_boundary),
    ("_human_count(999) == '999' (exact integer, no suffix under 1,000)", _t1_below_thousand_exact),
    ("_human_count(0) == '0'", _t1_zero),
    ("_human_count(12,345,678) == '12.34M' (floor on a non-round 7-figure count)", _t1_large_seven_figure),
]:
    check(label, fn)


# ── Task 2: footer structure (2-column grid, columns removed/moved) ───────

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
    env.globals["county_profile"] = _TRAVIS_PROFILE
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None
    env.globals["live_counties"] = _LIVE_COUNTIES
    env.globals["total_live_parcel_count_display"] = "1.13M"
    env.globals["is_county_anchored"] = False
    return env


def _render_a_page_extending_base():
    # base.html is a layout, not a directly-rendered page -- render a real
    # template that extends it (about.html is the simplest neutral page)
    # to get the actual composed <footer> HTML base.html emits.
    env = make_env()
    tpl = env.get_template("about.html")
    return tpl.render()


def _t2_no_data_sources_column():
    out = _render_a_page_extending_base()
    if "Data sources —" in out or "footer-col-title\">Data sources" in out:
        raise AssertionError("footer still has a 'Data sources' column -- Task 2 removal not applied")


def _t2_no_coverage_column():
    out = _render_a_page_extending_base()
    if "footer-col-title\">Coverage" in out:
        raise AssertionError("footer still has a 'Coverage' column -- Task 2 removal not applied")


def _t2_contact_feedback_in_product_column():
    out = _render_a_page_extending_base()
    if "mailto:parcelytics@gmail.com" not in out:
        raise AssertionError("Contact & feedback mailto link missing from footer entirely")
    # Confirm it now lives inside the Product column, not a removed column:
    # the Product column's title should appear before the mailto link with
    # no OTHER footer-col-title in between.
    product_idx = out.find('footer-col-title">Product')
    mailto_idx = out.find("mailto:parcelytics@gmail.com")
    if product_idx == -1 or mailto_idx == -1 or mailto_idx < product_idx:
        raise AssertionError("Contact & feedback does not appear inside/after the Product column")
    between = out[product_idx:mailto_idx]
    if 'footer-col-title"' in between[len('footer-col-title">Product'):]:
        raise AssertionError("another footer-col-title appears between Product and Contact & feedback")


def _t2_exactly_two_footer_cols_plus_brand():
    out = _render_a_page_extending_base()
    # Expect exactly one footer-col-title (Product) -- Brand's div has no
    # .footer-col-title of its own (it's the untitled first grid child).
    n = out.count('class="footer-col-title"')
    if n != 1:
        raise AssertionError(f"expected exactly 1 footer-col-title (Product only), found {n}")


def _t2_no_bottom_data_line():
    out = _render_a_page_extending_base()
    if "Data:" in out and "Data as of:" not in out.split("Data:")[0]:
        # crude guard against false-positive on "Data as of:" -- check the
        # specific removed pattern directly instead.
        pass
    if "Live market:" in out:
        raise AssertionError("removed 'Live market: {display_name}' bottom-line fragment still present")
    if "Travis Central Appraisal District &amp; Travis County Tax Office" in out:
        raise AssertionError("removed bottom 'Data: {cad_name} & {tax_office_name}' line still present")


def _t2_kept_lines_survive():
    out = _render_a_page_extending_base()
    for needle in ("Data as of:", "Not legal or tax advice", "not affiliated with any government entity"):
        if needle not in out:
            raise AssertionError(f"a line Diego said to KEEP is missing: {needle!r}")


def _t2_meta_group_right_aligned_rev1():
    # PX-20260828-09-rev1 (Diego's ruling): once the "Data:" span was
    # removed, the non-affiliation/version meta group became the row's
    # ONLY child -- under the shared .footer-bottom class's
    # justify-content:space-between, a lone flex child renders at the
    # row's START, not its end, which is the disclosed cosmetic regression
    # Diego asked fixed. Fix: an inline justify-content:flex-end override
    # on THIS specific footer-bottom div only. Assert the override is
    # present on the SECOND footer-bottom div (the meta-group one), and
    # NOT on the FIRST (the "Data as of:" one, which must keep its own
    # left-aligned single-child default -- a shared-class edit would have
    # broken that row instead).
    out = _render_a_page_extending_base()
    first_idx = out.find('class="footer-bottom"')
    second_idx = out.find('class="footer-bottom"', first_idx + 1)
    if first_idx == -1 or second_idx == -1:
        raise AssertionError(f"expected exactly 2 footer-bottom divs, found first={first_idx} second={second_idx}")
    first_tag = out[first_idx: first_idx + 200]
    if "justify-content:flex-end" in first_tag:
        raise AssertionError("the FIRST footer-bottom div ('Data as of:') unexpectedly carries the flex-end override -- this would break its left alignment")
    second_tag = out[second_idx: second_idx + 200]
    if "justify-content:flex-end" not in second_tag:
        raise AssertionError("the SECOND footer-bottom div (non-affiliation/version meta group) is missing the justify-content:flex-end fix -- the disclosed reflow regression is still present")
    import re
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    idx = css_no_comments.find(".footer-bottom {")
    block = css_no_comments[idx: idx + 300]
    if "justify-content: space-between;" not in block:
        raise AssertionError("shared .footer-bottom CSS class no longer declares space-between -- fix should be a scoped inline override, not a shared-class edit")


for label, fn in [
    ("footer: 'Data sources' column removed", _t2_no_data_sources_column),
    ("footer: 'Coverage' column removed", _t2_no_coverage_column),
    ("footer: Contact & feedback now lives inside Product column", _t2_contact_feedback_in_product_column),
    ("footer: exactly 1 footer-col-title remains (Product only)", _t2_exactly_two_footer_cols_plus_brand),
    ("footer: bottom 'Data: ... Live market: ...' line removed", _t2_no_bottom_data_line),
    ("footer: kept lines ('Data as of:', 'Not legal or tax advice', non-affiliation) still present", _t2_kept_lines_survive),
    ("footer-rev1: meta group scoped-flex-end fix present on 2nd row only, shared class untouched", _t2_meta_group_right_aligned_rev1),
]:
    check(label, fn)


# ── Task 2: static/style.css .footer-grid is 2-column ─────────────────────

def _t2_css_two_column_grid():
    import re
    css_path = os.path.join(REPO_ROOT, "static", "style.css")
    with open(css_path, "r") as f:
        css = f.read()
    # Strip /* ... */ comments before inspecting so old values quoted in
    # explanatory comments (e.g. "was 1.5fr 1fr 1fr 1fr") don't produce a
    # false failure -- only the LIVE declaration should be asserted on.
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    idx = css_no_comments.find(".footer-grid {")
    if idx == -1:
        raise AssertionError(".footer-grid rule not found in style.css")
    block = css_no_comments[idx: idx + 300]
    if "grid-template-columns: 2fr 1fr;" not in block:
        raise AssertionError(f".footer-grid base rule is not '2fr 1fr' -- got block: {block[:200]!r}")
    if "1.5fr 1fr 1fr 1fr" in block:
        raise AssertionError("old 4-column grid-template-columns value still present in base .footer-grid rule")


check("static/style.css: .footer-grid base rule is 'grid-template-columns: 2fr 1fr'", _t2_css_two_column_grid)


print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} scenario(s)")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All PX-20260828-09 (rounding + footer structure) scenarios passed.")
