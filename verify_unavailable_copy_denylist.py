#!/usr/bin/env python3
"""
verify_unavailable_copy_denylist.py — PX-20260830-04 Task 1 recurrence guard.

Diego's standing ruling (restated in the PX-20260830-04 brief): user-facing
data_unavailable / error-state copy carries NO table names, file paths,
function names, or internal county codes. Task 1 fixed six real violations
of this rule (five branches inside app.py's _snapshot_summary_freshness(),
plus the /rates page's own reason, plus one more inside templates/
snapshot.html's own "no rows for this view" fallback) by routing all of them
through one function, unavailable_copy() (app.py), instead of each call site
hand-rolling its own f-string.

This scanner is the guard that keeps that fix from silently rotting: it
does NOT just re-check the six spots fixed today (those are simple to
eyeball) -- it structurally asserts that no CURRENT OR FUTURE data_unavailable
string, anywhere in app.py or templates/*.html, contains a denylisted
developer-facing token. Wired in as a recurrence guard, in the same family
as verify_template_county_scoping.py and verify_county_scoping.py.

Denylist (PM-specified, verbatim from the brief):
  "loaders/", ".py", "_breakdown", "_totals", "snapshot_", "group_stats",
  and any all-caps internal county code (e.g. "TRAVIS", "DALLAS", "HARRIS").
The county-code half of the denylist is NOT hand-typed here: it's parsed
statically out of app.py's own COUNTY_SLUGS dict (ast, no Flask import),
the same no-hand-maintained-list principle verify_template_county_scoping.py
already uses for its CAD-institution denylist and
verify_launch_surface_registry.py uses for its registered-slug list. A
future county's code is covered automatically the day it's added to
COUNTY_SLUGS.

Scope, and why each half is scanned the way it is:

  app.py (scanned via `ast`, NOT regex-with-comment-stripping): the six real
  call sites all read cleanly as specific AST shapes (an Assign or a Return
  tuple whose value/2nd-element is a literal string or f-string; a Call to
  unavailable_copy() whose page_label=/view_label= keyword args are literal
  strings; unavailable_copy()'s own function body, the canonical template
  text). AST is used here instead of raw-text regex specifically because
  this fix's OWN explanatory comments and docstrings in app.py quote the
  banned OLD strings verbatim as documentation of what was fixed (e.g.
  "run loaders/load_tax_rates.py for this county" appears in a code comment
  explaining the fix) -- a naive text scan would immediately flag its own
  fix's paper trail. Comments and docstrings are not part of the AST shapes
  this scanner walks, so this false-positive class cannot occur by
  construction, without needing a separate comment-stripping pass.

  templates/*.html (regex, with comment- AND Jinja-expression-stripping):
  the equivalent risk here is Jinja identifiers, not comments -- legitimate
  template code references real internal names like
  `{{ snapshot_breakdown.median_price }}` or `{% if data_unavailable %}`
  that are structurally similar to the denylist tokens but are Jinja code,
  not user-facing prose. `{{ ... }}` and `{% ... %}` spans (plus `{# #}`/
  `<!-- -->` comments) are blanked before scanning, so only literal HTML
  text nodes -- what a visitor actually reads -- are checked.
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_PY = REPO_ROOT / "app.py"
TEMPLATES_DIR = REPO_ROOT / "templates"

LITERAL_DENYLIST_TOKENS = (
    "loaders/", ".py", "_breakdown", "_totals", "snapshot_", "group_stats",
)


@dataclass
class Finding:
    filepath: str
    lineno: int
    kind: str      # "app-py-literal" | "template-literal"
    severity: str  # "FAIL" (no PASS/EXEMPT notion, same as verify_template_county_scoping.py)
    detail: str


def _load_county_codes():
    """Dynamically parses app.py's own COUNTY_SLUGS dict for the all-caps
    county-code half of the denylist -- see module docstring. Falls back to
    an empty set (loud print(), never a silent False) if app.py's shape
    ever changes enough that this parse can't find COUNTY_SLUGS."""
    codes = set()
    try:
        tree = ast.parse(APP_PY.read_text())
    except (OSError, SyntaxError) as exc:
        print(f"WARNING: could not parse {APP_PY} for COUNTY_SLUGS ({exc}) "
              f"-- all-caps county-code denylist will be empty this run.")
        return codes

    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "COUNTY_SLUGS" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            for val_node in node.value.values:
                if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
                    codes.add(val_node.value)
            break

    if not codes:
        print("WARNING: COUNTY_SLUGS assignment not found in app.py "
              "-- all-caps county-code denylist will be empty this run.")
    return codes


def _denylist_hits(text, county_code_re):
    """Returns every denylisted token found in `text` (a single literal
    string chunk, already isolated to just the user-facing prose)."""
    hits = [tok for tok in LITERAL_DENYLIST_TOKENS if tok in text]
    if county_code_re is not None:
        m = county_code_re.search(text)
        if m:
            hits.append(f"county-code:{m.group(0)}")
    return hits


def _joined_str_literal_chunks(node):
    """For an ast.JoinedStr (f-string), returns just the literal (non-
    interpolated) text chunks concatenated -- e.g. f"{county_name}'s data is
    being prepared" yields "'s data is being prepared", never the runtime
    value of {county_name}. That's the correct scope: this scanner checks
    what a DEVELOPER TYPED, not what a variable might evaluate to at
    runtime (a variable named county_name is already required by
    unavailable_copy()'s own signature to hold a real display name, never
    a raw code -- that contract is enforced by code review / the function's
    docstring, not by this string-content scanner)."""
    return "".join(
        v.value for v in node.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    )


def _string_or_fstring_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _joined_str_literal_chunks(node)
    return None


def scan_app_py(app_py: Path = APP_PY, county_codes=None) -> list:
    if county_codes is None:
        county_codes = _load_county_codes()
    county_code_re = (
        re.compile(r"\b(?:" + "|".join(re.escape(c) for c in sorted(county_codes)) + r")\b")
        if county_codes else None
    )

    source = app_py.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"WARNING: could not parse {app_py} ({exc}) -- app.py side of "
              f"this scanner will report zero findings this run.")
        return []

    rel = app_py.name
    findings = []

    def _report(lineno, chunk, why):
        hits = _denylist_hits(chunk, county_code_re)
        for hit in hits:
            findings.append(Finding(
                rel, lineno, "app-py-literal", "FAIL",
                f"{why} contains denylisted token {hit!r} in literal text {chunk!r} -- "
                f"user-facing data_unavailable copy must carry no table/file/function "
                f"names or internal county codes. Route this string through "
                f"unavailable_copy() instead of a hand-rolled literal.",
            ))

    for node in ast.walk(tree):
        # (a) `data_unavailable_reason = <literal or f-string>` -- the
        # recurrence guard for a future call site reverting to a raw string
        # instead of calling unavailable_copy(). Today this fires zero times
        # (all six real call sites call unavailable_copy() instead), which
        # is the expected, correct state -- not a scanner gap.
        if isinstance(node, ast.Assign):
            targets_named = any(
                isinstance(t, ast.Name) and t.id == "data_unavailable_reason"
                for t in node.targets
            )
            if targets_named:
                text = _string_or_fstring_text(node.value)
                if text is not None:
                    _report(node.lineno, text, "data_unavailable_reason assignment")

        # (b) `return False, <literal or f-string>` -- same recurrence-guard
        # intent as (a), for the shape _snapshot_summary_freshness() and its
        # siblings actually use (a (bool, reason) 2-tuple, the same
        # gate-result shape this codebase's G1-G6 ingest-gate functions also
        # use). Scoped precisely by the tuple's own shape -- first element a
        # literal `False`, second a literal string/f-string -- rather than a
        # substring guard on the enclosing function's source text: keying off
        # a specific phrase like "data_unavailable" is exactly the kind of
        # heuristic that silently stops firing the moment a real call site's
        # surrounding code changes shape, which is the opposite of what a
        # recurrence guard is for.
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
            first, second = node.value.elts
            if isinstance(first, ast.Constant) and first.value is False:
                text = _string_or_fstring_text(second)
                if text is not None:
                    _report(node.lineno, text, "return (False, <reason>) literal reason")

        # (c) unavailable_copy()'s own function body -- the canonical
        # template text. Should always be clean; if this ever fires, the
        # single source of truth itself has regressed.
        if isinstance(node, ast.FunctionDef) and node.name == "unavailable_copy":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return):
                    text = _string_or_fstring_text(inner.value)
                    if text is not None:
                        _report(inner.lineno, text, "unavailable_copy() own return literal")

        # (d) literal page_label=/view_label= keyword arguments at any
        # unavailable_copy(...) call site -- catches a future call site
        # that passes a developer-facing phrase as a caller-supplied label.
        if isinstance(node, ast.Call):
            func = node.func
            is_unavailable_copy_call = (
                (isinstance(func, ast.Name) and func.id == "unavailable_copy")
                or (isinstance(func, ast.Attribute) and func.attr == "unavailable_copy")
            )
            if is_unavailable_copy_call:
                for kw in node.keywords:
                    if kw.arg in ("page_label", "view_label"):
                        text = _string_or_fstring_text(kw.value)
                        if text is not None:
                            _report(node.lineno, text, f"unavailable_copy({kw.arg}=...) call-site literal")

    return findings


# ── templates/*.html side ───────────────────────────────────────────────
# Comment- and Jinja-expression-stripping so only literal HTML text (what a
# visitor actually reads) is scanned -- see module docstring for why.
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_JINJA_EXPR_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STMT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
# <script>...</script> blocks are JS CODE, not visitor-facing prose --
# blanked out entirely (not just their comments) before scanning. Confirmed
# necessary against the real tree: property.html's `entity_breakdown` JS
# field name and search.html's `//` comments citing app.py/verify_*.py by
# name (explaining the county-registry refactor) both live inside
# <script> blocks and are legitimate code/documentation, not something a
# visitor reads -- the same "don't scan code as if it were prose"
# principle already applied to {{ }}/{% %}, just for a differently-delimited
# code region this scanner also needs to know about.
_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)


def _blank(m: "re.Match") -> str:
    return "".join(ch if ch == "\n" else " " for ch in m.group(0))


def _strip_jinja_and_comments(text: str) -> str:
    text = _JINJA_COMMENT_RE.sub(_blank, text)
    text = _HTML_COMMENT_RE.sub(_blank, text)
    text = _SCRIPT_BLOCK_RE.sub(_blank, text)
    text = _JINJA_EXPR_RE.sub(_blank, text)
    text = _JINJA_STMT_RE.sub(_blank, text)
    return text


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_template_file(filepath: Path, county_codes=None) -> list:
    if county_codes is None:
        county_codes = _load_county_codes()
    county_code_re = (
        re.compile(r"\b(?:" + "|".join(re.escape(c) for c in sorted(county_codes)) + r")\b")
        if county_codes else None
    )

    raw = filepath.read_text()
    text = _strip_jinja_and_comments(raw)
    rel = f"templates/{filepath.name}"
    findings = []

    # Scan line-by-line so a single hit reports a precise, human-checkable
    # line number rather than an offset into the whole blanked document.
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits = _denylist_hits(line, county_code_re)
        for hit in hits:
            findings.append(Finding(
                rel, lineno, "template-literal", "FAIL",
                f"Literal template text contains denylisted token {hit!r} on this "
                f"line -- user-facing copy must carry no table/file/function names "
                f"or internal county codes. Route this text through "
                f"unavailable_copy() (registered as a Jinja global) instead.",
            ))
    return findings


def run_audit(app_py: Path = APP_PY, templates_dir: Path = TEMPLATES_DIR) -> list:
    county_codes = _load_county_codes()
    findings = scan_app_py(app_py, county_codes=county_codes)
    for filepath in sorted(templates_dir.glob("*.html")):
        findings.extend(scan_template_file(filepath, county_codes=county_codes))
    return findings


def print_report(findings: list) -> None:
    fails = [f for f in findings if f.severity == "FAIL"]
    print("verify_unavailable_copy_denylist.py -- data_unavailable copy recurrence guard")
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
