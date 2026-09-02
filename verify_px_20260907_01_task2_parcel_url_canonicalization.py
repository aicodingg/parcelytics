#!/usr/bin/env python3
"""
verify_px_20260907_01_task2_parcel_url_canonicalization.py -- PX-20260907-01
Task 2.

PM's brief (live-verify residual on parcel 0189128): '/parcel/0284460113
?county=dallas-tx' (a real Dallas geo_id) 301-redirected to
'/travis-tx/parcel/0284460113?county=dallas-tx' -- a Dallas parcel under a
Travis path slug, with the query param carrying the actual truth and never
being read by the redirect logic at all.

ROOT CAUSE, confirmed by reading the code (not assumed): '/parcel/<geo_id>'
and '/parcel/<geo_id>/export.pdf' are legacy, pre-county-prefix compat
routes (_LEGACY_REDIRECT_ROUTES). Every entry in that list used to run
through the same generic _make_legacy_redirect(), which builds its target
via plain `url_for(target_endpoint, geo_id=...)` -- no county_slug of its
own -- and _add_county_slug()'s url_defaults hook then fills that gap with
`getattr(g, "county_slug", DEFAULT_COUNTY_SLUG)`. A request at a bare,
unprefixed '/parcel/...' path has no g.county_slug at all (this route has
no <county_slug> segment), so this ALWAYS resolved to DEFAULT_COUNTY_SLUG
('travis-tx') -- for every geo_id, Dallas's included, regardless of any
'?county=' query param (which was never read for resolution purposes; it
just rode along, unexamined, as part of the preserved querystring).

THE FIX: these two geo_id-keyed routes are pulled out of the generic
_LEGACY_REDIRECT_ROUTES list and given their own resolver
(_make_legacy_parcel_redirect()) that looks up the PARCEL'S REAL county
via resolve_exact_parcel(geo_id, county_code=None) -- the exact same
cross-county lookup _resolve_quick_search() already uses for the neutral
home/search-landing "type an account number" box (PX-20260828-03/-06b),
which itself loops over every live county since geo_id is only unique
WITHIN a county (SPEC_COUNTY_PARTITIONING.md §3's own investigated
finding -- a bare geo_id genuinely cannot be resolved to the right county
without checking real data). The redirect target's county_slug now comes
from a new shared helper, _slug_for_county_code() (replacing
_resolve_quick_search()'s own former inline copy of the same expression --
"one source of truth", per the brief), applied to the RESOLVED parcel's
real county_code -- never a default, and the incoming '?county=' param is
deliberately never trusted for this decision (it's exactly the kind of
unverified input this whole brief exists to stop treating as
authoritative). An unresolvable geo_id (genuinely bad/nonexistent, not
merely wrong-county) falls through to the SAME DEFAULT_COUNTY_SLUG-based
redirect as before, landing on property_detail()'s own existing, unchanged
404 handling -- this fix only changes the outcome for a geo_id that DOES
exist somewhere.

This fixture cannot open a real Postgres connection (no DB in this
sandbox -- same standing limitation as every prior PX brief's own
fixtures), so Section 2 extracts the real `_view` closure's source from
app.py and executes it against a stub `resolve_exact_parcel`/`url_for`/
`request`/`redirect`, proving the real shipped logic branches correctly
for a Dallas geo_id, a Travis geo_id, and an unresolvable one -- not just
eyeballing the diff.

Run: python3 verify_px_20260907_01_task2_parcel_url_canonicalization.py
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
# Section 1: static shape -- the two geo_id routes are OUT of the generic
# list, _make_legacy_parcel_redirect() exists and is registered for both,
# and the new _slug_for_county_code() helper is the one source of truth
# used by BOTH the redirect and _resolve_quick_search()
# ─────────────────────────────────────────────────────────────────────────
section("Section 1 -- static wiring")

legacy_list_block = re.search(
    r"_LEGACY_REDIRECT_ROUTES = \[.*?\n\]", app_src, re.DOTALL,
).group(0)
check('"/parcel/<geo_id>" is NOT in the generic _LEGACY_REDIRECT_ROUTES list',
      '("/parcel/<geo_id>", "property_detail")' not in legacy_list_block)
check('"/parcel/<geo_id>/export.pdf" is NOT in the generic list either',
      '("/parcel/<geo_id>/export.pdf", "export_due_diligence_pdf")' not in legacy_list_block)

check("_slug_for_county_code() is defined",
      "def _slug_for_county_code(county_code):" in app_src)

resolver_src = extract_function("_slug_for_county_code")
check("_slug_for_county_code() does the COUNTY_SLUGS reverse-lookup, "
      "falling back to DEFAULT_COUNTY_SLUG only for a truly-unregistered "
      "county_code",
      "for slug, code in COUNTY_SLUGS.items() if code == county_code" in resolver_src
      and "DEFAULT_COUNTY_SLUG," in resolver_src)

quick_search_src = extract_function("_resolve_quick_search")
check("_resolve_quick_search()'s redirect now calls the SHARED helper "
      "(no more of its own inline COUNTY_SLUGS.items() copy -- the "
      "brief's explicit 'fix from one single source of truth' ask)",
      "match_slug = _slug_for_county_code(parcel[\"county_code\"])" in quick_search_src
      and quick_search_src.count("for slug, code in COUNTY_SLUGS.items()") == 0)

parcel_redirect_src = extract_function("_make_legacy_parcel_redirect")
check("_make_legacy_parcel_redirect() exists",
      "def _make_legacy_parcel_redirect(target_endpoint):" in parcel_redirect_src)
check("its _view() calls resolve_exact_parcel(geo_id, county_code=None) "
      "-- the real cross-county lookup, not a default",
      "resolve_exact_parcel(geo_id, county_code=None)" in parcel_redirect_src)
check("it derives the slug from the SAME shared helper "
      "(_slug_for_county_code), from the RESOLVED parcel's own "
      "county_code -- never a hardcoded default when a parcel is found",
      'slug = _slug_for_county_code(parcel["county_code"]) if parcel else DEFAULT_COUNTY_SLUG'
      in parcel_redirect_src)
check("only falls back to DEFAULT_COUNTY_SLUG when the parcel genuinely "
      "isn't found anywhere (preserves today's honest 404 behavior for a "
      "truly bad geo_id, changes nothing there)",
      "if parcel else DEFAULT_COUNTY_SLUG" in parcel_redirect_src)
check("preserves the real query string on redirect (an existing "
      "?county=... or ?view=... bookmark must not silently lose its "
      "params)",
      "qs = request.query_string.decode()" in parcel_redirect_src
      and 'target = f"{target}?{qs}"' in parcel_redirect_src)
check("still a real 301 (permanent) redirect -- PM's brief explicitly "
      "wants 301s from the wrong-slug form, not a softer 302",
      "return redirect(target, code=301)" in parcel_redirect_src)

check('property_detail is registered against the new resolver',
      re.search(
          r'\(\s*"/parcel/<geo_id>",\s*"property_detail"\s*\),\s*\n\s*'
          r'\(\s*"/parcel/<geo_id>/export\.pdf",\s*"export_due_diligence_pdf"\s*\),\s*\n\s*\):\s*\n'
          r'\s*app\.add_url_rule\(\s*\n\s*_old_path,\s*\n\s*endpoint=f"\{_endpoint\}__legacy_redirect",\s*\n'
          r'\s*view_func=_make_legacy_parcel_redirect\(_endpoint\),',
          app_src,
      ) is not None)


# ─────────────────────────────────────────────────────────────────────────
# Section 2: execute the REAL extracted _view() closure against stub
# resolve_exact_parcel/url_for/request/redirect -- proves the shipped
# logic, not just the source text, produces the right target for a
# Dallas geo_id, a Travis geo_id, and an unresolvable one.
# ─────────────────────────────────────────────────────────────────────────
section("Section 2 -- real extracted _view() logic, executed against stubs")

view_body_match = re.search(
    r"    def _view\(geo_id, \*\*kwargs\):\n(?:        .*\n)+", parcel_redirect_src,
)
if not view_body_match:
    raise AssertionError("could not extract _make_legacy_parcel_redirect()'s _view() body")
view_src = "def _view(geo_id, resolve_exact_parcel, url_for, request, redirect, target_endpoint, **kwargs):\n" \
    + view_body_match.group(0).split("\n", 1)[1]

COUNTY_SLUGS = {"travis-tx": "TRAVIS", "dallas-tx": "DALLAS"}
DEFAULT_COUNTY_SLUG = "travis-tx"


def _slug_for_county_code(county_code):
    return next((slug for slug, code in COUNTY_SLUGS.items() if code == county_code), DEFAULT_COUNTY_SLUG)


class _FakeRequest:
    def __init__(self, qs=""):
        self._qs = qs

    @property
    def query_string(self):
        class _B:
            def __init__(self, s):
                self._s = s

            def decode(self):
                return self._s
        return _B(self._qs)


def _fake_url_for(endpoint, **kw):
    return f"/{kw['county_slug']}/parcel/{kw['geo_id']}" + ("/export.pdf" if endpoint == "export_due_diligence_pdf" else "")


class _FakeRedirectResult:
    def __init__(self, target, code):
        self.target, self.code = target, code

    def __repr__(self):
        return f"redirect({self.target!r}, code={self.code})"


PARCELS_BY_GEO_ID = {
    "0284460113": {"geo_id": "0284460113", "county_code": "DALLAS"},
    "0100030105": {"geo_id": "0100030105", "county_code": "TRAVIS"},
}


def _fake_resolve_exact_parcel(geo_id, county_code=None):
    assert county_code is None, "must search cross-county, not pin to a default"
    return PARCELS_BY_GEO_ID.get(geo_id)


namespace = {
    "DEFAULT_COUNTY_SLUG": DEFAULT_COUNTY_SLUG,
    "_slug_for_county_code": _slug_for_county_code,
}
exec(compile(view_src, "<_view>", "exec"), namespace)
_view = namespace["_view"]


def run_view(geo_id, qs, target_endpoint="property_detail"):
    return _view(
        geo_id,
        resolve_exact_parcel=_fake_resolve_exact_parcel,
        url_for=_fake_url_for,
        request=_FakeRequest(qs),
        redirect=lambda t, code: _FakeRedirectResult(t, code),
        target_endpoint=target_endpoint,
    )


# The brief's own literal fixture: a real Dallas geo_id, requested with a
# STALE/misleading '?county=dallas-tx' on the bare legacy path (exactly
# PM's repro) -- must land on /dallas-tx/, not /travis-tx/.
r = run_view("0284460113", "county=dallas-tx")
check(f"Dallas geo_id 0284460113 (?county=dallas-tx on the bare legacy "
      f"path) -> {r.target!r} (expected '/dallas-tx/parcel/0284460113"
      f"?county=dallas-tx')",
      r.target == "/dallas-tx/parcel/0284460113?county=dallas-tx" and r.code == 301)

# Same Dallas geo_id with NO query string at all -- must still resolve to
# Dallas from the real data, not fall back to Travis just because there
# was no '?county=' hint this time (the param was never trusted either way).
r = run_view("0284460113", "")
check(f"Dallas geo_id 0284460113 with NO query string -> {r.target!r} "
      f"(expected '/dallas-tx/parcel/0284460113', still Dallas -- proves "
      f"the fix resolves from real data, not from the query param)",
      r.target == "/dallas-tx/parcel/0284460113" and r.code == 301)

# Fixture: Travis parcels are unchanged.
r = run_view("0100030105", "")
check(f"Travis geo_id 0100030105 -> {r.target!r} (expected "
      f"'/travis-tx/parcel/0100030105', unchanged)",
      r.target == "/travis-tx/parcel/0100030105" and r.code == 301)

# A Dallas geo_id even with a WRONG/misleading '?county=travis-tx' hint --
# must still land on Dallas (real data wins over an untrusted, possibly
# stale or manually-edited query param in either direction).
r = run_view("0284460113", "county=travis-tx")
check(f"Dallas geo_id 0284460113 with a WRONG '?county=travis-tx' hint -> "
      f"{r.target!r} (still Dallas -- the param is never trusted for "
      f"resolution, only carried through unchanged on the querystring)",
      r.target == "/dallas-tx/parcel/0284460113?county=travis-tx" and r.code == 301)

# A genuinely nonexistent geo_id -- preserves today's exact behavior:
# falls through to DEFAULT_COUNTY_SLUG, landing on property_detail()'s own
# real, unchanged 404 render (this fixture proves only the REDIRECT target
# here, not property_detail()'s DB-backed 404 body, which needs a live DB).
r = run_view("9999999999", "")
check(f"Nonexistent geo_id 9999999999 -> {r.target!r} (expected "
      f"'/travis-tx/parcel/9999999999', same DEFAULT_COUNTY_SLUG fallback "
      f"as before this fix -- property_detail() itself still 404s there, "
      f"unchanged)",
      r.target == "/travis-tx/parcel/9999999999" and r.code == 301)

# export.pdf variant -- same resolver, same slug logic, different target
# endpoint/path shape.
r = run_view("0284460113", "", target_endpoint="export_due_diligence_pdf")
check(f"export.pdf variant, Dallas geo_id -> {r.target!r} (expected "
      f"'/dallas-tx/parcel/0284460113/export.pdf')",
      r.target == "/dallas-tx/parcel/0284460113/export.pdf" and r.code == 301)


# ─────────────────────────────────────────────────────────────────────────
# Section 3: "every place canonical property URLs are generated" audit --
# the brief's own checklist (redirects, <link rel=canonical>, sitemap,
# internal links from search results and compare)
# ─────────────────────────────────────────────────────────────────────────
section("Section 3 -- canonical-URL generation site audit")

templates_dir = os.path.join(REPO, "templates")
all_template_src = ""
for fname in os.listdir(templates_dir):
    if fname.endswith(".html"):
        with open(os.path.join(templates_dir, fname), encoding="utf-8") as f:
            all_template_src += f.read()

check("no <link rel=\"canonical\"> tag exists anywhere in templates/ -- "
      "nothing to fix there, but noted in the report as a real SEO gap "
      "given this brief's own wrong-slug-URL history (a canonical tag "
      "would make search engines converge on the right URL even if a "
      "stale wrong-slug link stays indexed)",
      'rel="canonical"' not in all_template_src and "rel='canonical'" not in all_template_src)
check("no sitemap.xml / sitemap route exists anywhere in the repo -- "
      "same disclosure as above, nothing broken, nothing to fix",
      not re.search(r"sitemap", app_src, re.IGNORECASE))

macros_src = open(os.path.join(templates_dir, "_macros.html")).read()
check("_macros.html's addr_match_results (search-result disambiguation "
      "list, used by both index.html and search.html) ALREADY builds its "
      "property_detail link with an explicit, per-row county_slug -- "
      "url_for('property_detail', geo_id=m.geo_id, county_slug=m.county_"
      "slug) -- confirmed correct, no fix needed here",
      "url_for('property_detail', geo_id=m.geo_id, county_slug=m.county_slug)" in macros_src)

compare_route_src = extract_function("compare_parcels")
check("compare_parcels() is registered under /<county_slug>/compare "
      "(confirmed via its own docstring cross-reference below) and scopes "
      "EVERY geo_id in ?ids= to the SAME g.county_code for every lookup -- "
      "meaning every parcel shown on one render of the compare page is "
      "already, structurally, from one county. compare.html's own "
      "internal property links (url_for('property_detail', geo_id=p."
      "geo_id), no explicit county_slug) inherit _add_county_slug()'s "
      "g.county_slug fallback correctly BECAUSE of that structural "
      "single-county scoping -- not a second instance of this bug",
      'county_code = g.county_code' in compare_route_src)
check("compare_parcels() itself is registered with a real <county_slug> "
      "path segment (not a bare route) -- so g.county_slug IS set for "
      "every real render of this page",
      '@app.route("/<county_slug>/compare")' in app_src
      and re.search(r'@app\.route\("/<county_slug>/compare"\)\s*\n(?:@\w[\w.]*\([^\n]*\)\s*\n)*def compare_parcels\(\):', app_src) is not None)


print()
if all_ok:
    print("ALL CHECKS PASSED")
else:
    print("SOME CHECKS FAILED")
print()
print("NOT PROVEN HERE (standing sandbox limitation -- no Flask, no live "
      "Postgres, no live browser in this environment):")
print("  - A real HTTP request to '/parcel/0284460113?county=dallas-tx' "
      "against a running app+DB, confirming the actual 301 Location "
      "header is '/dallas-tx/parcel/0284460113?county=dallas-tx'.")
print("  - property_detail()'s own DB-backed 404 render for a genuinely "
      "nonexistent geo_id (unchanged by this fix, not re-verified here).")
print("  - The bare '/compare' and '/parcels' legacy redirects (still "
      "generic, still default to Travis) -- disclosed in the final report "
      "as a related, pre-existing, NOT-fixed-here gap: compare_parcels() "
      "itself has no cross-county support to resolve a mixed-county ?ids= "
      "list against in the first place, a larger, separate design "
      "question than 'find the URL builder for one geo_id'.")
print("  - Whether any external site/search engine currently has the old "
      "'/travis-tx/parcel/<dallas-geo-id>' form indexed -- flagged as an "
      "SEO note for Diego in the final report, not something checkable "
      "from this sandbox.")
sys.exit(0 if all_ok else 1)
