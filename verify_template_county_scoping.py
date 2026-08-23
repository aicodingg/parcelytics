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
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

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


def scan_file(filepath: Path) -> list:
    raw = filepath.read_text()
    text = _strip_comments(raw)
    rel = f"templates/{filepath.name}"
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

    return findings


def run_audit(templates_dir: Path = TEMPLATES_DIR) -> list:
    findings = []
    for filepath in sorted(templates_dir.glob("*.html")):
        findings.extend(scan_file(filepath))
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
