#!/usr/bin/env python3
"""
verify_search_scroll_fix.py — render/fixture checks for USER-FEEDBACK-1
(search results/error not scrolling into view on the homepage hero
search). Reuses templates/index.html verbatim through a real Jinja
render (via verify_property_html_render.py's make_env() -- same url_for/
request/config stub convention already established in this repo, not
duplicated here).

WHAT THIS PROVES (and what it cannot):
  - scrollToResults() exists, by that exact name, and is wired to
    document's DOMContentLoaded.
  - #searchResults' data-search-happened attribute reflects the real
    server-side signal (`addr_matches or error`) for three real
    scenarios: fresh load (no search), a successful search with results,
    and a genuine no-results error -- not a client-side text-content
    guess.
  - scrollIntoView is called with behavior:"smooth".
  - The function's own guard condition matches data-search-happened,
    i.e. it will not fire on a fresh load.

WHAT THIS DOES NOT PROVE, per this brief's own verification requirement:
whether the page ACTUALLURL visibly scrolls in a real browser. That is
JS runtime behavior this Jinja-only render harness cannot execute or
observe -- Diego's own live browser check (the three real cases: fresh
load / successful search / no-results search) is required and is not a
substitute for the checks below, nor are the checks below a substitute
for it.

Run: python3 verify_search_scroll_fix.py
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_property_html_render import make_env

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


def render_index(**overrides):
    env = make_env()
    tpl = env.get_template("index.html")
    ctx = dict(mode="homeowner", q=None, error=None, addr_matches=None)
    ctx.update(overrides)
    return tpl.render(**ctx)


def _search_results_attr(html):
    m = re.search(r'<div id="searchResults"[^>]*data-search-happened="(true|false)"', html)
    return m.group(1) if m else None


def test_scroll_function_exists_and_is_named_scrollToResults():
    html = render_index()
    return check("scrollToResults() function is present in rendered HTML",
                 "function scrollToResults" in html)


def test_scroll_function_wired_to_dom_content_loaded():
    html = render_index()
    return check('scrollToResults is registered on DOMContentLoaded',
                 'document.addEventListener("DOMContentLoaded", scrollToResults)' in html)


def test_scroll_uses_smooth_behavior():
    html = render_index()
    ok = check('scrollIntoView call requests behavior:"smooth"', 'behavior: "smooth"' in html)
    ok = check('scrollIntoView call requests block:"start"', 'block: "start"' in html) and ok
    return ok


def test_scroll_gated_on_data_search_happened_attribute_not_text_content():
    html = render_index()
    ok = check('guard reads data-search-happened attribute',
               'getAttribute("data-search-happened")' in html)
    ok = check('guard does NOT use the old textContent-sniffing heuristic',
               "results.textContent" not in html) and ok
    return ok


def test_fresh_page_load_has_no_search_happened_false():
    """No search performed -- addr_matches and error both falsy/absent."""
    html = render_index(q=None, error=None, addr_matches=None)
    attr = check_attr = _search_results_attr(html)
    ok = check("fresh load: data-search-happened is 'false'", attr == "false", f"got {attr!r}")
    ok = check("fresh load: no results card rendered", 'class="card mt-3"' not in html) and ok
    ok = check("fresh load: no error block rendered", "search-error-block" not in html) and ok
    return ok


def test_successful_search_has_search_happened_true():
    # PX-20260828-03: county_slug (PX-20260827-03-rev1) and county_name
    # (this task's own addition) are now unconditionally present on every
    # row search_parcels_by_address() returns -- this fixture was stale
    # (missing both), which StrictUndefined caught the moment index.html's
    # disambiguation-list markup moved into the shared addr_match_results()
    # macro (templates/_macros.html) and started checking `m.county_name`'s
    # truthiness directly, rather than a permissive-Undefined harness
    # silently treating a missing key as falsy.
    html = render_index(
        q="1201 s lamar",
        addr_matches=[{"geo_id": "0100030105", "situs_address": "1201 S Lamar Blvd", "owner_name": "Test Owner",
                        "county_slug": "travis-tx", "county_name": "Travis County"}],
    )
    attr = _search_results_attr(html)
    ok = check("successful search: data-search-happened is 'true'", attr == "true", f"got {attr!r}")
    ok = check("successful search: results card is actually rendered", 'class="card mt-3"' in html) and ok
    ok = check("successful search: matched address text present", "1201 S Lamar Blvd" in html) and ok
    return ok


def test_no_results_error_has_search_happened_true():
    """The second real case the brief explicitly requires: a genuine
    'no results found' search must ALSO scroll -- same as a successful one."""
    html = render_index(
        q="zzz nonexistent address",
        error='No parcels found matching "zzz nonexistent address".',
    )
    attr = _search_results_attr(html)
    ok = check("no-results error: data-search-happened is 'true'", attr == "true", f"got {attr!r}")
    ok = check("no-results error: error block is actually rendered", "search-error-block" in html) and ok
    ok = check("no-results error: no results card rendered (this IS the no-results case)",
               'class="card mt-3"' not in html) and ok
    return ok


def test_404_parcel_lookup_error_also_gets_search_happened_true():
    """property_detail()'s 404 fallback (app.py ~line 2216) renders
    index.html with q=geo_id, error=<message>, addr_matches not passed
    (defaults to Undefined/falsy) -- confirms this third real call site
    of index.html also gets the fix automatically, since it's the same
    template."""
    html = render_index(q="9999999999", error='We couldn\'t find parcel "9999999999".')
    attr = _search_results_attr(html)
    return check("404 parcel-lookup fallback: data-search-happened is 'true'", attr == "true", f"got {attr!r}")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} SEARCH-SCROLL-FIX RENDER CHECKS PASSED")
    print()
    print("NOT PROVEN HERE (needs Diego's real browser check, per this brief's own")
    print("verification requirement): that the page actually, visibly scrolls in a")
    print("real browser for (a) a successful search and (b) a genuine no-results")
    print("search, and does NOT scroll on (c) a fresh page load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
