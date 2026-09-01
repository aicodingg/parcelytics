#!/usr/bin/env python3
"""
test_px_20260901_04.py — PX-20260901-04 dedicated fixture coverage.

Covers the two behaviors the brief explicitly requires a fixture for:

Task 1 — Annual Trends panel row selection. Renders the REAL, unmodified
templates/snapshot.html (via verify_m4_part1_other_pages_render.py's own
make_env(), same mock environment already established for this template)
against a bench_trends fixture shaped exactly like the live bug: multiple
property_type_label rows (Residential, Agricultural, Commercial, ...) for
the SAME tax_year. Asserts the panel shows the Residential row's real
numbers -- not Agricultural's -- for both a Dallas-shaped and a
Travis-shaped bench_trends payload, using the PM's own quoted live numbers
so a regression back to "first row wins" would fail this test with the
exact same symptom PM reported (Travis: 3,770 Agricultural; Dallas:
2,335/2,254/2,377/2,346/2,379 Agricultural instead of 579,225 Residential).
Also proves a sector view (retail) resolves bench_label to "Commercial"
(the real county_benchmark row Retail/Industrial/Office/Hotel all share --
see snapshot_taxonomy.py's ptype_and_sort_case_for_view() bench_labels
mapping) rather than a nonexistent "Retail" row.

Task 2 — Investor takeaway carry-forward branching. Renders the same
template's Overall investor-takeaway block against three res-row shapes:
flat-majority (PM's exact Dallas numbers), a genuine non-zero-median case,
and a zero-median-but-not-flat-majority edge case -- proving the "never
say 'expect' with a zero median" rule holds in all three branches.

Run: python3 test_px_20260901_04.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from verify_m4_part1_other_pages_render import make_env
from snapshot_taxonomy import (
    _SNAPSHOT_VIEW_TAB_ORDER, _SNAPSHOT_TAB_BUTTON_LABEL,
    ptype_and_sort_case_for_view,
)

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  OK   {label}")
    else:
        FAILURES.append(f"{label}  --  {detail}")


def _bd_row(ptype, mv25_b=10.0, mv26_b=10.5, n_parcels=100, n_up=60, n_down=30, n_flat=10, med_pct=5.0):
    return {"ptype": ptype, "sort_key": ptype, "n_parcels": n_parcels, "n_up": n_up, "n_down": n_down,
            "n_flat": n_flat, "median_pct": med_pct, "p25_pct": med_pct - 2, "p75_pct": med_pct + 2,
            "total_mv25_b": mv25_b, "total_mv26_b": mv26_b}


def _bt_row(tax_year, property_type_label, parcel_count, median_market_value=300000.0):
    """A county_benchmark row shape, matching exactly what app.py's real
    Annual Trends query returns and templates/snapshot.html's bench_trends
    loop consumes (property_type_label, parcel_count, median_market_value,
    p25/p75_market_value, median_assessment_ratio, median_yoy_value_change_pct)."""
    return {
        "tax_year": tax_year, "property_type_label": property_type_label,
        "parcel_count": parcel_count, "median_market_value": median_market_value,
        "p25_market_value": median_market_value * 0.8, "p75_market_value": median_market_value * 1.2,
        "median_assessment_ratio": 0.9, "median_yoy_value_change_pct": 4.0,
    }


def _full_ctx(**overrides):
    ctx = dict(
        view="overall", mode="investor", status_2026="certified",
        data_unavailable=False, data_unavailable_reason=None,
        rows=[_bd_row("Residential"), _bd_row("Retail"), _bd_row("Industrial"),
              _bd_row("Office"), _bd_row("Hotel"), _bd_row("Multi-Family")],
        totals={"n_total": 1000, "n_up": 600, "n_down": 300, "n_flat": 100,
                "total_mv25_b": 180.0, "total_mv26_b": 190.0, "median_pct": 5.5},
        bench_trends=[], new_construction_count=42, risk_flagged_count=7,
        subtype_cap=8, top_neighborhoods=[], bottom_neighborhoods=[],
        available_tabs=list(_SNAPSHOT_VIEW_TAB_ORDER), tab_button_labels=_SNAPSHOT_TAB_BUTTON_LABEL,
        coverage_line=None, bench_label="Residential",
        overall_tab_description="All taxable real property — residential, multi-family, retail, "
                                 "industrial, office, hotel, land, agricultural, other",
        has_ajr_data=True, has_year_built_data=True,
    )
    ctx.update(overrides)
    return ctx


# ═══════════════════════════════════════════════════════════════════════════
# Task 1 — Annual Trends benchmark-row selection, per view, per county-shape
# ═══════════════════════════════════════════════════════════════════════════

def test_task1_ptype_and_sort_case_for_view_bench_labels():
    """The real, unmodified snapshot_taxonomy.py source -- proves the fix's
    root cause (5-label 'overall' bench_labels) is narrowed to exactly the
    one real row, and that the sector-view mapping used for bench_label is
    what it actually is (Retail/Industrial/Office/Hotel all point at the
    single 'Commercial' county_benchmark row -- there is no per-sub-sector
    row), not a plausible-sounding guess."""
    _, _, bench_labels_overall, _, _ = ptype_and_sort_case_for_view("overall")
    check("Task1/overall: bench_labels is exactly ['Residential'], not the old 5-label list",
          bench_labels_overall == ["Residential"], bench_labels_overall)

    _, _, bench_labels_residential, _, _ = ptype_and_sort_case_for_view("residential")
    check("Task1/residential view: bench_labels is ['Residential']",
          bench_labels_residential == ["Residential"], bench_labels_residential)

    for view in ("retail", "industrial", "office", "hotel"):
        _, _, bl, _, _ = ptype_and_sort_case_for_view(view)
        check(f"Task1/{view} view: bench_labels is ['Commercial'] (no per-sub-sector "
              f"county_benchmark row exists)", bl == ["Commercial"], bl)

    _, _, bench_labels_other, _, _ = ptype_and_sort_case_for_view("other")
    check("Task1/other view: bench_labels is [] (no county-wide 'Other' benchmark row -- "
          "panel must not render at all for this view)", bench_labels_other == [], bench_labels_other)


def _render_annual_trends(bench_trends, bench_label, view="overall"):
    env = make_env()
    tpl = env.get_template("snapshot.html")
    ctx = _full_ctx(view=view, bench_trends=bench_trends, bench_label=bench_label)
    return tpl.render(**ctx)


def test_task1_travis_shaped_overall_picks_residential_not_agricultural():
    """PM's exact live-bug reproduction for Travis: Annual Trends showed
    'Parcels in This Category 3,770' (Agricultural) on the Overall view.
    Feed a bench_trends fixture with BOTH Agricultural (3,770) and
    Residential rows for the same tax_year, render the real template, and
    assert the rendered parcel count is Residential's, with Agricultural's
    number nowhere in the Annual Trends card."""
    bench_trends = [
        _bt_row(2025, "Agricultural", 3770),
        _bt_row(2025, "Commercial", 41000),
        _bt_row(2025, "Land/Vacant", 12000),
        _bt_row(2025, "Multi-Family", 8900),
        _bt_row(2025, "Residential", 341250),
        _bt_row(2024, "Agricultural", 3690),
        _bt_row(2024, "Residential", 338900),
    ]
    out = _render_annual_trends(bench_trends, bench_label="Residential")
    card_start = out.find("Annual Trends")
    card_end = out.find("</table>", card_start)
    card_html = out[card_start:card_end]

    check("Task1/Travis-shaped: panel header reads 'Annual Trends — Residential', "
          "not the old 'County Median'", "Annual Trends" in out and "Residential" in out[card_start:card_start + 60],
          out[card_start:card_start + 60])
    check("Task1/Travis-shaped: Residential's real parcel count (341,250) is shown",
          "341,250" in card_html, card_html)
    check("Task1/Travis-shaped: Agricultural's parcel count (3,770) is NOT shown anywhere "
          "in the Annual Trends card -- this is the exact live bug symptom",
          "3,770" not in card_html, card_html)


def test_task1_dallas_shaped_overall_picks_residential_not_agricultural():
    """PM's exact live-bug reproduction for Dallas: Annual Trends showed
    2,335 / 2,254 / 2,377 / 2,346 / 2,379 (Agricultural, across 5 years)
    while the real Residential figure was 579,225. Same assertion shape as
    the Travis test above, with Dallas's own quoted numbers."""
    years = [2022, 2023, 2024, 2025, 2026]
    ag_counts = [2335, 2254, 2377, 2346, 2379]
    bench_trends = []
    for yr, ag in zip(years, ag_counts):
        bench_trends.append(_bt_row(yr, "Agricultural", ag))
        bench_trends.append(_bt_row(yr, "Residential", 579225))
        bench_trends.append(_bt_row(yr, "Commercial", 88000))

    out = _render_annual_trends(bench_trends, bench_label="Residential")
    card_start = out.find("Annual Trends")
    card_end = out.find("</table>", card_start)
    card_html = out[card_start:card_end]

    check("Task1/Dallas-shaped: Residential's real parcel count (579,225) is shown",
          "579,225" in card_html, card_html)
    for ag in ag_counts:
        formatted = f"{ag:,}"
        check(f"Task1/Dallas-shaped: Agricultural's parcel count ({formatted}) is NOT shown "
              f"anywhere in the Annual Trends card", formatted not in card_html, card_html)


def test_task1_sector_view_picks_commercial_row_for_retail():
    """A sector view (retail) must show the shared 'Commercial'
    county_benchmark row -- not a nonexistent 'Retail' row, and not
    whichever row happens to sort first among Commercial/Land/Vacant/etc."""
    bench_trends = [
        _bt_row(2025, "Commercial", 41000),
        _bt_row(2025, "Agricultural", 3770),
        _bt_row(2025, "Land/Vacant", 12000),
        _bt_row(2024, "Commercial", 40200),
        _bt_row(2024, "Agricultural", 3690),
    ]
    out = _render_annual_trends(bench_trends, bench_label="Commercial", view="retail")
    card_start = out.find("Annual Trends — ")
    card_end = out.find("</table>", card_start)
    card_html = out[card_start:card_end]

    check("Task1/retail view: panel header reads 'Annual Trends — Commercial'",
          "Commercial" in out[card_start:card_start + 40], out[card_start:card_start + 40])
    check("Task1/retail view: Commercial's real parcel count (41,000) is shown",
          "41,000" in card_html, card_html)
    check("Task1/retail view: Agricultural's parcel count (3,770) is NOT shown",
          "3,770" not in card_html, card_html)


def test_task1_no_bench_labels_view_renders_no_panel():
    """'other' view has bench_labels == [] -> bench_label is None ->
    bench_trends is empty (app.py's real query only runs when bench_labels
    is non-empty) -- the Annual Trends card must not render at all, per the
    brief's own rule ('sector views ... no panel where none does')."""
    out = _render_annual_trends(bench_trends=[], bench_label=None, view="other")
    check("Task1/other view: no Annual Trends panel rendered at all",
          "Annual Trends" not in out, "panel heading unexpectedly present")


# ═══════════════════════════════════════════════════════════════════════════
# Task 2 — Investor takeaway carry-forward branching
# ═══════════════════════════════════════════════════════════════════════════

def _render_takeaway(res_row):
    env = make_env()
    tpl = env.get_template("snapshot.html")
    rows = [res_row, _bd_row("Retail"), _bd_row("Industrial"), _bd_row("Office"),
            _bd_row("Hotel"), _bd_row("Multi-Family")]
    ctx = _full_ctx(rows=rows)
    return tpl.render(**ctx)


def test_task2_flat_majority_case():
    """PM's exact Dallas numbers: 315,464 of 545,925 residential parcels
    carried forward unchanged; among the rest, 109,674 rose and 120,787
    fell. Must lead with the flat-majority fact and never say 'expect'."""
    res = _bd_row("Residential", n_flat=315464, n_up=109674, n_down=120787,
                   n_parcels=545925, med_pct=0.0)
    out = _render_takeaway(res)
    expected = ("Most residential parcels carried forward unchanged\n        "
                "(315,464 of 545,925); among those that\n        changed, "
                "109,674 rose and 120,787 fell.")
    # Whitespace in the rendered HTML mirrors the template's own indentation --
    # compare on collapsed whitespace to avoid coupling this test to incidental
    # template formatting.
    norm_out = " ".join(out.split())
    check("Task2/flat-majority: leads with the flat-majority sentence using "
          "the exact real numbers",
          "Most residential parcels carried forward unchanged (315,464 of 545,925)" in norm_out
          and "109,674 rose and 120,787 fell" in norm_out,
          norm_out[:400])
    check("Task2/flat-majority: never uses the word 'expect' anywhere in the takeaway "
          "(median is 0.00%)", "expect" not in norm_out.lower(), norm_out[:400])


def test_task2_non_flat_increase_case():
    """A genuine non-zero-median, non-flat-majority case: up-share and
    down-share both individually exceed the flat share -- the original
    'should expect increases' framing is correct here and must still work."""
    res = _bd_row("Residential", n_flat=100, n_up=600, n_down=300,
                   n_parcels=1000, med_pct=5.5)
    out = _render_takeaway(res)
    norm_out = " ".join(out.split())
    check("Task2/non-flat-increase: renders 'should expect increases' with the real median",
          "Residential owners should expect" in norm_out and "increases" in norm_out
          and "median +5.5%" in norm_out and "300 down / 600 up" in norm_out,
          norm_out[:400])


def test_task2_non_flat_relief_case():
    """Same non-flat-majority shape but a negative median -- must say
    'relief', not 'increases'."""
    res = _bd_row("Residential", n_flat=50, n_up=200, n_down=700,
                   n_parcels=950, med_pct=-3.2)
    out = _render_takeaway(res)
    norm_out = " ".join(out.split())
    check("Task2/non-flat-relief: renders 'should expect relief' with the real median",
          "Residential owners should expect" in norm_out and "relief" in norm_out
          and "median -3.2%" in norm_out and "700 down / 200 up" in norm_out,
          norm_out[:400])


def test_task2_zero_median_not_flat_majority_edge_case():
    """PM's rule taken literally: 'never the word expect with a zero
    median' -- even in the edge case where median_pct is 0 WITHOUT the
    flat bucket being the single largest group (n_flat is NOT greater than
    both n_up and n_down here: 100 is not > 110). Must fall through to the
    dedicated zero-median branch, not the 'expect' branch."""
    res = _bd_row("Residential", n_flat=100, n_up=110, n_down=90,
                   n_parcels=300, med_pct=0.0)
    out = _render_takeaway(res)
    norm_out = " ".join(out.split())
    check("Task2/zero-median-edge-case: never uses the word 'expect'",
          "expect" not in norm_out.lower(), norm_out[:400])
    check("Task2/zero-median-edge-case: uses the dedicated flat-overall sentence "
          "with the real down/up numbers",
          "Residential values were flat overall" in norm_out
          and "median 0.00%" in norm_out and "90 down / 110 up" in norm_out,
          norm_out[:400])


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PX-20260901-04 TASK 1+2 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
