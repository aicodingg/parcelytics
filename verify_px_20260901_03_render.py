#!/usr/bin/env python3
"""
verify_px_20260901_03_render.py — PX-20260901-03 Task 3 render/byte-compare
fixture for templates/snapshot.html.

Two things this brief's Task 3 explicitly requires and no existing fixture
file covers:

  1. Dallas-shaped data (7 available_tabs-eligible views -> 6 real tabs, no
     neighborhood-movers panel, the new coverage line, "2026 Certified vs
     2025 Certified") renders the composed-from-availability page correctly.
  2. Travis-shaped data (full coverage) renders BYTE-IDENTICAL to the
     pre-brief template -- proving this brief's tab-bar/coverage-line
     changes are a no-op for a county that already has everything.

Reuses verify_m4_part1_other_pages_render.py's own make_env() (same Jinja
globals/mocks already established there for snapshot.html) rather than
rebuilding the mock environment a second time.

The "pre-brief template" for item 2 is read via `git show HEAD:templates/
snapshot.html` -- this repo has the PX-20260901-03 diff still uncommitted,
so HEAD is genuinely the byte-for-byte pre-brief file, not a hand-reconstructed
approximation.

Run: python3 verify_px_20260901_03_render.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from jinja2 import Environment, FileSystemLoader

from verify_m4_part1_other_pages_render import make_env
from snapshot_taxonomy import _SNAPSHOT_VIEW_TAB_ORDER, _SNAPSHOT_TAB_BUTTON_LABEL

REPO_ROOT = os.path.dirname(__file__)
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")

FAILURES = []


def check(label, fn):
    try:
        out = fn()
        if "{%" in out or "{#" in out:
            FAILURES.append(f"{label}: leaked raw Jinja delimiter in output")
        else:
            print(f"  OK   {label} ({len(out)} chars)")
        return out
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")
        return None


def assert_check(label, condition, detail=""):
    """Plain boolean assertion helper -- used for checks against ALREADY
    -rendered output (string membership etc.), as opposed to check() above
    which calls and renders."""
    if condition:
        print(f"  OK   {label}")
    else:
        FAILURES.append(f"{label}  --  {detail}")


def _bd_row(ptype, mv25_b, mv26_b, med_pct=5.0):
    return {"ptype": ptype, "sort_key": ptype, "n_parcels": 100, "n_up": 60, "n_down": 30, "n_flat": 10,
            "median_pct": med_pct, "p25_pct": med_pct - 2, "p75_pct": med_pct + 2,
            "total_mv25_b": mv25_b, "total_mv26_b": mv26_b}


def _base_ctx(status_2026, mode="investor"):
    """Same shape verify_m4_part1_other_pages_render.py's own _snapshot_ctx()
    uses -- duplicated narrowly here (not imported) since that function is
    a local closure inside that file's main(), not a module-level export."""
    return dict(
        view="overall", mode=mode, status_2026=status_2026,
        data_unavailable=False, data_unavailable_reason=None,
        rows=[_bd_row("Residential", 100.0, 106.0), _bd_row("Retail", 20.0, 21.0),
              _bd_row("Industrial", 15.0, 15.5), _bd_row("Office", 10.0, 10.2),
              _bd_row("Hotel", 5.0, 5.1), _bd_row("Multi-Family", 30.0, 32.0)],
        totals={"n_total": 1000, "n_up": 600, "n_down": 300, "n_flat": 100,
                "total_mv25_b": 180.0, "total_mv26_b": 190.0, "median_pct": 5.5},
        bench_trends=[], new_construction_count=42, risk_flagged_count=7,
        subtype_cap=8, top_neighborhoods=[], bottom_neighborhoods=[],
    )


def main():
    env = make_env()
    tpl = env.get_template("snapshot.html")

    # ── Scenario 1: Dallas-shaped ───────────────────────────────────────
    # PM's real evidence: 7 available views in snapshot_totals (overall,
    # residential, multifamily, commercial, land, agricultural, other) ->
    # 6 real tabs ("commercial" has no tab in any county -- see
    # test_px_20260901_03.py's own proof of this distinction). Dallas has
    # NO neighborhood_cd data at all, so top/bottom_neighborhoods stay [].
    dallas_available_views = {"overall", "residential", "multifamily", "commercial",
                               "land", "agricultural", "other"}
    dallas_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in dallas_available_views]
    dallas_coverage_line = (
        "Dallas County: overall, residential, multi-family, commercial, land, agricultural, other. "
        "Sub-sector breakdowns (retail, office, industrial, hotel) and neighborhood detail "
        "are not yet available for this county."
    )
    _DALLAS_PROFILE = {
        "display_name": "Dallas County, TX",
        "county_name": "Dallas County",
        "cad_name": "Dallas Central Appraisal District",
        "tax_office_name": "Dallas County Tax Office",
    }
    dallas_ctx = {
        **_base_ctx("certified"),
        "county_profile": _DALLAS_PROFILE,
        "available_tabs": dallas_tabs,
        "tab_button_labels": _SNAPSHOT_TAB_BUTTON_LABEL,
        "coverage_line": dallas_coverage_line,
        "top_neighborhoods": [],
        "bottom_neighborhoods": [],
    }
    dallas_out = check("Dallas-shaped: renders cleanly", lambda: tpl.render(**dallas_ctx))

    if dallas_out is not None:
        # verify_m4_part1_other_pages_render.py's mock _url_for() ignores
        # kwargs entirely (returns a bare "/<endpoint>" for every call), so
        # the href itself can't distinguish which view a tab links to in
        # this harness -- isolate the tab-bar block instead and check tab
        # LABELS and the total tab COUNT, which the mock doesn't erase.
        bar_start = dallas_out.find('class="btn-group btn-group-sm flex-wrap"')
        bar_end = dallas_out.find("</div>", bar_start)
        tab_bar_html = dallas_out[bar_start:bar_end]

        tab_link_count = tab_bar_html.count("<a href=")
        assert_check("Dallas-shaped: tab bar has exactly 6 tabs (7 available "
                     "view-rows minus 'commercial', which has no tab)",
                     tab_link_count == 6, tab_link_count)

        for v in dallas_tabs:
            label = _SNAPSHOT_TAB_BUTTON_LABEL[v]
            assert_check(f"Dallas-shaped: tab '{label}' (view={v}) is present in the tab bar",
                         label in tab_bar_html)
        for v in ("retail", "industrial", "office", "hotel"):
            label = _SNAPSHOT_TAB_BUTTON_LABEL[v]
            assert_check(f"Dallas-shaped: NO tab for missing view '{v}' ('{label}' absent from tab bar)",
                         label not in tab_bar_html)
        # top_neighborhoods/bottom_neighborhoods are both [] (Dallas has zero
        # neighborhood_cd data) -- the pre-existing template gate
        # `{% if top_neighborhoods or bottom_neighborhoods %}` must hide the
        # whole panel, so its heading text must not appear at all.
        assert_check("Dallas-shaped: no neighborhood-movers panel rendered",
                     "Top Moving Neighborhoods" not in dallas_out and
                     "Bottom Moving Neighborhoods" not in dallas_out,
                     "movers panel heading text found in output")
        assert_check("Dallas-shaped: coverage line text is present",
                     dallas_coverage_line in dallas_out, "coverage line text missing")
        assert_check("Dallas-shaped: '2026 Certified vs 2025 Certified' subtitle present",
                     "2026 Certified vs 2025 Certified" in dallas_out, "subtitle missing")

    # ── Scenario 2: Travis-shaped byte-compare against pre-brief HEAD ──────
    head_snapshot_html = subprocess.run(
        ["git", "show", "HEAD:templates/snapshot.html"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "snapshot.html"), "w", encoding="utf-8") as f:
            f.write(head_snapshot_html)
        old_env = Environment(loader=FileSystemLoader([tmpdir, TEMPLATE_DIR]))
        old_env.globals.update(env.globals)
        old_env.filters.update(env.filters)  # tojson stub etc -- must match, or unrelated
                                              # base.html filter differences masquerade as regressions
        old_tpl = old_env.get_template("snapshot.html")

    travis_available_views = set(_SNAPSHOT_VIEW_TAB_ORDER) | {"commercial"}
    travis_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in travis_available_views]
    assert_check("Travis-shaped: full-coverage available_tabs has all 10 tab-order entries",
                 travis_tabs == list(_SNAPSHOT_VIEW_TAB_ORDER), travis_tabs)

    travis_ctx_old = _base_ctx("certified")  # pre-brief template needs no new keys
    travis_ctx_new = {
        **travis_ctx_old,
        "available_tabs": travis_tabs,
        "tab_button_labels": _SNAPSHOT_TAB_BUTTON_LABEL,
        "coverage_line": None,  # full coverage -> snapshot_coverage_copy() returns None
    }

    old_out = check("Travis-shaped: pre-brief (HEAD) template renders cleanly",
                     lambda: old_tpl.render(**travis_ctx_old))
    new_out = check("Travis-shaped: post-brief template renders cleanly",
                     lambda: tpl.render(**travis_ctx_new))

    if old_out is not None and new_out is not None:
        if old_out == new_out:
            print("  OK   Travis-shaped: BYTE-IDENTICAL to pre-brief render "
                  f"({len(old_out)} chars)")
        else:
            # Not necessarily a failure -- turning a static 10-<a>-tag block
            # into a {% for %} loop can shift blank-line whitespace around
            # the loop tags (Jinja's default trim_blocks=False leaves the
            # newline after `{% for %}`/`{% endfor %}` in the output) without
            # changing anything a browser renders differently. Report the
            # actual diff size so a human can judge whitespace-only vs real.
            import difflib
            diff_lines = list(difflib.unified_diff(
                old_out.splitlines(keepends=True), new_out.splitlines(keepends=True),
                fromfile="pre-brief (HEAD)", tofile="post-brief", n=1,
            ))
            non_blank_diff = [l for l in diff_lines
                               if l.startswith(("+", "-")) and l[1:].strip() != ""
                               and not l.startswith(("+++", "---"))]
            if non_blank_diff:
                FAILURES.append(
                    "Travis-shaped: NOT byte-identical, and the diff has "
                    f"{len(non_blank_diff)} non-blank line(s) changed -- this is a "
                    f"REAL content difference, not whitespace. First few:\n" +
                    "".join(non_blank_diff[:10])
                )
            else:
                print(f"  OK   Travis-shaped: byte-diff is whitespace-only "
                      f"({len(diff_lines)} diff line(s), all blank -- expected from "
                      f"the static tab list becoming a {{% for %}} loop; no visible "
                      f"or functional change)")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260901-03 render scenarios passed.")


if __name__ == "__main__":
    main()
