#!/usr/bin/env python3
"""
test_verify_unavailable_copy_denylist.py — fixture tests for
verify_unavailable_copy_denylist.py (PX-20260830-04 Task 1).

Proves the "alarm-must-fire rule" both directions:
  Fixture 1: a planted raw `data_unavailable_reason = f"...loaders/..."`
             assignment in a synthetic app.py-shaped module IS caught.
  Fixture 2: a planted `return False, "...snapshot_totals..."` inside a
             data_unavailable-related function IS caught.
  Fixture 3: a planted all-caps county code ("DALLAS") in literal reason
             text IS caught, via the dynamically-derived COUNTY_SLUGS denylist.
  Fixture 4: a real unavailable_copy(...) call site (page_label="Market
             Snapshot", no denylisted tokens) is clean (negative control).
  Fixture 5: a planted literal ".py" token in a template's HTML text node
             IS caught.
  Fixture 6: a template using `{{ snapshot_breakdown.median_price }}` and
             `{% if data_unavailable %}` -- real Jinja code, not prose -- is
             clean (negative control: Jinja expressions/statements are
             blanked before scanning, so internal identifiers structurally
             matching denylist tokens don't false-positive).
  Fixture 7: an app.py comment/docstring that QUOTES an old banned string
             (exactly what this fix's own real comments do, documenting
             what was fixed) does NOT trip the scanner -- comments and
             docstrings are outside every AST shape this scanner walks.
  Fixture 8: a real-repo cross-check -- the actual app.py + templates/*.html
             tree (post-Task-1-fix) scans clean, end to end.
"""

import ast
import sys
import tempfile
import textwrap
from pathlib import Path

import verify_unavailable_copy_denylist as vucd

_FAKE_COUNTY_CODES = {"TRAVIS", "DALLAS", "HARRIS"}


def _scan_app_py_text(source: str):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "app.py"
        p.write_text(textwrap.dedent(source))
        return vucd.scan_app_py(p, county_codes=_FAKE_COUNTY_CODES)


def _scan_template_text(text: str, filename: str = "fixture.html"):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / filename
        p.write_text(text)
        return vucd.scan_template_file(p, county_codes=_FAKE_COUNTY_CODES)


def check(label, cond, extra=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond and extra is not None:
        print(f"       {extra}")
    return cond


def main():
    all_ok = True

    # ── Fixture 1: planted raw data_unavailable_reason assignment ──
    fixture1 = """
    def _snapshot_summary_freshness(county_code):
        if True:
            data_unavailable_reason = f"Run loaders/load_tax_rates.py for {county_code}"
            return False, data_unavailable_reason
        return True, None
    """
    findings1 = _scan_app_py_text(fixture1)
    all_ok &= check(
        "Fixture 1: raw data_unavailable_reason f-string with 'loaders/' is caught",
        any("loaders/" in f.detail for f in findings1),
        findings1,
    )

    # ── Fixture 2: planted return-tuple literal with a table-name token ──
    fixture2 = """
    def _snapshot_summary_freshness(county_code):
        if True:
            return False, "snapshot_totals has no rows for this view"
        return True, None
    """
    findings2 = _scan_app_py_text(fixture2)
    all_ok &= check(
        "Fixture 2: return-tuple literal with 'snapshot_' is caught",
        any("snapshot_" in f.detail for f in findings2),
        findings2,
    )

    # ── Fixture 3: planted all-caps county code in literal reason text ──
    fixture3 = """
    def _snapshot_summary_freshness(county_code):
        if True:
            data_unavailable_reason = "DALLAS has no data for this view yet"
            return False, data_unavailable_reason
        return True, None
    """
    findings3 = _scan_app_py_text(fixture3)
    all_ok &= check(
        "Fixture 3: literal all-caps county code 'DALLAS' is caught via the "
        "dynamically-derived COUNTY_SLUGS denylist",
        any("county-code:DALLAS" in f.detail for f in findings3),
        findings3,
    )

    # ── Fixture 4: real-shaped unavailable_copy() call site is clean ──
    fixture4 = """
    def _rates_response(county_code):
        profile = {"county_name": "Dallas County"}
        if True:
            data_unavailable_reason = unavailable_copy(
                "being_prepared", profile["county_name"],
                page_label="tax rate history", view_label="rate data",
            )
            return False, data_unavailable_reason
        return True, None
    """
    findings4 = _scan_app_py_text(fixture4)
    all_ok &= check(
        "Fixture 4: real unavailable_copy(...) call site (honest page_label/"
        "view_label, no denylisted tokens) produces zero findings",
        len(findings4) == 0,
        findings4,
    )

    # ── Fixture 5: planted '.py' token in template HTML text ──
    fixture5 = """
    <div class="alert alert-info">
      2026 data not yet loaded for this view. Run the 2026 loader script
      (load_tax_rates.py) to populate this page.
    </div>
    """
    findings5 = _scan_template_text(fixture5)
    all_ok &= check(
        "Fixture 5: literal '.py' token in template prose is caught",
        any(".py" in f.detail for f in findings5),
        findings5,
    )

    # ── Fixture 6: real Jinja code (not prose) is clean ──
    fixture6 = """
    {% if data_unavailable %}
      <div class="alert alert-info">{{ unavailable_copy("being_prepared", county_profile.county_name) }}</div>
    {% else %}
      <p>{{ snapshot_breakdown.median_price }}</p>
      {% for row in group_stats %}
        <span>{{ row.label }}</span>
      {% endfor %}
    {% endif %}
    """
    findings6 = _scan_template_text(fixture6)
    all_ok &= check(
        "Fixture 6: Jinja identifiers (snapshot_breakdown, group_stats, "
        "data_unavailable) inside {{ }}/{% %} produce zero findings -- "
        "blanked as code, not scanned as prose",
        len(findings6) == 0,
        findings6,
    )

    # ── Fixture 7: a comment quoting an old banned string is NOT flagged ──
    fixture7 = '''
    def unavailable_copy(kind, county_name, page_label=None, view_label=None):
        """Was a raw f-string: 'Run loaders/load_tax_rates.py for this
        county' -- county_tax_rate has no rows, snapshot_totals also empty.
        Fixed under PX-20260830-04 Task 1."""
        # old: f"{county_name} -- run loaders/load_tax_rates.py"
        if kind == "being_prepared":
            return f"{county_name}'s data is being prepared."
        return f"{county_name} does not publish this data."
    '''
    findings7 = _scan_app_py_text(fixture7)
    all_ok &= check(
        "Fixture 7: docstring/comment quoting the OLD banned string is NOT "
        "flagged (comments/docstrings are outside the AST shapes scanned) "
        "-- unavailable_copy()'s own real return literals are also clean",
        len(findings7) == 0,
        findings7,
    )

    # ── Fixture 8: real-repo cross-check, post-Task-1-fix ──
    real_app_findings = vucd.scan_app_py()
    real_app_fails = [f for f in real_app_findings if f.severity == "FAIL"]
    all_ok &= check(
        "Fixture 8a: real app.py scans clean end-to-end (post-Task-1-fix)",
        len(real_app_fails) == 0,
        real_app_fails,
    )

    real_template_findings = []
    for filepath in sorted(vucd.TEMPLATES_DIR.glob("*.html")):
        real_template_findings.extend(vucd.scan_template_file(filepath))
    real_template_fails = [f for f in real_template_findings if f.severity == "FAIL"]
    all_ok &= check(
        "Fixture 8b: real templates/*.html tree scans clean end-to-end "
        "(post-Task-1-fix)",
        len(real_template_fails) == 0,
        real_template_fails,
    )

    print()
    if all_ok:
        print("ALL UNAVAILABLE_COPY DENYLIST FIXTURE TESTS PASSED")
        return 0
    else:
        print("SOME UNAVAILABLE_COPY DENYLIST FIXTURE TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
