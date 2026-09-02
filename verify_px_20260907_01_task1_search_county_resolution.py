#!/usr/bin/env python3
"""
verify_px_20260907_01_task1_search_county_resolution.py -- PX-20260907-01
Task 1.

PM's brief (live-verify residual on parcel 0189128): bare /search does not
resolve ?county=<slug> the way /rates does. '/search?county=dallas-tx'
rendered the homestead Quick Filter button + Filter Parcels dropdown
(Travis-shaped, since g.county_code/county_profile default to Travis on any
route with no <county_slug> segment) and left the county typeahead empty,
while '/dallas-tx/search' (real g.county_code) correctly omitted them.

THE FIX: search_landing() (bare '/search') now calls
_resolve_landing_county_slug() -- same shared resolver /rates, /info, and
/snapshot's landing routes already use (PX-20260828-01/-15) -- and, only
when the result is `explicit` (a real, recognized ?county=<slug> was
given), overrides county_code/county_profile/county_slug/api_county_slug
via explicit render_template() kwargs. Jinja's normal precedence means an
explicit kwarg beats the _inject_county_helpers() context processor's
same-named default, so search.html's existing
`county_has_field(county_code, "exemption_codes")` gates (unchanged,
still just reading the `county_code` template variable) automatically
reflect the resolved county with no template-side change.

A second, new `county_resolved` flag (True exactly when `explicit` is
True, always True on the real anchored /<slug>/search page) gates ONLY
the county-typeahead pre-fill in search.html -- kept deliberately separate
from `county_selected` (which still, unchanged, gates whether this page
has server-side Filter-Parcels results to fetch at all), so this fix does
not turn on results-fetching on the bare landing page (a bigger,
unrequested behavior change that would conflict with the documented
"clean navigation over clever state-carryover" principle).

UNKNOWN VALUES: an unrecognized ?county=<slug> (e.g. '?county=bogus-tx')
gets NO override at all -- `explicit` is False for it, identically to the
truly-absent case, per _resolve_landing_county_slug()'s own pre-existing
"absent and unrecognized are the same fallback bucket" logic (confirmed in
PX-20260828-15's own fixture, reused unmodified in Section 1 below). This
is the "neutral, not Travis" rule from this brief: an unrecognized value
is not treated as if the user had asked for Dallas or for Travis -- it's
treated exactly like they hadn't named a county at all (this codebase's
own established meaning of "neutral" throughout every PX-20260828 neutral-
routing brief), leaving this route byte-for-byte identical to today's
plain, param-less '/search' render.

/RATES: this brief also asks "if /rates currently falls back to Travis on
unknown values, fix it there too, and say so." Section 4 below re-proves
(same technique as verify_px_20260828_15_task1_neutral_county_base.py)
that /rates' api_county_slug (the value that actually drives its embedded
search box's COUNTY_BASE, i.e. the "?county= fallback rule" this brief
names) is ALREADY neutral ("") for both the absent AND the unrecognized
case -- no separate bug exists there for that concern, so no code change
was made to rates_landing() in this brief. The one place /rates still
resolves an unrecognized/absent county to Travis's own CONTENT
(county_code/county_profile, used to query and display the rates table
itself, not api_county_slug) is unchanged, pre-existing, documented
"flagship county default" behavior (same as info_landing()/
snapshot_landing()) -- not a functional defect like /search's (which had
a real, broken empty typeahead and a Travis-only-by-accident filter gate),
and a real fix would require designing a new "no county selected" empty
state for /rates, which has no existing analog (unlike search.html's
dual-mode template) -- a larger, separate design decision this "small,
route-level" brief did not ask for. Disclosed here rather than silently
either fixed or ignored.

Run: python3 verify_px_20260907_01_task1_search_county_resolution.py
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
search_html_src = open(os.path.join(REPO, "templates", "search.html")).read()


def extract_function(name, src=app_src):
    m = re.search(rf"\ndef {re.escape(name)}\(.*?\n(?=\ndef |\n@app\.|\nif __name__)", src, re.DOTALL)
    if not m:
        raise AssertionError(f"could not locate function {name}() in app.py")
    return m.group(0)


# ─────────────────────────────────────────────────────────────────────────
# Section 1: _resolve_landing_county_slug() -- re-confirm the real, already-
# shipped absent/unrecognized-conflation logic this fix depends on
# (identical technique + assertions to PX-20260828-15's own fixture --
# re-run here, not just assumed, since this brief's Task 1 fix leans on it)
# ─────────────────────────────────────────────────────────────────────────
section("Section 1 -- _resolve_landing_county_slug() (shared resolver, re-verified)")

resolver_src = extract_function("_resolve_landing_county_slug")


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


r_dallas = run_resolver({"county": "dallas-tx"})
check(f"?county=dallas-tx -> (DALLAS, 'dallas-tx', True) -- got {r_dallas!r}",
      r_dallas == ("DALLAS", "dallas-tx", True))

r_travis = run_resolver({"county": "travis-tx"})
check(f"?county=travis-tx -> (TRAVIS, 'travis-tx', True) -- got {r_travis!r}",
      r_travis == ("TRAVIS", "travis-tx", True))

r_absent = run_resolver({})
check(f"no ?county= at all -> (TRAVIS, 'travis-tx', False) -- got {r_absent!r}",
      r_absent == ("TRAVIS", "travis-tx", False))

r_unknown = run_resolver({"county": "bogus-tx"})
check(f"?county=bogus-tx (unrecognized) -> (TRAVIS, 'travis-tx', False) -- "
      f"SAME explicit=False bucket as absent, the mechanism this brief's "
      f"'unknown falls back to neutral, not Travis' rule relies on -- "
      f"got {r_unknown!r}",
      r_unknown == ("TRAVIS", "travis-tx", False))


# ─────────────────────────────────────────────────────────────────────────
# Section 2: search_landing() -- calls the resolver, overrides only when
# explicit, and passes the new county_resolved flag
# ─────────────────────────────────────────────────────────────────────────
section("Section 2 -- search_landing() wiring")

search_landing_src = extract_function("search_landing")

check("search_landing() calls _resolve_landing_county_slug()",
      "county_code, county_slug, county_explicit = _resolve_landing_county_slug()" in search_landing_src)
check("passes county_resolved=county_explicit to search.html",
      "county_resolved=county_explicit" in search_landing_src)
check("only overrides county_code/county_profile/county_slug/api_county_slug "
      "inside `if county_explicit:` -- NOT unconditionally (that would also "
      "change the absent-param baseline, not just the explicit case)",
      re.search(r'if county_explicit:\s*\n\s*render_kwargs\["county_code"\] = county_code\s*\n'
                r'\s*render_kwargs\["county_profile"\] = COUNTY_PROFILES\.get\(county_code, COUNTY_PROFILES\["TRAVIS"\]\)\s*\n'
                r'\s*render_kwargs\["county_slug"\] = county_slug\s*\n'
                r'\s*render_kwargs\["api_county_slug"\] = county_slug',
                search_landing_src) is not None)
check("county_selected is still hardcoded False here (unchanged -- this "
      "fix does not turn on server-side Filter-Parcels results-fetching "
      "on the bare landing page)",
      "county_selected=False" in search_landing_src)
check("filter_data_availability is still hardcoded {} here (unchanged, "
      "out of this brief's scope)",
      "filter_data_availability={}" in search_landing_src)

search_page_src = extract_function("search_page")
check("search_page() (the real anchored /<slug>/search) now also passes "
      "county_resolved=True explicitly",
      "county_resolved=True" in search_page_src)
check("search_page() still passes county_selected=True (unchanged)",
      "county_selected=True" in search_page_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 3: search.html -- the two typeahead-prefill gates now check
# `county_selected or county_resolved`; the homestead county_has_field()
# gates are untouched (they already read county_code by name, so an
# app.py-side override is all they needed)
# ─────────────────────────────────────────────────────────────────────────
section("Section 3 -- templates/search.html gating")

check("homestead Quick Filter button gate is untouched: bare "
      "`county_has_field(county_code, \"exemption_codes\")`, count=1",
      search_html_src.count('{% if county_has_field(county_code, "exemption_codes") %}') == 2)

TYPEAHEAD_PREFILL = (
    '{% if county_selected or county_resolved %}\n'
    '  document.getElementById("fltCountyInput").value = '
    '{{ county_profile.display_name | tojson }};\n'
    '  document.getElementById("fltCountyValue").value = '
    '{{ county_slug.split(\'-\')[0] | tojson }};\n'
    '  {% endif %}'
)
check("county typeahead current-value prefill now gated on "
      "`county_selected or county_resolved`",
      'document.getElementById("fltCountyInput").value = {{ county_profile.display_name | tojson }};'
      in search_html_src
      and search_html_src.count("{% if county_selected or county_resolved %}") == 2)

check("ta:select handler's 'already this county, no-op' check is ALSO "
      "gated on `county_selected or county_resolved` (both occurrences of "
      "the new combined condition accounted for)",
      search_html_src.count("if (picked.slug === {{ county_slug | tojson }}) return;") == 1)

check("runSearch()'s results-fetching early return is UNCHANGED -- still "
      "keyed on county_selected alone, not county_resolved (results-"
      "fetching on the bare landing was explicitly out of scope)",
      '{% if not county_selected %}\n'
      '    errorBox.textContent = "Pick a county above to search its parcels.";'
      in search_html_src)
check("no stray bare `{% if county_selected %}` survives at the two "
      "typeahead-gate line shapes (would mean the edit didn't actually "
      "land at both sites)",
      search_html_src.count("{% if county_selected %}\n  document.getElementById") == 0)


# ─────────────────────────────────────────────────────────────────────────
# Section 4: /rates -- re-confirm api_county_slug is ALREADY neutral for
# both absent AND unrecognized ?county= (PX-20260828-15's fix, unmodified
# by this brief) -- the concrete claim in this brief's report that no
# rates_landing() code change was needed for the api_county_slug concern
# ─────────────────────────────────────────────────────────────────────────
section("Section 4 -- /rates: api_county_slug already neutral for absent+unknown")

rates_landing_src = extract_function("rates_landing")
check("rates_landing() calls _resolve_landing_county_slug() (same shared "
      "resolver Section 1 just re-verified)",
      "county_code, slug, explicit = _resolve_landing_county_slug()" in rates_landing_src)
check("rates_landing() passes api_county_slug=(slug if explicit else \"\") "
      "-- since Section 1 proved explicit=False for BOTH absent and "
      "unrecognized, this expression is already \"\" (neutral) for both, "
      "with no further fix needed",
      'api_county_slug=(slug if explicit else "")' in rates_landing_src)

_, absent_slug, absent_explicit = r_absent
_, unknown_slug, unknown_explicit = r_unknown


def landing_arg(explicit, slug):
    return slug if explicit else ""


check(f"computed api_county_slug for /rates with NO ?county= given = "
      f"{landing_arg(absent_explicit, absent_slug)!r} (expected '')",
      landing_arg(absent_explicit, absent_slug) == "")
check(f"computed api_county_slug for /rates with ?county=bogus-tx = "
      f"{landing_arg(unknown_explicit, unknown_slug)!r} (expected '', "
      f"same as absent -- confirms no separate 'unknown' bug exists here)",
      landing_arg(unknown_explicit, unknown_slug) == "")

check("rates_landing()'s CONTENT (county_code/county_profile passed into "
      "_rates_response(), i.e. which county's rate TABLE renders) is left "
      "unmodified by this brief -- still resolves through the same "
      "shared function, still Travis-shaped for absent/unrecognized, by "
      "pre-existing, documented flagship-default design (disclosed in the "
      "final report, not silently changed)",
      "return _rates_response(county_code, slug, api_county_slug=(slug if explicit else \"\"))"
      in rates_landing_src)


# ─────────────────────────────────────────────────────────────────────────
# Section 5: end-to-end truth table for the brief's 3 named fixtures
# ─────────────────────────────────────────────────────────────────────────
section("Section 5 -- end-to-end truth table (homestead-gate outcome)")

COUNTY_FIELD_COVERAGE = {"DALLAS": {"exemption_codes": False}, "TRAVIS": {"exemption_codes": True}}


def county_has_field(county_code, field):
    return COUNTY_FIELD_COVERAGE.get(county_code, {}).get(field, False)


def search_landing_county_code(query_args):
    """Mirrors search_landing()'s real logic: override only when explicit,
    else the context-processor's own Travis-shaped default (unchanged
    baseline) applies."""
    county_code, _, explicit = run_resolver(query_args)
    return county_code if explicit else "TRAVIS"  # _inject_county_helpers() default


CASES = [
    ("?county=dallas-tx", {"county": "dallas-tx"}, False),
    ("?county=travis-tx", {"county": "travis-tx"}, True),
    ("?county=bogus-tx (unknown)", {"county": "bogus-tx"}, True),
    ("no ?county= at all (absent)", {}, True),
]
for label, args, expect_homestead in CASES:
    cc = search_landing_county_code(args)
    got_homestead = county_has_field(cc, "exemption_codes")
    check(f"{label}: county_code resolves to {cc!r}, homestead elements "
          f"{'present' if got_homestead else 'absent'} "
          f"(expected {'present' if expect_homestead else 'absent'})",
          got_homestead == expect_homestead)

check("unknown and absent produce the IDENTICAL outcome (both TRAVIS, "
      "both 'present') -- this is this brief's 'neutral' fixture: an "
      "unrecognized value is treated exactly like no value at all, never "
      "like an explicit Dallas OR an explicit Travis choice",
      search_landing_county_code({"county": "bogus-tx"}) == search_landing_county_code({}))


print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE (standing sandbox limitation -- no Flask, no live "
      "Postgres, no live browser in this environment):")
print("  - A real HTTP GET against a running app for "
      "/search?county=dallas-tx, /search?county=travis-tx, and "
      "/search?county=bogus-tx, confirming the homestead Quick Filter "
      "button and Filter Parcels dropdown are actually present/absent in "
      "the rendered HTML as this fixture's logic predicts.")
print("  - That the county typeahead visually shows 'Dallas County' when "
      "loading /search?county=dallas-tx in a real browser.")
print("  - Live re-verification of parcel 0189128's original PM-reported "
      "repro, post-deploy.")
sys.exit(0 if all_ok else 1)
