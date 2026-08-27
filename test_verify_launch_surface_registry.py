#!/usr/bin/env python3
"""
test_verify_launch_surface_registry.py — fixture tests for
verify_launch_surface_registry.py (PX-20260827-03-rev1 Task 4a).

Proves the "alarm-must-fire rule" for each of the scanner's three
independent checks, using the exact real-world shapes those checks are
named after:

  Fixture 1: Class (A) -- a launch-surface template with NO Jinja binding
             to live_counties/live_slugs is flagged missing-registry-binding.
  Fixture 2: Class (A) negative control -- a template that DOES bind
             `{{ live_slugs | tojson }}` into a JS const is clean.
  Fixture 3: Class (B) -- a MARKETS array entry with a hardcoded
             `status: "live"` literal (the actual pre-Task-3 bug shape) is
             flagged hardcoded-market-status.
  Fixture 4: Class (B) negative control -- a MARKETS entry using the
             sanctioned `LIVE_SLUGS.includes(...) ? "live" : "soon"`
             ternary is clean.
  Fixture 5: Class (C) -- index.html and search.html disagree: a slug
             present in one file's FIPS map is missing from the other's.
             This is the literal "two independent sources of the same
             'what's live' fact" scenario Diego named.
  Fixture 6: Class (C) -- the two files list the SAME slug with two
             DIFFERENT FIPS codes (a typo in one file).
  Fixture 7: Class (C) negative control -- both files agree on every
             registered slug's FIPS code -- clean.
  Fixture 8: real-repo cross-check -- the actual templates/index.html and
             templates/search.html (post-Task-3) scan clean end to end.
"""

import sys
import tempfile
from pathlib import Path

import verify_launch_surface_registry as vlsr


def check(label, cond, extra=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond and extra is not None:
        print(f"       {extra}")
    return cond


def _write(tmpdir: Path, name: str, text: str) -> Path:
    p = tmpdir / name
    p.write_text(text)
    return p


_MARKETS_TEMPLATE = """
<script>
{registry_binding}
const MARKETS = [
  {{ fips: "48453", slug: "travis-tx",     {status_expr}, name: "Travis County, TX" }},
  {{ fips: "48113", slug: "dallas-tx",     status: LIVE_SLUGS.includes("dallas-tx") ? "live" : "soon", name: "Dallas County, TX" }},
];
</script>
"""

_SEARCH_TEMPLATE_TEMPLATE = """
<script>
const FIPS_BY_SLUG = {{
  {fips_by_slug_body}
}};
</script>
"""


def main():
    all_ok = True

    with tempfile.TemporaryDirectory() as d:
        tmpdir = Path(d)

        # ── Fixture 1: no registry binding at all -- flagged ──
        no_binding = _write(tmpdir, "no_binding.html", """
        <script>
          const LIVE_SLUGS = ["travis-tx"];  // hardcoded, no Jinja binding
        </script>
        """)
        f1 = vlsr._check_registry_binding_present(no_binding)
        all_ok &= check(
            "Fixture 1: template with no live_counties/live_slugs Jinja binding is flagged",
            any(f.kind == "missing-registry-binding" for f in f1),
            f1,
        )

        # ── Fixture 2: negative control -- real binding present ──
        with_binding = _write(tmpdir, "with_binding.html", """
        <script>
          const LIVE_SLUGS = {{ live_slugs | tojson }};
        </script>
        """)
        f2 = vlsr._check_registry_binding_present(with_binding)
        all_ok &= check(
            "Fixture 2: template WITH a live_slugs|tojson binding is clean",
            len(f2) == 0,
            f2,
        )

        # ── Fixture 2b: negative control -- search.html's own binding shape ──
        with_binding_search_shape = _write(tmpdir, "with_binding_search.html", """
        <script>
          const LIVE_SLUGS = {{ live_counties | map(attribute='slug') | list | tojson }};
        </script>
        """)
        f2b = vlsr._check_registry_binding_present(with_binding_search_shape)
        all_ok &= check(
            "Fixture 2b: search.html's live_counties|map|list|tojson binding shape is clean",
            len(f2b) == 0,
            f2b,
        )

        # ── Fixture 3: hardcoded status literal -- flagged ──
        hardcoded_status = _write(tmpdir, "hardcoded_status.html", _MARKETS_TEMPLATE.format(
            registry_binding="const LIVE_SLUGS = {{ live_slugs | tojson }};",
            status_expr='status: "live"',
        ))
        f3 = vlsr._check_no_hardcoded_market_status(hardcoded_status)
        all_ok &= check(
            "Fixture 3: MARKETS entry with hardcoded status:\"live\" is flagged",
            any(f.kind == "hardcoded-market-status" for f in f3),
            f3,
        )

        # ── Fixture 4: negative control -- sanctioned ternary ──
        ternary_status = _write(tmpdir, "ternary_status.html", _MARKETS_TEMPLATE.format(
            registry_binding="const LIVE_SLUGS = {{ live_slugs | tojson }};",
            status_expr='status: LIVE_SLUGS.includes("travis-tx") ? "live" : "soon"',
        ))
        f4 = vlsr._check_no_hardcoded_market_status(ternary_status)
        all_ok &= check(
            "Fixture 4: MARKETS entry using LIVE_SLUGS.includes(...) ternary is clean",
            len(f4) == 0,
            f4,
        )

        # ── Fixture 5: cross-file drift -- slug present in one map, missing from other ──
        index_missing_harris = _write(tmpdir, "index_missing_harris.html", _MARKETS_TEMPLATE.format(
            registry_binding="const LIVE_SLUGS = {{ live_slugs | tojson }};",
            status_expr='status: LIVE_SLUGS.includes("travis-tx") ? "live" : "soon"',
        ))
        search_has_harris = _write(tmpdir, "search_has_harris.html", _SEARCH_TEMPLATE_TEMPLATE.format(
            fips_by_slug_body='"travis-tx": "48453",\n  "dallas-tx": "48113",\n  "harris-tx": "48201",'
        ))
        f5 = vlsr._check_cross_file_fips_consistency(
            {"travis-tx", "dallas-tx", "harris-tx"},
            index_path=index_missing_harris,
            search_path=search_has_harris,
        )
        all_ok &= check(
            "Fixture 5: slug ('harris-tx') present in search.html's map but missing from index.html's is flagged",
            any(f.kind == "fips-map-drift" and "harris-tx" in f.detail for f in f5),
            f5,
        )

        # ── Fixture 6: cross-file drift -- same slug, different FIPS (typo) ──
        index_typo = _write(tmpdir, "index_typo.html", _MARKETS_TEMPLATE.format(
            registry_binding="const LIVE_SLUGS = {{ live_slugs | tojson }};",
            status_expr='status: LIVE_SLUGS.includes("travis-tx") ? "live" : "soon"',
        ))
        # index_typo's travis-tx fips is "48453" (from the shared template);
        # give search.html a deliberately wrong value for the same slug.
        search_typo = _write(tmpdir, "search_typo.html", _SEARCH_TEMPLATE_TEMPLATE.format(
            fips_by_slug_body='"travis-tx": "48999",\n  "dallas-tx": "48113",'
        ))
        f6 = vlsr._check_cross_file_fips_consistency(
            {"travis-tx", "dallas-tx"},
            index_path=index_typo,
            search_path=search_typo,
        )
        all_ok &= check(
            "Fixture 6: same slug with mismatched FIPS codes across the two files is flagged",
            any(f.kind == "fips-map-drift" and "DIFFERENT FIPS" in f.detail for f in f6),
            f6,
        )

        # ── Fixture 7: negative control -- both files agree on every registered slug ──
        index_ok = _write(tmpdir, "index_ok.html", _MARKETS_TEMPLATE.format(
            registry_binding="const LIVE_SLUGS = {{ live_slugs | tojson }};",
            status_expr='status: LIVE_SLUGS.includes("travis-tx") ? "live" : "soon"',
        ))
        search_ok = _write(tmpdir, "search_ok.html", _SEARCH_TEMPLATE_TEMPLATE.format(
            fips_by_slug_body='"travis-tx": "48453",\n  "dallas-tx": "48113",'
        ))
        f7 = vlsr._check_cross_file_fips_consistency(
            {"travis-tx", "dallas-tx"},
            index_path=index_ok,
            search_path=search_ok,
        )
        all_ok &= check(
            "Fixture 7: matching FIPS maps across both files is clean",
            len(f7) == 0,
            f7,
        )

    # ── Fixture 8: real-repo cross-check -- actual templates scan clean ──
    real_findings = vlsr.run_audit()
    all_ok &= check(
        "Fixture 8: real templates/index.html + templates/search.html scan clean end-to-end",
        len(real_findings) == 0,
        real_findings,
    )

    print()
    print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
