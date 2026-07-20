"""
verify_property_html_render.py — real Jinja RENDER verification for property.html.

Built July 2026, per Fable review P0-1 (the homeowner-mode waterfall
regression: a malformed Jinja comment closed early and leaked literal
template-delimiter text onto every homeowner-mode page, while
`jinja2.Environment().parse()` — the check this project relied on all
session — reported the file as syntactically valid, because it *was*
syntactically valid. A premature comment close is not a parse error; it's
a semantic bug that only a real render can catch.

This script does NOT use Flask (not installed in this sandbox, no network
access to install it). It builds a bare Jinja2 Environment against the
templates/ directory, stubs the two Flask-Jinja integration points the
templates rely on (`url_for`, `request.path`), and renders property.html
with a realistic mock context for a fixed set of scenarios. It is a syntax
and template-logic check only — it proves the template renders, produces
no leaked delimiters, and contains the elements a real page must contain.
It does NOT prove the real live app + real database produce this same
output; only Diego's live check can confirm that.

Two regression guards (per Diego's explicit P0-1 ask), enforced for every
scenario below:
  1. Rendered output must never contain a raw, un-rendered Jinja delimiter
     ("{%" or "#}") as visible text — this is exactly the failure mode of
     the bug this script was built to catch.
  2. A known-good, verified-billing parcel's rendered page (both modes)
     must contain the waterfall's real container id
     (id="billWaterfallChart") when bill_waterfall is populated.

Run: python3 verify_property_html_render.py
Exits non-zero and prints a diagnosis if any scenario fails.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Issue 1 ("Homestead-Cap Data Integrity: Full Fix Set" Cowork brief, July
# 2026) -- real function, not a mock: base_context() calls this on its 2026
# fixture row the same way property_detail() (app.py) does, so every
# template scenario built from base_context() carries the same
# est_assessed_2026/est_taxable_2026/basis_2026/is_approx_2026 keys real
# pages always have, instead of leaving the 2026 row's shape
# production never actually produces (present current_2026, absent derived
# keys) and masking template bugs that only StrictUndefined would catch.
from tax_logic.texas import derive_2026_baseline as _derive_2026_baseline

# Cowork brief "Version Display + Single Source of Truth", July 2026: base.html
# now reads config.VERSION, injected in production by app.py's inject_mode()
# context processor for every request regardless of what an individual route's
# render_template() call passes. The real config module is imported and
# registered as a Jinja global below (same treatment as url_for/request) so
# every existing scenario picks this up automatically, with no per-scenario
# context dict needing an explicit "config" key added.
import config as _real_config

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

# ── Flask-Jinja integration stubs ───────────────────────────────────────────
class _FakeRequest:
    def __init__(self, path="/parcel/0100030109"):
        self.path = path
        self.args = {}


def _url_for(endpoint, **kwargs):
    if endpoint == "static":
        return "/static/" + kwargs.get("filename", "")
    return "/" + endpoint.lstrip("/")


def _tojson(value):
    # Approximates Flask's |tojson (html-safe JSON string embed) closely
    # enough for a syntax/render check -- not byte-identical to Flask's,
    # which additionally escapes <, >, &, ' for safe embedding in <script>.
    return json.dumps(value)


def make_env():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # StrictUndefined so a missing mock field fails loudly (KeyError-like)
        # instead of silently rendering blank -- we want this script to force
        # the mock context to be genuinely complete, not accidentally pass by
        # skipping sections whose variables happen to be Undefined.
        undefined=StrictUndefined,
    )
    env.globals["url_for"] = _url_for
    env.globals["request"] = _FakeRequest()
    env.globals["config"] = _real_config
    env.filters["tojson"] = _tojson
    return env


# ── Mock data builders ──────────────────────────────────────────────────────
def _hist_row(year, market_value, assessed_value, taxable_value, total_tax,
               is_billing_verified, total_tax_derived=False, computed_total_tax=None,
               hs_cap_loss=0, land_value=None, imprv_value=None, exemption_codes=None,
               data_source="taxcur_current", billing_source=None, billing_exemptions=None):
    return {
        "tax_year": year,
        "market_value": market_value,
        "assessed_value": assessed_value,
        "taxable_value": taxable_value,
        "total_tax": total_tax,
        "is_billing_verified": is_billing_verified,
        "total_tax_derived": total_tax_derived,
        "computed_total_tax": computed_total_tax,
        "hs_cap_loss": hs_cap_loss,
        "land_value": land_value or int(assessed_value * 0.3),
        "imprv_value": imprv_value or int(assessed_value * 0.7),
        "exemption_codes": exemption_codes,
        "data_source": data_source,
        "billing_source": billing_source,
        "billing_exemptions": billing_exemptions,
    }


def _entity_row(code, name, rate, amount_due, rate_prev=None, is_school=False):
    return {
        "entity_code": code,
        "entity_name": name,
        "rate": rate,
        "rate_prev": rate_prev if rate_prev is not None else rate,
        "amount_due": amount_due,
        "amount_paid": amount_due,
        "is_school": is_school,
        "rate_used": rate,
        "rate_projected": False,
        "seller_tax": amount_due,
        "delta": 0,
        "taxable": amount_due / rate * 100 if rate else None,
        "est_tax": amount_due,
    }


def build_bill_waterfall_mock(reset=False):
    """
    reset=False: ordinary, small YoY change -- reset_signature should NOT fire.
    reset=True: the 0121230106 (1 Hedge Ln) regression fixture (P0-5) --
    $15,887 (2024) -> $33,372 (2025), +110%, taxable roughly doubling on a
    barely-moved rate. Real per-entity/assessed figures aren't available in
    this sandbox (no live DB), so these numbers are a constructed stand-in
    that reproduces the REPORTED aggregate pattern (modest assessed growth,
    near-flat rate, a large exemption_effect dominating a small value_effect)
    -- verified via build_bill_waterfall() itself (extracted from app.py and
    exec'd against this exact fixture) to reproduce reset_signature=True with
    real_delta matching $17,485 exactly; see the P0-5 report for that run.
    Diego should re-confirm reset_signature actually fires against this
    parcel's REAL data live -- this fixture only proves the template renders
    the callout correctly when the flag is True, not that the flag itself
    fires correctly against 0121230106's real database row (that part was
    verified separately, against the app.py function directly, not through
    this template-only harness).
    """
    if reset:
        return {
            "prior_year": 2024,
            "cur_year": 2025,
            "start_total": 15887.00,
            "end_total": 33372.00,
            "value_effect": 1025.00,
            "exemption_effect": 16298.00,
            "rate_effect": 162.00,
            "other_effect": 0.0,
            "real_delta": 17485.00,
            "incomplete": False,
            "reset_signature": True,
            "reset_note": ("Exemptions or a cap likely reset (commonly after a sale) — "
                            "most of the change in 2025 taxable value isn't explained by "
                            "the assessed-value change alone."),
            "entity_rate_effects": [
                {"entity_code": "TCO", "entity_name": "Travis County", "rate_cur": 2.06,
                 "rate_prior": 2.05, "rate_effect": 162.0},
            ],
        }
    return {
        "prior_year": 2024,
        "cur_year": 2025,
        "start_total": 8200.00,
        "end_total": 8850.00,
        "value_effect": 900.00,
        "exemption_effect": -150.00,
        "rate_effect": -100.00,
        "other_effect": 0.0,
        "real_delta": 650.00,
        "incomplete": False,
        "reset_signature": False,
        "reset_note": None,
        "entity_rate_effects": [
            {"entity_code": "01", "entity_name": "Travis County", "rate_cur": 0.35,
             "rate_prior": 0.36, "rate_effect": -50.0},
            {"entity_code": "02", "entity_name": "Austin ISD", "rate_cur": 0.9505,
             "rate_prior": 0.97, "rate_effect": -50.0},
        ],
    }


def _load_real_app_functions():
    """
    Extract build_tax_calendar(), build_document_sources(),
    combine_confidence_tiers(), CERTIFIED_TIER_DATA_SOURCES, and
    _row_confidence() straight from app.py's own source (July 2026 Cowork
    briefs: Tax Calendar/Documents & Sources panel, and the AJR/historical-
    year confidence-tiering fix) via the same slice-and-exec technique
    already used to isolate-test them this session -- app.py can't be
    imported directly here (Flask/psycopg2 aren't installed in this
    sandbox, per this file's own module docstring), so this harness
    exercises the REAL function bodies rather than hand-typed mocks that
    could silently drift from what app.py actually returns.

    build_document_sources() now calls _row_confidence() and
    combine_confidence_tiers() internally (July 2026 fix) -- both are
    defined much later in app.py (~line 3894/3991, well after
    build_document_sources' own ~line 566) but that's fine: exec'd function
    bodies resolve free variables against `ns` (their __globals__) at CALL
    time, not def time, so as long as all three names are in `ns` before
    _build_document_sources() is actually invoked, order-of-definition
    within this loader doesn't matter -- only order-of-slicing to find each
    def's own end boundary does.
    """
    src = open(os.path.join(os.path.dirname(__file__), "app.py")).read()
    from tax_logic.classify import property_type_label, label_case_sql, label_sort_case_sql
    # build_insights() (added for the "Fix Remaining Homestead-Cap Gaps" Cowork
    # brief's item 1 regression scenarios) calls property_type_label() directly
    # (app.py's own `from tax_logic.classify import ...` at module level) --
    # pre-populate it in `ns` the same way `date` already is, since the exec'd
    # function body resolves free variables against `ns` at call time.
    ns = {
        "date": __import__("datetime").date,
        "property_type_label": property_type_label,
        "label_case_sql": label_case_sql,
        "label_sort_case_sql": label_sort_case_sql,
    }
    # Each start_marker is a distinctive substring (verified unique in app.py
    # via a one-off grep before writing this) anchored right at the actual
    # def/assignment -- NOT just the bare name, since names like
    # "combine_confidence_tiers" and "_row_confidence" also appear earlier
    # in app.py inside comments/docstrings referencing them, which would
    # make a bare src.index(name) find the wrong (earlier, comment) spot.
    for start_marker, next_marker in [
        ("\ndef build_insights", "\ndef build_bill_waterfall"),
        ("\ndef build_projections", "\ndef build_tax_calendar"),
        ("\nTAX_DELQ_EXPORT_DATE = date(", "\ndef build_insights"),
        ("\ndef build_tax_calendar", "\ndef build_document_sources"),
        ("\ndef build_document_sources", "\ndef generate_property_narrative"),
        ("\ndef combine_confidence_tiers", "\nCERTIFIED_TIER_DATA_SOURCES = frozenset("),
        ("\nCERTIFIED_TIER_DATA_SOURCES = frozenset(", "\ndef _row_confidence"),
        ("\ndef _row_confidence", "\n@app.route(\"/api/search_filter\")"),
    ]:
        start = src.index(start_marker)
        end = src.index(next_marker, start)
        exec(src[start:end], ns)
    return (ns["build_tax_calendar"], ns["build_document_sources"], ns["_row_confidence"],
            ns["TAX_DELQ_EXPORT_DATE"], ns["build_projections"], ns["build_insights"])


(_build_tax_calendar, _build_document_sources, _row_confidence, _TAX_DELQ_EXPORT_DATE,
 _build_projections, _build_insights) = _load_real_app_functions()


def base_context(mode="homeowner", with_waterfall=True, residential=True, waterfall_reset=False,
                  delinquent=None):
    """A complete mock context matching every kwarg property_detail() passes
    to render_template(), for a well-behaved residential parcel with
    verified 2024+2025 billing (so bill_waterfall renders when requested).
    waterfall_reset=True (P0-5): use the 0121230106-style reset fixture
    instead of the ordinary small-change one, to exercise the new
    reset_signature callout.
    delinquent (July 2026, per Diego's "Delinquency Data Freshness" Cowork
    brief): None by default (matches property_detail()'s own real shape for
    the large majority of parcels, which have no tax_delinquent row at all);
    pass a dict shaped like a tax_delinquent row (total_due, first_delinquent_yr,
    cause_number) to exercise the Delinquency panel in both modes."""
    entity_detail = [
        _entity_row("01", "Travis County", 0.35, 3000.0, rate_prev=0.36, is_school=False),
        _entity_row("02", "Austin ISD", 0.9505, 5850.0, rate_prev=0.97, is_school=True),
    ]
    history = [
        _hist_row(2021, 380000, 350000, 330000, 7100.0, True),
        _hist_row(2022, 410000, 370000, 350000, 7500.0, True),
        _hist_row(2023, 440000, 395000, 375000, 7900.0, True),
        _hist_row(2024, 460000, 410000, 390000, 8200.0, True),
        _hist_row(2025, 480000, 430000, 410000, 8850.0, True),
        _hist_row(2026, 505000, 452000, None, None, False),
    ]
    current = next(r for r in history if r["tax_year"] == 2025)
    current_2026 = next(r for r in history if r["tax_year"] == 2026)
    # Issue 1: real derive_2026_baseline() call, same as property_detail()
    # (app.py) -- see this module's top-of-file import comment.
    _baseline_2026 = _derive_2026_baseline(current, current_2026)
    current_2026.update(_baseline_2026 or {
        "est_assessed_2026": None, "est_taxable_2026": None,
        "basis_2026": None, "is_approx_2026": False, "confidence_2026": None,
    })

    parcel = {
        "geo_id": "0100030109",
        "prop_id": "123456",
        "owner_name": "TEST OWNER" if residential else "TEST LLC OWNER",
        "situs_address": "123 Test St, Austin, TX 78701",
        "legal_desc": "LOT 1 BLK A TEST SUB",
        "classi_cd": "A1" if residential else "F1",
        "state_cd1": "A" if residential else "F",
        "prop_type_cd": "R" if residential else "C",
        "neighborhood_cd": "08SC",
        "year_built": 1995,
        "living_area_sqft": 2100,
        "gross_building_area_sqft": 2400,
        "gross_excluded_sqft": 300,
        "gross_excluded_detail": "Garage 300 SF",
        "land_sqft": 7200,
        "imp_det_json": json.dumps([
            {"code": "MA", "desc": "Main Area", "sqft": 2100, "excluded": False},
            {"code": "GAR", "desc": "Garage", "sqft": 300, "excluded": True},
        ]),
    }

    insights = {
        "earliest_year": 2021, "latest_year": 2025, "span": 4,
        "earliest_market": 380000, "latest_market": 480000, "latest_assessed": 430000,
        "value_cagr": 6.0, "value_change_pct": 26.3,
        "total_rate_2025": 2.05, "rate_delta": -0.02,
        "entity_count": 2, "est_annual_tax": 8850.0,
        "prop_class": "Residential",
        "delinquent_amount": 0, "delinquent_since": None,
        # hs_history_* deliberately omitted, not set to None: app.py's
        # build_insights() only ever sets these three keys together, as a
        # group, when a real AJR homestead-cap history row exists -- never
        # present-but-None. The template gates on `is defined`, so a mock
        # with these keys present-but-None doesn't match any real shape
        # build_insights() actually produces.
    }

    kpi = {
        "market_value": 505000, "market_value_year": 2026, "market_value_source": "preliminary",
        "yoy_pct": 5.2, "yoy_label": "2025 → 2026",
        "assessment_ratio": 89.6, "assessment_ratio_year": 2025,
        "effective_tax_rate": 1.8437, "effective_tax_rate_year": 2025,
        "effective_tax_rate_derived": False,
        # P0-3 fields: weakest-link tier/note computed by app.py's
        # combine_confidence_tiers(), not just effective_tax_rate_derived.
        "effective_tax_rate_tier": "verified",
        "effective_tax_rate_note": "Verified",
        "effective_tax_rate_2026_est": 1.79,
    }

    def _metrics_row(year, effective_tax_rate, yoy_tax_amount_pct, yoy_market_value_pct,
                      assessment_ratio=89.6, yoy_assessed_value_pct=4.9,
                      cumulative_value_growth_pct=26.3):
        return {
            "tax_year": year, "coverage_level": "full", "has_tax_data": True,
            "effective_tax_rate": effective_tax_rate,
            "effective_tax_rate_derived": False,
            "yoy_tax_amount_pct": yoy_tax_amount_pct,
            "yoy_market_value_pct": yoy_market_value_pct,
            "yoy_assessed_value_pct": yoy_assessed_value_pct,
            "assessment_ratio": assessment_ratio,
            "cumulative_value_growth_pct": cumulative_value_growth_pct,
            "risk_delinquent": False,
            "risk_homestead_cap_expiry": False,
            "cap_step_up_exposure": False,
            "cap_expiry_signal": False,
            "risk_data_incomplete": False,
            "risk_large_value_jump": False,
            "risk_large_value_jump_pct": None,
        }

    metrics_by_year = {
        2021: _metrics_row(2021, 0.0187, None, None),
        2022: _metrics_row(2022, 0.0183, 5.6, 7.9),
        2023: _metrics_row(2023, 0.0180, 5.3, 7.3),
        2024: _metrics_row(2024, 0.0178, 3.8, 4.5),
        2025: _metrics_row(2025, 0.018437, 7.9, 4.3),
    }

    # Real field names per build_projections()'s _make_rows() (app.py):
    # year / market / assessed / rate / est_tax / value_change -- NOT
    # tax_year/market_value/assessed_value/year_index, which belong to a
    # different structure (the estimator's own multiyear rows).
    def _proj_rows(cagr):
        rows = []
        base_market = 480000
        for i in range(1, 6):
            pmv = round(base_market * (1 + cagr) ** i)
            rows.append({
                "year": 2026 + i,
                "market": pmv,
                "assessed": round(pmv * 0.95),
                "rate": round(2.0 - 0.02 * i, 6),
                "est_tax": round(pmv * 0.95 * (2.0 - 0.02 * i) / 100),
                "value_change": round((pmv - base_market) / base_market * 100, 1),
            })
        return rows

    proj_bands = {
        "cagr_low": 4.0, "cagr_base": 6.0, "cagr_high": 8.0,
        "low": _proj_rows(0.04),
        "high": _proj_rows(0.08),
    }
    projections = _proj_rows(0.06)

    # compute_annual_trends() (app.py) returns a LIST of row dicts, not one
    # dict -- {% for row in annual_trends %} iterates rows directly.
    annual_trends = [
        dict(label="Market Value Growth", twelve_month="+5.2%", hist_avg="+6.0%",
             forecast_avg="+6.0%", peak="+8.1%", peak_when="2023",
             trough="+3.4%", trough_when="2021", note=""),
        dict(label="Assessment Ratio", twelve_month="89.6%", hist_avg="88.2%",
             forecast_avg="—", peak="91.0%", peak_when="2022",
             trough="85.4%", trough_when="2021", note=""),
        dict(label="Effective Tax Rate (2025)", twelve_month="1.8437%", hist_avg="1.8437%",
             forecast_avg="—", peak="1.8437%", peak_when="2025",
             trough="1.8437%", trough_when="2025", note=""),
        dict(label="Tax Amount", twelve_month="$8,850", hist_avg="$7,910",
             forecast_avg="~$9,400", peak="$8,850", peak_when="2025",
             trough="$7,100", trough_when="2021", note=""),
    ]

    # P0-4 (10% cap term): includes cap_savings_estimated/total_savings_incl_cap/
    # homestead_cap_pct so the new homestead_savings_card() "no exemption yet,
    # here's what filing (incl. the cap) would save" branch renders for real
    # instead of hitting StrictUndefined -- also exercises that branch's
    # non-cap fallback wording implicitly via has_hs=True fixtures elsewhere.
    hs_potential_savings = None
    if residential:
        hs_potential_savings = {
            "estimated_annual_savings": 3421.0,
            "school_hs_exemption": 140000,
            "local_option_entities": ["Travis County"],
            "cap_savings_estimated": 7500.0,
            "total_savings_incl_cap": 10921.0,
            "homestead_cap_pct": 0.10,
        }

    bill_waterfall = build_bill_waterfall_mock(reset=waterfall_reset) if with_waterfall else None

    # Fixed "today" (not datetime.now()) so this harness's output is
    # deterministic across runs -- matches the sandbox's actual server date
    # confirmed earlier this session (July 16, 2026).
    _today = __import__("datetime").date(2026, 7, 16)
    tax_calendar = _build_tax_calendar(_today, current_2026, delinquent)
    doc_sources = _build_document_sources(parcel, history, current, entity_detail, delinquent)

    return dict(
        parcel=parcel,
        imp_det=json.loads(parcel["imp_det_json"]),
        history=history,
        rate_history=[],
        current=current,
        current_2026=current_2026,
        tax_calendar=tax_calendar,
        doc_sources=doc_sources,
        entity_detail=entity_detail,
        delinquent=delinquent,
        delinquent_export_date=_TAX_DELQ_EXPORT_DATE,
        insights=insights,
        projections=projections,
        proj_bands=proj_bands,
        proj_baseline="2025 certified → 2026 preliminary",
        metrics_by_year=metrics_by_year,
        # Left empty deliberately: the Submarket Position / neighborhood-
        # benchmark sections gated on this (P1 item 6/11 territory, not
        # part of this P0-1 pass) reference a `_r25` local `{% set %}` that
        # turned out to be scoped inside a conditional block elsewhere in
        # the template and isn't reliably available outside it in a partial
        # mock -- worth its own investigation when P1 items 6/11 are
        # actually worked, not a blocker for the P0-1 waterfall/order checks
        # this script exists to run right now.
        benchmark_by_year={},
        bench_label="Residential" if residential else "Commercial",
        state_cd_descriptions={},
        use_code_lookup={},
        val_method="Market",
        entity_rate_by_code={},
        chart_entity_data={},
        chart_years=list(range(2016, 2026)),
        estimated_tax_2026=8700.0,
        assumed_rate_2026=1.7995,
        kpi=kpi,
        narrative="Test narrative sentence.",
        annual_trends=annual_trends,
        hs_potential_savings=hs_potential_savings,
        bill_waterfall=bill_waterfall,
        mode=mode,
    )


def confidence_tier_context(mode, source_prefix, anomaly_year=None):
    """
    Cowork brief (July 2026, "Fix AJR/Historical-Year Confidence Tiering",
    build phase) regression fixture -- 3 scenarios named explicitly in the
    brief: a cert_202x row with no anomaly (expect Verified), an ajr_202x
    row with no anomaly (expect Verified), and a row that trips the AV>MV
    check (expect it stays Partial, with the anomaly icon shown).

    Built as a thin wrapper around base_context() rather than a parallel
    fixture, so it inherits the exact same well-formed parcel/entity/
    metrics/projections shape that fixture already got right -- only the
    2021-2024 history rows' data_source (and, for one scenario, one year's
    assessed_value) are overridden.

    base_context()'s OWN default history fixture uses a placeholder
    data_source ("taxcur_current") that isn't a real value app.py ever
    writes -- pre-existing, out of scope for this fix, and harmless for the
    8 waterfall/copy/leaked-delimiter scenarios that fixture serves (they
    don't test confidence tiering). It does mean this is the first place in
    this harness that exercises the certified-tier-eligible branch of
    _row_confidence()/its Jinja mirror with realistic data_source values --
    flagged here rather than silently relying on it having been covered
    already.

    source_prefix: "cert" or "ajr" -- becomes cert_2021..cert_2024 or
    ajr_2021..ajr_2024 on the 2021-2024 rows (real values per
    CERTIFIED_TIER_DATA_SOURCES, app.py). The 2025 "current" row is also
    set to the real "certified" data_source (base_context()'s default
    doesn't cover this either) so the top "Property-level confidence
    badges" block's own certified-tier branch gets exercised too, alongside
    doc_sources' AJR row -- confirming the anomaly check stays scoped to
    the specific year it applies to and doesn't leak into 2025's separate
    badge.

    anomaly_year: if given (2021-2024), that single year's assessed_value
    is pushed $5,000 above its own market_value -- the real per-record
    AV>MV signal _row_confidence() now checks -- while every other year
    stays clean, so the resulting scenario isolates exactly one flagged
    year against three clean ones (matching combine_confidence_tiers()'s
    weakest-link behavior: one Partial year should demote the whole
    doc_sources AJR row to Partial, but must NOT touch the 2025 badge or
    the OTHER three years' own anomaly icons).
    """
    ctx = base_context(mode, with_waterfall=True, residential=True)
    ctx["current"]["data_source"] = "certified"
    for r in ctx["history"]:
        if r["tax_year"] in (2021, 2022, 2023, 2024):
            r["data_source"] = f"{source_prefix}_{r['tax_year']}"
            if r["tax_year"] == anomaly_year:
                r["assessed_value"] = r["market_value"] + 5000
    # doc_sources was already built once inside base_context(), off the
    # unmodified history -- rebuild it for real off the now-overridden
    # rows, same call signature property_detail() (app.py) uses.
    ctx["doc_sources"] = _build_document_sources(
        ctx["parcel"], ctx["history"], ctx["current"], ctx["entity_detail"], ctx["delinquent"]
    )
    return ctx


# ── Regression guards ───────────────────────────────────────────────────────
RAW_DELIMITERS = ["{%", "#}", "{#"]


def check_no_leaked_delimiters(html, label):
    problems = []
    for delim in RAW_DELIMITERS:
        if delim in html:
            idx = html.index(delim)
            snippet = html[max(0, idx - 60):idx + 60].replace("\n", " ")
            problems.append(f"  found raw '{delim}' in rendered output: ...{snippet}...")
    if problems:
        print(f"FAIL [{label}] leaked template delimiter(s):")
        for p in problems:
            print(p)
        return False
    return True


def check_waterfall_present(html, label):
    if 'id="billWaterfallChart"' not in html:
        print(f"FAIL [{label}] expected waterfall container id=\"billWaterfallChart\" not found in rendered output")
        return False
    return True


def check_waterfall_absent(html, label):
    if 'id="billWaterfallChart"' in html:
        print(f"FAIL [{label}] waterfall container id=\"billWaterfallChart\" unexpectedly present (bill_waterfall was None)")
        return False
    return True


def check_reset_callout_present(html, label):
    """P0-5 regression guard: when bill_waterfall.reset_signature is True
    (the 0121230106-style pattern), the reset_note text must actually reach
    rendered output -- not just be present in the mock context."""
    if "Exemptions or a cap likely reset" not in html:
        print(f"FAIL [{label}] expected reset-signature callout text not found in rendered output")
        return False
    return True


def check_top_appraisal_badge(html, label, expect_text):
    """
    Confidence-tiering fix (July 2026) regression guard: the "Property-level
    confidence badges" block (property.html ~line 1442-1450) shows exactly
    one of 4 literal, mutually-exclusive strings for the 2025 `current` row
    -- "Appraisal: 2025 Certified" / "Appraisal: Partial" / "Appraisal: 2026
    Preliminary" / "Appraisal: Unknown". expect_text is one of those; this
    checks it's present and none of the OTHER three leaked in as well.
    """
    all_labels = ["Appraisal: 2025 Certified", "Appraisal: Partial",
                  "Appraisal: 2026 Preliminary", "Appraisal: Unknown"]
    if expect_text not in html:
        print(f"FAIL [{label}] expected top appraisal badge {expect_text!r} not found in rendered output")
        return False
    others_present = [t for t in all_labels if t != expect_text and t in html]
    if others_present:
        print(f"FAIL [{label}] unexpected additional top appraisal badge text also present: {others_present}")
        return False
    return True


def check_doc_sources_ajr_badge(html, label, expect_badge_label, expect_coverage_substr=None):
    """
    Confidence-tiering fix (July 2026) regression guard: the Documents &
    Sources panel's AJR row (build_document_sources(), app.py) now computes
    a real per-year combined tier instead of a hardcoded "Partial" -- checks
    the rendered badge text in that row matches what this scenario's
    data_source/anomaly setup should produce, and (optionally) that the
    coverage cell's flagged-year annotation is present/absent as expected.
    """
    # "Annual Jurisdiction Roll (AJR)" alone is NOT a safe anchor -- it also
    # appears in the Value History card's static footer prose ("2021–2024
    # values — Texas Comptroller Annual Jurisdiction Roll (AJR)."), which
    # renders BEFORE this panel and has no badge of its own; html.find()
    # would latch onto that first occurrence and silently check the wrong
    # spot. Anchor on the Documents & Sources card header first, then search
    # for the AJR row within that panel only.
    panel_idx = html.find("Documents &amp; Sources")
    if panel_idx == -1:
        print(f"FAIL [{label}] Documents & Sources panel not found in rendered output")
        return False
    marker = "Annual Jurisdiction Roll (AJR)"
    idx = html.find(marker, panel_idx)
    if idx == -1:
        print(f"FAIL [{label}] Documents & Sources AJR row not found within the panel")
        return False
    window = html[idx:idx + 700]
    if f">{expect_badge_label}<" not in window:
        print(f"FAIL [{label}] Documents & Sources AJR badge does not show expected {expect_badge_label!r}")
        return False
    if expect_coverage_substr is not None and expect_coverage_substr not in window:
        print(f"FAIL [{label}] Documents & Sources AJR coverage text missing expected substring {expect_coverage_substr!r}")
        return False
    return True


def check_value_history_anomaly_icons(html, label, expect_years_with_icon):
    """
    Confidence-tiering fix (July 2026) regression guard: the Value History
    table's per-row "!" badge-data-anomaly icon (property.html ~line 3183-
    3192) is the visible explanation for a Partial tier -- checks it shows
    up on exactly the years expected to trip assessed>market, and nowhere
    else (proving the anomaly check stays scoped to the one flagged year,
    not leaking across the whole table).
    """
    import re
    rows = list(re.finditer(r'<tr data-year="(\d{4})">', html))
    if not rows:
        print(f"FAIL [{label}] no Value History table rows found (data-year pattern didn't match)")
        return False
    ok = True
    for i, m in enumerate(rows):
        year = int(m.group(1))
        start = m.end()
        end = rows[i + 1].start() if i + 1 < len(rows) else html.find("</tbody>", start)
        row_html = html[start:end]
        has_icon = "badge-data-anomaly" in row_html
        should_have = year in expect_years_with_icon
        if has_icon != should_have:
            print(f"FAIL [{label}] Value History row for {year}: anomaly icon present={has_icon}, expected={should_have}")
            ok = False
    return ok


def check_delinquency_as_of_date(html, label, expect_present, expect_date_str=None, expect_total_due=None):
    """
    Delinquency Data Freshness fix (July 2026, per Diego's Cowork brief)
    regression guard. expect_present=False: confirms the panel is genuinely
    absent when there's no delinquent balance (the existing `{% if
    delinquent... %}` guard, unchanged by this fix). expect_present=True:
    confirms the Total Due dollar figure, the as-of date (in the same
    "Month Day, Year" format tax_calendar_strip() already uses via
    strftime('%B %-d, %Y')), and the Tax Code §33.01 growth caveat all
    appear together -- both modes render some literal marker text
    ("Delinquency" in Investor mode, "Unpaid Taxes on" in Homeowner mode),
    so this checks for "As of {date}" plus the dollar figure without
    assuming which mode's exact card title is present.
    """
    if not expect_present:
        if "As of " in html and "§33.01" in html:
            print(f"FAIL [{label}] delinquency as-of-date text unexpectedly present with no delinquent balance in the fixture")
            return False
        return True

    ok = True
    as_of_marker = f"As of {expect_date_str}"
    if as_of_marker not in html:
        print(f"FAIL [{label}] expected as-of-date text {as_of_marker!r} not found in rendered output")
        ok = False
    if "§33.01" not in html:
        print(f"FAIL [{label}] expected Tax Code §33.01 growth-caveat citation not found in rendered output")
        ok = False
    if expect_total_due is not None:
        due_str = "${:,.2f}".format(expect_total_due)
        if due_str not in html:
            print(f"FAIL [{label}] expected Total Due figure {due_str!r} not found in rendered output")
            ok = False
    return ok


def check_taxable_value_kpi(html, label, mode, expect_tv25, expect_tv26, expect_gap25_pct=None, expect_gap26_pct=None):
    """
    Taxable Value KPI card fix (July 2026, per Diego's Cowork brief --
    county_benchmark / Est. 2026 Total Tax investigation round) regression
    guard. Confirms the new card renders the right dollar figure in both
    modes, and -- the specific bug this check exists to catch -- that the
    "N% below market" sub-line renders a SINGLE percent sign, not two
    ("15%% below market"), a real bug caught during this round's own
    verification (the format string already appends "%"; the template text
    after it also had a literal "%", doubling it up).
    """
    ok = True
    if "%%" in html:
        print(f"FAIL [{label}] literal doubled percent sign ('%%') found in rendered output -- format-string / template-text duplication bug")
        ok = False
    if expect_tv25 is not None:
        tv25_str = "${:,.0f}".format(expect_tv25)
        if tv25_str not in html:
            print(f"FAIL [{label}] expected 2025 taxable value {tv25_str!r} not found in rendered output")
            ok = False
    if expect_tv26 is not None:
        tv26_str = "${:,.0f}".format(expect_tv26)
        if tv26_str not in html:
            print(f"FAIL [{label}] expected 2026 taxable value {tv26_str!r} not found in rendered output")
            ok = False
    if expect_gap25_pct is not None:
        gap_str = f"{expect_gap25_pct:.0f}% below market"
        if gap_str not in html:
            print(f"FAIL [{label}] expected 2025 gap text {gap_str!r} not found in rendered output")
            ok = False
    if expect_gap26_pct is not None:
        gap_str = f"{expect_gap26_pct:.0f}% below market"
        if gap_str not in html:
            print(f"FAIL [{label}] expected 2026 gap text {gap_str!r} not found in rendered output")
            ok = False
    # Mode-specific label text -- confirms the card actually made it into
    # both modes' markup, not just one.
    if mode == "investor" and "Taxable Value" not in html:
        print(f"FAIL [{label}] 'Taxable Value' card label not found (investor mode)")
        ok = False
    if mode == "homeowner" and "taxed on" not in html:
        print(f"FAIL [{label}] 'taxed on' card label not found (homeowner mode)")
        ok = False
    return ok


def check_possessive_voice(html, label, is_residential):
    """Copy review — Homeowner mode, item 1 regression guard: on a
    non-residential (LLC-owned) parcel in Homeowner mode, the second-person
    phrasing on the 'What you paid'/'Your Exemptions'/'Where Your Tax
    Dollars Went'/'Your Tax Rate & Bill' cards must flip to third person;
    on a residential parcel it must NOT flip (regression against accidentally
    breaking the common case while fixing the non-residential one)."""
    second_person_markers = [
        "What you paid in taxes",
        "Your Exemptions</div>",
        "Where Your 2025 Tax Dollars Went",
        ">You Paid<",
        "Your Tax Rate &amp; Bill — Last 5 Years",
    ]
    third_person_markers = [
        "What this property paid in taxes",
        "Exemptions</div>",  # bare "Exemptions" (no "Your") title
        "Where This Property's 2025 Tax Dollars Went",
        ">Paid<",
        "Tax Rate &amp; Bill — Last 5 Years",  # without leading "Your "
    ]
    if is_residential:
        missing = [m for m in second_person_markers if m not in html]
        if missing:
            print(f"FAIL [{label}] expected second-person marker(s) missing on residential render: {missing}")
            return False
    else:
        present = [m for m in second_person_markers if m in html]
        if present:
            print(f"FAIL [{label}] second-person marker(s) leaked into non-residential render: {present}")
            return False
        missing = [m for m in third_person_markers if m not in html]
        if missing:
            print(f"FAIL [{label}] expected third-person marker(s) missing on non-residential render: {missing}")
            return False
    return True


# ── Scenarios ────────────────────────────────────────────────────────────────
def check_suspicious_comments(template_path):
    """
    Source-level HEURISTIC lint, not an exhaustive detector -- flags any
    `{# ... #}` comment whose body contains a nested `{#`. This is a real
    smell (why would a comment's own text contain literal Jinja-comment-open
    syntax, unless by accident referencing another comment's delimiters the
    way the first version of the P0-1 bug did) and is cheap to run before
    ever rendering.

    Important limitation, confirmed while building this script: this check
    is NOT capable of catching every case in this bug class. Jinja comments
    close at the first `#}` found after `{#`, full stop -- there is no
    syntactic way to distinguish "this is genuinely where the comment ends"
    from "this closed earlier than the author intended" from source text
    alone, because intent isn't a syntax property. The P0-1 fix itself was
    re-broken by a SECOND instance of this exact bug mid-fix (this time with
    no nested `{#` at all, just a bare `"#}"` quoted for illustration) --
    `Environment().parse()` reported that version as syntactically valid
    too, and this heuristic would have missed it as well. The only
    unconditionally reliable guard against this bug class is the rendered-
    output check below (`check_no_leaked_delimiters`): if a raw delimiter
    shows up in real rendered HTML, a comment closed somewhere it shouldn't
    have, regardless of why.
    """
    src = open(template_path, encoding="utf-8").read()
    problems = []
    i, n, line = 0, len(src), 1
    while i < n:
        if src[i] == "\n":
            line += 1
        if src[i:i + 2] == "{#":
            start_line = line
            k = src.find("#}", i + 2)
            if k == -1:
                problems.append(f"line {start_line}: unclosed comment (no matching close found)")
                break
            inner = src[i + 2:k]
            if "{#" in inner:
                problems.append(f"line {start_line}: comment body contains a nested '{{#' -- likely referencing another comment's delimiters literally")
            line += src[i:k + 2].count("\n")
            i = k + 2
            continue
        i += 1
    return problems


def run():
    env = make_env()
    tmpl = env.get_template("property.html")
    all_ok = True

    print("── Source-level heuristic: nested '{#' inside a comment body ──")
    print("   (a lint, not a complete detector -- see check_suspicious_comments' docstring)")
    src_problems = check_suspicious_comments(
        os.path.join(TEMPLATE_DIR, "property.html")
    )
    if src_problems:
        for p in src_problems:
            print("WARN", p)
        # Heuristic-only: does not fail the run by itself, since it can also
        # false-positive on a comment that legitimately discusses Jinja
        # syntax without actually being broken. The render checks below are
        # what actually gate pass/fail for this bug class.
    else:
        print("ok: no nested '{#' found in any comment body")
    print()

    scenarios = [
        ("homeowner, residential, verified waterfall", base_context("homeowner", True, True), True, False),
        ("investor, residential, verified waterfall",  base_context("investor", True, True), True, False),
        ("homeowner, non-residential, verified waterfall", base_context("homeowner", True, False), True, False),
        ("investor, non-residential, verified waterfall", base_context("investor", True, False), True, False),
        ("homeowner, no waterfall data (incomplete billing)", base_context("homeowner", False, True), False, False),
        ("investor, no waterfall data (incomplete billing)", base_context("investor", False, True), False, False),
        # P0-5: 0121230106 (1 Hedge Ln) regression fixture -- reset_signature
        # should fire and its callout text should reach rendered output, in
        # both modes.
        ("homeowner, 0121230106-style reset fixture", base_context("homeowner", True, True, waterfall_reset=True), True, True),
        ("investor, 0121230106-style reset fixture",  base_context("investor", True, True, waterfall_reset=True), True, True),
    ]

    for label, ctx, expect_waterfall, expect_reset_callout in scenarios:
        try:
            html = tmpl.render(**ctx)
        except Exception as e:
            print(f"FAIL [{label}] render raised {type(e).__name__}: {e}")
            all_ok = False
            continue

        ok = check_no_leaked_delimiters(html, label)
        if expect_waterfall:
            ok = check_waterfall_present(html, label) and ok
        else:
            ok = check_waterfall_absent(html, label) and ok
        if expect_reset_callout:
            ok = check_reset_callout_present(html, label) and ok
        # Copy review — Homeowner mode, item 1: only meaningful in Homeowner
        # mode (Investor mode's cards were deliberately left untouched, per
        # Diego's explicit scope for that review).
        if ctx.get("mode") == "homeowner":
            # Mirrors property.html's own derivation: is_residential =
            # (not bench_label) or bench_label == 'Residential'.
            _bl = ctx.get("bench_label")
            _is_res = (not _bl) or _bl == "Residential"
            ok = check_possessive_voice(html, label, _is_res) and ok

        if ok:
            print(f"PASS [{label}]")
        else:
            all_ok = False

    # ── Homestead cap fix, build_projections() (July 2026 Cowork brief: "Fix
    # 6-Year Projection's Missing Homestead Cap for 2025+ Parcels") ─────────
    # Direct calls against the real build_projections() body itself (loaded
    # from app.py via _load_real_app_functions() above), not the template --
    # base_context()'s own `projections`/`proj_bands` are hand-built mocks
    # (see build_bill_waterfall_mock()'s sibling comment on why -- no live DB
    # to compute a real one against), so a template-render check alone could
    # never have caught this bug or verified its fix; it has to call the
    # real function.
    #
    # Scenario: a parcel whose earliest available history starts in 2025 --
    # NO 2021-2024 rows at all (not just missing hs_cap_loss on rows that
    # exist -- genuinely absent, the real shape of a parcel newly on the
    # certified/preliminary rolls). Real 2025 + 2026 figures modeled on
    # 0100050414 (one of the 1,361 parcels this brief's investigation found
    # affected): 2025 market=$552,000, HS-exempt, real ~58% market/taxable
    # gap (assessed capped, per its real exemption_codes='HS,OV65' -- see
    # the investigation's verify_all_1361_fixed.py cross-check). 2026
    # preliminary market bumped to $700,000 (+26.8% -- deliberately a much
    # higher single-year jump than any linear 10%/yr cap could track, so a
    # still-buggy has_hs_cap=False would produce a projection table that
    # visibly, unambiguously tracks raw market value instead of the capped
    # figure by year 1, not just a subtle drift by year 5).
    print()
    print("── build_projections() homestead-cap fix: 2025-only-history scenario ──")

    _hs2025_history = [
        _hist_row(2025, 552000, 232069, 232069, 5200.0, True,
                  hs_cap_loss=0, exemption_codes="HS,OV65", data_source="certified"),
        _hist_row(2026, 700000, 660000, None, None, False,
                  hs_cap_loss=0, exemption_codes="HS,OV65", data_source="preliminary"),
    ]
    _hs2025_entities = [
        _entity_row("01", "Travis County", 0.35, 800.0),
        _entity_row("02", "Austin ISD", 1.45, 3400.0, is_school=True),
    ]
    rows, baseline_label, bands = _build_projections(
        _hs2025_history, rate_history=[], entity_detail=_hs2025_entities, state_cd1="A"
    )
    ok = True
    if not rows:
        print("FAIL [hs-cap 2025-only] build_projections() returned no rows -- can't check capping")
        ok = False
    else:
        base_market, base_assessed = 700000.0, 660000.0
        year5 = rows[4]
        # The bug: pav = pmv unconditionally when has_hs_cap is (wrongly) False.
        # With a 2025-only history and NO fix, year 1's assessed would equal
        # that year's market exactly. With the OR-fix (exemption_codes='HS'
        # present in the 2025 row), assessed must be genuinely capped --
        # below market by year 1, and nowhere close to market's own ~26.8%
        # CAGR compounded five years by year 5.
        year1 = rows[0]
        if year1["assessed"] >= year1["market"]:
            print(f"FAIL [hs-cap 2025-only] year 1 assessed (${year1['assessed']:,}) not below market "
                  f"(${year1['market']:,}) -- cap not applied; has_hs_cap fix did not take effect "
                  f"(this is exactly the pre-fix bug's signature)")
            ok = False
        expected_capped_y5 = round(base_assessed * (1.10 ** 5))
        if year5["assessed"] != expected_capped_y5:
            print(f"FAIL [hs-cap 2025-only] year 5 assessed (${year5['assessed']:,}) != expected "
                  f"10%%/yr-capped value (${expected_capped_y5:,}) -- capping math wrong or not applied")
            ok = False
        if year5["assessed"] >= year5["market"] * 0.85:
            print(f"FAIL [hs-cap 2025-only] year 5 assessed (${year5['assessed']:,}) too close to "
                  f"market (${year5['market']:,}) for a genuinely capped projection over a 26.8%%/yr "
                  f"market CAGR -- cap likely not applied")
            ok = False
    if ok:
        print(f"PASS [hs-cap 2025-only] cap correctly applied with no 2021-2024 hs_cap_loss history "
              f"(exemption_codes='HS' fix): year1 assessed=${rows[0]['assessed']:,} < market=${rows[0]['market']:,}; "
              f"year5 assessed=${rows[4]['assessed']:,} (10%%/yr-capped) vs uncapped market=${rows[4]['market']:,}")
    else:
        all_ok = False

    # Regression guard: a parcel with NEITHER signal (no hs_cap_loss ANY
    # year, no 'HS' in exemption_codes ANY year -- i.e. genuinely not
    # homesteaded) must NOT get capped -- confirms the fix is a real OR, not
    # an accidental always-True.
    _no_hs_history = [
        _hist_row(2025, 800000, 800000, 800000, 15000.0, True,
                  hs_cap_loss=0, exemption_codes=None, data_source="certified"),
        _hist_row(2026, 1000000, 950000, None, None, False,
                  hs_cap_loss=0, exemption_codes=None, data_source="preliminary"),
    ]
    rows2, _, _ = _build_projections(
        _no_hs_history, rate_history=[], entity_detail=_hs2025_entities, state_cd1="F"
    )
    if rows2 and rows2[0]["assessed"] == rows2[0]["market"]:
        print(f"PASS [hs-cap regression: no HS signal at all] non-homesteaded parcel correctly "
              f"NOT capped (assessed tracks market: ${rows2[0]['assessed']:,})")
    else:
        print(f"FAIL [hs-cap regression: no HS signal at all] non-homesteaded parcel unexpectedly "
              f"capped -- OR condition may have become an always-True bug")
        all_ok = False

    # ── Homestead cap fix, build_insights() (July 2026 Cowork brief: "Fix
    # Remaining Homestead-Cap Gaps," item 1) ────────────────────────────────
    # Direct calls against the real build_insights() body -- same reasoning
    # as build_projections() above: base_context()'s `insights` is a
    # hand-built mock, so only a direct call exercises this fix.
    # Fixture: 0100050414's real 2025/2026 figures (a confirmed real parcel
    # from the original 1,361-affected population -- market/assessed/
    # exemption_codes pulled from the raw certified export during this
    # brief's own investigation, not invented).
    print()
    print("── build_insights() homestead-cap fix: exemption_codes approximation ──")

    _bi_parcel = {"state_cd1": "A", "classi_cd": "01", "prop_type_cd": "R"}
    _bi_entities = [
        {"rate": 0.3527, "rate_prev": 0.36},
        {"rate": 0.9464, "rate_prev": 0.97},
    ]

    # (a) No 2021-2024 AJR hs_cap_loss on file, but 'HS' in exemption_codes --
    # must now populate hs_history_* via the market-minus-assessed
    # approximation, flagged hs_history_is_approx=True.
    _bi_hist_approx = [
        {"tax_year": 2025, "market_value": 552000, "assessed_value": 232069,
         "taxable_value": 232069, "hs_cap_loss": None, "exemption_codes": "HS,OV65"},
        {"tax_year": 2026, "market_value": 580000, "assessed_value": 255276,
         "taxable_value": 255276, "hs_cap_loss": None, "exemption_codes": "HS,OV65"},
    ]
    out_a = _build_insights(_bi_parcel, _bi_hist_approx, _bi_entities, None)
    ok = True
    expected_loss = 580000 - 255276
    if out_a.get("hs_history_is_approx") is not True:
        print(f"FAIL [build_insights: exemption-code approx] hs_history_is_approx expected True, got {out_a.get('hs_history_is_approx')!r}")
        ok = False
    if out_a.get("hs_history_loss") != expected_loss:
        print(f"FAIL [build_insights: exemption-code approx] hs_history_loss expected {expected_loss:,}, got {out_a.get('hs_history_loss')!r}")
        ok = False
    if out_a.get("hs_history_year") != 2026:
        print(f"FAIL [build_insights: exemption-code approx] hs_history_year expected 2026 (latest HS-coded year), got {out_a.get('hs_history_year')!r}")
        ok = False
    if ok:
        print(f"PASS [build_insights: exemption-code approx] hs_history_loss=${out_a['hs_history_loss']:,} "
              f"(market-assessed approximation), year={out_a['hs_history_year']}, is_approx=True — "
              f"no AJR hs_cap_loss on file, correctly derived from exemption_codes instead")
    else:
        all_ok = False

    # (b) Real AJR hs_cap_loss present -- must use it as-is, is_approx=False
    # (regression: the approximation branch must not override real data).
    _bi_hist_real = [
        {"tax_year": 2023, "market_value": 400000, "assessed_value": 300000,
         "taxable_value": 300000, "hs_cap_loss": 45000, "exemption_codes": None},
        {"tax_year": 2025, "market_value": 480000, "assessed_value": 430000,
         "taxable_value": 410000, "hs_cap_loss": None, "exemption_codes": "HS"},
    ]
    out_b = _build_insights(_bi_parcel, _bi_hist_real, _bi_entities, None)
    if out_b.get("hs_history_is_approx") is False and out_b.get("hs_history_loss") == 45000 and out_b.get("hs_history_year") == 2023:
        print(f"PASS [build_insights: real AJR data unchanged] hs_history_loss=${out_b['hs_history_loss']:,} "
              f"(real AJR figure), year={out_b['hs_history_year']}, is_approx=False — unaffected by the fix")
    else:
        print(f"FAIL [build_insights: real AJR data unchanged] expected real AJR figure (45000, year 2023, "
              f"is_approx=False), got loss={out_b.get('hs_history_loss')!r} year={out_b.get('hs_history_year')!r} "
              f"is_approx={out_b.get('hs_history_is_approx')!r}")
        all_ok = False

    # (c) Neither signal (no hs_cap_loss, no 'HS' exemption code anywhere) --
    # hs_history_year must stay undefined, same as before this fix.
    _bi_hist_none = [
        {"tax_year": 2025, "market_value": 600000, "assessed_value": 600000,
         "taxable_value": 600000, "hs_cap_loss": None, "exemption_codes": None},
    ]
    out_c = _build_insights(_bi_parcel, _bi_hist_none, _bi_entities, None)
    if "hs_history_year" not in out_c:
        print(f"PASS [build_insights: no HS signal at all] hs_history_year correctly absent")
    else:
        print(f"FAIL [build_insights: no HS signal at all] hs_history_year unexpectedly present: {out_c.get('hs_history_year')!r}")
        all_ok = False

    # ── Homestead cap fix, tax_logic/texas.py's cap_was_active (July 2026
    # Cowork brief: "Fix Remaining Homestead-Cap Gaps," item 3) ─────────────
    # texas.py has no Flask/psycopg2 dependency (confirmed via its own module
    # docstring/imports), so it's imported and called directly here -- no
    # slice-and-exec needed, same fixture (0100050414) as build_insights() above.
    print()
    print("── tax_logic/texas.py cap_was_active fix: market-assessed approximation ──")
    import tax_logic.texas as _texas

    _tx_parcel = {"state_cd1": "A", "prop_type_cd": "R", "classi_cd": "01"}
    _tx_current_yr_row = {
        "market_value": 552000, "assessed_value": 232069, "taxable_value": 232069,
        "hs_cap_loss": None, "exemption_codes": "HS,OV65",
    }
    _tx_entities = [
        {"entity_code": "01", "entity_name": "Travis County", "rate": 0.3527, "amount_due": 818.6},
        {"entity_code": "02", "entity_name": "Austin ISD", "rate": 0.9464, "amount_due": 2196.3},
    ]
    tx_result = _texas.estimate_post_acquisition(
        _tx_parcel, _tx_current_yr_row, _tx_entities,
        purchase_price=600000, buyer_status="owner_occupant",
    )
    ok = True
    expected_hs_loss = 552000 - 232069
    if tx_result.get("cap_was_active") is not True:
        print(f"FAIL [texas.py: cap_was_active] expected True, got {tx_result.get('cap_was_active')!r}")
        ok = False
    if tx_result.get("hs_cap_loss") != expected_hs_loss:
        print(f"FAIL [texas.py: cap_was_active] hs_cap_loss expected {expected_hs_loss:,}, got {tx_result.get('hs_cap_loss')!r}")
        ok = False
    if tx_result.get("hs_cap_loss_is_approx") is not True:
        print(f"FAIL [texas.py: cap_was_active] hs_cap_loss_is_approx expected True, got {tx_result.get('hs_cap_loss_is_approx')!r}")
        ok = False
    cap_assumption = next((a for a in tx_result.get("assumptions", []) if a.startswith("Cap reset")), None)
    if not cap_assumption or "estimated as market" not in cap_assumption:
        print(f"FAIL [texas.py: cap_was_active] assumptions string missing concrete dollar figure + approximation caveat: {cap_assumption!r}")
        ok = False
    if f"${expected_hs_loss:,.0f}" not in (cap_assumption or ""):
        print(f"FAIL [texas.py: cap_was_active] assumptions string missing the concrete dollar figure ${expected_hs_loss:,.0f}")
        ok = False
    if ok:
        print(f"PASS [texas.py: cap_was_active] cap_was_active=True, hs_cap_loss=${tx_result['hs_cap_loss']:,} "
              f"(approximated), assumptions line: {cap_assumption!r}")
    else:
        all_ok = False

    # Regression: real hs_cap_loss present -- must use it as-is, not the
    # approximation, and cap_was_active must still correctly fire.
    _tx_current_yr_row_real = dict(_tx_current_yr_row, hs_cap_loss=50000)
    tx_result_real = _texas.estimate_post_acquisition(
        _tx_parcel, _tx_current_yr_row_real, _tx_entities,
        purchase_price=600000, buyer_status="owner_occupant",
    )
    if (tx_result_real.get("cap_was_active") is True
            and tx_result_real.get("hs_cap_loss") == 50000
            and tx_result_real.get("hs_cap_loss_is_approx") is False):
        print(f"PASS [texas.py: real hs_cap_loss unchanged] hs_cap_loss=$50,000 (real figure), "
              f"is_approx=False — unaffected by the fix")
    else:
        print(f"FAIL [texas.py: real hs_cap_loss unchanged] expected real figure ($50,000, is_approx=False), "
              f"got hs_cap_loss={tx_result_real.get('hs_cap_loss')!r} is_approx={tx_result_real.get('hs_cap_loss_is_approx')!r}")
        all_ok = False

    # Regression: no homestead exemption at all -- cap_was_active must stay
    # False (confirms the fix didn't become an always-True bug).
    _tx_current_yr_row_none = {
        "market_value": 800000, "assessed_value": 800000, "taxable_value": 800000,
        "hs_cap_loss": None, "exemption_codes": None,
    }
    tx_result_none = _texas.estimate_post_acquisition(
        _tx_parcel, _tx_current_yr_row_none, _tx_entities,
        purchase_price=850000, buyer_status="owner_occupant",
    )
    if tx_result_none.get("cap_was_active") is False and tx_result_none.get("seller_has_homestead") is False:
        print(f"PASS [texas.py: no homestead exemption] cap_was_active correctly False "
              f"(no exemption_codes, assessed == market)")
    else:
        print(f"FAIL [texas.py: no homestead exemption] cap_was_active unexpectedly "
              f"{tx_result_none.get('cap_was_active')!r} with no exemption on file")
        all_ok = False

    # ── compute_metrics.py's cap_step_up_exposure / cap_expiry_signal SQL
    # (Issue 2, "Homestead-Cap Data Integrity: Full Fix Set" Cowork brief,
    # July 2026 -- replaces risk_homestead_cap_expiry) ─────────────────────
    # Pure SQL UPDATEs, no live DB to run them against in this sandbox --
    # source-level regression guards (catch an accidental revert) plus
    # direct Python re-implementations of each WHERE clause's boolean
    # logic, run against real/realistic data so the LOGIC itself is
    # verified, not just its presence in the file. Diego's own live DB run
    # is still the only way to confirm the real row-count populations.
    print()
    print("── compute_metrics.py cap_step_up_exposure / cap_expiry_signal: SQL logic check ──")
    _cm_src = open(os.path.join(os.path.dirname(__file__), "loaders", "compute_metrics.py")).read()
    if "SET cap_step_up_exposure = TRUE" in _cm_src and ">= 0.22" in _cm_src:
        print("PASS [compute_metrics.py source] cap_step_up_exposure UPDATE + 22% threshold present")
    else:
        print("FAIL [compute_metrics.py source] expected cap_step_up_exposure UPDATE/threshold not found -- possible revert")
        all_ok = False
    if "SET cap_expiry_signal = TRUE" in _cm_src and "NOT LIKE '%HS%'" in _cm_src:
        print("PASS [compute_metrics.py source] cap_expiry_signal UPDATE present")
    else:
        print("FAIL [compute_metrics.py source] expected cap_expiry_signal UPDATE not found -- possible revert")
        all_ok = False

    def _cap_step_up_exposure(state_cd1, tax_year, market_value, assessed_value,
                               exemption_codes, effective_rate=0.02):
        """Direct re-implementation of the cap_step_up_exposure WHERE clause."""
        if not (state_cd1 or "").startswith("A"):
            return False
        if tax_year != 2025:
            return False
        if "HS" not in {c.strip().upper() for c in (exemption_codes or "").replace(";", ",").split(",")}:
            return False
        if not (market_value and market_value > 0) or assessed_value is None:
            return False
        if assessed_value >= market_value:
            return False
        rel_gap = (market_value - assessed_value) / market_value
        if rel_gap < 0.22:
            return False
        dollar_gap = (market_value - assessed_value) * effective_rate
        return dollar_gap >= 500

    def _cap_expiry_signal(state_cd1, tax_year_2025_exemption_codes, tax_year_2026_exemption_codes,
                            has_2026_row):
        """Direct re-implementation of the cap_expiry_signal WHERE clause."""
        if not (state_cd1 or "").startswith("A"):
            return False
        if "HS" not in {c.strip().upper() for c in (tax_year_2025_exemption_codes or "").replace(";", ",").split(",")}:
            return False
        if not has_2026_row:
            return True
        return "HS" not in {c.strip().upper() for c in (tax_year_2026_exemption_codes or "").replace(";", ",").split(",")}

    step_up_cases = [
        ("0100050414-style: A-prefix, 2025, HS, 58% gap, real dollar gap -> True",
         ("A", 2025, 552000, 232069, "HS,OV65"), True),
        ("no HS on file -> False regardless of gap",
         ("A", 2025, 552000, 232069, ""), False),
        ("gap present but below 22% relative threshold (10%, the retuned-away original) -> False",
         ("A", 2025, 600000, 540000, "HS"), False),
        ("non-residential (F-prefix) -> False regardless of gap",
         ("F", 2025, 600000, 232069, "HS"), False),
        ("wrong tax_year (2024) -> False (flag is 2025-scoped by design)",
         ("A", 2024, 552000, 232069, "HS"), False),
        ("assessed == market (no real gap at all) -> False",
         ("A", 2025, 552000, 552000, "HS"), False),
    ]
    for label, args, expect in step_up_cases:
        got = _cap_step_up_exposure(*args)
        status = "PASS" if got == expect else "FAIL"
        print(f"{status} [cap_step_up_exposure logic: {label}] got {got}, expected {expect}")
        if got != expect:
            all_ok = False

    expiry_cases = [
        ("HS on 2025, no 2026 row at all -> True (absent counts as gone)",
         ("A", "HS,OV65", None, False), True),
        ("HS on 2025, 2026 row exists but exemption_codes blank -> True",
         ("A", "HS,OV65", "", True), True),
        ("HS on 2025, 2026 still has HS -> False (protection intact)",
         ("A", "HS", "HS", True), False),
        ("No HS on 2025 at all -> False (nothing to lose)",
         ("A", "", "HS", True), False),
        ("non-residential (F-prefix) -> False regardless",
         ("F", "HS", None, False), False),
    ]
    for label, args, expect in expiry_cases:
        got = _cap_expiry_signal(*args)
        status = "PASS" if got == expect else "FAIL"
        print(f"{status} [cap_expiry_signal logic: {label}] got {got}, expected {expect}")
        if got != expect:
            all_ok = False

    # Sane-band tripwire (Issue 2's explicit ask): a future count anomaly
    # like the original 404,355-row surprise should fail a check
    # automatically instead of surfacing during a manual review. Can't
    # check a live population in this sandbox -- this asserts the
    # THRESHOLD CONSTANTS themselves stay inside the declared band (22%
    # relative / $500 dollar), so an accidental edit that widens them back
    # toward "any assessed<market row" is caught here, and documents the
    # expected live flag-rate band for Diego's own post-run check.
    import re as _re
    _thresh_m = _re.search(r">=\s*0\.(\d+)\b", _cm_src)
    _declared_band_ok = bool(_thresh_m) and 0.15 <= float(f"0.{_thresh_m.group(1)}") <= 0.40
    print(f"{'PASS' if _declared_band_ok else 'FAIL'} [cap_step_up_exposure sane-band tripwire] "
          f"relative threshold {'in' if _declared_band_ok else 'OUTSIDE'} declared 15%-40% band "
          f"(top-quartile-ish; live check: cap_step_up_exposure should flag ~5%-40% of the HS "
          f"population -- tens of thousands, not hundreds of thousands, per Diego's brief)")
    if not _declared_band_ok:
        all_ok = False

    # ── data_coverage.py manifest checks (Cross-cutting deliverable,
    # "Homestead-Cap Data Integrity: Full Fix Set" Cowork brief, July 2026)
    # ─────────────────────────────────────────────────────────────────────
    # Two checks, as scoped by the brief:
    #   1. Data-side band assertion -- the manifest's SEEDED numbers (from
    #      Diego's live queries, not re-derived here) stay inside sane
    #      declared bands, so an accidental hand-edit that corrupts a
    #      number is caught here rather than silently trusted by every
    #      future consumer of this manifest.
    #   2. Code-side grep lint for accessor-only reads -- generalizes the
    #      one-off "grep -rn hs_cap_loss" sweep that found and fixed 5
    #      instances of the same bug this round (property.html line ~3203,
    #      compare.html's Cap Loss row, plus the 3 already fixed earlier
    #      this project) into a permanent regression check: no file outside
    #      the field's own accessor/derivation code may read hs_cap_loss as
    #      a raw truthiness/comparison ("hs_cap_loss > 0" or bare
    #      "hs_cap_loss and ...") given it is CONFIRMED structurally 0.0%
    #      populated for 2025-2026 (see data_coverage.py).
    print()
    print("── data_coverage.py manifest: band assertion + accessor-only-read lint ──")
    import data_coverage as _dc

    # 1. Data-side band assertion. Bands, not exact-value re-derivation --
    # this sandbox has no live DB to independently confirm Diego's exact
    # percentages, only to notice if a future edit pushes a seeded number
    # outside the range he actually confirmed.
    _band_checks = [
        ("hs_cap_loss 2021 (91.1% confirmed)", _dc.HS_CAP_LOSS_COVERAGE[2021], 0.85, 0.95),
        ("hs_cap_loss 2022 (99.9% confirmed)", _dc.HS_CAP_LOSS_COVERAGE[2022], 0.98, 1.00),
        ("hs_cap_loss 2023 (99.9% confirmed)", _dc.HS_CAP_LOSS_COVERAGE[2023], 0.98, 1.00),
        ("hs_cap_loss 2024 (99.9% confirmed)", _dc.HS_CAP_LOSS_COVERAGE[2024], 0.98, 1.00),
        ("hs_cap_loss 2025 (0.0% confirmed -- always-false zone)", _dc.HS_CAP_LOSS_COVERAGE[2025], 0.0, 0.0),
        ("hs_cap_loss 2026 (0.0% confirmed -- always-false zone)", _dc.HS_CAP_LOSS_COVERAGE[2026], 0.0, 0.0),
        ("exemption_codes, all years (46.5%-55.1% confirmed band)",
         min(_dc.EXEMPTION_CODES_COVERAGE.values()), 0.40, None),
        ("exemption_codes, all years (46.5%-55.1% confirmed band)",
         max(_dc.EXEMPTION_CODES_COVERAGE.values()), None, 0.60),
    ]
    for label, val, lo, hi in _band_checks:
        ok = (lo is None or val >= lo) and (hi is None or val <= hi)
        status = "PASS" if ok else "FAIL"
        band_str = f"[{lo if lo is not None else '-inf'}, {hi if hi is not None else '+inf'}]"
        print(f"{status} [coverage band: {label}] {val:.3f} in declared band {band_str}")
        if not ok:
            all_ok = False
    if _dc.STATE_CD1_SCOPE == "AJR-only":
        print("PASS [coverage: state_cd1 scope] confirmed 'AJR-only' as declared")
    else:
        print(f"FAIL [coverage: state_cd1 scope] expected 'AJR-only', found {_dc.STATE_CD1_SCOPE!r}")
        all_ok = False

    # 2. Code-side grep lint: no raw hs_cap_loss truthiness/comparison
    # outside the field's own accessor/derivation code and the manifest's
    # own docstring/comments. Allow-list is the files where a raw read is
    # the deliberate implementation of the accessor itself, or safe
    # data-pipeline/standalone-diagnostic code (not a template-facing
    # consumer that could mislead an end user). Comment text (Python "#"
    # lines, Jinja "{# ... #}" blocks) is stripped before matching -- a
    # first pass over this lint's own naive line-regex flagged 7 "hits"
    # that were, on inspection, all either historical/explanatory comments
    # referencing the pattern by name (app.py:464, app.py:5648,
    # compare.html:74, property.html:3305 -- documenting fixes already
    # made, not living bugs) or files outside the live app entirely
    # (task_staging/ -- untracked scratch from past sessions, confirmed via
    # `git ls-files task_staging` returning nothing, i.e. not part of this
    # repo). Comment-stripping + the task_staging exclusion below replace
    # that naive first pass.
    _lint_allowlist = {
        "tax_logic/texas.py",          # cap_was_active()'s own accessor logic
        "loaders/compute_metrics.py",  # scoped SQL pass, not a template-facing truthiness check
        "data_coverage.py",            # this manifest's own documentation
        "verify_property_html_render.py",  # this harness file itself
        "KNOWN_LIMITATIONS.md",
        "review_check.py",  # standalone live-DB diagnostic Diego runs manually (needs psycopg2 + a
                             # real connection, not importable by the app) -- scans ALL years by
                             # design to find real historical cap instances; per data_coverage.py's
                             # confirmed 0.0% 2025/2026 population this naturally only ever surfaces
                             # genuine 2021-2024 AJR rows, so it isn't the "current year" bug pattern
                             # this lint exists to catch, and it never renders to an end user.
    }

    def _strip_comments_keep_lines(src, is_html):
        """Blank out comment interiors while preserving newlines (so line
        numbers in matches stay accurate) -- Python '#' comments for .py,
        Jinja '{# ... #}' blocks for .html. A regex match inside a comment
        is documentation, not a live code path."""
        if is_html:
            def _blank(m):
                return "".join(c if c == "\n" else " " for c in m.group(0))
            return _re.sub(r"\{#.*?#\}", _blank, src, flags=_re.DOTALL)
        else:
            out_lines = []
            for line in src.splitlines():
                stripped = line.lstrip()
                out_lines.append("" if stripped.startswith("#") else line)
            return "\n".join(out_lines)

    _lint_pattern = _re.compile(r"hs_cap_loss\s*(>\s*0|and\b)")
    _lint_hits = []
    for _root, _dirs, _files in os.walk(os.path.dirname(__file__) or "."):
        _dirs[:] = [d for d in _dirs
                    if d not in (".git", "__pycache__", "node_modules", "task_staging")]
        for _fn in _files:
            if not (_fn.endswith(".py") or _fn.endswith(".html")):
                continue
            _fpath = os.path.join(_root, _fn)
            _relpath = os.path.relpath(_fpath, os.path.dirname(__file__) or ".")
            if _relpath in _lint_allowlist:
                continue
            try:
                _src = open(_fpath, encoding="utf-8").read()
            except (UnicodeDecodeError, IsADirectoryError):
                continue
            _clean_src = _strip_comments_keep_lines(_src, _fn.endswith(".html"))
            for _lineno, _line in enumerate(_clean_src.splitlines(), start=1):
                if _lint_pattern.search(_line):
                    _lint_hits.append(f"{_relpath}:{_lineno}: {_line.strip()}")
    if not _lint_hits:
        print("PASS [accessor-only-read lint: hs_cap_loss] no raw truthiness/comparison reads found outside allow-listed accessor code / comments")
    else:
        print(f"FAIL [accessor-only-read lint: hs_cap_loss] {len(_lint_hits)} raw read(s) found outside allow-listed accessor code:")
        for hit in _lint_hits:
            print(f"       {hit}")
        all_ok = False

    # ── Template render check: the "Homestead Cap History" card's new
    # hs_history_is_approx branch (property.html, Investor mode only --
    # same AST-confirmed structural placement as the confidence-tiering
    # badges/Value History table checks above). Confirms the template
    # actually renders the new copy correctly (no leaked delimiters, correct
    # figures, honest "not AJR data" framing) rather than only checking the
    # Python-side data that feeds it. #}
    print()
    print("── Template render: Homestead Cap History card, exemption-code-approx branch ──")
    _hs_approx_ctx = base_context("investor", with_waterfall=True, residential=True)
    _hs_approx_ctx["insights"]["hs_history_loss"]      = 324724
    _hs_approx_ctx["insights"]["hs_history_year"]       = 2026
    _hs_approx_ctx["insights"]["hs_history_pct"]        = 56.0
    _hs_approx_ctx["insights"]["hs_history_is_approx"]  = True
    try:
        html = tmpl.render(**_hs_approx_ctx)
    except Exception as e:
        print(f"FAIL [Homestead Cap History: exemption-code-approx render] render raised {type(e).__name__}: {e}")
        all_ok = False
    else:
        ok = check_no_leaked_delimiters(html, "Homestead Cap History: exemption-code-approx render")
        if "$324,724" not in html:
            print("FAIL [Homestead Cap History: exemption-code-approx render] expected dollar figure $324,724 not found")
            ok = False
        if "no 2021–2024\n          AJR-recorded cap-loss figure" not in html and "no 2021–2024" not in html:
            print("FAIL [Homestead Cap History: exemption-code-approx render] expected honest 'no AJR data' framing not found")
            ok = False
        if "recorded in AJR data as recently as" in html:
            print("FAIL [Homestead Cap History: exemption-code-approx render] approx case incorrectly used the real-AJR-data copy")
            ok = False
        if ok:
            print("PASS [Homestead Cap History: exemption-code-approx render] renders correctly, honest AJR-vs-approximation framing")
        else:
            all_ok = False

    # ── Confidence-tiering fix (July 2026 Cowork brief, build phase) ───────
    # Direct unit assertions against the real _row_confidence() body itself
    # (loaded from app.py via _load_real_app_functions() above) -- cheapest,
    # most precise check of the actual tiering logic, independent of any
    # template wiring.
    print()
    print("── _row_confidence() direct unit assertions ──")
    unit_cases = [
        ("cert_202x, assessed <= market -> verified", ("cert_2022", 370000, 410000), "verified"),
        ("ajr_202x, assessed <= market -> verified", ("ajr_2023", 395000, 440000), "verified"),
        ("cert_202x, assessed > market -> partial", ("cert_2023", 450000, 440000), "partial"),
        ("ajr_202x, assessed > market -> partial", ("ajr_2021", 355000, 350000), "partial"),
        ("literal 'certified', no anomaly -> verified", ("certified", 430000, 480000), "verified"),
        ("'preliminary' unaffected by anomaly -> preliminary", ("preliminary", 999999, 1), "preliminary"),
        ("unrecognized data_source -> partial (fallback NOT loosened)", ("some_future_string", 100, 200), "partial"),
        ("legacy NULL data_source -> partial (fallback NOT loosened)", (None, 100, 200), "partial"),
    ]
    for case_label, args, expect in unit_cases:
        got = _row_confidence(*args)
        if got == expect:
            print(f"PASS [{case_label}] _row_confidence{args} == {expect!r}")
        else:
            print(f"FAIL [{case_label}] _row_confidence{args} == {got!r}, expected {expect!r}")
            all_ok = False

    # Full-render regression scenarios -- the 3 Diego named explicitly:
    # a cert_202x row with no anomaly (Verified), an ajr_202x row with no
    # anomaly (Verified), and a row that trips the AV>MV check (stays
    # Partial, with the anomaly icon shown). Each checks the Documents &
    # Sources panel's AJR row (the aggregate 2021-2024 tier, called from
    # both modes' branches) plus mode-specific surfaces:
    #
    #   - The real, COMPUTED top "Property-level confidence badges" block
    #     ("Appraisal: 2025 Certified"/Partial/etc, property.html
    #     ~line 1441-1451, the Jinja mirror this brief named directly) only
    #     exists in INVESTOR mode -- confirmed via jinja2 AST inspection
    #     (env.parse(), top-level If node at line 371): that block sits at
    #     line 1441, inside the mode=='homeowner' If's ELSE branch (lines
    #     1317-4070). Homeowner mode has its OWN top badge instead -- a
    #     hardcoded, unconditional "Appraisal Certified" string (line 629,
    #     inside the homeowner IF body, lines 371-1290) that does NOT read
    #     current.data_source or the AV>MV check at all. That's a separate,
    #     pre-existing finding (flagged to Diego in this round's report, not
    #     silently changed -- it's about 2025 specifically, not the
    #     2021-2024/AJR years this brief scoped, and current.data_source is
    #     documented elsewhere as "should only ever legitimately be
    #     'certified'" for the real 2025 row in practice). So expect_top=None
    #     means "skip this check, not applicable in this mode."
    #
    #   - The Value History table's per-row "!" anomaly icon (~line 3167-
    #     3193) is ALSO Investor-mode only, same ELSE-branch reasoning --
    #     Homeowner mode has no equivalent full appraisal-detail table (its
    #     "Your Tax Rate & Bill" table, line ~1204, is billing-only, no
    #     per-row appraisal anomaly icon). expect_anomaly_years=None means
    #     the same "skip, not applicable" as expect_top=None above.
    print()
    print("── Full-render confidence-tiering scenarios ──")
    confidence_scenarios = [
        ("confidence: cert_202x, no anomaly (investor)",
         confidence_tier_context("investor", "cert", anomaly_year=None),
         "Appraisal: 2025 Certified", "Verified", None, set()),
        ("confidence: ajr_202x, no anomaly (homeowner)",
         confidence_tier_context("homeowner", "ajr", anomaly_year=None),
         None, "Verified", None, None),
        ("confidence: cert_202x, 2023 trips AV>MV (investor)",
         confidence_tier_context("investor", "cert", anomaly_year=2023),
         "Appraisal: 2025 Certified", "Partial", "2023 flagged", {2023}),
    ]
    for label, ctx, expect_top, expect_ajr_badge, expect_coverage_substr, expect_anomaly_years in confidence_scenarios:
        try:
            html = tmpl.render(**ctx)
        except Exception as e:
            print(f"FAIL [{label}] render raised {type(e).__name__}: {e}")
            all_ok = False
            continue

        ok = check_no_leaked_delimiters(html, label)
        if expect_top is not None:
            ok = check_top_appraisal_badge(html, label, expect_top) and ok
        ok = check_doc_sources_ajr_badge(html, label, expect_ajr_badge, expect_coverage_substr) and ok
        if expect_anomaly_years is not None:
            ok = check_value_history_anomaly_icons(html, label, expect_anomaly_years) and ok

        if ok:
            print(f"PASS [{label}]")
        else:
            all_ok = False

    # Delinquency Data Freshness fix (July 2026, per Diego's Cowork brief):
    # a real delinquent fixture (mirrors 0100030804's actual tax_delinquent
    # row: $91,429.42 total_due, first delinquent 2014, cause number
    # GN26003081 -- confirmed against the loaded TaxDelqOpenData.csv itself,
    # not invented) plus the no-delinquent default, in both modes -- 3 of
    # the 4 combinations that matter: Investor+delinquent (existing card,
    # now with the date), Homeowner+delinquent (brand new card), and
    # Investor+no-delinquent (confirms the panel still correctly disappears,
    # unchanged regression). Homeowner+no-delinquent is implicitly covered
    # by every earlier homeowner scenario above (all use delinquent=None
    # and none of them ever showed delinquency text).
    print()
    print("── Delinquency Data Freshness scenarios ──")
    _delinquent_fixture = {
        "total_due": 91429.42,
        "first_delinquent_yr": 2014,
        "cause_number": "GN26003081",
    }
    delinquency_scenarios = [
        ("delinquency: investor, real balance (0100030804-style)",
         base_context("investor", True, True, delinquent=_delinquent_fixture),
         True, "June 20, 2026", 91429.42),
        ("delinquency: homeowner, real balance (0100030804-style, new card)",
         base_context("homeowner", True, True, delinquent=_delinquent_fixture),
         True, "June 20, 2026", 91429.42),
        ("delinquency: investor, no balance (panel absent, unchanged)",
         base_context("investor", True, True, delinquent=None),
         False, None, None),
    ]
    for label, ctx, expect_present, expect_date_str, expect_total_due in delinquency_scenarios:
        try:
            html = tmpl.render(**ctx)
        except Exception as e:
            print(f"FAIL [{label}] render raised {type(e).__name__}: {e}")
            all_ok = False
            continue

        ok = check_no_leaked_delimiters(html, label)
        ok = check_delinquency_as_of_date(html, label, expect_present, expect_date_str, expect_total_due) and ok

        if ok:
            print(f"PASS [{label}]")
        else:
            all_ok = False

    # Taxable Value KPI card (July 2026, per Diego's Cowork brief --
    # county_benchmark / Est. 2026 Total Tax investigation round). base_context()'s
    # own default 2025 fixture (mv25=480000, tv25=410000) already has a real gap
    # (~14.6% -> "15% below market"), exercising that branch by default; the
    # default 2026 fixture has taxable_value=None (no gap possible), so one
    # scenario below overrides it to a real value to exercise the 2026 card's
    # "value present, real gap" branch too, and a second scenario leaves it None
    # to confirm the fallback branch still renders cleanly (no exception, no
    # leaked delimiter) rather than only ever testing the happy path.
    print()
    print("── Taxable Value KPI card scenarios ──")

    def _tv26_override_ctx(mode):
        ctx = base_context(mode, with_waterfall=True, residential=True)
        # July 2026 Issue 1 fix (central 2026 baseline / derive_2026_baseline()):
        # the card now reads current_2026.est_taxable_2026 (TCAD's own figure
        # when assessed_2026 < market_2026, i.e. basis_2026 == 'tcad_capped' --
        # true by default in this fixture since assessed_2026=452000 <
        # market_2026=505000), NOT the raw taxable_value field. Overriding raw
        # taxable_value alone (the pre-Issue-1 fixture shape) no longer moves
        # what renders -- est_taxable_2026 must be set directly to exercise
        # the "value present, real gap" branch.
        ctx["current_2026"]["taxable_value"] = 401500
        ctx["current_2026"]["est_taxable_2026"] = 401500  # basis_2026 stays 'tcad_capped' (default)
        return ctx

    taxable_kpi_scenarios = [
        # Gap percentages are the real values ("{:.0f}%".format(...)) --
        # 2025: (480000-410000)/480000 = 14.58% -> "15%"; 2026 override:
        # (505000-401500)/505000 = 20.50% -> "20%".
        ("taxable KPI: investor, both years have real gaps",
         _tv26_override_ctx("investor"), "investor", 410000, 401500, 15, 20),
        ("taxable KPI: homeowner, both years have real gaps",
         _tv26_override_ctx("homeowner"), "homeowner", 410000, 401500, 15, 20),
        ("taxable KPI: investor, 2026 taxable value not yet available (fallback)",
         base_context("investor", True, True), "investor", 410000, None, 15, None),
    ]
    for label, ctx, mode, exp_tv25, exp_tv26, exp_gap25, exp_gap26 in taxable_kpi_scenarios:
        try:
            html = tmpl.render(**ctx)
        except Exception as e:
            print(f"FAIL [{label}] render raised {type(e).__name__}: {e}")
            all_ok = False
            continue

        ok = check_no_leaked_delimiters(html, label)
        ok = check_taxable_value_kpi(html, label, mode, exp_tv25, exp_tv26, exp_gap25, exp_gap26) and ok

        if ok:
            print(f"PASS [{label}]")
        else:
            all_ok = False

    # ── quarantine_contamination.py: tightened contamination assertion
    # (Cowork brief "Tighten the Contamination Assertion, Begin Class A
    # Resolution", July 2026) ────────────────────────────────────────────
    # This module imports psycopg2 (no live DB in this sandbox), so it
    # can't be imported and exercised directly here the way
    # data_coverage.py was -- source-level checks instead, same pattern as
    # this file's other loaders/compute_metrics.py source-string guards.
    print()
    print("── quarantine_contamination.py: tightened assertion + Class A allowlist ──")
    _qc_src = open(os.path.join(os.path.dirname(__file__), "loaders", "quarantine_contamination.py")).read()
    if "geo_id != ALL(%s)" in _qc_src and "CLASS_A_TRACKED_EXCEPTIONS" in _qc_src:
        print("PASS [quarantine_contamination.py source] verify_year_bounds() excludes the named allowlist, not a bare count()")
    else:
        print("FAIL [quarantine_contamination.py source] expected allowlist-scoped assertion not found -- possible revert to bare 'count = 0'")
        all_ok = False
    if '"AJR385736"' in _qc_src:
        print("PASS [quarantine_contamination.py source] AJR385736 present in CLASS_A_TRACKED_EXCEPTIONS with its investigation finding")
    else:
        print("FAIL [quarantine_contamination.py source] AJR385736 missing from the tracked allowlist")
        all_ok = False
    # The allowlist must never silently grow into a bare, unexplained ID
    # dump -- every entry needs SOME resolution-status prose nearby. Cheap
    # proxy check: the comment block immediately above the list must be
    # substantially longer than "geo_id: reason" one-liners would produce,
    # confirmed by requiring the specific investigation-finding language
    # (not just the ID) to be present.
    if "ATMOS ENERGY" in _qc_src and "Travis County-owned exempt parcel" in _qc_src:
        print("PASS [quarantine_contamination.py source] AJR385736's real-owner finding (Atmos Energy, not county-exempt) documented inline")
    else:
        print("FAIL [quarantine_contamination.py source] AJR385736's investigation finding not documented -- allowlist entry looks unexplained")
        all_ok = False
    if "--emit-class-a" in _qc_src:
        print("PASS [quarantine_contamination.py source] --emit-class-a helper present for completing the remaining 11 entries")
    else:
        print("FAIL [quarantine_contamination.py source] --emit-class-a helper missing")
        all_ok = False

    # ── Cowork brief "Restore the 12 Class A Parcels from Quarantine, and
    # Complete the Tracked Exceptions List" (July 2026) ─────────────────────
    print()
    print("── quarantine_contamination.py: 12-parcel restore + quarantine-state invariance ──")
    _all_12 = [
        "AJR385736", "0164800619", "0202500315", "0210110712", "0215050312",
        "0215050419", "0242600206", "0242700249", "0244071202", "0336100301",
        "0339110404", "0339110406",
    ]
    _missing = [g for g in _all_12 if f'"{g}"' not in _qc_src]
    if not _missing:
        print(f"PASS [quarantine_contamination.py source] all {len(_all_12)} Class A geo_ids present in CLASS_A_TRACKED_EXCEPTIONS")
    else:
        print(f"FAIL [quarantine_contamination.py source] {len(_missing)} geo_id(s) missing from CLASS_A_TRACKED_EXCEPTIONS: {_missing}")
        all_ok = False
    # 12 (11 list-entry inline notes + 1 in the block comment's own prose
    # explaining what the placeholder means) -- not 11 -- is the correct
    # expected count here.
    if _qc_src.count("pending individual investigation") == 12:
        print("PASS [quarantine_contamination.py source] the 11 unresolved entries each carry a 'pending individual investigation' note, not a guessed reason")
    else:
        print(f"FAIL [quarantine_contamination.py source] expected 12 occurrences (11 list entries + 1 explanatory comment) of 'pending individual investigation', found {_qc_src.count('pending individual investigation')}")
        all_ok = False

    # Quarantine-state-invariance fix: _INVESTIGATE_SQL (which --emit-class-a
    # and --investigate both run) used to read contamination evidence ONLY
    # from tax_billing -- meaning a parcel whose contaminated rows already
    # moved to tax_billing_quarantine would silently vanish from the orphan
    # detection entirely (zero rows left in tax_billing to trigger it). This
    # is exactly the state the 12 Class A parcels were actually in when this
    # brief started (swept into quarantine by an earlier over-broad
    # --include-orphans run) -- confirmed by tracing the CTE logic by hand,
    # not by a live query this sandbox can't run.
    if "UNION\n    SELECT geo_id FROM tax_billing_quarantine" in _qc_src:
        print("PASS [quarantine_contamination.py source] _INVESTIGATE_SQL's contaminated_geo CTE checks tax_billing_quarantine too (state-invariant)")
    else:
        print("FAIL [quarantine_contamination.py source] _INVESTIGATE_SQL appears to read tax_billing only -- would silently miss already-quarantined orphans")
        all_ok = False

    # restore_class_a() presence + refuses untracked geo_ids (the one real
    # safety property this function needs: it must never restore something
    # verify_year_bounds() doesn't already know to exclude).
    if "def restore_class_a(" in _qc_src and "REFUSING" in _qc_src:
        print("PASS [quarantine_contamination.py source] restore_class_a() present and refuses untracked geo_ids")
    else:
        print("FAIL [quarantine_contamination.py source] restore_class_a() missing or doesn't guard against untracked geo_ids")
        all_ok = False
    if "--restore-class-a" in _qc_src:
        print("PASS [quarantine_contamination.py source] --restore-class-a wired into the CLI")
    else:
        print("FAIL [quarantine_contamination.py source] --restore-class-a CLI flag missing")
        all_ok = False

    # Structural column-parity check: the quarantine INSERT (run()) and the
    # restore INSERT (restore_class_a()) must move the exact same 13 real
    # tax_billing columns, in the exact same order, in both directions --
    # a silent column-order drift between the two would insert values into
    # the wrong columns without raising any error (all the affected columns
    # are compatible types: VARCHAR/NUMERIC/BOOLEAN/SMALLINT/TEXT). Checked
    # by normalizing whitespace (the real source line-wraps these lists
    # differently in a few places) and requiring the exact 13-name sequence
    # to appear, unbroken, at least 4 times: RETURNING + SELECT in both
    # run()'s quarantine path and restore_class_a()'s restore path.
    _expected_cols = ("geo_id, tax_year, billing_num, owner_name, total_tax, total_paid, "
                       "total_due, is_delinquent, first_delinquent_yr, cause_number, "
                       "exemption_codes, data_source, confidence_level")
    _normalized = _re.sub(r"\s+", " ", _qc_src)
    _n_occurrences = _normalized.count(_expected_cols)
    if _n_occurrences >= 4:
        print(f"PASS [quarantine_contamination.py source] {_n_occurrences} occurrences of the exact 13-column order found (quarantine + restore, both directions)")
    else:
        print(f"FAIL [quarantine_contamination.py source] expected >=4 occurrences of the exact 13-column order (quarantine RETURNING/SELECT + restore RETURNING/SELECT), found {_n_occurrences} -- possible column-order drift between quarantine and restore paths")
        all_ok = False

    # ── Cowork brief "Reconcile a Discrepancy in --verify's Reporting"
    # (July 2026): verify_year_bounds()'s primary contamination count used
    # to be computed with a DIFFERENT condition ("tax_year NOT BETWEEN 1990
    # AND current_year+1") than _CONTAMINATION_WHERE ("tax_year < 2021 OR
    # tax_year = 9999") -- the two disagree on the entire 1990-2020 range,
    # which is exactly why restoring the 12 Class A parcels' 37 contaminated
    # rows reported "1" instead of "37" (36 of them had a tax_year somewhere
    # in 1990-2020, invisible to the old, looser condition). Fixed by
    # reusing _CONTAMINATION_WHERE directly for the primary count, with the
    # generic bound kept only as a clearly separate, distinctly-labeled
    # secondary check. Regression guard: the primary ASSERTION line must
    # read _CONTAMINATION_WHERE's own text, not a hand-typed "NOT BETWEEN"
    # duplicate of it.
    print()
    print("── quarantine_contamination.py: verify_year_bounds() contamination-definition fix ──")
    if "SELECT count(*) FROM tax_billing WHERE {_CONTAMINATION_WHERE} AND geo_id != ALL(%s)" in _qc_src:
        print("PASS [quarantine_contamination.py source] primary contamination count reuses _CONTAMINATION_WHERE (single source of truth), not a separately-typed-out bound")
    else:
        print("FAIL [quarantine_contamination.py source] primary contamination count no longer reuses _CONTAMINATION_WHERE -- possible regression back to the 1-vs-37 mismatch bug")
        all_ok = False
    if "SEPARATE CHECK (generic sanity bound" in _qc_src:
        print("PASS [quarantine_contamination.py source] generic sanity bound kept as a distinctly-labeled secondary check, not merged with the primary count")
    else:
        print("FAIL [quarantine_contamination.py source] generic sanity bound check missing or no longer distinctly labeled")
        all_ok = False

    # ── Cowork brief "Version Display + Single Source of Truth" (July 2026):
    # base.html's footer now reads config.VERSION (injected as a Jinja global
    # in make_env(), mirroring app.py's real inject_mode() context processor).
    # Real render check -- confirms the actual VERSION file's contents reach
    # the actual footer markup, not just that config.VERSION is importable.
    print()
    print("── base.html: version display ──")
    _version_ctx = base_context()
    _version_html = tmpl.render(**_version_ctx)
    _expected_version_str = f"v{_real_config.VERSION}"
    if _real_config.VERSION == "1.0.0" and _expected_version_str in _version_html:
        print(f"PASS [base.html footer] '{_expected_version_str}' found in rendered output, sourced from the real VERSION file (config.VERSION == '1.0.0')")
    else:
        print(f"FAIL [base.html footer] expected '{_expected_version_str}' in rendered output (VERSION file contents: {_real_config.VERSION!r}) -- not found, or VERSION file no longer reads '1.0.0'")
        all_ok = False

    # ── Cowork brief "Terms of Service, Privacy Policy, Disclaimer Page,
    # Beta Popup, Footer Notice" (July 2026): real renders of the three new
    # legal pages, the site-wide beta popup markup, and the footer additions.
    # These templates don't need property.html's elaborate mock context --
    # they extend base.html directly, so the only per-render context key
    # needed (beyond the url_for/request/config globals already registered)
    # is `mode`, which base.html's nav reads directly (not a Jinja global in
    # this harness, unlike production where inject_mode() supplies it).
    print()
    print("── /terms, /privacy, /disclaimer: real render + exact-text spot checks ──")

    def _render_simple(template_name):
        t = env.get_template(template_name)
        html = t.render(mode="homeowner")
        if "{%" in html or "#}" in html:
            print(f"FAIL [{template_name}] raw Jinja delimiter leaked into rendered output")
            return html, False
        return html, True

    _terms_html, _terms_ok = _render_simple("terms.html")
    all_ok = all_ok and _terms_ok
    _terms_expected_strings = [
        "Parcelytics — Terms of Service",
        "Last updated: Jul 19, 2026",
        "By accessing or using Parcelytics (\"the Service,\" \"we,\" \"us\"), you agree to",
        "Parcelytics is currently in beta.",
        "Parcelytics is not affiliated with, endorsed by, or officially",
        "connected to any government entity.",
        "TO THE MAXIMUM EXTENT PERMITTED BY LAW, PARCELYTICS AND ITS OPERATORS",
        "THE SERVICE IS PROVIDED WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR",
        "These Terms are governed by the laws of the State of Texas",
        "Questions about these Terms:",
        "parcelytics@gmail.com",
        "Use automated tools to scrape, crawl, or bulk-extract data from the Service beyond normal browsing use",
    ]
    _missing = [s for s in _terms_expected_strings if s not in _terms_html]
    if not _missing:
        print(f"PASS [terms.html] all {len(_terms_expected_strings)} exact-text spot-check strings found in rendered output")
    else:
        print(f"FAIL [terms.html] missing expected strings: {_missing}")
        all_ok = False

    _privacy_html, _privacy_ok = _render_simple("privacy.html")
    all_ok = all_ok and _privacy_ok
    _privacy_expected_strings = [
        "Parcelytics — Privacy Policy",
        "Last updated: Jul 19, 2026",
        "Parcelytics does not currently require an account to use the Service.",
        "Error and diagnostic data via Sentry (our error-monitoring provider)",
        "we do not send request bodies, headers, or other personally-identifying details to Sentry.",
        "We do not sell your data.",
        "Render (hosting and database infrastructure)",
        "Sentry (error monitoring)",
        "to remember",
        "that you've seen the beta/disclaimer notice so it isn't shown again.",
        "Our database is not publicly accessible and is restricted to authorized",
        "Parcelytics is not directed at children under 13",
        "Questions about this Privacy Policy:",
    ]
    _missing = [s for s in _privacy_expected_strings if s not in _privacy_html]
    if not _missing:
        print(f"PASS [privacy.html] all {len(_privacy_expected_strings)} exact-text spot-check strings found in rendered output")
    else:
        print(f"FAIL [privacy.html] missing expected strings: {_missing}")
        all_ok = False

    _disclaimer_html, _disclaimer_ok = _render_simple("disclaimer.html")
    all_ok = all_ok and _disclaimer_ok
    _disclaimer_expected_strings = [
        "Parcelytics — Disclaimer",
        "Last updated: Jul 19, 2026",
        "NOT INVESTMENT, TAX, OR LEGAL ADVICE",
        "Always consult a licensed",
        "DATA ACCURACY",
        "NOT AFFILIATED WITH ANY GOVERNMENT ENTITY",
        "For the full legal terms governing your use of this site, see our",
        'href="/terms"',
        'href="/privacy"',
    ]
    _missing = [s for s in _disclaimer_expected_strings if s not in _disclaimer_html]
    if not _missing:
        print(f"PASS [disclaimer.html] all {len(_disclaimer_expected_strings)} exact-text spot-check strings found in rendered output")
    else:
        print(f"FAIL [disclaimer.html] missing expected strings: {_missing}")
        all_ok = False

    # base.html itself (via any of the above renders, since they all extend
    # it): beta popup markup + footer additions.
    print()
    print("── base.html: beta popup markup + footer additions ──")
    _popup_checks = {
        'id="betaDisclaimerModal"': "modal element present",
        'data-bs-backdrop="static"': "backdrop-click dismissal disabled (only Continue/× dismiss, per brief)",
        'data-bs-keyboard="false"': "Escape-key dismissal disabled (only Continue/× dismiss, per brief)",
        "Parcelytics is in beta.": "modal title text",
        "We're still actively testing and refining this platform.": "modal body paragraph 1",
        "always verify anything important directly with the relevant": "modal body paragraph 2",
        "By clicking Continue, you agree to our": "modal body paragraph 3 (agreement line)",
        ">Continue<": "Continue button text",
        'class="btn-close" data-bs-dismiss="modal"': "close (×) icon, same dismiss mechanism as Continue",
        "beta-disclaimer.js": "popup JS included",
    }
    for needle, desc in _popup_checks.items():
        if needle in _terms_html:
            print(f"PASS [base.html popup] {desc}")
        else:
            print(f"FAIL [base.html popup] {desc} -- expected {needle!r} not found in rendered output")
            all_ok = False

    _footer_checks = {
        'href="/terms"': "footer links to Terms of Service",
        'href="/privacy"': "footer links to Privacy Policy",
        'href="/disclaimer"': "footer links to Disclaimer",
        "Parcelytics is not affiliated with any government entity.": "non-affiliation line present",
        "Not legal or tax advice": "pre-existing footer line untouched",
    }
    for needle, desc in _footer_checks.items():
        if needle in _terms_html:
            print(f"PASS [base.html footer] {desc}")
        else:
            print(f"FAIL [base.html footer] {desc} -- expected {needle!r} not found in rendered output")
            all_ok = False

    return all_ok


if __name__ == "__main__":
    ok = run()
    if not ok:
        print("\nOne or more render checks FAILED.")
        sys.exit(1)
    print("\nAll render checks passed.")
