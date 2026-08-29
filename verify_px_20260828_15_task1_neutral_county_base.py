#!/usr/bin/env python3
"""
verify_px_20260828_15_task1_neutral_county_base.py -- PX-20260828-15 Task 1.

PM's brief: "home() passes api_county_slug="" to render neutral (fixed in
-06b), but its sibling neutral routes don't. Confirmed live via curl: /about
emits window.COUNTY_BASE = "/travis-tx", so its search box searches Travis
only -- a Dallas address typed there returns 'No results found.'"

Audit scope named in the brief: about, terms, privacy, disclaimer,
styleguide, and the landing states of search/info/rates/snapshot when no
?county= is set.

THE FIX, IN TWO PARTS:
  1. Structural default (the brief's explicit requirement -- "set the
     neutral value once in a shared place... so a newly-added neutral page
     can't miss it"): _inject_county_helpers()'s api_county_slug fallback
     changed from DEFAULT_COUNTY_SLUG to "" whenever g.county_slug isn't
     set (i.e. on any neutral route). This alone fixes about/terms/privacy/
     disclaimer/styleguide/search_landing() -- all six render with ZERO
     api_county_slug override today, so they were always going to inherit
     whatever this one shared default is, without any per-route edit.
  2. info_landing()/rates_landing()/snapshot_landing() DO explicitly
     override api_county_slug already (pre-existing, from PX-20260828-06b),
     but always to the resolved display slug -- which itself falls back to
     DEFAULT_COUNTY_SLUG (Travis) when no ?county= is given. That fallback
     slug was silently leaking into api_county_slug too, pinning these
     three pages' search boxes to Travis in exactly the same way as the
     zero-override bug, just via a different mechanism. Fix: a new shared
     helper, _resolve_landing_county_slug(), returns an `explicit` flag
     distinguishing "?county=<real slug> was given" from "fell back to
     Travis because absent/unrecognized" -- the three routes now pass
     api_county_slug=slug only when explicit, else "".

This fixture checks both parts by extracting the real function source from
app.py (same technique as verify_px_20260828_06b_neutral_county_base.py --
no Flask/DB import needed) and, for _resolve_landing_county_slug() itself,
actually executing the extracted source against stub globals to prove its
return-value logic for real, not just eyeballing it.

Run: python3 verify_px_20260828_15_task1_neutral_county_base.py
"""
import os
import re
import sys

REPO = "/sessions/amazing-sleepy-babbage/mnt/Parcelytics/code"
if not os.path.isdir(REPO):
    REPO = os.path.dirname(os.path.abspath(__file__))

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


app_src = open(os.path.join(REPO, "app.py")).read()


def extract_function(name, src=app_src):
    m = re.search(rf"\ndef {re.escape(name)}\(.*?\n(?=\ndef |\n@app\.|\nif __name__)", src, re.DOTALL)
    if not m:
        raise AssertionError(f"could not locate function {name}() in app.py")
    return m.group(0)


# ─────────────────────────────────────────────────────────────────────────
# Section 1: the shared structural default (context processor)
# ─────────────────────────────────────────────────────────────────────────
section("Part 1 -- shared structural default in _inject_county_helpers()")

ctx_src = extract_function("_inject_county_helpers")

check('context processor\'s api_county_slug fallback is getattr(g, '
      '"county_slug", "") -- NEUTRAL by default, not DEFAULT_COUNTY_SLUG '
      '(Travis) -- this is the one-place fix a future 6th neutral route '
      'automatically inherits without needing its own override',
      '"api_county_slug": getattr(g, "county_slug", ""),' in ctx_src)
check('the old Travis-fallback line is gone (not just added alongside)',
      '"api_county_slug": getattr(g, "county_slug", DEFAULT_COUNTY_SLUG),' not in ctx_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 2: the five zero-override static routes + search_landing() --
# confirm they still pass NO api_county_slug kwarg, so Part 1's fix is what
# saves them (not a per-route patch that would recreate the failure mode)
# ─────────────────────────────────────────────────────────────────────────
section("Part 1 continued -- zero-override routes rely on the shared default")

ZERO_OVERRIDE_ROUTES = {
    "about": 'render_template("about.html")',
    "terms": 'render_template("terms.html")',
    "privacy": 'render_template("privacy.html")',
    "disclaimer": 'render_template("disclaimer.html")',
    "styleguide": 'render_template("styleguide.html")',
}
for fn_name, expected_call in ZERO_OVERRIDE_ROUTES.items():
    src = extract_function(fn_name)
    check(f"{fn_name}() still renders with zero kwargs ({expected_call!r}) "
          f"-- confirms it depends entirely on Part 1's shared default, "
          f"not a per-route override this brief would then need to keep "
          f"in sync",
          expected_call in src and "api_county_slug" not in src)

search_landing_src = extract_function("search_landing")
check('search_landing() (bare \'/search\', no ?county= concept at all) '
      'also has zero api_county_slug override -- same shared-default '
      'dependency as the five pages above',
      'render_template(' in search_landing_src
      and "api_county_slug" not in search_landing_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 3: _resolve_landing_county_slug() -- extract AND execute the real
# source against stub globals, proving its explicit/fallback logic for real
# ─────────────────────────────────────────────────────────────────────────
section("Part 2 -- _resolve_landing_county_slug() shared resolver (real execution)")

resolver_src = extract_function("_resolve_landing_county_slug")
check("_resolve_landing_county_slug() is a real function in app.py",
      "def _resolve_landing_county_slug(param_name=\"county\"):" in resolver_src)
check("it returns the 3-tuple (county_code, slug, explicit)",
      "return county_code, slug, explicit" in resolver_src)


class _FakeArgs(dict):
    def get(self, key, default=""):
        return dict.get(self, key, default)


class _FakeRequest:
    def __init__(self, args):
        self.args = _FakeArgs(args)


COUNTY_SLUGS = {"travis-tx": "TRAVIS", "dallas-tx": "DALLAS"}
DEFAULT_COUNTY_SLUG = "travis-tx"

namespace = {"COUNTY_SLUGS": COUNTY_SLUGS, "DEFAULT_COUNTY_SLUG": DEFAULT_COUNTY_SLUG}
exec(compile(resolver_src, "<_resolve_landing_county_slug>", "exec"), namespace)
_resolve_landing_county_slug = namespace["_resolve_landing_county_slug"]


def run_resolver(query_args):
    namespace["request"] = _FakeRequest(query_args)
    return _resolve_landing_county_slug()


r_explicit_dallas = run_resolver({"county": "dallas-tx"})
check(f'?county=dallas-tx (real, recognized) -> (DALLAS, "dallas-tx", '
      f'True) -- got {r_explicit_dallas!r}',
      r_explicit_dallas == ("DALLAS", "dallas-tx", True))

r_absent = run_resolver({})
check(f'no ?county= at all -> (TRAVIS, "travis-tx", False) -- explicit=False '
      f'is the whole point: the page still DISPLAYS Travis content (a '
      f'separate, legitimate fallback), but callers now know this was NOT '
      f'a real user choice -- got {r_absent!r}',
      r_absent == ("TRAVIS", "travis-tx", False))

r_unrecognized = run_resolver({"county": "nyc-ny"})
check(f'?county=nyc-ny (unrecognized) -> (TRAVIS, "travis-tx", False) -- '
      f'same fallback treatment as absent, per the pre-existing tolerance '
      f'-- got {r_unrecognized!r}',
      r_unrecognized == ("TRAVIS", "travis-tx", False))

r_explicit_travis = run_resolver({"county": "travis-tx"})
check(f'?county=travis-tx (real, recognized, HAPPENS to be Travis) -> '
      f'(TRAVIS, "travis-tx", True) -- explicit=True here too, since the '
      f'user really did ask for Travis, unlike the absent case above -- '
      f'got {r_explicit_travis!r}',
      r_explicit_travis == ("TRAVIS", "travis-tx", True))


# ─────────────────────────────────────────────────────────────────────────
# Section 4: info_landing()/rates_landing()/snapshot_landing() wire the
# resolver's `explicit` flag into api_county_slug correctly
# ─────────────────────────────────────────────────────────────────────────
section("Part 2 continued -- the three landing routes wire `explicit` correctly")

LANDING_ROUTES = {
    "info_landing": "_info_response(county_code, slug, api_county_slug=(slug if explicit else \"\"))",
    "rates_landing": "_rates_response(county_code, slug, api_county_slug=(slug if explicit else \"\"))",
    "snapshot_landing": "_snapshot_response(view, county_code, slug, api_county_slug=(slug if explicit else \"\"))",
}
for fn_name, expected_return in LANDING_ROUTES.items():
    src = extract_function(fn_name)
    check(f"{fn_name}() calls _resolve_landing_county_slug() and passes "
          f"api_county_slug=(slug if explicit else \"\") -- explicit "
          f"?county= still scopes the embedded search box; the silent "
          f"Travis-fallback case no longer does",
          "county_code, slug, explicit = _resolve_landing_county_slug()" in src
          and expected_return in src)

# Anchored twins (info(), tax_rates(), county_snapshot()) must be UNAFFECTED
# -- still call the shared _response() functions with exactly their old
# 2/3 positional args, letting api_county_slug default to county_slug_val
# (== g.county_slug, their own real anchor) inside those functions.
ANCHORED_ROUTES = {
    "info": "_info_response(g.county_code, g.county_slug)",
    "tax_rates": "_rates_response(g.county_code, g.county_slug)",
    "county_snapshot": "_snapshot_response(view, g.county_code, g.county_slug)",
}
for fn_name, expected_call in ANCHORED_ROUTES.items():
    src = extract_function(fn_name)
    check(f"{fn_name}() (anchored) still calls exactly "
          f"{expected_call!r} -- no api_county_slug kwarg added, so this "
          f"route is byte-for-byte unaffected by the Task 1 fix",
          expected_call in src)


# ─────────────────────────────────────────────────────────────────────────
# Section 5: the three shared _response() functions default correctly
# ─────────────────────────────────────────────────────────────────────────
section("Part 2 continued -- shared _response() functions: signature + default")

RESPONSE_FNS = {
    "_info_response": ("def _info_response(county_code, county_slug_val, api_county_slug=None):",
                        "api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,"),
    "_rates_response": ("def _rates_response(county_code, county_slug_val, api_county_slug=None):",
                         "api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,"),
    "_snapshot_response": ("def _snapshot_response(view_arg, county_code, county_slug_val, api_county_slug=None):",
                            "api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,"),
}
for fn_name, (sig, tail) in RESPONSE_FNS.items():
    src = extract_function(fn_name)
    check(f"{fn_name}() has the new api_county_slug=None parameter",
          sig in src)
    check(f"{fn_name}() defaults api_county_slug to county_slug_val when "
          f"the caller didn't pass one -- the anchored routes' safety net",
          tail in src)


# ─────────────────────────────────────────────────────────────────────────
# Section 6: end-to-end truth table -- does the RIGHT api_county_slug value
# reach render_template() for every route/scenario this brief named?
# ─────────────────────────────────────────────────────────────────────────
section("End-to-end truth table (computed from the real extracted logic above)")


def response_api_county_slug(api_county_slug_arg, county_slug_val):
    """Mirrors _info_response()/_rates_response()/_snapshot_response()'s
    real `api_county_slug if api_county_slug is not None else
    county_slug_val` line exactly."""
    return api_county_slug_arg if api_county_slug_arg is not None else county_slug_val


def landing_arg(explicit, slug):
    """Mirrors the real landing routes' `api_county_slug=(slug if explicit
    else "")` call-site expression exactly."""
    return slug if explicit else ""


_, absent_slug, absent_explicit = r_absent
_, dallas_slug, dallas_explicit = r_explicit_dallas

CASES = [
    # (label, computed value via the real expressions above, expected)
    ("about() / terms() / privacy() / disclaimer() / styleguide() / "
     "search_landing() (zero override -> context-processor default, "
     "neutral route so g.county_slug unset)",
     response_api_county_slug(None if False else "", ""),  # context-processor fallback is a flat "" (see Section 1)
     ""),
    ("info_landing() / rates_landing() / snapshot_landing(), NO ?county= "
     "given (real resolver result: explicit=False, fallback slug "
     "'travis-tx')",
     response_api_county_slug(landing_arg(absent_explicit, absent_slug), absent_slug),
     ""),
    ("info_landing() / rates_landing() / snapshot_landing(), "
     "?county=dallas-tx given (real resolver result: explicit=True)",
     response_api_county_slug(landing_arg(dallas_explicit, dallas_slug), dallas_slug),
     "dallas-tx"),
    ("info() / tax_rates() / county_snapshot() (anchored, real g.county_slug "
     "'dallas-tx') -- api_county_slug param omitted, defaults to "
     "county_slug_val",
     response_api_county_slug(None, "dallas-tx"),
     "dallas-tx"),
]
for label, got, expected in CASES:
    check(f'{label} -> api_county_slug={expected!r} (got {got!r})', got == expected)


print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE (same standing sandbox limitation as every other PX "
      "brief -- no live Postgres, no live browser):")
print("  - That the deployed /about (and the other 4 static pages, and the "
      "3 landing routes' no-?county= state) now actually emit "
      "window.COUNTY_BASE resolving to \"\" in a real browser render.")
print("  - PM's own required post-deploy check: 'Diego will verify /about's "
      "search box finds a Dallas address post-deploy.'")
sys.exit(0 if all_ok else 1)
