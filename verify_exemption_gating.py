#!/usr/bin/env python3
"""
verify_exemption_gating.py -- PX-20260901-05 Task 3 recurrence guard.

PX-20260901-05 ("Exemption-coverage gating") found and fixed every surface on
this site that presented exemption-derived content (the homestead-savings
pitch, the "Homes Without Homestead Exemption" lead filter, the Exemptions
row on the Value/Billing History table, the Cap Loss row on the compare
page, the Assessment Gap upside framing, and the Post-Acquisition
Estimator's cap-reset assumption) as if it had been checked and found
negative, for a county (Dallas) whose exemption_codes field was simply never
loaded -- an unmapped field reading as a confirmed "no exemption," which is
false. Task 1 built the fix's foundation (COUNTY_PROFILES.field_coverage +
county_has_field()); Task 2 wired every one of those surfaces behind it
(has_exemption_coverage in property.html, county_has_field(county_code,
"exemption_codes") directly in compare.html/search.html, an
exemption_coverage kwarg threaded through estimate_post_acquisition()).

This script is the guard that keeps that fix from silently rotting the next
time someone edits one of these templates: it does NOT just re-check today's
six template call sites by string match (a future refactor could easily move
one out from behind its gate while leaving the surrounding text -- and thus
any naive "does the string X appear near string Y" check -- unchanged). It
structurally walks each protected call site's ENCLOSING Jinja if/elif/else
nesting and asserts that every code path reaching it requires
has_exemption_coverage (or county_has_field(county_code, "exemption_codes"))
to be True first -- the same gating relationship Task 2 actually put in
place, not just a proxy for it.

Two things this scanner is explicitly NOT trying to be, matching this
repo's existing recurrence-guard scanners (verify_unavailable_copy_denylist.py,
verify_county_scoping.py, verify_template_county_scoping.py): it is not a
general Jinja control-flow analyzer (no macro-body inlining, no cross-file
tracking of a `{% set %}`'s definition site, no handling of `and`/`or`
sub-expressions beyond a single coverage-check substring), and it does not
discover new protected surfaces on its own -- REGISTRY below is a curated
list of the specific call sites Task 2's own audit found, the same
curated-list approach verify_launch_surface_registry.py and this repo's
EXEMPTIONS dict already use elsewhere. A future exemption-derived surface
still needs a human to add it to REGISTRY, same as a future writer needs a
human to register it in verify_tax_billing_rollup_canonical.py's closed
writer set. What this scanner DOES guarantee, mechanically, for every
site that IS registered: the gating relationship is real (see the
if/elif/else nesting model below), not just adjacent text.

── The nesting model ───────────────────────────────────────────────────────

For a linear scan of a template's `{% if %}` / `{% elif %}` / `{% else %}` /
`{% endif %}` tags up to a given position, this scanner tracks, for each
currently-open `if`-chain, three facts:
  - the coverage-relevance of the CURRENTLY ACTIVE branch's own condition
    (does it itself assert has_exemption_coverage / county_has_field(...,
    "exemption_codes") is True, is False, or say nothing about it), and
  - whether any EARLIER sibling branch in this same chain (the `if` or a
    prior `elif`) was a bare negation of the coverage check -- because
    Jinja only reaches a later branch when every earlier condition was
    false, a negation being false there means coverage is guaranteed True
    for every branch after it, REGARDLESS of that later branch's own
    condition. This is exactly the shape Task 2 used at all three of its
    real "elif shows the real exemption text" sites (property.html's
    Your Exemptions panel and Value/Billing History Exemptions row,
    compare.html's Cap Loss row): `{% if not <coverage> %}Not
    Available{% elif <real-data-condition> %}<real exemption text>{%
    endif %}` -- the elif's own condition never mentions coverage at all;
    it doesn't need to, because reaching it already proves coverage is True.
  - ambient gating inherited from any enclosing `if`-chain (a nested `{% if
    %}` inside an already-gated branch is gated regardless of its own
    condition -- property.html's Assessment Gap block nests `{% if
    has_exemption_coverage %}` inside two unrelated outer ifs; the outer
    ifs contribute no gating themselves, the inner one does).

A position is "gated" if the innermost currently-open frame's own condition
positively asserts coverage, OR any earlier sibling in that frame's chain
was a bare coverage negation, OR the frame was entered while already gated
by an ancestor chain.

── Fixture proof this scanner has teeth (module bottom, run_selftest()) ────

Per the brief's minimum bar ("a fixture that fails if the homestead-pitch
block loses its gate"): two small synthetic Jinja snippets, NOT the real
property.html (too large to usefully mutate here), reproduce the exact
`{% if has_exemption_coverage %}{{ homestead_savings_card(...) }}{% endif
%}` shape at property.html:837-838. One keeps the gate, one has it stripped
(the exact regression this whole brief exists to prevent) -- run_selftest()
asserts the scanner reports gated=True for the first and gated=False for
the second. If a future edit to this scanner's own nesting model ever broke
its ability to catch that regression, run_selftest() would start failing
even though every REGISTRY entry below still passes (they'd all still be
gated in the untouched real files) -- that's the whole point of testing the
detector against a case it MUST fail, not just cases it should pass.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = REPO_ROOT / "templates"
APP_PY = REPO_ROOT / "app.py"
TEXAS_PY = REPO_ROOT / "tax_logic" / "texas.py"

# A coverage assertion, optionally negated. Deliberately scoped to
# "exemption_codes" inside county_has_field(...) (not a bare
# county_has_field(...) call for some unrelated field) so this scanner can't
# be satisfied by a gate that's checking coverage of a different field
# entirely -- see module docstring's "not a general analyzer" caveat.
_COVERAGE_RE = re.compile(
    r"(not\s+)?\b(?:has_exemption_coverage\b|county_has_field\([^)]*exemption_codes[^)]*\))"
)

_TAG_RE = re.compile(r"\{%-?\s*(if|elif|else|endif)\b(.*?)-?%\}", re.DOTALL)

# Comments (Jinja and HTML) and <script> blocks routinely quote OLD Jinja
# tag shapes as documentation of a past bug or a past refactor (this file's
# own real property.html has several -- e.g. the has_exemption_coverage
# {% set %}'s own comment, ~line 400, literally says "moved here, BEFORE
# the {% if mode == 'homeowner' %} split below" as prose). Scanning those
# tag-shaped substrings as if they were real tags would corrupt the if-stack
# for everything after them. Blanked out (character-for-character, newlines
# preserved so offsets and line numbers stay valid) before every scan --
# same "don't scan documentation as if it were code" principle
# verify_unavailable_copy_denylist.py already applies for the identical
# reason (see that file's module docstring).
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)


def _blank(m: "re.Match") -> str:
    return "".join(ch if ch == "\n" else " " for ch in m.group(0))


def _strip_non_code(text: str) -> str:
    text = _JINJA_COMMENT_RE.sub(_blank, text)
    text = _HTML_COMMENT_RE.sub(_blank, text)
    text = _SCRIPT_BLOCK_RE.sub(_blank, text)
    return text


def _condition_coverage_class(cond_text: str) -> str:
    """'positive' if cond_text itself asserts coverage is True, 'negative'
    if it asserts coverage is False, 'none' if it says nothing about
    coverage at all (may still be gated via an ancestor or an earlier
    sibling negation -- see _gated_at)."""
    m = _COVERAGE_RE.search(cond_text)
    if not m:
        return "none"
    return "negative" if m.group(1) else "positive"


class _Frame:
    __slots__ = ("own_class", "any_negative_prior", "ambient_gated")

    def __init__(self, own_class: str, ambient_gated: bool):
        self.own_class = own_class
        self.any_negative_prior = False
        self.ambient_gated = ambient_gated


def _frame_gated(frame: "_Frame") -> bool:
    return frame.ambient_gated or frame.any_negative_prior or frame.own_class == "positive"


def gated_at(text: str, offset: int) -> bool:
    """True iff character position `offset` in `text` (raw Jinja template
    source) is reachable ONLY when has_exemption_coverage / a
    county_has_field(..., "exemption_codes") check is True, per the
    if/elif/else nesting model in this module's docstring. A linear,
    single-pass scan of every if/elif/else/endif tag before `offset` --
    not a real interpreter, same pragmatic-regex approach this repo's other
    template scanners already use (see module docstring). Comments and
    <script> blocks are blanked (length-preserving) before the scan so a
    tag shape quoted as documentation inside one can't be mistaken for a
    real tag -- see _strip_non_code()."""
    text = _strip_non_code(text)
    stack = []
    for m in _TAG_RE.finditer(text, 0, offset):
        kind = m.group(1)
        cond = m.group(2).strip()
        if kind == "if":
            ambient = _frame_gated(stack[-1]) if stack else False
            stack.append(_Frame(_condition_coverage_class(cond), ambient))
        elif kind == "elif":
            if not stack:
                continue
            top = stack[-1]
            if top.own_class == "negative":
                top.any_negative_prior = True
            top.own_class = _condition_coverage_class(cond)
        elif kind == "else":
            if not stack:
                continue
            top = stack[-1]
            if top.own_class == "negative":
                top.any_negative_prior = True
            top.own_class = "none"
        elif kind == "endif":
            if stack:
                stack.pop()
    return _frame_gated(stack[-1]) if stack else False


@dataclass
class Finding:
    filepath: str
    marker: str
    description: str
    severity: str  # "PASS" | "FAIL" | "WARN" (marker not found -- can't verify)
    detail: str


# ── Registry of protected exemption-derived surfaces (PX-20260901-05 Task 2's
# own audit) ─────────────────────────────────────────────────────────────
# Each entry: (relative filepath, a regex uniquely matching the protected
# call site, human description). The regex's MATCH START is what gated_at()
# is evaluated at -- for a macro call or real-value branch, that's the
# {{ ... }} or literal text itself, not the {% if %} tag that (should) gate
# it, so a future edit that widens or removes the surrounding {% if %}
# without touching this literal text is still caught.
REGISTRY = [
    (
        "templates/property.html",
        re.compile(r"\{\{\s*homestead_savings_card\(is_residential, bench_label, has_hs, mv25"),
        "Homestead-savings KPI card, 2025 (property.html ~838)",
    ),
    (
        "templates/property.html",
        re.compile(r"\{\{\s*homestead_savings_card\(is_residential, bench_label, has_hs, mv26"),
        "Homestead-savings KPI card, 2026 (property.html ~946)",
    ),
    (
        "templates/property.html",
        re.compile(r"\{%\s*elif\s+excodes_list\s*%\}"),
        'Your Exemptions panel, real-exemption-list branch (property.html ~1353)',
    ),
    (
        "templates/property.html",
        re.compile(r"No exemptions are currently applied\."),
        'Your Exemptions panel, "no exemptions" fallback branch (property.html ~1375)',
    ),
    (
        "templates/property.html",
        re.compile(r"suggesting potential\s*\n?\s*for an upward assessment in future years\."),
        "Assessment Gap upside framing (property.html ~2836)",
    ),
    (
        "templates/property.html",
        re.compile(
            r'\{%\s*elif\s*\(current and \(current\.exemption_codes or current\.billing_exemptions\)\).*?%\}',
            re.DOTALL,
        ),
        "Value/Billing History Exemptions row, real-value branch (property.html ~3874)",
    ),
    (
        "templates/compare.html",
        re.compile(r"\{%\s*elif\s+p\.current and p\.current\.cap_loss_estimate\s*%\}"),
        "Cap Loss (HS, Est.) row, real-value branch (compare.html ~96)",
    ),
    (
        "templates/search.html",
        re.compile(r'data-preset="no_homestead"'),
        'Quick Filters "Homes Without Homestead Exemption" preset button (search.html ~138)',
    ),
    (
        "templates/search.html",
        re.compile(r'id="fltHomestead"'),
        "Filter Parcels Homestead Exemption <select> (search.html ~361)",
    ),
]


def scan_registry() -> list:
    findings = []
    for rel_path, marker_re, description in REGISTRY:
        filepath = REPO_ROOT / rel_path
        text = filepath.read_text()
        matches = list(marker_re.finditer(text))
        if len(matches) != 1:
            findings.append(Finding(
                rel_path, marker_re.pattern, description, "WARN",
                f"expected exactly 1 match for this marker, found {len(matches)} -- "
                f"the protected call site's surrounding text has changed shape enough "
                f"that this scanner can no longer locate it. Update REGISTRY's marker "
                f"regex to match the new text (and re-confirm the gate manually) rather "
                f"than trust a stale marker.",
            ))
            continue
        m = matches[0]
        # Evaluated at the marker's END, not its start: several markers ARE
        # the {% elif ... %} tag itself (the real exemption text sits in the
        # *body* that follows it, once the tag has been fully parsed and its
        # own condition applied to the enclosing if-chain's state) -- see
        # module docstring's if-not-coverage/elif pattern. Evaluating at the
        # tag's own start would check gating BEFORE that transition, which
        # for a tag whose OWN condition doesn't mention coverage (true for
        # all three real elif markers in this registry) always reads as
        # "whatever the chain looked like one branch earlier," not the
        # branch this marker is actually inside.
        if gated_at(text, m.end()):
            findings.append(Finding(rel_path, marker_re.pattern, description, "PASS", "gated"))
        else:
            findings.append(Finding(
                rel_path, marker_re.pattern, description, "FAIL",
                f"reachable WITHOUT has_exemption_coverage / "
                f'county_has_field(county_code, "exemption_codes") being required True first '
                f"-- this exemption-derived surface has lost its coverage gate.",
            ))
    return findings


# ── app.py / tax_logic/texas.py: the two non-template call sites Task 2
# fixed (a Python kwarg, not a Jinja gate -- checked by direct substring
# presence near the known call site, same style as this repo's existing
# compute_metrics.py source-string checks in verify_property_html_render.py) ──
def scan_python_sites() -> list:
    findings = []

    app_src = APP_PY.read_text()
    # A fixed-size window after the call's opening paren, not a non-greedy
    # match to its "closing" )\n -- this call's own kwargs include an
    # explanatory comment that itself contains a parenthesized function
    # call (estimate_post_acquisition()) followed by a newline, which a
    # naive `.*?\)\n` non-greedy match would (and initially did, before this
    # fix) mistake for the call's real closing paren, truncating the
    # searched text before the actual exemption_coverage= kwarg.
    call_idx = app_src.find("result = _tx_estimate(")
    window = app_src[call_idx:call_idx + 900] if call_idx != -1 else ""
    m = call_idx != -1
    if m and 'exemption_coverage=county_has_field(g.county_code, "exemption_codes")' in window:
        findings.append(Finding("app.py", "_tx_estimate(...) call", "api_estimate_acq() threads exemption_coverage", "PASS", "gated"))
    else:
        findings.append(Finding(
            "app.py", "_tx_estimate(...) call",
            "api_estimate_acq() threads exemption_coverage",
            "FAIL" if m else "WARN",
            "_tx_estimate() call site no longer passes exemption_coverage=county_has_field(...) "
            "-- estimate_post_acquisition()'s cap-assumption line and circuit-breaker warning "
            "will silently revert to asserting a checked-and-confirmed negative for a "
            "no-coverage county." if m else
            "could not locate the _tx_estimate(...) call site in app.py at all -- "
            "update this scanner's regex if api_estimate_acq() was refactored.",
        ))

    texas_src = TEXAS_PY.read_text()
    m = re.search(r"cb_eligible_now = \(.*?\)\n", texas_src, re.DOTALL)
    if m and "exemption_coverage" in m.group(0):
        findings.append(Finding("tax_logic/texas.py", "cb_eligible_now assignment", "circuit-breaker warning respects exemption_coverage", "PASS", "gated"))
    else:
        findings.append(Finding(
            "tax_logic/texas.py", "cb_eligible_now assignment",
            "circuit-breaker warning respects exemption_coverage",
            "FAIL" if m else "WARN",
            "cb_eligible_now no longer conditions on exemption_coverage -- the 20% "
            "non-homestead circuit-breaker warning will fire for a no-coverage county "
            "even though seller_has_homestead couldn't actually be checked." if m else
            "could not locate the cb_eligible_now assignment in tax_logic/texas.py -- "
            "update this scanner's regex if that function was refactored.",
        ))

    if 'if not exemption_coverage else' in texas_src and "could not be verified for this" in texas_src:
        findings.append(Finding("tax_logic/texas.py", "assumptions Cap line", "Cap assumption line branches on exemption_coverage", "PASS", "gated"))
    else:
        findings.append(Finding(
            "tax_logic/texas.py", "assumptions Cap line",
            "Cap assumption line branches on exemption_coverage", "FAIL",
            'the assumptions list\'s Cap-related string no longer branches on '
            'exemption_coverage -- a no-coverage county\'s Post-Acquisition Estimator '
            'will revert to asserting "no active homestead cap" as a checked fact.',
        ))

    return findings


# ── Self-test: proves this scanner would actually catch the homestead-pitch
# block losing its gate (the brief's stated minimum bar) ───────────────────
_SELFTEST_GATED = """
{% if has_exemption_coverage %}
{{ homestead_savings_card(is_residential, bench_label, has_hs, mv25, tv25) }}
{% endif %}
"""

_SELFTEST_UNGATED = """
{{ homestead_savings_card(is_residential, bench_label, has_hs, mv25, tv25) }}
"""

_SELFTEST_ELIF_GATED = """
{% if not has_exemption_coverage %}
<span>Not Available</span>
{% elif excodes_list %}
<span>{{ excodes_list }}</span>
{% endif %}
"""

_SELFTEST_ELIF_REGRESSED = """
{% if some_unrelated_flag %}
<span>Not Available</span>
{% elif excodes_list %}
<span>{{ excodes_list }}</span>
{% endif %}
"""


def run_selftest() -> list:
    """Reproduces the exact gated/ungated shapes at property.html:837-838
    (a direct positive {% if %}) and property.html:1348-1353 (the
    if-not-coverage/elif pattern) in miniature, and asserts gated_at()
    tells them apart correctly in BOTH directions. The _REGRESSED variants
    are the actual minimum-bar fixture: each swaps the real coverage gate
    for an unrelated condition -- exactly what "the homestead-pitch block
    loses its gate" looks like in a future diff -- and this must come back
    ungated, or this scanner itself has a blind spot."""
    findings = []

    def _check(name, text, marker, expect_gated):
        offset = text.index(marker)
        got = gated_at(text, offset)
        ok = got == expect_gated
        findings.append(Finding(
            "verify_exemption_gating.py (selftest)", name,
            f"expected gated={expect_gated}", "PASS" if ok else "FAIL",
            f"gated_at() returned {got}" if ok else
            f"gated_at() returned {got}, expected {expect_gated} -- the scanner's own "
            f"nesting model has a blind spot for this shape.",
        ))

    _check("direct if-gate, intact", _SELFTEST_GATED, "homestead_savings_card(", True)
    _check("direct if-gate, REGRESSED (gate removed)", _SELFTEST_UNGATED, "homestead_savings_card(", False)
    _check("if-not-coverage/elif pattern, intact", _SELFTEST_ELIF_GATED, "{{ excodes_list }}", True)
    _check("if-not-coverage/elif pattern, REGRESSED (condition swapped)", _SELFTEST_ELIF_REGRESSED, "{{ excodes_list }}", False)

    return findings


def print_report(registry_findings, python_findings, selftest_findings) -> None:
    print("verify_exemption_gating.py -- PX-20260901-05 Task 3 recurrence guard")
    print()

    def _section(title, findings):
        print(f"── {title} ({len(findings)}) " + "─" * 30)
        for f in findings:
            print(f"  {f.severity:4} {f.filepath} -- {f.description}: {f.detail}")
        print()

    _section("Template registry (real files)", registry_findings)
    _section("Python call sites (real files)", python_findings)
    _section("Self-test (scanner's own detection ability, synthetic fixtures)", selftest_findings)

    all_findings = registry_findings + python_findings + selftest_findings
    fails = [f for f in all_findings if f.severity == "FAIL"]
    warns = [f for f in all_findings if f.severity == "WARN"]
    print(f"Totals: {len(all_findings)} checks, "
          f"{len(all_findings) - len(fails) - len(warns)} PASS, {len(warns)} WARN, {len(fails)} FAIL")


def main() -> int:
    registry_findings = scan_registry()
    python_findings = scan_python_sites()
    selftest_findings = run_selftest()
    print_report(registry_findings, python_findings, selftest_findings)
    all_findings = registry_findings + python_findings + selftest_findings
    return 1 if any(f.severity == "FAIL" for f in all_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
