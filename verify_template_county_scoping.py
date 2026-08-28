#!/usr/bin/env python3
"""
verify_template_county_scoping.py — PX-20260823-03 Task 4: the amended
Dallas gate's TEMPLATE-LAYER check.

Companion to verify_county_scoping.py (MC2-BUILD-1's loader-side auditor).
That tool audits every INSERT/UPDATE/DELETE in app.py/loaders/tax_logic for
missing county_code scoping. This tool audits the OTHER place a hardcoded,
county-unaware path can silently break multi-county routing: the Jinja
templates themselves.

Why this is a real, separate gate and not covered by the loader-side tool:
county prefixing (app.py's `/<county_slug>/...` routes, `_add_county_slug`'s
`url_for()` auto-injection, per PX-20260822-04-ADDENDUM-2-rev1) only works
if every internal link and JS fetch() actually GOES THROUGH `url_for()` (or,
for JS, the `COUNTY_BASE` constant that itself comes from `url_for('index')`
in base.html). A template that hardcodes `href="/snapshot"` or
`fetch('/api/rates')` bypasses that mechanism entirely -- it will always
resolve to whatever DEFAULT_COUNTY_SLUG happens to be (today, Travis),
regardless of which county's page the user is actually looking at. Before
this PX-20260823-03 pass, this was the case for all 49 hardcoded paths
found across templates/*.html; those paths only "worked" because
_LEGACY_REDIRECT_ROUTES' 301 redirects silently papered over it.

This scanner checks two independent violation classes:

  (A) A hardcoded, absolute, county-unaware app-route path used as an
      href/action/fetch target -- i.e. a literal string or JS template
      literal that starts with one of this app's real route prefixes
      (mirrors app.py's `_LEGACY_REDIRECT_ROUTES`' old-path shapes:
      /search, /snapshot, /parcel, /parcels, /rates, /compare, /info,
      /about, /terms, /privacy, /disclaimer, /styleguide, /api/*).
      Whitelisted automatically: genuinely external URLs (they start with
      a scheme like https://, so they never match the "/<prefix>" pattern
      in the first place), static-asset references (this app always uses
      `{{ url_for('static', filename=...) }}`, never a raw `/static/...`
      href, so "static" is deliberately NOT in the route-prefix list),
      fragment-only links (`href="#..."`), `javascript:` pseudo-links, and
      any occurrence immediately preceded by the `COUNTY_BASE` JS constant
      (base.html's `{{ url_for('index') | tojson }}`-derived county-aware
      base for JS-built URLs) -- those are the one sanctioned "build a
      county-aware path in JS" mechanism, not a second bypass.

  (B) A `request.path` string comparison against a route literal (e.g.
      `{% if request.path == '/search' %}`) -- this is what silently broke
      base.html's nav active-state highlighting the moment county
      prefixing landed (real paths becamse `/travis-tx/search`, so a
      literal `'/search'` comparison stopped matching, ever). The fix is
      always `request.endpoint == '<endpoint_name>'`, which is immune to
      any URL prefix by construction.

PX-20260828-02: this scanner now also globs static/*.js (previously
templates/*.html only) -- the confirmed-live bug that brief fixed
(static/parcel-typeahead.js hardcoding "/api/address_search" and
"/parcel/<geo_id>", used by all four search boxes site-wide) is the exact
Class (A) violation this scanner exists to catch, and it had a real,
unexamined blind spot for shared JS assets. See scan_file()'s own
docstring for the full reasoning.

Both violation classes are checked against the literal template TEXT with
Jinja `{# ... #}` comments and HTML `<!-- ... -->` comments stripped first
(blanked out character-for-character so line numbers stay accurate) --
otherwise an explanatory code comment that merely QUOTES an example old
path (as several of this repo's own PX-20260823-02/03 fix comments do)
would itself trip the scanner. This is the same category of self-inflicted
false positive verify_county_scoping.py hit with docstrings during
PX-20260823-02 -- solved here at the source (strip comments before
scanning) rather than by rewording every comment to avoid the pattern.

Acceptance bar (per brief): zero findings after Tasks 1-3 land, and a
fixture test proving the scanner actually fires on a planted violation of
EACH class (the alarm-must-fire rule) -- see
test_verify_template_county_scoping.py.

PX-20260827-01 Task 3 adds a third, independent violation class:

  (C) A hardcoded, county-institution-specific string or domain (a real CAD
      abbreviation, CAD/tax-office name, or domain literal registered in
      app.py's COUNTY_PROFILES) appearing inside an <a>...</a> anchor tag's
      href or visible text, instead of being sourced from
      county_profile/county_cad_link(). This is the generalized version of
      the Travis/TCAD leak this brief found hardcoded into property.html's
      Helpful Links cards: a denylist, not a one-off pattern match, derived
      dynamically from COUNTY_PROFILES via static source parsing (see
      _load_county_institution_denylist()) so it grows automatically the
      day a future county (e.g. Harris) gets a real profile entry, with no
      edit to this file required. Scoped to <a> tags specifically (not all
      template text) because this repo has real, disclosed, single-county
      educational pages (info.html, compare.html) that cite real government
      documents by name in ordinary prose -- see EXEMPT_FILES for the
      narrow, reasoned exception for those two pages' own <a> tags.
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_PY = Path(__file__).resolve().parent / "app.py"

# Real route prefixes this app owns (mirrors app.py's _LEGACY_REDIRECT_ROUTES'
# old, pre-county-prefix path shapes -- the exact set of paths that used to
# be real routes and now only survive via a 301 redirect). Deliberately does
# NOT include "static": this app always links static assets via
# {{ url_for('static', filename=...) }}, never a raw href, so a literal
# /static/... path would be a DIFFERENT, not-yet-seen problem worth its own
# finding class, not silently swept into this one.
ROUTE_PREFIXES = (
    "search",
    "snapshot",
    "parcel",
    "parcels",
    "rates",
    "compare",
    "info",
    "about",
    "terms",
    "privacy",
    "disclaimer",
    "styleguide",
    "api",
)

_ROUTE_PREFIX_ALT = "|".join(ROUTE_PREFIXES)

# Class (A): a quote/backtick immediately followed by one of the route
# prefixes above. Matching starts strictly at the open-quote boundary so
# that a COUNTY_BASE-prefixed template literal like `${COUNTY_BASE}/api/x`
# never matches (the character right after the backtick there is "$", not
# "/") -- only a route path that STARTS the string/template-literal is a
# real violation.
HARDCODED_PATH_RE = re.compile(
    r"""(?P<quote>["'`])(?P<path>/(?:""" + _ROUTE_PREFIX_ALT + r""")\b[^"'`]*)(?P=quote)"""
)

# Class (B): request.path directly compared with == or != (either operand
# order). Deliberately does NOT match a bare mention of "request.path" in
# prose (e.g. an explanatory comment) -- only an actual comparison.
REQUEST_PATH_CMP_RE = re.compile(
    r"request\.path\s*(?:==|!=)|(?:==|!=)\s*request\.path"
)

# How many characters immediately before a Class-A match to inspect for the
# COUNTY_BASE token -- covers every real shape this repo uses:
# `${COUNTY_BASE}/api/...`, `COUNTY_BASE + '/compare?...'`,
# `COUNTY_BASE + "/api/billing/"`. 40 chars comfortably covers all of them
# with room to spare.
_COUNTY_BASE_LOOKBACK = 40

# ── Class (C), PX-20260827-01 Task 3: hardcoded county-institution references ──
# The self-enforcing part of this brief: a denylist of every real CAD/tax-office
# abbreviation, name, and domain currently registered in app.py's
# COUNTY_PROFILES, checked against every <a ...>...</a> anchor tag's full span
# (href attribute AND visible link text) in every template. This is what
# caught the original bug (property.html hardcoding "TCAD" / traviscad.org
# for every county), and -- because the denylist is DERIVED from
# COUNTY_PROFILES via static source parsing rather than hand-typed here --
# it grows automatically the day Harris (or any future county) gets a real
# entry, with zero edit to this file required. That's the difference between
# "a scanner that caught today's known leak" and "a scanner that catches
# tomorrow's leak too," which is what the brief actually asked for.
#
# Scope is deliberately "inside <a> tags," not "anywhere in the template
# text": this repo has real, disclosed, single-county educational/reference
# pages (info.html, compare.html) that cite real Travis government documents
# by name in ordinary prose ("TCAD's own guidance...", "Travis County Tax
# Office — Truth in Taxation Summary") -- that prose is not a bug, it's
# accurate citation of a real source on a page that already discloses "Only
# Texas / Travis County are built out today" and gates its content via
# data-county="travis" panels, a DIFFERENT and already-honest county-scoping
# mechanism from the one this brief is about (institutional ACTION links --
# apply/protest/pay/contact -- rendered from county_profile). Restricting to
# <a> tags catches exactly the violation class this brief is about (a link
# whose target or label silently assumes one county) without alarming on
# every citation of a real document by its real name.
#
# EXEMPT_FILES: pages whose <a> tags legitimately cite real, county-specific
# institutional names/URLs as disclosed SOURCE CITATIONS on an
# already-single-county-scoped educational page, not as user-facing action
# links meant to work for every county. Each entry names the file and the
# reason -- same visibility bar as verify_county_scoping.py's EXEMPTIONS
# registry (PX-20260823-02). Adding a file here is not a decision this
# scanner should be trusted to make silently: if a future page's exemption
# claim doesn't hold up (e.g. it turns out to be a general-purpose page that
# should have been profile-driven), that's a Fable-reviewable call, not a
# scanner bug.
EXEMPT_FILES = {
    "info.html": (
        "Learn/education page: cites real Travis government and regulatory "
        "documents by name (TCAD FAQ pages, Travis County Tax Office "
        "publications, Texas Comptroller PDFs) as disclosed SOURCE "
        "CITATIONS inside data-county=\"travis\" panels. The page's own "
        "header text already discloses 'Only Texas / Travis County are "
        "built out today; more states will show here as Parcelytics "
        "expands' -- this is honest, structurally-scoped, single-county "
        "reference content, not a silent institutional-link leak."
    ),
    "compare.html": (
        "Same data-county-gated educational-page pattern as info.html; its "
        "only hardcoded-CAD-string occurrence is prose explaining a "
        "methodology footnote, not an <a> tag, but listed here defensively "
        "in case future edits add a real Travis citation link to this page."
    ),
}


def _load_county_institution_denylist():
    """Statically parses app.py's real COUNTY_PROFILES dict (via `ast`, the
    same static-analysis technique verify_county_scoping.py already uses on
    this same file -- no live app.py import, so no Flask/Sentry/DB
    side-effects in what's meant to be a fast, dependency-free CI check).
    Returns (abbrs, names_and_domains): abbrs is matched as a whole word
    (TCAD/DCAD/...); names_and_domains is matched as a substring (full CAD
    names, tax-office names, and every URL value's hostname, www.-stripped).
    Falls back to an empty denylist (scanner finds nothing, never crashes)
    if app.py's shape ever changes enough that this parse can't find
    COUNTY_PROFILES -- a loud print(), not a silent False, so CI notices."""
    abbrs = set()
    names_and_domains = set()
    try:
        tree = ast.parse(APP_PY.read_text())
    except (OSError, SyntaxError) as exc:
        print(f"WARNING: could not parse {APP_PY} for COUNTY_PROFILES ({exc}) "
              f"-- Class (C) denylist will be empty this run.")
        return abbrs, names_and_domains

    profiles_dict = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "COUNTY_PROFILES" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            profiles_dict = node.value
            break

    if profiles_dict is None:
        print("WARNING: COUNTY_PROFILES assignment not found in app.py "
              "-- Class (C) denylist will be empty this run.")
        return abbrs, names_and_domains

    for county_node in profiles_dict.values:
        if not isinstance(county_node, ast.Dict):
            continue
        for key_node, val_node in zip(county_node.keys, county_node.values):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            if not (isinstance(val_node, ast.Constant) and isinstance(val_node.value, str)):
                continue  # None values (unresolved links) carry nothing to denylist
            key, val = key_node.value, val_node.value
            if key == "cad_abbr":
                abbrs.add(val)
            elif key in ("cad_name", "tax_office_name"):
                names_and_domains.add(val)
            elif "://" in val:
                host = urlsplit(val).netloc
                if host:
                    names_and_domains.add(host)
                    if host.startswith("www."):
                        names_and_domains.add(host[len("www."):])
    return abbrs, names_and_domains


# Anchor-tag span: opening <a ...> through its closing </a>, non-greedy so
# adjacent, unrelated anchors on the same line don't get merged into one
# match. DOTALL via re.S so a multi-line anchor (this repo's real style,
# e.g. property.html's Helpful Links hrefs) is still one span.
_ANCHOR_TAG_RE = re.compile(r"<a\b.*?</a>", re.IGNORECASE | re.DOTALL)


def _find_hardcoded_cad_institution_findings(text: str, rel: str):
    """Class (C): scans every <a>...</a> span in `text` (already
    comment-stripped) for a denylisted CAD abbreviation, name, or domain not
    sourced from county_profile. Skipped entirely for EXEMPT_FILES."""
    if Path(rel).name in EXEMPT_FILES:
        return []
    abbrs, names_and_domains = _load_county_institution_denylist()
    if not abbrs and not names_and_domains:
        return []
    abbr_re = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in sorted(abbrs)) + r")\b") if abbrs else None
    findings = []
    for anchor in _ANCHOR_TAG_RE.finditer(text):
        span = anchor.group(0)
        hit = None
        if abbr_re is not None:
            m = abbr_re.search(span)
            if m:
                hit = m.group(0)
        if hit is None:
            for token in names_and_domains:
                if token in span:
                    hit = token
                    break
        if hit is None:
            continue
        findings.append(Finding(
            rel, _line_number(text, anchor.start()), "hardcoded-cad-institution", "FAIL",
            f"Anchor tag hardcodes {hit!r} -- a real county-institution "
            f"name/domain found in COUNTY_PROFILES, but not sourced from "
            f"county_profile/county_cad_link() here. This link will render "
            f"the same for every county regardless of which county's page "
            f"the user is on. Use {{{{ county_profile.<field> }}}} or "
            f"county_cad_link('<field>', ...) instead; if no real URL is "
            f"confirmed for this county yet, render the link absent or a "
            f"clearly-labeled 'not yet available' state -- never a "
            f"hardcoded fallback to another county's value.",
        ))
    return findings


def _strip_comments(text: str) -> str:
    """Blanks Jinja {# ... #} and HTML <!-- ... --> comments, replacing every
    non-newline character with a space so line numbers (and therefore
    findings' reported line numbers) stay accurate. Necessary because this
    repo's own PX-20260823-03 fix comments quote example old-style paths
    (e.g. "against a bare, unprefixed route literal (e.g. '/search')") as
    part of explaining the fix -- without stripping, the auditor would flag
    its own explanatory comments, the exact false-positive class
    verify_county_scoping.py hit with docstrings under PX-20260823-02."""

    def _blank(m: "re.Match") -> str:
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))

    text = re.sub(r"\{#.*?#\}", _blank, text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.DOTALL)
    # JS block comments (`/* ... */`) -- added PX-20260828-02 when this
    # scanner was extended to also cover static/*.js: parcel-typeahead.js's
    # own file-header comment uses this style (not `//`), and this fix's
    # own explanatory addition to that header quotes the exact hardcoded
    # strings being fixed ("/api/address_search", "/parcel/<geo_id>") --
    # the identical "a comment quoting an example old path" false-positive
    # class this function already exists to prevent for Jinja/HTML/`//`
    # comments, just in a comment style this scanner never had to handle
    # before JS files were in scope. Stripped BEFORE the `//` pass below so
    # a `//` that happens to appear inside a `/* ... */` block (none do
    # today, but it's a real possibility) can't corrupt this regex's own
    # `*/` boundary detection.
    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.DOTALL)
    # JS line comments (`// ...` to end of line) -- added PX-20260827-03-rev1
    # after this repo's own Task 1 routing-split comments (documenting the
    # new bare '/search' landing route) quoted the route name in single
    # quotes inside a `//` comment, tripping Class-A on a comment, the exact
    # false-positive class this function already exists to prevent for
    # Jinja/HTML comments. Negative lookbehind for ':' so a real URL embedded
    # in JS (e.g. `d3.json("https://cdn.jsdelivr.net/...")`) is never
    # blanked -- only an actual `//` comment marker (never immediately
    # preceded by ':') is stripped. Safe for this codebase's JS specifically
    # because JS has no `//` operator (unlike Python's integer division) and
    # this repo doesn't use protocol-relative `//host/path` URLs anywhere.
    text = re.sub(r"(?<!:)//[^\n]*", _blank, text)
    return text


@dataclass
class Finding:
    filepath: str
    lineno: int
    kind: str        # "hardcoded-path" | "request-path-cmp"
    severity: str     # "FAIL" (this tool has no PASS/EXEMPT notion yet)
    detail: str


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_file(filepath: Path, rel_dir: str = "templates") -> list:
    """rel_dir names the directory this file is reported under in findings
    (e.g. "templates" or "static") -- PX-20260828-02 extends this scanner
    to also cover static/*.js: the SAME violation class (a hardcoded,
    county-unaware app-route path bypassing url_for()/COUNTY_BASE) was
    confirmed live in static/parcel-typeahead.js (fetch("/api/address_
    search...") and window.location.href = "/parcel/..."), a file this
    scanner never looked at because it only ever globbed templates/*.html.
    That JS file is shared by all four search boxes site-wide, so this was
    a real, live, high-blast-radius gap in this exact scanner's own
    coverage -- closed here rather than left for the next JS file to slip
    through the same way."""
    raw = filepath.read_text()
    text = _strip_comments(raw)
    rel = f"{rel_dir}/{filepath.name}"
    findings = []

    for m in HARDCODED_PATH_RE.finditer(text):
        start = m.start()
        lookback = text[max(0, start - _COUNTY_BASE_LOOKBACK):start]
        if "COUNTY_BASE" in lookback:
            continue  # sanctioned JS county-aware base, not a bypass
        path = m.group("path")
        findings.append(Finding(
            rel, _line_number(text, start), "hardcoded-path", "FAIL",
            f"Hardcoded, county-unaware app-route path {path!r} -- bypasses "
            f"url_for()/COUNTY_BASE, so this link always resolves to "
            f"DEFAULT_COUNTY_SLUG regardless of which county's page the "
            f"user is on. Use {{{{ url_for(...) }}}} (Jinja context) or the "
            f"COUNTY_BASE JS constant (script context) instead.",
        ))

    for m in REQUEST_PATH_CMP_RE.finditer(text):
        findings.append(Finding(
            rel, _line_number(text, m.start()), "request-path-cmp", "FAIL",
            "request.path compared against a route literal -- breaks the "
            "moment a county prefix is present in the real path (e.g. "
            "'/travis-tx/search' != '/search', forever). Compare "
            "request.endpoint against the real endpoint name instead -- "
            "immune to any URL prefix by construction.",
        ))

    findings.extend(_find_hardcoded_cad_institution_findings(text, rel))

    return findings


def run_audit(templates_dir: Path = TEMPLATES_DIR, static_dir: Path = STATIC_DIR) -> list:
    findings = []
    for filepath in sorted(templates_dir.glob("*.html")):
        findings.extend(scan_file(filepath, rel_dir="templates"))
    # PX-20260828-02: static/*.js is now scanned too -- see scan_file()'s
    # own docstring for why. Class (C) (hardcoded CAD institution strings
    # inside <a> tags) is still checked against this text (no separate
    # gating needed): plain .js files have no <a> tags in practice, so it
    # simply finds nothing there, not a false-positive risk.
    for filepath in sorted(static_dir.glob("*.js")):
        findings.extend(scan_file(filepath, rel_dir="static"))
    return findings


def print_report(findings: list) -> None:
    fails = [f for f in findings if f.severity == "FAIL"]
    print(f"verify_template_county_scoping.py -- template-layer county-scoping audit")
    print(f"Findings: {len(findings)} ({len(fails)} FAIL)")
    print()
    if not fails:
        print("No failures.")
        return
    by_kind = {}
    for f in fails:
        by_kind.setdefault(f.kind, []).append(f)
    for kind, items in sorted(by_kind.items()):
        print(f"── {kind} ({len(items)}) " + "─" * 40)
        for f in items:
            print(f"  FAIL {f.filepath}:{f.lineno} -- {f.detail}")
        print()


def main() -> int:
    findings = run_audit()
    print_report(findings)
    return 1 if any(f.severity == "FAIL" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
