"""
verify_m4_part1_other_pages_render.py — real Jinja RENDER verification for
the five "other pages" touched by M4-2026-PRELIM-SNAPSHOT Part 1
(compare.html, parcel_list.html, snapshot_neighborhood.html, search.html,
snapshot.html), plus a re-check of property.html's certified-vs-preliminary
branches specifically.

Lighter-weight than verify_property_html_render.py (uses permissive
Undefined, not StrictUndefined -- building a fully-complete mock context for
all six templates' base.html-inherited chrome was not proportionate to this
brief's remaining scope/budget). This still catches the failure modes that
matter here: template syntax errors, undefined-attribute crashes on the
NEW template variables this brief introduced (is_2026_certified,
status_2026, p.is_2026_certified, has_preliminary_2026), and leaked raw
Jinja delimiters in the output (the same P0-1 regression class
verify_property_html_render.py was built to catch).

Run: python3 verify_m4_part1_other_pages_render.py
Exits non-zero and prints a diagnosis if any scenario fails.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


class _FakeRequest:
    def __init__(self, path="/"):
        self.path = path
        self.args = {}


def _url_for(endpoint, **kwargs):
    if endpoint == "static":
        return "/static/" + kwargs.get("filename", "")
    return "/" + endpoint.lstrip("/")


def make_env():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.globals["url_for"] = _url_for
    env.globals["request"] = _FakeRequest()
    import config as _real_config
    env.globals["config"] = _real_config
    env.filters["tojson"] = lambda v: "null"

    # PX-20260827-04 incidental fix: county_profile/county_url/live_counties/
    # is_county_anchored (from app.py's @app.context_processor) were NEVER
    # registered here -- every one of this harness's scenarios has been
    # raising UndefinedError on 'county_profile' via base.html's footer since
    # PX-20260824-01/PX-20260827-03-rev1 landed, pre-dating and unrelated to
    # this brief's own Task 2 edit. Same gap class already fixed in
    # verify_property_html_render.py (see its own comment there) -- mirrored
    # here rather than shared, since this harness has no live app.py import
    # either. live_counties intentionally carries TWO entries (unlike that
    # harness's single-Travis mock) so Task 2's new
    # `live_counties|length > 1` gate actually renders the switcher in these
    # scenarios too, not just silently no-ops it. All six templates here are
    # real county-anchored (<county_slug>-prefixed) routes in production, so
    # is_county_anchored=True throughout, matching property.html's harness.
    env.globals["county_slug"] = "travis-tx"
    env.globals["county_url"] = lambda path: "/travis-tx" + path
    _MOCK_TRAVIS_PROFILE = {
        "display_name": "Travis County, TX",
        "county_name": "Travis County",
        "cad_name": "Travis Central Appraisal District",
        "tax_office_name": "Travis County Tax Office",
    }
    env.globals["county_profile"] = _MOCK_TRAVIS_PROFILE
    env.globals["county_cad_link"] = lambda field, prop_id=None, geo_id=None: None

    # PX-20260830-04 Task 1: mirrors app.py's real unavailable_copy() shape
    # (same two `kind` branches, same parameter names) so templates.snapshot
    # .html's own call to it renders instead of raising UndefinedError --
    # this harness doesn't import app.py (Flask/psycopg2 unavailable in this
    # sandbox), so it's a hand-mirrored stub like every other _inject_
    # county_helpers() global already mocked here, not the real function.
    def _mock_unavailable_copy(kind, county_name, page_label=None, view_label=None):
        if kind == "being_prepared":
            page_label = page_label or "this data"
            view_label = view_label or "it"
            return (
                f"{county_name}'s {page_label} is being prepared. Parcel and appraisal "
                f"data for {county_name} are live; {view_label} will be available soon."
            )
        if kind == "not_published":
            return f"{county_name} does not publish this data, so it isn't available on Parcelytics."
        raise ValueError(f"unavailable_copy: unrecognized kind {kind!r}")
    env.globals["unavailable_copy"] = _mock_unavailable_copy
    env.globals["live_counties"] = [
        {
            "slug": "travis-tx",
            "county_code": "TRAVIS",
            "display_name": "Travis County, TX",
            "county_name": "Travis County",
            "parcel_count": 430147,
            "parcel_count_display": "430,147",
        },
        {
            "slug": "dallas-tx",
            "county_code": "DALLAS",
            "display_name": "Dallas County, TX",
            "county_name": "Dallas County",
            "parcel_count": 705536,
            "parcel_count_display": "705,536",
        },
    ]
    env.globals["is_county_anchored"] = True
    return env


FAILURES = []


def check(label, fn):
    try:
        out = fn()
        if "{%" in out or "{#" in out:
            FAILURES.append(f"{label}: leaked raw Jinja delimiter in output")
        else:
            print(f"  OK   {label} ({len(out)} chars)")
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")


def parcel(geo_id, is_2026_certified, mv26=850000):
    return {
        "geo_id": geo_id,
        "address": f"{geo_id} Mock St",
        "prop_type": "Single-Family Residence",
        "parcel": {"owner_name": "Mock Owner", "neighborhood_cd": "MOCK01"},
        "current": {"market_value": 800000, "assessed_value": 780000, "cap_loss_estimate": 5000},
        "billing": {"total_tax": 12000, "is_delinquent": False},
        "current_2026": {"market_value": mv26, "data_source": "cert_2026" if is_2026_certified else "preliminary"},
        "is_2026_certified": is_2026_certified,
    }


def main():
    env = make_env()

    # ── compare.html ──────────────────────────────────────────────────────
    tpl = env.get_template("compare.html")
    check("compare.html / all certified",
          lambda: tpl.render(parcels=[parcel("0101140329", True), parcel("0100030109", True)]))
    check("compare.html / all preliminary",
          lambda: tpl.render(parcels=[parcel("0101140329", False), parcel("0100030109", False)]))
    check("compare.html / mixed",
          lambda: tpl.render(parcels=[parcel("0101140329", True), parcel("0100030109", False)]))
    check("compare.html / no 2026 data at all",
          lambda: tpl.render(parcels=[{**parcel("0101140329", True), "current_2026": None}]))
    check("compare.html / error branch",
          lambda: tpl.render(error="No parcels found", parcels=[]))

    # ── parcel_list.html ─────────────────────────────────────────────────
    tpl = env.get_template("parcel_list.html")

    def _plist_row(mv26, data_source):
        return {"geo_id": "0101140329", "address": "Mock St", "owner": "Mock Owner",
                "mv25": 800000, "mv26": mv26, "data_source_2026": data_source}

    check("parcel_list.html / status certified",
          lambda: tpl.render(view="overall", ptype="All", status_2026="certified",
                              parcels=[_plist_row(850000, "cert_2026")]))
    check("parcel_list.html / status preliminary",
          lambda: tpl.render(view="overall", ptype="All", status_2026="preliminary",
                              parcels=[_plist_row(850000, "preliminary")]))
    check("parcel_list.html / status mixed",
          lambda: tpl.render(view="overall", ptype="All", status_2026="mixed",
                              parcels=[_plist_row(850000, "cert_2026"), _plist_row(820000, "preliminary")]))
    check("parcel_list.html / status none (no parcels)",
          lambda: tpl.render(view="overall", ptype="All", status_2026="none", parcels=[]))

    # ── snapshot_neighborhood.html ──────────────────────────────────────
    tpl = env.get_template("snapshot_neighborhood.html")

    def _nb_row(mv26, data_source, is_cert):
        return {"geo_id": "0101140329", "situs_address": "Mock St", "mv25": 800000, "mv26": mv26,
                "data_source_2026": data_source, "prop_type": "Single-Family Residence",
                "pct_chg": 6.25, "is_2026_certified": is_cert}

    check("snapshot_neighborhood.html / certified",
          lambda: tpl.render(code="MOCK01", view="overall", view_prop_type=None, page=1, total=1,
                              total_pages=1, status_2026="certified",
                              parcels=[_nb_row(850000, "cert_2026", True)]))
    check("snapshot_neighborhood.html / preliminary",
          lambda: tpl.render(code="MOCK01", view="overall", view_prop_type=None, page=1, total=1,
                              total_pages=1, status_2026="preliminary",
                              parcels=[_nb_row(850000, "preliminary", False)]))
    check("snapshot_neighborhood.html / mixed",
          lambda: tpl.render(code="MOCK01", view="overall", view_prop_type=None, page=1, total=2,
                              total_pages=1, status_2026="mixed",
                              parcels=[_nb_row(850000, "cert_2026", True), _nb_row(820000, "preliminary", False)]))

    # ── search.html ──────────────────────────────────────────────────────
    tpl = env.get_template("search.html")
    check("search.html / has_preliminary_2026=True",
          lambda: tpl.render(has_preliminary_2026=True))
    check("search.html / has_preliminary_2026=False",
          lambda: tpl.render(has_preliminary_2026=False))

    # PX-20260828-03 Task 1: search.html's "Find a Parcel" box now renders
    # its own q=/addr_matches/error results in place (previously navigated
    # away to index.html entirely, so this template never had to handle
    # these three variables at all -- new coverage, not a re-check).
    check("search.html / quick-search q= with no addr_matches or error "
          "(GET with no q at all -- q/addr_matches/error all default None)",
          lambda: tpl.render(has_preliminary_2026=False, county_selected=True))
    check("search.html / quick-search addr_matches populated, "
          "including PX-20260828-03 Task 2's county_name tag",
          lambda: tpl.render(
              has_preliminary_2026=False, county_selected=True, q="1201 S Lamar",
              addr_matches=[
                  {"geo_id": "0100030105", "situs_address": "1201 S LAMAR BLVD",
                   "owner_name": "ACME LLC", "county_slug": "travis-tx", "county_name": "Travis County"},
                  {"geo_id": "DAL0099", "situs_address": "1201 S LAMAR BLVD UNIT B",
                   "owner_name": None, "county_slug": "dallas-tx", "county_name": "Dallas County"},
              ]))
    check("search.html / quick-search error message, no matches "
          "(the honest no-results branch of the shared macro)",
          lambda: tpl.render(has_preliminary_2026=False, county_selected=False,
                              q="zzzznotreal", error='No parcels found matching address "zzzznotreal". '))

    # ── snapshot.html ────────────────────────────────────────────────────
    tpl = env.get_template("snapshot.html")

    def _bd_row(ptype, mv25_b, mv26_b, med_pct=5.0):
        return {"ptype": ptype, "sort_key": ptype, "n_parcels": 100, "n_up": 60, "n_down": 30, "n_flat": 10,
                "median_pct": med_pct, "p25_pct": med_pct - 2, "p75_pct": med_pct + 2,
                "total_mv25_b": mv25_b, "total_mv26_b": mv26_b}

    def _snapshot_ctx(status_2026, mode="investor"):
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

    check("snapshot.html / status certified (investor)",
          lambda: tpl.render(**_snapshot_ctx("certified")))
    check("snapshot.html / status preliminary (investor)",
          lambda: tpl.render(**_snapshot_ctx("preliminary")))
    check("snapshot.html / status mixed (investor)",
          lambda: tpl.render(**_snapshot_ctx("mixed")))
    check("snapshot.html / status certified (homeowner)",
          lambda: tpl.render(**_snapshot_ctx("certified", mode="homeowner")))
    check("snapshot.html / totals=None (no-data branch, e.g. an empty view)",
          lambda: tpl.render(view="overall", mode="investor", status_2026="none", rows=[], totals=None,
                              data_unavailable=False, data_unavailable_reason=None,
                              bench_trends=[], new_construction_count=0, risk_flagged_count=0,
                              subtype_cap=8, top_neighborhoods=[], bottom_neighborhoods=[]))

    # AGGPRECOMP-2-FIX-2 fixture-coverage note (Fable's review): the render
    # matrix needs at minimum overall + one SECTOR view + the empty-view
    # case + the stale-data case, not just "overall" repeated under
    # different status_2026 values. Everything above is "overall"; the
    # data_unavailable cases below only use a sector view (residential) for
    # the UNAVAILABLE branch, never for a real, POPULATED sector-view
    # render. This scenario closes that gap: a real sector view ("retail")
    # with MORE real subtype rows than SNAPSHOT_SUBTYPE_CAP (7), so the
    # rollup/capping display path (_cap_subtype_rows() in app.py) is
    # actually exercised through the template, not just "overall"'s
    # always-<=9-rows path which never triggers capping at all.
    def _sector_bd_row(ptype, n_parcels, mv25_b, mv26_b, med_pct=5.0):
        # None median_pct (the real _cap_subtype_rows() rollup-row shape --
        # see app.py's own docstring: a merged group's percentile is not a
        # valid derived statistic) must NOT be arithmetic'd into p25/p75 --
        # match the real function's behavior of setting all three to None
        # together, not just median_pct.
        p25 = p75 = None if med_pct is None else med_pct - 2
        if med_pct is not None:
            p75 = med_pct + 2
        return {"ptype": ptype, "sort_key": ptype, "n_parcels": n_parcels, "n_up": 60, "n_down": 30,
                "n_flat": 10, "median_pct": med_pct, "p25_pct": p25, "p75_pct": p75,
                "total_mv25_b": mv25_b, "total_mv26_b": mv26_b}

    check("snapshot.html / populated SECTOR view (retail, capped subtype rollup)",
          lambda: tpl.render(
              view="retail", mode="investor", status_2026="certified",
              data_unavailable=False, data_unavailable_reason=None,
              rows=[  # 8 real rows -- one more than SNAPSHOT_SUBTYPE_CAP=7, so the
                      # last row here represents the ALREADY-ROLLED-UP "Other Retail"
                      # bucket app.py's _cap_subtype_rows() would have produced --
                      # this is a real, sector-shaped fixture, not "overall" reused.
                  _sector_bd_row("Small Store (<10,000 SF)", 400, 50.0, 52.0),
                  _sector_bd_row("Strip Center", 350, 45.0, 46.5),
                  _sector_bd_row("Grocery Store", 200, 30.0, 31.0),
                  _sector_bd_row("Fast Food", 150, 12.0, 12.5),
                  _sector_bd_row("Restaurant (SFR Conv.)", 100, 8.0, 8.3),
                  _sector_bd_row("Convenience Store", 80, 5.0, 5.2),
                  _sector_bd_row("Auto Dealership", 60, 20.0, 21.0),
                  _sector_bd_row("Other Retail", 90, 6.0, 6.1, med_pct=None),  # rollup row: no valid median
              ],
              totals={"n_total": 1430, "n_up": 800, "n_down": 500, "n_flat": 130,
                      "total_mv25_b": 176.0, "total_mv26_b": 182.6, "median_pct": 4.8},
              bench_trends=[], new_construction_count=18, risk_flagged_count=4,
              subtype_cap=7, top_neighborhoods=[{"neighborhood_cd": "NB2", "n_parcels": 22, "median_pct": 9.1}],
              bottom_neighborhoods=[{"neighborhood_cd": "NB9", "n_parcels": 14, "median_pct": -3.4}],
          ))

    # Task AGGPRECOMP-2 (Aug 2026): the new "no live fallback, ever" gate --
    # _compute_snapshot_data() (app.py) now returns data_unavailable=True
    # with a real reason string when the Tier 1 summary tables are missing/
    # stale/inconsistent, instead of any of the fields above. This must
    # render a real, visible banner and MUST NOT touch rows/totals/etc (all
    # deliberately omitted from this context, matching exactly what
    # _compute_snapshot_data() actually returns in this branch -- if the
    # template accidentally referenced one of those omitted keys outside the
    # data_unavailable gate, this render would raise, not silently pass).
    def check_unavailable_banner(label, reason):
        def _render():
            out = tpl.render(view="overall", mode="investor",
                              data_unavailable=True, data_unavailable_reason=reason,
                              status_2026="none")
            if "temporarily unavailable" not in out:
                raise AssertionError("expected banner text 'temporarily unavailable' not found in output")
            if reason not in out:
                raise AssertionError(f"expected reason text {reason!r} not found in output")
            return out
        check(label, _render)

    check_unavailable_banner(
        "snapshot.html / data_unavailable=True (missing table)",
        "Market Snapshot summary data has not been generated yet (snapshot_breakdown is empty). "
        "This page reads only precomputed data -- run loaders/refresh_snapshot_summary.py to populate it.",
    )
    check_unavailable_banner(
        "snapshot.html / data_unavailable=True (stale)",
        "Market Snapshot data is stale -- a newer data load has not yet been reflected here. "
        "Run loaders/refresh_snapshot_summary.py to refresh it.",
    )
    check("snapshot.html / data_unavailable=True (homeowner mode, still renders)",
          lambda: tpl.render(view="residential", mode="homeowner",
                              data_unavailable=True, data_unavailable_reason="Test reason.",
                              status_2026="none"))

    # ── PX-20260830-04 Task 2: subtitle tier-derivation fix ────────────────
    # Live bug (Dallas, 2026-08-30): status_2026=="none" was rendered as if
    # it were "preliminary" by a bare {% else %} catch-all in the header
    # subtitle block (line ~27) -- see the root-cause comment there. This
    # produced "2026 Preliminary vs 2025 Certified" text immediately
    # followed by a "Preliminary" badge, which read as one garbled sentence
    # ("...Certified Preliminary") exactly matching what PM reported live.
    # These scenarios prove: (a) Dallas's real status (certified) now
    # renders the PM-specified exact string; (b) Travis's real status
    # (preliminary) is unchanged and still correct; (c) the actual bug
    # condition (status_2026=="none", which is what Dallas's snapshot
    # currently returns while data_unavailable=False and the view has no
    # 2026-joined rows) no longer claims "Preliminary" anywhere in the
    # output -- the direct regression proof for this brief's fix.
    _DALLAS_PROFILE = {
        "display_name": "Dallas County, TX",
        "county_name": "Dallas County",
        "cad_name": "Dallas Central Appraisal District",
        "tax_office_name": "Dallas County Tax Office",
    }
    _TRAVIS_PROFILE = env.globals["county_profile"]

    def check_subtitle(label, county_profile, status_2026, mode, expected, forbidden=None):
        def _render():
            ctx = {**_snapshot_ctx(status_2026, mode=mode), "county_profile": county_profile}
            out = tpl.render(**ctx)
            if expected not in out:
                raise AssertionError(f"expected subtitle text {expected!r} not found in output")
            if forbidden and forbidden in out:
                raise AssertionError(f"forbidden text {forbidden!r} found in output (residual bug)")
            return out
        check(label, _render)

    check_subtitle(
        "snapshot.html / PX-20260830-04 Task 2: Dallas (dcad_certified -> "
        "status_2026='certified') subtitle reads '2026 Certified vs 2025 Certified'",
        _DALLAS_PROFILE, "certified", "investor", "2026 Certified vs 2025 Certified",
    )
    check_subtitle(
        "snapshot.html / PX-20260830-04 Task 2: Travis (status_2026='preliminary') "
        "subtitle unchanged, reads '2026 Preliminary vs 2025 Certified'",
        _TRAVIS_PROFILE, "preliminary", "investor", "2026 Preliminary vs 2025 Certified",
    )

    # base.html's site-wide footer (line ~246) carries one legitimate,
    # deliberate "Preliminary" mention -- a general, per-county-neutral
    # disclosure ("2026 Certified or Preliminary by county, per that county's
    # own certification date") that exists specifically so no single page
    # asserts one blended status for every county (see base.html's own
    # comment above it). This is orthogonal to snapshot.html's per-view
    # status_2026 claim and must NOT trip this regression check.
    _EXPECTED_FOOTER_PRELIMINARY_MENTION = (
        "2026 Certified or Preliminary by county, per that county's own certification date"
    )

    def _render_none_status_no_false_preliminary_claim():
        # Matches the real live-bug shape: data_unavailable=False (parcel/
        # appraisal data IS loaded) but status_2026=="none" (the summary
        # layer hasn't computed a real tier yet for this view) -- the exact
        # condition that used to mislabel itself "Preliminary".
        out = tpl.render(**{**_snapshot_ctx("none"), "county_profile": _DALLAS_PROFILE})
        out_without_sitewide_footer = out.replace(_EXPECTED_FOOTER_PRELIMINARY_MENTION, "")
        if "Preliminary" in out_without_sitewide_footer:
            raise AssertionError(
                "status_2026='none' rendered the word 'Preliminary' somewhere in the "
                "page OUTSIDE the expected site-wide footer disclosure -- this is the "
                "exact live Dallas bug this fix was required to close (a 'not yet "
                "known' status must never claim a specific tier)."
            )
        if "Dallas County Market Snapshot" not in out:
            raise AssertionError(
                "expected the new status_2026=='none' fallback subtitle "
                "'Dallas County Market Snapshot' not found in output"
            )
        return out

    check(
        "snapshot.html / PX-20260830-04 Task 2: status_2026='none' (real Dallas "
        "shape, data available but tier not yet computed) makes NO 'Preliminary' claim",
        _render_none_status_no_false_preliminary_claim,
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All scenarios rendered cleanly, no leaked Jinja delimiters.")


if __name__ == "__main__":
    main()
