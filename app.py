"""
Travis County Property Tax Platform — Flask Web Application
Phase 1: Parcel search + 5-year history + tax rate trends
"""
import os
import sys
import json
import re
import time
from io import BytesIO
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, g, abort, has_request_context
import psycopg2
import psycopg2.extras
import psycopg2.errors
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

sys.path.insert(0, os.path.dirname(__file__))
import config

from tax_logic.texas import estimate_post_acquisition as _tx_estimate
from tax_logic.texas import estimate_homestead_savings as _tx_hs_savings
from tax_logic.texas import derive_2026_baseline as _derive_2026_baseline
from tax_logic.classify import property_type_label, label_case_sql, label_sort_case_sql
from loaders.scrape_billing_history import (
    fetch_html, parse_receipts, upsert_billing_rows,
    HTTP_OK, HTTP_NOT_FOUND, HTTP_NETWORK_ERR,
    TARGET_YEARS as _BILLING_TARGET_YEARS,
)
from parcel_filters import CANONICAL_PARCEL_EXCL, CANONICAL_PARCEL_EXCL_BARE, peer_state_cd1_match_sql, exclude_non_real_property_gap_sql
import search_logic
# AGGPRECOMP-2 (Aug 2026): Market Snapshot taxonomy + view-scoping SQL-fragment
# builders, extracted to snapshot_taxonomy.py so loaders/refresh_snapshot_summary.py
# can import the exact same logic without importing this Flask module -- see
# that module's docstring for the full reasoning. Every name below is used at
# its original app.py call sites unchanged (county_snapshot(), _compute_
# snapshot_data(), snapshot_neighborhood(), parcel_list()'s _ptype_drill_where/
# _use_code_expr_for_view(), api_benchmark()/api_benchmark_meta()'s
# USE_CODE_LOOKUP reads).
from snapshot_taxonomy import (
    USE_CODE_LOOKUP, use_code_case_sql,
    SNAPSHOT_LAND_SIZE_TIERS, SNAPSHOT_AG_SIZE_TIERS, _size_tier_case_sql,
    _snapshot_taxonomy_sql, _SNAPSHOT_TAB_ORDER, _snapshot_taxonomy_sort_case_sql,
    _SNAPSHOT_SECTOR_VIEWS, _SNAPSHOT_VALID_VIEWS, _SNAPSHOT_VIEW_PROP_TYPE_LABEL,
    _snapshot_view_where, ptype_and_sort_case_for_view,
    _SNAPSHOT_VIEW_TAB_ORDER, _SNAPSHOT_TAB_BUTTON_LABEL, _SNAPSHOT_COVERAGE_LABELS,
)

# BILLING-DIAG-1: _BILLING_TARGET_YEARS used to be its own separately-
# maintained {2021, 2022, 2023, 2024} literal here -- confirmed identical in
# value/type to loaders/scrape_billing_history.py's own TARGET_YEARS, but
# two independent definitions of the same real constant was flagged in this
# brief as worth consolidating regardless of whether it was the actual root
# cause (it wasn't -- see the fix's own comment in api_billing() for the
# real cause). Now a single import (see the loaders.scrape_billing_history
# import block above) -- can't drift out of sync again.
_BILLING_SENTINEL_YEAR = 9999   # stored when portal returns no target-year data

# BILLING-DIAG-2: a real, distinctive marker string confirmed present in
# BILLING-DIAG-1's own direct inspection of a genuine, successful portal
# fetch (22,594 real chars of actual page content). Used to reject a
# same-shape-but-wrong HTTP 200 (e.g. a WAF/bot-detection interstitial page,
# which returns 200 with HTML but isn't the real portal) rather than
# trusting status == HTTP_OK alone -- see api_billing()'s own comment for
# the full reasoning.
_BILLING_PORTAL_MARKER = "Travis County Tax"

# TaxDelqOpenData.csv snapshot date (July 2026, per Diego's "Delinquency Data
# Freshness" Cowork brief). TaxDelqOpenData is a periodic export from the
# Travis County Tax Office, not a live feed -- a delinquent balance shown on
# this site can be (and, per Texas Tax Code Sec. 33.01, IS: 6% penalty + 1%
# interest starting in February, +1% interest every month thereafter,
# uncapped, plus a one-time collection penalty if referred to a delinquent
# tax attorney) already stale by some amount the moment it's loaded, since
# real accrual continues daily on the county's side between exports.
#
# Unlike AJR/Certified/Preliminary exports (whose date is embedded in their
# source folder name, e.g. PRELIM_2026_DIR's "..._06092026"), the raw
# TaxDelqOpenData.csv file/filename carries NO internal date field or
# dated-folder convention -- confirmed by inspecting the file's own header
# row (Account #, Last Tax Roll Year, ..., Total Due, ... -- no as-of/export-
# date column) and by grepping the loader (loaders/load_tax_current.py,
# load_delinquent()) and schema.sql's tax_delinquent table, neither of which
# stores or tracks a load/export date at all. The best REAL, non-guessed
# signal available is the file's own last-modified timestamp -- confirmed
# via `stat TaxDelqOpenData.csv` against the actual file currently loaded
# (same file whose row for 0100030804 was checked and matches exactly what
# Parcelytics shows: $91,429.42, first delinquent 2014): Modify time
# 2026-06-20 18:33:51 (local). Same-day sibling TaxCurOpenData.csv (18:14)
# supports this being one coherent snapshot pull, not an incidental touch.
#
# Hardcoded as a literal, following the exact same established convention
# PRELIM_2026_DIR's "June 9, 2026" already uses elsewhere in this file and
# in templates/property.html (line ~3104) -- this project has no
# infrastructure that stores an export date dynamically per load; it's a
# manually-confirmed value refreshed by hand whenever the loader is next run
# against a newer file. A single named constant here (rather than the
# Preliminary date's pattern of repeating the literal string at each call
# site) so the property page's two modes can't drift from each other or
# from a future PDF/other surface.
#
# Diego should confirm this against Travis County Tax Office's own stated
# "last updated" date for the file on their open-data portal if pinpoint
# precision matters -- file mtime in this environment reflects when the
# file was placed on disk here, which is a strong proxy but not guaranteed
# byte-identical to the county's own publish timestamp.
TAX_DELQ_EXPORT_DATE = date(2026, 6, 20)


# ── Investor insight generator ────────────────────────────────────────────────
def build_insights(parcel, history, entity_detail, delinquent):
    hist = sorted([r for r in history if r["market_value"]], key=lambda r: r["tax_year"])
    if not hist:
        return None

    earliest = hist[0]
    latest   = next((r for r in hist if r["tax_year"] == 2025), hist[-1])
    span     = latest["tax_year"] - earliest["tax_year"]

    out = {
        "earliest_year":   earliest["tax_year"],
        "latest_year":     latest["tax_year"],
        "earliest_market": earliest["market_value"],
        "latest_market":   latest["market_value"],
        "latest_assessed": latest["assessed_value"],
        "latest_taxable":  latest["taxable_value"],
        "span":            span,
    }

    # Appreciation
    if span > 0 and earliest["market_value"]:
        pct  = (latest["market_value"] - earliest["market_value"]) / earliest["market_value"] * 100
        cagr = ((latest["market_value"] / earliest["market_value"]) ** (1 / span) - 1) * 100
        out["value_change_pct"] = round(pct, 1)
        out["value_cagr"]       = round(cagr, 1)

    # Homestead cap — only applies to single-family residential (state_cd1 == 'A').
    # AJR carries non-zero hs_cap_loss for commercial/multi-family parcels but this
    # is bad source data — homestead exemptions cannot apply to those property types.
    # Using the narrowest safe interpretation: class 'A' only.
    sc = (parcel.get("state_cd1") or "").strip()
    is_residential_sfr = sc.startswith("A")

    hs_row = None
    if is_residential_sfr:
        hs_row = next(
            (r for r in reversed(hist) if r.get("hs_cap_loss") and r["hs_cap_loss"] > 0),
            None
        )
    if hs_row and latest["market_value"]:
        # Renamed to hs_history_* — this is AJR-based historical context only.
        # The primary "is the cap active right now?" signal is parcel_metrics.cap_step_up_exposure
        # (renamed from risk_homestead_cap_expiry -- Issue 2, "Homestead-Cap Data
        # Integrity" Cowork brief, July 2026 -- see compute_metrics.py's own comment)
        # (2025 Certified data). These keys feed the calm historical note in the Insight Report.
        out["hs_history_loss"]     = hs_row["hs_cap_loss"]
        out["hs_history_year"]     = hs_row["tax_year"]
        out["hs_history_pct"]      = round(hs_row["hs_cap_loss"] / latest["market_value"] * 100, 1)
        out["hs_history_is_approx"] = False
    elif is_residential_sfr:
        # Same OR-fix as build_projections()'s has_hs_cap (Cowork brief, "Fix
        # 6-Year Projection's Missing Homestead Cap for 2025+ Parcels"),
        # reused verbatim rather than reimplemented: hs_cap_loss is only ever
        # populated by the 2021-2024 AJR loader, never by 2025/2026 loaders,
        # so a parcel whose earliest history starts in 2025+ can never
        # satisfy hs_row above even with a real, active homestead exemption
        # on file right now. exemption_codes containing 'HS' is the
        # complementary signal for exactly that case.
        #
        # Unlike build_projections() (a pure boolean gate), this function
        # also needs a dollar figure + year to populate the historical note
        # -- hs_cap_loss itself has no fallback source, so the loss is
        # approximated as market_value - assessed_value for the latest year
        # carrying the 'HS' code. Same approximation Diego approved for
        # tax_logic/texas.py's cap_was_active fix (the same underlying
        # quantity the Value-vs-Taxable chart / homestead savings cap term
        # already compute elsewhere) -- applied here for consistency across
        # all three fixes rather than leaving this one silently still blank.
        # hs_history_is_approx=True marks it so the template can describe it
        # honestly as certified-year-derived, not AJR data.
        _hs_latest = next(
            (r for r in reversed(hist) if "HS" in (r.get("exemption_codes") or "").split(",")),
            None
        )
        if _hs_latest and _hs_latest.get("market_value") and _hs_latest.get("assessed_value") is not None:
            _approx_loss = max(0, _hs_latest["market_value"] - _hs_latest["assessed_value"])
            out["hs_history_loss"]      = _approx_loss
            out["hs_history_year"]      = _hs_latest["tax_year"]
            out["hs_history_pct"]       = round(_approx_loss / _hs_latest["market_value"] * 100, 1)
            out["hs_history_is_approx"] = True

    # Tax rates
    rate_2025 = sum(e["rate"] for e in entity_detail if e["rate"])
    rate_2024 = sum(e["rate_prev"] for e in entity_detail if e["rate_prev"])
    out["total_rate_2025"] = rate_2025
    out["total_rate_2024"] = rate_2024
    if rate_2024:
        out["rate_delta"] = round(rate_2025 - rate_2024, 6)
    out["entity_count"] = len([e for e in entity_detail if e["rate"]])

    # Estimated tax burden
    taxable = latest["taxable_value"] or latest["assessed_value"]
    if taxable and rate_2025:
        out["est_annual_tax"] = round(taxable * rate_2025 / 100)

    # Property type — classi_cd-first (Task 1): an apartment carrying a
    # multi-family improvement code is Multi-Family even when state_cd1 = 'A'.
    ptype = (parcel["prop_type_cd"] or "").strip()
    sc    = (parcel["state_cd1"] or "").strip()
    _label = property_type_label(parcel.get("classi_cd"), sc)
    if _label == "Residential":
        out["prop_class"] = "Single-family residential"
    elif _label == "Multi-Family":
        out["prop_class"] = "Multi-family residential"
    elif _label == "Commercial":
        out["prop_class"] = "Commercial"
    elif _label == "Agricultural":
        out["prop_class"] = "Agricultural"
    elif _label == "Land/Vacant":
        out["prop_class"] = "Land / Vacant"
    else:
        out["prop_class"] = sc or ptype or "Unknown"

    # Delinquency
    if delinquent and delinquent.get("total_due") and delinquent["total_due"] > 0:
        out["delinquent_amount"] = delinquent["total_due"]
        out["delinquent_since"]  = delinquent.get("first_delinquent_yr")

    return out


# ── Bill-Change Waterfall (July 2026, per Diego's outside-review brief) ─────
def build_bill_waterfall(history, entity_detail, entity_detail_prev,
                          cur_year, prior_year):
    """
    Decomposes the year-over-year change in total tax bill into three real,
    reconciling effects: value change, rate change, exemption change.

    INVESTIGATION / MATH DERIVATION (don't guess, don't approximate):
    Only two real facts are needed per taxing entity, for each of the two
    years being compared -- the entity's actual billed amount
    (tax_billing_entity.amount_due, real dollars) and its actual rate
    (county_tax_rate.rate, real, per $100 valuation, same convention already
    used by the COMPUTED_HIST_TAX_ENABLED block above: tax = taxable * rate/100).
    From those two real numbers we can back out the entity's own IMPLIED
    taxable value for that year:

        taxable_i(year) = amount_due_i(year) / rate_i(year) * 100

    This is deliberately NOT the shared parcel_tax_year.taxable_value column,
    because that column is computed against a single reference entity's
    exemption schedule ("assessed minus entity exemptions (TCO entity used)"
    per schema.sql) -- every other entity's real exemption can differ (e.g. a
    school district's mandatory homestead exemption is larger than a city's).
    Backing out taxable_i per entity from its own real amount_due/rate avoids
    that gap entirely and guarantees Σ amount_due_i == the real total_tax by
    construction.

    Chain (Laspeyres-style) decomposition per entity, priced so the two terms
    telescope EXACTLY to the entity's real delta (no residual, verified
    algebraically and numerically -- see /tmp/waterfall/verify_decomposition.py,
    both a synthetic multi-entity case with a genuine exemption change and a
    real-numbers pass for 0100030105):

        value_effect_i = (taxable_i(cur) - taxable_i(prior)) * rate_i(prior) / 100
        rate_effect_i  = taxable_i(cur) * (rate_i(cur) - rate_i(prior)) / 100
        value_effect_i + rate_effect_i == amount_due_i(cur) - amount_due_i(prior)   [exact]

    value_effect_i is then split into a "pure" value effect (the parcel-wide
    assessed_value change, which — unlike taxable_value — genuinely IS the
    same number for every entity in Texas) and an exemption effect (whatever
    is left over, i.e. the entity-specific change in its own exemption):

        pure_value_effect_i = (assessed(cur) - assessed(prior)) * rate_i(prior) / 100
        exemption_effect_i  = value_effect_i - pure_value_effect_i

    Summed across entities, pure_value_effect_i collapses to
    ΔAssessed * combined_rate(prior)/100 (ΔAssessed is entity-invariant), and
    the three totals still telescope exactly to the real aggregate delta.

    Only entities with real amount_due AND real rate on file for BOTH years
    are included (`incomplete` flags when any were skipped) -- this never
    fabricates a number for a gap in the data.

    Reconciliation: start_total/end_total below are the REAL tax_billing.total_tax
    figures already shown elsewhere on this page (KPI cards, Value History table),
    not the entity-sum -- so if any entity was skipped/incomplete, the visible
    "Other / Unmatched" gap makes that shortfall honest instead of silently
    forcing the three segments to sum to a total they didn't actually produce.
    """
    cur_row   = next((r for r in history if r["tax_year"] == cur_year), None)
    prior_row = next((r for r in history if r["tax_year"] == prior_year), None)
    if not cur_row or not prior_row:
        return None
    # Only build this on two years whose billing is genuinely verified —
    # same is_billing_verified flag already computed above, no new trust tier.
    if not cur_row.get("is_billing_verified") or not prior_row.get("is_billing_verified"):
        return None
    assessed_cur   = cur_row.get("assessed_value")
    assessed_prior = prior_row.get("assessed_value")
    if not assessed_cur or not assessed_prior:
        return None

    prev_due_by_entity = {
        r["entity_code"]: float(r["amount_due"])
        for r in (entity_detail_prev or []) if r.get("amount_due")
    }

    rows = []
    total_value = total_exemption = total_rate = 0.0
    incomplete = False

    for e in (entity_detail or []):
        code       = e["entity_code"]
        due_cur    = float(e["amount_due"]) if e.get("amount_due") else None
        due_prior  = prev_due_by_entity.get(code)
        rate_cur   = float(e["rate"])      if e.get("rate")      is not None else None
        rate_prior = float(e["rate_prev"]) if e.get("rate_prev") is not None else None

        if due_cur is None or not due_prior or not rate_cur or not rate_prior:
            incomplete = True
            continue  # can't decompose this entity without all four real inputs — skip, don't guess

        taxable_cur   = due_cur   / rate_cur   * 100
        taxable_prior = due_prior / rate_prior * 100

        value_effect = (taxable_cur - taxable_prior) * rate_prior / 100
        rate_effect  = taxable_cur * (rate_cur - rate_prior) / 100

        pure_value_effect = (assessed_cur - assessed_prior) * rate_prior / 100
        exemption_effect  = value_effect - pure_value_effect

        rows.append({
            "entity_code": code,
            "entity_name": e.get("entity_name") or code,
            "rate_cur": rate_cur,
            "rate_prior": rate_prior,
            "rate_effect": round(rate_effect, 2),
        })
        total_value     += pure_value_effect
        total_exemption += exemption_effect
        total_rate      += rate_effect

    if not rows:
        return None

    # Real headline totals — the SAME tax_billing.total_tax figures already
    # shown in the KPI cards / Value History table, so this card never shows
    # a start/end total that disagrees with the rest of the page.
    start_total = prior_row.get("total_tax")
    end_total   = cur_row.get("total_tax")
    if start_total is None or end_total is None:
        return None
    start_total, end_total = float(start_total), float(end_total)

    modeled_delta = total_value + total_exemption + total_rate
    real_delta    = end_total - start_total
    other_effect  = real_delta - modeled_delta  # reconciliation gap, shown only if material

    # "Exemptions or a cap likely reset" signature (July 2026, per Fable
    # review P0-5, companion to the 0121230106 fixture -- see that parcel's
    # verification notes). taxable_i above is BACKED OUT from real amount_due/
    # rate, so exemption_effect_i is, by construction, "whatever part of the
    # taxable-value change ISN'T explained by the parcel-wide assessed-value
    # change alone" -- exactly what a homestead cap reset (buyer loses the
    # seller's 10%/yr cap, assessed jumps toward market in one year) or an
    # exemption reset (HS dropped/regained) produces: taxable moves by far
    # more than assessed did, on a rate that barely moved. Confirmed against
    # 0121230106 (1 Hedge Ln): $15,887 -> $33,372 (+110%), taxable roughly
    # doubling on a barely-moved rate -- exactly a large |exemption_effect|
    # dominating a small |value_effect|, the pattern this flags.
    # Threshold: exemption_effect must be BOTH the dominant of the two effects
    # (bigger in magnitude than the pure value effect) AND economically
    # material ($250+) -- avoids flagging rounding-level noise on parcels
    # whose bill barely moved at all.
    reset_signature = abs(total_exemption) > max(abs(total_value), 250)
    reset_note = (
        "Exemptions or a cap likely reset (commonly after a sale) — most of "
        f"the change in {cur_year} taxable value isn't explained by the "
        "assessed-value change alone."
    ) if reset_signature else None

    return {
        "prior_year": prior_year,
        "cur_year": cur_year,
        "start_total": round(start_total, 2),
        "end_total": round(end_total, 2),
        "value_effect": round(total_value, 2),
        "exemption_effect": round(total_exemption, 2),
        "rate_effect": round(total_rate, 2),
        "other_effect": round(other_effect, 2),
        "real_delta": round(real_delta, 2),
        "incomplete": incomplete,
        "reset_signature": reset_signature,
        "reset_note": reset_note,
        "entity_rate_effects": sorted(
            rows, key=lambda r: abs(r["rate_effect"]), reverse=True
        ),
    }


def build_projections(history, rate_history, entity_detail, years_ahead=5, state_cd1=None):
    """
    Project market value, assessed value, and estimated taxes for the next N years.

    Value trend  : CAGR from earliest→current market values.
    Rate trend   : avg annual change in combined rate over available rate history.
    Assessed     : if homestead cap exists, cap at 10%/yr; else tracks market.
    Est. tax     : assessed * projected_rate / 100.

    Task 10 — CAGR baseline extension:
    If 2026 preliminary market value exists and is non-anomalous (assessed ≤ market),
    extend the CAGR window to 2021–2026 for a 6-year baseline.

    Coherence fix (July 2026, per Fable review finding 1): projections now also
    START from that same 2026 preliminary figure when one exists, instead of
    always starting from 2025 regardless. Previously this function calibrated
    its growth rate using 2026 data (above) but still modeled a "2026" row from
    2025 * CAGR -- a computed guess that could (and on real parcels, did)
    contradict the actual, real 2026 preliminary market value already shown
    elsewhere on the same page (Value History / homeowner cards). Confirmed on
    parcel 0100030109: real 2026 preliminary = $55,410,000; the old modeled
    "2026" projection row showed ~$62,628,505 instead -- a real, visible
    self-contradiction. Anchoring the projection to the same cagr_endpoint used
    for calibration means the projection table's first row is now the next
    year WITHOUT a real figure yet (2027, not 2026) whenever a 2026 preliminary
    exists -- it never re-models a year the page already shows a real number
    for.

    Agricultural guard (D/E parcels):
    AJR 2021 stores productivity/use values in the market_value field for agricultural
    property classes. Using 2021 as the CAGR starting point for D/E parcels produces
    meaningless projections. 2021 is excluded and 2022 used as the earliest reliable year.
    """
    # Pre-existing bug fix (found incidentally while fixing item 1 above, not
    # part of that brief): these early returns used to be a 2-tuple ([], None)
    # while the call site (`projections, proj_baseline, proj_bands =
    # build_projections(...)`) always unpacks 3 values, and the normal-path
    # return below is a 3-tuple. Any parcel hitting one of these early
    # returns (fewer than 2 years of market_value history, or an
    # agricultural parcel where excluding 2021 drops it below 2 years) would
    # raise `ValueError: not enough values to unpack` and 500 the whole
    # property page. Fixed to always return a 3-tuple.
    hist = sorted([r for r in history if r["market_value"]], key=lambda r: r["tax_year"])
    if len(hist) < 2:
        return [], None, None

    # Agricultural guard: skip 2021 baseline for D/E property classes
    _is_ag = (state_cd1 or "").strip()[:1].upper() in ("D", "E")
    if _is_ag:
        hist = [r for r in hist if r["tax_year"] != 2021]
        if len(hist) < 2:
            return [], None, None

    earliest = hist[0]

    # Prefer 2026 preliminary for CAGR calibration if non-anomalous
    r2026 = next(
        (r for r in hist if r["tax_year"] == 2026
         and r.get("data_source") == "preliminary"
         and r.get("market_value") and r.get("assessed_value")
         and r["assessed_value"] <= r["market_value"]),
        None
    )
    cagr_endpoint = r2026 if r2026 else next(
        (r for r in hist if r["tax_year"] == 2025), hist[-1]
    )
    # Label now also states the projection's actual starting point (matches
    # the coherence fix above -- base_row == cagr_endpoint), not just the CAGR
    # calibration window, so it's clear from the footnote alone why the first
    # projected row is 2027 (not 2026) whenever a real 2026 preliminary exists.
    if _is_ag:
        baseline_label = (
            "Based on 2022–2026 preliminary trend, projected forward from 2026" if r2026
            else "Based on 2022–2025 certified trend, projected forward from 2025"
        )
    else:
        baseline_label = (
            "Based on 2021–2026 preliminary trend, projected forward from 2026" if r2026
            else "Based on 2021–2025 certified trend, projected forward from 2025"
        )

    span = cagr_endpoint["tax_year"] - earliest["tax_year"]
    if span <= 0 or not earliest["market_value"]:
        return [], None, None

    # CAGR uses earliest → cagr_endpoint
    value_cagr = (cagr_endpoint["market_value"] / earliest["market_value"]) ** (1 / span) - 1

    # Rate trend from rate_history
    rh = sorted(rate_history, key=lambda r: r["tax_year"])
    if len(rh) >= 2:
        rates = [float(r["total_rate"]) for r in rh]
        rate_changes = [rates[i+1] - rates[i] for i in range(len(rates)-1)]
        avg_rate_change = sum(rate_changes) / len(rate_changes)
        current_rate = rates[-1]
    else:
        avg_rate_change = 0
        current_rate = sum(float(e["rate"]) for e in entity_detail if e["rate"])

    # Homestead cap detection — two complementary signals, OR'd together, not
    # one replacing the other:
    #
    #   1. hs_cap_loss > 0 in any year -- populated ONLY by the 2021-2024 AJR
    #      loader (load_ajr.py). Real, direct evidence the cap was actively
    #      suppressing assessed value below market in a specific past year.
    #      KNOWN_LIMITATIONS.md: "hs_cap_loss (2025): Not available -- The
    #      Certified Export format... does not carry a cap-loss field."
    #
    #   2. exemption_codes contains 'HS' in any year -- populated by the
    #      2025 Certified (load_certified_2025.py) and 2026 Preliminary
    #      (load_2026_preliminary.py) loaders' EXEMPTION_FIELDS scan, NEVER
    #      by load_ajr.py (AJR's PTY_SQL insert has no exemption_codes
    #      column at all). This is a real Comptroller exemption-code flag,
    #      not inferred -- if 'HS' is present, a homestead exemption (and
    #      therefore the 10%/yr appraisal cap under Tax Code §23.23) is
    #      active for that year, regardless of whether hs_cap_loss happens
    #      to be a positive number for that specific row.
    #
    # These two fields are POPULATED BY DISJOINT LOADERS covering different
    # year ranges (signal 1: 2021-2024 only; signal 2: 2025-2026 only) --
    # never both real for the same row, so this is genuinely an OR of two
    # non-overlapping coverage windows, not two independent confirmations of
    # the same thing. A parcel whose earliest available history starts in
    # 2025 or later (no 2021-2024 AJR row on file at all) previously fell
    # through signal 1 with NO way to ever satisfy it, silently losing cap
    # protection in this function's projections even though it has an
    # active, real homestead exemption on record right now (signal 2).
    # Confirmed via raw-file/loader inspection: 1,361 parcels (2.9% of the
    # ~46,300 homestead-exempt parcels with a real 2025 market/taxable gap
    # >5%, median gap 59.5%) hit exactly this case -- see the county
    # Cowork brief "Fix 6-Year Projection's Missing Homestead Cap for 2025+
    # Parcels" for the full investigation and population query.
    hs_row = next(
        (r for r in reversed(hist) if r.get("hs_cap_loss") and r["hs_cap_loss"] > 0),
        None
    )
    has_hs_exemption_code = any(
        "HS" in (r.get("exemption_codes") or "").split(",")
        for r in hist
    )
    has_hs_cap = (hs_row is not None) or has_hs_exemption_code

    # Project from the most recent REAL figure available -- reuses
    # cagr_endpoint (the 2026 preliminary when non-anomalous, else 2025
    # certified) instead of hardcoding tax_year == 2025. See the coherence-fix
    # note in this function's docstring for why.
    #
    # Issue 1 fix ("Homestead-Cap Data Integrity: Full Fix Set" Cowork brief):
    # this used to read base_row["assessed_value"] raw. THIS was the actual
    # bug behind the live "Assessed = Market in every projected row" report
    # on 0100050414 -- NOT has_hs_cap (which was already True for that
    # parcel via real 2021 AJR data). When cagr_endpoint is the 2026 row and
    # TCAD's preliminary export hasn't applied the cap yet (assessed_value ==
    # market_value, the earlier "coherence fix"'s own trigger condition for
    # picking 2026 in the first place), base_assessed == base_market at the
    # start, and since it then compounds at 10%/yr while market compounds at
    # this parcel's real (often <10%/yr) CAGR, min(base_assessed*1.10**i, pmv)
    # picks pmv every single year -- reproduced exactly against the live
    # 0100050414 data. Now reads current_2026's derive_2026_baseline() output
    # (est_assessed_2026, attached to the SAME row dict via the shared
    # `history` list in property_detail()) when base_row is the 2026 row --
    # the 2025 certified row's real assessed_value is unaffected by this bug
    # and is used as-is.
    base_row      = cagr_endpoint
    if base_row["tax_year"] == 2026 and base_row.get("est_assessed_2026") is not None:
        base_assessed = float(base_row["est_assessed_2026"])
    else:
        base_assessed = float(base_row["assessed_value"] or base_row["market_value"] or 0)
    base_market   = float(base_row["market_value"])
    base_year     = base_row["tax_year"]

    # scenario_banded_projection_task3
    # CAGR offsets for scenario bands
    # Low  : CAGR − 2 pp, floored at −5%; rate holds flat
    # Base : existing CAGR; existing rate trend
    # High : CAGR + 2 pp; rate trend amplified 1.5x
    cagr_low  = max(-0.05, value_cagr - 0.02)
    cagr_base = value_cagr
    cagr_high = value_cagr + 0.02

    def _make_rows(cagr, rate_delta_mult):
        out = []
        for i in range(1, years_ahead + 1):
            py  = base_year + i
            pmv = round(base_market * (1 + cagr) ** i)
            pr  = max(0, current_rate + avg_rate_change * rate_delta_mult * i)
            if has_hs_cap:
                pav = round(min(base_assessed * (1.10 ** i), pmv))
            else:
                pav = pmv
            et = round(pav * pr / 100)
            out.append({
                "year":         py,
                "market":       pmv,
                "assessed":     pav,
                "rate":         round(pr, 6),
                "est_tax":      et,
                "value_change": round((pmv - base_market) / base_market * 100, 1),
            })
        return out

    rows      = _make_rows(cagr_base, 1.0)      # base (unchanged from previous behaviour)
    rows_low  = _make_rows(cagr_low,  0.0)      # low: flat rates
    rows_high = _make_rows(cagr_high, 1.5)      # high: steeper rate trend

    bands = {
        "low":  rows_low,
        "high": rows_high,
        "cagr_low":   round(cagr_low  * 100, 2),
        "cagr_base":  round(cagr_base * 100, 2),
        "cagr_high":  round(cagr_high * 100, 2),
    }

    return rows, baseline_label, bands


# ── Tax calendar / annual cycle position ───────────────────────────────────────
def build_tax_calendar(today, current_2026, delinquent):
    """
    Where a parcel sits in the annual Travis County property tax cycle right
    now (July 2026, per Diego's Cowork brief -- "Tax calendar/timeline
    section", item 1).

    MILESTONE SOURCING (investigated before building, not invented): every
    date/window below is the same statutory Texas/Travis County annual
    calendar already researched and cited in templates/info.html's "Travis
    County — who does what" card (sources: TCAD's "Property Tax System"
    page and the Travis County Tax Office's Truth-in-Taxation Summary, both
    linked in that card's own footer). Nothing here is a new date -- this
    function only computes where "today" (the real server clock) falls
    against a calendar that already exists and is already sourced
    elsewhere on this site.

    What is deliberately NOT here, because it doesn't exist in this
    platform's data: a per-parcel Notice-of-Appraised-Value mail date or a
    per-parcel protest deadline. Checked before assuming otherwise: neither
    TCAD's Certified Export nor the Preliminary Export (the two source
    files this platform loads -- see KNOWN_LIMITATIONS.md) carries a
    notice-mailed-date field, and no loader in this repo captures one (grep
    across loaders/*.py and this file's own query columns turned up
    nothing). So "Notices Mailed" (mid-April) and "Protest Deadline"
    (May 15) are shown as the general STATUTORY calendar -- true for
    essentially every Travis County parcel under Tax Code §41.44 and the
    Comptroller's truth-in-taxation calendar -- not a claim about when
    *this* parcel's own notice was mailed, which this platform doesn't
    have and isn't invented here.

    "Roll Certified" (July 25) is the one milestone with a real, dated,
    year-specific source already in this codebase -- see
    KNOWN_LIMITATIONS.md's "Certification date" section, which this
    function's date literally matches (both hardcode July 25 of the
    current year; if that ever needs to move to a config value instead of
    two independent hardcodes, that's a fair follow-up, flagged here
    rather than silently left inconsistent).

    Per-parcel REAL signals folded in on top of the generic calendar (not
    calendar assumptions):
      - current_2026.data_source: if this parcel's own 2026 row is still
        'preliminary' as of today, a note is attached to the Roll
        Certified milestone -- this is real, per-parcel
        parcel_tax_year.data_source, not a calendar guess.
      - delinquent.total_due: if this parcel carries a real, nonzero
        delinquent balance on file (tax_delinquent table, sourced from
        TaxDelqOpenData), a note is attached to the Payment Due milestone.

    KNOWN SIMPLIFICATION (flagged, not silently glossed over): milestones
    are built for the cycle_year = today.year, running Jan 1 (cycle_year)
    through Jan 31 (cycle_year + 1). In the first ~31 days of January this
    slightly under-represents the PRIOR cycle's still-relevant Jan 31
    payment deadline (that prior cycle's own Payment Due milestone, built
    a year earlier, already covers it as its own terminal stage -- but
    this function doesn't cross-reference two cycles at once). Not an
    issue for verification against today's actual date; noted for anyone
    extending this later.
    """
    milestones = [
        {"key": "valuation",      "date": date(today.year, 1, 1),
         "label": "Valuation Date",
         "desc": "TCAD sets property values as of this date (Tax Code §23.01)."},
        {"key": "notices",        "date": date(today.year, 4, 15),
         "label": "Notices Mailed",
         "desc": "Notices of Appraised Value go out (mid-April, statutory window)."},
        {"key": "protest",        "date": date(today.year, 5, 15),
         "label": "Protest Deadline",
         "desc": "May 15, or 30 days after your notice was mailed, whichever is later (Tax Code §41.44)."},
        {"key": "certification",  "date": date(today.year, 7, 25),
         "label": "Roll Certified",
         "desc": "TCAD certifies the appraisal roll."},
        {"key": "rate_adoption",  "date": date(today.year, 9, 1),
         "label": "Rates Adopted",
         "desc": "Taxing entities adopt their tax rates (August–September)."},
        {"key": "bills_mailed",   "date": date(today.year, 10, 1),
         "label": "Bills Mailed",
         "desc": "Tax bills begin mailing in October."},
        {"key": "payment_due",    "date": date(today.year + 1, 1, 31),
         "label": "Payment Due",
         "desc": "Taxes are due; unpaid balances become delinquent February 1."},
    ]

    if current_2026 and current_2026.get("data_source") == "preliminary":
        for m in milestones:
            if m["key"] == "certification":
                m["parcel_note"] = "This parcel's 2026 values are still Preliminary as of today."

    if delinquent and delinquent.get("total_due") and float(delinquent["total_due"]) > 0:
        for m in milestones:
            if m["key"] == "payment_due":
                m["parcel_note"] = (
                    f"This parcel has a real delinquent balance on file: "
                    f"${float(delinquent['total_due']):,.0f}."
                )

    for m in milestones:
        m["passed"] = today >= m["date"]

    # "Current" milestone selection (Part B fix, Diego-confirmed, July 2026):
    # the PREVIOUS logic picked the most-recently-PASSED milestone as
    # "current" and attached the countdown to the FOLLOWING milestone --
    # so a fully-passed deadline (e.g. Protest Deadline, May 15) got the
    # "You are here" / accent-glow treatment while the countdown text below
    # it counted down to a DIFFERENT, later milestone (e.g. Roll Certified,
    # Jul 25). Correct, but confusing: the highlighted node and the "N days"
    # text described two different events.
    #
    # Fixed definition: "current" is the NEXT UPCOMING milestone -- the
    # first one that hasn't passed yet -- and its own countdown (days until
    # ITSELF, not some other milestone) is attached to that same node. Every
    # already-passed milestone (the `m["passed"]` flag above, computed
    # first and unchanged) just renders as plain "done" -- no accent, no
    # "You are here" -- per templates/property.html's `_is_current` check
    # and static/style.css's `.passed` vs `.current` rules.
    current_index = next((i for i, m in enumerate(milestones) if not m["passed"]), None)
    if current_index is not None:
        milestones[current_index]["current"] = True
        # Renamed from days_to_next -> days_until: this now counts down to
        # the SAME milestone that's marked current, not "the next one after
        # current" (that concept no longer exists -- current IS next).
        milestones[current_index]["days_until"] = (
            milestones[current_index]["date"] - today
        ).days
        # Countdown strings (Fable's "the line is the indicator" redesign,
        # July 2026): both derived from the SAME days_until above, no new
        # date math. countdown_full feeds the pill at normal container
        # widths; countdown_compact feeds it at <=640px (spec §6) -- the
        # server emits both so the responsive swap is a pure CSS media
        # query, no JS needed. "Today" (n==0) is included for completeness
        # per the spec's own enumeration, though in practice n is never 0
        # for the CURRENT milestone specifically: the moment today reaches
        # a milestone's own date, `passed` (today >= date) flips true for
        # THAT milestone and current_index advances to the next one, whose
        # own days_until is always >= 1. Kept anyway so this is correct if
        # that invariant ever changes, rather than silently assuming it.
        _n = milestones[current_index]["days_until"]
        milestones[current_index]["countdown_full"] = (
            "Today" if _n == 0 else "Tomorrow" if _n == 1 else f"In {_n} days"
        )
        milestones[current_index]["countdown_compact"] = "Now" if _n == 0 else f"{_n}d"
        # Proportional positioning fraction (Diego-confirmed, July 2026):
        # today's position along [last-passed date, this milestone's date],
        # clamped [0, 1]. Round 1/2 consumed this for a floating badge that
        # collided with node text at high fractions; Fable's redesign
        # (below) instead folds it into --today-frac, a single percentage
        # along the WHOLE track, so the "indicator" is the track's own
        # fill/tick rather than a separately-positioned element.
        #
        # current_index > 0 always holds here in any real invocation:
        # milestones[0] (Valuation Date, Jan 1 of today.year) can never
        # itself be "current", because Jan 1 of today's own year is always
        # <= today -- i.e. it's always already "passed" by the time this
        # function runs with a real server-clock `today`. Guarded anyway
        # (current_index > 0) so a synthetic/test `today` before Jan 1
        # can't hit a negative list index -- see today_frac_pct below for
        # how that defensive case (spec §5, "today before the first
        # milestone") is handled instead.
        if current_index > 0:
            prev_date = milestones[current_index - 1]["date"]
            cur_date = milestones[current_index]["date"]
            span_days = (cur_date - prev_date).days
            fraction = (today - prev_date).days / span_days if span_days else 0.0
            milestones[current_index]["here_fraction"] = max(0.0, min(1.0, fraction))
    # Edge case: every milestone in this cycle has already passed --
    # current_index stays None, no milestone gets "current"/a pill, and the
    # strip shows all seven as plain done checkmarks (spec §5, "today past
    # the final milestone").
    #
    # In practice this branch is structurally very hard to reach through a
    # real page load: `cycle_year` is pinned to `today.year` on every call
    # (two lines below), so the last milestone (Payment Due) is always
    # `date(today.year + 1, 1, 31)` -- by construction always later than any
    # `today` that still has `today.year == cycle_year`. Verified by direct
    # execution with a synthetic `today` past that date (see verification
    # notes) rather than assumed; handled defensively regardless, so a
    # future caller passing an atypical `today` (e.g. a test harness) can't
    # hit an unhandled state.

    # --today-frac (Fable's "the line is the indicator" redesign, July
    # 2026): ONE percentage (0-100) locating today along the whole 7-node
    # track, where node 0's center = 0% and node 6's center = 100% (6 equal
    # gaps between 7 nodes). Built entirely from the SAME already-verified
    # current_index/here_fraction above -- no new date math, just a
    # different way of expressing an already-correct position. This is the
    # one new value this redesign adds; every other field above is
    # untouched from the already-verified Part B / proportional-positioning
    # rounds. Emitted as an inline CSS custom property on the track element
    # (templates/property.html), consumed by both the progress-fill width
    # and the today-tick's left offset (static/style.css) -- one source of
    # truth, so they cannot disagree (spec acceptance criterion 4).
    #
    #   current_index is None    -> every milestone passed -> 100% (full fill)
    #   current_index == 0       -> nothing passed yet      -> 0% (no fill)
    #   otherwise                -> (i_last_passed + fraction) / 6 * 100,
    #                                i_last_passed = current_index - 1
    if current_index is None:
        today_frac_pct = 100.0
    elif current_index == 0:
        today_frac_pct = 0.0
    else:
        i_last_passed = current_index - 1
        today_frac_pct = round(((i_last_passed + milestones[current_index]["here_fraction"]) / 6) * 100, 2)

    return {
        "today": today,
        "cycle_year": today.year,
        "milestones": milestones,
        "current_index": current_index,
        "today_frac_pct": today_frac_pct,
    }


# ── CoStar-style property narrative generator ────────────────────────────────
def generate_property_narrative(parcel, history, metrics_by_year, benchmark_by_year,
                                insights, projections):
    """
    Assemble a 2–3 paragraph investor-facing narrative from actual parcel data.
    Text is fully data-driven — no AI generation.
    Returns a list of paragraph strings.
    """
    sc1 = (parcel.get("state_cd1") or "").strip()[:1]
    type_map = {
        "A": "single-family residential", "B": "multi-family residential",
        "C": "vacant land", "D": "agricultural land", "E": "rural land",
        "F": "commercial real property",
    }
    prop_type = type_map.get(sc1, "real property")
    address = parcel.get("situs_address") or "This parcel"

    hist = sorted([r for r in history if r.get("market_value")], key=lambda r: r["tax_year"])
    r2025 = next((r for r in hist if r["tax_year"] == 2025), None)
    r2026 = next((r for r in hist if r["tax_year"] == 2026), None)
    m25   = metrics_by_year.get(2025)
    paragraphs = []

    # PX-20260824-01: county_name from the request's real profile, not a
    # hardcoded "Travis County". ", Texas" (spelled out, matching this
    # sentence's own pre-existing style -- every other site occurrence
    # abbreviates "TX") is not a profile field: every county registered in
    # COUNTY_PROFILES today is a Texas county (Travis, Dallas; Harris when
    # it's onboarded), so this is safe for now but would need a real
    # "state" field the day a non-Texas county is added -- flagged here
    # rather than silently assumed to hold forever.
    county_profile = COUNTY_PROFILES.get(getattr(g, "county_code", None), COUNTY_PROFILES["TRAVIS"])

    # ── Para 1: property identity + value trajectory ──────────────────────────
    p1 = [f"{address} is a {prop_type} parcel in {county_profile['county_name']}, Texas."]
    if r2026 and r2026.get("market_value") and r2025 and r2025.get("market_value"):
        mv26, mv25 = r2026["market_value"], r2025["market_value"]
        pct = (mv26 - mv25) / mv25 * 100
        p1.append(
            f"The 2026 preliminary appraisal values the property at ${mv26:,.0f}, "
            f"{'up' if pct >= 0 else 'down'} {abs(pct):.1f}% from the 2025 "
            f"certified value of ${mv25:,.0f}."
        )
    elif r2025 and r2025.get("market_value"):
        p1.append(f"The 2025 certified market value is ${r2025['market_value']:,.0f}.")
        if insights and insights.get("value_change_pct") is not None and insights.get("span", 0) > 1:
            pct  = insights["value_change_pct"]
            cagr = insights.get("value_cagr", 0)
            p1.append(
                f"Market value has {'appreciated' if pct > 0 else 'declined'} "
                f"{abs(pct):.1f}% from {insights['earliest_year']} to "
                f"{insights['latest_year']} (CAGR {cagr:.1f}%)."
            )
    paragraphs.append(" ".join(p1))

    # ── Para 2: assessment ratio + tax burden ──────────────────────────────────
    p2 = []
    if r2025 and r2025.get("assessed_value") and r2025.get("market_value"):
        ratio = r2025["assessed_value"] / r2025["market_value"] * 100
        p2.append(
            f"For 2025, the assessed value is ${r2025['assessed_value']:,.0f} "
            f"({ratio:.1f}% of market value)."
        )
    if m25 and m25.get("effective_tax_rate") is not None:
        etr = float(m25["effective_tax_rate"]) * 100
        bench_str = ""
        b25 = benchmark_by_year.get(2025)
        if b25 and b25.get("median_assessment_ratio") is not None:
            try:
                county_ratio = float(b25["median_assessment_ratio"]) * 100
                bench_str = (
                    f" The county median assessment ratio for this property type is "
                    f"{county_ratio:.1f}%."
                )
            except Exception:
                pass
        p2.append(f"The effective tax rate in 2025 is {etr:.4f}%.{bench_str}")
    elif insights and insights.get("total_rate_2025"):
        rate = insights["total_rate_2025"]
        est  = insights.get("est_annual_tax")
        n    = insights.get("entity_count", "multiple")
        p2.append(
            f"The combined rate across {n} taxing entities is {rate:.4f}% in 2025"
            + (f", with estimated annual taxes of ${est:,.0f}." if est else ".")
        )
    if p2:
        paragraphs.append(" ".join(p2))

    # ── Para 3: risk flags or forward outlook ──────────────────────────────────
    p3 = []
    if m25:
        if m25.get("cap_step_up_exposure"):
            p3.append(
                "An active homestead cap is in place — assessed value is below market. "
                "A buyer loses this benefit at purchase and the assessed value resets to full market."
            )
        if m25.get("risk_large_value_jump"):
            flag_pct = m25.get("risk_large_value_jump_pct", 0)
            p3.append(
                f"A large year-over-year value change ({flag_pct:.0f}%) was flagged — "
                "verify against comparable sales before underwriting."
            )
        if m25.get("risk_delinquent"):
            p3.append(
                "Delinquent taxes are on record. These constitute a lien on the property "
                "and transfer to the buyer at closing unless negotiated otherwise."
            )
    if not p3 and projections:
        pl = projections[-1]
        p3.append(
            f"Based on the historical value trend, market value is projected at approximately "
            f"${pl['market']:,.0f} by {pl['year']}, with an estimated annual tax burden "
            f"of ${pl['est_tax']:,.0f}."
        )
    if p3:
        paragraphs.append(" ".join(p3))

    return paragraphs


# ── Annual Trends table computation ─────────────────────────────────────────
def compute_annual_trends(history, metrics_by_year, projections):
    """
    Compute the CoStar-style Annual Trends table rows for the property detail page.
    Returns a list of row dicts (label, twelve_month, hist_avg, forecast_avg,
    peak, peak_when, trough, trough_when).
    """
    hist = sorted([r for r in history if r.get("market_value") and r["tax_year"] <= 2026],
                  key=lambda r: r["tax_year"])

    # ── Market Value Growth ───────────────────────────────────────────────────
    yoy_list, peak_g, trough_g = [], None, None
    for i in range(1, len(hist)):
        prev, curr = hist[i-1], hist[i]
        if prev["market_value"] and curr["market_value"]:
            pct = (curr["market_value"] - prev["market_value"]) / prev["market_value"] * 100
            yoy_list.append((curr["tax_year"], round(pct, 1)))
            if peak_g is None or pct > peak_g[0]:
                peak_g = (round(pct, 1), curr["tax_year"])
            if trough_g is None or pct < trough_g[0]:
                trough_g = (round(pct, 1), curr["tax_year"])

    recent_yoy = yoy_list[-1][1] if yoy_list else None
    hist_avg_g = round(sum(v for _, v in yoy_list) / len(yoy_list), 1) if yoy_list else None
    # Forecast Avg fix (July 2026, per Fable review P1-9 -- "Forecast Avg YoY
    # mixing"): this used to average p["value_change"] across the 5 projected
    # years, but that field (build_projections()'s _make_rows()) is the
    # CUMULATIVE % change from the projection's base year to each row's own
    # year -- not a year-over-year rate. Averaging 5 cumulative figures from a
    # constant-CAGR model (e.g. +6.0%, +12.4%, +19.1%, +26.2%, +33.8% for a 6%
    # CAGR) produces ~19.5%, nearly 3x the real annual growth rate the model
    # actually assumes -- sitting directly next to Hist. Avg in the same row,
    # which IS a genuine annual average, so the two read as comparable numbers
    # in the same unit when they weren't. Fixed the same way hist_avg_g just
    # above computes it: chain the projection's own base market value with
    # each row's market value and average the YEAR-OVER-YEAR deltas between
    # consecutive years, not the cumulative-from-base figures. For this
    # model's constant single-CAGR compounding, every step's YoY is identical
    # (== the CAGR the projection was built with), so this now agrees with
    # the "Scenario Band" panel's own cagr_base % elsewhere on this page,
    # which the old figure never did.
    proj_avg_g = None
    if projections:
        base_mv = hist[-1]["market_value"] if hist else None
        if base_mv:
            _chain = [base_mv] + [p["market"] for p in projections if p.get("market")]
            _proj_yoy = [
                (_chain[i] - _chain[i - 1]) / _chain[i - 1] * 100
                for i in range(1, len(_chain)) if _chain[i - 1]
            ]
            proj_avg_g = round(sum(_proj_yoy) / len(_proj_yoy), 1) if _proj_yoy else None

    def _fmt_pct(v):
        return f"{'+' if v >= 0 else ''}{v:.1f}%" if v is not None else "—"

    rows = [dict(
        label="Market Value Growth",
        twelve_month=_fmt_pct(recent_yoy),
        hist_avg=_fmt_pct(hist_avg_g),
        forecast_avg=_fmt_pct(proj_avg_g) if proj_avg_g is not None else "—",
        peak=_fmt_pct(peak_g[0]) if peak_g else "—",
        peak_when=str(peak_g[1]) if peak_g else "—",
        trough=_fmt_pct(trough_g[0]) if trough_g else "—",
        trough_when=str(trough_g[1]) if trough_g else "—",
        note="",
    )]

    # ── Assessment Ratio ──────────────────────────────────────────────────────
    ratios = []
    for r in hist:
        if r.get("assessed_value") and r.get("market_value") and r["market_value"] > 0:
            ratios.append((r["tax_year"], round(r["assessed_value"] / r["market_value"] * 100, 1)))

    curr_ratio = ratios[-1][1] if ratios else None
    avg_ratio  = round(sum(v for _, v in ratios) / len(ratios), 1) if ratios else None
    peak_r     = max(ratios, key=lambda x: x[1]) if ratios else None
    trough_r   = min(ratios, key=lambda x: x[1]) if ratios else None

    def _fmt_ratio(v):
        return f"{v:.1f}%" if v is not None else "—"

    rows.append(dict(
        label="Assessment Ratio",
        twelve_month=_fmt_ratio(curr_ratio),
        hist_avg=_fmt_ratio(avg_ratio),
        forecast_avg="—",
        peak=_fmt_ratio(peak_r[1]) if peak_r else "—",
        peak_when=str(peak_r[0]) if peak_r else "—",
        trough=_fmt_ratio(trough_r[1]) if trough_r else "—",
        trough_when=str(trough_r[0]) if trough_r else "—",
        note="",
    ))

    # ── Effective Tax Rate ────────────────────────────────────────────────────
    m25  = metrics_by_year.get(2025)
    etr  = float(m25["effective_tax_rate"]) * 100 if (m25 and m25.get("effective_tax_rate") is not None) else None

    rows.append(dict(
        label="Effective Tax Rate (2025)",
        twelve_month=f"{etr:.4f}%" if etr is not None else "—",
        hist_avg=f"{etr:.4f}%" if etr is not None else "—",
        forecast_avg="—",
        peak=f"{etr:.4f}%" if etr is not None else "—",
        peak_when="2025" if etr is not None else "—",
        trough=f"{etr:.4f}%" if etr is not None else "—",
        trough_when="2025" if etr is not None else "—",
        note="Billing data available for 2025 only" if etr is None else "",
    ))

    # ── Tax Amount ────────────────────────────────────────────────────────────
    # Real fix (July 2026, Property Page Small Bugs Batch item 1, per Diego --
    # "same underlying blindness as the two prior fixes, just expressed as a
    # value pick rather than a colored badge"): twelve_month used to be picked
    # by hardcoding `yr == 2025`, regardless of whether that year's total_tax
    # was a genuinely verified figure, a derived/reconstructed sum, or a
    # portal-scrape partial receipt -- the same confidence-blindness the
    # Value & Tax History table's badge and the Growth & Assessment Metrics
    # coverage badge already had fixed this round (both now read
    # r.is_billing_verified instead of assuming 2025 == Verified). This row
    # has no separate badge cell to correct the way those two did, so the
    # equivalent fix is: (a) pick "current" from whichever year actually HAS
    # the most recent billing data, not an assumption it's always 2025 (same
    # "use the real year, don't hardcode it" convention build_projections()'s
    # cagr_endpoint already established elsewhere in this file), and (b) flag
    # via the existing `note` field -- the same mechanism the Effective Tax
    # Rate row above already uses -- whenever that figure isn't genuinely
    # verified, instead of silently presenting it as equally certain. Matches
    # the other two fixes' spirit exactly: correct the confidence claim,
    # don't hide the underlying number.
    tax_pts_full = [(r["tax_year"], float(r["total_tax"]), bool(r.get("is_billing_verified")))
                    for r in hist if r.get("total_tax")]
    tax_pts  = [(y, t) for y, t, _ in tax_pts_full]
    curr_year, curr_tax = tax_pts[-1] if tax_pts else (None, None)
    curr_row = next((r for r in hist if r["tax_year"] == curr_year), None) if curr_year else None
    curr_verified = bool(curr_row and curr_row.get("is_billing_verified"))
    avg_tax  = round(sum(t for _, t in tax_pts) / len(tax_pts)) if tax_pts else None
    peak_t   = max(tax_pts, key=lambda x: x[1]) if tax_pts else None
    trough_t = min(tax_pts, key=lambda x: x[1]) if tax_pts else None
    proj_tax = round(sum(p["est_tax"] for p in projections) / len(projections)) if projections else None

    def _fmt_usd(v):
        return f"${v:,.0f}" if v is not None else "—"

    # Hist.-Avg confidence-blend fix (July 2026, per Fable review P0-3 --
    # "any Hist. Avg cell that blends Verified and Partial years"). The
    # note below used to only ever describe the CURRENT year's confidence,
    # so a parcel with a clean, verified 2025 total_tax showed no note at
    # all even when avg_tax (the Hist. Avg cell right next to twelve_month)
    # silently averaged in one or more partial/derived years alongside it --
    # the average inherited a confidence it hadn't actually earned. Now
    # checks ALL years feeding avg_tax, not just the latest one.
    _n_partial = sum(1 for _, _, v in tax_pts_full if not v)
    if not tax_pts:
        tax_note = "Billing data available for 2025 only"
    elif not curr_verified:
        tax_note = f"{curr_year} total is a derived or partial figure, not independently confirmed"
    elif _n_partial:
        tax_note = (f"Hist. Avg blends {_n_partial} partial/derived year"
                     f"{'s' if _n_partial != 1 else ''} with verified years "
                     "— see Value & Tax History above for which")
    else:
        tax_note = ""

    rows.append(dict(
        label="Tax Amount",
        twelve_month=_fmt_usd(curr_tax),
        hist_avg=_fmt_usd(avg_tax),
        forecast_avg=f"~{_fmt_usd(proj_tax)}" if proj_tax else "—",
        peak=_fmt_usd(peak_t[1]) if peak_t else "—",
        peak_when=str(peak_t[0]) if peak_t else "—",
        trough=_fmt_usd(trough_t[1]) if trough_t else "—",
        trough_when=str(trough_t[0]) if trough_t else "—",
        note=tax_note,
    ))

    return rows


# ── Texas Comptroller state property use code descriptions ────────────────────
# Source: Texas Property Tax Code, Comptroller Rule 9.4001
STATE_CD_DESCRIPTIONS = {
    # Residential
    "A":  "Single-Family Residential",
    "A1": "Single-Family Residence",
    "A2": "Single-Family (Manufactured Home)",
    "A3": "Single-Family Residence Details",
    "A4": "Condominium",
    "A5": "Condominium Details",
    "A9": "HS Commercial (Highest & Best Use)",
    # Multi-family
    "B":  "Multi-Family Residential",
    "B1": "Multifamily",
    "B2": "Duplex",
    "B3": "Triplex",
    "B4": "Four-Plex",
    "B5": "Multifamily with HS",
    # Vacant / Land
    "C":  "Vacant Lots and Tracts",
    "C1": "Vacant Lot",
    "C2": "Colonia Property",
    # Agricultural
    "D":  "Agricultural",
    "D1": "Acreage — Qualified Open-Space Land (1-d-1)",
    "D2": "Farm/Ranch Improvements on Open-Space Land",
    "D3": "Agricultural (1-d)",
    "E":  "Rural Land (Not Qualified for Open-Space Appraisal)",
    "E1": "Farm and Ranch Improvements on Non-Ag Land",
    "E2": "Farm and Ranch Improvements (MH) on Non-Ag Land",
    "E3": "Farm and Ranch Misc Improvements on Non-Ag Land",
    # Commercial / Industrial
    "F":  "Commercial Real Property",
    "F1": "Commercial Real Property (Improved)",
    "F2": "Industrial / Major Manufacturing",
    "F3": "Commercial Details",
    "F4": "Commercial Condo",
    "F5": "Commercial Residential Conversion",
    # Minerals / Utilities
    "G1": "Oil and Gas",
    "G2": "Minerals",
    "G3": "Sub-Surface Mines and Quarries",
    "J1": "Water Utility",
    "J2": "Gas Distribution System",
    "J3": "Electric Company (incl. Co-ops)",
    "J4": "Telephone Company (incl. Co-ops)",
    "J5": "Railroad",
    "J6": "Pipeline",
    "J7": "Cable Company",
    "J8": "Other Utility",
    "J9": "Railroad Rolling Stock",
    # Personal property
    "L1": "Commercial Personal Property",
    "L2": "Industrial/Manufacturing Personal Property",
    "M1": "Mobile Home",
    "M2": "Other Tangible Personal Property",
    # Exempt / Special
    "X":  "Exempt Property",
    "X1": "Totally Exempt",
    # Non-standard codes that appear in TCAD data
    "O":  "Other / Unclassified",   # 3.9% of parcels — TCAD catch-all, no Comptroller equivalent
    "S":  "Special / State Property",
    "N":  "Non-Taxable",
    "ER": "Exempt — Religious",
}

# USE_CODE_LOOKUP and use_code_case_sql() (the TCAD numeric use-code table and
# the Market Snapshot taxonomy that's built from it) moved to
# snapshot_taxonomy.py (AGGPRECOMP-2, Aug 2026) -- see that module's
# docstring for why: loaders/refresh_snapshot_summary.py needs the exact
# same SQL-fragment builders this route uses, and no loaders/*.py file in
# this codebase imports app.py (Flask app creation, Sentry init, and a
# required FLASK_SECRET at import time are not things a batch script should
# drag in as side effects). Imported below, near this module's other
# app-root imports.

# Valuation method inferred from Texas Comptroller state_cd1 first character.
# Used as a fallback until the TCAD numeric use code field is loaded.
VALUATION_METHOD_BY_CLASS = {
    "A": "Cost",        # Residential SFR — market/cost approach
    "B": "Income",      # Multi-family — income approach
    "C": "Cost",        # Vacant land — sales comparison / cost
    "D": "Productivity",# Agricultural — 1-d-1 productivity value
    "E": "Cost",        # Rural land — cost/comparable sales
    "F": "Income",      # Commercial — income approach
    "G": "Income",      # Minerals/Oil — DCF / yield capitalisation
    "J": "Cost",        # Utilities — cost approach
    "L": "Cost",        # Personal property — cost (depreciated)
    "M": "Cost",        # Mobile home — cost
    "X": "Exempt",      # Exempt property
    "O": "Unknown",     # TCAD catch-all — no standard valuation method
    "S": "Unknown",     # Special/State
    "N": "Unknown",
}


def get_valuation_method(state_cd1: str) -> str:
    """Return the most likely valuation method for a parcel given its state_cd1 code."""
    if not state_cd1:
        return "Unknown"
    prefix = state_cd1.strip()[:1].upper()
    return VALUATION_METHOD_BY_CLASS.get(prefix, "Unknown")


# The Market Snapshot taxonomy (SNAPSHOT_*_CODES, SNAPSHOT_LAND_SIZE_TIERS/
# SNAPSHOT_AG_SIZE_TIERS, _size_tier_case_sql(), _snapshot_taxonomy_sql(),
# _SNAPSHOT_TAB_ORDER, _snapshot_taxonomy_sort_case_sql(),
# _SNAPSHOT_SECTOR_VIEWS, _SNAPSHOT_VALID_VIEWS, _SNAPSHOT_VIEW_PROP_TYPE_LABEL,
# _snapshot_view_where(), ptype_and_sort_case_for_view()) moved to
# snapshot_taxonomy.py (AGGPRECOMP-2, Aug 2026) -- same reasoning as
# USE_CODE_LOOKUP's move above. SNAPSHOT_SUBTYPE_CAP and _cap_subtype_rows()
# below stay here: that capping happens at READ TIME over already-small,
# already-precomputed rows (cheap, and not something the refresh script
# needs), unlike everything that moved, which builds the SQL queries
# themselves -- the actual aggregation logic the spec's own principle says
# must live only inside refresh functions.

# Part 2 — cap: within a sector tab's "By Property Type" subtype breakdown,
# show only the top N real use-code subtypes by parcel count; roll the rest
# into one honest "Other <Sector>" row rather than a table with (in the
# worst observed case) ~90 rows. Cutoff = 7: Diego's brief suggested 5-8 as
# "likely right" without a live distribution to size it against (no DB
# access in this sandbox, see Part 0). 7 was chosen over the endpoints of
# that range as a middle value that keeps a sector's table to at most 8 rows
# (7 real subtypes + 1 rollup) -- scannable at a glance without a scrollbar
# on a typical viewport, while still surfacing enough real subtypes that a
# sector with a genuinely diverse mix (e.g. Retail: Restaurant/Grocery/Strip
# Center/Fast Food/...) isn't flattened to 2-3 rows. This is a reasoned
# default, not a measured-optimal one -- Diego should sanity-check it against
# the real per-sector subtype counts task_staging/other_property_type/
# check_other_property_type_fix.command's Section 0 extension reports, and
# this constant is the one place to change it if 7 turns out wrong for the
# real data.
SNAPSHOT_SUBTYPE_CAP = 7


def _cap_subtype_rows(rows, fallback_label, top_n=SNAPSHOT_SUBTYPE_CAP):
    """Part 2 fix: collapse a sector's real per-use-code breakdown to the
    top `top_n` rows by parcel count, folding everything else (including any
    row that already used the SQL-level `fallback_label` ELSE bucket) into
    one combined rollup row.

    n_parcels/n_up/n_down/n_flat/total_mv25_b/total_mv26_b are exact sums
    across the rolled-up rows -- simple additive counts and dollar totals,
    mathematically valid to combine this way. median_pct/p25_pct/p75_pct are
    NOT: a percentile of a merged group is not derivable from the member
    groups' own percentiles (not their average, weighted or otherwise)
    without re-running the percentile calculation against the underlying
    per-parcel data, which this display-side cap deliberately avoids doing
    (the whole point is not re-querying per sector). Rather than fabricate a
    number that LOOKS like a median but isn't one, the rolled-up row shows
    "--" for those three columns -- same "honest label, not an invented
    number" discipline as the rest of this session's fixes.

    `_rolled_ptypes` is stashed on the returned rollup row (list of the real
    ptype strings folded into it, excluding fallback_label itself) so the
    drill-through link can match every parcel that's actually represented by
    this row, not just the ones that hit the SQL ELSE branch directly --
    see _ptype_drill_where()'s `rolled` handling.
    """
    if len(rows) <= top_n:
        return rows  # nothing to cap -- already a clean, scannable table

    ordered = sorted(rows, key=lambda r: (r["n_parcels"] or 0), reverse=True)
    keep = ordered[:top_n]
    overflow = ordered[top_n:]
    if not overflow:
        return keep

    rolled_ptypes = [r["ptype"] for r in overflow if r["ptype"] != fallback_label]
    rollup = {
        "ptype":         fallback_label,
        "n_parcels":     sum(r["n_parcels"] or 0 for r in overflow),
        "n_up":          sum(r["n_up"] or 0 for r in overflow),
        "n_down":        sum(r["n_down"] or 0 for r in overflow),
        "n_flat":        sum(r["n_flat"] or 0 for r in overflow),
        "median_pct":    None,  # honest -- see docstring, not a valid combined statistic
        "p25_pct":       None,
        "p75_pct":       None,
        "total_mv25_b":  round(sum(r["total_mv25_b"] or 0 for r in overflow), 3),
        "total_mv26_b":  round(sum(r["total_mv26_b"] or 0 for r in overflow), 3),
        "_rolled_ptypes": rolled_ptypes,
    }
    # If one of the kept top-N rows is itself already the literal
    # fallback_label (a real, sizeable ELSE bucket that made the cut on its
    # own), merge it into the rollup instead of showing "Other X" twice.
    existing_fallback = next((r for r in keep if r["ptype"] == fallback_label), None)
    if existing_fallback:
        keep = [r for r in keep if r["ptype"] != fallback_label]
        rollup["n_parcels"]    += existing_fallback["n_parcels"] or 0
        rollup["n_up"]         += existing_fallback["n_up"] or 0
        rollup["n_down"]       += existing_fallback["n_down"] or 0
        rollup["n_flat"]       += existing_fallback["n_flat"] or 0
        rollup["total_mv25_b"] = round(rollup["total_mv25_b"] + (existing_fallback["total_mv25_b"] or 0), 3)
        rollup["total_mv26_b"] = round(rollup["total_mv26_b"] + (existing_fallback["total_mv26_b"] or 0), 3)
    return keep + [rollup]


# ── Error monitoring (Sentry) ─────────────────────────────────────────────────
# Cowork brief "Error Monitoring (Sentry) + Rate Limiting (Flask-Limiter)",
# July 2026. Initialized BEFORE the Flask app is created (sentry_sdk's own
# recommended pattern -- FlaskIntegration hooks into app/request machinery
# that must exist by the time the first request comes in, and initializing
# earlier rather than later avoids any chance of an early error escaping
# uncaptured). DSN comes ONLY from the SENTRY_DSN environment variable
# (config.py's own os.environ.get(), no hardcoded fallback) -- if it isn't
# set (e.g. local dev without it exported), initialization is skipped
# entirely rather than erroring or silently pointing at a placeholder
# project. traces_sample_rate is deliberately low (0.1) -- we only need
# error capture right now, not full performance/request tracing; a future
# brief can raise this (or add profiles_sample_rate) if that's wanted later.
if config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.1,
        # send_default_pii=False (the default) -- no request body/headers/
        # user-identifying data sent to Sentry beyond what FlaskIntegration
        # captures for stack traces, consistent with this project's own
        # data-handling posture elsewhere (no third-party data sharing
        # beyond what's needed to operate the product).
    )
    print(f"  Sentry error monitoring: ENABLED (DSN configured via SENTRY_DSN)")
else:
    print(f"  Sentry error monitoring: DISABLED (SENTRY_DSN not set)")


app = Flask(__name__)
app.secret_key = config.FLASK_SECRET

# ── Rate limiting (Flask-Limiter) ─────────────────────────────────────────────
# Same brief as the Sentry block above. In-memory storage (Flask-Limiter's
# default) -- fine at this scale, but NOTE: this resets on every app restart
# (a restart effectively gives everyone a fresh quota) and does NOT
# coordinate across multiple app instances/workers (each process keeps its
# own independent counters, so the REAL effective limit under N gunicorn
# workers is N times the configured number, not the configured number). If
# this app is ever scaled to multiple processes/instances, this needs a
# shared backend (Redis, per Flask-Limiter's own docs: storage_uri=
# "redis://...") instead of the in-memory default used here.
#
# Global default (200/hour/IP) covers every route not given a tighter
# limit below. Tighter limits applied to the routes actually confirmed
# DB/network-heavy by reading their handlers (not guessed):
#   - "/" and "/parcel/<geo_id>": the core parcel-search and parcel-detail
#     paths named explicitly in the brief -- each does at least one real
#     DB query, "/parcel/<geo_id>" several (parcel, history, entity_detail,
#     delinquent, metrics, benchmark).
#   - "/parcel/<geo_id>/export.pdf": everything property_detail() does,
#     plus PDF rendering -- strictly heavier than the page itself.
#   - "/api/address_search": a typeahead endpoint, designed to be called on
#     near-every keystroke by legitimate users -- rate-limited per MINUTE
#     rather than per hour so normal typing isn't throttled, while still
#     capping abusive scripted hammering.
#   - "/api/billing/<geo_id>": explicitly documented in its own docstring
#     as a 5-7 SECOND external fetch to the county tax portal on first
#     call per parcel -- the single most expensive and most externally-
#     abusive-if-hammered endpoint in this app.
#   - "/api/geocode/<geo_id>": also makes an external network call (US
#     Census geocoder) on a cache miss -- same category of risk as billing,
#     lower expected latency.
#   - "/snapshot", "/snapshot/neighborhood/<code>", "/api/benchmark",
#     "/api/peer_set/<geo_id>", "/api/peer_benchmark_local/<geo_id>",
#     "/api/peer_benchmark_sf/<geo_id>", "/api/search_filter", "/compare":
#     aggregate/multi-row/multi-parcel queries -- "/api/peer_set/<geo_id>"
#     specifically had its own documented slowdown investigation earlier in
#     this project (PEER_SET_DISTRIBUTION_CHECK.sql), confirming this class
#     of endpoint is genuinely more expensive than a static page.
#
# Left at the global default only (read, not DB-heavy, or already fast):
# "/search" (confirmed: renders a static coverage-map page, no query at
# all), "/about", "/info", "/styleguide", "/api/benchmark/meta" (metadata,
# not the aggregation itself), "/api/news", "/rates", "/api/rates",
# "/api/parcel_entities", "/parcels". Also "/terms", "/privacy",
# "/disclaimer" (added later, "Terms of Service..." Cowork brief) -- static
# legal content, same category as /about.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)
_LIMIT_HEAVY     = "60 per hour"    # DB-heavy: multi-query pages, aggregates, PDF export
_LIMIT_EXTERNAL  = "30 per hour"    # makes an external network call (portal scrape, geocoder)
_LIMIT_TYPEAHEAD = "60 per minute"  # high legitimate call frequency (per-keystroke), so per-minute not per-hour


# ── Rate limit exemption allowlist (RATE-LIMIT-EXEMPT-1, Aug 2026) ────────────
# Real incident: Diego got 429'd on his own production site by _LIMIT_HEAVY
# during a live-testing session -- his real browser IP shares whatever
# bucket that session's traffic consumed. config.RATE_LIMIT_EXEMPT_IPS
# (env var RATE_LIMIT_EXEMPT_IPS, comma-separated) holds the allowlist.
#
# MECHANISM CHOSEN: @limiter.request_filter, not exempt_when= on individual
# @limiter.limit(...) decorators. Investigated both (Flask-Limiter >=3.5,
# the version pinned in requirements.txt, supports both):
#   - exempt_when= is per-decorator -- would need to be repeated at every
#     one of the ~13 @limiter.limit(_LIMIT_HEAVY/_LIMIT_EXTERNAL/
#     _LIMIT_TYPEAHEAD) call sites in this file, AND would still leave the
#     200/hour default_limits (which applies to every route with no
#     explicit decorator) unexempted, since default_limits has no
#     decorator to attach exempt_when= to at all.
#   - request_filter is evaluated once per request, before ANY limit
#     (default_limits or route-specific) is checked -- one function,
#     applied uniformly to every tier automatically, by construction, no
#     per-route repetition and no risk of a future new @limiter.limit(...)
#     call site accidentally being added without the exemption attached
#     (exactly the kind of partial-enforcement gap that makes a decorator-
#     by-decorator approach fragile as this file grows).
#
# SCOPE DECISION: exempt ALL tiers (_LIMIT_HEAVY, _LIMIT_EXTERNAL,
# _LIMIT_TYPEAHEAD, and the 200/hour default), not just _LIMIT_HEAVY.
# Reasoning: Diego's actual use case is live-testing the whole site via an
# automated browser session -- that traffic isn't confined to _LIMIT_HEAVY
# routes alone, it hits typeahead (_LIMIT_TYPEAHEAD), billing/geocode
# (_LIMIT_EXTERNAL), and everything under the default too. Exempting only
# _LIMIT_HEAVY would leave him getting 429'd on, say, the address typeahead
# mid-session -- not actually solving the real problem this brief exists
# for. request_filter's all-tiers-uniformly behavior is a natural fit for
# that, not an over-broad accident.
#
# REAL CLIENT IP -- Render sits behind its own edge/load balancer (and
# Cloudflare in front of that, per Render's own community docs); this
# app's `request.remote_addr` (what get_remote_address(), the limiter's
# key_func, actually reads) is very likely RENDER'S PROXY IP, not the
# real client's, on production -- there is no ProxyFix middleware or
# gunicorn --forwarded-allow-ips configured anywhere in this codebase
# (confirmed via grep) to translate X-Forwarded-For into remote_addr.
# THIS DOES NOT BREAK THE EXEMPTION BELOW: request_filter short-circuits
# rate-limit evaluation entirely for the whole request BEFORE key_func is
# ever invoked, so this function's own IP detection (below) is what
# matters for the exemption, independent of whatever the limiter's
# key_func sees or doesn't see correctly. It DOES mean the limiter's
# real-world bucketing (unrelated to this brief, out of scope to fix
# here per "do not... rearchitecture") may currently be grouping ALL
# production traffic under one shared proxy-IP bucket rather than one
# bucket per real visitor -- flagged in this task's final report as a
# separate, real, pre-existing finding worth its own investigation, not
# silently ignored just because it isn't this brief's job to fix.
#
# _get_client_ip() below reads X-Forwarded-For (Render/Cloudflare's own
# edge sets this, not directly attacker-controlled input reaching this
# app -- a client can't bypass Render's edge to talk to this process
# directly) and takes the LAST entry, not the first -- the standard
# "trust exactly one hop of your own known reverse proxy" convention
# (same rule Werkzeug's ProxyFix implements with x_for=1): if a request
# somehow arrived with a client-supplied X-Forwarded-For already present,
# the proxy appends its own observed value as the last entry, so trusting
# the last entry (not the first) doesn't hand control of "which IP does
# this look like" to arbitrary request input. Falls back to
# request.remote_addr when the header is absent (e.g. local dev, or if
# Render's proxy topology turns out not to set it the way assumed here).
# NOT independently verified against Render's actual live proxy behavior
# from this sandbox -- see this task's final report for what Diego needs
# to confirm live.
def _get_client_ip():
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "127.0.0.1"


@limiter.request_filter
def _rate_limit_exempt_ip():
    return _get_client_ip() in config.RATE_LIMIT_EXEMPT_IPS


if config.RATE_LIMIT_EXEMPT_IPS:
    print(f"  Rate limit exemption: ENABLED for {len(config.RATE_LIMIT_EXEMPT_IPS)} "
          f"IP(s): {', '.join(sorted(config.RATE_LIMIT_EXEMPT_IPS))}")
else:
    print(f"  Rate limit exemption: none configured (RATE_LIMIT_EXEMPT_IPS not set)")


# ── Homeowner / Investor mode ─────────────────────────────────────────────────
_MODES = ("homeowner", "investor")
_MODE_COOKIE = "parcelytics_mode"
_MODE_DEFAULT = "investor"


def _resolve_mode():
    """URL ?mode= overrides the cookie; cookie overrides the default."""
    m = (request.args.get("mode") or "").strip().lower()
    if m in _MODES:
        return m
    c = (request.cookies.get(_MODE_COOKIE) or "").strip().lower()
    return c if c in _MODES else _MODE_DEFAULT


@app.context_processor
def inject_mode():
    # Cowork brief "Version Display + Single Source of Truth", July 2026:
    # also inject `config` here so templates can read config.VERSION (and
    # any other config constant) without each route having to pass it
    # through render_template() individually.
    #
    # appraisal_badge_kind added Aug 2026 (URGENT dcad_certified fix): lets
    # property.html call _appraisal_badge_kind() directly instead of
    # maintaining its own duplicate CERTIFIED_TIER_DATA_SOURCES list --  see
    # that function's docstring for why the duplication existed and why it
    # caused a live bug. NOTE this function is defined further down in this
    # file (near CERTIFIED_TIER_DATA_SOURCES); Python resolves the name at
    # call time, not at inject_mode()'s definition time, so the forward
    # reference here is safe.
    return {"mode": _resolve_mode(), "config": config, "appraisal_badge_kind": _appraisal_badge_kind}


@app.after_request
def persist_mode(resp):
    """When ?mode= is present and valid, remember it for 30 days."""
    m = (request.args.get("mode") or "").strip().lower()
    if m in _MODES:
        resp.set_cookie(_MODE_COOKIE, m, max_age=30 * 24 * 3600, samesite="Lax")
    return resp


# ── DB helper ─────────────────────────────────────────────────────────────────
# statement_timeout safety net (July 2026, per Cowork's "confirm root cause and
# propose fix" investigation into the WORKER TIMEOUT/SIGKILL Sentry incidents):
# every connection this app opens goes through get_db() -- query() calls it,
# and the two routes that manage their own cursor (api_geocode(),
# api_billing()) call it directly too (confirmed via grep: no other
# psycopg2.connect() call exists anywhere in app.py) -- so setting
# statement_timeout here, once, at connection time covers every query this
# web app runs with no per-call-site duplication. (query_no_nestloop() also
# called it, historically -- retired, Task AGGPRECOMP-2, see the comment
# where it used to be defined, just above normalize_parcel_id().)
#
# 8000ms (8s): comfortably above the worst FIXED query time already measured
# in this codebase (the Market Snapshot neighborhoods query, 2393ms post-
# query_no_nestloop()-fix, back when that query still ran live -- see
# on/off measurement history), and well under gunicorn's 30s worker timeout
# (Start Command is `gunicorn app:app`, no --timeout flag, confirmed against
# Render's dashboard directly -- the plain default is in effect). This is a
# pure safety net, independent of which endpoint turns out to be the actual
# cause of the current SIGKILL incidents: a query that legitimately regresses
# (stale stats, planner misjudgment, data growth) now fails fast with a
# clean, catchable Postgres error instead of silently consuming an entire
# worker's timeout budget.
#
# Scope note (flagged, not silently assumed away): get_db() is NOT the only
# connection helper in this repo. loaders/db.py's own get_conn() is a
# separate, independent choke point used by all 21 loader scripts under
# loaders/ (compute_metrics.py, load_certified_2025.py, load_2026_
# preliminary.py, etc.) -- deliberately untouched here. Those scripts
# legitimately run bulk upserts over hundreds of thousands of rows
# (batch_upsert() in loaders/db.py) that can genuinely take far longer than
# 8 seconds; capping them at 8s would break real, intended-to-be-slow work,
# not catch a regression. A handful of standalone one-off investigation
# scripts at the repo root (query_2026_vs_2025.py, verify_ajr_fix.py,
# review_check.py, and similar) also hand-roll their own psycopg2.connect()
# -- these are manual dev tools never invoked by the running web app or by
# gunicorn, so they're outside this fix's scope for the same reason: this
# fix targets the live request path specifically, not every script in the
# repo that happens to open a database connection.
def get_db():
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS,
        options="-c statement_timeout=8000",
    )


def query(sql, params=None, one=False, timeout_ms=None):
    """
    PX-20260828-14: added timeout_ms, additive-only (default None => identical
    behavior to before this change -- the connection still runs under
    get_db()'s own 8000ms statement_timeout, set once at connect time).

    When timeout_ms is given, this session's statement_timeout is tightened
    (or loosened) to that value for the query that follows, on this
    connection only (each query() call opens and closes its own connection,
    per get_db()/finally: conn.close() above, so there's no bleed into any
    other call). This exists so a caller can give ONE specific query its own,
    independent timeout budget distinct from the connection-wide default --
    e.g. a secondary query that's allowed to fail fast and be degraded around,
    rather than either inheriting the full 8s budget or taking down the whole
    request if it regresses.

    Deliberately NOT using a %s bind parameter for the timeout value (`SET
    statement_timeout = %s`) -- PostgreSQL's SET is a utility statement and
    does not accept a parameterized value the way a normal DML/SELECT does;
    psycopg2 sends it through the extended query protocol like any other
    execute() and it fails with a syntax error. timeout_ms is never
    user-supplied (every call site in this codebase passes a hardcoded
    literal), so formatting it into the SQL text after an int() cast is safe
    -- int() itself is the guard against anything unexpected reaching here.

    PX-20260829-04 (Task 1 instrumentation): every call now records its own
    wall-clock cost -- split into connect_ms (get_db()'s psycopg2.connect()
    call specifically, isolated from the query itself) and exec_ms (cursor
    execute+fetch) -- onto g._diag_db_calls, a per-REQUEST list, when running
    inside a real Flask request (has_request_context()). This is additive
    only: every existing call site's return value and behavior is byte-for-
    byte unchanged; a caller that never reads g._diag_db_calls (i.e. every
    caller today except the two typeahead endpoints below) sees no
    difference at all. Exists so a single request's DB time can be measured
    and reported separately from "everything else" (Flask-Limiter, JSON
    serialization, Python-side loop overhead) -- the exact breakdown
    PX-20260829-04's brief asks for -- and, just as importantly, so the
    NUMBER of separate connect() calls one request makes is visible: this
    codebase opens a brand-new physical connection per query() call (no
    pooling anywhere in get_db()), so a code path that calls query() N times
    pays N separate connection-setup costs, not one.
    """
    _t0 = time.perf_counter()
    conn = get_db()
    _t_connected = time.perf_counter()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if timeout_ms is not None:
                cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params or ())
            result = cur.fetchone() if one else cur.fetchall()
            return result
    finally:
        conn.close()
        if has_request_context():
            _t_done = time.perf_counter()
            calls = g.setdefault("_diag_db_calls", [])
            calls.append({
                "connect_ms": round((_t_connected - _t0) * 1000, 2),
                "exec_ms":    round((_t_done - _t_connected) * 1000, 2),
                "sql_head":   " ".join(sql.split())[:60],
            })


# query_no_nestloop() -- RETIRED (Task AGGPRECOMP-2, Aug 2026). Used to live
# here: a query() variant applying a transaction-scoped `SET LOCAL
# enable_nestloop = off` to the three/four Market Snapshot queries in
# _compute_snapshot_data() that each joined parcel_tax_year twice (2025 +
# 2026) and hit a specific, measured Postgres 15 planner misjudgment on that
# second join (see task_staging/snapshot_perf/check_snapshot_nestloop_off.command
# for the full on/off EXPLAIN ANALYZE evidence that justified it at the
# time: breakdown 480-1489ms off vs 3008-9644ms on; Part 4 aggregate
# 299-535ms off vs 974-2491ms on; neighborhoods 361-362ms off vs 2382-2393ms
# on). Removed as dead code per SPEC_AGGREGATE_PRECOMPUTATION.md's own
# explicit retirement instruction ("each query migrated into Tier 1 or
# Tier 3 should have its query_no_nestloop() call site removed as part of
# that migration") once _compute_snapshot_data() was rewired to read
# loaders/refresh_snapshot_summary.py's precomputed summary tables instead
# of running these queries live -- confirmed via grep, at the time of
# removal, that its 4 real call sites were ALL inside that one function,
# and all 4 were removed by that same rewire, leaving zero callers. The
# override itself still runs, just once per refresh (~10x/year) inside
# loaders/refresh_snapshot_summary.py's own _compute_one_view(), not once
# per live request -- see that function's docstring for the reapplication.


# ── Search normalisation ──────────────────────────────────────────────────────
def normalize_parcel_id(raw: str) -> str:
    """
    Accept several input formats and return the 10-char TCAD geo_id:
      - 10-char long account:  '0100030105'       → '0100030105'
      - 14-char tax-office:    '01000301050000'   → '0100030105'
      - short integer:         '100008'            → looked up via prop_id
    """
    s = raw.strip().replace("-", "").replace(" ", "")
    if len(s) == 14 and s.isdigit():
        return s[:10]       # strip trailing 4 zeros
    return s                # return as-is; SQL will handle the lookup


# ── Shared search functions ──────────────────────────────────────────────────
# Cowork brief "Search overhaul — Phase 2 go-ahead", July 2026 (D2/D3). These
# two functions are the ONE place account-number resolution and address-text
# matching happen — both index() (full-results submit handler, below) and
# api_address_search() (the typeahead endpoint) call these identically.
# Previously these were two independent, slowly-diverging copies (the
# typeahead excluded AJR* geo_ids, the submit handler didn't — see D3 below);
# that class of bug can't recur if there's only one implementation to call.

def resolve_prop_id_to_geo_id(prop_id):
    """
    Resolve a prop_id to its geo_id — the ONE shared prop_id fallback,
    used by both call sites below (Migration M2,
    SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.5).

    Looks in `prop_unit` FIRST, not `parcel`. Before this migration, a
    prop_id search only ever worked if that prop_id happened to be the one
    that won parcel's `ON CONFLICT (geo_id) DO UPDATE` for its geo_id —
    every other unit sharing that geo_id (e.g. a condo regime's non-primary
    units) had no row anywhere with THAT prop_id in the `prop_id` column,
    so searching for it silently found nothing. `prop_unit` now contains
    every prop_id ever loaded, each pointing at its real geo_id, so this
    fallback now actually finds every unit, not just whichever one
    happened to load last. Falls back to `parcel` directly only as a
    defensive last resort (e.g. before any M2 loader has run against a
    given environment) — should not be the normal path once migration
    data is loaded.
    """
    # DALLAS-GATE-1 Part 2: county_code now threaded through both lookups
    # below via g.county_code (set per-request by _pull_county_slug, see
    # the county-routing block further down this file). Both prop_unit and
    # parcel have county_code as the LEADING column of their real live PK
    # (migrate_county_partitioning.py TABLE_SPECS) — filtering by it here
    # is required for these to stay index-covered lookups post-partition,
    # not just correctness (see POST-PARTITION-INCIDENT-1-AUDIT).
    row = query(
        "SELECT geo_id FROM prop_unit WHERE prop_id = %s AND county_code = %s",
        (prop_id, g.county_code), one=True)
    if row:
        return row["geo_id"]
    row = query(
        "SELECT geo_id FROM parcel WHERE prop_id = %s AND county_code = %s",
        (prop_id, g.county_code), one=True)
    return row["geo_id"] if row else None


def resolve_exact_parcel(q, county_code=None):
    """
    Try to resolve `q` as an exact TCAD account number / prop_id — the same
    numeric-first behavior index() has always had (10-char geo_id, 14-char
    Tax Office account, or short prop_id integer). Returns a dict (geo_id,
    situs_address, owner_name, ...) or None. Used by BOTH index() and
    api_address_search(), so a typed account number now resolves identically
    from the navbar typeahead as it does from the full-results submit path.

    PX-20260827-03-rev1: county_code is now an explicit optional param
    rather than an implicit g.county_code read, so the neutral home page
    (Task 1 -- bare '/', no county in the URL, g.county_code unset) can
    search "any live county from anywhere" (Diego's ruling (b)) instead of
    only ever finding a Travis parcel. Resolution order: explicit
    county_code arg (unchanged behavior for every existing county-anchored
    call site) > g.county_code (same fallback every other call site in
    this file already relies on) > loop over every county in
    _live_counties() (only reached from a genuinely neutral request with
    no county context at all -- e.g. the bare home page)."""
    target_county = county_code or getattr(g, "county_code", None)
    geo_id = normalize_parcel_id(q)
    if target_county:
        parcel = query(
            "SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s",
            (geo_id, target_county), one=True)
        if not parcel and q.isdigit():
            fallback_geo_id = resolve_prop_id_to_geo_id(int(q))
            if fallback_geo_id:
                parcel = query(
                    "SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s",
                    (fallback_geo_id, target_county), one=True)
        return dict(parcel) if parcel else None

    # No county context at all -- neutral request. Try every live county in
    # turn; geo_id is only unique WITHIN a county (PARTITION-1's own
    # finding), so this cannot be collapsed into one county_code-less query.
    for entry in _live_counties():
        found = resolve_exact_parcel(q, county_code=entry["county_code"])
        if found:
            return found
    return None


def search_parcels_by_address(q, limit=8, county_code=None):
    """
    The one shared address-text matcher (D2). Algorithm, informed by Phase
    1's finding that there is no queryable city column and zip_code is 0%
    populated (city/zip, when present at all, are free text embedded inside
    situs_address, present only ~38% of the time in the one AJR year
    inspectable in this sandbox, with real spelling drift in the source
    data itself):

      1. search_logic.normalize_query_tokens() — uppercase, strip
         commas/periods, collapse whitespace, drop standalone TX/TEXAS.
      2. Try the full token string as a substring of situs_address (this
         alone preserves every search that already works today).
      3. On zero matches, progressively drop the trailing token and retry
         (search_logic.address_match_attempts) — each dropped token becomes
         a "boost token" — down to a 2-token floor.
      4. Rank matches (search_logic.rank_candidates): rows whose
         situs_address contains more boost tokens first, then prefix
         matches, then alphabetical.

    geo_id NOT LIKE 'AJR%%' is applied at every attempt (D3 — see the brief
    response; matches the CANONICAL_PARCEL_EXCL convention already used at
    ~8 other call sites in this file for "real, situs-addressable property
    only", now made consistent here too instead of only in the old
    typeahead). City/zip are NEVER a hard filter anywhere in this function —
    a row missing a city token (like 3411 Bridle Path's own situs_address)
    or a user's wrong zip still surfaces via its street-level match; boost
    tokens only affect ranking, never inclusion.

    ORDER BY before LIMIT 200 (post-review fix, July 2026): with no ORDER
    BY, Postgres returns an arbitrary, undefined 200 rows whenever a
    pattern matches more than 200 parcels countywide (e.g. a bare "CAMERON
    RD" attempt) — rank_candidates() below would then be ranking a random
    subset of the real candidate pool, silently able to drop the actual
    best match, and identical repeated searches could return different
    results run to run. Prefix matches are sorted into the pool first
    (same bias the old standalone typeahead endpoint used, restored here so
    a >200-match pattern doesn't lose its exact-prefix rows to arbitrary
    ordering before rank_candidates() even sees them), then alphabetical
    purely for determinism. rank_candidates()'s own boost-token ranking
    runs on top of this pool afterward and is unchanged.
    """
    # PX-20260827-03-rev1: same county-context resolution order as
    # resolve_exact_parcel() above -- explicit arg > g.county_code > (for a
    # genuinely neutral request, e.g. the bare home page) every live
    # county in turn. Every returned row gets a county_slug alongside its
    # existing fields so a caller with no g.county_slug of its own (the
    # neutral home page) can still build a correct, non-defaulted link to
    # whichever county the match actually lives in -- _add_county_slug()'s
    # own DEFAULT_COUNTY_SLUG fallback would otherwise silently point every
    # result at Travis regardless of where the match was actually found.
    #
    # PX-20260829-04 Task 1 (measured-diagnosis fix, sign-off given): this
    # branch used to recurse into itself once PER LIVE COUNTY, serially --
    # each recursive call could itself issue up to (token count - 1)
    # further serial query() calls (address_match_attempts()'s own
    # progressive token-drop retry), and every single query() call opens a
    # brand-new physical DB connection (get_db() has no pooling). For 2 live
    # counties and a typical 4-token address with no match at full
    # specificity, that was up to 6 serial connections for ONE keystroke,
    # BEFORE _live_counties() itself (below) added 3 more. Rewritten to run
    # ONE query per token-drop attempt, scoped to every live county at once
    # via `county_code = ANY(%s)`, instead of one query per (county x
    # attempt) combination -- caps this branch at (token count - 1) queries
    # total regardless of how many counties are live, and that number only
    # gets BETTER as more counties (Harris, etc.) come online, not worse.
    # Also fixes a latent correctness quirk in the old per-county loop: a
    # full-specificity match in county B was previously delayed behind
    # county A's ENTIRE token-drop cascade failing first (recursion tried
    # counties one at a time, each running its own complete cascade before
    # moving on) -- the combined query now finds whichever county matches
    # at a given specificity level in the same pass, for every county,
    # every time.
    target_county = county_code or getattr(g, "county_code", None)
    if target_county is None:
        live = _live_counties()
        if not live:
            return []
        live_codes = [entry["county_code"] for entry in live]
        code_to_slug = {entry["county_code"]: entry["slug"] for entry in live}
        tokens = search_logic.normalize_query_tokens(q)
        if not tokens:
            return []
        for pattern_tokens, boost_tokens in search_logic.address_match_attempts(tokens):
            pattern = " ".join(pattern_tokens)
            rows = query("""
                SELECT geo_id, situs_address, owner_name, county_code
                FROM   parcel
                WHERE  UPPER(situs_address) ILIKE %(pattern)s
                  AND  geo_id NOT LIKE 'AJR%%'
                  AND  county_code = ANY(%(county_codes)s)
                ORDER  BY
                    CASE WHEN UPPER(situs_address) LIKE %(prefix_pattern)s THEN 0 ELSE 1 END,
                    situs_address
                LIMIT  200
            """, {"pattern": f"%{pattern}%", "prefix_pattern": f"{pattern}%",
                  "county_codes": live_codes})
            if rows:
                ranked = search_logic.rank_candidates(
                    [dict(r) for r in rows], boost_tokens, pattern_tokens
                )
                for r in ranked:
                    r["county_slug"] = code_to_slug.get(r["county_code"], DEFAULT_COUNTY_SLUG)
                    r["county_name"] = COUNTY_PROFILES.get(
                        r["county_code"], COUNTY_PROFILES["TRAVIS"]
                    )["county_name"]
                return ranked[:limit]
        return []

    county_slug = next((slug for slug, code in COUNTY_SLUGS.items() if code == target_county), DEFAULT_COUNTY_SLUG)
    # PX-20260828-03 Task 2: county_name alongside county_slug, same
    # per-call derivation, same reason -- a NEUTRAL search (county_code=None
    # above) loops over every live county and concatenates results, so a
    # single site-wide county_name (the shape api_address_search() used
    # before this task, safe there only because that endpoint is always
    # mono-county) would be wrong here; it has to be stamped per-row, once
    # per county this function is actually querying. Consumed by the
    # shared addr_match_results() macro (templates/_macros.html) to render
    # the same "(Dallas County)"-style tag static/parcel-typeahead.js's
    # dropdown already shows, and by api_address_search() below (which
    # calls this same function), replacing that endpoint's own separate
    # single-value derivation.
    county_name = COUNTY_PROFILES.get(target_county, COUNTY_PROFILES["TRAVIS"])["county_name"]
    tokens = search_logic.normalize_query_tokens(q)
    if not tokens:
        return []
    for pattern_tokens, boost_tokens in search_logic.address_match_attempts(tokens):
        pattern = " ".join(pattern_tokens)
        # DALLAS-GATE-1 Part 2: county_code added to the WHERE clause. Note
        # this one is NOT an index-coverage fix the way the two lookups
        # above are -- situs_address has no real index either way, this
        # was already a text-pattern scan (see the ORDER-BY-before-LIMIT
        # comment above) -- but it's still a REQUIRED correctness fix: a
        # county-unscoped ILIKE here would return cross-county address
        # matches once Dallas/Harris data exists, silently mixing counties
        # in search results.
        rows = query("""
            SELECT geo_id, situs_address, owner_name
            FROM   parcel
            WHERE  UPPER(situs_address) ILIKE %(pattern)s
              AND  geo_id NOT LIKE 'AJR%%'
              AND  county_code = %(county_code)s
            ORDER  BY
                CASE WHEN UPPER(situs_address) LIKE %(prefix_pattern)s THEN 0 ELSE 1 END,
                situs_address
            LIMIT  200
        """, {"pattern": f"%{pattern}%", "prefix_pattern": f"{pattern}%", "county_code": target_county})
        if rows:
            ranked = search_logic.rank_candidates(
                [dict(r) for r in rows], boost_tokens, pattern_tokens
            )
            for r in ranked:
                r["county_slug"] = county_slug
                r["county_name"] = county_name
            return ranked[:limit]
    return []


# ── County-in-URL routing (DALLAS-GATE-1, Part 1/2) ────────────────────────────
# Real, decided input (Diego, 2026-08-15): county routing is a URL PATH
# SEGMENT -- e.g. /travis-tx/parcel/<geo_id> -- not inferred-from-geo_id, not
# subdomain-based. Slug format CONFIRMED against real, live evidence (Diego's
# own call, same session): "travis-tx" -- matching the slug already shipped
# and rendered today in templates/index.html's MARKETS array (data-market
# attributes, homepage coverage map JS) -- NOT "travis-county" (the shape
# used only as a loose example in the brief and in Fable's one relayed,
# never-independently-confirmed growth-plan note; no separate growth-plan/
# programmatic-SEO document exists in this repo or in the connected Notion
# workspace to verify that shape against). Diego's own stated reasoning for
# travis-tx: the state suffix disambiguates same-named counties across
# states (a real, correct concern -- e.g. "Dallas County" also exists in AL,
# AR, IA, MO, TX).
#
# COUNTY_SLUGS was originally seeded with dallas-tx/harris-tx while only
# travis-tx was live -- costs nothing to register early (an unrecognized
# slug 404s either way) and means the URL scheme itself never needs a
# second decision when real county data eventually loads. Loading that
# data was gated behind DATA_LIFECYCLE.md's own onboarding prerequisites
# (Source Registry, County Profile, Classification Map) -- registering a
# slug string here was never the same as onboarding a county, and doesn't
# shortcut that gate.
#
# UPDATE (Aug 2026, URGENT dcad_certified live-correctness fix): Dallas is
# NO LONGER "reserved" -- Diego confirmed it went live that night, all 5
# years (2022-2026) loaded and verified via live psql row counts. This
# comment (and the loader's own docstring, and this file's dcad-loaded-yet
# framing generally) had gone stale the moment that load happened, since a
# DB write leaves no commit here -- that staleness directly contributed to
# CERTIFIED_TIER_DATA_SOURCES shipping without "dcad_certified" in it and
# nobody noticing until Diego caught the live badge himself. Harris is
# still genuinely unloaded as of this fix (no live Harris evidence found
# anywhere in this repo) -- don't assume the same silently-went-live
# pattern applies there without confirming with Diego first.
COUNTY_SLUGS = {
    "travis-tx": "TRAVIS",
    "dallas-tx": "DALLAS",   # LIVE (Aug 2026) -- 2022-2026 all loaded, see update above
    "harris-tx": "HARRIS",   # reserved -- no real data loaded yet, see above
}
DEFAULT_COUNTY_SLUG = "travis-tx"


# PX-20260824-01: the county-profile-of-record for site COPY (institutional
# names, source-attribution strings), as distinct from COUNTY_SLUGS above
# (which is purely a routing table). Every field here is copied verbatim
# or mechanically concatenated from Notion's real "County Public Profile
# (Website Content Template)" database -- queried directly for this brief,
# not guessed or invented. Travis is "Live" there; Dallas is "In Prep" (its
# row exists with real CAD/Tax-Office names already filled in, confirming
# DALLAS-CLASS-1-rev's own DCAD naming) but several URL fields are still
# genuinely blank in Notion for Dallas -- left as None here rather than
# guessed, per this brief's explicit "no wording/URLs invented" rule. A
# None field means: don't render that link for this county yet; that's a
# real, honest gap, not a bug to paper over.
#
# PX-20260827-01: the fields below (cad_property_search_url through
# cad_contact_url) close the exact gap this docstring used to flag as
# deliberately unclosed. Their provenance is DIFFERENT from the fields
# above: Notion has no corresponding columns for these (still true), so
# these were NOT pulled from Notion. Instead each URL was independently
# verified by live web research against the real CAD site (WebSearch +
# direct page fetch, cross-checked against multiple independent hits with
# matching page titles/content) as part of this brief's explicit mandate to
# do that per-county URL research rather than defer it again. Every value
# is either a real, confirmed, currently-live URL, or None where research
# could not confirm one -- per this brief's rule, a genuinely unknown URL
# renders as absent, never a guessed pattern and never a fallback to
# another county's URL.
#
# "_template" fields contain a literal "{prop_id}" placeholder substituted
# at render time with the parcel's own prop_id -- never another county's ID
# shape. A template that is None (Dallas has none yet for the interactive
# map) means: render the non-parcel-specific fallback, or omit entirely if
# no fallback exists either.
#
# DALLAS-SPECIFIC NOTES (so a future editor doesn't "fix" these as bugs):
#   - cad_parcel_detail_url_template uses dallascad.org's confirmed-real
#     AcctDetailRes.aspx?ID=<account_number> pattern (the "Res" suffix is
#     DCAD's own naming for its residential account-detail page; no
#     separate commercial-account URL shape has been confirmed, so this
#     template is applied uniformly and may not resolve for non-residential
#     Dallas accounts -- a disclosed limitation, not a guess).
#   - cad_homestead_exemption_url_template points at a DIFFERENT, confirmed
#     real subdomain (hstead.dallascad.org) DCAD uses specifically for
#     homestead applications -- not a guess; Dallas has no single top-level
#     homestead URL the way traviscad.org's root conveniently doubles as.
#   - cad_protest_url_template reuses the same AcctDetailRes.aspx page,
#     because DCAD's real uFile protest system is launched FROM that
#     per-account page (confirmed: "search your account, then select uFile
#     Online Protest") -- there is no separate standalone protest-portal
#     URL to point to. This is a disclosed design choice, not a guess of a
#     URL that doesn't exist; flagged in the PX-20260827-01 report for the
#     PM to override if a "not yet available" rendering is preferred instead.
#   - tax_office_website is Dallas COUNTY's own tax-office payment domain
#     (dallascounty.org/tax), confirmed via DCAD's own "Paying Taxes" page
#     content -- distinct from dallascad.org, mirroring Travis's own
#     pattern of a separate tax-office domain from the CAD's domain.
#   - cad_interactive_map_url remains None: research surfaced several
#     candidate map-tool domains (maps.dcad.org, esearch.dallascad.org)
#     without independent confirmation strong enough to trust, so this
#     stays unset rather than guessed -- unchanged from the prior brief.
#
# PX-20260827-02 (verification brief) UPDATES to the notes above:
#   - cad_interactive_map_url is NO LONGER None: maps.dcad.org/prd/dpm/ was
#     independently live-verified this brief (real DCAD Property Map tool,
#     linked from DCAD's own site nav as "Find Property on Map"). It is
#     ALSO deep-linkable per-parcel via "?parcelid=<ACCOUNT_NUM>" (verified
#     live against a real account -- correct address/owner/value rendered)
#     but that deep-link capability is NOT wired into COUNTY_PROFILES or any
#     template here, since doing so is outside this brief's scope (see the
#     PX-20260827-02 report) -- a real, disclosed opportunity for a
#     follow-up brief, not a guess.
#   - FIXED in PX-20260827-02b (was: CONFIRMED BUG, disclosed but
#     deliberately not fixed in PX-20260827-02 -- outside that brief's
#     scope): cad_parcel_detail_url_template, cad_homestead_exemption_url_
#     template, and cad_protest_url_template were all called from
#     property.html with parcel.prop_id, but DCAD's real "ID=" parameter
#     requires the literal ACCOUNT_NUM string (== this schema's geo_id),
#     not prop_id. Per REV1's derive_prop_id_geo_id() (dcad_format.py): for
#     an all-digit ACCOUNT_NUM, prop_id is int(ACCOUNT_NUM), so the old
#     str(prop_id) substitution silently dropped leading zeros --
#     live-verified in PX-20260827-02 to break the deep link (DCAD returned
#     "Error in Account Details: Account Number is missing or in wrong
#     format" for the zero-stripped form). For an alphanumeric ACCOUNT_NUM
#     (~25% of real Dallas accounts), prop_id is instead a synthetic
#     SHA-256-derived integer disjoint from any real account number, so the
#     substituted URL could not resolve at all. Travis was NOT affected --
#     see county_cad_link()'s docstring for exactly why (Travis's prop_id
#     is a genuine TCAD-native id read off PROP.TXT, not a value we
#     synthesize the way Dallas's is). FIX: the three Dallas fields above
#     now use a "{geo_id}" placeholder instead of "{prop_id}"; every real
#     call site in property.html now passes both parcel.prop_id and
#     parcel.geo_id to county_cad_link(), which substitutes whichever
#     placeholder(s) the requested field's URL actually contains. NOT YET
#     confirmed live by Diego as of this fix landing -- see the
#     PX-20260827-02b report for the required live spot-check before this
#     is trusted in production (a URL-substitution bug is exactly the class
#     that can pass every automated check here and still be wrong live).
#   - Also newly confirmed (informational, not yet used anywhere): DCAD
#     serves a SEPARATE commercial-account detail page at
#     "AcctDetailCom.aspx?ID=<account_number>", distinct from
#     AcctDetailRes.aspx (residential). This updates the "no separate
#     commercial-account URL shape has been confirmed" note above -- one
#     now has been, though cad_parcel_detail_url_template here still points
#     only at the residential shape. PX-20260827-02b considered building
#     residential-vs-commercial routing (state_cd1-based) here but left it
#     out: it's not a trivial addition (needs a per-parcel property-type
#     check threaded through every call site, not just a field rename) and
#     wasn't asked for beyond "note as a possible future refinement" --
#     flagging for a future brief rather than scope-creeping this fix.
COUNTY_PROFILES = {
    "TRAVIS": {
        "display_name": "Travis County, TX",
        "county_name": "Travis County",
        "cad_name": "Travis Central Appraisal District",
        "cad_abbr": "TCAD",
        "cad_website": "https://traviscad.org",
        "cad_interactive_map_url": "https://travis.prodigycad.com/maps",
        "cad_property_search_url": "https://traviscad.org/propertysearch/",
        "cad_parcel_detail_url_template": "https://travis.prodigycad.com/property-detail/{prop_id}/2026",
        "cad_homestead_exemption_url": "https://traviscad.org",
        "cad_homestead_exemption_url_template": None,
        "cad_protest_url": "https://traviscad.org/protests",
        "cad_protest_url_template": None,
        "cad_contact_url": "https://traviscad.org/contact",
        "tax_office_name": "Travis County Tax Office",
        "tax_office_website": "https://tax-office.traviscountytx.gov",
        # PX-20260827-03-rev1 Task 1: gates the /info page's substantive
        # legal/exemption content (currently real, researched Texas Tax
        # Code + Travis-specific taxing-entity figures -- not boilerplate).
        # True here because that research has actually been done for
        # Travis. Per Diego's ruling, a county without this flag renders an
        # honest "not available for this county yet" state instead of
        # silently reusing Travis's copy -- same pattern as any other
        # unpopulated profile field (e.g. cad_interactive_map_url = None),
        # not a special case.
        "info_content_available": True,
    },
    "DALLAS": {
        "display_name": "Dallas County, TX",
        "county_name": "Dallas County",
        "cad_name": "Dallas Central Appraisal District",
        "cad_abbr": "DCAD",
        "cad_website": "https://www.dallascad.org",
        "cad_interactive_map_url": "https://maps.dcad.org/prd/dpm/",   # PX-20260827-02 Task 3: confirmed live -- see note above (deep-linkable via ?parcelid=<geo_id>, not wired here; see report)
        "cad_property_search_url": "https://www.dallascad.org/SearchOwner.aspx",
        "cad_parcel_detail_url_template": "https://www.dallascad.org/AcctDetailRes.aspx?ID={geo_id}",   # PX-20260827-02b: {prop_id} -> {geo_id} (see county_cad_link() docstring)
        "cad_homestead_exemption_url": None,
        "cad_homestead_exemption_url_template": "https://hstead.dallascad.org/ns/HomesteadExemption.aspx?ID={geo_id}",   # PX-20260827-02b: {prop_id} -> {geo_id}
        "cad_protest_url": None,
        "cad_protest_url_template": "https://www.dallascad.org/AcctDetailRes.aspx?ID={geo_id}",   # PX-20260827-02b: {prop_id} -> {geo_id}
        "cad_contact_url": "https://www.dallascad.org/contact.aspx",
        "tax_office_name": "Dallas County Tax Office",
        "tax_office_website": "https://www.dallascounty.org/tax",
        # PX-20260827-03-rev1: no Dallas-specific exemption/protest/ag/
        # delinquency legal research has been done yet (that's real,
        # separate content work -- Texas Tax Code sections are state-wide
        # and would carry over, but the county-level sections on /info are
        # genuinely Travis-specific taxing-entity facts that do not).
        # Omitted (falls back to False via .get() at the read site) rather
        # than guessed True -- same "unknown renders as absent" rule this
        # profile already applies to every URL field above.
        "info_content_available": False,
    },
    # HARRIS intentionally NOT registered here yet -- COUNTY_SLUGS reserves
    # the routing slug, but Notion's County Public Profile row for Harris
    # is still "Planned" with real names not yet independently confirmed
    # (per that database's own note). Registering a profile with guessed
    # names would be worse than a template falling back to raw text for a
    # county nobody can reach today anyway (harris-tx has no loaded data).
    # Per PX-20260827-01 Task 4, Harris's registration is also now gated on
    # having the institutional link fields above populated or explicitly
    # None -- not just the pre-existing name/website fields.
}


@app.url_value_preprocessor
def _pull_county_slug(endpoint, values):
    """Runs BEFORE the matched view function, for every real route below
    that declares a leading <county_slug> path segment. Pops county_slug
    out of the URL's own captured values (so view functions never receive
    it as a kwarg -- they read g.county_code instead, the same pattern any
    other Flask app-wide request-context value uses) and resolves it to
    the real county_code every county-keyed query() call site should now
    filter by.

    404s on an unrecognized slug -- deliberately NOT a silent fallback to
    Travis. A wrong/mistyped slug silently serving Travis data instead of
    a 404 would be a real, live correctness hazard (the same class of bug
    this whole DALLAS-GATE-1 brief exists to close off), not just a typo
    someone notices.

    Routes with no <county_slug> segment at all (currently only /healthz,
    a DB-free liveness probe with no county concept) are unaffected --
    values.pop with a default of None makes this a no-op for them."""
    if values is None:
        return
    slug = values.pop("county_slug", None)
    if slug is None:
        return
    county_code = COUNTY_SLUGS.get(slug)
    if county_code is None:
        abort(404)
    g.county_slug = slug
    g.county_code = county_code


@app.url_defaults
def _add_county_slug(endpoint, values):
    """Companion to _pull_county_slug(): auto-injects the CURRENT request's
    county_slug into every url_for() call for an endpoint that expects one,
    so existing call sites (e.g. index()'s own
    `redirect(url_for("property_detail", geo_id=...))` below) keep working
    unchanged -- they get the right county prefix for free instead of each
    needing an individual county_slug=... edit. Falls back to
    DEFAULT_COUNTY_SLUG for url_for() calls made outside a real request
    context that resolved one (e.g. future CLI/script usage)."""
    if "county_slug" in values:
        return
    if app.url_map.is_endpoint_expecting(endpoint, "county_slug"):
        values["county_slug"] = getattr(g, "county_slug", DEFAULT_COUNTY_SLUG)


def county_url(path):
    """Template-facing helper (registered below via context_processor):
    prefixes a real, already-known site-internal path with the CURRENT
    request's county slug -- e.g. county_url('/snapshot') ->
    '/travis-tx/snapshot'. Exists so templates can build a direct,
    already-correct link (no redirect hop) instead of the old bare path,
    which still works today ONLY because of the legacy 301 redirects
    registered below -- county_url() is the real, non-redirected way to
    link internally going forward. `path` must start with '/' and must NOT
    already include a county slug."""
    slug = getattr(g, "county_slug", DEFAULT_COUNTY_SLUG)
    return f"/{slug}{path}"


def county_cad_link(field, prop_id=None, geo_id=None):
    """PX-20260827-01 template-facing helper, FIXED in PX-20260827-02b:
    resolves one of the institutional-link fields added to COUNTY_PROFILES
    (e.g. "cad_parcel_detail_url_template", "cad_protest_url") for the
    CURRENT request's county.

    PX-20260827-02b BUGFIX: "*_template" fields used to carry only a
    "{prop_id}" placeholder, substituted with a single prop_id argument.
    PX-20260827-02 live-confirmed this was wrong for Dallas: DCAD's real
    "ID=" URL parameter requires the literal ACCOUNT_NUM (this schema's
    geo_id), not prop_id. For Dallas, prop_id is a value WE derive/
    synthesize purely to satisfy our own BIGINT primary-key uniqueness
    (int(ACCOUNT_NUM), or a synthetic SHA-256 hash for the ~25% of
    accounts that are alphanumeric) -- it is not a real DCAD identifier,
    so substituting it into a DCAD URL produced either a leading-zero-
    stripped account number (broken) or a nonsense disjoint number
    (broken) live on the real site.

    Travis is NOT the same situation and was correctly unaffected: Travis
    loads prop_id directly off TCAD's own PROP.TXT export (ears_format.py's
    PROP_SLICES["prop_id"], slice(0,12)) -- a genuine, TCAD-native internal
    property ID, completely separate from geo_id (PROP_SLICES["geo_id"],
    the account number). ProdigyCAD's property-detail URLs were
    independently verified (JOHNNY-FEEDBACK-1) to want exactly that
    TCAD-native ID. So Travis's prop_id is the CORRECT id for its own CAD
    website's URL shape; Dallas's prop_id never was, because Dallas's
    prop_id isn't a CAD-native id at all.

    Fix: a "*_template" field's placeholder(s) now declare which id(s)
    they need, by name -- "{prop_id}" and/or "{geo_id}" may each appear
    (independently substituted if present). This lets each county's own
    URL template say which id its own CAD website actually expects,
    instead of this function guessing or every caller needing per-county
    knowledge. Travis's templates keep "{prop_id}" (correct, unchanged --
    see above); Dallas's cad_parcel_detail_url_template,
    cad_homestead_exemption_url_template, and cad_protest_url_template
    were switched to "{geo_id}" as part of this fix.

    Returns None whenever the field is genuinely unset for this county, OR
    a *_template field is requested and any placeholder it actually
    contains lacks a corresponding non-empty id argument -- in all cases
    the caller MUST render nothing (or an explicit "not yet available"
    state), never fall back to another county's value or synthesize a
    guessed URL. This mirrors county_url()'s same-file fallback convention
    for use outside a real request context."""
    county_code = getattr(g, "county_code", COUNTY_SLUGS[DEFAULT_COUNTY_SLUG])
    profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])
    value = profile.get(field)
    if value is None:
        return None
    needs_prop_id = "{prop_id}" in value
    needs_geo_id = "{geo_id}" in value
    if (needs_prop_id and not prop_id) or (needs_geo_id and not geo_id):
        return None
    if needs_prop_id:
        value = value.replace("{prop_id}", str(prop_id))
    if needs_geo_id:
        value = value.replace("{geo_id}", str(geo_id))
    return value


def unavailable_copy(kind, county_name, page_label=None, view_label=None):
    """PX-20260830-04 Task 1: single source of truth for every user-facing
    data_unavailable_reason string in the app (the rates page's own reason
    at _rates_response(), all five real failure branches inside
    _snapshot_summary_freshness(), and templates/snapshot.html's own
    "no rows for this view" fallback -- registered into Jinja below so the
    template can call it too, same mechanism as county_cad_link()).

    Takes county_name directly (the real human-readable string, e.g.
    "Dallas County") rather than an internal county_code -- every call
    site already has this resolved one way or another (a `profile` dict
    already looked up from COUNTY_PROFILES in the two Python call sites
    below, or `county_profile.county_name` directly in the template), and
    COUNTY_PROFILES' own per-county dicts do NOT carry their own key back
    as a field, so re-deriving a code inside this function would need a
    second lookup table for no benefit. This also keeps the "never the raw
    code" rule structurally true: there is no county_code parameter here
    for a caller to accidentally pass straight through.

    Diego's standing ruling (PX-20260830-04): user-facing data_unavailable
    messages carry NO table names, file paths, function names, or internal
    county codes. Before this fix, every one of the six call sites above
    built its own raw f-string mixing an internal county_code (e.g. the
    literal "DALLAS") and/or a real table/file name straight into the
    sentence a visitor sees. This collapses all six into two possible
    published shapes, chosen by `kind`:

      - "being_prepared": the honest, common case for a newly-onboarded
        county -- parcel/appraisal data for the county IS loaded, but this
        specific derived/summary view hasn't been computed for it yet (a
        precomputation lag, not a coverage gap). This is what actually
        applies at all six current call sites (Dallas's Market Snapshot
        summary tables not yet refreshed; a hypothetical future county
        whose rate loader hasn't run yet).
      - "not_published": for the DIFFERENT, currently-unused case where a
        county's own source genuinely does not publish the underlying data
        type at all -- a real source-coverage gap, not a pipeline lag.
        Deliberately narrower wording (no "will be available soon" promise,
        since there may be nothing to become available). Only wire a call
        site to this branch where that's literally true for that county;
        do not use it as a generic "something's missing" fallback.

    `page_label`/`view_label` are optional human phrases naming what's being
    prepared and what will appear (e.g. page_label="Market Snapshot",
    view_label="the summary view"; page_label="tax rate history",
    view_label="rate data") -- both default to neutral generic phrasing so a
    call site can omit them entirely and still get a safe, honest sentence."""
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


def snapshot_coverage_copy(county_name, available_views, has_neighborhood_movers):
    """PX-20260901-03 Task 2: the one honest, GENERATED "here's what this
    county's Market Snapshot actually covers" sentence -- Diego's standing
    principle that a county's page is composed from the data that county
    has, complete on its own terms, never Travis's template rendered with
    holes.

    `available_views` is the real set of view values present in
    snapshot_totals for this county (app.py's _compute_snapshot_data());
    `has_neighborhood_movers` is the same county-level coverage fact
    _snapshot_summary_freshness() uses to decide whether
    snapshot_neighborhood_movers is even a required table for this county
    (see _county_has_neighborhood_data()). Both are real facts passed in
    by the caller -- this function itself never queries anything and never
    hand-types a per-county sentence; only the fixed label VOCABULARY
    (snapshot_taxonomy._SNAPSHOT_COVERAGE_LABELS) is a static lookup table.

    Returns None when the county has full coverage (every known view AND
    neighborhood movers) -- Travis today -- so a fully-covered county's
    page renders with NO coverage line at all, byte-identical to before
    this brief. The line only appears for a county missing something
    real."""
    all_views = tuple(_SNAPSHOT_COVERAGE_LABELS)
    missing_views = [v for v in all_views if v not in available_views]

    if not missing_views and has_neighborhood_movers:
        return None

    available_list = ", ".join(_SNAPSHOT_COVERAGE_LABELS[v] for v in all_views if v in available_views)

    missing_clauses = []
    if missing_views:
        missing_list = ", ".join(_SNAPSHOT_COVERAGE_LABELS[v] for v in missing_views)
        noun = "Sub-sector breakdown" if len(missing_views) == 1 else "Sub-sector breakdowns"
        missing_clauses.append(f"{noun} ({missing_list})")
    if not has_neighborhood_movers:
        missing_clauses.append("neighborhood detail")

    if len(missing_clauses) == 2:
        missing_sentence = f"{missing_clauses[0]} and {missing_clauses[1]} are not yet available for this county."
    else:
        verb = "is" if missing_clauses[0] == "neighborhood detail" else "are"
        missing_sentence = f"{missing_clauses[0]} {verb} not yet available for this county."

    return f"{county_name}: {available_list}. {missing_sentence}"


@app.context_processor
def _inject_county_helpers():
    """PX-20260824-01: county_profile added alongside the existing
    county_slug/county_url values -- same injection point, no new
    mechanism. Falls back to TRAVIS's profile outside a real request
    context (matching county_slug/county_url's own DEFAULT_COUNTY_SLUG
    fallback above) so a template read never raises for a KeyError on a
    profile that isn't registered -- .get() with the TRAVIS profile as a
    safety net, not a silent assumption that every county IS Travis.
    PX-20260827-01 adds county_cad_link alongside these, same mechanism."""
    county_code = getattr(g, "county_code", COUNTY_SLUGS[DEFAULT_COUNTY_SLUG])
    # PX-20260827-03-rev1: the one registry-driven "what's live" list,
    # available on every template render (not just search/info/home) so the
    # nav-level county switcher (Task 2) and the footer's coverage block
    # (Task 3) both read from the same source. Includes a real (TTL-cached,
    # see _live_counties_with_counts()'s own docstring) parcel_count on each
    # entry -- pickers/switchers simply don't reference that key, so one
    # list serves both uses. Computed once here (not once per dict value
    # below) so the new total_live_parcel_count_display global reuses this
    # exact same list/TTL-cache lookups instead of triggering a second
    # _live_counties_with_counts() call.
    _live = _live_counties_with_counts()
    return {
        "county_slug": getattr(g, "county_slug", DEFAULT_COUNTY_SLUG),
        "county_url": county_url,
        "county_profile": COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"]),
        "county_cad_link": county_cad_link,
        "unavailable_copy": unavailable_copy,
        "live_counties": _live,
        # PX-20260828-09 Task 1: single shared source for every "N+ parcels
        # across M counties" summed-total display (base.html's brand
        # tagline, index.html's trust strip/provenance kicker/provenance
        # H2, search.html's coverage lines) -- was six-plus separate
        # {{ "{:,}".format(live_counties | sum(attribute='parcel_count')) }}
        # Jinja filter chains, each re-summing live_counties itself.
        # Applies the same _human_count() floor-rounding each entry's own
        # parcel_count_display already uses.
        "total_live_parcel_count_display": _human_count(sum(c["parcel_count"] for c in _live)),
        # Distinct from county_slug's own DEFAULT_COUNTY_SLUG fallback
        # above -- hasattr checks whether _pull_county_slug() actually ran
        # and set a REAL g.county_slug for this request (a county-anchored
        # route), vs. a neutral bare route where county_slug above is only
        # ever the fallback value. base.html's nav-level county switcher
        # (Task 2) needs this distinction: Diego's ruling calls for the
        # switcher on county-anchored pages specifically, not the neutral
        # ones, where county_slug being "truthy" can't tell them apart.
        "is_county_anchored": hasattr(g, "county_slug"),
        # PX-20260828-01/PX-20260828-15 Task 1: the county COUNTY_BASE
        # (base.html) should target for this page's own /api/* fetch()
        # calls. On an anchored route (g.county_slug set by
        # _pull_county_slug()) this is just that real slug, same as
        # always. On a NEUTRAL route (no <county_slug> URL segment) the
        # fallback below is "" (bare COUNTY_BASE, cross-county-capable
        # /api/address_search), NOT DEFAULT_COUNTY_SLUG -- corrected by
        # PX-20260828-15 Task 1, which found that the old
        # DEFAULT_COUNTY_SLUG fallback silently pinned every neutral
        # page's search box to Travis (confirmed live for /about: typing
        # a Dallas address returned "No results found") whenever that
        # page didn't happen to override this value itself. Making the
        # SHARED DEFAULT itself neutral -- not just patching the known
        # offending routes -- is the fix PM's brief explicitly asked for
        # ("set the neutral value once in a shared place... so a
        # newly-added neutral page can't miss it"): about/terms/privacy/
        # disclaimer/styleguide/search_landing() render with zero
        # api_county_slug override and now correctly inherit "" instead
        # of quietly inheriting Travis. info_landing()/rates_landing()/
        # snapshot_landing() still override this explicitly (see
        # _resolve_landing_county_slug() and those routes' own
        # docstrings) because their override logic is more nuanced than
        # a flat default -- an explicit ?county=<slug> should still
        # scope their embedded search box to that slug, only the
        # no-?county-given fallback state needed to change from
        # Travis-slug to "".
        "api_county_slug": getattr(g, "county_slug", ""),
    }


# ── Legacy URL → county-prefixed URL, real permanent (301) redirects ───────────
# Real, required per Part 1.2 (this is a live, already-indexed site with real
# backlinks, including ones on LinkedIn from recent marketing work): every
# existing Travis URL keeps resolving, forever, via a genuine 301 (not a
# soft/JS redirect, not a 302) to its new county-prefixed shape. Listed here
# as (old_path, real_endpoint_name) pairs -- old_path is EXACTLY today's
# pre-DALLAS-GATE-1 route string for that endpoint (captured directly from
# the real @app.route decorators before they were each given a leading
# <county_slug> segment below), so this list is a real, verifiable diff
# against this file's own prior state, not hand-typed from memory.
#
# /healthz is deliberately excluded -- it was never given a county prefix
# (see COUNTY_SLUGS block above), so there is no old-vs-new shape to redirect
# between; it is the same single DB-free path either way.
# PX-20260827-03-rev1: '/', '/search', '/about', '/terms', '/privacy',
# '/disclaimer', and '/styleguide' are REMOVED from this list -- each is now
# a real, directly-served neutral bare-path route in its own right (home(),
# search_landing(), about(), terms(), privacy(), disclaimer(), styleguide()
# below), not a redirect target. Leaving their old entries here would
# register two different view functions at the same literal path.
# PX-20260828-01: '/info', '/rates', and '/snapshot' are ALSO now removed,
# for the identical reason -- Diego's 2026-08-28 ruling supersedes
# -03-rev1's "Info stays anchored" decision immediately above, and gives
# Rate Trends/Market Snapshot the same neutral-URL treatment. info_landing()/
# rates_landing()/snapshot_landing() are the real routes at those bare
# paths now, each with its own in-page county filter (?county=<slug>), not
# a redirect target. '/snapshot/neighborhood/<code>' is UNCHANGED and
# stays here -- this brief named Home/Search/Info/Rates/Snapshot only; the
# neighborhood drill-down (reached solely via Market Snapshot's own links,
# never a nav destination) was explicitly left out of scope, so a bare
# request there still needs the 301-to-Travis this list already provides.
# '/api/rates' also stays -- it's a JSON API endpoint, not a page, and was
# never asked to become neutral-URL; its own missing county_code scoping
# was fixed directly in api_rates() instead (see that route's docstring).
# PX-20260828-06: '/api/address_search' is REMOVED from this list -- unlike
# '/api/rates' above, this one WAS asked to become a true neutral route
# (the live-typing typeahead needs a genuinely cross-county bare endpoint,
# not a single-county default). api_address_search_landing() is the real
# route at that bare path now, reusing search_parcels_by_address()/
# resolve_exact_parcel() with county_code=None -- the same cross-county
# mechanism _home_search_response() already uses -- not a redirect target.
_LEGACY_REDIRECT_ROUTES = [
    ("/parcel/<geo_id>", "property_detail"),
    ("/parcel/<geo_id>/export.pdf", "export_due_diligence_pdf"),
    ("/api/parcel_entities", "api_parcel_entities"),
    ("/snapshot/neighborhood/<code>", "snapshot_neighborhood"),
    ("/api/rates", "api_rates"),
    ("/api/benchmark", "api_benchmark"),
    ("/api/benchmark/meta", "api_benchmark_meta"),
    ("/api/search_filter", "api_search_filter"),
    ("/api/estimate_acq/<geo_id>", "api_estimate_acq"),
    ("/api/peer_benchmark_local/<geo_id>", "api_peer_benchmark_local"),
    ("/api/peer_benchmark_sf/<geo_id>", "api_peer_benchmark_sf"),
    ("/api/news", "api_news"),
    ("/api/geocode/<geo_id>", "api_geocode"),
    ("/api/peer_set/<geo_id>", "api_peer_set"),
    ("/api/billing/<geo_id>", "api_billing"),
    ("/parcels", "parcel_list"),
    ("/compare", "compare_parcels"),
]


def _make_legacy_redirect(target_endpoint):
    """Returns a real, working Flask view function that 301-redirects any
    request at an OLD (pre-county-prefix) path to that same endpoint's NEW
    county-prefixed URL, preserving both the real captured path parameters
    (**kwargs, e.g. geo_id) and the real query string (?view=..., ?ids=...
    etc. -- dropping these on redirect would silently break every existing
    filtered/paramaterized bookmark and backlink, not just the bare page
    URLs). url_for(target_endpoint, **kwargs) automatically gets the right
    county_slug injected by _add_county_slug() above -- defaults to
    DEFAULT_COUNTY_SLUG ('travis-tx') since a request arriving at a legacy,
    unprefixed URL has no county_slug of its own to carry forward (there
    is, today, only one real county to redirect it to)."""
    def _view(**kwargs):
        target = url_for(target_endpoint, **kwargs)
        qs = request.query_string.decode()
        if qs:
            target = f"{target}?{qs}"
        return redirect(target, code=301)
    return _view


for _old_path, _endpoint in _LEGACY_REDIRECT_ROUTES:
    app.add_url_rule(
        _old_path,
        endpoint=f"{_endpoint}__legacy_redirect",
        view_func=_make_legacy_redirect(_endpoint),
    )


# ── Routes ────────────────────────────────────────────────────────────────────
# Every real route below (except /healthz) is now registered with a leading
# <county_slug> path segment -- e.g. "/<county_slug>/parcel/<geo_id>" -- per
# Diego's real routing decision (see the county-routing block above this
# comment for the full design rationale). _pull_county_slug() resolves that
# segment into g.county_code before the view function body runs; view
# functions read g.county_code the same way they'd read any other real
# Flask request-context value -- they do NOT receive county_slug as an
# argument (Flask's url_value_preprocessor pops it before dispatch).

def _resolve_quick_search(q, county_code=None):
    """PX-20260828-03 Task 1: the one shared three-way quick-search
    resolution -- exact match -> redirect straight to property_detail();
    address-like query (contains letters) -> a disambiguation list;
    anything else with no exact match -> an honest error message.

    Extracted out of _home_search_response() (which used to own this logic
    exclusively) so search_page()/search_landing()'s own "Find a Parcel"
    box can reuse the IDENTICAL resolution instead of navigating away to
    index()/home()'s results view -- Diego's ruling on this brief's Task 1
    read-first report was "unify on one results UI", and reusing one
    function rather than hand-copying its body a second time is what
    keeps that unification real (a second hand-copy could silently drift
    on what counts as a match, exactly the failure mode
    static/parcel-typeahead.js's July 2026 consolidation already fixed
    once for this same address-search feature).

    Returns a dict with exactly one of these shapes:
      {"redirect": <Response>}       -- caller should return this directly
      {"addr_matches": [...]}        -- caller renders its own template with this
      {"error": "..."}               -- caller renders its own template with this
      {}                             -- q was empty; nothing to render

    county_code=None (the neutral case, used by home() and search_landing())
    makes resolve_exact_parcel()/search_parcels_by_address() search every
    live county in turn, per Diego's ruling (b) ("users reach any live
    county from anywhere") -- county_code=g.county_code (the anchored case,
    used by index() and search_page()) keeps single-county behavior. The
    redirect uses the MATCHED parcel's own county_code/county_slug
    explicitly rather than relying on _add_county_slug()'s
    DEFAULT_COUNTY_SLUG fallback, which would otherwise silently send a
    Dallas match to a Travis-prefixed URL when called from a neutral,
    county-less page."""
    if not q:
        return {}

    parcel = resolve_exact_parcel(q, county_code=county_code)
    if parcel:
        match_slug = next(
            (slug for slug, code in COUNTY_SLUGS.items() if code == parcel["county_code"]),
            DEFAULT_COUNTY_SLUG,
        )
        return {"redirect": redirect(url_for("property_detail", geo_id=parcel["geo_id"], county_slug=match_slug))}

    # Address-like query (contains letters) — show disambiguation list
    if any(c.isalpha() for c in q):
        addr_matches = search_parcels_by_address(q, limit=20, county_code=county_code)
        if addr_matches:
            return {"addr_matches": addr_matches}
        return {"error": (
            f"No parcels found matching address \"{q}\". "
            # PX-20260829-01: was "...or use the 10-digit TCAD account
            # number" -- Travis-specific copy in an error path, same class
            # as -15 Task 2/-11 Task 4 (both already generalized similar
            # hardcoded-format copy elsewhere; this one survived because
            # error paths weren't part of either audit's sweep). Dallas's
            # own real DCAD ACCOUNT_NUM is 17 characters, not 10 -- this
            # now uses the same generic "parcel ID (from your county's
            # appraisal district or tax office)" phrasing already
            # established for index.html/search.html's own search hints
            # (PX-20260828-09 Task 4), rather than naming a county-specific
            # digit count that's only ever true for one of the live
            # counties.
            "Try a shorter street name, or search by your parcel ID (from your county's "
            "appraisal district or tax office) instead."
        )}

    return {"error": (
        f"We couldn't find a parcel matching \"{q}\". "
        # PX-20260829-01: same generalization as above -- was "the 10-digit
        # TCAD account number works most reliably. The 14-digit Tax Office
        # account and short prop_id integer are also accepted," which
        # named three Travis-specific digit counts as if they applied
        # site-wide. Not naming Dallas's own real formats explicitly either
        # (its 17-char DCAD account, its own tax-office account
        # convention) since this message has no county context to know
        # which one to cite -- same "accepts a parcel ID..." generic
        # phrasing used elsewhere, honest without being wrong for either
        # county.
        "Double-check the format — this accepts a parcel ID from your county's appraisal "
        "district or tax office, not just a street address."
    )}


def _resolve_landing_county_slug(param_name="county"):
    """PX-20260828-15 Task 1: shared resolver for the three '?county=<slug>'
    neutral landing routes (info_landing/rates_landing/snapshot_landing) --
    replaces three copies of the identical
        slug = request.args.get("county", "")
        county_code = COUNTY_SLUGS.get(slug)
        if county_code is None:
            slug = DEFAULT_COUNTY_SLUG
            county_code = COUNTY_SLUGS[slug]
    block. Extracted now specifically so a future 4th neutral landing page
    reuses this function instead of hand-copying the pattern a 4th time --
    per PM's explicit structural requirement ("don't fix five routes
    individually and recreate the failure mode for route six").

    Returns (county_code, slug, explicit): `explicit` is True only when the
    caller's own ?county=<slug> was present AND recognized -- callers need
    this third value to know whether to scope their page's embedded search
    box to that real slug (explicit ask) or leave it neutral (silent
    Travis-fallback case -- see api_county_slug usage at each call site)."""
    slug = request.args.get(param_name, "")
    county_code = COUNTY_SLUGS.get(slug)
    explicit = county_code is not None
    if not explicit:
        slug = DEFAULT_COUNTY_SLUG
        county_code = COUNTY_SLUGS[slug]
    return county_code, slug, explicit


def _home_search_response(q, county_code=None, api_county_slug=None):
    """PX-20260827-03-rev1: shared body for both index() (county-anchored,
    '/<county_slug>') and home() (neutral, bare '/'). PX-20260828-03: now a
    thin wrapper over _resolve_quick_search() (see that function's
    docstring for the full county_code=None/neutral-search reasoning),
    kept as its own function because index()/home() render index.html
    unconditionally on every non-redirect outcome, unlike search_page()/
    search_landing() below, which merge the same result into a page that
    also has its own, separate Filter Parcels context.

    PX-20260828-06b: api_county_slug is passed straight through to
    render_template() as an explicit kwarg, same mechanism as
    _rates_response()/_info_response()/_snapshot_response() (an explicit
    render_template() kwarg overrides the _inject_county_helpers() context
    processor's own same-named default, per Jinja's normal precedence --
    no new mechanism). Both callers below pass their own real value
    unconditionally, not just the neutral one -- index() passes
    g.county_slug (identical to what the context processor would already
    default to there, so anchored pages are byte-for-byte unaffected);
    home() passes "" (see home()'s own docstring for why empty, not a
    slug, is the correct neutral value here)."""
    result = _resolve_quick_search(q, county_code=county_code)
    if "redirect" in result:
        return result["redirect"]
    return render_template(
        "index.html", q=q, error=result.get("error"), addr_matches=result.get("addr_matches"),
        api_county_slug=api_county_slug,
    )


@app.route("/<county_slug>", strict_slashes=False)
@limiter.limit(_LIMIT_HEAVY)
def index():
    """PX-20260827-05: strict_slashes=False -- Flask's default exact-match
    trailing-slash behavior meant '/dallas-tx' (this route's real registered
    shape) resolved fine but '/dallas-tx/' 404'd instead of also matching.
    Standard Flask footgun, not new to this session, low severity (no
    app-generated link produces the trailing-slash form) -- fixed by
    opting this route out of strict matching so both forms resolve
    identically, rather than adding a second explicit redirect rule."""
    q = request.args.get("q", "").strip()
    return _home_search_response(q, county_code=g.county_code, api_county_slug=g.county_slug)


@app.route("/")
@limiter.limit(_LIMIT_HEAVY)
def home():
    """PX-20260827-03-rev1 Task 1: the real neutral, bare-path home page --
    NOT a redirect (the old _LEGACY_REDIRECT_ROUTES 301-to-Travis entry for
    '/' is removed; see that list's own comment). index() above (registered
    at '/<county_slug>') is kept as a SEPARATE, still-real route rather
    than being torn out: base.html's COUNTY_BASE JS constant is built from
    url_for('index') and is the one mechanism every page's JS fetches use
    to call the CURRENT county's own /api/* endpoints (see base.html) --
    collapsing index() into this bare route would silently break that for
    every other page on the site, a much bigger blast radius than "make
    home reachable at a bare URL" was ever asking for. '/dallas-tx' (and
    any future county) remains a real, useful county-specific landing page
    in its own right; '/' is the county-agnostic entry point Diego's
    ruling (b) actually wants. county_code=None here is what makes the
    home search box reach any live county, not just Travis.

    PX-20260828-06b: api_county_slug="" (NOT a real slug, unlike
    info_landing()/rates_landing()/snapshot_landing()'s own api_county_slug
    overrides, which always resolve to a concrete filtered-or-Travis slug
    because those three pages DO filter to one county at a time). This
    page's typeahead has no such filter -- it's meant to search every live
    county, the same as its own full-page Enter-to-search flow above
    already does via county_code=None -- so the base.html-built COUNTY_BASE
    constant needs to point at the bare '/api/*' path, not any one
    county's anchored path. base.html builds it as
    `url_for('index', county_slug=api_county_slug).replace(/\/$/, '')`;
    with api_county_slug="", url_for('index', county_slug='') builds
    '/' (Werkzeug substitutes a route's dynamic segment values at
    url_for()-time without re-validating them against that segment's own
    matching regex -- this is standard, long-established Flask/Werkzeug
    behavior, not something this specific empty-string case is exempt
    from), which .replace(/\/$/, '') then strips to ''. static/parcel-
    typeahead.js's own fetch() call is `global.COUNTY_BASE +
    "/api/address_search?q=..."` (plain string concatenation, confirmed by
    reading that file directly) -- with COUNTY_BASE === '', that resolves
    to exactly '/api/address_search?q=...', the new bare route
    (PX-20260828-06) added for exactly this, not '/travis-tx/api/
    address_search?q=...'. See verify_px_20260828_06b_neutral_county_base.py
    for the render-harness proof and this docstring's own sandbox-vs-live
    disclosure (Flask/Werkzeug aren't installed in this sandbox either, so
    the url_for()-build-time claim above is framework-documented behavior,
    not something executed here -- Diego's own DevTools check on the real
    deployed homepage is the closing step, exactly as this brief asked)."""
    q = request.args.get("q", "").strip()
    return _home_search_response(q, county_code=None, api_county_slug="")


@app.route("/healthz")
@limiter.exempt
def healthz():
    """DB-free liveness check (POST-PARTITION-INCIDENT-1-AUDIT, adjacent
    item). Deliberately touches NO database connection, NO query() call --
    returns 200 the instant the process can run Python at all. Exists
    specifically so a health checker can distinguish "the gunicorn process
    itself is alive" from "the database is slow/down" -- Render's current
    health check (if pointed at "/", per its own real DB-heavy index()
    handler above) cannot make that distinction today, and would report a
    healthy process as unhealthy during exactly the kind of DB slowdown
    this audit exists to catch (a false "process is dead" signal during a
    real query-timeout incident, potentially triggering an unwanted
    restart mid-incident instead of leaving the process up to investigate).
    Exempted from Flask-Limiter (@limiter.exempt) since a health checker
    may poll this frequently and should never be throttled.

    NOTE: this route alone does not change what Render actually checks --
    Render's Health Check Path is a separate setting on the service itself
    (Render Dashboard -> service -> Settings -> Health & Alerts), and
    updating it to "/healthz" is a deliberate dashboard change left for
    Diego to make himself, not something this code change can do."""
    return jsonify({"ok": True}), 200


@app.route("/<county_slug>/search")
def search_page():
    """Task 13 — dedicated search page with a US coverage map (visual only).
    Not an interactive GIS map; just communicates current coverage (Travis County).

    PX-20260828-03 Task 1: this page's own "Find a Parcel" quick box used
    to submit to index()'s bare url_for('index') -- county_slug got
    auto-injected by _add_county_slug() from g.county_slug, so the box
    silently navigated the user OFF /search entirely and onto the
    homepage-hero results view, a different template/layout, the moment
    they searched. The box's form now submits back to this same page
    (action="{{ request.path }}" in search.html), and q= is resolved here
    via the same _resolve_quick_search() index()/home() already use --
    one shared resolver, not a second hand-copied implementation that
    could drift on what counts as a match."""
    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: the Tax Year filter's "2026"
    # option used to hardcode "(Preliminary)" regardless of actual
    # data_source -- today's certified load means most 2026 rows are now
    # certified. Cheap EXISTS check (stops at first match) rather than a
    # full COUNT, since the dropdown only needs a yes/no for whether any
    # 2026 row is still on the preliminary tier.
    # DALLAS-GATE-1 Part 2: county_code-scoped -- this county's search page
    # should describe this county's own 2026 data state, not another
    # county's.
    has_preliminary_2026 = bool(query(
        "SELECT EXISTS(SELECT 1 FROM parcel_tax_year WHERE tax_year = 2026 "
        "AND data_source = 'preliminary' AND county_code = %s) AS x",
        (g.county_code,), one=True,
    )["x"])
    q = request.args.get("q", "").strip()
    result = _resolve_quick_search(q, county_code=g.county_code)
    if "redirect" in result:
        return result["redirect"]
    # PX-20260828-15 Task 3 (filter honesty for unavailable data, Diego's
    # sign-off: disable the affected control inline, generic per-table
    # mechanism): this page has a real county (g.county_code), so check
    # each registered filter's backing table now rather than deferring to
    # api_search_filter() -- the whole point is telling the user BEFORE
    # they run a search that would silently come back empty, not after.
    filter_data_availability = {
        key: _table_has_county_data(table, g.county_code)
        for key, table in FILTER_DATA_REQUIREMENTS.items()
    }
    return render_template(
        "search.html", has_preliminary_2026=has_preliminary_2026, county_selected=True,
        q=q, addr_matches=result.get("addr_matches"), error=result.get("error"),
        filter_data_availability=filter_data_availability,
    )


@app.route("/search")
def search_landing():
    """PX-20260827-03-rev1 Task 1/2: the neutral, bare-path search LANDING --
    the filter form + a real county picker (Task 2), no results, no
    g.county_code (this route has no <county_slug> segment at all). Diego's
    ruling: "search landing (county is a filter on the form)" is neutral;
    "search results when county-filtered" stays county-anchored. Rather
    than duplicate search.html's ~800 lines of filter/map/pagination JS
    into a second template, this renders the SAME template with
    county_selected=False -- search.html's own JS (Task 2 edit) reads that
    flag to skip the results-fetching path entirely and instead have its
    County selector NAVIGATE to '/<slug>/search' on selection (preserving
    whatever filter values are already filled in), landing the user on the
    existing, fully-working anchored page. Matches Diego's stated
    preference: "clean navigation over clever state-carryover" -- no new
    cross-county fetch/state logic, just a real page load into the
    county-anchored route that already does this correctly.

    PX-20260828-03 Task 1: the "Find a Parcel" box's q= is now resolved
    here too, with county_code=None (the same neutral, every-live-county
    search home()/index() use for the bare '/' page) -- this closes a
    SECOND, worse instance of the box's original bug: because this route
    has no g.county_slug, the old bare url_for('index') fell back to
    _add_county_slug()'s DEFAULT_COUNTY_SLUG, meaning the box ALWAYS
    submitted to /travis-tx?q=... from this page regardless of what was
    typed -- silently Travis-only, found while reading this route for
    Task 1's read-first report, not part of the originally reported bug."""
    q = request.args.get("q", "").strip()
    result = _resolve_quick_search(q, county_code=None)
    if "redirect" in result:
        return result["redirect"]
    # PX-20260828-15 Task 3: no real county chosen yet on this neutral landing
    # (no g.county_code at all -- see this route's own docstring) -- explicit
    # empty dict rather than omitting the kwarg, so search.html's
    # filter_data_availability.get(key, True) reads as "no gap known yet"
    # (available) instead of relying on Jinja's Undefined semantics for a
    # missing template variable.
    return render_template(
        "search.html", has_preliminary_2026=False, county_selected=False,
        q=q, addr_matches=result.get("addr_matches"), error=result.get("error"),
        filter_data_availability={},
    )


@app.route("/<county_slug>/parcel/<geo_id>")
@limiter.limit(_LIMIT_HEAVY)
def property_detail(geo_id):
    # DALLAS-GATE-1 Part 2: every query below in this function is now scoped
    # to g.county_code (set per-request by _pull_county_slug). This is the
    # highest-traffic route in the app and was the ORIGINAL site of the
    # incident this brief exists to close out -- see
    # POST-PARTITION-INCIDENT-1-AUDIT and schema.sql's reactive-index
    # comments for the four tables (parcel, parcel_tax_year, tax_billing,
    # tax_delinquent) that incident already forced index fixes for.
    county_code = g.county_code

    # Core parcel
    parcel = query(
        "SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s",
        (geo_id, county_code), one=True)
    if not parcel:
        # PX-20260829-01: same Travis-specific-copy generalization as
        # _resolve_quick_search()'s two error branches above (see that
        # function's own comment for the full reasoning) -- this route's
        # own 404 path is a THIRD, previously-unaudited occurrence of the
        # identical hardcoded "10-digit TCAD account number" phrase, found
        # while fixing the other two. Same generic "parcel ID (from your
        # county's appraisal district or tax office)" phrasing.
        #
        # Whether this 404 path should ALSO try other live counties and
        # redirect (the way _resolve_quick_search()'s search-box path
        # already does for a typed query) is investigated, not fixed, in
        # this brief's own final report -- see the report's dedicated
        # section on /travis-tx/parcel/<dallas-geo-id> behavior.
        return render_template(
            "index.html",
            q=geo_id,
            error=(
                f"We couldn't find parcel \"{geo_id}\". "
                "Double-check the format — this accepts a parcel ID from your county's "
                "appraisal district or tax office."
            )
        ), 404

    # 5-year value history
    history = query("""
        SELECT pty.tax_year,
               pty.market_value,
               pty.assessed_value,
               pty.taxable_value,
               pty.land_value,
               pty.imprv_value,
               pty.hs_cap_loss,
               pty.exemption_codes,
               pty.data_source,
               tb.total_tax,
               tb.total_due,
               tb.is_delinquent,
               tb.exemption_codes  AS billing_exemptions,
               tb.data_source      AS billing_source,
               tb.confidence_level AS billing_confidence
        FROM   parcel_tax_year pty
        LEFT JOIN tax_billing   tb  ON tb.geo_id      = pty.geo_id
                                   AND tb.tax_year     = pty.tax_year
                                   AND tb.county_code  = pty.county_code
        WHERE  pty.geo_id = %s AND pty.county_code = %s
        ORDER  BY pty.tax_year
    """, (geo_id, county_code))

    # Multi-unit panel (Migration M2, SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.5).
    # A geo_id can now genuinely represent more than one TCAD unit (condo
    # regime, multi-improvement account, etc — see parcel_tax_year.unit_count
    # on the `history` rows above). This surfaces those individual units so a
    # visitor isn't shown one blended number with no indication it's a sum.
    # "latest" = the most recent tax_year we have any history row for, not a
    # hardcoded year, so this keeps working as new years get loaded.
    units = None
    latest_unit_year = max((row["tax_year"] for row in history), default=None)
    if latest_unit_year is not None:
        unit_rows = query("""
            SELECT u.prop_id, u.owner_name, u.situs_address,
                   y.market_value, y.assessed_value, y.taxable_value
            FROM   prop_unit u
            LEFT JOIN prop_unit_tax_year y
                   ON y.prop_id     = u.prop_id
                  AND y.tax_year    = %s
                  AND y.county_code = u.county_code
            WHERE  u.geo_id = %s AND u.county_code = %s
            ORDER  BY u.prop_id
        """, (latest_unit_year, geo_id, county_code))
        # Only render a "multi-unit" panel when there's genuinely more than
        # one unit — a single-unit parcel showing a one-row panel would just
        # repeat the KPI cards above with no new information.
        units = unit_rows if unit_rows and len(unit_rows) > 1 else None

    # Current-year entity breakdown
    entity_detail = query("""
        SELECT tbe.entity_code,
               ctr.entity_name,
               ctr.rate,
               ctr_prev.rate   AS rate_prev,
               tbe.amount_due,
               tbe.amount_paid
        FROM   tax_billing_entity tbe
        LEFT JOIN county_tax_rate  ctr      ON ctr.entity_code      = tbe.entity_code
                                           AND ctr.tax_year         = 2025
                                           AND ctr.county_code      = tbe.county_code
        LEFT JOIN county_tax_rate  ctr_prev ON ctr_prev.entity_code = tbe.entity_code
                                           AND ctr_prev.tax_year    = 2024
                                           AND ctr_prev.county_code = tbe.county_code
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025 AND tbe.county_code = %s
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id, county_code))

    # Prior-year (2024) entity breakdown — needed ONLY for the Bill-Change
    # Waterfall (build_bill_waterfall, below). entity_detail above already
    # carries 2025's amount_due plus BOTH 2025 (rate) and 2024 (rate_prev)
    # rates — the one real field still missing is 2024's actual amount_due
    # per entity, which this query adds. Real data (tax_billing_entity has
    # had per-entity 2021-2024 rows since this session's PIR loader work).
    entity_detail_prev = query("""
        SELECT tbe.entity_code, tbe.amount_due
        FROM   tax_billing_entity tbe
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2024 AND tbe.county_code = %s
    """, (geo_id, county_code))

    # Delinquency
    delinquent = query(
        "SELECT * FROM tax_delinquent WHERE geo_id = %s AND county_code = %s",
        (geo_id, county_code), one=True
    )

    # Current year snapshot (2025)
    current = next((r for r in history if r["tax_year"] == 2025), None)

    # Confidence now comes directly from tax_billing.confidence_level, set at
    # WRITE time by the loader (load_tax_current.py, July 2026 fix, mirroring
    # the tagging load_pir_billing_2021_full.py/scrape_billing_history.py
    # already do for other years) instead of being re-derived here on every
    # page load from total_tax truthiness + an ad hoc entity-sum recomputation.
    # 'verified' -> real, source-reported total. 'derived' -> total_tax was
    # reconstructed from entity-level DUE amounts because the source's own
    # total was missing/zero (the TaxCurOpenData "0.00" quirk documented in
    # KNOWN_LIMITATIONS.md). NULL -> no usable total at all (rare; e.g. a row
    # this loader fix hasn't reached yet, or a row with neither a source total
    # nor any entity data).
    #
    # This also replaces the two independently-written is_billing_verified
    # checks (Homeowner's "Your Tax Rate & Bill" table vs. Investor's Value
    # History table) that a July 2026 truth-table audit found could silently
    # diverge on 8/36 field combinations -- both now read this one value.
    for _r in history:
        _r["total_tax_derived"]  = (_r.get("billing_confidence") == "derived")
        _r["is_billing_verified"] = (_r.get("billing_confidence") == "verified")

    # Defensive fallback ONLY for rows the write-time fix hasn't reached yet
    # (confidence_level still NULL -- e.g. Diego's backfill/rerun of existing
    # tax_billing rows hasn't run, or hasn't reached this parcel). Once that
    # backfill completes for all rows, confidence_level is never NULL for a
    # row that has entity data, so this branch stops firing on its own; kept
    # rather than deleted outright since this sandbox has no live DB to
    # confirm the backfill has actually reached every row yet.
    if (current is not None and current.get("billing_confidence") is None
            and not current.get("total_tax") and entity_detail):
        derived_tax = sum(e["amount_due"] for e in entity_detail if e["amount_due"])
        if derived_tax:
            current["total_tax"] = derived_tax
            current["total_tax_derived"] = True

    # Bill-Change Waterfall (per Diego's outside-review brief, top priority
    # item): 2024 -> 2025 is the most recent pair where BOTH years have
    # genuinely verified billing (is_billing_verified, just computed above)
    # and per-entity data on file. Returns None (card simply doesn't render)
    # when either year isn't verified, entity data is missing, or assessed
    # values aren't available — never fabricates a decomposition on partial
    # data. See build_bill_waterfall()'s docstring for the full math
    # derivation and exactness proof.
    bill_waterfall = build_bill_waterfall(
        history, entity_detail, entity_detail_prev,
        cur_year=2025, prior_year=2024
    )

    # Historical combined tax rate for this parcel's entities (for trend projection)
    rate_history = query("""
        SELECT ctr.tax_year, SUM(ctr.rate) AS total_rate
        FROM   county_tax_rate ctr
        WHERE  ctr.county_code = %s
        AND    ctr.entity_code IN (
                   SELECT entity_code FROM tax_billing_entity
                   WHERE  geo_id = %s AND tax_year = 2025 AND county_code = %s
               )
        AND    ctr.tax_year BETWEEN 2021 AND 2025
        GROUP  BY ctr.tax_year
        ORDER  BY ctr.tax_year
    """, (county_code, geo_id, county_code))

    # ── Computed historical tax (feature flag: COMPUTED_HIST_TAX_ENABLED) ────────
    # When enabled, rows where total_tax is NULL (2021–2024 without billing data)
    # receive a computed estimate: taxable_value × combined_rate / 100.
    # Stored as computed_total_tax (separate key) — never overwrites real billing data.
    # Label: "computed from certified value × rate; billing unconfirmed"
    #
    # Fabrication-risk investigation (July 2026, per Diego, parcel 0438011527 —
    # a parcel with no certified record at all until 2024): this loop has NO
    # explicit "did the parcel exist in year `yr`" check of its own. It computes
    # for whatever `row` it's handed. The only reason it can't fabricate a
    # figure for a year the parcel didn't exist is that `history` (above) is
    # built FROM parcel_tax_year, not from a fixed 2021–2026 range — so a year
    # the parcel has no parcel_tax_year row for never becomes a `row` here in
    # the first place, and this loop never gets a chance to run for it.
    #
    # That protection is IMPLICIT and NOT enforced by this code — it's a
    # structural side effect of every current parcel_tax_year writer
    # (load_certified_historical.py, load_ajr.py, load_cert_2021.py,
    # load_certified_2025.py, load_2026_preliminary.py) independently parsing
    # only its own year's source file, with no carry-forward/backfill logic
    # between years. Confirmed by reading all of them — none of them ever
    # invents a parcel_tax_year row for a year a parcel wasn't actually present
    # in that year's county export.
    #
    # If a future loader (e.g. the still-unbuilt load_pir_tcad.py, referenced
    # in run_all.py's docstring but not yet written) ever back-fills or
    # synthesizes parcel_tax_year rows for years a parcel didn't really exist
    # — even for a reasonable-sounding reason like "give every parcel a full
    # 2021–2026 row set for UI consistency" — it would silently reintroduce
    # exactly the fabrication risk this comment is warning about: a real
    # dollar figure, with a "computed" confidence label, for a year the
    # property wasn't a taxable entity yet. Anyone changing a parcel_tax_year
    # writer should re-verify this loop's safety before doing so, not assume
    # it's guarded here.
    if config.COMPUTED_HIST_TAX_ENABLED:
        _rate_map = {r["tax_year"]: float(r["total_rate"])
                     for r in rate_history if r.get("total_rate")}
        for row in history:
            if row.get("total_tax") is not None:
                continue  # real billing data present — do not overlay
            yr = row.get("tax_year")
            tv = row.get("taxable_value")
            rate = _rate_map.get(yr)
            if tv and rate and rate > 0:
                row["computed_total_tax"] = round(float(tv) * rate / 100.0, 2)

    # Entity rate history for trend chart + rate columns (2016–2025 for 10-year chart context)
    rate_history_rows = query("""
        SELECT ctr.entity_code, ctr.tax_year, ctr.rate
        FROM   county_tax_rate ctr
        WHERE  ctr.county_code = %s
        AND    ctr.entity_code IN (
                   SELECT entity_code FROM tax_billing_entity
                   WHERE  geo_id = %s AND tax_year = 2025 AND county_code = %s
               )
        AND    ctr.tax_year BETWEEN 2016 AND 2025
        ORDER  BY ctr.entity_code, ctr.tax_year
    """, (county_code, geo_id, county_code))

    # {entity_code: {year: rate_float}}
    entity_rate_by_code = {}
    for r in rate_history_rows:
        code = r["entity_code"]
        entity_rate_by_code.setdefault(code, {})[r["tax_year"]] = (
            float(r["rate"]) if r["rate"] is not None else None
        )

    # Chart JSON — only entities with ≥2 data points; years 2016–2025
    chart_years = list(range(2016, 2026))
    chart_entity_data = {}
    for code, yr_map in entity_rate_by_code.items():
        pts = [yr_map.get(y) for y in chart_years]
        if sum(1 for p in pts if p is not None) >= 2:
            chart_entity_data[code] = pts

    # ── Central 2026 baseline derivation (Issue 1, "Homestead-Cap Data
    # Integrity: Full Fix Set" Cowork brief, July 2026) ────────────────────
    # SINGLE assembly point for the 2026 row's derived assessed/taxable
    # figures -- every downstream consumer (build_projections()'s CAGR-
    # endpoint base below, estimated_tax_2026, the Taxable Value 2026 KPI
    # card, the Current & Preliminary Values table's 2026 column) must read
    # est_assessed_2026/est_taxable_2026/basis_2026 from current_2026 (this
    # same dict object also lives inside `history`, so any code iterating
    # history picks these up too) instead of independently re-deriving or
    # trusting current_2026["assessed_value"]/["taxable_value"] raw. Full
    # rule/derivation writeup lives in tax_logic/texas.py's
    # derive_2026_baseline() docstring, not duplicated here. Attaches
    # NEW keys only -- never overwrites the raw assessed_value/taxable_value
    # fields, so any code that genuinely needs TCAD's raw preliminary figure
    # (as opposed to the display/computation figure) still can.
    # MUST run before build_projections() -- that function's own
    # cagr_endpoint/base_assessed logic is the fix's original trigger case
    # and reads current_2026's derived fields via the shared `history` list.
    current_2026 = next((r for r in history if r["tax_year"] == 2026), None)
    if current_2026:
        _baseline_2026 = _derive_2026_baseline(current, current_2026)
        # Always set all four keys when current_2026 exists, even when
        # _baseline_2026 is None (only happens if current_2026 lacks a
        # market_value -- shouldn't occur for a real row but guarded anyway)
        # -- templates test `current_2026 and current_2026.est_assessed_2026`
        # etc., and Jinja's `and` only short-circuits on the FIRST falsy
        # operand, so a truthy current_2026 missing these keys entirely
        # (rather than having them present-but-None) raises UndefinedError
        # under this app's StrictUndefined templates, not a graceful "—".
        current_2026.update(_baseline_2026 or {
            "est_assessed_2026": None, "est_taxable_2026": None,
            "basis_2026": None, "is_approx_2026": False,
            "confidence_2026": None,
        })

    # M4-2026-PRELIM-SNAPSHOT Part 3: original 2026 Preliminary values,
    # read from the standalone parcel_2026_preliminary_snapshot table (Part
    # 2) rather than parcel_tax_year -- today's certified load overwrote
    # the live preliminary values in place, so this snapshot table is now
    # the ONLY place the original preliminary numbers still exist. None
    # when this parcel has no snapshot row (e.g. it didn't exist yet as of
    # the preliminary export, or the snapshot loader hasn't been run) --
    # the template must show an explicit "not available" gap for that case,
    # never a blank/zero/assumed value (brief's data-honesty requirement).
    # DALLAS-GATE-2 Part 3: parcel_2026_preliminary_snapshot is now in
    # migrate_county_partitioning.py's TABLE_SPECS (added this brief,
    # reversing the original spec's "retire, don't migrate" recommendation
    # -- see that script's own module docstring UPDATE note). Scoped by
    # county_code below.
    #
    # REAL SEQUENCING REQUIREMENT, not just a style note: this query will
    # raise "column county_code does not exist" against a live database
    # the migration hasn't been run against yet. The migration
    # (migrate_county_partitioning.py, this table's new Mode-1 entry) MUST
    # run before this app.py change reaches a live gunicorn worker, or
    # every property-detail page load 500s for a parcel with a 2026
    # preliminary snapshot row. Flagged prominently in this brief's report
    # -- do not deploy this commit before running the migration.
    prelim_2026_snapshot = query("""
        SELECT market_value, assessed_value, taxable_value,
               land_value, imprv_value, exemption_codes, unit_count
        FROM   parcel_2026_preliminary_snapshot
        WHERE  geo_id = %s AND county_code = %s
    """, (geo_id, county_code), one=True)

    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: computed here (Python), not as a
    # Jinja {% set %} inside property.html's {% block content %} -- a
    # {% set %} there is invisible inside the file's separate
    # {% block scripts %} (Jinja gives every named block its own local
    # scope), which is exactly the render-harness failure that surfaced
    # while building Part 3 (the Chart.js tooltip in {% block scripts %}
    # references this flag). Passing it as a real context variable, like
    # current_2026 itself, makes it visible in every block.
    is_2026_certified = bool(
        current_2026 and current_2026.get("data_source") in CERTIFIED_TIER_DATA_SOURCES
    )

    insights    = build_insights(parcel, history, entity_detail, delinquent)
    projections, proj_baseline, proj_bands = build_projections(
        history, rate_history, entity_detail,
        state_cd1=parcel.get("state_cd1")
    )

    # ── Phase 2: computed insight metrics ──────────────────────────────────────
    # Populated by compute_metrics.py. Gracefully absent before first run.
    metrics_by_year  = {}
    bench_label      = None
    benchmark_by_year = {}
    try:
        for m in query(
            "SELECT * FROM parcel_metrics WHERE geo_id = %s AND county_code = %s ORDER BY tax_year",
            (geo_id, county_code)
        ):
            metrics_by_year[m["tax_year"]] = m

        # classi_cd-first (Task 1): pull the benchmark row matching the parcel's
        # *actual* use, not just its state_cd1 prefix.
        bench_label = property_type_label(parcel.get("classi_cd"), parcel.get("state_cd1"))
        if bench_label:
            for b in query("""
                SELECT * FROM county_benchmark
                WHERE property_type_label = %s AND county_code = %s ORDER BY tax_year
            """, (bench_label, county_code)):
                benchmark_by_year[b["tax_year"]] = b
    except Exception:
        pass  # Phase 2 tables not yet populated — skip metrics sections

    # Tax calendar (July 2026, per Diego's Cowork brief item 1) — uses the
    # real server clock, not a static illustration; see build_tax_calendar()'s
    # own docstring for the full sourcing writeup.
    tax_calendar = build_tax_calendar(datetime.now().date(), current_2026, delinquent)

    # Estimated 2026 total tax: taxable_value_2026 × blended 2025 entity rates
    # Uses this parcel's specific entity mix (not county-wide avg) for accuracy.
    # Only computed when taxable_value is available for 2026 — never falls back to MV.
    #
    # Issue 1 fix: reads est_taxable_2026 (the derive_2026_baseline() output
    # attached to current_2026 above), NOT the raw taxable_value field. Real
    # scope, confirmed live: in the cohort where the cap actually binds
    # (equality + market grown past 110% of 2025's real assessed value --
    # 4,266 parcels), 97.9% (4,178) also had taxable_value tracking the
    # uncapped market figure, meaning this KPI was overstating tax for the
    # large majority of that cohort before this fix. est_taxable_2026 is
    # always populated (same value as taxable_value, just under a different
    # key) whenever current_2026 exists, via derive_2026_baseline()'s
    # uncapped_no_cap/tcad_capped branches -- so this is a pure key rename in
    # the common case, and the actual correction only in the cap-binds case.
    estimated_tax_2026 = None
    assumed_rate_2026 = None
    if current_2026 and current_2026.get("est_taxable_2026") and entity_detail:
        tv26 = current_2026["est_taxable_2026"]
        blended_rate_2025 = sum(
            float(e["rate"]) for e in entity_detail if e.get("rate") is not None
        )
        if blended_rate_2025 > 0:
            estimated_tax_2026 = round(tv26 * blended_rate_2025 / 100.0, 2)
            # Exposed to the template (Two-Year Card Redesign, July 2026, per Diego)
            # so the Homeowner-mode "Estimated 2026 homestead savings" card can apply
            # this SAME assumed rate to the 2026 preliminary values, the same way the
            # 2025 card applies insights.total_rate_2025 to the 2025 values — reusing
            # this exact number rather than a second independently-computed one.
            # Real 2026 entity rates aren't adopted until Aug/Sept, so "assumed" here
            # explicitly means "last known (2025) rates," same assumption
            # estimated_tax_2026 already makes.
            assumed_rate_2026 = blended_rate_2025

    # Est. 2026 effective tax rate (Task 3) — same-year basis, Estimated badge:
    # estimated 2026 tax ÷ 2026 preliminary market value. Kept separate from the
    # Verified 2025 ETR; never blended with it.
    est_etr_2026 = None
    if estimated_tax_2026 and current_2026 and current_2026.get("market_value"):
        _mv26 = current_2026["market_value"]
        if _mv26 and _mv26 > 0:
            est_etr_2026 = round(estimated_tax_2026 / _mv26 * 100, 4)

    # ── CoStar-style KPI cards ─────────────────────────────────────────────────
    kpi = {}
    if current_2026 and current_2026.get("market_value"):
        kpi["market_value"]        = current_2026["market_value"]
        kpi["market_value_year"]   = 2026
        # M4-2026-PRELIM-SNAPSHOT Part 1 fix: this used to hardcode
        # "preliminary" unconditionally -- wrong as of today's 2026
        # certified load for any parcel whose 2026 row is now cert_2026.
        # Not currently read by property.html (that template computes its
        # own is_2026_certified from current_2026.data_source directly),
        # but fixed here too so this field is never factually wrong for
        # any future consumer.
        kpi["market_value_source"] = (
            "certified" if current_2026.get("data_source") in CERTIFIED_TIER_DATA_SOURCES
            else "preliminary"
        )
    elif current and current.get("market_value"):
        kpi["market_value"]        = current["market_value"]
        kpi["market_value_year"]   = 2025
        kpi["market_value_source"] = "certified"

    if current_2026 and current_2026.get("market_value") and current and current.get("market_value"):
        kpi["yoy_pct"]   = round((current_2026["market_value"] - current["market_value"])
                                  / current["market_value"] * 100, 1)
        kpi["yoy_label"] = "2025 → 2026"
    elif metrics_by_year.get(2025) and metrics_by_year[2025].get("yoy_market_value_pct") is not None:
        kpi["yoy_pct"]   = round(float(metrics_by_year[2025]["yoy_market_value_pct"]), 1)
        kpi["yoy_label"] = "2024 → 2025"

    if current and current.get("assessed_value") and current.get("market_value"):
        kpi["assessment_ratio"]      = round(current["assessed_value"] / current["market_value"] * 100, 1)
        kpi["assessment_ratio_year"] = 2025
    elif current_2026 and current_2026.get("est_assessed_2026") and current_2026.get("market_value"):
        kpi["assessment_ratio"]      = round(current_2026["est_assessed_2026"] / current_2026["market_value"] * 100, 1)
        kpi["assessment_ratio_year"] = 2026

    _m25 = metrics_by_year.get(2025)
    if _m25 and _m25.get("effective_tax_rate") is not None:
        kpi["effective_tax_rate"]      = round(float(_m25["effective_tax_rate"]) * 100, 4)
        kpi["effective_tax_rate_year"] = 2025
        # Masking-bug fix (July 2026, per Diego): effective_tax_rate_derived is TRUE
        # when the figure above came from summing tax_billing_entity.amount_due rather
        # than a real tax_billing.total_tax value -- same provenance concept as
        # total_tax_derived elsewhere on this page. Deliberately NOT coerced with
        # bool() here: that would silently turn a missing/pre-recompute None into
        # False and badge those rows "Verified" by accident. Passed through as-is
        # (True / False / None) so the template only shows "Verified" when this is
        # explicitly False -- True *and* None (not yet recomputed) both fall through
        # to the Partial treatment, fail-safe rather than fail-open.
        kpi["effective_tax_rate_derived"] = _m25.get("effective_tax_rate_derived")
        # Real weakest-link confidence (July 2026, per Fable review P0-3 --
        # "confidence doesn't propagate through derived figures"). The
        # effective_tax_rate_derived flag above is a NARROW signal (was this
        # specifically reconstructed by summing tax_billing_entity amounts)
        # -- it says nothing about a portal-scrape-sourced total_tax that
        # wasn't "derived" in that narrow sense but still isn't genuinely
        # verified, and says nothing at all about whether the market_value
        # denominator was even certified. A quotient can't be more certain
        # than either of its real inputs, so this badge now combines BOTH,
        # via the same shared combine_confidence_tiers() export_due_diligence_pdf()
        # uses: Total Tax's real confidence (current.is_billing_verified,
        # the same field the "Billing: 2025 ..." badge above already reads)
        # and Market Value's confidence (current.data_source via the same
        # _row_confidence() the PDF export and /api/search_filter already
        # use, reused here rather than a third hand-rolled tier mapping).
        # AV>MV anomaly check (July 2026 fix): pass assessed/market values
        # through so a certified-tier row that fails the per-record anomaly
        # check still demotes to Partial here, not just data_source alone.
        _mkt_tier = _row_confidence(current.get("data_source"), current.get("assessed_value"), current.get("market_value")) if current else "not_available"
        _tax_tier = bool(current and current.get("is_billing_verified"))
        kpi["effective_tax_rate_tier"], kpi["effective_tax_rate_note"] = combine_confidence_tiers([
            ("Market Value", _mkt_tier),
            ("Total Tax", _tax_tier),
        ])
    elif insights and insights.get("total_rate_2025"):
        # Fallback: if no billing data, show the combined rate as an approximation
        kpi["rate_approx"] = round(float(insights["total_rate_2025"]), 4)

    # Est. 2026 ETR (Estimated badge) — only when we could estimate 2026 tax.
    if est_etr_2026 is not None:
        kpi["effective_tax_rate_2026_est"] = est_etr_2026

    # ── Annual trends ────────────────────────────────────────────────────────
    # generate_property_narrative() (defined above) is intentionally no
    # longer called here (July 2026, per Diego's Copy review — Investor
    # mode, item 1): the on-page "Overview" collapsible it fed duplicated
    # the Investor Insight Report at lower quality -- see property.html's
    # own comment at the removed <details> block for the full writeup and
    # the flagged "repurpose as PDF abstract" follow-up. Function kept
    # in place, just unused, as the building block for that follow-up.
    annual_trends = compute_annual_trends(history, metrics_by_year, projections)

    # Estimated homestead savings for parcels without one (Part 2c). Computed
    # only for parcels classify.py identifies as Residential (or when
    # bench_label couldn't be determined at all -- Phase 2 metrics tables
    # not yet populated -- to avoid a false negative hiding real content
    # when classification is simply unavailable, not because it's non-
    # residential). Homeowner-mode gating fix (July 2026): this used to run
    # unconditionally for every parcel, which is how a commercial LLC-owned
    # restaurant (1201 S Lamar Blvd) ended up showing a homestead-savings
    # estimate -- homestead exemptions only apply to an owner-occupied
    # primary residence, categorically impossible for that parcel. The
    # template ALSO gates display on is_residential (defense in depth), but
    # gating the computation itself here means a non-residential parcel
    # never has a nonsensical hs_potential_savings value in scope at all --
    # not even for a future API/JSON consumer that might not re-check
    # bench_label the way this template now does.
    hs_potential_savings = None
    if not bench_label or bench_label == "Residential":
        # 10% cap term (July 2026, per Fable review P0-4): pass the 2026
        # preliminary assessed/market values through when available so
        # estimate_homestead_savings() can compute the cap-savings term
        # alongside the exemption-only figure it already produced. current_2026
        # is computed above (Two-Year Card Redesign); combined entity rate is
        # NOT threaded in here -- the function recomputes it from the same
        # entity_detail list it already walks, so it can never drift from
        # assumed_rate_2026 while avoiding a second rate parameter.
        hs_potential_savings = _tx_hs_savings(
            entity_detail, current.get("assessed_value") if current else None,
            preliminary_assessed=current_2026.get("assessed_value") if current_2026 else None,
            preliminary_market=current_2026.get("market_value") if current_2026 else None,
        )

    # Improvement Detail (per-parcel IMP_DET components) for the collapsible table.
    imp_det = []
    if parcel.get("imp_det_json"):
        try:
            imp_det = json.loads(parcel["imp_det_json"])
        except (ValueError, TypeError):
            imp_det = []

    return render_template(
        "property.html",
        parcel=parcel,
        imp_det=imp_det,
        history=history,
        units=units,
        units_tax_year=latest_unit_year,
        rate_history=rate_history,
        current=current,
        current_2026=current_2026,
        is_2026_certified=is_2026_certified,
        prelim_2026_snapshot=prelim_2026_snapshot,
        tax_calendar=tax_calendar,
        entity_detail=entity_detail,
        delinquent=delinquent,
        # As-of date for the Delinquency panel (July 2026, per Diego's
        # "Delinquency Data Freshness" Cowork brief) -- see
        # TAX_DELQ_EXPORT_DATE's own comment (top of this file) for how this
        # was sourced. Passed through even when `delinquent` is None; the
        # template only renders it inside the same `{% if delinquent... %}`
        # guard the rest of the panel already uses.
        delinquent_export_date=TAX_DELQ_EXPORT_DATE,
        insights=insights,
        projections=projections,
        proj_bands=proj_bands,
        proj_baseline=proj_baseline,
        metrics_by_year=metrics_by_year,
        benchmark_by_year=benchmark_by_year,
        bench_label=bench_label,
        state_cd_descriptions=STATE_CD_DESCRIPTIONS,
        use_code_lookup=USE_CODE_LOOKUP,
        val_method=(
            USE_CODE_LOOKUP.get(parcel.get("classi_cd") or "", ("", ""))[1]
            or get_valuation_method(parcel.get("state_cd1") or "")
        ),
        entity_rate_by_code=entity_rate_by_code,
        chart_entity_data=chart_entity_data,
        chart_years=chart_years,
        estimated_tax_2026=estimated_tax_2026,
        assumed_rate_2026=assumed_rate_2026,
        kpi=kpi,
        annual_trends=annual_trends,
        hs_potential_savings=hs_potential_savings,
        bill_waterfall=bill_waterfall,
    )


@app.route("/<county_slug>/parcel/<geo_id>/export.pdf")
@limiter.limit(_LIMIT_HEAVY)
def export_due_diligence_pdf(geo_id):
    """
    "Tax Due Diligence" PDF export (July 2026, per Diego's outside-review
    brief, item 3). The review flagged this as the platform's actual
    monetization wedge — free to view the page, a citable, exportable
    one-page(ish) PDF as the premium feature. Diego hasn't built
    accounts/paywall infrastructure yet, so per the brief's explicit scope
    this route is freely available — gating it is a separate, later task.

    PDF LIBRARY CHOICE (investigated, not assumed, before committing):
    checked what's actually installed in this sandbox rather than assuming.
    wkhtmltopdf (the binary `pdfkit` wraps) is NOT installed and there's no
    root/sudo here to install it (same constraint that already blocked a
    local Postgres install earlier this session — see the
    PEER_SET_DISTRIBUTION_CHECK.sql round). weasyprint is not installed
    either (`pip3 show weasyprint` → not found). reportlab IS installed
    (4.5.1, confirmed via `import reportlab`) and needs no external binary
    at all — pure Python — so it's the only one of the four that's
    guaranteed to run wherever this app itself runs. Built directly with
    reportlab's platypus layout API (Table/Paragraph/SimpleDocTemplate)
    rather than an HTML-to-PDF converter, since there's no HTML renderer
    available here regardless. NOT yet in requirements.txt — flagged below,
    add before deploying this route.

    CONTENTS (per Diego's brief — "citable, defensible enough to attach to
    an underwriting memo" framing): key verified figures WITH their real
    confidence tier preserved as text (a color badge doesn't survive being
    printed/described), the 2025 entity-level tax breakdown, a condensed
    multi-year value/tax history with a per-row confidence column, the
    Bill-Change Waterfall summary (build_bill_waterfall(), same exact
    function/numbers as the property page's new waterfall card — not a
    second, independently computed version), and real source citations.

    JUDGMENT CALL (flagged, not a silent shortcut): this route queries its
    own minimal data set rather than reusing property_detail()'s full
    pipeline. That function has been the subject of many careful,
    precisely-ordered fixes this session (see its own inline docstrings —
    several steps explicitly document "must run AFTER X"). Refactoring it to
    share state with this new route would be real risk to re-verify blind,
    with no live DB in this sandbox to test the refactor against. A
    shared-state refactor is possible later, with Diego's live verification,
    if he'd rather eliminate this duplication than keep two call sites.
    """
    # PX-20260828-12 fix: this fetch lacked county_code (same audit-wide gap
    # as the three queries the DALLAS-GATE-2 Part 2 comment below already
    # documents/fixes for this route -- this one predates that pass too).
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, g.county_code), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": f"Parcel \"{geo_id}\" not found"}), 404

    # DALLAS-GATE-2 Part 2: this route duplicates property_detail()'s query
    # shapes (see this function's own docstring, "JUDGMENT CALL" section)
    # rather than sharing its pipeline -- which meant it did NOT inherit
    # property_detail()'s DALLAS-GATE-1 Part 2 county_code scoping when that
    # fix landed. The three queries below (history / entity_detail /
    # entity_detail_prev) are exactly verify_index_coverage.py's flagged
    # dynamic-WHERE-fragment gaps at app.py:2649/:2664/:2676 -- each filtered
    # tax_billing / tax_billing_entity (both county_code-leading composite-PK
    # tables per migrate_county_partitioning.py's TABLE_SPECS) by geo_id alone.
    # county_code added below as an ADDITIONAL predicate only.
    county_code = g.county_code
    # PX-20260824-01: profile-driven source citations below (institutional
    # names/URLs), same county_code this route already resolves.
    county_profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])

    history = query("""
        SELECT pty.tax_year, pty.market_value, pty.assessed_value, pty.taxable_value,
               pty.hs_cap_loss, pty.exemption_codes, pty.data_source,
               tb.total_tax, tb.total_due, tb.is_delinquent,
               tb.data_source      AS billing_source,
               tb.confidence_level AS billing_confidence
        FROM   parcel_tax_year pty
        LEFT JOIN tax_billing tb ON tb.geo_id = pty.geo_id AND tb.tax_year = pty.tax_year
                                 AND tb.county_code = pty.county_code
        WHERE  pty.geo_id = %s AND pty.county_code = %s
        ORDER  BY pty.tax_year
    """, (geo_id, county_code))

    # Entity detail WITH prior-year (2024) rate, needed both for the key-figures
    # table and to feed build_bill_waterfall() below — same shape
    # property_detail()'s own entity_detail query uses.
    entity_detail = query("""
        SELECT tbe.entity_code, ctr.entity_name, ctr.rate,
               ctr_prev.rate AS rate_prev, tbe.amount_due
        FROM   tax_billing_entity tbe
        LEFT JOIN county_tax_rate ctr      ON ctr.entity_code      = tbe.entity_code
                                           AND ctr.tax_year         = 2025
                                           AND ctr.county_code      = tbe.county_code
        LEFT JOIN county_tax_rate ctr_prev ON ctr_prev.entity_code = tbe.entity_code
                                           AND ctr_prev.tax_year    = 2024
                                           AND ctr_prev.county_code = tbe.county_code
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025 AND tbe.county_code = %s
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id, county_code))

    entity_detail_prev = query("""
        SELECT tbe.entity_code, tbe.amount_due
        FROM   tax_billing_entity tbe
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2024 AND tbe.county_code = %s
    """, (geo_id, county_code))

    # PX-20260830-05 Task 1 (Bucket A): was WHERE geo_id = %s only -- tax_delinquent
    # is composite_pk-migrated (county_code-leading, see migrate_county_partitioning.py's
    # TABLE_SPECS), so a bare geo_id filter can't use the real leading-prefix index and,
    # more importantly, is a real tenant-scoping gap for the same route this whole
    # session's other queries already scope by county_code. county_code is already
    # resolved in this function's own scope (used by every query above it).
    delinquent = query("SELECT * FROM tax_delinquent WHERE geo_id = %s AND county_code = %s", (geo_id, county_code), one=True)

    current = next((r for r in history if r["tax_year"] == 2025), None)
    # 2026 Preliminary row (July 2026, per Diego's PDF feedback round): the
    # `history` query above already pulls every parcel_tax_year row with no
    # year filter, so a 2026 row is already present here whenever this parcel
    # has preliminary data loaded (load_2026_preliminary.py) -- no extra query
    # needed, same as how the live page's `current_2026` is derived.
    current_2026 = next((r for r in history if r["tax_year"] == 2026), None)

    # Confidence read directly from tax_billing.confidence_level -- same
    # source-level fields property_detail() now reads (app.py, July 2026 fix)
    # instead of each route re-deriving the verified/derived split from
    # total_tax truthiness. Keeping this identical to property_detail()'s
    # block (not calling a shared helper) is a deliberate, flagged judgment
    # call -- see this route's own docstring on why it stays self-contained.
    for _r in history:
        _r["total_tax_derived"]  = (_r.get("billing_confidence") == "derived")
        _r["is_billing_verified"] = (_r.get("billing_confidence") == "verified")

    # Same defensive fallback as property_detail(), for rows the write-time
    # fix/backfill hasn't reached yet.
    if (current is not None and current.get("billing_confidence") is None
            and not current.get("total_tax") and entity_detail):
        derived_tax = sum(e["amount_due"] for e in entity_detail if e["amount_due"])
        if derived_tax:
            current["total_tax"] = derived_tax
            current["total_tax_derived"] = True

    market_value     = current.get("market_value")   if current else None
    assessed_value   = current.get("assessed_value")  if current else None
    taxable_value    = current.get("taxable_value")   if current else None
    total_tax        = current.get("total_tax")       if current else None
    eff_rate = (float(total_tax) / float(market_value) * 100) if (total_tax and market_value) else None
    assessment_ratio = (float(assessed_value) / float(market_value) * 100) if (assessed_value and market_value) else None

    # Same tiering Card 2 of the Investor KPI row uses (app.py render call
    # for property.html — "Total Tax" card): Verified / Partial (two
    # distinct reasons) / Not Available. Written out as plain text here
    # since a PDF has no badge color to lean on.
    if total_tax and (current.get("billing_source") == "portal_scrape"):
        tax_confidence = "Partial — amount paid per payment receipt, not necessarily the full levy"
        tax_tier = "partial"
    elif total_tax and current.get("total_tax_derived"):
        tax_confidence = "Partial — reconstructed by summing entity-level billing records"
        tax_tier = "partial"
    elif total_tax:
        tax_confidence = "Verified"
        tax_tier = "verified"
    else:
        tax_confidence = "Not Available"
        tax_tier = "not_available"

    # Appraisal-domain confidence for Market/Assessed/Taxable Value (bug fix,
    # per Diego's live-review pass): these three figures all come from the
    # SAME parcel_tax_year row, so they share ONE confidence tier driven by
    # that row's data_source — NOT the hardcoded "Verified"/blank this route
    # previously used. Mirrors templates/property.html's own "Property-level
    # confidence badge" block verbatim (search that file for that heading).
    # Tiering (July 2026, per Diego's "Fix AJR/Historical-Year Confidence
    # Tiering" brief -- see CERTIFIED_TIER_DATA_SOURCES/_row_confidence()'s
    # own docstrings for the full rationale):
    #   data_source is a certified export of ANY vintage (2025's own
    #   'certified', or a 2021-2024 cert_202x/ajr_202x historical export --
    #   all the same certifying chief appraiser's data under Tax Code
    #   Sec.26.01(b)) AND assessed_value <= market_value for this record
    #                                             -> Verified
    #   same, but assessed_value > market_value for THIS record
    #                                             -> Partial (real per-record
    #                                                anomaly, not a blanket
    #                                                source-level penalty)
    #   data_source == 'preliminary'              -> Preliminary
    #   anything else (legacy NULL, unrecognized) -> Partial (unchanged
    #                                                safe default)
    # Same tiering _row_confidence() (app.py, used by /api/search_filter)
    # already codifies for exactly this reason — reused here by name so this
    # route can never drift from that single source of truth.
    if current:
        appraisal_tier = _row_confidence(current.get("data_source"), current.get("assessed_value"), current.get("market_value"))
    else:
        appraisal_tier = None
    # .get() with a default, not a direct dict index (per Diego's live-review
    # pass): read _row_confidence()'s actual body to confirm what it can
    # return today — three unconditional if/if/return branches ("verified" /
    # "preliminary" / "partial"), no other exit path, never raises — so this
    # dict IS exhaustive as of right now. But that function's OWN docstring
    # frames "only 3 of 5 tiers reachable" as true in ITS ORIGINAL calling
    # context (/api/search_filter's INNER JOIN), not as a permanent contract
    # for every future caller — this route is a second, different caller it
    # was never written with in mind. A future 4th/5th branch added there
    # (e.g. an "estimated" tier) would silently KeyError here with a plain
    # index; .get(..., "Not Available") degrades instead of crashing.
    #
    # "Partial" wording updated (July 2026 fix): the old text ("appraisal
    # certification status isn't tracked for this record") described the
    # PRE-fix blanket rule -- a row could only be Partial because its
    # data_source wasn't literally 'certified', regardless of the record
    # itself. Now that the tier is driven by the actual per-record AV>MV
    # anomaly (or a genuinely unrecognized data_source), the wording says so
    # instead of implying an untracked/unknown status that no longer
    # accurately describes why a row lands here.
    appraisal_confidence = {
        "verified": "Verified",
        "preliminary": "Preliminary",
        "partial": "Partial — assessed value exceeds market value in this year's source data, or this record's certification status is unrecognized",
    }.get(appraisal_tier, "Not Available")

    # Same tiering for the 2026 Preliminary column (Current & Preliminary
    # Values table below) -- mirrors templates/property.html's own column
    # header badges ("2026 Preliminary" only appears when current_2026
    # exists) rather than inventing new confidence logic for this PDF. Not
    # passing assessed/market values here: 'preliminary' isn't in
    # CERTIFIED_TIER_DATA_SOURCES, so the anomaly check never runs for this
    # branch regardless -- nothing to demote it from.
    appraisal_tier_2026 = _row_confidence(current_2026.get("data_source")) if current_2026 else None

    # Effective Tax Rate = total_tax ÷ market_value, so its confidence is
    # only as strong as the WEAKER of the two inputs it actually divides —
    # never just inherits Total Tax's own badge unmodified. Now calls the
    # shared combine_confidence_tiers() (July 2026, per Fable review P0-3)
    # instead of its own hand-rolled version -- see that function's
    # docstring for why. Wording changes slightly (names every weak input,
    # not just the first one checked) but the underlying Verified/Partial/
    # Not Available determination is unchanged.
    if eff_rate is None:
        eff_rate_confidence = "Not Available"
    else:
        _, eff_rate_confidence = combine_confidence_tiers([
            ("Market Value", appraisal_tier),
            ("Total Tax", tax_tier),
        ])

    bill_waterfall = build_bill_waterfall(
        history, entity_detail, entity_detail_prev, cur_year=2025, prior_year=2024
    )

    # ── Build the PDF ───────────────────────────────────────────────────────
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.65 * inch, rightMargin=0.65 * inch,
        title=f"Parcelytics Tax Due Diligence Report — {geo_id}",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=16, spaceAfter=2)
    h2    = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, spaceBefore=14, spaceAfter=4)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8, textColor=rl_colors.HexColor("#666666"))
    body  = styles["Normal"]
    # Rendering-bug fix (found while re-testing the confidence-label fix
    # below): the longer confidence strings ("Partial — reconstructed by
    # summing entity-level billing records") are plain strings, not
    # flowables — reportlab's Table does NOT wrap plain strings, so a string
    # wider than its column silently overflows LEFT into the neighboring
    # column's already-rendered text (confirmed visually via a rendered PNG:
    # the Total Tax row's "$21,000" and its confidence note were literally
    # overlapping, illegible). note_style + note() wrap any longer free-text
    # cell in a real Paragraph, which reportlab wraps properly within the
    # cell's own width — no more overlap regardless of string length.
    note_style = ParagraphStyle("Note", parent=styles["Normal"], fontSize=8, leading=10, alignment=0)  # 0 = left

    def note(text):
        return Paragraph(text, note_style)

    def styled_table(rows, col_widths, header=True, left_align_cols=()):
        t = Table(rows, colWidths=col_widths)
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#dddddd")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ]
        for col in left_align_cols:
            style.append(("ALIGN", (col, 0), (col, -1), "LEFT"))
        if header:
            style += [
                ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#f2f2f2")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        t.setStyle(TableStyle(style))
        return t

    story = []
    story.append(Paragraph("Parcelytics — Tax Due Diligence Report", title_style))
    addr = parcel.get("situs_address") or "Address not on file"
    story.append(Paragraph(f"{addr} &nbsp;·&nbsp; Geo ID {geo_id}", body))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y')} — for informational purposes; "
        f"verify official figures with {county_profile['cad_abbr']} / the "
        f"{county_profile['tax_office_name']} before relying on this for a transaction.", small
    ))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", color=rl_colors.HexColor("#dddddd")))

    # "Current & Preliminary Values" (July 2026, per Diego's PDF feedback
    # round): mirrors templates/property.html's own table of the same name
    # verbatim -- same three rows this route already had data for
    # (Market/Assessed/Taxable Value), now shown 2026 Preliminary alongside
    # 2025 Certified side by side instead of 2025-only, same column
    # labels/order the live page uses ("" | 2026 Preliminary | 2025 Certified
    # | Change). Land/Improvement Value (also on the live table) are left out
    # here -- this route doesn't query those columns and they're not part of
    # what makes this PDF a tax-focused due-diligence doc; reusing the live
    # page's STRUCTURE/labeling convention, not necessarily every one of its
    # rows, matches the brief ("reuse that same structure/labeling
    # convention rather than inventing new layout logic").
    story.append(Paragraph("Current &amp; Preliminary Values", h2))
    market_value_2026   = current_2026.get("market_value")   if current_2026 else None
    # Issue 1 fix: est_assessed_2026/est_taxable_2026 (derive_2026_baseline()
    # output, attached to current_2026 in property_detail()) instead of the
    # raw assessed_value/taxable_value fields -- same central-assembly-point
    # correction as the live page's KPI cards and Current & Preliminary
    # Values table. is_approx_2026_pdf drives the "~" prefix + note below,
    # mirroring property.html's tilde/is_approx convention rather than a
    # separate PDF-only wording.
    assessed_value_2026 = current_2026.get("est_assessed_2026") if current_2026 else None
    taxable_value_2026  = current_2026.get("est_taxable_2026")  if current_2026 else None
    is_approx_2026_pdf  = bool(current_2026 and current_2026.get("is_approx_2026"))

    def _pct_change(v2025, v2026):
        if not v2025 or v2026 is None:
            return "—"
        pct = (float(v2026) - float(v2025)) / float(v2025) * 100
        return f"{'+' if pct >= 0 else ''}{pct:.1f}%"

    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: this used to hardcode
    # "2026 Preliminary" unconditionally whenever current_2026 existed --
    # wrong as of today's 2026 certified load. appraisal_tier_2026
    # (computed just above via the same _row_confidence() this route
    # already uses) is the single source of truth for whether this
    # parcel's 2026 row is actually certified; reused here rather than a
    # second, independent data_source check.
    col_2026_label = (
        "2026 Certified" if appraisal_tier_2026 == "verified"
        else "2026 Preliminary" if current_2026 else "2026 (not yet available)"
    )
    col_2025_label = "2025 Certified" if appraisal_tier == "verified" else "2025"
    cv_rows = [["Metric", col_2026_label, col_2025_label, "Change"]]
    cv_rows.append([
        "Market Value",
        f"${market_value_2026:,.0f}" if market_value_2026 else "—",
        f"${market_value:,.0f}" if market_value else "—",
        _pct_change(market_value, market_value_2026),
    ])
    _av26_prefix = "~" if is_approx_2026_pdf else ""
    _tv26_prefix = "~" if is_approx_2026_pdf else ""
    cv_rows.append([
        "Assessed Value",
        f"{_av26_prefix}${assessed_value_2026:,.0f}" if assessed_value_2026 else "—",
        f"${assessed_value:,.0f}" if assessed_value else "—",
        _pct_change(assessed_value, assessed_value_2026),
    ])
    cv_rows.append([
        "Taxable Value",
        f"{_tv26_prefix}${taxable_value_2026:,.0f}" if taxable_value_2026 else "—",
        f"${taxable_value:,.0f}" if taxable_value else "—",
        _pct_change(taxable_value, taxable_value_2026),
    ])
    story.append(styled_table(cv_rows, [150, 130, 130, 90]))
    # Confidence for these three rows is per-COLUMN (all three share one
    # data_source per year), same as the live page's column-header badges --
    # not a fourth per-row column, so the caveat is one line beneath instead.
    conf_2026_note = {
        "verified": "Verified", "preliminary": "Preliminary — subject to change until certification",
    }.get(appraisal_tier_2026, "Not yet available" if not current_2026 else "Partial — certification status not tracked")
    if is_approx_2026_pdf:
        # Issue 1: Assessed/Taxable Value are a derived estimate (TCAD's own
        # preliminary export shows the pre-cap figure), not a real TCAD
        # number -- the "~" prefix alone isn't enough in a PDF someone may
        # print/forward without the live page's tooltip; state it in the note.
        conf_2026_note += (
            f". Assessed/Taxable Value are Parcelytics estimates of the capped value "
            f"({county_profile['cad_abbr']}'s preliminary export doesn't yet reflect the "
            f"homestead cap for this parcel) — Market Value is {county_profile['cad_abbr']}'s real figure."
        )
    story.append(Paragraph(
        f"<i>2026 column: {conf_2026_note}. 2025 column: {appraisal_confidence}.</i>", small
    ))

    story.append(Paragraph("2025 Billing &amp; Tax Rate", h2))
    key_rows = [["Metric", "Value", "Confidence / Note"]]
    key_rows.append(["Effective Tax Rate", f"{eff_rate:.4f}%" if eff_rate is not None else "—", note(eff_rate_confidence)])
    key_rows.append(["Total Tax", f"${float(total_tax):,.0f}" if total_tax else "—", note(tax_confidence)])
    if assessment_ratio is not None and abs(assessment_ratio - 100) >= 0.5:
        key_rows.append([
            "Assessment Ratio", f"{assessment_ratio:.1f}%",
            note("Below typical ~100%" if assessment_ratio < 100 else "Above typical ~100% — data anomaly"),
        ])
    if delinquent and delinquent.get("total_due"):
        # As-of date added (July 2026, per Diego's follow-up to the
        # "Delinquency Data Freshness" Cowork brief): reuses
        # TAX_DELQ_EXPORT_DATE, the same constant the property page's own
        # Delinquency panels (both modes) already surface -- see that
        # constant's own comment (top of this file) for how it was sourced.
        # Without this, the PDF's own delinquent figure carried no date at
        # all, silently contradicting the page it's exported from once that
        # page started showing one.
        key_rows.append([
            "Delinquent Taxes", f"${float(delinquent['total_due']):,.0f}",
            note(f"Since {delinquent.get('first_delinquent_yr') or '—'} — "
                 f"as of {TAX_DELQ_EXPORT_DATE.strftime('%B %-d, %Y')}, grows monthly (Tax Code §33.01)"),
        ])
    # Column 2 (Confidence / Note) now holds Paragraph flowables (see note()
    # above) instead of plain strings, so long confidence text wraps within
    # its own cell instead of overflowing into the Value column — confirmed
    # visually via a rendered PNG that this previously overlapped/garbled
    # the "Total Tax" row specifically (any confidence string longer than
    # ~30 characters, e.g. the "reconstructed by summing..." case).
    story.append(styled_table(key_rows, [150, 110, 240], left_align_cols=[2]))

    if entity_detail and any(e.get("amount_due") for e in entity_detail):
        story.append(Paragraph("2025 Entity-Level Tax Breakdown", h2))
        ent_rows = [["Taxing Entity", "Rate", "Amount Due"]]
        for e in entity_detail:
            if not e.get("amount_due"):
                continue
            ent_rows.append([
                note(e.get("entity_name") or e["entity_code"]),  # same overflow risk for long entity names
                f"{float(e['rate']):.4f}%" if e.get("rate") is not None else "—",
                f"${float(e['amount_due']):,.2f}",
            ])
        story.append(styled_table(ent_rows, [230, 90, 120], left_align_cols=[0]))

    story.append(Paragraph("Value &amp; Tax History", h2))
    # Collapsed back to a single "Confidence" column (July 2026, per Diego's
    # live-review pass on the two-column version): Diego found two separate
    # columns is more than he needs day-to-day. Rather than just picking one
    # domain and dropping the other, this combines them with the same
    # weakest-link logic eff_rate_confidence (above) already uses for
    # Effective Tax Rate -- take the LOWER tier of appraisal (value) vs.
    # billing, and when it's not fully Verified, name what's actually
    # holding it back, so the real distinction that justified having two
    # columns stays visible in the text instead of just disappearing.
    # Now a thin wrapper around the shared combine_confidence_tiers() (July
    # 2026, per Fable review P0-3) instead of its own hand-rolled copy of
    # the same weakest-link idea -- see that function's docstring. Only
    # behavioral difference from the prior version: when BOTH appraisal and
    # billing are non-Verified, the note now names both reasons (the shared
    # function always lists every weak input), where this used to as well
    # ("appraisal is X and billing Y") -- same substance, slightly
    # different wording.
    def combine_confidence(value_tier, billing_tier):
        tier, note_text = combine_confidence_tiers([
            ("Appraisal", value_tier),
            ("Billing", billing_tier),
        ])
        return "N/A" if tier == "not_available" else note_text

    hist_rows = [["Year", "Market Value", "Assessed Value", "Total Tax", "Confidence"]]
    for r in sorted(history, key=lambda r: r["tax_year"]):
        value_conf_tier = _row_confidence(r.get("data_source"), r.get("assessed_value"), r.get("market_value")) if r.get("market_value") else None

        if r.get("billing_confidence") == "verified" or r.get("is_billing_verified"):
            billing_tier = "verified"
        elif r.get("total_tax"):
            billing_tier = "partial"
        else:
            billing_tier = None

        combined_conf = combine_confidence(value_conf_tier, billing_tier)
        hist_rows.append([
            str(r["tax_year"]),
            f"${r['market_value']:,.0f}" if r.get("market_value") else "—",
            f"${r['assessed_value']:,.0f}" if r.get("assessed_value") else "—",
            f"${float(r['total_tax']):,.0f}" if r.get("total_tax") else "—",
            note(combined_conf),
        ])
    # Confidence column widened (was two ~65-70pt columns, now one) since the
    # combined text can run longer than either original column's contents --
    # wrapped via note() so a long reason string never overflows into the
    # next column (same fix already applied to the Key Figures table).
    story.append(styled_table(hist_rows, [40, 110, 110, 85, 160], left_align_cols=[4]))

    if bill_waterfall:
        story.append(Paragraph(
            # Plain ASCII arrow ("to", not "→") -- reportlab's base
            # Helvetica font (WinAnsiEncoding) doesn't cleanly map the
            # Unicode arrow glyph; confirmed via a rendered test PDF where
            # "→" came out as a garbled "fi" instead of an arrow.
            f"Why This Bill Changed ({bill_waterfall['prior_year']} to {bill_waterfall['cur_year']})", h2
        ))
        def fmt_effect(v):
            # Bug fix (July 2026, Property Page Small Bugs Batch item 5,
            # found during last round's PDF work): the sign was applied
            # OUTSIDE the ${:,.0f} format, so a negative value rendered as
            # "$-1,500" (sign inside the dollar amount) instead of the
            # conventional "-$1,500" (sign in front of the currency symbol).
            # Positive case was already correct ("+$1,500"); only the
            # negative branch needed the sign moved in front of "$".
            sign = "+" if v >= 0 else "-"
            return f"{sign}${abs(v):,.0f}"
        wf_rows = [["Effect", "Amount"]]
        wf_rows.append([f"{bill_waterfall['prior_year']} Total", f"${bill_waterfall['start_total']:,.0f}"])
        wf_rows.append(["Value Change", fmt_effect(bill_waterfall["value_effect"])])
        wf_rows.append(["Rate Change", fmt_effect(bill_waterfall["rate_effect"])])
        wf_rows.append(["Exemption Change", fmt_effect(bill_waterfall["exemption_effect"])])
        if abs(bill_waterfall["other_effect"]) >= 1:
            wf_rows.append(["Other / Unmatched", fmt_effect(bill_waterfall["other_effect"])])
        wf_rows.append([f"{bill_waterfall['cur_year']} Total", f"${bill_waterfall['end_total']:,.0f}"])
        story.append(styled_table(wf_rows, [220, 130]))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", color=rl_colors.HexColor("#dddddd")))
    story.append(Paragraph("Sources", h2))
    for s in [
        # NOTE (PX-20260824-01): institutional names below are profile-driven;
        # the named-dataset/document identifiers (Rates History, TaxCurOpenData,
        # PIR bulk billing export, TaxDelqOpenData) are Travis's actual system
        # names and are NOT converted -- no COUNTY_PROFILES field confirms an
        # equivalent named system exists for other counties. Flagged, not guessed.
        f"Market / assessed / taxable values: {county_profile['cad_name']} ({county_profile['cad_abbr']}) Certified Appraisal Roll.",
        "Entity tax rates: Travis County Rates History (1990–2025), as adopted by each taxing entity.",
        f"Current-year billing: {county_profile['tax_office_name']} current-year billing data (TaxCurOpenData).",
        f"Prior-year billing (2021–2024): {county_profile['tax_office_name']} PIR bulk billing export, cross-verified "
        "against known sanity-check accounts.",
        f"Delinquency status: {county_profile['tax_office_name']} delinquent-account data (TaxDelqOpenData), where applicable — "
        f"as of {TAX_DELQ_EXPORT_DATE.strftime('%B %-d, %Y')}; balance grows monthly under Tax Code §33.01.",
    ]:
        # Plain hyphen, not "•" -- same base-font glyph-mapping issue as the
        # arrow above (confirmed via test PDF: "•" came out as "(cid:127)"
        # in extracted text, not a real bullet character).
        story.append(Paragraph("- " + s, small))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Generated by Parcelytics. This report reflects the most recent verified county data "
        f"available in the system at the time of generation and is not an official "
        f"{county_profile['cad_abbr']} or {county_profile['tax_office_name']} document.", small
    ))

    doc.build(story)
    buf.seek(0)
    filename = f"parcelytics_due_diligence_{geo_id}.pdf"
    return Response(
        buf.read(), mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# Display order for the Rate Trends entity selector's category groups
# (Part 3 — see categorize_entity() docstring for how membership is decided).
ENTITY_CATEGORY_ORDER = ["School District", "County", "City", "Hospital District", "MUD/WCID", "Other"]


def categorize_entity(code, name, county_code=None):
    """
    Infer a display category for a taxing entity, for grouping the Rate
    Trends page's entity selector (Task: Rate Trends Page brief, Part 3).

    There is no category/type column on county_tax_rate (confirmed via
    schema.sql) and no other entity-classification table exists in this
    codebase — so this is a lightweight, RULE-BASED INFERENCE from the
    entity_name text (sourced from the county's own JURISNAME column in
    2025RatesHistory1990-2025.xlsx), in the same spirit as how
    tax_logic/classify.py infers property-type buckets from state_cd1
    prefixes elsewhere in this app. It is NOT an authoritative legal
    classification — flagging per the brief's explicit instruction rather
    than presenting this as more certain than it is.

    Known imperfection, left as-is rather than hand-patched (see brief
    conversation / final report): 3 of the 4 "Pilot Knob" MUDs (U4M, U4P,
    U4R) land in "Other" because the source spreadsheet's JURISNAME text
    for those three omits the word "MUD" (unlike the 4th, U4N "Pilot Knob
    MUD #4", which matches correctly) — a naming inconsistency in the
    county's own source file, not something this function special-cases.

    PX-20260824-01: `county_code` (optional, defaults to Travis's own
    profile if omitted) replaces the previous hardcoded `n == "TRAVIS
    COUNTY"` literal comparison — that line matched the *current* county's
    own line-item in its rate table by exact string equality, not a
    general pattern, so it could never have matched Dallas's real
    "DALLAS COUNTY" row. Read via COUNTY_PROFILES rather than re-deriving
    the county's own name from `name` itself (the string being classified
    IS the thing being compared against, so there's nothing else to derive
    it from).

    The other four rules (ISD suffix, "CITY OF"/"VILLAGE OF" prefix, HEALTH
    substring for hospital districts, MUD/WCID/WSID/UTILITY DISTRICT for
    special districts) are NOT county-specific literals -- they're
    statewide Texas administrative-naming conventions (independent school
    district names always end "ISD", municipalities are always styled
    "City of X", MUD/WCID/WSID are standard Texas Water Code district
    abbreviations used across every TX county, not just Travis's). Only
    the exact "county's own row" comparison was genuinely Travis-only;
    changed here. Left AS A REAL, NAMED LIMITATION rather than silently
    assumed to transfer: whether Dallas's real JURISNAME text actually
    follows these same conventions is unverified until real Dallas rate
    data is loaded and read -- this fix makes the mechanism correct, it
    does not verify Dallas's real strings against it (that requires real
    Dallas source data this sandbox doesn't have).
    """
    n = (name or "").upper()
    county_name = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])["county_name"]
    if "ISD" in n:
        return "School District"
    if n == county_name.upper():
        return "County"
    if "CITY OF" in n or "VILLAGE OF" in n:
        return "City"
    if "HEALTH" in n:                 # e.g. THD = "Travis Central Health", the county hospital district
        return "Hospital District"
    if "MUD" in n or "WCID" in n or "WSID" in n or "UTILITY DISTRICT" in n:
        return "MUD/WCID"
    return "Other"   # ESDs, road districts, limited districts, disannexed
                      # entries, Austin Community College, and anything else
                      # that doesn't match a bucket above.


def _most_recent_entity_names(rows):
    """Deterministic display-name-per-entity_code rule (PX-20260830-01
    Task 3). county_tax_rate stores the RAW entity_name string per row;
    entity_code is the canonical join key. After a DALLAS_ENTITY_ALIASES
    merge, one code can carry several distinct raw spellings across its
    years (real example: DAL6A7524C now carries 'Oak Hollow Sheffield
    PID', 'OakHollowSheffield PID', AND 'OakHollowSheffield' across its
    merged 2015-2025 span) -- whatever picks ONE name to show for that
    code must do so deterministically, not "whichever raw string a
    GROUP BY/dict-overwrite happens to surface first."

    Real finding this replaces: the previous inline version of this logic
    (a bare `entity_names[code] = r["entity_name"]` inside the same loop
    that builds by_entity, run over rows already sorted `ORDER BY
    entity_code, tax_year` ASC) happened to ALREADY land on the most-
    recent year's spelling for every code -- but only as a side effect of
    that specific SQL ORDER BY combined with plain dict-overwrite
    semantics (last write wins). That was never a documented rule
    anywhere; a future change to the query's ORDER BY, or any other
    caller feeding rows in a different order, would have silently changed
    which spelling wins with no test to catch it. This function makes the
    SAME rule (PM's own suggested default: most-recent tax_year's name
    wins, since that matches what the source currently calls the entity)
    explicit and independent of row order.

    Generic/county-agnostic per Task 3's own requirement -- takes any
    iterable of rows carrying entity_code/entity_name/tax_year (works
    identically whether `rows` came from a Dallas query, a Travis query,
    or any future county's; Travis has the same theoretical exposure even
    though no real drift has been observed there yet).

    Returns {entity_code: entity_name} using each code's highest tax_year
    row (ties are impossible -- (county_code, entity_code, tax_year) is
    this table's real primary key)."""
    names = {}
    best_year = {}
    for r in rows:
        code = r["entity_code"]
        year = r["tax_year"]
        if code not in best_year or year >= best_year[code]:
            names[code] = r["entity_name"]
            best_year[code] = year
    return names


def _rates_response(county_code, county_slug_val, api_county_slug=None):
    """Shared body for tax_rates() (county-anchored, '/<county_slug>/rates')
    and rates_landing() (neutral, bare '/rates') -- PX-20260828-01, same
    refactor pattern as _home_search_response()/_info_response(). county_
    profile/county_slug/api_county_slug are passed explicitly so
    rates_landing() can render the FILTERED county's rate data without
    ever setting g.county_slug/g.county_code (see rates_landing()'s own
    docstring for why).

    PX-20260828-15 Task 1: api_county_slug is a separate parameter from
    county_slug_val (defaults to it, so tax_rates()'s anchored call is
    unaffected) -- see _info_response()'s identical PX-20260828-15
    comment for why the two values need to diverge for the neutral
    landing route specifically."""
    # DALLAS-GATE-2 Part 1: county_tax_rate is a county_code-leading
    # composite-PK table per migrate_county_partitioning.py's TABLE_SPECS
    # (old_pk ["entity_code", "tax_year"] -> new_pk ["county_code",
    # "entity_code", "tax_year"]). This route is explicitly named in
    # DALLAS-GATE-2's brief ("/rates"). county_code added as an ADDITIONAL
    # predicate only -- ORDER BY/shape unchanged.

    # Key entities to highlight in the main chart
    KEY_ENTITIES = ["TCO", "IAU", "CAT", "THD", "ACT"]

    # Part 0 finding: this previously read "WHERE tax_year >= 2006", an
    # undocumented restriction that contradicted both the source file
    # (2025RatesHistory1990-2025.xlsx genuinely has RATE90…RATE25 — 36
    # years, confirmed directly from the workbook) and every other page's
    # own "rates back to 1990" claims (index.html, about.html, base.html
    # footer). No WHERE clause is needed at all — county_tax_rate only ever
    # gets rows from that same 1990-2025 loader — so the full confirmed
    # range is used here rather than re-imposing an arbitrary floor.
    # PX-20260829-07 Task 2 (approved schema/display design): mo_rate/is_rate
    # added to schema.sql's county_tax_rate (nullable, populated only when a
    # county's source actually publishes the M&O/I&S split -- Dallas's DCTO
    # rate pages do, back to 2015; Travis's loaded 1990-2025 blended workbook
    # does not, and stays total-only per Diego's own Task 1 ruling: "don't
    # layer a 5-year split source on top of the 36-year blended file"). Added
    # to the SELECT unconditionally -- NULL for every Travis row until/unless
    # that separate follow-up brief happens, same as any other per-county
    # source gap this page already handles via data_unavailable-style flags.
    rates = query("""
        SELECT entity_code, entity_name, tax_year, rate, mo_rate, is_rate
        FROM   county_tax_rate
        WHERE  county_code = %(county_code)s
        ORDER  BY entity_code, tax_year
    """, {"county_code": county_code})

    profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])

    # PX-20260828-07 Task 1 (real bug, fixed under the same brief's Task 3
    # go-ahead): _rates_response()'s own SQL above has been county_code-
    # scoped since DALLAS-GATE-2, but county_tax_rate has never actually had
    # any Dallas rows loaded -- loaders/load_tax_rates.py only ever reads
    # config.TAX_RATES_XL, a single hardcoded Travis-only workbook, and no
    # Dallas rates source file is registered anywhere. Before this fix, a
    # zero-row county silently rendered an empty "Entities" sidebar under a
    # header that STILL claimed a real "1990-2025" range (the year_min/
    # year_max fallback further below exists ONLY for this empty case, and
    # was never meant to be read as a genuine coverage claim -- but with no
    # honesty affordance at all, it looked like one). Same data_unavailable
    # / data_unavailable_reason mechanism _compute_snapshot_data() (app.py)
    # / snapshot.html already use for Market Snapshot's own "missing
    # precomputed data" case -- not a new pattern, per Diego's explicit
    # instruction to mirror it rather than invent something new.
    data_unavailable = not rates
    data_unavailable_reason = None
    if data_unavailable:
        # PX-20260830-04 Task 1: was a raw f-string naming county_tax_rate
        # and loaders/load_tax_rates.py directly -- also factually wrong for
        # Dallas specifically, which is loaded via a different file
        # (loaders/load_dallas_tax_rates.py) and in any case is no longer
        # reachable for Dallas today (PX-20260829-07 loaded its rates); this
        # branch now only fires for a genuinely not-yet-loaded county (e.g.
        # Harris, reserved). See unavailable_copy()'s own docstring.
        data_unavailable_reason = unavailable_copy(
            "being_prepared", profile["county_name"], page_label="tax rate history", view_label="rate data"
        )

    # Build {entity_code: [{year, rate, mo_rate, is_rate}, …]} structure for JS.
    # PX-20260829-07 Task 2: mo_rate/is_rate carried through as None (not 0.0)
    # when the source doesn't publish them for that entity/year -- same
    # never-coerce-a-gap-to-zero rule loaders/dallas_rates_format.py's own
    # _parse_rate_cell() already enforces at parse time; this is just not
    # re-breaking that discipline on the read side.
    by_entity = {}
    # PX-20260830-01 Task 3: entity_names is now built by a dedicated,
    # order-independent function -- see _most_recent_entity_names()'s own
    # docstring for the real display-drift risk this closes (previously
    # this was a bare per-row dict-overwrite in the loop below, which only
    # happened to land on the most-recent spelling as a side effect of
    # this query's own ORDER BY).
    entity_names = _most_recent_entity_names(rates)
    has_rate_split = False
    for r in rates:
        code = r["entity_code"]
        mo_rate = float(r["mo_rate"]) if r["mo_rate"] is not None else None
        is_rate = float(r["is_rate"]) if r["is_rate"] is not None else None
        if mo_rate is not None or is_rate is not None:
            has_rate_split = True
        by_entity.setdefault(code, []).append({
            "year": r["tax_year"],
            "rate": float(r["rate"]) if r["rate"] else None,
            "mo_rate": mo_rate,
            "is_rate": is_rate,
        })

    # Actual available year range, computed from what's really loaded
    # rather than hardcoded — avoids a number on the page that could
    # silently drift from the real data over time.
    all_years = [r["tax_year"] for r in rates]
    year_min = min(all_years) if all_years else 1990
    year_max = max(all_years) if all_years else 2025
    # Default window: most recent 10 years, matching the page's existing
    # "10-year rate history chart" framing, not the full 35-year span.
    default_year_from = max(year_min, year_max - 9)

    # All available entities for the selector, grouped by inferred category
    # (Part 3). category_rank lets the template sort by ENTITY_CATEGORY_ORDER
    # without re-implementing that ordering in Jinja.
    category_rank = {cat: i for i, cat in enumerate(ENTITY_CATEGORY_ORDER)}
    all_entities = sorted(
        [
            {
                "code": code,
                "name": entity_names[code],
                "category": categorize_entity(code, entity_names[code], county_code),
            }
            for code in by_entity.keys()
        ],
        key=lambda e: (category_rank.get(e["category"], 999), e["name"] or "", e["code"]),
    )
    entity_category = {e["code"]: e["category"] for e in all_entities}

    return render_template(
        "rates.html",
        data_unavailable=data_unavailable,
        data_unavailable_reason=data_unavailable_reason,
        has_rate_split=has_rate_split,
        by_entity_json=json.dumps(by_entity),
        entity_names_json=json.dumps(entity_names),
        entity_category_json=json.dumps(entity_category),
        all_entities=all_entities,
        entity_category_order=ENTITY_CATEGORY_ORDER,
        key_entities=KEY_ENTITIES,
        year_min=year_min,
        year_max=year_max,
        default_year_from=default_year_from,
        county_profile=profile,
        county_slug=county_slug_val,
        api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,
    )


@app.route("/<county_slug>/rates")
def tax_rates():
    """Tax rate trend page — county-level, no parcel required.

    PX-20260828-01: Diego's 2026-08-28 ruling supersedes any assumption
    this page is Travis-only -- its own core query has been county_code-
    scoped since DALLAS-GATE-2. rates_landing() (bare '/rates') is now the
    real, nav-linked page, with its own in-page county filter; this
    county-anchored route is KEPT, unchanged, as a real direct deep link,
    the same relationship home()/search_landing()/info_landing() now have
    to their own anchored twins."""
    return _rates_response(g.county_code, g.county_slug)


@app.route("/rates")
def rates_landing():
    """PX-20260828-01: the real, neutral bare '/rates' page -- Diego's
    2026-08-28 ruling. Carries the filtered county via ?county=<slug>
    (e.g. '/rates?county=dallas-tx'), same mechanism and same fallback
    tolerance as info_landing() (absent/unrecognized value -> Travis, not
    a 404 -- see that route's docstring for the full reasoning). Also
    deliberately does NOT set g.county_slug/g.county_code, for the same
    is_county_anchored reason documented on info_landing().

    PX-20260828-15 Task 1: api_county_slug is resolved separately from
    the displayed county_slug via _resolve_landing_county_slug()'s
    `explicit` flag -- see info_landing()'s identical PX-20260828-15
    comment for the full reasoning (explicit ?county= still scopes the
    embedded search box; the no-?county-given fallback now gets the
    neutral, cross-county "" instead of silently inheriting Travis)."""
    county_code, slug, explicit = _resolve_landing_county_slug()
    return _rates_response(county_code, slug, api_county_slug=(slug if explicit else ""))


@app.route("/<county_slug>/api/parcel_entities")
def api_parcel_entities():
    """
    Rate Trends page, Part 5 — "which entities apply to my property".

    Resolves a parcel ID (reusing normalize_parcel_id(), the exact same
    function the "/" route uses — not a new ID-parsing scheme) and returns
    that parcel's 2025 billing entity codes, using the identical
    tax_billing_entity / tax_year=2025 convention already used by
    property_detail()'s entity_detail query.

    This endpoint intentionally does NOT duplicate api_address_search()'s
    address-text matching — the frontend calls that existing endpoint
    directly for the address-typeahead dropdown (same as Search/homepage),
    and calls this endpoint only with a geo_id (either typed directly, or
    taken from an api_address_search() result the user clicked). This is
    also why a bare address string here (e.g. "S Lamar") intentionally
    returns ok:false rather than attempting its own address search.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "No parcel ID or geo_id provided."})

    geo_id = normalize_parcel_id(q)
    # PX-20260828-12 fix: both parcel-resolution fetches below lacked
    # county_code (composite-PK table, geo_id alone not globally unique --
    # same audit-wide gap as compare_parcels()/api_estimate_acq()/
    # api_peer_benchmark_sf()/api_peer_benchmark_local()). Previously
    # tracked as "out of scope" by the DALLAS-GATE-2 Part 2 comment below;
    # folded into this fix since it's the same bug class.
    parcel = query(
        "SELECT geo_id, situs_address FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, g.county_code), one=True
    )
    if not parcel and q.isdigit():
        # Shared prop_id fallback (Migration M2) — see resolve_prop_id_to_geo_id()'s
        # docstring for why this now checks prop_unit before parcel.
        fallback_geo_id = resolve_prop_id_to_geo_id(int(q))
        if fallback_geo_id:
            parcel = query(
                "SELECT geo_id, situs_address FROM parcel WHERE geo_id = %s AND county_code = %s", (fallback_geo_id, g.county_code), one=True
            )
    if not parcel:
        return jsonify({"ok": False, "error": f"No parcel found matching \"{q}\"."})

    # DALLAS-GATE-2 Part 2: verify_index_coverage.py flagged this WHERE
    # clause's tax_billing_entity filter (geo_id + tax_year, no county_code)
    # -- a county_code-leading composite-PK table per migrate_county_
    # partitioning.py's TABLE_SPECS. county_code added as an ADDITIONAL
    # predicate only; the parcel-resolution queries above this block are
    # unchanged (out of this fix's scope -- tracked separately under
    # DALLAS-GATE-2 Part 1's route sweep).
    rows = query("""
        SELECT DISTINCT entity_code
        FROM   tax_billing_entity
        WHERE  geo_id = %s AND tax_year = 2025 AND county_code = %s
    """, (parcel["geo_id"], g.county_code))
    entity_codes = sorted(r["entity_code"] for r in rows)

    return jsonify({
        "ok": True,
        "geo_id": parcel["geo_id"],
        "situs_address": parcel["situs_address"] or "",
        "entity_codes": entity_codes,
    })


# ── Market Snapshot shared scoping — canonical exclusion filter + the
# view -> property-type WHERE fragment. Hoisted to module level so any
# route that needs the same "current Market Snapshot sector" population
# (originally just _compute_snapshot_data(), now also
# snapshot_neighborhood() below) reuses these exactly rather than each
# re-declaring its own copy of the same literal SQL.
#
# CANONICAL_PARCEL_EXCL (July 2026 fix): now imported from parcel_filters.py
# — the single canonical, NULL-safe definition every consumer in this
# codebase reuses (app.py's routes below, /api/benchmark, the /parcels
# drill-through, and loaders/compute_metrics.py's county_benchmark
# builder). See parcel_filters.py's own docstring for the full story: this
# used to be FOUR independently-typed copies of the same intended filter,
# one of which had already drifted (silently dropping fewer exclusions
# than the others), and none of which were NULL-safe against state_cd1 —
# which is what produced the ~20% Market Snapshot county-total undercount
# this fix addresses.

# view -> property_type_label, matching templates/snapshot.html's
# _view_to_prop_type Jinja mapping and search.html's SNAPSHOT_VIEW_BY_LABEL
# (inverse direction) — same 5-category classify.py taxonomy used
# everywhere on this site, not a new one for this route.
_SNAPSHOT_VIEW_PROP_TYPE_LABEL = {
    "residential": "Residential", "multifamily": "Multi-Family",
    "commercial": "Commercial", "land": "Land/Vacant", "agricultural": "Agricultural",
}


def _snapshot_view_where(view):
    """
    Property-type WHERE-clause fragment for a Market Snapshot `view`.

    New 8-tab-plus-Other views (residential/multifamily/retail/industrial/
    office/hotel/land/agricultural/other) route through the scoped
    _snapshot_taxonomy_sql() (see its docstring/big comment block above) --
    NOT classify.py's label_case_sql().

    "commercial" is kept as a LEGACY view, routed through the original
    canonical label_case_sql() unchanged -- this is not one of the 10 tabs
    on the page anymore, but /snapshot?view=commercial is still a live,
    working URL: the untouched nav sector dropdown (base.html) and Search's
    canonical Property Type filter (search.html's SNAPSHOT_VIEW_BY_LABEL)
    both still deep-link to it, and neither of those is in scope to change
    this round. See the big taxonomy comment block above for the full
    reasoning.

    "overall" returns "" since it spans every type, unrestricted (same as
    before).
    """
    if view in _SNAPSHOT_SECTOR_VIEWS:
        label = _SNAPSHOT_SECTOR_VIEWS[view]
        _tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
        return f"AND ({_tax}) = '{label}'"
    if view == "commercial":
        label = _SNAPSHOT_VIEW_PROP_TYPE_LABEL["commercial"]
        _lbl = label_case_sql("p.classi_cd", "p.state_cd1")
        return f"AND ({_lbl}) = '{label}'"
    return ""


# Part 1 performance fix — simple in-process cache for /snapshot, keyed by
# view. TTL-bounded rather than event-invalidated: this app's data reloads
# happen out-of-band via separate loader scripts (compute_metrics.py etc.),
# not through any live signal this long-running Flask process could listen
# for, so wiring up precise reload-triggered invalidation would mean adding
# new cross-process coordination (e.g. a sentinel file or admin endpoint)
# beyond what "simple" calls for. A 10-minute TTL bounds staleness to a
# window that's a non-issue for data that's reloaded manually and rarely.
# Known limitation, accepted rather than engineered around: this dict is
# per-process, so under a multi-worker deployment (e.g. gunicorn -w 4) each
# worker keeps its own cache and the effective hit rate drops accordingly —
# still correct, just less effective than a shared cache would be.
_SNAPSHOT_CACHE = {}
_SNAPSHOT_CACHE_TTL_SECONDS = 600


def _snapshot_response(view_arg, county_code, county_slug_val, api_county_slug=None):
    """Shared body for county_snapshot() (county-anchored,
    '/<county_slug>/snapshot') and snapshot_landing() (neutral, bare
    '/snapshot') -- PX-20260828-01, same refactor pattern as
    _home_search_response()/_info_response()/_rates_response(). county_
    profile/county_slug/api_county_slug are passed explicitly so
    snapshot_landing() can render the FILTERED county's data without ever
    setting g.county_slug/g.county_code (see snapshot_landing()'s own
    docstring for why).

    PX-20260828-15 Task 1: api_county_slug is a separate parameter from
    county_slug_val (defaults to it, so county_snapshot()'s anchored call
    is unaffected) -- see _info_response()'s identical PX-20260828-15
    comment for why the two values need to diverge for the neutral
    landing route specifically.

    Supports ?view=overall|residential|multifamily|retail|industrial|office|
    hotel|land|agricultural|other (the 10 tabs), plus the legacy
    ?view=commercial (default: overall) -- see _SNAPSHOT_VALID_VIEWS.
    """
    view = view_arg
    if view not in _SNAPSHOT_VALID_VIEWS:
        view = "overall"
    # Homeowner mode only sees residential home values.
    if _resolve_mode() == "homeowner":
        view = "residential"

    # _compute_snapshot_data(view) is purely a function of `view` (and
    # current DB state) — mode only changes which template text renders,
    # not the query results — so it's safe to cache by view alone, shared
    # across homeowner/investor mode.
    # DALLAS-GATE-1 Part 2: cache key now (county_code, view), not view alone
    # -- this closes the "not yet county-scoped" gap _compute_snapshot_data()
    # itself flagged (see its docstring/comment, PARTITION-2-IMPLEMENT Part
    # 3). Keying by view alone would have let Travis and Dallas requests for
    # the same view collide on one cache entry once both have real data.
    cache_key = (county_code, view)
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _SNAPSHOT_CACHE_TTL_SECONDS:
        payload = cached["payload"]
    else:
        payload = _compute_snapshot_data(view, county_code)
        _SNAPSHOT_CACHE[cache_key] = {"payload": payload, "ts": time.time()}

    profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])
    return render_template(
        "snapshot.html",
        view=view,
        county_profile=profile,
        county_slug=county_slug_val,
        api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,
        **payload,
    )


@app.route("/<county_slug>/snapshot")
@limiter.limit(_LIMIT_HEAVY)
def county_snapshot():
    """County Market Snapshot — 2026 preliminary vs 2025 certified.

    PX-20260828-01: Diego's 2026-08-28 ruling supersedes any assumption
    this page is Travis-only -- its own core query has been county_code-
    scoped since DALLAS-GATE-1/2. snapshot_landing() (bare '/snapshot') is
    now the real, nav-linked page, with its own in-page county filter;
    this county-anchored route is KEPT, unchanged, as a real direct deep
    link, the same relationship home()/search_landing()/info_landing()/
    tax_rates() now have to their own bare '_landing' twins."""
    view = request.args.get("view", "overall")
    return _snapshot_response(view, g.county_code, g.county_slug)


@app.route("/snapshot")
@limiter.limit(_LIMIT_HEAVY)
def snapshot_landing():
    """PX-20260828-01: the real, neutral bare '/snapshot' page -- Diego's
    2026-08-28 ruling. Carries the filtered county via ?county=<slug>
    (e.g. '/snapshot?county=dallas-tx'), same mechanism and fallback
    tolerance as info_landing()/rates_landing() (absent/unrecognized value
    -> Travis, not a 404). ?view= keeps working identically alongside
    ?county= -- e.g. '/snapshot?county=dallas-tx&view=residential'.
    Deliberately does NOT set g.county_slug/g.county_code, for the same
    is_county_anchored reason documented on info_landing().

    PX-20260828-15 Task 1: api_county_slug is resolved separately from
    the displayed county_slug via _resolve_landing_county_slug()'s
    `explicit` flag -- see info_landing()'s identical PX-20260828-15
    comment for the full reasoning."""
    county_code, slug, explicit = _resolve_landing_county_slug()
    view = request.args.get("view", "overall")
    return _snapshot_response(view, county_code, slug, api_county_slug=(slug if explicit else ""))


def _county_has_neighborhood_data(county_code):
    """PX-20260901-03 Task 1: whether this county's `parcel` rows carry any
    real neighborhood_cd at all -- a coverage FACT independent of any one
    Market Snapshot view, derived from data, never a hardcoded county list.
    Dallas's DCAD neighborhood_cd field was never mapped during onboarding
    (a separate, not-yet-scheduled loader brief) -- Dallas correctly has
    zero snapshot_neighborhood_movers rows for every view, forever, until
    that loader work happens, and that absence must never be mistaken for
    a freshness failure (_snapshot_summary_freshness() below).

    Cached on flask.g for the lifetime of one request: this fact is asked
    for twice per snapshot request (once by the freshness gate, once by
    snapshot_coverage_copy()'s missing-coverage line) and the underlying
    query is identical every time within a request -- same g-based
    per-request caching convention as this file's other
    getattr(g, ..., None) call sites."""
    cache = getattr(g, "_snapshot_neighborhood_data_cache", None)
    if cache is None:
        cache = {}
        g._snapshot_neighborhood_data_cache = cache
    if county_code not in cache:
        row = query(
            "SELECT EXISTS (SELECT 1 FROM parcel WHERE county_code = %s "
            "AND neighborhood_cd IS NOT NULL AND neighborhood_cd <> '') AS has_data",
            (county_code,), one=True,
        )
        cache[county_code] = bool(row["has_data"]) if row else False
    return cache[county_code]


def _county_has_ajr_data(county_code):
    """PX-20260901-04 Task 4: whether this county has any AJR-sourced rows
    at all (AJR is Travis's 2021-2024 personal-property-supplement export,
    loaded by loaders/load_ajr.py and identified everywhere else in this
    file by the same geo_id LIKE 'AJR%%' pattern -- see the AJR* exclusion
    clauses throughout api_peer_benchmark_local/api_peer_set/etc). Dallas's
    DCAD source has no AJR-equivalent supplement file, so it never has any
    AJR rows -- a real, data-derived fact, not a hardcoded "Travis only"
    literal, so this stays correct if a future county DOES get an AJR-style
    supplement loaded.

    Gates the Market Snapshot page's AJR-specific copy (the "Partial = AJR
    data" legend and "AJR* personal property supplements excluded" asides)
    so a county that has never had any AJR data doesn't carry a footnote
    describing a data quirk it's never had. Same per-request g-cache
    convention as _county_has_neighborhood_data() above."""
    cache = getattr(g, "_snapshot_ajr_data_cache", None)
    if cache is None:
        cache = {}
        g._snapshot_ajr_data_cache = cache
    if county_code not in cache:
        row = query(
            "SELECT EXISTS (SELECT 1 FROM parcel WHERE county_code = %s "
            "AND geo_id LIKE 'AJR%%') AS has_data",
            (county_code,), one=True,
        )
        cache[county_code] = bool(row["has_data"]) if row else False
    return cache[county_code]


def _county_has_year_built_data(county_code):
    """PX-20260901-04 Task 3: whether this county's `parcel` rows carry any
    real year_built at all -- a real, data-derived coverage fact, same
    pattern as _county_has_neighborhood_data()/_county_has_ajr_data() above.

    Dallas shows "NEW CONSTRUCTION 0" on the Snapshot page, which reads as
    "zero new construction this year" but is actually "year_built is
    unmapped for this county." DCAD's real source almost certainly carries
    a year-built-equivalent field on its RES_DETAIL/COM_DETAIL building-
    component-detail tables -- loaders/dcad_format.py's own
    TABLE_LOAD_POLICY marks BOTH of those tables "DELIBERATELY UNLOADED...
    no existing Travis-side schema column at this granularity," with no
    iter_*_records function ever written for either one, so no column-level
    confirmation of a year-built field has ever been done. This makes
    year_built a THIRD unmapped DCAD field alongside exemption_codes and
    neighborhood_cd (same PX-20260826-03/PX-20260901-02 precedent) --
    joining the Dallas loader brief, not a bug in this page's query.

    Confirming SQL (read-only, run against production -- this sandbox has
    no live DB access, see this brief's own report for the disclosure):
        SELECT COUNT(*) FILTER (WHERE year_built IS NOT NULL)
        FROM parcel WHERE county_code='DALLAS';
    A 0 (or near-0) result confirms unmapped, matching every other
    DCAD-unmapped field's signature; any large non-zero count would mean
    this helper's EXISTS check already routes correctly around a real,
    populated field and this docstring's DCAD-source theory can be
    disregarded in favor of "genuinely 0 new construction" instead.

    Same per-request g-cache convention as the other _county_has_*_data()
    helpers."""
    cache = getattr(g, "_snapshot_year_built_data_cache", None)
    if cache is None:
        cache = {}
        g._snapshot_year_built_data_cache = cache
    if county_code not in cache:
        row = query(
            "SELECT EXISTS (SELECT 1 FROM parcel WHERE county_code = %s "
            "AND year_built IS NOT NULL) AS has_data",
            (county_code,), one=True,
        )
        cache[county_code] = bool(row["has_data"]) if row else False
    return cache[county_code]


def _snapshot_summary_freshness(county_code="TRAVIS"):
    """
    Tier 1 "no live fallback, ever" gate for /snapshot (SPEC_AGGREGATE_
    PRECOMPUTATION.md's own explicit principle). Returns (is_fresh: bool,
    reason: str|None) -- reason is a short, honest, user-facing sentence
    explaining WHY the data isn't available when is_fresh is False, None
    when it is.

    Same tables-agree-with-each-other + agree-with-the-latest-load_batch
    logic as loaders/refresh_snapshot_summary.assert_snapshot_summary_fresh()
    -- PX-20260901-03 Task 1: the neighborhood-movers table is REQUIRED in
    that table set only for a county that actually has neighborhood data
    (see _county_has_neighborhood_data() below); breakdown/totals remain
    required for every county
    -- reimplemented here directly against this file's own query() helper
    rather than cross-imported from loaders/, to keep app.py and loaders/
    mutually independent (matching this codebase's existing module-boundary
    discipline -- see snapshot_taxonomy.py's own docstring for why loaders/
    scripts never import app.py; this is the same discipline in the other
    direction, and the check itself is small enough that duplicating it
    once here is cheaper and safer than adding a novel app.py -> loaders/
    dependency edge for a single function).

    county_code (PARTITION-2-IMPLEMENT, Part 3, per SPEC_COUNTY_
    PARTITIONING.md finding 9.7): every query below is now scoped to one
    real county's rows, not the table as a whole. Real, concrete bug this
    fixes -- once snapshot_breakdown/snapshot_totals/
    snapshot_neighborhood_movers hold multiple counties' rows (after
    migrate_county_partitioning.py's migration runs), refreshing Travis
    ALONE via the county-scoped reload procedure (loaders/
    reload_county_scope.py, §9.2(c)) would leave the table's batch_id
    values for Dallas's rows untouched -- an UNSCOPED version of this
    check (the old table-wide SELECT DISTINCT source_import_batch_id FROM
    <tbl>, no WHERE at all) would then either report the WHOLE table
    stale forever (multiple distinct batch_ids present, tripping the
    ">1 batch_id" branch below on every refresh from now on, since
    Travis's and Dallas's batch_ids will almost never coincide) or, worse,
    silently treat Dallas's still-stale rows as "fresh" by construction of
    whichever single value happened to look right -- neither is correct;
    the honest answer is "is THIS county's data fresh," which is what the
    WHERE county_code = %s scoping below actually answers.

    Defaults to 'TRAVIS' -- the same single, explicitly-marked hardcoded
    seam SPEC_COUNTY_PARTITIONING.md's finding 9.5 (the resolve_parcel()
    resolver design) uses, until Diego's separate application-level
    routing/UI decision (§7, still undecided, still not this function's
    job) gives every call site a real county_code to pass instead of this
    default.

    REAL DEPLOYMENT-SEQUENCING WARNING, stated plainly (see this task's
    final report): this diff references a county_code column that does
    NOT YET EXIST on snapshot_breakdown/snapshot_totals/
    snapshot_neighborhood_movers on production today --
    migrate_county_partitioning.py has not been run there. Deploying this
    version of app.py BEFORE that migration actually executes against
    production would break /snapshot outright (every query below would
    fail with "column county_code does not exist"). This code is built
    now per the brief's own explicit instruction ("this logic change
    matters immediately once county_code exists as a real column... don't
    defer this part"), but it must not reach production until AFTER
    migrate_county_partitioning.py's migration of these three tables has
    actually run there.
    """
    # PX-20260830-04 Task 1: unavailable_copy() takes a resolved county_name,
    # not the raw county_code -- see its own docstring for why.
    profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])
    # PX-20260901-03 Task 1: snapshot_neighborhood_movers is only a REQUIRED
    # table for a county that actually has neighborhood data to begin with
    # -- derived from data (_county_has_neighborhood_data()), never a
    # hardcoded county list. Dallas has zero rows in this table for every
    # view (DCAD's neighborhood_cd field was never mapped -- see that
    # function's docstring) and that is CORRECT, complete-on-its-own-terms
    # coverage, not a freshness failure: the old unconditional 3-table
    # check would have permanently reported Dallas's Market Snapshot as
    # "being prepared" even after breakdown/totals committed successfully,
    # which is exactly the live bug this task fixes. A county that DOES
    # have neighborhood data (Travis) keeps the original 3-table
    # requirement unchanged -- if ITS movers rows are missing or
    # batch-mismatched, that is still a real freshness gap.
    tables = ["snapshot_breakdown", "snapshot_totals"]
    if _county_has_neighborhood_data(county_code):
        tables.append("snapshot_neighborhood_movers")

    batch_ids_by_table = {}
    for tbl in tables:
        rows = query(
            f"SELECT DISTINCT source_import_batch_id FROM {tbl} WHERE county_code = %s",
            (county_code,),
        )
        batch_ids_by_table[tbl] = {r["source_import_batch_id"] for r in rows}

    latest_row = query("SELECT MAX(batch_id) AS latest FROM load_batch", one=True)
    latest_batch_id = latest_row["latest"] if latest_row else None

    # PX-20260830-04 Task 1: all five reasons below used to be raw f-strings
    # naming the internal county_code (e.g. literal "DALLAS"), real table
    # names, and loaders/refresh_snapshot_summary.py directly -- exactly the
    # developer-facing language Diego's standing ruling prohibits on a
    # visitor-facing page. All five real failure shapes here (missing rows,
    # >1 batch_id, no load recorded, tables out of sync, stale vs latest
    # batch) are the SAME user-visible fact from a visitor's point of view:
    # "this county's data is loaded, but the Market Snapshot summary view
    # for it isn't ready yet" -- so all five now share one honest sentence
    # via unavailable_copy() rather than five subtly different developer
    # diagnostics. The distinct internal detail these five branches encode
    # remains useful for US to know while debugging, so it stays in each
    # return's own surrounding code comment/log context, just not in the
    # string a visitor sees.
    for tbl in tables:
        if not batch_ids_by_table[tbl]:
            return False, unavailable_copy(
                "being_prepared", profile["county_name"], page_label="Market Snapshot", view_label="the summary view"
            )
        if len(batch_ids_by_table[tbl]) > 1:
            return False, unavailable_copy(
                "being_prepared", profile["county_name"], page_label="Market Snapshot", view_label="the summary view"
            )

    if latest_batch_id is None:
        return False, unavailable_copy(
            "being_prepared", profile["county_name"], page_label="Market Snapshot", view_label="the summary view"
        )

    table_batch_ids = {tbl: next(iter(batch_ids_by_table[tbl])) for tbl in tables}
    if len(set(table_batch_ids.values())) > 1:
        return False, unavailable_copy(
            "being_prepared", profile["county_name"], page_label="Market Snapshot", view_label="the summary view"
        )

    common_batch_id = next(iter(set(table_batch_ids.values())))
    if common_batch_id != latest_batch_id:
        return False, unavailable_copy(
            "being_prepared", profile["county_name"], page_label="Market Snapshot", view_label="the summary view"
        )

    return True, None


def _compute_snapshot_data(view, county_code):
    """
    Reads the Market Snapshot data for one sector view from the Tier 1
    summary tables (snapshot_breakdown / snapshot_totals /
    snapshot_neighborhood_movers -- see schema.sql's own comments, and
    loaders/refresh_snapshot_summary.py) -- Task AGGPRECOMP-2, Aug 2026 --
    instead of running the live aggregation queries this function used to
    run on every request.

    THIS IS THE ACTUAL FIX for /snapshot's real, live 500 errors -- not a
    query optimization. The prior four rounds of performance work (see git
    history: GROUPING SETS merge, the flattened-CTE no-op, and finally
    query_no_nestloop()'s SET LOCAL enable_nestloop=off override) reduced
    but never eliminated the live cost, because the queries were still
    running once per request against the full parcel/parcel_tax_year join.
    This migration removes that live aggregation from the request path
    entirely: the breakdown/totals/neighborhoods numbers are now precomputed
    ONCE per data load by loaders/refresh_snapshot_summary.py, and this
    function does nothing but SELECT a handful of already-small rows out of
    three summary tables. query_no_nestloop() itself (and its 4 real call
    sites, all of which lived in this function) has been REMOVED from
    app.py as dead code -- it has zero remaining callers now that the
    queries it wrapped no longer run live. See that function's former
    docstring (git history) for the full on/off measurement evidence that
    justified it while it was still needed.

    NO LIVE FALLBACK, EVER -- per SPEC_AGGREGATE_PRECOMPUTATION.md's own
    explicit Tier 1 principle. If the summary tables are missing, empty, or
    stale (see _snapshot_summary_freshness() below), this function returns
    data_unavailable=True with a real, honest reason string. It does NOT
    silently recompute live -- that would just resurrect the exact timeout
    class this migration exists to retire. templates/snapshot.html renders
    a loud, visible "data temporarily unavailable" state for this case (see
    that template's data_unavailable block) instead of a half-populated or
    broken page.

    bench_trends (the Annual Trends chart) is NOT part of this migration --
    it already reads a separate, small, pre-existing table (county_benchmark,
    populated independently by compute_metrics.py), was never one of the
    slow query_no_nestloop() call sites, and is explicitly out of this
    task's scope (AGGPRECOMP-2 brief). Stays a plain, unchanged query()
    against county_benchmark, gated on the same bench_labels mapping as
    before -- now sourced from snapshot_taxonomy.ptype_and_sort_case_for_view()
    rather than a local if/elif, so this is still exactly one place that
    mapping is defined, not two.

    subtype capping (_cap_subtype_rows(), SNAPSHOT_SUBTYPE_CAP) and the
    top-5/bottom-5 neighborhood-mover slicing both stay READ-TIME operations
    here, unchanged in behavior -- see schema.sql's snapshot_breakdown /
    snapshot_neighborhood_movers comments for why: both operate over
    already-small, already-precomputed rows (cheap Python sort/slice), not
    a live DB aggregate, so neither belongs in the refresh script per the
    spec's own "aggregation logic lives only inside refresh functions"
    principle -- capping/slicing isn't aggregation, it's display shaping.
    """
    # DALLAS-GATE-1 Part 2: closes the gap PARTITION-2-IMPLEMENT Part 3 left
    # explicitly open and disclosed (see git history for this docstring's
    # prior wording) -- county_code is now a real caller-supplied parameter
    # (from g.county_code via county_snapshot(), set per-request by
    # _pull_county_slug), not a hardcoded "TRAVIS" literal. Every read query
    # below (snapshot_breakdown/snapshot_totals/snapshot_neighborhood_movers/
    # county_benchmark) and the freshness check are now scoped to it.
    is_fresh, unavailable_reason = _snapshot_summary_freshness(county_code=county_code)
    if not is_fresh:
        return {
            "data_unavailable": True,
            "data_unavailable_reason": unavailable_reason,
            "rows": [],
            "totals": None,
            "bench_trends": [],
            "bench_label": None,
            "new_construction_count": 0,
            "risk_flagged_count": 0,
            "subtype_cap": SNAPSHOT_SUBTYPE_CAP,
            "top_neighborhoods": [],
            "bottom_neighborhoods": [],
            "status_2026": "none",
            # PX-20260901-03 Task 2: safe defaults -- the template's tab bar
            # and coverage line both live inside the same {% else %} branch
            # as the rest of this page's real content, so these are never
            # actually read on the data_unavailable path, but declared here
            # anyway to match this dict's existing every-key-explicit style.
            "available_tabs": [],
            "tab_button_labels": _SNAPSHOT_TAB_BUTTON_LABEL,
            "overall_tab_description": "All taxable real property",
            "coverage_line": None,
            "has_ajr_data": False,
            "has_year_built_data": False,
        }

    # PX-20260901-03 Task 2: composition facts -- which views this county's
    # snapshot_totals rows actually cover, and whether it has neighborhood
    # data at all. Independent of the specific `view` being rendered (a
    # nav-bar/coverage-line concern, not a per-view one); real, generated
    # facts from the already-precomputed summary tables, never a hardcoded
    # per-county list -- reading a small DISTINCT off an already-small
    # summary table, not a live aggregation, so this stays within Tier 1's
    # "no live fallback" principle (see this function's own docstring).
    available_view_rows = query(
        "SELECT DISTINCT view FROM snapshot_totals WHERE county_code = %s",
        (county_code,),
    )
    available_views = {r["view"] for r in available_view_rows}
    available_tabs = [v for v in _SNAPSHOT_VIEW_TAB_ORDER if v in available_views]
    # PX-20260901-04 Task 4: the Overall tab's own sub-copy line ("All
    # taxable real property -- residential, multi-family, retail, ...")
    # used to hand-list every sector regardless of whether this county's
    # `available_tabs` actually includes it -- generated here instead, from
    # the SAME available_tabs list the tab bar itself renders from, so a
    # county missing sub-sector breakdowns (Dallas today) never claims to
    # cover sectors it has no tab for.
    _overall_desc_views = [v for v in available_tabs if v != "overall"]
    overall_tab_description = (
        "All taxable real property — " + ", ".join(_SNAPSHOT_COVERAGE_LABELS[v] for v in _overall_desc_views)
        if _overall_desc_views else "All taxable real property"
    )
    has_neighborhood_movers = _county_has_neighborhood_data(county_code)
    has_ajr_data = _county_has_ajr_data(county_code)
    has_year_built_data = _county_has_year_built_data(county_code)
    coverage_line = snapshot_coverage_copy(
        COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])["county_name"],
        available_views, has_neighborhood_movers,
    )

    # bench_labels/fallback_label reused verbatim from the same shared
    # helper the refresh script itself used to build these views' SQL --
    # guarantees this read-time logic can never silently drift from what
    # was actually computed at write time (see that function's docstring).
    _, _, bench_labels, _order_by_expr, fallback_label = ptype_and_sort_case_for_view(view)
    sector_or_commercial = view in _SNAPSHOT_SECTOR_VIEWS or view == "commercial"
    order_sql = "ORDER BY n_parcels DESC NULLS LAST" if sector_or_commercial else "ORDER BY sort_key::int NULLS LAST"

    rows = [dict(r) for r in query(f"""
        SELECT ptype, sort_key, n_parcels, n_up, n_down, n_flat,
               median_pct, p25_pct, p75_pct, total_mv25_b, total_mv26_b
        FROM snapshot_breakdown
        WHERE view = %s AND county_code = %s
        {order_sql}
    """, (view, county_code))]

    # Part 2 fix (unchanged behavior): cap the within-tab subtype breakdown
    # to the top SNAPSHOT_SUBTYPE_CAP rows by parcel count for sector-scoped
    # views (the 8 tabs, plus the legacy "commercial" view). Not applied to
    # "overall" (fallback_label is None there -- never more than 9 rows to
    # begin with).
    if sector_or_commercial:
        rows = _cap_subtype_rows(rows, fallback_label)

    totals_row = query("""
        SELECT n_total, n_up, n_down, n_flat, median_pct, total_mv25_b, total_mv26_b,
               new_construction_count, risk_flagged_count, n_preliminary_2026, n_total_2026
        FROM snapshot_totals
        WHERE view = %s AND county_code = %s
    """, (view, county_code), one=True)

    totals = None
    new_construction_count = 0
    risk_flagged_count = 0
    # M4-2026-PRELIM-SNAPSHOT Part 1 (unchanged derivation, now read-time
    # over the precomputed n_preliminary_2026/n_total_2026 counts instead of
    # a live cert_agg query): "certified"/"preliminary"/"mixed"/"none".
    status_2026 = "none"
    if totals_row:
        totals = {
            "n_total":      totals_row["n_total"],
            "n_up":         totals_row["n_up"],
            "n_down":       totals_row["n_down"],
            "n_flat":       totals_row["n_flat"],
            "total_mv25_b": totals_row["total_mv25_b"],
            "total_mv26_b": totals_row["total_mv26_b"],
            "median_pct":   totals_row["median_pct"],
        }
        new_construction_count = int(totals_row["new_construction_count"] or 0)
        risk_flagged_count = int(totals_row["risk_flagged_count"] or 0)
        n_prelim = int(totals_row["n_preliminary_2026"] or 0)
        n_total_2026 = int(totals_row["n_total_2026"] or 0)
        if n_total_2026:
            if n_prelim == 0:
                status_2026 = "certified"
            elif n_prelim == n_total_2026:
                status_2026 = "preliminary"
            else:
                status_2026 = "mixed"

    # ── County Benchmark Annual Trends for the selected view (unchanged --
    # out of Tier 1 scope, see docstring above) ──────────────────────────
    # PX-20260901-04 Task 1: bench_labels is now always 0 or 1 entries (see
    # ptype_and_sort_case_for_view()'s "overall" branch -- narrowed to just
    # ["Residential"] this brief). bench_label (singular) is the one label
    # this panel actually shows, passed through to the template so it can
    # label the panel header truthfully ("Residential"/"Commercial"/etc)
    # instead of the old blanket "County Median" that implied a county-wide
    # row which doesn't exist in county_benchmark.
    bench_trends = []
    bench_label = bench_labels[0] if bench_labels else None
    if bench_labels:
        fmt_labels = ", ".join(f"'{lb}'" for lb in bench_labels)
        bench_trends = query(f"""
            SELECT
                tax_year,
                property_type_label,
                parcel_count,
                median_market_value,
                p25_market_value,
                p75_market_value,
                median_assessment_ratio,
                median_yoy_value_change_pct
            FROM county_benchmark
            WHERE property_type_label IN ({fmt_labels}) AND county_code = %s
            ORDER BY tax_year, property_type_label
        """, (county_code,))

    # Top/bottom moving neighborhoods (unchanged behavior): every
    # neighborhood clearing HAVING COUNT(*) >= 10 was already filtered AT
    # REFRESH TIME (see schema.sql's snapshot_neighborhood_movers comment)
    # -- this is a cheap read-time sort/slice over what's typically a few
    # dozen rows per view, not a new aggregate.
    top_neighborhoods = []
    bottom_neighborhoods = []
    if totals:
        nb_rows = query("""
            SELECT neighborhood_cd, n_parcels, median_pct
            FROM snapshot_neighborhood_movers
            WHERE view = %s AND county_code = %s
            ORDER BY median_pct DESC
        """, (view, county_code))
        if nb_rows:
            nb_rows = [dict(r) for r in nb_rows]
            top_neighborhoods = nb_rows[:5]
            bottom_neighborhoods = nb_rows[-5:][::-1]

    return {
        "data_unavailable": False,
        "rows": rows,
        "totals": totals,
        "bench_trends": bench_trends,
        "bench_label": bench_label,
        "new_construction_count": new_construction_count,
        "risk_flagged_count": risk_flagged_count,
        "subtype_cap": SNAPSHOT_SUBTYPE_CAP,
        "top_neighborhoods": top_neighborhoods,
        "bottom_neighborhoods": bottom_neighborhoods,
        "status_2026": status_2026,
        # PX-20260901-03 Task 2: composition-from-availability facts, computed
        # above the freshness-gate branch point in this function's body.
        "available_tabs": available_tabs,
        "tab_button_labels": _SNAPSHOT_TAB_BUTTON_LABEL,
        "overall_tab_description": overall_tab_description,
        "coverage_line": coverage_line,
        # PX-20260901-04 Task 4: real, data-derived fact gating the AJR-
        # specific copy (see _county_has_ajr_data() docstring above) --
        # never a hardcoded "county == TRAVIS" check.
        "has_ajr_data": has_ajr_data,
        # PX-20260901-04 Task 3: real, data-derived fact gating the New
        # Construction KPI card (see _county_has_year_built_data() above).
        "has_year_built_data": has_year_built_data,
    }


@app.route("/<county_slug>/snapshot/neighborhood/<code>")
@limiter.limit(_LIMIT_HEAVY)
def snapshot_neighborhood(code):
    """
    Neighborhood drill-down for Market Snapshot's Top/Bottom Moving
    Neighborhoods table — parcel-level detail for one neighborhood_cd, both
    years' values side by side. Replaces the earlier /search?neighborhood=
    linking approach (that URL-param handling is still present and dormant
    on the Search page — not reverted, just no longer linked from here; it
    could be a useful entry point for something else later).

    ?view=<sector> (optional, same values as /snapshot) scopes results to
    that sector's property type — mirrors how /snapshot's own links pass
    prop_type today. Defaults to "overall" (no property-type restriction).

    Reuses, rather than re-derives:
      - CANONICAL_PARCEL_EXCL and _snapshot_view_where() (module-level,
        above _compute_snapshot_data()) for the exact same
        parcel-eligibility filter and view->property-type scoping that
        function's breakdown/neighborhoods queries use.
      - SEARCH_FILTER_PAGE_SIZE (50) and the same total/total_pages math
        already used by /api/search_filter, so "page size" means the same
        thing everywhere on the site rather than a page-specific number.

    Uses plain query(), NOT query_no_nestloop() — deliberately, despite the
    superficial resemblance to the breakdown/Part 4/neighborhoods queries
    that used to need it, back when they still ran live (now migrated to
    reading precomputed summary tables, see _compute_snapshot_data()).
    Measured via
    task_staging/neighborhood_drilldown/check_neighborhood_drilldown_perf.command:
    for this query, Nested Loop is 15-100x FASTER than forcing it off
    (3-5ms vs 79-367ms), the opposite of those other three. The difference
    is selectivity: this query filters to one neighborhood_cd via an index
    first, narrowing to ~79 rows before the two-year join, where an
    indexed point-lookup Nested Loop is the correct, fast plan — unlike
    the whole-county queries that fix targeted, where the planner's own
    Nested Loop choice was the actual problem. (query_no_nestloop() itself
    was later retired entirely, Task AGGPRECOMP-2 -- its whole-county
    callers moved to reading precomputed summary tables instead of running
    live; this route was never one of those callers, so it's unaffected by
    that migration and still runs its own live, indexed point-lookup query
    exactly as described above.)
    """
    view = request.args.get("view", "overall")
    if view not in _SNAPSHOT_VALID_VIEWS:
        view = "overall"

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    view_where = _snapshot_view_where(view)
    # Part 4 fix (this round): the new 8 tabs' sector label now comes from
    # _SNAPSHOT_SECTOR_VIEWS (Retail/Industrial/Office/Hotel/etc., not the
    # canonical 5-category set); "commercial" still falls back to the legacy
    # _SNAPSHOT_VIEW_PROP_TYPE_LABEL dict, unchanged. None for "overall".
    view_prop_type = _SNAPSHOT_SECTOR_VIEWS.get(view) or _SNAPSHOT_VIEW_PROP_TYPE_LABEL.get(view)

    # Part 4 fix (this round): per-parcel prop_type shown in this drill-down
    # table now comes from the same _snapshot_taxonomy_sql() the breakdown
    # table above uses (Overall's own branch, since this route's "overall"
    # spans every sector the same way) -- previously used classify.py's
    # canonical label_case_sql(), which would have shown a DIFFERENT label
    # than the sector tab the user actually clicked through from (e.g. a
    # parcel landing in the new "Retail" tab could have shown "Commercial"
    # here under the old canonical labeling) -- exactly the kind of
    # cross-page inconsistency this whole round is about eliminating.
    # _snapshot_taxonomy_sql() always resolves to one of the 9 real labels
    # (its own ELSE is 'Other', never NULL), so no COALESCE needed here.
    ptype_case = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")

    offset = (page - 1) * SEARCH_FILTER_PAGE_SIZE

    rows = query(f"""
        SELECT
            p.geo_id,
            p.situs_address,
            t25.market_value AS mv25,
            t26.market_value AS mv26,
            t26.data_source  AS data_source_2026,
            ({ptype_case}) AS prop_type,
            (t26.market_value - t25.market_value)::FLOAT / t25.market_value * 100 AS pct_chg,
            COUNT(*) OVER() AS total_count
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
                                 AND t25.county_code = p.county_code
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
                                 AND t26.county_code = p.county_code
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          AND p.neighborhood_cd = %(code)s
          AND p.county_code = %(county_code)s
          {CANONICAL_PARCEL_EXCL}
          {view_where}
        ORDER BY pct_chg DESC
        LIMIT {SEARCH_FILTER_PAGE_SIZE} OFFSET %(offset)s
    """, params={"code": code, "offset": offset, "county_code": g.county_code})

    total = int(rows[0]["total_count"]) if rows else 0
    total_pages = (total + SEARCH_FILTER_PAGE_SIZE - 1) // SEARCH_FILTER_PAGE_SIZE if total else 0
    parcels = [dict(r) for r in rows]
    for p in parcels:
        # Precomputed here (not left to the template) since Jinja can't see
        # the Python-side CERTIFIED_TIER_DATA_SOURCES set directly -- same
        # reasoning as property.html's is_2026_certified / compare.html's
        # is_2026_certified per-parcel field.
        p["is_2026_certified"] = p.get("data_source_2026") in CERTIFIED_TIER_DATA_SOURCES

    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: same technique as parcel_list()'s
    # status_2026 -- this page's header/legend used to hardcode "2026
    # Preliminary" regardless of data_source. Every row here has a 2026
    # value (INNER JOIN above), so no "none" case is needed.
    _n_certified_2026 = sum(1 for p in parcels if p["is_2026_certified"])
    if not parcels:
        status_2026 = "none"
    elif _n_certified_2026 == len(parcels):
        status_2026 = "certified"
    elif _n_certified_2026 == 0:
        status_2026 = "preliminary"
    else:
        status_2026 = "mixed"

    return render_template(
        "snapshot_neighborhood.html",
        code=code,
        view=view,
        view_prop_type=view_prop_type,
        page=page,
        total=total,
        total_pages=total_pages,
        parcels=parcels,
        status_2026=status_2026,
    )


@app.route("/<county_slug>/api/rates")
def api_rates():
    """JSON endpoint for rate data (for dynamic chart filtering).

    PX-20260828-01 investigation found this endpoint had NO county_code
    predicate at all -- despite being registered at a county-anchored
    path (g.county_code was set and simply never used), it returned
    EVERY county's rows unioned together. Confirmed dead code today (no
    template or JS calls it -- rates.html's own dynamic filtering reads
    the by_entity_json/entity_names_json already embedded server-side by
    tax_rates()/rates_landing(), not this endpoint), but it's a live,
    callable route regardless, and this is the same class of bug
    DALLAS-GATE-2 fixed on tax_rates()'s own main query -- fixed
    alongside this brief's other work rather than left live and unscoped."""
    rates = query("""
        SELECT entity_code, entity_name, tax_year, rate
        FROM   county_tax_rate
        WHERE  county_code = %(county_code)s
          AND  tax_year >= 2006
        ORDER  BY entity_code, tax_year
    """, {"county_code": g.county_code})
    return jsonify([dict(r) for r in rates])


@app.route("/<county_slug>/api/benchmark")
@limiter.limit(_LIMIT_HEAVY)
def api_benchmark():
    """
    Live benchmark query for the County Benchmark filter UI.

    Query params:
      year      int  (default 2025)   — certified year only (not 2026)
      prop_type str  (default "")     — broad type label from county_benchmark table
      classi_cd str  (default "")     — specific TCAD use code; triggers on-the-fly aggregation
    """
    year         = request.args.get("year", 2025, type=int)
    prop_type    = request.args.get("prop_type", "").strip()
    classi_cd    = request.args.get("classi_cd", "").strip()
    neighborhood = request.args.get("neighborhood", "").strip()

    # Guard: allow certified years 2021–2025 plus 2026 preliminary
    if year not in (2021, 2022, 2023, 2024, 2025, 2026):
        return jsonify({"ok": False, "error": "Year must be between 2021 and 2026"})

    # Allow AJR data (ajr_2021…ajr_2024) and certified (NULL) for historical years.
    # Exclude only 'preliminary' (2026 data loaded into all years if ever reloaded).
    # 2026 has no filter — preliminary data is intentionally included.
    ds_filter = "" if year == 2026 else "AND (t.data_source IS NULL OR t.data_source != 'preliminary')"
    nb_filter = "AND p.neighborhood_cd = %s" if neighborhood else ""
    # Exclude non-real-property accounts from all live benchmark queries.
    # Only X (exempt) and N (personal property, 3 parcels) excluded from state_cd1.
    # M (manufactured homes) and O (other real property) are kept — confirmed real property in Travis CAD.
    # AJR* geo_ids = personal property supplement accounts loaded from AJR (not real estate); excluded.
    # July 2026 fix: this used to be an independently-typed literal that
    # merely CLAIMED to mirror compute_metrics.py's BENCHMARK_EXCLUDE_PREFIXES
    # (comment-enforced, not structural) — now a direct reference to the same
    # canonical, NULL-safe constant every other exclusion consumer uses (see
    # parcel_filters.py). Cannot drift from the others again.
    excl_filter = CANONICAL_PARCEL_EXCL

    # DALLAS-GATE-2 Part 2: county_code (g.county_code, set per-request by
    # _pull_county_slug) threaded through every live on-the-fly aggregation
    # query below. NOTE for whoever re-runs verify_index_coverage.py against
    # this function later: its schema-sql-mode static audit flagged only the
    # prev_row query below (app.py's original :3916) as a dynamic WHERE
    # fragment -- the sibling `row` queries in both branches (classi_cd's
    # main query, and the 2026-live prop_type query further down) use the
    # exact same unscoped-geo_id/classi_cd shape but were NOT flagged, because
    # each SELECTs a `COUNT(*) FILTER (WHERE t.data_source = 'preliminary')`
    # expression -- the audit tool's WHERE-clause boundary search
    # (verify_index_coverage.py's `_clause_end`) matches on the FIRST `WHERE`
    # keyword in the statement, which is this inner FILTER's WHERE, not the
    # real outer WHERE further down; it then reports zero unresolved/filtered
    # columns for the outer clause instead of flagging it. Found only by
    # reading this function's real SQL directly, not by trusting the tool's
    # findings list -- fixed here anyway, since the underlying gap is
    # identical. Flagging this tool limitation for a follow-up fix to
    # verify_index_coverage.py itself (out of this task's scope).
    county_code = g.county_code

    if classi_cd and classi_cd != "all":
        # ── On-the-fly aggregation by classi_cd ──────────────────────────
        params_cc = [year, classi_cd, county_code]
        if neighborhood:
            params_cc.append(neighborhood)
        row = query(f"""
            SELECT
                COUNT(*)                                                               AS n_parcels,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value)           AS median_market_value,
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY t.market_value)          AS p25_market_value,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY t.market_value)          AS p75_market_value,
                COUNT(*) FILTER (WHERE t.data_source = 'preliminary')                 AS n_preliminary
            FROM parcel p
            JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = %s
                                   AND t.county_code = p.county_code
            WHERE p.classi_cd = %s
              AND p.county_code = %s
              AND t.market_value IS NOT NULL AND t.market_value > 0
              {ds_filter}
              {excl_filter}
              {nb_filter}
        """, params_cc, one=True)

        entry = USE_CODE_LOOKUP.get(classi_cd, (classi_cd, ""))
        filter_label = f"{entry[0]} (code {classi_cd})"

        # YoY vs prior year
        prev_year = year - 1
        yoy = None
        if prev_year >= 2021:
            prev_ds = "" if prev_year == 2026 else "AND (t.data_source IS NULL OR t.data_source != 'preliminary')"
            prev_params = [prev_year, classi_cd, county_code]
            if neighborhood:
                prev_params.append(neighborhood)
            prev_row = query(f"""
                SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value) AS prev_med
                FROM parcel p
                JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = %s
                                       AND t.county_code = p.county_code
                WHERE p.classi_cd = %s
                  AND p.county_code = %s
                  AND t.market_value IS NOT NULL AND t.market_value > 0
                  {prev_ds}
                  {excl_filter}
                  {nb_filter}
            """, prev_params, one=True)
            if prev_row and prev_row["prev_med"] and row and row["median_market_value"]:
                yoy = round((float(row["median_market_value"]) / float(prev_row["prev_med"]) - 1) * 100, 2)

        if row and row["n_parcels"] > 0:
            return jsonify({
                "ok": True,
                "n_parcels": int(row["n_parcels"]),
                "median_market_value": float(row["median_market_value"] or 0),
                "p25_market_value":    float(row["p25_market_value"]    or 0),
                "p75_market_value":    float(row["p75_market_value"]    or 0),
                "median_yoy_value_change_pct": yoy,
                "filter_label": filter_label,
                "year": year,
                # M4-2026-PRELIM-SNAPSHOT Part 1 fix: this used to hardcode
                # year == 2026 -- wrong as of today's 2026 certified load.
                # n_preliminary is a real count of THIS slice's rows still on
                # data_source='preliminary', not a year-based guess; > 0
                # means at least one parcel in this exact filter/year combo
                # hasn't been certified yet.
                "is_preliminary": bool(row.get("n_preliminary") or 0) > 0,
            })
        return jsonify({"ok": False, "error": "No data for this use code / year combination."})

    elif prop_type:
        if year == 2026:
            # ── 2026 live aggregation (preliminary — not in county_benchmark table) ──
            _label_map = {
                "Residential": ["A"], "Multi-Family": ["B"], "Land/Vacant": ["C"],
                "Agricultural": ["D", "E"], "Commercial": ["F", "L"],
            }
            prefixes = _label_map.get(prop_type, [])
            if not prefixes:
                return jsonify({"ok": False, "error": "Unknown property type."})
            like_parts = " OR ".join(f"p.state_cd1 LIKE '{px}%%'" for px in prefixes)
            params_2026 = [county_code]
            if neighborhood:
                params_2026.append(neighborhood)
            row = query(f"""
                SELECT
                    COUNT(*)                                                            AS n_parcels,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value)        AS median_market_value,
                    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY t.market_value)       AS p25_market_value,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY t.market_value)       AS p75_market_value,
                    COUNT(*) FILTER (WHERE t.data_source = 'preliminary')              AS n_preliminary
                FROM parcel p
                JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2026
                                       AND t.county_code = p.county_code
                WHERE ({like_parts})
                  AND p.county_code = %s
                  AND t.market_value IS NOT NULL AND t.market_value > 0
                  {excl_filter}
                  {nb_filter}
            """, params_2026, one=True)
            if row and row["n_parcels"] > 0:
                return jsonify({
                    "ok": True,
                    "n_parcels": int(row["n_parcels"]),
                    "median_market_value": float(row["median_market_value"] or 0),
                    "p25_market_value":    float(row["p25_market_value"]    or 0),
                    "p75_market_value":    float(row["p75_market_value"]    or 0),
                    "median_yoy_value_change_pct": None,
                    "filter_label": prop_type,
                    "year": 2026,
                    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: was hardcoded True
                    # unconditionally -- see the classi_cd branch above for
                    # the same fix and rationale.
                    "is_preliminary": bool(row.get("n_preliminary") or 0) > 0,
                })
            return jsonify({"ok": False, "error": "No 2026 data for this property type."})
        else:
            # ── Pre-aggregated county_benchmark table ─────────────────────────
            row = query("""
                SELECT * FROM county_benchmark
                WHERE property_type_label = %s AND tax_year = %s
            """, (prop_type, year), one=True)
            if row:
                return jsonify({
                    "ok": True,
                    "n_parcels": int(row["parcel_count"] or 0),
                    "median_market_value": float(row["median_market_value"] or 0),
                    "p25_market_value":    float(row["p25_market_value"]    or 0),
                    "p75_market_value":    float(row["p75_market_value"]    or 0),
                    "median_yoy_value_change_pct": (
                        float(row["median_yoy_value_change_pct"])
                        if row["median_yoy_value_change_pct"] is not None else None
                    ),
                    "filter_label": prop_type,
                    "year": year,
                    "is_preliminary": False,
                })
        return jsonify({"ok": False, "error": "No benchmark data for this property type / year."})

    return jsonify({"ok": False, "error": "Specify prop_type or classi_cd."})


@app.route("/<county_slug>/api/benchmark/meta")
def api_benchmark_meta():
    """Return available property types and use codes with ≥10 parcels (for filter dropdowns)."""
    # DALLAS-GATE-2 Part 1: this whole function was entirely county-implicit
    # -- none of its 5 queries (county_benchmark, parcel x2, and the two
    # neighborhood/total-count parcel counts below) filtered by county_code.
    # county_benchmark and parcel are both county_code-leading composite-PK
    # tables per migrate_county_partitioning.py's TABLE_SPECS/DEFAULT_ONLY_
    # TABLES. Explicitly named in DALLAS-GATE-2's brief ("api/benchmark*
    # (both api_benchmark and api_benchmark_meta)") but NOT among the 22
    # tool-flagged dynamic-clause gaps (these are plain, statically-visible
    # WHERE clauses the coverage tool should already catch on its next run --
    # this fix is Part 1 direct wiring, not a Part 2 dynamic-gap fix).
    # county_code added as an ADDITIONAL predicate to every query below;
    # no query shape/join/index-path changes.
    county_code = g.county_code
    prop_types_raw = query("""
        SELECT DISTINCT property_type_label
        FROM county_benchmark WHERE tax_year = 2025 AND county_code = %(county_code)s
        ORDER BY property_type_label
    """, {"county_code": county_code})
    prop_types = [r["property_type_label"] for r in prop_types_raw]

    # Was a hand-rolled, state_cd1-only CASE that duplicated (and diverged
    # from) tax_logic.classify.label_case_sql(): it didn't apply the
    # classi_cd-first Multi-Family/Commercial override (Task 1) and had the
    # same M/O gap as classify.py did before the "Other" bucket fix. Now
    # calls the single canonical classifier so this dropdown's grouping can
    # never disagree with Market Snapshot or county_benchmark again.
    _meta_label = label_case_sql("p.classi_cd", "p.state_cd1")
    use_codes_raw = query(f"""
        SELECT
            p.classi_cd,
            COALESCE({_meta_label}, 'Other') AS prop_type,
            COUNT(*) AS n
        FROM parcel p
        WHERE p.classi_cd IS NOT NULL AND p.classi_cd != '00'
          AND p.county_code = %(county_code)s
        GROUP BY p.classi_cd, prop_type
        HAVING COUNT(*) >= 10
        ORDER BY prop_type, n DESC
    """, {"county_code": county_code})

    by_type = {}
    for r in use_codes_raw:
        pt = r["prop_type"]
        if pt not in by_type:
            by_type[pt] = []
        desc = USE_CODE_LOOKUP.get(r["classi_cd"], (r["classi_cd"], ""))[0]
        by_type[pt].append({"code": r["classi_cd"], "desc": desc, "n": int(r["n"])})

    # Neighborhoods with ≥5 parcels (sorted by count desc).
    #
    # Fix (Neighborhood Link Silent Failure investigation): this used to end
    # with "LIMIT 500 (capped ... to avoid huge dropdown)". That cap silently
    # dropped any neighborhood ranked below the top 500 by raw parcel count
    # from this dropdown's option list entirely — including real, valid
    # Market Snapshot "moving neighborhood" codes (confirmed: H0D6C, Q23000,
    # R331C), since a neighborhood can clear the Moving Neighborhoods query's
    # HAVING COUNT(*) >= 10 (its qualifying-population threshold: parcels
    # present with a valid market_value in BOTH 2025 and 2026, non-excluded
    # state_cd1/geo_id) while still ranking outside the top 500 county-wide
    # by total raw parcel count (this query's unrelated, looser population:
    # every parcel in `parcel`, any year, any state_cd1, no join at all).
    # Every other difference between this query and the Moving Neighborhoods
    # query already makes THIS one's population a strict superset (no
    # tax-year join, no canonical X/N/AJR exclusion, and a lower >= 5 vs >= 10
    # threshold) — the LIMIT was the only mechanism that could still make
    # this list narrower than the moving-neighborhoods query for an
    # individual code, and did. Search's Neighborhood filter dropdown must
    # be a superset of anything Market Snapshot's neighborhood links can
    # point to, so the cap is removed rather than raised to some other
    # arbitrary number — a moving-neighborhood code must always be
    # selectable, not selectable-until-the-list-grows-again.
    nb_raw = query("""
        SELECT neighborhood_cd, COUNT(*) AS n
        FROM parcel
        WHERE neighborhood_cd IS NOT NULL AND neighborhood_cd != ''
          AND county_code = %(county_code)s
        GROUP BY neighborhood_cd
        HAVING COUNT(*) >= 5
        ORDER BY n DESC
    """, {"county_code": county_code})
    total_parcels = query(
        "SELECT COUNT(*) AS n FROM parcel WHERE county_code = %(county_code)s",
        {"county_code": county_code}, one=True
    )["n"]
    nb_non_null = query(
        "SELECT COUNT(*) AS n FROM parcel WHERE neighborhood_cd IS NOT NULL AND neighborhood_cd != '' "
        "AND county_code = %(county_code)s",
        {"county_code": county_code}, one=True
    )["n"]
    nb_coverage_pct = round(100.0 * nb_non_null / total_parcels, 1) if total_parcels else 0

    neighborhoods = [{"code": r["neighborhood_cd"], "n": int(r["n"])} for r in nb_raw]

    return jsonify({
        "prop_types": prop_types,
        "use_codes_by_type": by_type,
        "neighborhoods": neighborhoods,
        "neighborhood_coverage_pct": nb_coverage_pct,
    })


# ── Filtered parcel search (Search page filter system) ─────────────────────────
# County-agnostic by design: the "county" param is accepted and validated but
# only "travis" exists today (single-option dropdown in the UI, same
# structured-for-more-later pattern as the /info page's state/county
# selectors) — adding a second county later is new WHERE-clause branches here,
# not a rewrite of this route.
#
# Property-type taxonomy note: this reuses tax_logic.classify's canonical
# 5-category taxonomy (Residential / Multi-Family / Commercial / Land-Vacant /
# Agricultural) via label_case_sql() — the SAME taxonomy the nav sector
# dropdown, Market Snapshot, and /api/benchmark/meta already use. There is no
# 8-category (Commercial-Retail / Industrial / Hospitality-Other / Exempt)
# taxonomy anywhere in this codebase; see the brief report for detail.
SEARCH_FILTER_PAGE_SIZE = 50

# Homestead exemption_codes are a comma/semicolon-separated token string
# (schema.sql: "comma-separated codes (HS, OV65, DP, DV, etc.)"). Matching
# must be a word-boundary check on the HS token specifically — a plain
# substring/ILIKE '%HS%' would incorrectly match DVHS / DVHSS (Disabled
# Veteran Homestead — a real, different exemption that contains the letters
# "HS" but is not the general Homestead exemption).
_HS_TOKEN_RE = r'(^|[,;])\s*HS\s*($|[,;])'


def combine_confidence_tiers(inputs):
    """
    Weakest-link confidence combiner for any DERIVED figure (a quotient, a
    year-over-year change, a multi-year average, a $/SF ratio, ...): the
    result can never be more certain than its least-certain input.

    inputs: a list of (label, tier) pairs, where tier is one of
    "verified" / "preliminary" / "partial" / "estimated" / "not_available"
    (case-insensitive), or a bare bool (True -> verified, False -> partial
    -- convenient for passing an is_billing_verified-style flag directly),
    or None/""/missing (treated as not_available).

    Returns (tier, note): `tier` is the single weakest tier among the
    inputs (so a caller can badge-color consistently with the rest of the
    site's Verified/Preliminary/Partial/Estimated palette); `note` is a
    short human-readable explanation naming which input(s) fell short of
    Verified, or "Verified" / "Not Available" for the two clean-sweep
    cases.

    Promoted (July 2026, per Fable review P0-3 -- "confidence doesn't
    propagate through derived figures") from TWO independently hand-rolled
    instances of this exact idea that already existed in
    export_due_diligence_pdf(): the inline eff_rate_confidence computation
    (Effective Tax Rate = Total Tax ÷ Market Value, weakest of the two) and
    the inline combine_confidence() helper (Value & Tax History's single
    Confidence column, weakest of appraisal vs. billing). Both did the same
    "weakest tier wins, name what's holding it back" thing with slightly
    different code -- this is the one shared version. export_due_diligence_pdf()
    now calls this instead of its own two copies (verified byte-identical
    output for the same inputs -- see verify_confidence_helpers.py), and
    property_detail() now calls it too for the page-layer gap Fable found:
    the Effective Tax Rate KPI card was badging "Verified" whenever the
    narrower effective_tax_rate_derived flag (specifically: was this
    reconstructed by summing entity amounts) was False -- which says
    nothing about whether the underlying total_tax was itself a portal-
    scrape/otherwise-Partial figure, and says nothing at all about whether
    the market_value denominator was even certified. A quotient can't be
    more certain than either of its actual inputs.
    """
    TIER_RANK = {"verified": 3, "preliminary": 2, "partial": 1,
                 "estimated": 1, "not_available": 0}

    def _norm(tier):
        if tier is True:
            return "verified"
        if tier is False:
            return "partial"
        if not tier:
            return "not_available"
        return str(tier).lower()

    ranked = [(TIER_RANK.get(_norm(t), 0), label, _norm(t)) for label, t in inputs]
    if not ranked:
        return "not_available", "Not Available"

    ranked.sort(key=lambda x: x[0])
    weakest_tier = ranked[0][2]

    if weakest_tier == "not_available":
        return "not_available", "Not Available"
    if all(r[2] == "verified" for r in ranked):
        return "verified", "Verified"

    reason_word = {
        "preliminary": "preliminary",
        "partial": "not fully verified",
        "estimated": "estimated, not from a real record",
        "not_available": "not available",
    }
    reasons = [f"{label} is {reason_word.get(tier, 'not verified')}"
               for _, label, tier in ranked if tier != "verified"]
    return weakest_tier, "Partial — " + "; ".join(reasons)


# ── Confidence-tier data_source classification (July 2026, per Diego's
# Cowork brief -- "Fix AJR/Historical-Year Confidence Tiering") ─────────────
# The investigation that preceded this fix found _row_confidence() below was
# bucketing every 2021-2024 parcel_tax_year row to "Partial" purely because
# its data_source string wasn't the literal 'certified' -- regardless of
# whether that specific record showed any real data-quality issue. Per Tax
# Code Sec.26.01(b), TCAD's own chief appraiser submits this same certified
# roll data to the Comptroller (as EARS/AJR) in addition to certifying it
# locally -- so "not literally 'certified'" was never a real quality signal
# on its own. Confirmed live: 89.5% of 2021-2024 rows are cert_202x, 10.5%
# are ajr_202x; both trace back to the same TCAD-certified source. This set
# names every data_source string entitled to certified-tier treatment --
# the ACTUAL per-record signal is now the assessed>market anomaly check
# inside _row_confidence() itself, not the data_source string beyond "is
# this actually a certified export, from TCAD or its EARS submission, of
# any vintage."
CERTIFIED_TIER_DATA_SOURCES = frozenset({
    "certified",
    "cert_2021", "cert_2022", "cert_2023", "cert_2024", "cert_2026",
    "ajr_2021", "ajr_2022", "ajr_2023", "ajr_2024",
    # "dcad_certified" added Aug 2026 (URGENT live-correctness fix, filed
    # alongside PX-20260828-11): this set was built for Travis-shaped
    # data_source strings only and was never extended when Dallas came
    # online. Dallas's loader (loaders/load_dallas_certified.py, DATA_SOURCE
    # constant) writes the literal string "dcad_certified" for every row --
    # a genuine DCAD certified-roll export, entitled to the exact same
    # certified-tier treatment as TCAD's own "certified"/cert_202x/ajr_202x
    # strings above (same underlying legal status: a county chief
    # appraiser's certified roll under Tax Code Sec.26.01(b), just filed by
    # a different county's CAD). Confirmed live in production: Dallas is
    # loaded for all 5 years 2022-2026 (703,446 units for 2025 alone), so
    # this was misclassifying real, currently-live Dallas rows as "partial"
    # -- not a future/theoretical gap. See templates/property.html's
    # "Property-level confidence badge" block for the independent Jinja
    # mirror of this same set, which needed the identical fix.
    "dcad_certified",
})


def _row_confidence(data_source, assessed_value=None, market_value=None):
    """Confidence tier for a single parcel_tax_year row.

    Mirrors the property page's per-year confidence badge logic exactly
    (templates/property.html, "Property-level confidence badge" block,
    ~line 1427) -- same branching -- so a parcel shown here reads the same
    confidence it would show on its own detail page. Not a shared call site
    (that logic is inline Jinja on the property page, which can't call a
    Python function); if this changes, that block needs a matching update
    -- see its own comment for why it's a deliberate, not accidental,
    duplication.

    Tiering (July 2026 fix -- see CERTIFIED_TIER_DATA_SOURCES above for the
    full rationale):
      - data_source in CERTIFIED_TIER_DATA_SOURCES (TCAD's 2025 certified
        export, OR a certified/EARS-submitted historical export or AJR
        record for 2021-2024 -- all ultimately the same certifying chief
        appraiser's own data under Tax Code Sec.26.01(b)):
          - assessed_value <= market_value, or the comparison isn't
            possible (either side missing) -> "verified"
          - assessed_value > market_value for THIS record -> "partial"
            (the real, per-record anomaly check -- reuses the same
            assessed>market comparison the page's own "!" data-anomaly
            icon already uses; not a second, independently-invented check)
      - data_source == 'preliminary' (2026 preliminary roll) -> "preliminary"
        -- unaffected by the anomaly check; a preliminary row isn't
        certified-tier-eligible in the first place, so there's nothing to
        demote it FROM.
      - anything else (legacy NULL, an unrecognized string) -> "partial",
        unchanged safe default -- this fallback is NOT loosened by this fix.

    assessed_value/market_value are optional (default None) so existing
    callers that only have data_source on hand keep working -- they just
    don't get the anomaly-check benefit until updated to pass real values.
    Every real call site in this codebase has been updated to pass them
    (see the July 2026 brief's own diff) -- a caller passing only
    data_source going forward should be treated as a gap to close, not a
    supported permanent shape.

    Only 3 of the site's 5 confidence tiers are reachable from this
    function ("not_available" and "estimated" are not -- see prior
    docstring revision for why; unchanged by this fix).
    """
    if data_source in CERTIFIED_TIER_DATA_SOURCES:
        if assessed_value is not None and market_value is not None and assessed_value > market_value:
            return "partial"
        return "verified"
    if data_source == "preliminary":
        return "preliminary"
    return "partial"


def _appraisal_badge_kind(data_source, assessed_value=None, market_value=None):
    """Single source of truth for property.html's "Property-level confidence
    badge" block (and its smaller "2025 Certified" mini-badge on the Current
    Values card) -- which of the four possible "Appraisal: ..." badges to
    show for a given row.

    Added Aug 2026 as part of the same fix that added "dcad_certified" to
    CERTIFIED_TIER_DATA_SOURCES above. Before this function existed,
    property.html kept its OWN hand-copied duplicate of
    CERTIFIED_TIER_DATA_SOURCES plus its own inline reimplementation of the
    assessed>market anomaly check, on the stated rationale that "Jinja can't
    call a Python module function" -- which was never actually true. Flask
    injects context-processor globals (this app already does exactly that
    for `config`, see inject_mode() above) into every template render, so a
    plain Python function is just as callable from Jinja as from Python.
    That duplicate copy silently missed "dcad_certified" when Dallas went
    live, mislabeling real certified Dallas rows as "Appraisal: Unknown" --
    the live bug this function's introduction is meant to make structurally
    impossible going forward (one classification function, not two
    hand-synced copies). See its registration via context_processor below.

    Returns one of:
      "certified"   -- certified-tier data_source, no AV>MV anomaly this year
      "anomaly"     -- certified-tier data_source, but assessed_value >
                       market_value for this specific record
      "preliminary" -- data_source == "preliminary" (2026 roll, pre-cert)
      "unknown"     -- anything else (legacy NULL, or a genuinely
                       unrecognized data_source string -- NOT "not yet added
                       to CERTIFIED_TIER_DATA_SOURCES," which is a bug to
                       fix in that set, not a legitimate "unknown" case)
    """
    if data_source in CERTIFIED_TIER_DATA_SOURCES:
        if assessed_value is not None and market_value is not None and assessed_value > market_value:
            return "anomaly"
        return "certified"
    if data_source == "preliminary":
        return "preliminary"
    return "unknown"


def _county_has_data(county_code):
    """PX-20260824-06: real, ground-truth "has this county actually been
    loaded" check -- distinct from "is this county registered" (COUNTY_SLUGS
    membership, already enforced by _pull_county_slug() before any view
    function runs) and distinct from COUNTY_PROFILES membership (at the time
    this function was written, Dallas had a profile entry with zero loaded
    rows -- since gone live, Aug 2026, see COUNTY_SLUGS' own updated comment
    -- Harris is still in that zero-loaded-rows state as of this writing).
    This function's whole POINT is that it never needs updating when that
    changes: it queries parcel directly rather than a separate
    "counties with data" registry constant: any manually-maintained flag
    is exactly the kind of thing that goes stale the moment a real load
    lands (the failure class this whole brief exists to close off), while
    parcel is the actual table every query in this function reads from, so
    this can't drift from what the real query below would find. Cheap:
    migrate_county_partitioning.py made county_code the leading column of
    parcel's composite primary key, so this is an index-only prefix scan,
    not a sequential scan (see schema.sql's "real, urgent hotfix" comment
    block for the migration that did this)."""
    return query(
        "SELECT EXISTS(SELECT 1 FROM parcel WHERE county_code = %(county_code)s) AS has_data",
        {"county_code": county_code}, one=True,
    )["has_data"]


# ── PX-20260828-15 Task 3 (filter honesty for unavailable data) ────────────
# Diego's sign-off (per this brief's own gate -- Task 3's UX needed approval
# before building): disable the affected Filter Parcels control inline,
# named-county caption, via a GENERIC per-table availability check rather
# than a one-off boolean -- so the next data-dependent filter (see comment
# on FILTER_DATA_REQUIREMENTS below) reuses this instead of a copy-pasted
# has_x_data flag, the same "fix it once, structurally" ruling this brief's
# own Task 1 already applied to the neutral-route bug.
#
# Distinct from _county_has_data() above: that function gates the whole
# route on "has this county been loaded AT ALL" (the parcel table, the one
# prerequisite every query here shares). This function is for the narrower,
# additional case Diego approved: a county that DOES have real data can
# still be missing one specific FILTER's backing table entirely -- checked
# per-table, per-county, so it can never silently drift out of sync with
# what a real search for that filter would actually find (same reasoning
# _county_has_data() itself already uses).
def _table_has_county_data(table, county_code):
    """Cheap EXISTS check for whether `table` has any row at all for
    `county_code` -- `table` is always a literal from FILTER_DATA_REQUIREMENTS
    below, never user input, so an f-string here carries the same trust
    boundary as _county_has_data()'s own literal "parcel" table name."""
    return query(
        f"SELECT EXISTS(SELECT 1 FROM {table} WHERE county_code = %(county_code)s) AS has_data",
        {"county_code": county_code}, one=True,
    )["has_data"]


# Registry of Filter Parcels controls whose backing table isn't guaranteed
# populated for every registered, has-data county -- i.e. narrower gaps than
# _county_has_data() catches. Each entry name is the search.html Jinja/JS key
# ({{ filter_data_availability[...] }} / data-unavailable attribute); each
# value is the one real table that filter's WHERE/JOIN condition in
# api_search_filter() actually reads.
#
# 'delinquent': tax_delinquent is sourced solely from Travis's own
# TaxDelqOpenData.csv (see loaders/load_tax_current.py's load_delinquent()
# docstring) -- confirmed via a full-repo grep that no Dallas-equivalent
# loader exists anywhere (load_tax_current.py's --county flag can WRITE any
# county's rows once given a source file, but no such source file has been
# acquired for Dallas yet), and via the table's own resting row count
# (~10,087, all Travis, per SPEC_COUNTY_PARTITIONING.md's pre-partition
# snapshot) -- the confirmed, in-scope case for this brief.
#
# 'effective_tax_rate': the etr_min/etr_max filter's real source is
# parcel_metrics.effective_tax_rate, itself computed by compute_metrics.py
# from SUM(tax_billing_entity.amount_due) at tax_year=2025 (never from
# parcel_metrics directly, and never from tax_billing.total_tax -- see that
# file's own comment on why: TOTAL_TAX is blank for ~93% of 2025 rows).
# tax_billing_entity is itself a derived rollup (tax_billing_rollup.py),
# rebuilt from tax_billing_account_entity -- the real per-account source
# table -- so it's the correct proxy for "was ETR's underlying billing data
# ever loaded for this county," same relationship tax_delinquent has to its
# own loader above.
# Diego confirmed live (2026-08-29): tax_billing DALLAS = 0 rows,
# tax_billing_account DALLAS = 0 rows. Same root cause as 'delinquent':
# confirmed via repo-wide grep, there is no Dallas billing loader at all --
# not a job that hasn't run yet, but one that was never built. (Dallas's
# own load_dallas_certified.py loader writes parcel/parcel_tax_year/prop_unit
# from DCAD's certified-roll export; DCAD billing/tax-collection data is a
# separate source that hasn't been acquired.) tax_billing_entity will read
# empty for DALLAS until that acquisition work happens, so this entry is
# correct today, not provisional.
#
# _table_has_county_data() assumes `table` has a live `county_code` column --
# true for tax_billing_entity despite schema.sql's own CREATE TABLE IF NOT
# EXISTS for it still showing the pre-PARTITION-2 DDL (no county_code): that
# DDL is bootstrap-only and never re-runs against an already-existing table,
# while migrate_county_partitioning.py's TABLE_SPECS (which already includes
# tax_billing_entity in the same batch as tax_delinquent) is what actually
# added the column in production. Confirmed via tax_billing_rollup.py's own
# real INSERT INTO tax_billing_entity (county_code, geo_id, tax_year, ...) --
# the live write shape a stale schema.sql alone would not have shown.
FILTER_DATA_REQUIREMENTS = {
    "delinquent": "tax_delinquent",
    "effective_tax_rate": "tax_billing_entity",
}


# PX-20260829-04 Task 1 (measured-diagnosis fix, sign-off given): TTL cache
# for _live_counties(), same pattern as _SNAPSHOT_CACHE above (module-level
# dict, TTL in seconds, no event-invalidation -- this data only ever changes
# when Diego runs a loader that brings a new county live, not on any
# request-driven signal this process could listen for). Before this cache,
# EVERY call to _live_counties() -- and every typeahead keystroke on a
# neutral page called it once, via search_parcels_by_address()'s own
# cross-county branch above -- issued one query() (one fresh DB connection,
# get_db() has no pooling) per REGISTERED slug (3 today: travis-tx,
# dallas-tx, harris-tx), regardless of how many are actually live. That's 3
# serial connections just to answer "what's live," before a single byte of
# the actual address search had run. 60s is short enough that a real
# go-live is visible within a minute (Diego doesn't need this instant --
# there's no user-facing SLA on "how fast does a newly-loaded county appear
# in the picker"), long enough to absorb an entire burst of typeahead
# keystrokes on one cache fill.
#
# Per-process, same caveat as _SNAPSHOT_CACHE (see that comment above): under
# N gunicorn workers, each worker keeps its own independent copy of this
# cache -- correct, just N separate 60s windows rather than one shared one.
# Worth stating plainly (flagged in this brief's own final report) rather
# than leaving implicit, especially since this brief's own Task 1 finding
# is that the app currently runs under gunicorn's default of ONE worker --
# where this distinction doesn't even apply yet, but will the moment worker
# count is increased per this same report's infra recommendation.
_LIVE_COUNTIES_CACHE = {"ts": 0, "value": None}
_LIVE_COUNTIES_CACHE_TTL_SECONDS = 60


def _live_counties():
    """PX-20260827-03-rev1 Task 2/3: the one, real "what's live" list every
    county picker (search/info dropdowns, nav switcher) and every launch
    surface (homepage coverage map/legend/cards, footer coverage block)
    should render from -- registered (COUNTY_SLUGS) AND has real loaded
    data (_county_has_data), same two-part definition Task 3's brief spells
    out. Deliberately NOT "has a COUNTY_PROFILES entry" -- Dallas had a
    profile before it had data (see COUNTY_PROFILES' own comments), and a
    profile-only county should not show as live anywhere. Returns a list of
    dicts (not a bare county_code list) so templates/JS get slug + display
    name for free, in COUNTY_SLUGS' own declared order -- one query per
    registered slug (currently 3 max), each an index-only prefix scan per
    _county_has_data's own docstring.

    PX-20260829-04: now TTL-cached (see _LIVE_COUNTIES_CACHE above) -- this
    function used to be called fresh on every single invocation (every page
    render via the context processor below, AND every typeahead keystroke
    on a neutral page), each one paying 3 real DB round trips regardless of
    whether anything could possibly have changed since the last call a
    fraction of a second earlier."""
    now = time.monotonic()
    cached = _LIVE_COUNTIES_CACHE
    if cached["value"] is not None and (now - cached["ts"]) < _LIVE_COUNTIES_CACHE_TTL_SECONDS:
        return cached["value"]
    live = []
    for slug, county_code in COUNTY_SLUGS.items():
        if _county_has_data(county_code):
            profile = COUNTY_PROFILES.get(county_code, {})
            live.append({
                "slug": slug,
                "county_code": county_code,
                "value": slug.split("-")[0],
                "display_name": profile.get("display_name", county_code.title()),
                "county_name": profile.get("county_name", county_code.title()),
                # PX-20260828-09 Task 3: the homepage provenance panel used
                # to read county_profile.cad_name/cad_abbr/tax_office_name
                # directly -- the CURRENT REQUEST's county only (Travis's
                # profile on every neutral page, since county_profile falls
                # back to TRAVIS outside an anchored route), so it never
                # showed Dallas's real sources even once Dallas went live.
                # Added here (not a separate lookup) so every live county's
                # own two real sources travel with the same live_counties
                # list every other "what's live" surface already reads from
                # -- single source of truth, not a second registry read.
                "cad_name": profile.get("cad_name", ""),
                "cad_abbr": profile.get("cad_abbr", ""),
                "tax_office_name": profile.get("tax_office_name", ""),
            })
    _LIVE_COUNTIES_CACHE["ts"] = now
    _LIVE_COUNTIES_CACHE["value"] = live
    return live


# PX-20260827-03-rev1 Task 3: parcel counts for the coverage map / footer /
# market cards are real per-county COUNT(*) queries (Task 3's own rule: "no
# invented numbers"), NOT a hardcoded marketing figure like the old "508K+"
# text was -- but _live_counties() above is called from the SITE-WIDE
# context processor (base.html's footer coverage block needs it on every
# page, not just the homepage), and a full COUNT(*) against a 500K+-row
# table on every single request, once per live county, is exactly the class
# of per-request aggregate cost this codebase has already had to walk back
# reactively elsewhere (see AGGPRECOMP-1/2's group_stats/snapshot_summary
# precomputation). A short in-process TTL cache is the minimal fix that
# keeps the number real and DB-sourced (not invented) without adding a live
# COUNT(*) to every page load -- worth graduating to a real precomputed
# table later if this stops being cheap enough (e.g. once Harris's larger
# parcel count is also live).
_COUNTY_PARCEL_COUNT_CACHE = {}
_COUNTY_PARCEL_COUNT_CACHE_TTL_SECONDS = 300


def _cached_county_parcel_count(county_code):
    now = time.monotonic()
    cached = _COUNTY_PARCEL_COUNT_CACHE.get(county_code)
    if cached is not None and (now - cached[1]) < _COUNTY_PARCEL_COUNT_CACHE_TTL_SECONDS:
        return cached[0]
    # Real-property-only, same exclusion every other "parcel count" figure
    # on this site already uses (CANONICAL_PARCEL_EXCL + the L-class gap
    # fix from AGGPRECOMP-1-FIX) -- not a different, inconsistent count.
    count_row = query(
        f"SELECT COUNT(*) AS n FROM parcel p WHERE p.county_code = %(county_code)s "
        f"{CANONICAL_PARCEL_EXCL} AND {exclude_non_real_property_gap_sql('p.state_cd1')}",
        {"county_code": county_code}, one=True,
    )
    count = count_row["n"] if count_row else 0
    _COUNTY_PARCEL_COUNT_CACHE[county_code] = (count, now)
    return count


def _human_count(n):
    """PX-20260828-09 Task 1, Diego's 2026-08-28 ruling: rounded, human-scale
    parcel-count formatting ("1.19M+"), replacing the exact comma-formatted
    figures ("1,199,683+") this function's own docstring used to argue FOR
    (see git history) -- that earlier call is explicitly SUPERSEDED by this
    ruling, not an oversight. Exact counts require ongoing upkeep and read
    oddly as counties are added; a rounded figure removes that churn without
    giving up honesty, because of the specific rounding direction below.

    FLOOR, not round-to-nearest -- this is the part that preserves the "+"
    suffix's existing honesty guarantee (every call site already appends
    "+" itself; this function never does). Round-to-nearest would let a
    real count of 1,199,683 display as "1.20M+", which is FALSE -- 1,199,683
    is less than 1,200,000, so "at least 1.20M" overstates the real number.
    Flooring to "1.19M+" is always true: the real count is guaranteed to be
    >= the displayed figure, exactly the guarantee "+" is supposed to carry.

    Rule (approved as proposed, PX-20260828-09):
      - n >= 1,000,000: floor to 2 decimals, "M" suffix (1,199,683 -> "1.19M")
      - 1,000 <= n < 1,000,000: floor to whole thousands, "K" suffix
        (705,536 -> "705K")
      - n < 1,000: exact integer, no suffix -- too small for K/M to read as
        anything but silly (a future tiny county's edge case, not reachable
        by today's real data).

    Deliberately NOT used for analytical sample-size figures (e.g. Market
    Snapshot's "n=47 comparable parcels", property.html's peer-set counts)
    -- those are a different category of number where an exact count is the
    point (rounding a sample size down would misrepresent the statistical
    basis, not just the marketing framing), and PX-20260828-09's own brief
    scoped this rule to site-coverage displays only.
    """
    n = int(n)
    if n >= 1_000_000:
        return f"{(n // 10_000) / 100:.2f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def _live_counties_with_counts():
    """Same list as _live_counties(), with a real (TTL-cached) parcel_count
    added to each entry. Used specifically by surfaces that display a
    count (homepage trust strip/market cards, footer coverage block) --
    the county picker/switcher (Task 2) uses the cheaper _live_counties()
    directly since it has no count to show."""
    live = _live_counties()
    for entry in live:
        count = _cached_county_parcel_count(entry["county_code"])
        entry["parcel_count"] = count
        # PX-20260828-09 Task 1: was f"{count:,}" (exact, comma-formatted) --
        # Diego's ruling supersedes that earlier "exact is more honest"
        # decision (see _human_count()'s own docstring for the full
        # reasoning: floor-based rounding preserves the same honesty
        # guarantee the "+" suffix already relies on, while removing the
        # upkeep churn of exact counts as more counties go live).
        entry["parcel_count_display"] = _human_count(count)
    return live


@app.route("/<county_slug>/api/search_filter")
@limiter.limit(_LIMIT_HEAVY)
def api_search_filter():
    """Filtered parcel search behind the Search page's optional filter panel.
    Returns paginated results; requires at least one real filter beyond
    County (and Tax Year — see has_real_filter below) to avoid running an
    effectively-unbounded query against 508K+ parcels."""
    args = request.args

    def _f(name):
        v = (args.get(name) or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None

    def _i(name):
        v = (args.get(name) or "").strip()
        try:
            return int(v) if v else None
        except ValueError:
            return None

    county          = (args.get("county") or "travis").strip().lower()
    neighborhood    = (args.get("neighborhood") or "").strip()
    prop_type       = (args.get("prop_type") or "").strip()
    use_code        = (args.get("use_code") or "").strip()
    mv_min, mv_max  = _f("mv_min"), _f("mv_max")
    etr_min, etr_max = _f("etr_min"), _f("etr_max")
    etr_include_na  = (args.get("etr_include_na") or "") == "1"
    bldg_min, bldg_max = _f("bldg_min"), _f("bldg_max")
    land_min, land_max = _f("land_min"), _f("land_max")
    yr_min, yr_max  = _i("yr_min"), _i("yr_max")
    large_value_jump = (args.get("large_value_jump") or "") == "1"
    homestead       = (args.get("homestead") or "").strip()   # 'has' | 'not_has' | ''
    verified_only   = (args.get("verified_only") or "") == "1"
    # NICK-DELINQUENT-1 (Aug 2026): "Delinquent Only" filter -- Nick's real
    # feature request, combinable with every other filter here (asset type
    # included, per his actual ask: narrow delinquent parcels down by type).
    delinquent_only = (args.get("delinquent_only") or "") == "1"
    tax_year        = _i("tax_year") or 2025
    page            = max(1, _i("page") or 1)

    # PX-20260824-06: was `if county != "travis": ... 400` -- a hardcoded
    # literal comparison against the `county` query-string param (Task 1
    # finding: this param predates URL-based county routing -- g.county_code,
    # already resolved+validated against COUNTY_SLUGS by _pull_county_slug()
    # before this view function ever runs, is what actually scopes every
    # query below; `county` itself is otherwise unused in this function).
    # Replaced with a registry-derived check against g.county_slug (the
    # request's own already-validated slug) rather than a second hardcoded
    # county list to keep in sync with COUNTY_SLUGS -- catches a stale/
    # malformed `county` param (e.g. a page loaded before a dropdown
    # refresh, or a hand-crafted request) without reintroducing a literal.
    expected_county_token = g.county_slug.split("-")[0]
    if county != expected_county_token:
        return jsonify({
            "ok": False,
            "error": (f"County mismatch: this page is scoped to "
                      f"{COUNTY_PROFILES.get(g.county_code, COUNTY_PROFILES['TRAVIS'])['county_name']}, "
                      f"but the request's county parameter was '{county}'."),
        }), 400

    # ── Minimum-filter guard ────────────────────────────────────────────────
    # County and Tax Year each SELECT which slice of data to look at — neither
    # narrows the underlying population on its own, so neither counts toward
    # the "at least one filter beyond County" requirement. Every other filter
    # does narrow the result set, so any one of them satisfies the guard.
    #
    # etr_include_na is deliberately NOT in this list. It's a modifier on the
    # ETR range filter, not a filter on its own — the WHERE-clause block below
    # only emits a condition when etr_min or etr_max is set (see "if etr_min
    # is not None or etr_max is not None" further down); etr_include_na alone
    # never reaches that block, so it never actually narrows the query. Before
    # this fix, etr_include_na=1 with no min/max satisfied this guard while
    # producing zero WHERE conditions — i.e. an unfiltered scan of all 508K+
    # parcels could slip through. It must pair with etr_min and/or etr_max.
    has_real_filter = any([
        neighborhood, prop_type, use_code,
        mv_min is not None, mv_max is not None,
        etr_min is not None, etr_max is not None,
        bldg_min is not None, bldg_max is not None,
        land_min is not None, land_max is not None,
        yr_min is not None, yr_max is not None,
        large_value_jump, homestead, verified_only, delinquent_only,
    ])
    if not has_real_filter:
        return jsonify({
            "ok": False,
            "error": ("Select at least one filter beyond County to run a search — County alone "
                      "would match all 508,000+ Travis County parcels."),
        }), 400

    # DALLAS-GATE-2 Part 2 (search_filter-related dynamic WHERE construction --
    # same risk shape as the original incident): this `where` list used to
    # have NO county_code predicate anywhere in it, and the query built below
    # assembles its WHERE clause via `" AND ".join(where)` -- an f-string
    # fragment (`{where_sql}`) verify_index_coverage.py's static audit cannot
    # resolve, which is exactly why this gap survived schema-sql-mode
    # auditing undetected until a human read the real, assembled SQL.
    # Filtering only via geo_id-equality JOINs (p.geo_id = pty.geo_id, etc.)
    # against parcel / parcel_tax_year / parcel_metrics -- all county_code-
    # leading composite-PK tables per migrate_county_partitioning.py's
    # TABLE_SPECS -- is precisely the incident's failure shape. g.county_code
    # (set per-request by _pull_county_slug) is threaded through below as an
    # ADDITIONAL predicate only -- no existing filter, ORDER BY, or column
    # selection changes.
    county_code = g.county_code

    # PX-20260824-06 Task 2: "has data" gate, distinct from the registration
    # check above. g.county_code being resolvable at all already proves the
    # county is REGISTERED (COUNTY_SLUGS membership, enforced by
    # _pull_county_slug()) -- it says nothing about whether that county has
    # actually been loaded. Dallas/Harris will sit registered-but-dataless
    # between COUNTY_SLUGS registration and their real first load (see
    # PX-20260824-05's onboarding runbook) -- without this check, a request
    # for either would fall through to the query below, which would just
    # return an honest-looking "0 results" page indistinguishable from a
    # genuine Travis search that happens to match nothing. That's a worse
    # failure mode than an error: it looks like the search worked.
    if not _county_has_data(county_code):
        county_name = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])["county_name"]
        return jsonify({
            "ok": False,
            "no_data_yet": True,
            "error": f"{county_name} data hasn't been loaded yet.",
        }), 200

    where = ["1=1", "p.county_code = %(county_code)s"]
    params = {"tax_year": tax_year, "county_code": county_code}

    if neighborhood:
        where.append("p.neighborhood_cd = %(neighborhood)s")
        params["neighborhood"] = neighborhood

    _ptype_sql = label_case_sql("p.classi_cd", "p.state_cd1")  # emits no '%' — safe alongside %()s params

    if prop_type:
        where.append(f"({_ptype_sql}) = %(prop_type)s")
        params["prop_type"] = prop_type

    if use_code:
        where.append("p.classi_cd = %(use_code)s")
        params["use_code"] = use_code

    if mv_min is not None:
        where.append("pty.market_value >= %(mv_min)s")
        params["mv_min"] = mv_min
    if mv_max is not None:
        where.append("pty.market_value <= %(mv_max)s")
        params["mv_max"] = mv_max

    # effective_tax_rate is stored as a fraction (e.g. 0.020465), displayed
    # elsewhere ×100 as a percentage — user-entered min/max here are percentages
    # and must be divided by 100 before comparing against the stored column.
    if etr_min is not None or etr_max is not None:
        etr_conds = []
        if etr_min is not None:
            etr_conds.append("pm.effective_tax_rate >= %(etr_min)s")
            params["etr_min"] = etr_min / 100.0
        if etr_max is not None:
            etr_conds.append("pm.effective_tax_rate <= %(etr_max)s")
            params["etr_max"] = etr_max / 100.0
        etr_clause = " AND ".join(etr_conds)
        if etr_include_na:
            where.append(f"(({etr_clause}) OR pm.effective_tax_rate IS NULL)")
        else:
            where.append(f"({etr_clause})")

    if bldg_min is not None:
        where.append("p.living_area_sqft >= %(bldg_min)s")
        params["bldg_min"] = bldg_min
    if bldg_max is not None:
        where.append("p.living_area_sqft <= %(bldg_max)s")
        params["bldg_max"] = bldg_max

    if land_min is not None:
        where.append("p.land_sqft >= %(land_min)s")
        params["land_min"] = land_min
    if land_max is not None:
        where.append("p.land_sqft <= %(land_max)s")
        params["land_max"] = land_max

    if yr_min is not None:
        where.append("p.year_built >= %(yr_min)s")
        params["yr_min"] = yr_min
    if yr_max is not None:
        where.append("p.year_built <= %(yr_max)s")
        params["yr_max"] = yr_max

    if large_value_jump:
        where.append("pm.risk_large_value_jump = TRUE")

    if homestead in ("has", "not_has"):
        params["hs_re"] = _HS_TOKEN_RE
        if homestead == "has":
            where.append("(pty.exemption_codes IS NOT NULL AND pty.exemption_codes ~ %(hs_re)s)")
        else:
            # Lead-gen filter quality fix (July 2026, per Fable review P1-14):
            # this used to check ONLY the requested tax_year's exemption_codes
            # (pty, joined above at =%(tax_year)s -- 2025 certified by
            # default). A parcel that has ALREADY filed a homestead, visible
            # only on the 2026 preliminary roll (not yet reflected in 2025
            # certified figures), still passed this check and showed up as a
            # "no homestead" lead -- the same "use the latest available
            # year's exemption data" gap already fixed on the property page
            # (see property.html's excodes_year logic). This "Homes Without
            # Homestead Exemption" quick filter exists specifically to
            # surface real outreach leads, so a parcel that's already filed
            # isn't a cosmetic near-miss here, it's a wrong lead. Now also
            # excludes any parcel whose 2026 preliminary row shows HS,
            # regardless of which tax_year the rest of the search is
            # otherwise scoped to.
            # PX-20260828-12 fix: this correlated subquery matched pty26 to
            # the outer pty row by geo_id alone. The outer query is already
            # county-scoped (p.county_code = %(county_code)s above), but
            # geo_id is only unique WITHIN a county (composite PK per
            # migrate_county_partitioning.py), so an unscoped subquery like
            # this one could in principle match a different county's row
            # sharing the same geo_id string. Added pty26.county_code to
            # close that gap explicitly rather than relying on it being
            # low-probability given Dallas's differently-shaped account IDs.
            where.append("""(
                (pty.exemption_codes IS NULL OR pty.exemption_codes !~ %(hs_re)s)
                AND NOT EXISTS (
                    SELECT 1 FROM parcel_tax_year pty26
                    WHERE pty26.geo_id = pty.geo_id AND pty26.tax_year = 2026
                      AND pty26.county_code = pty.county_code
                      AND pty26.exemption_codes IS NOT NULL
                      AND pty26.exemption_codes ~ %(hs_re)s
                )
            )""")

    if verified_only:
        # Consolidated (July 2026, per Diego's "Fix AJR/Historical-Year
        # Confidence Tiering" brief, item 4): this used to be a raw
        # `pty.data_source = 'certified'` string match -- an independent
        # copy of the same confidence idea _row_confidence() codifies,
        # found during the brief's call-site audit. Left as-is it would
        # have gone stale the moment _row_confidence() below was widened:
        # a caller requesting tax_year=2022 with verified_only=1 would
        # always get zero results, even for a 2022 row that now genuinely
        # qualifies as verified-tier (cert_2022/ajr_2022, no AV>MV anomaly)
        # -- "verified only" silently meaning something narrower than what
        # "Verified" now means everywhere else on the site. Rebuilt to use
        # the same CERTIFIED_TIER_DATA_SOURCES set (data_source membership)
        # AND the same per-record AV>MV anomaly check _row_confidence()
        # uses, rather than a second hand-written comparison -- not a
        # parallel copy of the tiering rule, the same rule expressed as SQL
        # because this filter runs before rows are ever loaded into Python.
        params["cert_sources"] = list(CERTIFIED_TIER_DATA_SOURCES)
        where.append("pty.data_source = ANY(%(cert_sources)s)")
        where.append("(pty.assessed_value IS NULL OR pty.market_value IS NULL OR pty.assessed_value <= pty.market_value)")

    # ── Delinquent Only (NICK-DELINQUENT-1) ─────────────────────────────────
    # The actual filter/join happens below via delinquent_join (an INNER JOIN
    # on tax_delinquent with the total_due > 0 condition baked into its ON
    # clause, per the brief's explicit ask for "a real join/filter against
    # tax_delinquent.total_due > 0") -- nothing to add to `where` for the
    # join condition itself.
    #
    # Real-property scoping finding, flagged rather than silently decided:
    # this route (every filter above -- asset type, value range, homestead,
    # etc.) does NOT apply CANONICAL_PARCEL_EXCL anywhere today. Confirmed by
    # reading this entire function; grep of app.py also shows no
    # CANONICAL_PARCEL_EXCL reference inside api_search_filter(). So "respect
    # this week's Classification Map work / CANONICAL_PARCEL_EXCL... no more
    # than any other filter does" (the brief's own words) has a literal
    # reading (none of them do it, so neither must this one) and an intent
    # reading (a delinquency-focused lead list is exactly the kind of
    # export-for-outreach feature where a stray exempt/personal-property row
    # is most visible and most costly to get wrong). Applied here, scoped
    # ONLY to delinquent_only, not to the route as a whole -- retrofitting
    # every existing filter's population is a real, separate change with its
    # own production-count impact on filters already in active use, and
    # wasn't authorized by this brief. Uses CANONICAL_PARCEL_EXCL_BARE
    # together with exclude_non_real_property_gap_sql() (not
    # CANONICAL_PARCEL_EXCL alone) -- this is a raw-grain query returning
    # individual parcel rows, not one grouped through label_case_sql()'s
    # taxonomy, so it has none of that taxonomy's incidental protection
    # against 'L'-class (Business Personal Property) rows slipping through,
    # the exact gap AGGPRECOMP-1-FIX documented and fixed for group_stats.
    if delinquent_only:
        where.append(CANONICAL_PARCEL_EXCL_BARE)
        where.append(exclude_non_real_property_gap_sql("p.state_cd1"))

    where_sql = " AND ".join(where)
    offset = (page - 1) * SEARCH_FILTER_PAGE_SIZE
    params["offset"] = offset

    # total_due / first_delinquent_yr (both real tax_delinquent columns) are
    # only selected/joined when delinquent_only is active -- see
    # api_search_filter's results loop below and search.html's
    # fltDelinquentOnly-gated column toggle for the other half of this.
    delinquent_join = ""
    delinquent_select = ""
    if delinquent_only:
        # DALLAS-GATE-2 Part 2: county_code added to this JOIN's ON clause
        # (tax_delinquent is one of migrate_county_partitioning.py's
        # county_code-leading composite-PK tables) -- matches the p.county_code
        # predicate now in `where` above rather than leaving this one JOIN as
        # the sole unscoped access path into tax_delinquent.
        delinquent_join = "JOIN tax_delinquent d ON d.geo_id = p.geo_id AND d.total_due > 0 AND d.county_code = p.county_code"
        delinquent_select = ", d.total_due, d.first_delinquent_yr"

    # PX-20260828-14: live 500 (psycopg2.errors.QueryCanceled, 8s
    # statement_timeout) on County=Dallas + Market Value >= 1,000,000,
    # tax_year 2025 -- confirmed root cause via reconstructed-query EXPLAIN
    # reasoning (no live DB in this sandbox; Diego re-verified live post-fix):
    # `COUNT(*) OVER() AS total_count` is a blocking window function with no
    # PARTITION BY -- Postgres must fully materialize and join EVERY matching
    # row before it can emit the first page's 50, regardless of the
    # (also unindexed-until-this-fix, see idx_parcel_county_situs in
    # schema.sql) situs_address sort. Dallas's five-year load
    # (~3.5M parcel_tax_year rows) pushed a previously-tolerable pattern past
    # the timeout. Fix approved by Diego: (a) drop the window function, run
    # a separate COUNT query instead; (c) add a supporting index for the
    # county-scoped situs_address sort. (b) -- "has next page" via LIMIT+1 --
    # held as fallback, not the primary mechanism, per Diego's own read of
    # search.html: body.total/body.total_pages are load-bearing for more than
    # display text (see comment below at total_pages/has_more computation).
    #
    # Hard requirement (Diego, verbatim): "the separate count query must
    # share the exact WHERE/JOIN construction with the data query -- build
    # the predicates once, use them in both. Do not hand-copy a parallel
    # set." from_joins_sql below is that shared construction -- both queries
    # below interpolate this SAME Python variable (and the SAME where_sql
    # variable, already built once above), not two independently maintained
    # copies of the same FROM/JOIN text. This is the identical failure shape
    # Diego cited from tonight's audit (PX-20260828-12 Category 7): two
    # predicate sets that must agree forever, drifting apart the moment one
    # is edited and the other isn't.
    # PX-20260828-16: parcel_metrics is joined via LEFT JOIN and NEITHER
    # query's SELECT list ever reads a pm.* column (confirmed by reading both
    # SELECT lists below) -- the only reason to join it at all is that
    # etr_min/etr_max/large_value_jump filter on pm.effective_tax_rate /
    # pm.risk_large_value_jump inside where_sql. Whenever none of those three
    # filters is active, this join does real, unindexed-by-county work (its
    # real PK is (geo_id, tax_year) per schema.sql -- no county_code in the
    # index at all) for zero effect on the answer, on every single request
    # regardless of filter shape. It's LEFT JOIN + at-most-one-match-per-row
    # (guaranteed by that (geo_id, tax_year) PK) + zero columns read, so
    # dropping it changes nothing about which rows exist or how many --
    # only removes wasted work. This is one shared decision (needs_metrics),
    # derived from the SAME where_sql string already built once above, used
    # identically to build the ONE from_joins_sql both queries interpolate --
    # not two independently-tracked flags that could drift.
    needs_metrics = "pm." in where_sql
    parcel_metrics_join = (
        "LEFT JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = %(tax_year)s\n"
        "                                    AND pm.county_code = p.county_code"
        if needs_metrics else ""
    )
    from_joins_sql = f"""
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = %(tax_year)s
                                 AND pty.county_code = p.county_code
        {parcel_metrics_join}
        {delinquent_join}
    """

    # LIMIT+1: always fetch one extra row beyond the page size. This is
    # option (b) ("has next page" semantics) held as Diego's approved
    # fallback -- not used for normal pagination (total/total_pages from the
    # real COUNT query below remain the primary, exact source search.html's
    # display text and CSV-exporter page-walking loop rely on), but computed
    # for free as a byproduct of the data query so it's available as a
    # cheap, always-correct has_more signal on the rare path where the count
    # query itself times out (see below) -- degrading the endpoint to
    # approximate pagination info instead of failing the whole request.
    params["fetch_limit"] = SEARCH_FILTER_PAGE_SIZE + 1

    data_sql = f"""
        SELECT
            p.geo_id, p.situs_address, p.neighborhood_cd,
            ({_ptype_sql}) AS prop_type_label,
            pty.market_value, pty.assessed_value, pty.data_source, pty.tax_year
            {delinquent_select}
        {from_joins_sql}
        WHERE {where_sql}
        ORDER BY p.situs_address NULLS LAST, p.geo_id
        LIMIT %(fetch_limit)s OFFSET %(offset)s
    """
    fetched = query(data_sql, params)
    has_more = len(fetched) > SEARCH_FILTER_PAGE_SIZE
    rows = fetched[:SEARCH_FILTER_PAGE_SIZE]

    # Separate COUNT query, no window function, no wide SELECT list, no
    # ORDER BY/LIMIT/OFFSET -- reuses from_joins_sql and where_sql verbatim
    # (same params dict too; COUNT(*) never references fetch_limit/offset,
    # psycopg2 silently ignores unused dict keys in a named-parameter query,
    # so no separate params object is needed either).
    #
    # Own, independent timeout (Diego, verbatim): "give the count query its
    # own timeout handling so if it's ever the slower half, it degrades
    # rather than taking the whole endpoint down." 5000ms -- deliberately
    # SHORTER than get_db()'s connection-wide 8000ms default -- so a
    # regressed count fails fast and leaves headroom for the data query
    # (already fetched above, successfully, by the time this runs) to still
    # reach the client. If a filter combination ever makes the count the
    # slower half, the user gets real, correct results with an honestly
    # degraded total instead of a 500 on the whole request.
    count_sql = f"""
        SELECT COUNT(*) AS total_count
        {from_joins_sql}
        WHERE {where_sql}
    """
    count_unavailable = False
    try:
        count_rows = query(count_sql, params, timeout_ms=5000)
        total = int(count_rows[0]["total_count"]) if count_rows else 0
        total_pages = (total + SEARCH_FILTER_PAGE_SIZE - 1) // SEARCH_FILTER_PAGE_SIZE if total else 0
    except psycopg2.errors.QueryCanceled as exc:
        # PX-20260828-16: this except clause -- narrowly typed to
        # psycopg2.errors.QueryCanceled, confirmed NOT catching anything
        # broader (any other exception here still propagates and 500s,
        # visible to Sentry's FlaskIntegration auto-capture like any other
        # uncaught error; that's correct and unchanged) -- was firing
        # silently: no log line, no Sentry event, nothing distinguished a
        # real timeout from a request that just happened to degrade.
        # Confirmed via a real incident (Dallas mv_min=9999999, tax_year
        # 2025): the count degraded in the UI while Render logs showed a
        # plain 200 with zero trace of why. Whatever the true cause turns
        # out to be for a given occurrence -- a genuinely slow plan, a
        # planner misestimate, contention under concurrent load, or
        # something not yet considered -- this must be diagnosable from
        # Render logs alone from now on, not require a hand-run EXPLAIN to
        # even notice something happened. Logs the exception type/message
        # AND enough request context (county, tax_year, and which filters
        # were active -- not the full SQL/params, to keep log volume sane)
        # to reproduce. Uses the same sentry_sdk.capture_exception() +
        # flush(timeout=2) pattern already established for exactly this
        # "caught exception has zero Sentry visibility" gap (BILLING-DIAG-3/
        # BILLING-DIAG-4), plus a print() alongside it (BILLING-DIAG-5's
        # same reasoning: redundant visibility straight in Render's log
        # stream, independent of Sentry being reachable/configured).
        active_filters = sorted(k for k, v in {
            "neighborhood": neighborhood, "prop_type": prop_type, "use_code": use_code,
            "mv_min": mv_min, "mv_max": mv_max, "etr_min": etr_min, "etr_max": etr_max,
            "bldg_min": bldg_min, "bldg_max": bldg_max, "land_min": land_min, "land_max": land_max,
            "yr_min": yr_min, "yr_max": yr_max, "large_value_jump": large_value_jump,
            "homestead": homestead, "verified_only": verified_only, "delinquent_only": delinquent_only,
        }.items() if v)
        print(f"PX-20260828-16 count query degraded: county={county_code} tax_year={tax_year} "
              f"active_filters={active_filters} needs_metrics_join={needs_metrics} "
              f"exc_type={type(exc).__name__} exc_msg={exc}", flush=True)
        sentry_sdk.capture_exception(exc)
        sentry_sdk.flush(timeout=2)
        # Degrade, don't fail: the data query above already succeeded and
        # `rows` holds real, correct results for this page. total/total_pages
        # become None -- search.html (both the results-count text and the
        # CSV exporter's page-walking loop) must check count_unavailable and
        # fall back to has_more rather than treating None as a number.
        count_unavailable = True
        total = None
        total_pages = None

    results = []
    for r in rows:
        # pty.assessed_value added to the SELECT above (July 2026 fix) so
        # this reflects the same per-record AV>MV anomaly check
        # _row_confidence() now applies everywhere else -- previously this
        # endpoint only had market_value on hand, so a certified-tier row
        # could never be demoted here even when it should have been.
        confidence = _row_confidence(r["data_source"], r.get("assessed_value"), r["market_value"])
        result = {
            "geo_id": r["geo_id"],
            "situs_address": r["situs_address"],
            "neighborhood_cd": r["neighborhood_cd"],
            "prop_type": r["prop_type_label"],
            "market_value": float(r["market_value"]) if r["market_value"] is not None else None,
            "tax_year": r["tax_year"],
            "confidence": confidence,
        }
        if delinquent_only:
            result["total_due"] = float(r["total_due"]) if r.get("total_due") is not None else None
            result["first_delinquent_yr"] = r.get("first_delinquent_yr")
        results.append(result)

    return jsonify({
        "ok": True,
        "results": results,
        "total": total,
        "page": page,
        "page_size": SEARCH_FILTER_PAGE_SIZE,
        "total_pages": total_pages,
        "has_more": has_more,
        "count_unavailable": count_unavailable,
    })


@app.route("/<county_slug>/api/estimate_acq/<geo_id>")
@limiter.limit(_LIMIT_HEAVY)
def api_estimate_acq(geo_id):
    """
    Post-acquisition tax estimator API (Task 1).
    Query params:
      price          int    purchase price (required, no commas)
      buyer          str    'non_owner_occupant' (default) | 'owner_occupant'
      rate_mode      str    'certified' (default) | 'projected'
      market_growth  float  optional override for the annual appreciation
                             assumption, as a PERCENT (e.g. "3.5" = 3.5%/yr) --
                             added July 2026 for the property page's Custom
                             assumptions panel (Diego's "Property Page Polish
                             Round" item 2). When omitted, falls back to the
                             existing per-parcel CAGR computed below (unchanged
                             default behaviour -- this param is additive only).
                             Only the market growth rate is overridable; the
                             statutory exemption/cap constants below are not
                             accepted as params on purpose (see item 2 notes).
    """
    price_raw    = request.args.get("price", "").strip().replace(",", "").replace("$", "")
    buyer_status = request.args.get("buyer", "non_owner_occupant").strip()
    rate_mode    = request.args.get("rate_mode", "certified").strip()
    market_growth_raw = request.args.get("market_growth", "").strip()

    if buyer_status not in ("non_owner_occupant", "owner_occupant"):
        buyer_status = "non_owner_occupant"
    if rate_mode not in ("certified", "projected"):
        rate_mode = "certified"

    if not price_raw or not re.fullmatch(r"\d+", price_raw):
        return jsonify({"ok": False, "error": "price must be a positive integer (no commas or $)"})

    purchase_price = int(price_raw)
    if purchase_price <= 0:
        return jsonify({"ok": False, "error": "price must be positive"})

    # PX-20260828-12 fix: parcel/parcel_tax_year fetches here lacked
    # county_code (composite-PK tables, geo_id alone not globally unique --
    # see compare_parcels()'s identical fix for the full audit-wide pattern).
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, g.county_code), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    current_yr_row = query("""
        SELECT market_value, assessed_value, taxable_value, hs_cap_loss, exemption_codes
        FROM   parcel_tax_year
        WHERE  geo_id = %s AND tax_year = 2025 AND county_code = %s
    """, (geo_id, g.county_code), one=True)

    if not current_yr_row or not current_yr_row.get("market_value"):
        return jsonify({"ok": False, "error": "No 2025 certified market value for this parcel"})

    # DALLAS-GATE-2 Part 2: verify_index_coverage.py flagged this WHERE
    # clause (tax_billing_entity filtered by geo_id + tax_year alone --
    # county_code-leading composite-PK table per migrate_county_
    # partitioning.py's TABLE_SPECS, same shape as county_tax_rate joined
    # below). county_code added as an ADDITIONAL predicate only.
    entity_detail = query("""
        SELECT tbe.entity_code, ctr.entity_name, ctr.rate, tbe.amount_due
        FROM   tax_billing_entity tbe
        LEFT JOIN county_tax_rate ctr
               ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
              AND ctr.county_code = tbe.county_code
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025 AND tbe.county_code = %s
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id, g.county_code))

    if not entity_detail:
        return jsonify({"ok": False, "error": "No 2025 entity billing data for this parcel"})

    # Per-entity rate history (for the projected-rate scenario)
    codes = tuple({e["entity_code"] for e in entity_detail})
    entity_rate_history = {}
    if codes:
        for r in query(
            "SELECT entity_code, tax_year, rate FROM county_tax_rate "
            "WHERE entity_code IN %s AND county_code = %s AND tax_year >= 2016 "
            "ORDER BY tax_year",
            (codes, g.county_code),
        ):
            entity_rate_history.setdefault(r["entity_code"], {})[r["tax_year"]] = (
                float(r["rate"]) if r["rate"] is not None else None
            )

    # Parcel market-growth assumption — mirror the main 6-year projection's CAGR
    # (Task 5): earliest → latest INCLUDING the 2026 preliminary, and allow the
    # rate to be negative (decline) instead of flooring at 0%. This is why the
    # multi-year projection was staying flat; now it compounds the same CAGR the
    # main projection uses. Clamped to a sane band.
    mkt_hist = query("""
        SELECT tax_year, market_value FROM parcel_tax_year
        WHERE geo_id = %s AND county_code = %s AND market_value IS NOT NULL AND tax_year <= 2026
        ORDER BY tax_year
    """, (geo_id, g.county_code))
    market_growth = None
    pts = [(r["tax_year"], float(r["market_value"])) for r in mkt_hist if r["market_value"]]
    if len(pts) >= 2 and pts[0][1] > 0:
        span = pts[-1][0] - pts[0][0]
        if span > 0:
            cagr = (pts[-1][1] / pts[0][1]) ** (1.0 / span) - 1.0
            market_growth = max(-0.05, min(0.12, cagr))   # allow decline; mirror main projection

    # Custom-assumptions override (July 2026, Task Brief item 2): only the
    # market growth rate is user-editable -- see docstring above. Silently
    # ignored (falls back to the computed default) if it isn't a parseable
    # number, rather than erroring the whole estimate over a bad override.
    # estimate_post_acquisition() clamps to [-5%, 12%] internally regardless
    # (tax_logic/texas.py), so no separate clamp is needed here.
    if market_growth_raw:
        try:
            market_growth = float(market_growth_raw) / 100.0
        except ValueError:
            pass

    result = _tx_estimate(
        dict(parcel),
        dict(current_yr_row),
        [dict(e) for e in entity_detail],
        purchase_price,
        buyer_status,
        rate_mode=rate_mode,
        entity_rate_history=entity_rate_history,
        market_growth=market_growth,
    )
    result["ok"] = True

    # ── PID / billing-only pass-through ──────────────────────────────────────
    # Entity codes in 2025 billing but absent from county_tax_rate (PIDs, WCIDs,
    # special districts) carry rate=NULL in the LEFT JOIN and are silently skipped
    # by texas.py.  Pass them through at prior-year billing amount — the only
    # available basis.  See ENTITY_CODE_AUDIT.md for the full finding and impact.
    billing_only = [
        e for e in entity_detail
        if e.get("amount_due") and not e.get("rate")
    ]
    if billing_only:
        pid_passthrough = round(sum(float(e["amount_due"]) for e in billing_only), 2)
        result["pid_passthrough"]          = pid_passthrough
        result["pid_entity_codes"]         = [e["entity_code"] for e in billing_only]
        result["pid_entity_names"]         = [
            e.get("entity_name") or e["entity_code"] for e in billing_only
        ]
        result["estimated_total_incl_pid"] = round(
            result["estimated_total_tax"] + pid_passthrough, 2
        )
        # Corrected delta: buyer estimate (rate + PID) vs seller actual (already
        # includes PID via seller_total_tax sum in texas.py)
        result["delta_incl_pid"] = round(
            result["estimated_total_incl_pid"] - result["seller_total_tax"], 2
        )
    else:
        result["pid_passthrough"]          = 0.0
        result["pid_entity_codes"]         = []
        result["pid_entity_names"]         = []
        result["estimated_total_incl_pid"] = result["estimated_total_tax"]
        result["delta_incl_pid"]           = result["delta"]

    # Convert any Decimal/non-serialisable types to float/int
    def _clean(v):
        if hasattr(v, "__float__"):
            return float(v)
        return v

    result["entity_breakdown"] = [
        {k: _clean(val) for k, val in row.items()}
        for row in result["entity_breakdown"]
    ]
    return jsonify(result)



def _log_typeahead_timing(endpoint_label, t0, q, result_count):
    """
    PX-20260829-04 Task 1: end-to-end timing breakdown for one typeahead
    request -- "log time spent in the DB call vs. everything else", per the
    brief's own wording. Reads g._diag_db_calls (populated by every query()
    call this request made, see that function's own PX-20260829-04 comment)
    and prints ONE structured line: total wall-clock time for the whole
    request, DB time (connect + exec, summed across every query() call this
    request happened to make), the resulting "everything else" remainder
    (Python-side loop/JSON overhead, Flask-Limiter's own bookkeeping,
    request/response plumbing), and -- critically for diagnosing the
    suspected serial-per-county-query multiplier -- the actual NUMBER of
    separate query() calls (each its own physical DB connection, since
    get_db() does not pool) this one request made.

    print()'d rather than routed through logging/Sentry: matches this
    codebase's existing DIAG convention (see BILLING-DIAG-3 through -8,
    api_billing's own breadcrumb style) for a lightweight, always-on,
    grep-able diagnostic that needs no separate log-level configuration to
    show up in Render's log stream. Safe to leave in past this incident --
    each line is ~150 bytes, and every real production typeahead keystroke
    is already rate-limited to 60/minute (_LIMIT_TYPEAHEAD) per client.
    """
    total_ms = round((time.perf_counter() - t0) * 1000, 2)
    calls = g.get("_diag_db_calls", []) if has_request_context() else []
    db_ms = round(sum(c["connect_ms"] + c["exec_ms"] for c in calls), 2)
    connect_ms = round(sum(c["connect_ms"] for c in calls), 2)
    other_ms = round(total_ms - db_ms, 2)
    print(
        f"  [DIAG PX-20260829-04] {endpoint_label} q={q!r} results={result_count} "
        f"total_ms={total_ms} db_ms={db_ms} (connect_ms={connect_ms}) "
        f"other_ms={other_ms} db_call_count={len(calls)}"
    )


@app.route("/<county_slug>/api/address_search")
@limiter.limit(_LIMIT_TYPEAHEAD)
def api_address_search():
    """
    Address typeahead API (Task 2; matching rebuilt per Cowork brief "Search
    overhaul — Phase 2 go-ahead", July 2026, D2/D3).
    Returns up to 8 matching parcels for a partial address OR account-number
    query, via the two shared functions above (resolve_exact_parcel() /
    search_parcels_by_address()) — the exact same functions the "/" route's
    full-results submit handler uses, so a query resolves identically
    whether it came from this typeahead or a full-page Enter submit.
    Query params:
      q   str   partial address string, or an account number (min 3 chars)

    PX-20260828-02 (Diego's addition): every result also carries county_name
    so parcel-typeahead.js can render a visible "(Travis County)"/"(Dallas
    County)" tag on every result. This is a visibility aid, not a new
    scoping mechanism -- every result from a single call to this anchored
    (/<county_slug>/...) endpoint is already guaranteed to be from one
    county (resolve_exact_parcel()/search_parcels_by_address() both scope
    to g.county_code when no explicit county_code is passed, which is the
    case here); the point is that if COUNTY_BASE or the URL built from it
    is ever wrong again, a user sees the mismatch directly instead of it
    being silently wrong.

    PX-20260828-03 Task 2: the address-match branch below now reads
    county_name straight off each row (search_parcels_by_address() stamps
    it per-row itself, alongside county_slug, as of this same task) rather
    than this endpoint deriving its own separate copy from g.county_code --
    one source for the value, not two that could silently drift apart. The
    exact-match branch still derives its own local `county_name` below,
    since resolve_exact_parcel()'s single-dict return has no county_slug/
    county_name of its own to read (it isn't a multi-county-aware result
    list the way search_parcels_by_address()'s rows are).

    PX-20260829-01: every result also now carries county_slug -- the live
    bug this brief fixes is that parcel-typeahead.js's click handler
    navigated using the PAGE's county (global.COUNTY_BASE) rather than the
    RESULT's county. On an anchored page those are normally the same
    county, but this endpoint's exact-match branch (is_account_number_query
    hit) can resolve to a DIFFERENT county than g.county_code entirely --
    resolve_exact_parcel(q) with no explicit county_code falls back to
    g.county_code first, but on a miss there it is NOT re-tried against
    other counties from this anchored call (only the neutral landing
    endpoint's own county_code=None path loops all live counties) -- so in
    practice, on THIS endpoint, a hit's county really is always
    g.county_code. Stamped explicitly anyway (both branches, not just
    derived implicitly by the caller) so the JS never has to guess: county
    on this endpoint's exact-match branch is unconditionally g.county_slug
    (the one county this anchored route ever queries); the address-match
    branch reads whatever search_parcels_by_address() already put on the
    row (same value, since that function scopes to g.county_code too when
    called from here), falling back to g.county_slug only for defense
    against a row that somehow lacks it.
    """
    _t0 = time.perf_counter()
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "results": []})

    county_name = COUNTY_PROFILES.get(g.county_code, COUNTY_PROFILES["TRAVIS"])["county_name"]

    # D2 item 5 / D3: a numeric account number resolves here too now (it
    # didn't before — this endpoint never did geo_id/prop_id matching,
    # only address text), so typing an account number into any of the four
    # search boxes shows a typeahead suggestion, not just a blank dropdown
    # until Enter is pressed. PX-20260827-04: is_account_number_query() (
    # renamed from is_numeric_account_query(), see search_logic.py) also
    # now recognizes Dallas's real alphanumeric ACCOUNT_NUM shape, not
    # just all-digit strings -- this was the one confirmed gate preventing
    # 205,049 real Dallas accounts from being found via typeahead.
    if search_logic.is_account_number_query(q):
        exact = resolve_exact_parcel(q)
        if exact:
            _log_typeahead_timing("api_address_search[exact]", _t0, q, 1)
            return jsonify({"ok": True, "results": [{
                "geo_id":      exact["geo_id"],
                "address":     exact.get("situs_address") or "",
                "owner":       exact.get("owner_name") or "",
                "county_name": county_name,
                "county_slug": g.county_slug,
            }]})
        # Falls through to address-text matching below on a numeric miss —
        # e.g. a 5-digit zip typed alone shouldn't just dead-end here.

    rows = search_parcels_by_address(q, limit=8)
    results = [
        {
            "geo_id":      r["geo_id"],
            "address":     r.get("situs_address") or "",
            "owner":       r.get("owner_name") or "",
            "county_name": r.get("county_name", county_name),
            "county_slug": r.get("county_slug", g.county_slug),
        }
        for r in rows
    ]
    _log_typeahead_timing("api_address_search[address]", _t0, q, len(results))
    return jsonify({"ok": True, "results": results})


@app.route("/api/address_search")
@limiter.limit(_LIMIT_TYPEAHEAD)
def api_address_search_landing():
    """
    PX-20260828-06: the neutral, bare-path sibling of api_address_search()
    above -- matching the index()/home() anchored/neutral pattern already
    established at line ~2375 (anchored route defined first, bare route
    right after, both delegating to the same shared underlying function(s)
    with county_code=None for the neutral case).

    Exists because '/api/address_search' had no real route of its own --
    it only ever existed as a _LEGACY_REDIRECT_ROUTES 301-to-Travis stub
    (that entry is removed in this same change; a real route and a
    redirect rule registered at the same literal path would conflict at
    Flask registration time regardless). Reuses resolve_exact_parcel()/
    search_parcels_by_address() with county_code=None -- the exact same
    cross-county loop _resolve_quick_search()/_home_search_response()
    already call successfully for the neutral home page's own full-page
    Enter-to-search flow -- so a bare, county-less typeahead request
    resolves identically to that already-correct path, not a second,
    separately-written cross-county mechanism.

    Same q/results contract as the anchored endpoint. One structural
    difference: this route has no g.county_code at all (it is never
    reached via a <county_slug> URL segment), so county_name can't be
    derived from a single request-wide value the way the anchored
    endpoint's exact-match branch does. Instead:
      - the address-match branch reads county_name straight off each row
        (search_parcels_by_address()'s county_code=None loop stamps a
        real, correct per-row county_name on every result -- see that
        function's own PX-20260828-03 comment -- since a single neutral
        call can span multiple counties, there is no single value to
        fall back to here, unlike the anchored endpoint);
      - the exact-match branch derives county_name from the resolved
        parcel's OWN county_code column instead -- resolve_exact_parcel()
        with county_code=None still runs a real `SELECT * FROM parcel
        WHERE ...`, so that field is always present on a hit.

    PX-20260829-01: county_slug is stamped the same way, on both branches,
    for the same reason as the anchored endpoint's own PX-20260829-01
    comment above -- this is in fact the MORE important of the two
    endpoints to fix, since it's the one where "page county" and "result
    county" are guaranteed to differ whenever this neutral endpoint's own
    cross-county loop finds a match outside whatever county the visiting
    page happens to be near (the live bug report's own repro: /about ->
    "2626 cartwright" -> a Dallas result). The address-match branch reads
    county_slug straight off each row, same as county_name (search_
    parcels_by_address()'s county_code=None loop already stamps both). The
    exact-match branch has no such row to read from -- resolve_exact_parcel()
    returns a bare `parcel` table dict, which has county_code but no
    county_slug column of its own -- so county_slug is derived here via the
    same COUNTY_SLUGS reverse-lookup search_parcels_by_address() itself
    uses internally, rather than inventing a second lookup helper for one
    call site.
    """
    _t0 = time.perf_counter()
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "results": []})

    if search_logic.is_account_number_query(q):
        exact = resolve_exact_parcel(q, county_code=None)
        if exact:
            exact_county_code = exact.get("county_code")
            exact_county_name = COUNTY_PROFILES.get(
                exact_county_code, COUNTY_PROFILES["TRAVIS"]
            )["county_name"]
            # PX-20260829-01: same reverse-lookup search_parcels_by_address()
            # already does internally -- DEFAULT_COUNTY_SLUG here is only
            # ever a defensive fallback for a county_code with no registered
            # slug at all (shouldn't happen for a row that came out of the
            # `parcel` table, since every row there has a real, registered
            # county_code by construction), not a "we don't know" case that
            # would silently point this specific result at Travis.
            exact_county_slug = next(
                (slug for slug, code in COUNTY_SLUGS.items() if code == exact_county_code),
                DEFAULT_COUNTY_SLUG,
            )
            _log_typeahead_timing("api_address_search_landing[exact]", _t0, q, 1)
            return jsonify({"ok": True, "results": [{
                "geo_id":      exact["geo_id"],
                "address":     exact.get("situs_address") or "",
                "owner":       exact.get("owner_name") or "",
                "county_name": exact_county_name,
                "county_slug": exact_county_slug,
            }]})
        # Falls through to address-text matching below on a numeric miss,
        # same as the anchored endpoint.

    rows = search_parcels_by_address(q, limit=8, county_code=None)
    results = [
        {
            "geo_id":      r["geo_id"],
            "address":     r.get("situs_address") or "",
            "owner":       r.get("owner_name") or "",
            "county_name": r.get("county_name"),
            "county_slug": r.get("county_slug"),
        }
        for r in rows
    ]
    # PX-20260829-04 Task 1: this is the branch under live suspicion -- the
    # cross-county loop inside search_parcels_by_address() (county_code=None
    # above) calls itself once per live county, SERIALLY, and each of those
    # per-county calls can itself issue up to (len(tokens)-1) separate
    # query() calls (address_match_attempts()'s progressive token-drop
    # retry, search_logic.py) before giving up on that county -- every one
    # of which opens its own brand-new DB connection (get_db() has no
    # pooling). db_call_count in the printed line below is the direct,
    # measured count of how many of those this one request actually made.
    _log_typeahead_timing("api_address_search_landing[address,CROSS-COUNTY]", _t0, q, len(results))
    return jsonify({"ok": True, "results": results})


@app.route("/<county_slug>/api/peer_benchmark_local/<geo_id>")
@limiter.limit(_LIMIT_HEAVY)
def api_peer_benchmark_local(geo_id):
    """
    Neighborhood + type + size-band peer benchmark (Task 3).
    Peer set: same neighborhood_cd, same state_cd1 prefix, 2025 MV within ±50%.
    Returns peer count, median MV, p25/p75 MV, median total_tax, this parcel's rank.
    """
    # DALLAS-GATE-2 Part 2 (peer-match-related dynamic WHERE construction --
    # same risk shape as the original incident): threaded through the
    # candidates CTE and both LEFT JOINs below. g.county_code, same
    # per-request value every other DALLAS-GATE fix in this file reads.
    county_code = g.county_code

    # PX-20260828-12 fix: this subject-parcel fetch and its market-value
    # lookup lacked county_code, even though the candidates CTE below (the
    # DALLAS-GATE-2 Part 2 fix) already threads it through correctly -- same
    # audit-wide gap as compare_parcels()/api_estimate_acq()/
    # api_peer_benchmark_sf()/api_parcel_entities()/export_due_diligence_pdf().
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, county_code), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    mv_row = query("""
        SELECT market_value FROM parcel_tax_year WHERE geo_id = %s AND tax_year = 2025 AND county_code = %s
    """, (geo_id, county_code), one=True)

    neighborhood = (parcel.get("neighborhood_cd") or "").strip()
    state_cd1    = (parcel.get("state_cd1") or "").strip()[:1]
    this_mv      = float(mv_row["market_value"]) if mv_row and mv_row.get("market_value") else None

    if not neighborhood or not this_mv:
        return jsonify({"ok": False, "error":
            "Peer benchmark requires neighborhood code and 2025 market value"})

    mv_lo = this_mv * 0.50
    mv_hi = this_mv * 1.50

    # NULL-safety fix (July 2026): `LEFT(p.state_cd1, 1) = %(sc1)s` silently
    # drops any candidate peer row with NULL state_cd1 (new-construction
    # parcels newer than the 2021-2024 AJR extract) via the same
    # NULL-propagation mechanism as CANONICAL_PARCEL_EXCL's bug — see
    # parcel_filters.py's peer_state_cd1_match_sql() docstring for the full
    # reasoning. state_cd1 above is already Python-side NULL-safe
    # (`or ""` coalesces None); this makes the SQL-side candidate column
    # NULL-safe too, applied to both this query and its fallback below.
    _peer_match = peer_state_cd1_match_sql()

    # Peer set: same neighborhood, same state_cd1 prefix, MV band ±50%
    # tb.total_tax is 0.00 (not NULL) for ~93% of 2025 tax_billing rows at the
    # source (see KNOWN_LIMITATIONS.md) — entity_tax_sum is the per-geo_id
    # SUM(amount_due) from tax_billing_entity, used as a fallback below so a
    # real, verified figure isn't silently dropped for the median/percentile.
    #
    # Self-contamination fix (July 2026, per Fable review finding, wave 2):
    # this query never excluded the subject parcel itself. The WHERE clause
    # (same neighborhood, same state_cd1 prefix, MV within ±50% of this_mv)
    # is trivially satisfied by the subject's own row -- it's centered on
    # this_mv by construction -- so the subject was always its own peer,
    # skewing median/percentile stats toward its own value on small peer
    # sets (confirmed live: "Peer Median Tax" exactly equaling the subject's
    # own tax figure on a peer set of only ~5 properties). AND p.geo_id !=
    # %(geo_id)s added to both this query and its fallback below.
    # Task M6-PEER-QUERY-PERF (Aug 2026): both this query and the fallback
    # below used to LEFT JOIN a subquery that GROUPed BY geo_id over the
    # ENTIRE tax_billing_entity table for tax_year=2025 (~2.1M rows scanned
    # county-wide, confirmed via production EXPLAIN ANALYZE — 13.4s, past
    # the 8s statement_timeout, the direct cause of Sentry PYTHON-FLASK-5),
    # then merge-joined that whole-county aggregate against a candidate set
    # of only ~200-300 rows (narrowed by neighborhood_cd/state_cd1/MV band).
    # All the aggregation work for the whole county was done to use a few
    # hundred rows of it.
    #
    # Fixed (Option A per the brief — CTE the candidates first, then
    # aggregate only against them): `candidates` is computed once, and the
    # tax_billing_entity subquery is scoped with
    # `geo_id IN (SELECT geo_id FROM candidates)`, so the planner aggregates
    # only the rows belonging to this request's actual candidate set instead
    # of the whole table. tax_billing_entity's existing PRIMARY KEY is
    # (geo_id, tax_year, entity_code) — already geo_id-first, so this scoped
    # form can drive off that index directly; no new index needed (Option B
    # not applied — see this task's final report for the reasoning and the
    # explicit disclosure that this could not be timed against a live DB in
    # this sandbox).
    #
    # Logic is unchanged: identical WHERE conditions (NULL-safe state_cd1
    # match, MV band, AJR exclusion, self-contamination exclusion), identical
    # output columns, identical ORDER BY — this is a performance-only
    # rewrite, verified for equivalence by inspection (see report), not a
    # peer-set/logic change.
    peers = query(f"""
        WITH candidates AS (
            SELECT p.geo_id, p.county_code, pty.market_value, pty.assessed_value
            FROM   parcel p
            JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
                                       AND pty.county_code = p.county_code
            WHERE  p.neighborhood_cd = %(nb)s
              AND  {_peer_match}
              AND  pty.market_value BETWEEN %(lo)s AND %(hi)s
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  p.geo_id != %(geo_id)s
              AND  p.county_code = %(county_code)s
              AND  pty.market_value > 0
        )
        SELECT
            c.geo_id,
            c.market_value,
            c.assessed_value,
            tb.total_tax,
            tbe.entity_tax_sum
        FROM   candidates c
        LEFT JOIN tax_billing  tb  ON tb.geo_id  = c.geo_id AND tb.tax_year  = 2025
                                   AND tb.county_code = c.county_code
        LEFT JOIN (
            SELECT geo_id, SUM(amount_due) AS entity_tax_sum
            FROM   tax_billing_entity
            WHERE  tax_year = 2025
              AND  county_code = %(county_code)s
              AND  geo_id IN (SELECT geo_id FROM candidates)
            GROUP  BY geo_id
        ) tbe ON tbe.geo_id = c.geo_id
        ORDER  BY c.market_value
    """, {"nb": neighborhood, "sc1": state_cd1, "lo": mv_lo, "hi": mv_hi, "geo_id": geo_id, "county_code": county_code})

    n = len(peers)
    if n < 3:
        # Fallback: relax to neighborhood + type only, drop MV band. Same
        # Task M6-PEER-QUERY-PERF rewrite applied here — this branch can run
        # against an even LARGER candidate set than the primary query (no MV
        # band restriction), so it needed the identical fix, not just the
        # primary query above.
        peers = query(f"""
            WITH candidates AS (
                SELECT p.geo_id, p.county_code, pty.market_value, pty.assessed_value
                FROM   parcel p
                JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
                                           AND pty.county_code = p.county_code
                WHERE  p.neighborhood_cd = %(nb)s
                  AND  {_peer_match}
                  AND  p.geo_id NOT LIKE 'AJR%%'
                  AND  p.geo_id != %(geo_id)s
                  AND  p.county_code = %(county_code)s
                  AND  pty.market_value > 0
            )
            SELECT c.geo_id, c.market_value, c.assessed_value,
                   tb.total_tax, tbe.entity_tax_sum
            FROM   candidates c
            LEFT JOIN tax_billing  tb  ON tb.geo_id  = c.geo_id AND tb.tax_year  = 2025
                                       AND tb.county_code = c.county_code
            LEFT JOIN (
                SELECT geo_id, SUM(amount_due) AS entity_tax_sum
                FROM   tax_billing_entity
                WHERE  tax_year = 2025
                  AND  county_code = %(county_code)s
                  AND  geo_id IN (SELECT geo_id FROM candidates)
                GROUP  BY geo_id
            ) tbe ON tbe.geo_id = c.geo_id
            ORDER  BY c.market_value
        """, {"nb": neighborhood, "sc1": state_cd1, "geo_id": geo_id, "county_code": county_code})
        n = len(peers)
        band_note = "Size band relaxed (neighborhood + type only — fewer than 3 ±50% MV peers)"
    else:
        band_note = f"Neighborhood {neighborhood}, {state_cd1}-type, MV within ±50% of this parcel"

    if n == 0:
        return jsonify({"ok": False, "error": "No peers found in this neighborhood + property type"})

    mvs   = sorted([float(r["market_value"]) for r in peers if r.get("market_value")])
    avs   = sorted([float(r["assessed_value"]) for r in peers if r.get("assessed_value")])

    # Effective tax per peer: tax_billing.total_tax when it's a real (nonzero)
    # figure; otherwise fall back to the tax_billing_entity sum (also real,
    # also verified — same pattern as app.py's single-property `current`
    # fallback). A peer with neither (no total_tax AND no entity billing) has
    # genuinely no 2025 billing data and is excluded from the stat — that's
    # different from "billed but the aggregate field reads 0.00", and we don't
    # want to conflate the two.
    def _effective_tax(r):
        if r.get("total_tax"):
            return float(r["total_tax"])
        if r.get("entity_tax_sum"):
            return float(r["entity_tax_sum"])
        return None

    tax_values   = [_effective_tax(r) for r in peers]
    taxes        = sorted([t for t in tax_values if t is not None])
    peer_tax_n   = len(taxes)

    def pct(lst, p):
        if not lst: return None
        i = (len(lst) - 1) * p / 100
        lo_, hi_ = int(i), min(int(i) + 1, len(lst) - 1)
        return round(lst[lo_] + (lst[hi_] - lst[lo_]) * (i - lo_))

    def median(lst):
        return pct(lst, 50)

    # Where does this parcel rank by MV among peers?
    mv_rank = sum(1 for v in mvs if v < this_mv) + 1
    mv_pct  = round(mv_rank / n * 100) if n else None

    return jsonify({
        "ok":           True,
        "geo_id":       geo_id,
        "peer_count":   n,
        "band_note":    band_note,
        "this_mv":      round(this_mv),
        "peer_mv": {
            "p25":    pct(mvs, 25),
            "median": median(mvs),
            "p75":    pct(mvs, 75),
        },
        "peer_av": {
            "p25":    pct(avs, 25),
            "median": median(avs),
            "p75":    pct(avs, 75),
        },
        "peer_tax": {
            "p25":    pct(taxes, 25),
            "median": median(taxes),
            "p75":    pct(taxes, 75),
        },
        # Sample-size disclosure: peer_tax is built from fewer peers than
        # peer_mv/peer_av when some peers genuinely have no 2025 billing data
        # at all (excluded, not zero-filled). Surfaced in the UI as "(n of N)"
        # next to the Peer Median Tax figure so the stat isn't presented as if
        # it covers the same peer set as MV/AV.
        "peer_tax_sample_size": peer_tax_n,
        "peer_tax_total_count": n,
        "this_mv_pct_rank": mv_pct,
    })


@app.route("/<county_slug>/api/peer_benchmark_sf/<geo_id>")
@limiter.limit(_LIMIT_HEAVY)
def api_peer_benchmark_sf(geo_id):
    """
    Per-SF peer benchmark (Task B).
    Peer set: same neighborhood_cd + state_cd1 prefix + living_area_sqft size band.
    Size band starts at ±40%; relaxes to ±60% then unconstrained if fewer than 5 peers.
    Returns assessed $/SF and market $/SF percentiles for this parcel vs peers.
    Parcels with null/zero living_area_sqft return ok=False with error='no_sf_basis'.
    """
    # DALLAS-GATE-2 Part 2 (peer-match-related dynamic WHERE construction --
    # same risk shape as the original incident, same function family as
    # api_peer_benchmark_local() above): threaded through the peer query's
    # WHERE clause below. g.county_code, same per-request value every other
    # DALLAS-GATE fix in this file reads.
    county_code = g.county_code

    # PX-20260828-12 fix: this subject-parcel fetch (and the parcel_data
    # JOIN's ON clause) lacked county_code -- same audit-wide gap as
    # api_peer_benchmark_local()/compare_parcels()/api_estimate_acq().
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, county_code), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    parcel_data = query("""
        SELECT p.living_area_sqft,
               p.gross_building_area_sqft,
               pty.market_value,
               pty.assessed_value
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
                                   AND pty.county_code = p.county_code
        WHERE  p.geo_id = %s AND p.county_code = %s
    """, (geo_id, county_code), one=True)

    if not parcel_data:
        return jsonify({"ok": False, "error": "No 2025 data for this parcel"})

    sqft       = float(parcel_data["living_area_sqft"]) if parcel_data.get("living_area_sqft") else None
    gross_sqft = float(parcel_data["gross_building_area_sqft"]) if parcel_data.get("gross_building_area_sqft") else None
    this_mv = float(parcel_data["market_value"])     if parcel_data.get("market_value")     else None
    this_av = float(parcel_data["assessed_value"])   if parcel_data.get("assessed_value")   else None

    if not sqft or sqft <= 0:
        return jsonify({
            "ok": False, "error": "no_sf_basis",
            "message": "No living area SF for this parcel (vacant land, exempt-only, or loader not run)"
        })

    # Gross Building Area $/SF (Task 6) — provisional basis (total improvement area)
    this_market_psf_gross   = round(this_mv / gross_sqft, 2) if (this_mv and gross_sqft) else None
    this_assessed_psf_gross = round(this_av / gross_sqft, 2) if (this_av and gross_sqft) else None

    neighborhood = (parcel.get("neighborhood_cd") or "").strip()
    state_cd1    = (parcel.get("state_cd1") or "").strip()[:1]

    if not neighborhood:
        return jsonify({"ok": False, "error": "No neighborhood code for this parcel"})

    # NULL-safety fix (July 2026) -- see api_peer_benchmark_local()'s
    # identical comment above; parcel_filters.py's peer_state_cd1_match_sql()
    # docstring has the full reasoning.
    _peer_match = peer_state_cd1_match_sql()

    this_market_psf   = round(this_mv / sqft, 2) if this_mv   else None
    this_assessed_psf = round(this_av / sqft, 2) if this_av   else None

    # Progressively relax size band until ≥ 5 peers
    band_attempts = [0.40, 0.60, None]   # ±40%, ±60%, unconstrained
    peers = []
    band_note = ""

    # Self-contamination fix (July 2026, per Fable review finding, wave 2):
    # found while fixing the same bug in api_peer_benchmark_local() just above
    # -- this query has the identical gap. The subject parcel trivially
    # satisfies its own neighborhood/type/size-band filters (the band is
    # centered on its own sqft), so it was always included in its own peer
    # set here too. AND p.geo_id != %(geo_id)s added; geo_id added to every
    # params dict below.
    for band in band_attempts:
        if band is not None:
            sqft_lo = sqft * (1.0 - band)
            sqft_hi = sqft * (1.0 + band)
            size_clause = "AND p.living_area_sqft BETWEEN %(sqft_lo)s AND %(sqft_hi)s"
            params = {
                "nb": neighborhood, "sc1": state_cd1,
                "sqft_lo": sqft_lo, "sqft_hi": sqft_hi, "geo_id": geo_id,
                "county_code": county_code,
            }
        else:
            size_clause = ""
            params = {"nb": neighborhood, "sc1": state_cd1, "geo_id": geo_id, "county_code": county_code}

        peers = query(f"""
            SELECT
                p.geo_id,
                pty.market_value,
                pty.assessed_value,
                CAST(p.living_area_sqft AS FLOAT)                               AS sqft,
                CAST(p.gross_building_area_sqft AS FLOAT)                       AS gross_sqft,
                CAST(pty.market_value   AS FLOAT) / p.living_area_sqft          AS market_psf,
                CAST(pty.assessed_value AS FLOAT) / p.living_area_sqft          AS assessed_psf,
                CASE WHEN p.gross_building_area_sqft > 0
                     THEN CAST(pty.market_value   AS FLOAT) / p.gross_building_area_sqft END AS market_psf_gross,
                CASE WHEN p.gross_building_area_sqft > 0
                     THEN CAST(pty.assessed_value AS FLOAT) / p.gross_building_area_sqft END AS assessed_psf_gross
            FROM   parcel p
            JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
                                       AND pty.county_code = p.county_code
            WHERE  p.neighborhood_cd  = %(nb)s
              AND  {_peer_match}
              AND  p.living_area_sqft > 0
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  p.geo_id != %(geo_id)s
              AND  p.county_code = %(county_code)s
              AND  pty.market_value   > 0
              AND  pty.assessed_value > 0
              {size_clause}
            ORDER  BY p.living_area_sqft
        """, params)

        n = len(peers)
        if n >= 5:
            if band is not None:
                band_note = (
                    f"Neighborhood {neighborhood}, {state_cd1}-type, "
                    f"SF within ±{int(band * 100)}% of {sqft:,.0f} SF"
                )
            else:
                band_note = (
                    f"Neighborhood {neighborhood}, {state_cd1}-type, "
                    f"all SF sizes (size band relaxed — fewer than 5 peers in ±60% band)"
                )
            break

    n = len(peers)
    if n < 3:
        return jsonify({
            "ok": False,
            "error": "Fewer than 3 SF peers in this neighborhood + property type",
        })

    market_psf_vals   = sorted(float(r["market_psf"])   for r in peers if r.get("market_psf"))
    assessed_psf_vals = sorted(float(r["assessed_psf"]) for r in peers if r.get("assessed_psf"))
    market_psf_gross_vals   = sorted(float(r["market_psf_gross"])   for r in peers if r.get("market_psf_gross"))
    assessed_psf_gross_vals = sorted(float(r["assessed_psf_gross"]) for r in peers if r.get("assessed_psf_gross"))

    def _pct(lst, p):
        if not lst:
            return None
        i = (len(lst) - 1) * p / 100.0
        lo_, hi_ = int(i), min(int(i) + 1, len(lst) - 1)
        return round(lst[lo_] + (lst[hi_] - lst[lo_]) * (i - lo_), 2)

    this_market_psf_rank   = None
    this_assessed_psf_rank = None
    if this_market_psf and market_psf_vals:
        rk = sum(1 for v in market_psf_vals if v < this_market_psf) + 1
        this_market_psf_rank = round(rk / n * 100)
    if this_assessed_psf and assessed_psf_vals:
        rk = sum(1 for v in assessed_psf_vals if v < this_assessed_psf) + 1
        this_assessed_psf_rank = round(rk / n * 100)

    return jsonify({
        "ok":                     True,
        "geo_id":                 geo_id,
        "peer_count":             n,
        "band_note":              band_note,
        "this_sqft":              round(sqft),
        "this_gross_sqft":        round(gross_sqft) if gross_sqft else None,
        "this_market_psf":        this_market_psf,
        "this_assessed_psf":      this_assessed_psf,
        "this_market_psf_rank":   this_market_psf_rank,
        "this_assessed_psf_rank": this_assessed_psf_rank,
        "this_market_psf_gross":   this_market_psf_gross,
        "this_assessed_psf_gross": this_assessed_psf_gross,
        "peer_market_psf": {
            "p25":    _pct(market_psf_vals, 25),
            "median": _pct(market_psf_vals, 50),
            "p75":    _pct(market_psf_vals, 75),
        },
        "peer_assessed_psf": {
            "p25":    _pct(assessed_psf_vals, 25),
            "median": _pct(assessed_psf_vals, 50),
            "p75":    _pct(assessed_psf_vals, 75),
        },
        "peer_market_psf_gross": {
            "p25":    _pct(market_psf_gross_vals, 25),
            "median": _pct(market_psf_gross_vals, 50),
            "p75":    _pct(market_psf_gross_vals, 75),
        },
        "peer_assessed_psf_gross": {
            "p25":    _pct(assessed_psf_gross_vals, 25),
            "median": _pct(assessed_psf_gross_vals, 50),
            "p75":    _pct(assessed_psf_gross_vals, 75),
        },
        "gross_provisional": True,
    })



_NEWS_CACHE = {}     # query string -> {"ts": float, "items": list}
_NEWS_TTL = 3600     # seconds

# Property-type-specific news queries (keyed by the classi_cd-first label).
# NOTE (PX-20260824-01): "Austin" segments are NOT converted -- no
# COUNTY_PROFILES field carries a colloquial city name distinct from
# county_name, so those stay hardcoded/Travis-specific and are flagged in
# the PX-20260824-01 report. The "Travis County" / "TCAD" segments ARE
# profile-derivable and are built per-request from county_profile below.
def _news_queries(county_profile):
    cn = county_profile["county_name"]
    return {
        "homeowner":    f"{cn} homestead exemption OR Austin property tax homeowner OR Austin school tax",
        "Residential":  f"{cn} homestead exemption OR Austin residential property tax",
        "Multi-Family": "Austin multifamily property tax OR Austin apartment market",
        "Commercial":   f"{cn} commercial property tax",
        "Land/Vacant":  f"{cn} property tax {county_profile['cad_abbr']}",
        "Agricultural": f"{cn} agricultural property tax",
    }


def _news_generic(county_profile):
    return f"{county_profile['county_name']} property tax OR {county_profile['cad_name']}"


def _fetch_news(query):
    """Fetch + parse Google News RSS for a query. Returns a list, or None on failure."""
    import urllib.request, urllib.parse
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=en-US&gl=US&ceid=US:en")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Parcelytics/1.0 (news reader)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = []
        for it in root.findall(".//item")[:12]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            source = ""
            src_el = it.find("source")
            if src_el is not None and src_el.text:
                source = src_el.text.strip()
            if source and title.endswith(" - " + source):
                title = title[: -(len(source) + 3)].strip()
            elif " - " in title and not source:
                source = title.rsplit(" - ", 1)[-1].strip()
                title = title.rsplit(" - ", 1)[0].strip()
            try:
                date_iso = parsedate_to_datetime(pub).date().isoformat()
            except Exception:
                date_iso = ""
            if title and link:
                items.append({"title": title, "link": link, "source": source, "date": date_iso})
        # Sort order fix (July 2026, per Fable review, wave 2): items came back
        # in whatever order Google News RSS happened to return them for a given
        # query -- confirmed live: investor-mode items appeared as Apr 21, Jun
        # 22, Mar 09, not sorted by any consistent rule. There was no explicit
        # sort call anywhere in this function, for any query -- "homeowner mode
        # sorts ascending" (Diego's reference behavior) was a coincidence of
        # that specific query's RSS ordering, not a real code difference between
        # modes; every query (homeowner and every property-type tab) shares this
        # exact function. Sorted explicitly, once, here -- ascending by date
        # (oldest first, matching the homeowner-mode ordering used as the
        # reference) -- so every current and future caller gets the same
        # deterministic order instead of depending on Google's per-query RSS
        # quirks. Items with an unparseable date (empty date_iso) sort last
        # regardless of direction, not first.
        items.sort(key=lambda it: (it["date"] == "", it["date"]))
        return items
    except Exception:
        return None


@app.route("/<county_slug>/api/news")
def api_news():
    """Real, property-type-aware Travis County property-tax news.

    ?type=<property_type_label> selects a tailored query (cached per type, not per
    parcel). Falls back to the generic query, then to an honest 'unavailable' —
    never fabricates headlines.
    """
    import time as _time
    county_profile = COUNTY_PROFILES.get(g.county_code, COUNTY_PROFILES["TRAVIS"])
    news_generic = _news_generic(county_profile)
    ptype = (request.args.get("type", "") or "").strip()
    query = _news_queries(county_profile).get(ptype, news_generic)
    now = _time.time()

    def _cached(q):
        c = _NEWS_CACHE.get(q)
        return c["items"] if (c and (now - c["ts"]) < _NEWS_TTL) else None

    items = _cached(query)
    if items is None:
        items = _fetch_news(query)
        if items:
            _NEWS_CACHE[query] = {"ts": now, "items": items}
    # Fall back to the generic query if the tailored one failed or was empty.
    if not items and query != news_generic:
        items = _cached(news_generic) or _fetch_news(news_generic)
        if items:
            _NEWS_CACHE[news_generic] = {"ts": now, "items": items}
            query = news_generic
    if not items:
        return jsonify({"ok": False, "error": "news_unavailable"})
    return jsonify({"ok": True, "items": items, "query_type": ptype or "generic"})


@app.route("/<county_slug>/api/geocode/<geo_id>")
@limiter.limit(_LIMIT_EXTERNAL)
def api_geocode(geo_id):
    """Return {lat, lng} for a parcel — for the satellite map.

    Uses cached parcel.latitude/longitude when present; otherwise geocodes the
    situs address via the free U.S. Census geocoder (no key) and caches the
    result. Returns ok=False (no fabricated coordinates) on any failure.
    """
    import urllib.request, urllib.parse, json as _json
    # DALLAS-GATE-1 Part 2 pattern (see property_detail): scoped to
    # g.county_code, set per-request by _pull_county_slug from this route's
    # own <county_slug> segment. Missed in the original seam pass.
    county_code = g.county_code
    row = query("SELECT latitude, longitude, situs_address FROM parcel "
                "WHERE geo_id = %s AND county_code = %s",
                (geo_id, county_code), one=True)
    if not row:
        return jsonify({"ok": False, "error": "not_found"})
    if row.get("latitude") is not None and row.get("longitude") is not None:
        return jsonify({"ok": True, "lat": float(row["latitude"]), "lng": float(row["longitude"]), "cached": True})

    addr = (row.get("situs_address") or "").strip()
    if not addr:
        return jsonify({"ok": False, "error": "no_address"})
    one_line = " ".join(addr.split())  # collapse double spaces
    url = ("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?address="
           + urllib.parse.quote(one_line)
           + "&benchmark=Public_AR_Current&format=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Parcelytics/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read())
        matches = data.get("result", {}).get("addressMatches", [])
        if not matches:
            return jsonify({"ok": False, "error": "no_match"})
        c = matches[0]["coordinates"]
        lat, lng = float(c["y"]), float(c["x"])
        # Cache to the (previously empty) parcel columns.
        try:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute("UPDATE parcel SET latitude=%s, longitude=%s "
                            "WHERE geo_id=%s AND county_code=%s",
                            (lat, lng, geo_id, county_code))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"ok": True, "lat": lat, "lng": lng, "cached": False})
    except Exception as e:
        return jsonify({"ok": False, "error": "geocode_failed", "detail": str(e)[:100]})


@app.route("/<county_slug>/api/peer_set/<geo_id>")
@limiter.limit(_LIMIT_HEAVY)
def api_peer_set(geo_id):
    """Task 7 — up to 5 comparable parcels for the Submarket Position section.

    Tightened filter (July 2026, per Diego): this used to match on the BROAD
    classify.py category (property_type_label — e.g. "Commercial"), which is
    why a use-code-59 parcel (Office/Retail SFR Conv.) could appear as a
    "comparable" for a use-code-53 subject (Office Small) — confirmed as
    working-as-designed, not a bug, in an earlier investigation, with the
    recommendation to relabel the footnote rather than change the filter.
    Diego has now made the call directly: tighten the filter itself to
    require the SAME EXACT classi_cd as the subject, not just the same broad
    category.

    Thin-result risk (per Diego's brief — investigate before finalizing):
    this sandbox has no live DB, so the real distribution of "how many
    parcels share this parcel's exact use code in its neighborhood" can't be
    queried here — see PEER_SET_DISTRIBUTION_CHECK.sql (repo root) for a
    ready-to-run diagnostic Diego can execute to get real counts before/after
    this ships. Regardless of what that distribution turns out to be, the
    code below has to behave sanely across the whole range from "plenty of
    exact-use-code neighbors" to "this use code is rare in this parcel's
    area" to "no other parcel in the county shares this exact code" — so it's
    built as an explicit, reported cascade rather than a single query that
    might silently return zero rows:
      1. exact classi_cd + same neighborhood_cd (tightest, most relevant)
      2. exact classi_cd + same state_cd1 prefix, any neighborhood (existing
         relaxation pattern, now applied on top of the exact-code match
         instead of instead of it)
      3. exact classi_cd, county-wide (drop neighborhood/state-prefix), and
         widen the market-value band from ±25% to ±40% — Diego's suggested
         "widen the geographic/value radius while keeping the exact use
         code" fallback
      4. only if the subject itself has no classi_cd on file at all (exact
         matching is impossible, not just thin) OR tier 3 still returns
         zero: fall back to the old broad-category behavior, but flagged
         explicitly via `scope` so the UI can say so rather than silently
         reverting to the pre-tightening behavior unlabeled
    Each tier is tried only if the previous one came up short (<5 peers);
    `scope` in the response tells the frontend which tier actually produced
    the results shown, and `limited` flags anything other than tier 1 so the
    UI can render an honest "limited comps" note instead of presenting a
    widened or broad-category result as if it were the tightest possible
    match.
    """
    # DALLAS-GATE-2 Part 1 (prioritized -- Sentry-flagged chronic endpoint):
    # county_code threaded through every query in this function, including
    # the two performance-tuned MATERIALIZED CTE tiers below. Added as an
    # ADDITIONAL filter predicate only -- does not change either CTE's
    # chosen index (idx_parcel_classi_cd / idx_pty_year_market_value), so
    # this does not reintroduce the round-1 query-shape regression this
    # function's own history warns about.
    county_code = g.county_code
    parcel = query(
        "SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s",
        (geo_id, county_code), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    subj = query("""
        SELECT pty.market_value
        FROM parcel_tax_year pty WHERE pty.geo_id = %s AND pty.tax_year = 2025 AND pty.county_code = %s
    """, (geo_id, county_code), one=True)
    if not subj or not subj.get("market_value"):
        return jsonify({"ok": False, "error": "No 2025 market value for subject"})

    subj_label = property_type_label(parcel.get("classi_cd"), parcel.get("state_cd1"))
    subj_cc    = (parcel.get("classi_cd") or "").strip().upper()
    nb         = (parcel.get("neighborhood_cd") or "").strip()
    sc1        = (parcel.get("state_cd1") or "").strip()[:1]
    subj_mv    = float(subj["market_value"])
    lbl_sql    = label_case_sql("p.classi_cd", "p.state_cd1")
    # NULL-safety fix (July 2026) -- Tier 2 below matches on state_cd1
    # prefix; see api_peer_benchmark_local()'s comment / parcel_filters.py's
    # peer_state_cd1_match_sql() docstring for the full reasoning.
    # upper=True matches this tier's existing UPPER(p.state_cd1) usage.
    _peer_match_upper = peer_state_cd1_match_sql(upper=True)

    common_cols = """
        SELECT p.geo_id, p.prop_id, p.situs_address, p.classi_cd,
               p.living_area_sqft, p.land_sqft, p.year_built,
               pty.market_value, pty.assessed_value,
               ROUND(pty.assessed_value::numeric / NULLIF(pty.market_value, 0), 4) AS assessment_ratio,
               (SELECT SUM(ctr.rate)
                  FROM tax_billing_entity tbe
                  JOIN county_tax_rate ctr
                    ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
                   AND ctr.county_code = tbe.county_code
                 WHERE tbe.geo_id = p.geo_id AND tbe.tax_year = 2025
                   AND tbe.county_code = p.county_code) AS total_tax_rate
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
                                 AND pty.county_code = p.county_code
        WHERE p.geo_id <> %(geo)s
          AND p.geo_id NOT LIKE 'AJR%%'
          AND p.county_code = %(county_code)s
    """
    exact_select = common_cols + " AND UPPER(TRIM(p.classi_cd)) = %(cc)s"
    broad_select = common_cols + f" AND ({lbl_sql}) IS NOT DISTINCT FROM %(lbl)s"

    params = {
        "geo": geo_id, "lo": subj_mv * 0.75, "hi": subj_mv * 1.25,
        "lo_wide": subj_mv * 0.60, "hi_wide": subj_mv * 1.40,
        "lbl": subj_label, "nb": nb, "sc1": sc1, "cc": subj_cc, "mv": subj_mv,
        "county_code": county_code,
    }

    # Task PEER-SET-PERF-1/2 (Aug 2026): Tiers 2 and 3 below, history:
    #
    # Round 1 diagnosis (correct, still stands): confirmed via production
    # EXPLAIN ANALYZE on geo_id 0172190210's real Tier-2 query -- 8.2s, past
    # the 8s statement_timeout (Sentry PYTHON-FLASK-6). NOT the same bug as
    # M6 (api_peer_benchmark_local's full-table aggregate; this query's
    # total_tax_rate correlated subquery is fast, ~1.7ms/lookup). The
    # original problem was join ORDER: the planner drove from
    # parcel_tax_year (weakly filtered by market_value alone, ~73,768 rows)
    # and probed `parcel` by primary key ONE ROW AT A TIME (73,768 lookups)
    # to apply the actually-selective classi_cd/state_cd1 filters, for only
    # 532 real matches.
    #
    # Round 1 FIX REGRESSED, confirmed live (do not repeat this mistake):
    # a single MATERIALIZED CTE (parcel filters only) joined directly to
    # parcel_tax_year measured 16.4s -- WORSE than the 8.2s original. The
    # CTE itself worked exactly as intended (idx_parcel_classi_cd, ~220ms,
    # 10,144 real rows) -- the regression was the planner UNDERESTIMATING
    # that CTE's own output (guessed ~1,550 rows, 10,144 actual, a ~6.5x
    # miss) and choosing ANOTHER bad Nested Loop -- this time probing
    # parcel_tax_year_pkey 10,144 times -- to join it to parcel_tax_year.
    # Moving the bottleneck from one join to another isn't a fix.
    # classi_cd='01' (this incident's real value, "Single-Family Residence")
    # is confirmed (not inferred, per round 2's live count) to match
    # 308,046 of 517,614 parcels county-wide -- a candidate pool in the
    # thousands after Tier 2's full filter set is a normal, EXPECTED outcome
    # for this use code, not a fluke; the query has to handle that pool
    # size well, not just relocate where it chokes on it.
    #
    # Round 2 fix (current): two INDEPENDENTLY computed MATERIALIZED CTEs,
    # joined to each other on geo_id equality, rather than one CTE driving a
    # per-row probe into the other table:
    #   - `candidates`: parcel's own filters (classi_cd, state_cd1 prefix,
    #     geo_id exclusion) -- unchanged from round 1, already confirmed
    #     fast (~220ms).
    #   - `mv_band`: parcel_tax_year filtered independently by
    #     tax_year=2025 AND the market_value band -- using
    #     idx_pty_year_market_value ON parcel_tax_year(tax_year,
    #     market_value), a composite index that ALREADY EXISTS in
    #     schema.sql for exactly this filter shape (confirmed by reading
    #     schema.sql directly, not assumed).
    # Neither CTE's plan depends on the other's row count, and the join
    # between them is a plain equality with no ORDER BY dependency forcing
    # either side to drive a per-row lookup into the other -- this is the
    # shape Postgres's planner naturally prefers to execute as a Hash Join
    # regardless of which side's cardinality estimate is off, unlike round
    # 1's shape, which forced a correlated per-row lookup no matter which
    # side any estimate favored. This avoids BOTH the original bug's
    # mechanism (probing `parcel` per parcel_tax_year row) and round 1's
    # regression (probing `parcel_tax_year` per candidate row), rather than
    # trading one for the other again.
    #
    # ALSO applied (schema.sql, additive): `ALTER TABLE parcel ALTER COLUMN
    # classi_cd/state_cd1 SET STATISTICS 500` + `ANALYZE parcel` -- the
    # brief's own "cheapest thing to try first": better statistics should
    # make the planner's cardinality estimate for `candidates` much more
    # accurate on its own, independent of this query-shape change, for this
    # and any future query filtering on these columns.
    #
    # Still NOT applying query_no_nestloop(): even with round 2's real
    # evidence that THIS query's planner can badly misjudge a Nested Loop,
    # this query runs for every classi_cd value, including genuinely RARE
    # ones where a small candidate count legitimately makes Nested Loop the
    # correct, fast choice -- unconditionally forcing it off risks quietly
    # regressing those currently-fine cases to fix this one, a real,
    # unmeasured trade this sandbox cannot evaluate. The two-independent-
    # CTE restructure above targets the actual mechanism without forcing
    # any planner setting, so it doesn't need that trade -- IF it measures
    # well live; see the report for why this is not asserted as proven,
    # given round 1's own lesson.
    #
    # Tier 1 (below, unchanged) is NOT touched -- per the brief's own
    # assessment, neighborhood_cd (idx_parcel_neighborhood_cd, confirmed in
    # schema.sql) is a genuinely selective filter for most neighborhoods
    # (e.g. the single largest neighborhood is ~3,462 of 517,614 parcels,
    # per KNOWN_LIMITATIONS.md), so Tier 1 was not confirmed to share this
    # bug and the brief asks to leave anything not confirmed broken alone.
    #
    # Logic unchanged: identical WHERE conditions, identical SELECT columns,
    # identical ORDER BY/LIMIT, identical tier cascade order and thresholds
    # -- this is a query-shape rewrite only (see the final report for the
    # equivalence reasoning), not a change to which tier wins or what a tier
    # returns.
    peers, scope = [], "none"
    if subj_cc:
        # Tier 1: exact use code, same neighborhood — unchanged (not
        # confirmed to share this bug; see comment above).
        if nb:
            peers = query(exact_select + " AND pty.market_value BETWEEN %(lo)s AND %(hi)s"
                          " AND p.neighborhood_cd = %(nb)s"
                          " ORDER BY ABS(pty.market_value - %(mv)s) LIMIT 5", params)
            if peers:
                scope = "exact_neighborhood"
        # Tier 2: exact use code, same state_cd1 prefix (any neighborhood) —
        # confirmed slow (8.2s in production, round 1); rewritten again
        # (round 2) per the comment above after round 1's own rewrite
        # measured worse (16.4s).
        if len(peers) < 5:
            wider = query(f"""
                WITH candidates AS MATERIALIZED (
                    SELECT p.geo_id, p.prop_id, p.situs_address, p.classi_cd,
                           p.living_area_sqft, p.land_sqft, p.year_built
                    FROM   parcel p
                    WHERE  p.geo_id <> %(geo)s
                      AND  p.geo_id NOT LIKE 'AJR%%'
                      AND  UPPER(TRIM(p.classi_cd)) = %(cc)s
                      AND  {_peer_match_upper}
                      AND  p.county_code = %(county_code)s
                ),
                mv_band AS MATERIALIZED (
                    SELECT pty.geo_id, pty.market_value, pty.assessed_value
                    FROM   parcel_tax_year pty
                    WHERE  pty.tax_year = 2025
                      AND  pty.market_value BETWEEN %(lo)s AND %(hi)s
                      AND  pty.county_code = %(county_code)s
                )
                SELECT c.geo_id, c.prop_id, c.situs_address, c.classi_cd,
                       c.living_area_sqft, c.land_sqft, c.year_built,
                       m.market_value, m.assessed_value,
                       ROUND(m.assessed_value::numeric / NULLIF(m.market_value, 0), 4) AS assessment_ratio,
                       (SELECT SUM(ctr.rate)
                          FROM tax_billing_entity tbe
                          JOIN county_tax_rate ctr
                            ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
                           AND ctr.county_code = tbe.county_code
                         WHERE tbe.geo_id = c.geo_id AND tbe.tax_year = 2025
                           AND tbe.county_code = %(county_code)s) AS total_tax_rate
                FROM   candidates c
                JOIN   mv_band m ON m.geo_id = c.geo_id
                ORDER  BY ABS(m.market_value - %(mv)s) LIMIT 5
            """, params)
            if len(wider) > len(peers):
                peers, scope = wider, "exact_state_prefix"
        # Tier 3: exact use code, county-wide, wider value band (±40%) — the
        # "widen the radius, keep the exact use code" fallback for a genuinely
        # rare use code rather than silently reverting to broad category.
        # Structurally at LEAST as exposed as Tier 2 (even wider MV band,
        # and drops the state_cd1 prefix filter entirely, so more rows
        # would pass the WHERE if it hit the same bad plan) — same round-2
        # two-independent-CTE rewrite applied as a precaution; not
        # separately confirmed via its own live Tier-3 EXPLAIN ANALYZE
        # (see final report).
        if len(peers) < 3:
            widest = query(f"""
                WITH candidates AS MATERIALIZED (
                    SELECT p.geo_id, p.prop_id, p.situs_address, p.classi_cd,
                           p.living_area_sqft, p.land_sqft, p.year_built
                    FROM   parcel p
                    WHERE  p.geo_id <> %(geo)s
                      AND  p.geo_id NOT LIKE 'AJR%%'
                      AND  UPPER(TRIM(p.classi_cd)) = %(cc)s
                      AND  p.county_code = %(county_code)s
                ),
                mv_band AS MATERIALIZED (
                    SELECT pty.geo_id, pty.market_value, pty.assessed_value
                    FROM   parcel_tax_year pty
                    WHERE  pty.tax_year = 2025
                      AND  pty.market_value BETWEEN %(lo_wide)s AND %(hi_wide)s
                      AND  pty.county_code = %(county_code)s
                )
                SELECT c.geo_id, c.prop_id, c.situs_address, c.classi_cd,
                       c.living_area_sqft, c.land_sqft, c.year_built,
                       m.market_value, m.assessed_value,
                       ROUND(m.assessed_value::numeric / NULLIF(m.market_value, 0), 4) AS assessment_ratio,
                       (SELECT SUM(ctr.rate)
                          FROM tax_billing_entity tbe
                          JOIN county_tax_rate ctr
                            ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
                           AND ctr.county_code = tbe.county_code
                         WHERE tbe.geo_id = c.geo_id AND tbe.tax_year = 2025
                           AND tbe.county_code = %(county_code)s) AS total_tax_rate
                FROM   candidates c
                JOIN   mv_band m ON m.geo_id = c.geo_id
                ORDER  BY ABS(m.market_value - %(mv)s) LIMIT 5
            """, params)
            if len(widest) > len(peers):
                peers, scope = widest, "exact_widened"

    # Tier 4: subject has no classi_cd on file (can't exact-match at all), or
    # even the widened exact search above still came up empty. Falls back to
    # the pre-tightening broad-category behavior — but explicitly flagged via
    # `scope`, not silently, so the UI can say a real comp search came up
    # short rather than presenting this as an ordinary result.
    if not peers:
        peers = query(broad_select + " AND pty.market_value BETWEEN %(lo)s AND %(hi)s"
                      " ORDER BY ABS(pty.market_value - %(mv)s) LIMIT 5", params)
        if peers:
            scope = "broad_category_fallback" if subj_cc else "broad_category_no_subject_code"

    out = []
    for p in peers:
        cc = (p.get("classi_cd") or "").strip()
        desc = USE_CODE_LOOKUP.get(cc, ("", ""))[0]
        out.append({
            "geo_id":           p["geo_id"],
            "prop_id":          p["prop_id"],
            "address":          p.get("situs_address") or "—",
            "classi_cd":        cc or None,
            "use_desc":         desc or None,
            "main_area_sqft":   round(float(p["living_area_sqft"])) if p.get("living_area_sqft") else None,
            "land_sqft":        round(float(p["land_sqft"])) if p.get("land_sqft") else None,
            "year_built":       p.get("year_built"),
            "market_value":     int(p["market_value"]) if p.get("market_value") else None,
            "assessment_ratio": float(p["assessment_ratio"]) if p.get("assessment_ratio") is not None else None,
            "total_tax_rate":   float(p["total_tax_rate"]) if p.get("total_tax_rate") is not None else None,
        })
    return jsonify({
        "ok": True,
        "subject_label": subj_label,
        "subject_use_code": subj_cc or None,
        "neighborhood": nb,
        "scope": scope,
        "limited": scope not in ("exact_neighborhood",),
        "peers": out,
        "count": len(out),
    })


# ── On-demand billing fetch ────────────────────────────────────────────────────
@app.route("/<county_slug>/api/billing/<geo_id>")
@limiter.limit(_LIMIT_EXTERNAL)
def api_billing(geo_id):
    """Fetch + cache 2021-2024 billing data for one parcel from the portal.

    Called asynchronously by the property page after initial load.
    First call: hits the portal (~5-7 s), stores results, returns data.
    Subsequent calls: DB-only lookup, returns in <100 ms.

    Sentinel row (tax_year=9999): stored when portal responds but has no
    2021-2024 receipts, so we don't re-fetch on every page view.
    Network errors are NOT cached — the next visit will retry.
    """
    geo_id = geo_id.strip()
    # DALLAS-GATE-2 Part 1: tax_billing is a county_code-leading composite-PK
    # table per migrate_county_partitioning.py's TABLE_SPECS (already
    # migrated live in production by DALLAS-GATE-1). Every query below
    # (already-fetched check, sentinel INSERT + its ON CONFLICT target, final
    # SELECT) previously had no county_code at all -- county_code added as an
    # ADDITIONAL predicate/column throughout, ON CONFLICT target corrected to
    # match the real live unique constraint (county_code, geo_id, tax_year).
    # Also see loaders/scrape_billing_history.py's _UPSERT_SQL fix (this
    # route's upsert_billing_rows() call below shares that same write path
    # and had the identical county_code bug -- fixed there, not duplicated
    # here). BILLING-DIAG-7: that same shared function's OWN commit behavior
    # was separately investigated and found to already be correct (it always
    # committed) -- see the BILLING-DIAG-7 report for the full correction of
    # an incorrect "missing commit" theory. It now uses `with conn:` instead
    # of a bare conn.commit() call purely as defense-in-depth (auto-rollback
    # on exception, not previously guaranteed for this route's own error
    # path), not because anything was silently failing to commit.
    county_code = g.county_code
    conn = get_db()
    try:
        # 1. Already fetched? (real data from ANY source, or sentinel, both count)
        #
        # BILLING-DIAG-8: this check used to require data_source =
        # 'portal_scrape' specifically. Real, live evidence on a genuinely
        # fresh parcel (0121230106) showed the write path executing
        # correctly (receipts_parsed=13 target_matched=4 rows_written=4
        # commit_confirmed=True per BILLING-DIAG-7's own log line) while
        # this route's response stayed empty -- an unscoped SQL check found
        # why: that parcel already had real, correct, authoritative data
        # for all 4 target years from pir_billing_YYYY_full sources, and
        # _UPSERT_SQL's own ON CONFLICT ... WHERE clause correctly refused
        # to overwrite it with a portal scrape (a legitimate no-op, not a
        # bug -- that WHERE clause is NOT touched by this fix). The real
        # gap was here: a parcel with better existing data was treated as
        # never-fetched, so this route re-attempted a wasteful live portal
        # fetch on every single page view, for zero benefit (the write
        # could never win against the conflict guard anyway).
        #
        # Widened to: data_source = 'portal_scrape' (unchanged -- still
        # covers both real portal rows for the target years AND the
        # sentinel row, tax_year=9999, exactly as before) OR tax_year
        # BETWEEN 2021 AND 2024 (new -- any OTHER source's real row for a
        # target year now also counts as covered). A parcel with only
        # partial other-source coverage (e.g. 2021 only) is treated as
        # fully covered too, same as a parcel with full coverage --
        # consistent with the brief's own framing ("existing data for
        # 2021-2024 should be treated as already covered, skipping the
        # wasteful re-fetch"), flagged here as a real simplification: this
        # will not attempt to backfill the missing years for a
        # partially-covered parcel. A parcel with genuinely no row at all
        # (no sentinel, no other-source data) still evaluates to False,
        # exactly as before.
        # TAX-BILLING-REKEY-3 (§7.3 design (a)): portal-scrape rows
        # (including this route's own sentinel) now live in
        # tax_billing_portal_scrape, not tax_billing directly -- tax_billing
        # only picks them up once tax_billing_rollup.py's merge step runs
        # (a loader-orchestration step, not triggered synchronously by this
        # route). "Already fetched" must therefore check BOTH tables: a real
        # portal row or sentinel in tax_billing_portal_scrape (this route's
        # own write path), OR a real other-source row already in tax_billing
        # (unchanged from BILLING-DIAG-8's own widening).
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT "
                "  (EXISTS (SELECT 1 FROM tax_billing_portal_scrape "
                "           WHERE geo_id = %s AND county_code = %s)) "
                "  OR "
                "  (EXISTS (SELECT 1 FROM tax_billing "
                "           WHERE geo_id = %s AND county_code = %s "
                "             AND tax_year BETWEEN 2021 AND 2024)) "
                "  AS already_fetched",
                (geo_id, county_code, geo_id, county_code)
            )
            already_fetched = cur.fetchone()["already_fetched"]

        # 2. Portal fetch (only if not cached)
        if not already_fetched:
            # BILLING-DIAG-1 (real, live bug found and fixed here): this call
            # used to be `fetch_html(geo_id)` -- exactly ONE attempt, no
            # retry, REQUEST_TIMEOUT=20s. Confirmed via live evidence (curl
            # + a direct SQL check against production on two real parcels --
            # neither had so much as a sentinel row) that a single,
            # un-retried external HTTP call to this portal genuinely fails
            # often enough in production to matter, and the failure branch
            # was a silent no-op (no logging, no Sentry signal, by original
            # design -- "let next page visit retry") -- invisible until this
            # brief's manual curl+SQL cross-check, because Sentry only
            # captures unhandled exceptions and this path deliberately
            # doesn't raise one.
            #
            # Fix: 2 attempts, 10s timeout each -- deliberately NOT reusing
            # loaders/scrape_billing_history.py's own CLI-loader pattern
            # (MAX_RETRIES=3, REQUEST_TIMEOUT=20s each, up to 60s worst
            # case). That pattern is safe for the CLI batch script, which
            # has no request-time budget, but would be dangerous on this
            # live route: get_db()'s own comment (~line 1524) confirms
            # gunicorn runs with no --timeout flag -- the plain 30s DEFAULT
            # worker timeout is in effect, a hard SIGKILL boundary. 3 x 20s
            # would comfortably blow past it and reintroduce exactly the
            # WORKER TIMEOUT/SIGKILL class of incident (Sentry
            # PYTHON-FLASK-5/6) this codebase already hit once. 2 x 10s = 20s
            # worst case, leaving ~10s margin for DB I/O + Flask overhead.
            # Judgment call, flagged rather than silently decided: this
            # trades a little reliability (vs. the CLI loader's more patient
            # retry) for staying safely inside the request-time budget: the
            # real, more robust long-term fix is the async/background
            # pattern already proposed for this route in a prior brief
            # (Cowork "Propose /api/billing async/background pattern"),
            # which would remove this tradeoff entirely -- not built here,
            # this fix keeps the existing synchronous architecture.
            html, status = None, HTTP_NETWORK_ERR
            for _attempt in range(2):
                html, status = fetch_html(geo_id, timeout=10)
                # BILLING-DIAG-2: a real HTTP 200 with html content is no
                # longer trusted as "the real portal" on its own. New,
                # externally-corroborated evidence (Render community threads
                # + Render's own docs: outbound IPs are SHARED across ALL
                # Render customers in a region, changed range Nov 2025, and
                # other customers report 403s/WAF blocks reaching third-party
                # sites from those shared IPs) points to a real, structural
                # possibility this diagnosis didn't consider before: a WAF or
                # bot-detection layer in front of travis.go2gov.net could be
                # returning a real HTTP 200 with an HTML "blocked"/CAPTCHA
                # interstitial page instead of a clean 403 -- a common WAF
                # pattern specifically designed to defeat naive status-code
                # checks like BILLING-DIAG-1's `status == HTTP_OK`. If that's
                # what's happening, the OLD logic here would have treated a
                # block page as a successful, empty fetch and written a
                # PERMANENT, WRONG sentinel row (tax_year=9999) -- silently
                # poisoning that geo_id forever, since a sentinel makes
                # already_fetched True on every future visit, meaning it
                # would never be retried again. This also fully explains
                # BILLING-DIAG-2's own mystery (why the new Sentry warning
                # never fired): the code believed it had succeeded, so it
                # never reached the warning branch at all.
                # _BILLING_PORTAL_MARKER is real page content confirmed
                # present in BILLING-DIAG-1's own direct inspection of a
                # genuine, successful fetch (22,594 real chars, real
                # <title>Travis County Tax</title>). Its absence on an
                # otherwise-200 response is now treated the SAME as a
                # network failure (retried, then reported if exhausted) --
                # not as "genuinely fetched, no data."
                if html is not None and status == HTTP_OK and _BILLING_PORTAL_MARKER not in html:
                    html, status = None, HTTP_NETWORK_ERR
                if html is not None and status == HTTP_OK:
                    break
                if status == HTTP_NOT_FOUND:
                    break   # account genuinely not in portal -- don't retry

            if html is not None and status == HTTP_OK:
                receipts = parse_receipts(html)
                target   = [r for r in receipts if r["tax_year"] in _BILLING_TARGET_YEARS]
                if target:
                    records = [
                        {
                            "geo_id":     geo_id,
                            "tax_year":   r["tax_year"],
                            "total_paid": r["payment_amount"],
                            "county_code": county_code,
                        }
                        for r in target
                    ]
                    # BILLING-DIAG-7: upsert_billing_rows() now returns the real
                    # row count and commits internally via `with conn:` (see
                    # loaders/scrape_billing_history.py). PERMANENT (not a
                    # temporary diagnostic, per Fable's review): the write path
                    # emitted nothing on success this entire time, which is
                    # exactly why 79 real sentinel rows accumulated in
                    # production with zero real target-year rows ever visibly
                    # confirmed as written -- this line makes that state
                    # observable on every future request, not just during an
                    # active investigation.
                    written = upsert_billing_rows(conn, records)
                    print(
                        f"api_billing write: geo_id={geo_id} county_code={county_code} "
                        f"receipts_parsed={len(receipts)} target_matched={len(target)} "
                        f"rows_written={written} commit_confirmed=True",
                        flush=True,
                    )
                else:
                    # Portal has this account but no 2021-2024 receipts —
                    # sentinel. TAX-BILLING-REKEY-3: redirected to
                    # tax_billing_portal_scrape, the same real table this
                    # route's own successful-fetch branch above now writes
                    # to (via upsert_billing_rows()) -- both the sentinel and
                    # real portal rows are this loader's own write path, per
                    # §7.3 design (a), and belong in the same table.
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO tax_billing_portal_scrape "
                            "  (county_code, geo_id, tax_year, data_source, confidence_level) "
                            "VALUES (%s, %s, %s, 'portal_scrape', 'partial') "
                            "ON CONFLICT (county_code, geo_id, tax_year) DO NOTHING",
                            (county_code, geo_id, _BILLING_SENTINEL_YEAR)
                        )
                    conn.commit()
                    # BILLING-DIAG-7: PERMANENT. Symmetric with the real-write
                    # log line above -- this is the branch that has actually
                    # been firing for all 79 real sentinel rows found in
                    # production. Making it observable is the real, concrete
                    # next step toward answering whether that's correct
                    # (parse_receipts() genuinely finds no 2021-2024 data for
                    # these parcels) or a real parsing bug -- not resolved by
                    # this brief, see the BILLING-DIAG-7 report.
                    print(
                        f"api_billing write: geo_id={geo_id} county_code={county_code} "
                        f"receipts_parsed={len(receipts)} target_matched=0 "
                        f"rows_written=0 sentinel_written=True commit_confirmed=True",
                        flush=True,
                    )
            elif status != HTTP_NOT_FOUND:
                # Network/429/5xx/WAF-block-page, both attempts exhausted →
                # still don't cache (let next page visit retry, same as
                # before) — but now a low-noise, non-exception Sentry signal
                # (level=warning) so a PERSISTENTLY failing parcel is visible
                # as a pattern instead of indistinguishable from "genuinely
                # no receipts", which is exactly what made BILLING-DIAG-1's
                # bug invisible until a manual curl+SQL cross-check found it.
                #
                # BILLING-DIAG-2: explicit flush(), bounded to 2s, added
                # after this call. sentry_sdk queues events to a background
                # delivery thread by default and does NOT guarantee delivery
                # before the request returns/the process moves on -- under
                # gunicorn's 30s default worker timeout (no --timeout flag
                # set, confirmed in BILLING-DIAG-1's own report) a request
                # that runs close to that budget risks the worker being
                # recycled/killed before a queued-but-undelivered message
                # reaches Sentry. This doesn't prove that's what happened to
                # BILLING-DIAG-2's own missing warning (this sandbox cannot
                # reach Sentry's ingest API to confirm either way), but it's
                # a real, low-risk, unconditionally-correct hardening
                # regardless of which of BILLING-DIAG-2's hypotheses turns
                # out to be the actual cause.
                sentry_sdk.capture_message(
                    f"api_billing: portal fetch failed for geo_id={geo_id} "
                    f"after 2 attempts, last status={status}",
                    level="warning",
                )
                sentry_sdk.flush(timeout=2)
            # HTTP_NOT_FOUND (404): genuinely no account in portal — no
            # sentinel written here either (unchanged from before this fix;
            # the sentinel is specifically for "account found, no target-
            # year receipts", a different real case than "no account at
            # all" — worth its own look but out of BILLING-DIAG-1's scope).

        # 3. Return 2021-2024 rows from ANY data_source (sentinel excluded by
        # year range regardless of source -- tax_year=9999 is outside
        # BETWEEN 2021 AND 2024 no matter what).
        #
        # BILLING-DIAG-8: this used to also filter on data_source =
        # 'portal_scrape', so real, correct, existing data from other
        # sources (e.g. pir_billing_YYYY_full) never displayed through this
        # endpoint even when it was sitting right there in the table --
        # same root cause as the already_fetched widening above. Removing
        # that filter is the whole fix; the year-range + county_code
        # filters are unchanged and still do all the real scoping work.
        # TAX-BILLING-REKEY-3: unioned with tax_billing_portal_scrape --
        # a real fetch this same request just performed lands there, not in
        # tax_billing (which only picks it up once tax_billing_rollup.py's
        # merge step next runs, a loader-orchestration step this live route
        # does not trigger). Without this union, a first-ever fetch would
        # write real data and then immediately return an empty `rows` list
        # for it -- a real regression the read side must account for, not
        # just the write side. tax_billing's own row for a given
        # (geo_id, tax_year) always wins when both exist (mirrors
        # tax_billing_rollup.py's own account-grain-always-wins policy) --
        # a portal_scrape row only shows here when tax_billing has nothing
        # yet for that year.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "WITH tb AS ("
                "  SELECT tax_year, total_tax, total_paid, data_source, confidence_level "
                "  FROM tax_billing "
                "  WHERE geo_id = %s AND county_code = %s AND tax_year BETWEEN 2021 AND 2024"
                "), ps AS ("
                "  SELECT tax_year, total_paid AS total_tax, total_paid, data_source, confidence_level "
                "  FROM tax_billing_portal_scrape "
                "  WHERE geo_id = %s AND county_code = %s AND tax_year BETWEEN 2021 AND 2024"
                ") "
                "SELECT * FROM tb "
                "UNION ALL "
                "SELECT * FROM ps WHERE ps.tax_year NOT IN (SELECT tax_year FROM tb) "
                "ORDER BY tax_year",
                (geo_id, county_code, geo_id, county_code)
            )
            rows = [dict(r) for r in cur.fetchall()]

        # psycopg2 returns Decimal — convert for JSON
        for row in rows:
            for k in ("total_tax", "total_paid"):
                if row[k] is not None:
                    row[k] = float(row[k])

        return jsonify({"status": "ok", "cached": already_fetched, "rows": rows})

    except Exception as exc:
        # BILLING-DIAG-3: real, standalone gap found and fixed here, independent
        # of which BILLING-DIAG-3 hypothesis turns out to be the cause. This
        # except block has ALWAYS silently converted any real exception into a
        # clean-looking {"status":"error",...} JSON response with ZERO Sentry
        # visibility -- confirmed via grep across the whole file: no
        # capture_exception() call existed anywhere, and sentry_sdk's
        # FlaskIntegration (app.py's own sentry_sdk.init() call, ~line 1332)
        # only auto-captures exceptions that bubble up UNCAUGHT to Flask --
        # an exception caught here, inside application code, never reaches
        # that auto-instrumentation. This means any real, unexpected error in
        # this route (a dropped DB connection, a KeyError, anything) has been
        # completely invisible in Sentry this entire time, regardless of the
        # billing-fetch investigation. Fixed unconditionally; not a guess.
        sentry_sdk.capture_exception(exc)
        sentry_sdk.flush(timeout=2)
        return jsonify({"status": "error", "message": str(exc), "rows": []})
    finally:
        conn.close()



# ── Task 5: ptype label → SQL WHERE fragments ──────────────────────────────────
# task5_drill_through
#
# Issue B fix (July 2026): this used to be a static dict keyed by the OLD,
# state_cd1-sub-prefix-based labels ('Single-Family', 'Condo / Townhome',
# 'Multifamily (5+ units)', etc.) — see use_code_case_sql()'s docstring for
# why those labels were replaced (not because the sub-codes are invalid --
# they're real Comptroller codes -- but because it's unconfirmed whether
# this data populates state_cd1 at that granularity, and classi_cd is the
# better subtype signal regardless). Once the breakdown query stopped emitting those
# labels, this dict would have silently stopped matching anything for every
# sector sub-type link, falling through to sc1_filter = "1=1" (i.e. clicking
# any specific subtype would show ALL parcels in the sector, not just that
# subtype) — a second, separate manifestation of the same root cause.
#
# Fix: rather than maintain a second hand-written label -> filter mapping
# that can drift out of sync with the breakdown query again, this route
# recomputes the *exact same* CASE expression _compute_snapshot_data() used
# to produce the clicked-through ptype label, and matches on equality. The
# link can never point to a different population than the row it came from.
def _ptype_drill_where(view, ptype, rolled=None):
    """WHERE-clause fragment selecting exactly the parcels that produced the
    given ptype row/label for this view -- reuses the same
    _snapshot_taxonomy_sql() / label_case_sql() / use_code_case_sql()
    expressions the Market Snapshot breakdown query groups by, so /parcels
    always matches what was actually counted.

    `rolled`: Part 2 fix (this round) -- when the clicked row is a capped
    "Other <Sector>" rollup (see _cap_subtype_rows()), it represents the
    UNION of several distinct real per-code ptype values, not one SQL-level
    value. `rolled` is that list (row["_rolled_ptypes"], passed through by
    the template/route) -- when present, matches ANY of those real values
    (via = ANY(%(rolled)s)) PLUS the sector's own literal fallback string
    itself, rather than a single ptype equality check.

    NOTE: this fragment is embedded inside parcel_list()'s y25 CTE
    definition, where the parcel table alias in scope is 'p' (the CTE
    itself, 'y25', isn't a valid reference within its own body) -- so this
    uses p.classi_cd/p.state_cd1, not y25.*. The prior version of this route
    referenced y25.state_cd1 inside the CTE's own WHERE for the sub-type
    fragments, which would have been an invalid column reference; it never
    surfaced because those sub-type labels ('Single-Family', 'Condo /
    Townhome', etc.) were never actually produced by the breakdown query
    once every parcel in a sector started collapsing to one "Other X" row
    (see use_code_case_sql()'s docstring -- root cause pending the live
    state_cd1 granularity check, but the symptom was real either way) --
    this alias bug and the breakdown bug were masking each other."""
    if view in _SNAPSHOT_SECTOR_VIEWS:
        # Part 1/4 fix (this round): the 8 new tabs + Other route through
        # the scoped _snapshot_taxonomy_sql(), not classify.py's
        # label_case_sql() -- matches _snapshot_view_where()'s routing.
        sector_label = _SNAPSHOT_SECTOR_VIEWS[view]
        _tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
        use_expr = _use_code_expr_for_view(view)
        if rolled:
            return f"({_tax}) = '{sector_label}' AND ({use_expr}) = ANY(%(rolled)s)"
        return f"({_tax}) = '{sector_label}' AND ({use_expr}) = %(ptype)s"
    if view == "commercial":
        # Legacy view -- unchanged canonical label_case_sql() matching, for
        # old deep links only. Still subject to the Part 2 cap (applied in
        # _compute_snapshot_data()), so `rolled` can apply here too.
        sector_label = _SNAPSHOT_VIEW_PROP_TYPE_LABEL["commercial"]
        _lbl = label_case_sql("p.classi_cd", "p.state_cd1")
        use_expr = _use_code_expr_for_view(view)
        if rolled:
            return f"({_lbl}) = '{sector_label}' AND ({use_expr}) = ANY(%(rolled)s)"
        return f"({_lbl}) = '{sector_label}' AND ({use_expr}) = %(ptype)s"
    # Overall: Part 1 fix (this round) -- ptype is now one of the 9 Market
    # Snapshot taxonomy labels (Residential/.../Other), via
    # _snapshot_taxonomy_sql(), matching _compute_snapshot_data()'s new
    # "overall" branch. That CASE's own ELSE is 'Other' (never NULL), so
    # ordinary equality covers the "Other" row too -- no IS NULL special
    # case needed anymore (the old canonical label_case_sql() COULD return
    # NULL, which is why the prior version of this branch needed one).
    _tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
    return f"({_tax}) = %(ptype)s"


def _use_code_expr_for_view(view):
    """Same expression _compute_snapshot_data() uses per view for the
    within-sector subtype breakdown, byte-for-byte, so drill-through always
    matches what produced the clicked row. Covers the 8 new Market Snapshot
    tabs (+ "other"), the legacy "commercial" view, and -- Land/Ag fix,
    this round -- the land_sqft size-tier expression for "land"/
    "agricultural" (see _compute_snapshot_data()'s Land/Ag branch and the
    big comment above SNAPSHOT_LAND_SIZE_TIERS: classi_cd/use_code_case_sql
    is structurally empty for these two sectors, so they don't use it)."""
    if view == "land":
        return _size_tier_case_sql("p.land_sqft", SNAPSHOT_LAND_SIZE_TIERS)
    if view == "agricultural":
        return _size_tier_case_sql("p.land_sqft", SNAPSHOT_AG_SIZE_TIERS)
    if view in _SNAPSHOT_SECTOR_VIEWS:
        sector_label = _SNAPSHOT_SECTOR_VIEWS[view]
        fallback = "Uncategorized" if sector_label == "Other" else f"Other {sector_label}"
    else:
        fallback = {"commercial": "Other Commercial"}.get(view, "Other")
    return use_code_case_sql("p.classi_cd", fallback)


@app.route("/<county_slug>/parcels")
def parcel_list():
    """
    Drill-through parcel list (Task 5).
    Query params:
      view   str   snapshot view (residential/retail/industrial/.../commercial legacy/etc.)
      ptype  str   ptype label from snapshot rows (e.g. 'Single-Family Residence',
                    or a capped "Other <Sector>" rollup label)
      rolled str   repeatable -- Part 2 fix (this round): when the clicked row
                    was a capped rollup (see _cap_subtype_rows()), the template
                    passes every real ptype string folded into it via repeated
                    ?rolled=... params so this route matches all of them, not
                    just a literal "Other <Sector>" equality.
    Returns up to 500 matching parcels with 2025 + 2026 market values.
    """
    view  = request.args.get("view", "overall")
    ptype = request.args.get("ptype", "").strip()
    rolled = request.args.getlist("rolled") or None

    where_fragment = _ptype_drill_where(view, ptype, rolled=rolled) if ptype else "1=1"

    # DALLAS-GATE-2 Part 2: verify_index_coverage.py flagged this WHERE
    # clause as entirely dynamic ({where_fragment}) and therefore
    # unresolvable by static analysis -- reading the real, assembled SQL by
    # hand shows it had NO county_code predicate anywhere. p.county_code
    # added to the y25 CTE's SELECT list (so it can be carried through to
    # the t26 JOIN below) and its WHERE clause; g.county_code threaded
    # through as an ADDITIONAL predicate only.
    county_code = g.county_code

    # Build alias-safe filter: join alias is 'y25', parcel alias is 'p'
    rows = query(f"""
        WITH y25 AS (
            SELECT p.geo_id, p.county_code, p.state_cd1, p.classi_cd, p.situs_address, p.owner_name,
                   t.market_value AS mv25
            FROM   parcel p
            JOIN   parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
                                     AND t.county_code = p.county_code
            WHERE  t.market_value > 0
              {CANONICAL_PARCEL_EXCL}
              AND  p.county_code = %(county_code)s
              AND  ({where_fragment})
        )
        SELECT
            y25.geo_id,
            y25.situs_address  AS address,
            y25.owner_name     AS owner,
            y25.mv25,
            t26.market_value   AS mv26,
            t26.data_source    AS data_source_2026
        FROM  y25
        LEFT JOIN parcel_tax_year t26
               ON t26.geo_id = y25.geo_id AND t26.tax_year = 2026
              AND t26.county_code = y25.county_code
        ORDER BY y25.mv25 DESC NULLS LAST
        LIMIT 500
    """, {"ptype": ptype, "rolled": rolled, "county_code": county_code})

    parcels = [dict(r) for r in rows]

    # M4-2026-PRELIM-SNAPSHOT Part 1 fix: this list used to hardcode a
    # page-wide "2026 Preliminary" header/footer regardless of each row's
    # actual data_source. Up to 500 parcels are shown here, so rather than
    # add a per-row badge (noisy at this scale) we compute the real
    # certified/preliminary split across just the rows that actually have a
    # 2026 value, and use that to pick an accurate, honest page-level label
    # (all-certified / all-preliminary / mixed) -- same technique as
    # /api/benchmark's n_preliminary fix.
    _2026_present = [p for p in parcels if p.get("mv26") is not None]
    _n_certified_2026 = sum(
        1 for p in _2026_present
        if p.get("data_source_2026") in CERTIFIED_TIER_DATA_SOURCES
    )
    if not _2026_present:
        status_2026 = "none"
    elif _n_certified_2026 == len(_2026_present):
        status_2026 = "certified"
    elif _n_certified_2026 == 0:
        status_2026 = "preliminary"
    else:
        status_2026 = "mixed"

    return render_template(
        "parcel_list.html",
        view=view,
        ptype=ptype or "All",
        parcels=parcels,
        status_2026=status_2026,
    )


@app.route("/<county_slug>/compare")
@limiter.limit(_LIMIT_HEAVY)
def compare_parcels():
    """
    Side-by-side parcel comparison (Task 5).
    Query param:
      ids  str   comma-separated geo_ids (2–4)
    """
    ids_raw = request.args.get("ids", "").strip()
    geo_ids = [g.strip() for g in ids_raw.split(",") if g.strip()][:4]

    if len(geo_ids) < 2:
        return render_template(
            "compare.html",
            parcels=[],
            error="Provide 2–4 geo_ids as ?ids=id1,id2 to compare.",
        )

    # DALLAS-GATE-2 Part 2: verify_index_coverage.py flagged this route's
    # tax_billing lookup below (geo_id + tax_year, no county_code -- a
    # county_code-leading composite-PK table per migrate_county_
    # partitioning.py's TABLE_SPECS). county_code added as an ADDITIONAL
    # predicate only.
    county_code = g.county_code

    parcels = []
    for geo_id in geo_ids:
        # PX-20260828-12 fix: this route's initial parcel/parcel_tax_year
        # fetches lacked county_code (parcel/parcel_tax_year are county_code-
        # leading composite-PK tables per migrate_county_partitioning.py --
        # geo_id alone is only unique WITHIN a county), even though the
        # billing query two lines below already had it (DALLAS-GATE-2 Part 2
        # fixed that one but missed these -- same gap found audit-wide, see
        # api_estimate_acq/api_peer_benchmark_sf/api_peer_benchmark_local/
        # api_parcel_entities/export_due_diligence_pdf).
        parcel = query("SELECT * FROM parcel WHERE geo_id = %s AND county_code = %s", (geo_id, county_code), one=True)
        if not parcel:
            continue

        current = query("""
            SELECT market_value, assessed_value, taxable_value, hs_cap_loss, data_source, exemption_codes
            FROM   parcel_tax_year WHERE geo_id = %s AND tax_year = 2025 AND county_code = %s
        """, (geo_id, county_code), one=True)

        current_2026 = query("""
            SELECT market_value, assessed_value, data_source
            FROM   parcel_tax_year WHERE geo_id = %s AND tax_year = 2026 AND county_code = %s
        """, (geo_id, county_code), one=True)

        billing = query("""
            SELECT total_tax, total_paid, total_due, is_delinquent
            FROM   tax_billing WHERE geo_id = %s AND tax_year = 2025 AND county_code = %s
        """, (geo_id, county_code), one=True)

        sc1 = (parcel.get("state_cd1") or "").strip()[:1]
        type_map = {
            "A": "Residential", "B": "Multi-Family", "C": "Land/Vacant",
            "D": "Agricultural", "E": "Agricultural", "F": "Commercial",
        }

        # Issue 3 fix ("Homestead-Cap Data Integrity: Full Fix Set" Cowork
        # brief, July 2026) -- this route's "Cap Loss (HS)" row is a 5th
        # instance of the same structural bug the property.html/build_
        # projections()/build_insights()/cap_was_active fixes already
        # closed: gating on current.hs_cap_loss > 0, which is structurally
        # NULL for every 2025 row (never populated by load_certified_2025.py).
        # This route doesn't fetch 2021-2024 history at all (only the single
        # 2025 row), so there's no real AJR hs_cap_loss to fall back to here
        # -- the market-minus-assessed approximation (same "~" register the
        # other four fixes use) is the ONLY possible signal in this route's
        # existing data shape, not a second-best option.
        cur_dict = dict(current) if current else {}
        cap_loss_est = None
        if (cur_dict.get("exemption_codes")
                and "HS" in {c.strip().upper() for c in cur_dict["exemption_codes"].replace(";", ",").split(",")}
                and cur_dict.get("market_value") and cur_dict.get("assessed_value") is not None
                and cur_dict["market_value"] > cur_dict["assessed_value"]):
            cap_loss_est = cur_dict["market_value"] - cur_dict["assessed_value"]
        cur_dict["cap_loss_estimate"] = cap_loss_est

        parcels.append({
            "geo_id":        geo_id,
            "address":       parcel.get("situs_address") or "Unknown",
            "prop_type":     type_map.get(sc1, sc1 or "Unknown"),
            "parcel":        dict(parcel),
            "current":       cur_dict,
            "current_2026":  dict(current_2026) if current_2026 else {},
            "billing":       dict(billing) if billing else {},
            # M4-2026-PRELIM-SNAPSHOT Part 1 fix: compare.html used to
            # hardcode "2026 Preliminary" unconditionally for every parcel.
            "is_2026_certified": bool(
                current_2026 and current_2026.get("data_source") in CERTIFIED_TIER_DATA_SOURCES
            ),
        })

    if not parcels:
        return render_template("compare.html", parcels=[], error="No valid parcels found for the provided IDs.")

    return render_template("compare.html", parcels=parcels, error=None)


def _info_response(county_code, county_slug_val, api_county_slug=None):
    """Shared body for info() (county-anchored, '/<county_slug>/info') and
    info_landing() (neutral, bare '/info') -- PX-20260828-01, same
    refactor pattern as _home_search_response()/search.html's landing pair.
    county_profile/county_slug are passed explicitly (Jinja lets an
    explicit render_template() kwarg override a same-named context-
    processor value) so info_landing() can render the FILTERED county's
    content without ever setting g.county_slug/g.county_code -- see
    info_landing()'s own docstring for why that matters.

    PX-20260828-15 Task 1: api_county_slug is now a SEPARATE parameter
    from county_slug_val, not always the same value. county_slug_val
    drives the page's own displayed content (which county's info sections
    show) and defaults api_county_slug to itself, so info() (the anchored
    route, which always passes a real, explicitly-chosen g.county_slug)
    is unaffected. info_landing() passes api_county_slug explicitly and
    separately -- see that route's own docstring for why the two values
    diverge specifically in its no-?county-given fallback case."""
    profile = COUNTY_PROFILES.get(county_code, COUNTY_PROFILES["TRAVIS"])
    return render_template(
        "info.html",
        info_content_available=profile.get("info_content_available", False),
        county_profile=profile,
        county_slug=county_slug_val,
        api_county_slug=api_county_slug if api_county_slug is not None else county_slug_val,
    )


@app.route("/<county_slug>/info")
def info():
    """Informational reference page -- topic sections (starting with Homestead
    Exemptions) filtered by state / county. Static content today (Texas /
    Travis County only), no parcel or DB data involved -- structured so more
    topics/states/counties can be added later without a route change.

    PX-20260828-01: Diego's 2026-08-28 ruling supersedes -03-rev1's
    "Info stays anchored" decision below -- info_landing() (bare '/info')
    is now the real, nav-linked page, with its own in-page county filter.
    This county-anchored route is KEPT, unchanged, as a real direct deep
    link (e.g. from external/marketing links built before this brief) --
    same relationship index()/tax_rates()/county_snapshot() now have to
    their own bare '_landing' twins.

    [-03-rev1, superseded reasoning, kept for history: Info stays
    county-anchored (kept its existing <county_slug> prefix -- Diego's
    ruling (b): its body content is real, researched legal/exemption
    material, not a name-and-logo template, so treating it as neutral
    would either silently show Travis content under a Dallas URL or
    require inventing Dallas content neither of us has researched.]

    info_content_available comes straight from COUNTY_PROFILES (default
    False if the field is missing entirely, e.g. a future county profile
    that hasn't set it yet) -- an honest "not populated for this county"
    render, the same pattern as any other unset profile field, not a
    silent Travis fallback. Both this route and info_landing() render
    that same honest state -- neutral-URL doesn't change what content
    exists, only how you get there."""
    return _info_response(g.county_code, g.county_slug)


@app.route("/info")
def info_landing():
    """PX-20260828-01: the real, neutral bare '/info' page -- Diego's
    2026-08-28 ruling. Carries the filtered county via ?county=<slug>
    (e.g. '/info?county=dallas-tx'), NOT a URL segment and NOT a redirect
    -- bookmarkable/shareable, matches the slug format COUNTY_SLUGS
    already uses everywhere, no session-state complexity. An absent or
    unrecognized ?county= value falls back to DEFAULT_COUNTY_SLUG
    (Travis) rather than 404ing -- a query-string filter is inherently
    soft (unlike a URL path segment, which IS the page's identity), so a
    typo or first bare-page visit should still render a real, useful
    page, not an error. Same tolerance search_landing()'s own
    ?neighborhood=/?prop_type= pre-fill already uses.

    Deliberately does NOT set g.county_slug/g.county_code -- doing so
    would flip is_county_anchored (hasattr(g, "county_slug")) to True on
    a page whose whole point is staying URL-neutral, which would
    incorrectly trigger the nav-level county-switcher's "real anchored
    page" treatment (see _inject_county_helpers()'s own comment).
    Instead, the resolved county is passed straight into the shared
    response body as explicit render_template() kwargs, which Jinja lets
    override the context-processor's own same-named defaults.

    PX-20260828-15 Task 1: api_county_slug is now resolved SEPARATELY
    from the page's own displayed county_slug (via
    _resolve_landing_county_slug()'s `explicit` flag). An explicit
    ?county=<slug> still scopes the embedded search box to that real
    county -- unchanged. But when no ?county= is given at all, this page
    still displays Travis's info content (a legitimate, disclosed content
    fallback -- info_content_available is real per-county data, not this
    bug), while its search box now gets the bare, cross-county-capable
    COUNTY_BASE ("") instead of silently inheriting the same Travis
    fallback the displayed content uses. Those are two independent
    questions -- "what content shows by default" and "what should this
    page's search box search" -- and conflating them was the root of the
    live /about bug this brief exists to close out."""
    county_code, slug, explicit = _resolve_landing_county_slug()
    return _info_response(county_code, slug, api_county_slug=(slug if explicit else ""))


# PX-20260827-03-rev1 Task 1: about/terms/privacy/disclaimer/styleguide are
# now NEUTRAL, bare-path routes (no leading <county_slug> segment) --
# Diego's ruling: reachable from anywhere, not tied to any one county's
# silo. Each one's corresponding _LEGACY_REDIRECT_ROUTES entry is removed
# below (that list's own comment explains why: these ARE the real routes
# now, not redirect targets). county_profile/county_slug in their
# templates fall back to TRAVIS via _inject_county_helpers()'s existing
# DEFAULT_COUNTY_SLUG convention (g.county_code is never set on a route
# with no <county_slug> segment) -- About discloses this fallback
# explicitly per Diego's ruling (a); terms/privacy/disclaimer/styleguide
# have no county-scoped content to begin with (confirmed by reading each
# template -- static legal text / a design-token reference).
@app.route("/about")
def about():
    return render_template("about.html")


# Cowork brief "Terms of Service, Privacy Policy, Disclaimer Page, Beta
# Popup, Footer Notice", July 2026. Static legal content, no DB/network
# involved -- same undecorated (global-rate-limit-only) treatment as /about,
# /info, /styleguide above.
@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/disclaimer")
def disclaimer():
    return render_template("disclaimer.html")


@app.route("/styleguide")
def styleguide():
    """Design-system reference: renders every token and component.
    Single source of truth for the visual language — review here before
    restyling real pages. Not linked in primary nav."""
    return render_template("styleguide.html")


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT)
