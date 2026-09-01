#!/usr/bin/env python3
"""
verify_px_20260901_03_render.py — PX-20260901-03 Task 3 render fixture for
templates/snapshot.html.

What this file still proves (item 2, the byte-identical Travis claim, was
retired -- see the PX-20260901-04 SUPERSESSION NOTE inline below):

  1. Dallas-shaped data (7 available_tabs-eligible views -> 6 real tabs, no
     neighborhood-movers panel, the new coverage line, "2026 Certified vs
     2025 Certified") renders the composed-from-availability page correctly.
  2. Travis-shaped data (full coverage) still gets all 10 tab-order entries
     in `available_tabs` -- the composition LOGIC this brief introduced is
     unaffected by PX-20260901-04's later, deliberate copy changes.

Reuses verify_m4_part1_other_pages_render.py's own make_env() (same Jinja
globals/mocks already established there for snapshot.html) rather than
rebuilding the mock environment a second time.

Run: python3 verify_px_20260901_03_render.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

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
        # PX-20260901-04 defaults -- Travis-shaped (full coverage) values;
        # dallas_ctx below overrides has_ajr_data/has_year_built_data/
        # bench_label/overall_tab_description with real Dallas-shaped ones.
        bench_label="Residential",
        overall_tab_description=(
            "All taxable real property — residential, multi-family, retail, "
            "industrial, office, hotel, land, agricultural, other"
        ),
        has_ajr_data=True, has_year_built_data=True,
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
        # PX-20260901-04 keys, added so this scenario keeps rendering after
        # that brief's template changes -- real Dallas-shaped values (see
        # verify_px_20260901_04_render.py for the dedicated fixture that
        # actually asserts on these; this file only needs them present so
        # this Task 3 scenario doesn't regress to Undefined-blank output).
        "bench_label": "Residential",
        "overall_tab_description": (
            "All taxable real property — " + ", ".join(
                v for v in ("residential", "multifamily", "land", "agricultural", "other")
            )
        ),
        "has_ajr_data": False,
        "has_year_built_data": False,
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

    # ── Scenario 2: Travis-shaped tab composition (byte-compare RETIRED) ────
    # PX-20260901-04 SUPERSESSION NOTE: this scenario used to byte-diff the
    # post-brief render against pre-brief HEAD and require the diff be
    # whitespace-only, proving PX-20260901-03's tab-bar/coverage-line change
    # was a true no-op for a fully-covered county. That guarantee no longer
    # holds -- PX-20260901-04's own brief states plainly that Task 1's
    # benchmark-row bug "has been live on Travis" too, and Task 4 explicitly
    # converts several Travis-visible strings (the Annual Trends "County
    # Median" header, the hardcoded "Certified 2021-2025" badge, the
    # "Red = increasing/green = decreasing" footer line) into county-aware
    # or generated copy -- Travis's rendered bytes are SUPPOSED to change now.
    # Keeping a byte-identical assertion here would either be a permanent,
    # known-false failure or would have to be quietly loosened until it
    # proved nothing. Retired in favor of the composition-only assertion
    # below (still true and still worth guarding) plus a dedicated new
    # fixture file, verify_px_20260901_04_render.py, that asserts the SPECIFIC
    # new Travis and Dallas content this brief introduced.
    travis_available_views = set(_SNAPSHOT_VIEW_TAB_ORDER) | {"commercial"}
    travis_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in travis_available_views]
    assert_check("Travis-shaped: full-coverage available_tabs has all 10 tab-order entries",
                 travis_tabs == list(_SNAPSHOT_VIEW_TAB_ORDER), travis_tabs)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260901-03 render scenarios passed.")


if __name__ == "__main__":
    main()
