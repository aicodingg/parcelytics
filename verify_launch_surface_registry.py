#!/usr/bin/env python3
"""
verify_launch_surface_registry.py — PX-20260827-03-rev1 Task 4a: the
"what's live" cross-surface drift gate.

Context: this brief's own Task 1/2/3 work found that "which counties are
live" used to be answered independently in (at least) two places:

  1. templates/index.html's JS `MARKETS` array — each entry's `status` field
     used to be a hardcoded "live"/"soon" literal.
  2. templates/search.html's D3 coverage-map JS — a single hardcoded
     `TRAVIS = "48453"` constant plus a `ROADMAP` object that listed Dallas
     as permanently "not yet live", completely independent of #1.

Diego's ruling on this brief was explicit: "two independent sources of the
same 'what's live' fact is exactly the class of drift this brief exists to
prevent." Both files were rewritten (Task 3) to derive their live/not-live
status from the SAME registry-driven Jinja context values -- app.py's
`_live_counties()` / `live_counties` / `live_slugs` -- rather than from a
second, hand-maintained judgment call. This scanner is the gate that keeps
it that way: it does not re-decide what's live (that's `_live_counties()`'s
job, and it queries real data), it only asserts that BOTH launch-surface
templates keep reading from that one source instead of quietly regressing
back to a hardcoded literal.

Three independent checks, each targeting a real historical or plausible
regression:

  (A) Presence: both templates must contain a Jinja-templated JS constant
      derived from `live_counties` or `live_slugs` (not just mention the
      word "live" -- an actual `{{ live_counties | ... }}` / `{{ live_slugs
      | ... }}` expression feeding a JS `const`). Guards against someone
      deleting the binding and reverting to a bare hardcoded array.

  (B) No hardcoded status literal: index.html's MARKETS array must not
      assign `status: "live"` or `status: "soon"` directly -- every status
      field must be a ternary that calls `.includes(...)` on the
      registry-derived array. A bare literal is exactly the pre-fix bug.

  (C) Cross-file FIPS-map consistency: index.html's MARKETS array and
      search.html's FIPS_BY_SLUG object are two independently-typed lookups
      of the same underlying fact (which FIPS code corresponds to which
      registered county slug). This is the check Diego named by name. Every
      slug present in EITHER file's map must appear in the OTHER file's map
      too (for slugs that are actually registered in app.py's COUNTY_SLUGS),
      with the identical FIPS value. A future onboarding that adds a slug's
      FIPS code to one file and forgets the other is exactly the drift this
      brief exists to prevent, and this check fails loudly if that happens.

Acceptance bar (per brief): zero findings today, and a fixture test proving
the scanner fires on each violation class -- see
test_verify_launch_surface_registry.py.
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = REPO_ROOT / "templates"
APP_PY = REPO_ROOT / "app.py"

INDEX_HTML = TEMPLATES_DIR / "index.html"
SEARCH_HTML = TEMPLATES_DIR / "search.html"

# Class (A): a Jinja expression that reads live_counties/live_slugs and
# feeds a JS const. Matches both index.html's `{{ live_slugs | tojson }}`
# shape and search.html's `{{ live_counties | map(attribute='slug') | list
# | tojson }}` shape -- deliberately loose (just requires the Jinja
# delimiters and one of the two registry names inside a `{{ }}` block that
# also mentions tojson), since the exact filter chain is legitimately
# different per file.
_REGISTRY_BINDING_RE = re.compile(
    r"\{\{[^}]*\b(?:live_counties|live_slugs)\b[^}]*\|\s*tojson[^}]*\}\}"
)

# Class (B): a `status:` field assigned a bare string literal, i.e. NOT
# followed (allowing for whitespace) by a ternary `? "live" : "soon"` whose
# condition calls `.includes(`. This regex flags the bad shape directly:
# `status:` then a quoted literal with nothing else on the statement (no
# `?`/`:` ternary, no `.includes(` anywhere before the closing brace/comma).
_MARKETS_ENTRY_RE = re.compile(
    r"\{\s*fips:\s*\"(?P<fips>\d+)\",\s*slug:\s*\"(?P<slug>[\w-]+)\",\s*status:\s*(?P<statusexpr>[^,]+),"
)

# Class (C): index.html's MARKETS array entries (fips + slug pairs).
_INDEX_MARKETS_BLOCK_RE = re.compile(r"const\s+MARKETS\s*=\s*\[(.*?)\];", re.DOTALL)

# Class (C): search.html's FIPS_BY_SLUG object (slug -> fips pairs).
_SEARCH_FIPS_BY_SLUG_BLOCK_RE = re.compile(r"const\s+FIPS_BY_SLUG\s*=\s*\{(.*?)\};", re.DOTALL)
_SLUG_FIPS_PAIR_RE = re.compile(r"\"(?P<slug>[\w-]+)\"\s*:\s*\"(?P<fips>\d+)\"")


@dataclass
class Finding:
    filepath: str
    kind: str
    severity: str  # "FAIL"
    detail: str


def _load_county_slugs():
    """Statically parses app.py's real COUNTY_SLUGS dict via `ast` (same
    no-Flask-import technique verify_county_scoping.py and
    verify_template_county_scoping.py already use on this file) so this
    scanner's registered-slug list can never drift from the app's own
    routing table. Falls back to an empty set (scanner then skips the
    registered-slug requirement, never crashes) if app.py's shape ever
    changes enough that this parse can't find COUNTY_SLUGS -- a loud
    print(), not a silent pass, so CI notices."""
    try:
        tree = ast.parse(APP_PY.read_text())
    except (OSError, SyntaxError) as exc:
        print(f"WARNING: could not parse {APP_PY} for COUNTY_SLUGS ({exc}) "
              f"-- Class (C) registered-slug check will be skipped this run.")
        return set()

    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "COUNTY_SLUGS" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            slugs = set()
            for key_node in node.value.keys:
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    slugs.add(key_node.value)
            return slugs

    print("WARNING: COUNTY_SLUGS assignment not found in app.py -- "
          "Class (C) registered-slug check will be skipped this run.")
    return set()


def _check_registry_binding_present(filepath: Path) -> list:
    text = filepath.read_text()
    rel = f"templates/{filepath.name}"
    if _REGISTRY_BINDING_RE.search(text):
        return []
    return [Finding(
        rel, "missing-registry-binding", "FAIL",
        "No JS constant in this file is bound to a "
        "`{{ live_counties | ... | tojson }}` / `{{ live_slugs | ... | "
        "tojson }}` Jinja expression. This is the launch surface's one "
        "sanctioned 'what's live' source (app.py's _live_counties()) -- "
        "without this binding, any live/soon logic in this file's JS has "
        "regressed to a second, hand-maintained answer to the same "
        "question, exactly the drift class this brief exists to prevent.",
    )]


def _check_no_hardcoded_market_status(filepath: Path) -> list:
    text = filepath.read_text()
    rel = f"templates/{filepath.name}"
    findings = []
    for m in _MARKETS_ENTRY_RE.finditer(text):
        expr = m.group("statusexpr").strip()
        slug = m.group("slug")
        if ".includes(" in expr and "?" in expr:
            continue  # the sanctioned ternary shape
        findings.append(Finding(
            rel, "hardcoded-market-status", "FAIL",
            f"MARKETS entry for slug {slug!r} sets status via {expr!r} -- "
            f"a hardcoded/non-registry status literal, not a "
            f"`LIVE_SLUGS.includes(...) ? \"live\" : \"soon\"` ternary. "
            f"This is the exact pre-Task-3 bug: a market's live/soon state "
            f"stops tracking app.py's _live_counties() the moment this "
            f"literal is introduced.",
        ))
    return findings


def _extract_index_fips_by_slug(filepath: Path = INDEX_HTML) -> dict:
    if not filepath.exists():
        return {}
    text = filepath.read_text()
    block_m = _INDEX_MARKETS_BLOCK_RE.search(text)
    if not block_m:
        return {}
    mapping = {}
    for m in _MARKETS_ENTRY_RE.finditer(block_m.group(1)):
        mapping[m.group("slug")] = m.group("fips")
    return mapping


def _extract_search_fips_by_slug(filepath: Path = SEARCH_HTML) -> dict:
    if not filepath.exists():
        return {}
    text = filepath.read_text()
    block_m = _SEARCH_FIPS_BY_SLUG_BLOCK_RE.search(text)
    if not block_m:
        return {}
    mapping = {}
    for m in _SLUG_FIPS_PAIR_RE.finditer(block_m.group(1)):
        mapping[m.group("slug")] = m.group("fips")
    return mapping


def _check_cross_file_fips_consistency(
    registered_slugs: set, index_path: Path = INDEX_HTML, search_path: Path = SEARCH_HTML
) -> list:
    findings = []
    index_map = _extract_index_fips_by_slug(index_path)
    search_map = _extract_search_fips_by_slug(search_path)

    if not index_map:
        findings.append(Finding(
            "templates/index.html", "fips-map-unparseable", "FAIL",
            "Could not find/parse a MARKETS array with fips/slug/status "
            "entries in index.html -- either the array was renamed/"
            "restructured (update this scanner's regex to match) or it was "
            "removed (a real regression: the coverage map has no data).",
        ))
    if not search_map:
        findings.append(Finding(
            "templates/search.html", "fips-map-unparseable", "FAIL",
            "Could not find/parse a FIPS_BY_SLUG object in search.html -- "
            "either it was renamed/restructured (update this scanner's "
            "regex to match) or the D3 map lost its slug-to-FIPS lookup "
            "(a real regression).",
        ))
    if not index_map or not search_map:
        return findings  # nothing more to cross-check without both maps

    # Only enforce cross-file agreement for slugs that are ACTUALLY
    # registered in app.py's COUNTY_SLUGS -- index.html legitimately lists
    # a few pure-roadmap cities (NYC, LA, Chicago) with no COUNTY_SLUGS
    # entry at all, and those have no reason to appear in search.html's map
    # (which only needs FIPS codes for counties that could plausibly go
    # live, i.e. registered ones).
    relevant_slugs = registered_slugs if registered_slugs else (set(index_map) | set(search_map))

    for slug in sorted(relevant_slugs):
        in_index = slug in index_map
        in_search = slug in search_map
        if in_index and not in_search:
            findings.append(Finding(
                "templates/index.html + templates/search.html", "fips-map-drift", "FAIL",
                f"Registered slug {slug!r} has a FIPS mapping in "
                f"index.html's MARKETS array ({index_map[slug]!r}) but is "
                f"missing from search.html's FIPS_BY_SLUG object. Once this "
                f"county goes live, index.html's coverage map will show it "
                f"correctly but search.html's D3 map will silently keep "
                f"treating it as a bare roadmap dot with no live styling -- "
                f"add {slug!r}: {index_map[slug]!r} to search.html's "
                f"FIPS_BY_SLUG.",
            ))
        elif in_search and not in_index:
            findings.append(Finding(
                "templates/index.html + templates/search.html", "fips-map-drift", "FAIL",
                f"Registered slug {slug!r} has a FIPS mapping in "
                f"search.html's FIPS_BY_SLUG ({search_map[slug]!r}) but is "
                f"missing from index.html's MARKETS array -- add a MARKETS "
                f"entry for {slug!r} with fips: {search_map[slug]!r}.",
            ))
        elif in_index and in_search and index_map[slug] != search_map[slug]:
            findings.append(Finding(
                "templates/index.html + templates/search.html", "fips-map-drift", "FAIL",
                f"Registered slug {slug!r} has DIFFERENT FIPS codes in the "
                f"two files: index.html says {index_map[slug]!r}, "
                f"search.html says {search_map[slug]!r}. A county's FIPS "
                f"code never changes -- this is a typo in one of the two "
                f"files, and it will make one of the two coverage maps "
                f"point at the wrong county.",
            ))

    return findings


def run_audit() -> list:
    findings = []
    registered_slugs = _load_county_slugs()

    for filepath in (INDEX_HTML, SEARCH_HTML):
        if not filepath.exists():
            findings.append(Finding(
                f"templates/{filepath.name}", "missing-file", "FAIL",
                f"{filepath.name} does not exist -- cannot audit a launch "
                f"surface that isn't there.",
            ))
            continue
        findings.extend(_check_registry_binding_present(filepath))

    if INDEX_HTML.exists():
        findings.extend(_check_no_hardcoded_market_status(INDEX_HTML))

    findings.extend(_check_cross_file_fips_consistency(registered_slugs))

    return findings


def print_report(findings: list) -> None:
    fails = [f for f in findings if f.severity == "FAIL"]
    print("verify_launch_surface_registry.py -- launch-surface 'what's live' drift gate")
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
            print(f"  FAIL {f.filepath} -- {f.detail}")
        print()


def main() -> int:
    findings = run_audit()
    print_report(findings)
    return 1 if any(f.severity == "FAIL" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
