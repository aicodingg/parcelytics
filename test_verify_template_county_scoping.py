#!/usr/bin/env python3
"""
test_verify_template_county_scoping.py — fixture tests for
verify_template_county_scoping.py (PX-20260823-03 Task 4).

Proves the two required behaviors per the brief's "alarm-must-fire rule":
  Fixture 1: a planted Class-A violation (hardcoded, county-unaware app-route
             path in an href/action/fetch context) IS caught.
  Fixture 2: a planted Class-B violation (request.path compared against a
             route literal) IS caught.
Plus negative-control fixtures proving the scanner does NOT false-positive
on the sanctioned patterns this repo actually uses after Tasks 1-3:
  Fixture 3: {{ url_for(...) }} usage is clean.
  Fixture 4: COUNTY_BASE-prefixed JS fetch/href usage is clean.
  Fixture 5: request.endpoint comparisons are clean.
  Fixture 6: genuinely external URLs, static-asset url_for() calls, and
             fragment-only hrefs are clean.
  Fixture 7: an explanatory code comment that QUOTES an example old-style
             path (as this repo's own real Task 3 fix comment in base.html
             does) does NOT trip the scanner -- comments are stripped before
             scanning.
Fixture 8: a real-repo cross-check -- the actual templates/*.html tree
           (post-Tasks-1-3) scans clean, end to end.
"""

import sys
import tempfile
from pathlib import Path

import verify_template_county_scoping as vtcs


def _scan_text(text: str):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fixture.html"
        p.write_text(text)
        return vtcs.scan_file(p)


def check(label, cond, extra=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond and extra is not None:
        print(f"       {extra}")
    return cond


def main():
    all_ok = True

    # ── Fixture 1: planted Class-A violation (hardcoded href) is caught ──
    fixture1 = """
    <a href="/snapshot?view=overall">Overall</a>
    """
    findings1 = _scan_text(fixture1)
    all_ok &= check(
        "Fixture 1: hardcoded href=\"/snapshot?...\" produces a hardcoded-path FAIL",
        any(f.kind == "hardcoded-path" and f.severity == "FAIL" for f in findings1),
        findings1,
    )

    # ── Fixture 1b: planted Class-A violation in a JS fetch() is caught ──
    fixture1b = """
    <script>
      fetch('/api/benchmark/meta').then(r => r.json());
    </script>
    """
    findings1b = _scan_text(fixture1b)
    all_ok &= check(
        "Fixture 1b: hardcoded fetch('/api/...') produces a hardcoded-path FAIL",
        any(f.kind == "hardcoded-path" and f.severity == "FAIL" for f in findings1b),
        findings1b,
    )

    # ── Fixture 2: planted Class-B violation (request.path ==) is caught ──
    fixture2 = """
    <a class="nav-link {% if request.path == '/search' %}active{% endif %}" href="/search">Search</a>
    """
    findings2 = _scan_text(fixture2)
    all_ok &= check(
        "Fixture 2: request.path == '/search' produces a request-path-cmp FAIL",
        any(f.kind == "request-path-cmp" and f.severity == "FAIL" for f in findings2),
        findings2,
    )
    # Also confirm the SAME fixture's hardcoded href="/search" is independently caught --
    # both violation classes can co-occur on one line, exactly like base.html's real
    # pre-fix code did.
    all_ok &= check(
        "Fixture 2: the same line's hardcoded href=\"/search\" ALSO produces a hardcoded-path FAIL",
        any(f.kind == "hardcoded-path" and f.severity == "FAIL" for f in findings2),
        findings2,
    )

    # ── Fixture 3: {{ url_for(...) }} usage is clean (negative control) ──
    fixture3 = """
    <a href="{{ url_for('county_snapshot', view='overall') }}">Overall</a>
    <a class="nav-link {% if request.endpoint == 'search_page' %}active{% endif %}"
       href="{{ url_for('search_page') }}">Search</a>
    """
    findings3 = _scan_text(fixture3)
    all_ok &= check(
        "Fixture 3: url_for()-only template produces zero findings",
        len(findings3) == 0,
        findings3,
    )

    # ── Fixture 4: COUNTY_BASE-prefixed JS usage is clean (negative control) ──
    fixture4 = """
    <script>
      const COUNTY_BASE = {{ url_for('index') | tojson }}.replace(/\\/$/, '');
      fetch(`${COUNTY_BASE}/api/benchmark/meta`).then(r => r.json());
      fetch(COUNTY_BASE + "/api/billing/" + GEO_ID);
      const href = COUNTY_BASE + '/compare?ids=' + ids.join(',');
    </script>
    """
    findings4 = _scan_text(fixture4)
    all_ok &= check(
        "Fixture 4: COUNTY_BASE-prefixed fetch()/string-concat produces zero findings",
        len(findings4) == 0,
        findings4,
    )

    # ── Fixture 5: request.endpoint comparisons are clean (negative control) ──
    fixture5 = """
    <a class="nav-link {% if request.endpoint == 'index' %}active{% endif %}"
       href="{{ url_for('index') }}">Home</a>
    """
    findings5 = _scan_text(fixture5)
    all_ok &= check(
        "Fixture 5: request.endpoint comparison produces zero findings",
        len(findings5) == 0,
        findings5,
    )

    # ── Fixture 6: external URLs, url_for('static', ...), fragment-only hrefs ──
    fixture6 = """
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
    <a href="mailto:parcelytics@gmail.com">Contact</a>
    <a href="javascript:history.back()">Back</a>
    <a href="#methodology">Jump</a>
    """
    findings6 = _scan_text(fixture6)
    all_ok &= check(
        "Fixture 6: external/static/fragment/javascript: hrefs produce zero findings",
        len(findings6) == 0,
        findings6,
    )

    # ── Fixture 7: a comment QUOTING an example old-style path is not flagged ──
    # This mirrors this repo's own real base.html Task-3 fix comment, which
    # explains the bug by quoting "'/search'" as an example -- must not
    # self-trigger the scanner (the same false-positive class
    # verify_county_scoping.py hit with docstrings under PX-20260823-02).
    fixture7 = """
    {# Task 3 fix: nav active-state used to compare request.path against a
       bare, unprefixed route literal (e.g. '/search') -- broke the moment
       county prefixing landed. Now compares request.endpoint instead. #}
    <a class="nav-link {% if request.endpoint == 'search_page' %}active{% endif %}"
       href="{{ url_for('search_page') }}">Search</a>
    <!-- another comment mentioning /snapshot?view=overall as an example -->
    """
    findings7 = _scan_text(fixture7)
    all_ok &= check(
        "Fixture 7: a comment quoting an example old-style path produces zero findings",
        len(findings7) == 0,
        findings7,
    )

    # ── Fixture 8: real-repo cross-check -- the actual templates/ tree is clean ──
    real_findings = vtcs.run_audit()
    all_ok &= check(
        f"Fixture 8: real templates/*.html tree (post-Tasks-1-3) scans clean "
        f"({len(real_findings)} findings)",
        len(real_findings) == 0,
        real_findings,
    )

    print()
    print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
