"""
Travis County Property Tax Platform — Flask Web Application
Phase 1: Parcel search + 5-year history + tax rate trends
"""
import os
import sys
import json
import re
import time
from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(__file__))
import config

from tax_logic.texas import estimate_post_acquisition as _tx_estimate
from tax_logic.texas import estimate_homestead_savings as _tx_hs_savings
from tax_logic.classify import property_type_label, label_case_sql, label_sort_case_sql
from loaders.scrape_billing_history import fetch_html, parse_receipts, upsert_billing_rows, HTTP_OK

_BILLING_TARGET_YEARS  = {2021, 2022, 2023, 2024}
_BILLING_SENTINEL_YEAR = 9999   # stored when portal returns no target-year data


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
        # The primary "is the cap active right now?" signal is parcel_metrics.risk_homestead_cap_expiry
        # (2025 Certified data). These keys feed the calm historical note in the Insight Report.
        out["hs_history_loss"] = hs_row["hs_cap_loss"]
        out["hs_history_year"] = hs_row["tax_year"]
        out["hs_history_pct"]  = round(hs_row["hs_cap_loss"] / latest["market_value"] * 100, 1)

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


def build_projections(history, rate_history, entity_detail, years_ahead=5, state_cd1=None):
    """
    Project market value, assessed value, and estimated taxes for the next N years.

    Value trend  : CAGR from earliest→current market values.
    Rate trend   : avg annual change in combined rate over available rate history.
    Assessed     : if homestead cap exists, cap at 10%/yr; else tracks market.
    Est. tax     : assessed * projected_rate / 100.

    Task 10 — CAGR baseline extension:
    If 2026 preliminary market value exists and is non-anomalous (assessed ≤ market),
    extend the CAGR window to 2021–2026 for a 6-year baseline. Projections still
    start from the 2025 certified values; the 2026 data only calibrates the CAGR.

    Agricultural guard (D/E parcels):
    AJR 2021 stores productivity/use values in the market_value field for agricultural
    property classes. Using 2021 as the CAGR starting point for D/E parcels produces
    meaningless projections. 2021 is excluded and 2022 used as the earliest reliable year.
    """
    hist = sorted([r for r in history if r["market_value"]], key=lambda r: r["tax_year"])
    if len(hist) < 2:
        return [], None

    # Agricultural guard: skip 2021 baseline for D/E property classes
    _is_ag = (state_cd1 or "").strip()[:1].upper() in ("D", "E")
    if _is_ag:
        hist = [r for r in hist if r["tax_year"] != 2021]
        if len(hist) < 2:
            return [], None

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
    if _is_ag:
        baseline_label = (
            "Based on 2022–2026 preliminary trend" if r2026
            else "Based on 2022–2025 certified trend"
        )
    else:
        baseline_label = (
            "Based on 2021–2026 preliminary trend" if r2026
            else "Based on 2021–2025 certified trend"
        )

    span = cagr_endpoint["tax_year"] - earliest["tax_year"]
    if span <= 0 or not earliest["market_value"]:
        return [], None

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

    # Homestead cap: check any year with hs_cap_loss (2025 Certified doesn't carry this field)
    hs_row = next(
        (r for r in reversed(hist) if r.get("hs_cap_loss") and r["hs_cap_loss"] > 0),
        None
    )
    has_hs_cap = hs_row is not None

    # Always project from 2025 certified base values
    base_row      = next((r for r in hist if r["tax_year"] == 2025), hist[-1])
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

    # ── Para 1: property identity + value trajectory ──────────────────────────
    p1 = [f"{address} is a {prop_type} parcel in Travis County, Texas."]
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
        if m25.get("risk_homestead_cap_expiry"):
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
    proj_avg_g = None
    if projections:
        base_mv = hist[-1]["market_value"] if hist else None
        if base_mv:
            proj_avg_g = round(
                sum(p["value_change"] for p in projections) / len(projections), 1
            )

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
    tax_pts = [(r["tax_year"], float(r["total_tax"])) for r in hist if r.get("total_tax")]
    curr_tax = next((t for yr, t in tax_pts if yr == 2025), None)
    avg_tax  = round(sum(t for _, t in tax_pts) / len(tax_pts)) if tax_pts else None
    peak_t   = max(tax_pts, key=lambda x: x[1]) if tax_pts else None
    trough_t = min(tax_pts, key=lambda x: x[1]) if tax_pts else None
    proj_tax = round(sum(p["est_tax"] for p in projections) / len(projections)) if projections else None

    def _fmt_usd(v):
        return f"${v:,.0f}" if v is not None else "—"

    rows.append(dict(
        label="Tax Amount",
        twelve_month=_fmt_usd(curr_tax),
        hist_avg=_fmt_usd(avg_tax),
        forecast_avg=f"~{_fmt_usd(proj_tax)}" if proj_tax else "—",
        peak=_fmt_usd(peak_t[1]) if peak_t else "—",
        peak_when=str(peak_t[0]) if peak_t else "—",
        trough=_fmt_usd(trough_t[1]) if trough_t else "—",
        trough_when=str(trough_t[0]) if trough_t else "—",
        note="Billing data available for 2025 only" if not tax_pts else "",
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

# ── TCAD internal numeric use code → (description, valuation_method) ──────────
# Source: TCAD improvement-level use codes from IMP_INFO.TXT.
# Field position [28:38] (10 chars, left-justified).  Loaded by backfill_classi_cd.py.
# Strategy: highest-value non-"00" improvement row per parcel is used as the
# property-level use code.  Tuple = (description, valuation_method).
# Loaded here for future use once that field is added to the schema.
# Key = numeric string as it appears in the TCAD export.
USE_CODE_LOOKUP = {
    # Residential — single-family / duplex / townhome / condo
    "01": ("Single-Family Residence",      "Cost"),
    "02": ("Duplex",                        "Cost"),
    "03": ("Triplex",                       "Income"),
    "04": ("Fourplex",                      "Income"),
    # Multi-family apartments
    "05": ("Apartment 5–25 Units",         "Income"),
    "06": ("Apartment 26–49 Units",        "Income"),
    "07": ("Apartment 50–100 Units",       "Income"),
    "08": ("Apartment 100+ Units",         "Income"),
    "09": ("Special Residential (F-V)",    "Income"),
    # Manufactured / mobile home
    "10": ("Manufactured Commercial Bldg", "Cost"),
    "11": ("Mobile Home — Single (PP)",    "Cost"),
    "12": ("Mobile Home — Double (PP)",    "Cost"),
    "13": ("Mobile Home — Single (Real)",  "Cost"),
    "14": ("Mobile Home — Double (Real)",  "Cost"),
    # Attached residential
    "15": ("Condominium (Stacked)",        "Cost"),
    "16": ("Townhome",                      "Cost"),
    "17": ("Clubhouse",                     "Cost"),
    "19": ("Special (No Depreciation)",    "Cost"),
    # Small retail / garage apt
    "20": ("Small Store (<10,000 SF)",     "Income"),
    "21": ("Garage Apartment",             "Cost"),
    "22": ("Hi-Rise Condo / Apartment",    "Income"),
    # Office condos / industrial campus
    "23": ("Small Office Condo",           "Income"),
    "24": ("Commercial Space Condos",      "Income"),
    "26": ("Large Office Condo",           "Income"),
    "27": ("Major Industrial — Office",    "Cost"),
    "28": ("Major Industrial — Eng.",      "Cost"),
    "29": ("Major Industrial — Mfg.",      "Cost"),
    # Retail — strip centers / restaurants / hotels
    "30": ("Strip Center (<10,000 SF)",    "Income"),
    "31": ("Night Club / Bar",             "Income"),
    "32": ("Restaurant",                   "Income"),
    "33": ("Fast Food Restaurant",         "Income"),
    "34": ("Hotel — Full Service",         "Income"),
    "35": ("Hotel — Limited Service",      "Income"),
    "37": ("Motel — Extended Stay",        "Income"),
    "39": ("Restaurant (SFR Conversion)",  "Income"),
    # Shopping centers / big-box retail
    "40": ("Regional Shopping Center",     "Income"),
    "41": ("Community Shopping Center",    "Income"),
    "42": ("Neighborhood Shopping Center", "Income"),
    "43": ("Strip Center (>10,000 SF)",    "Income"),
    "44": ("Grocery Store",                "Income"),
    "45": ("Dept. Store (>25,000 SF)",     "Income"),
    "46": ("Discount Store (>25,000 SF)",  "Income"),
    "47": ("Retail Store",                 "Income"),
    "48": ("Convenience Store",            "Income"),
    "49": ("Bed & Breakfast",              "Income"),
    # Office
    "50": ("Office Hi-Rise (≥6 Stories)",  "Income"),
    "51": ("Office Large (>35,000 SF)",    "Income"),
    "52": ("Office Medium (10–35,000 SF)", "Income"),
    "53": ("Office Small (<10,000 SF)",    "Income"),
    "54": ("Medical Office (<10,000 SF)",  "Income"),
    "55": ("Medical Office (>10,000 SF)",  "Income"),
    "56": ("Bank — Office",                "Income"),
    "57": ("Bank — Drive-Thru",            "Income"),
    "58": ("Bank — Branch Office",         "Income"),
    "59": ("Office / Retail (SFR Conv.)",  "Income"),
    # Industrial / warehouse
    "60": ("Industrial 20K+ SF (<25% FO)", "Cost"),
    "61": ("Warehouse (<20,000 SF)",       "Cost"),
    "63": ("Mini-Warehouse / Self-Storage","Income"),
    "64": ("Industrial 20K+ SF (25–49%)",  "Cost"),
    "65": ("Industrial 20K+ SF (50–74%)",  "Cost"),
    "66": ("Industrial 20K+ SF (>75% FO)", "Cost"),
    "67": ("Computer / Data Center",       "Income"),
    "68": ("Transit Warehouse",            "Cost"),
    "69": ("Mfg / Eng / Lab Industrial",   "Cost"),
    # Institutional / special use
    "70": ("Religious Facility",           "Cost"),
    "72": ("Fraternity / Sorority",        "Cost"),
    "73": ("Dormitory",                    "Cost"),
    "74": ("Dormitory Hi-Rise",            "Cost"),
    "76": ("Retirement Center",            "Cost"),
    "77": ("Hospital",                     "Income"),
    "78": ("Day Care Center",              "Income"),
    # Auto / service
    "80": ("Auto Dealership",              "Income"),
    "81": ("Service Station",              "Income"),
    "82": ("Self-Service (Car Wash Booth)","Income"),
    "83": ("Service / Repair Garage",      "Income"),
    "84": ("Mini-Lube / Tune-Up",          "Income"),
    "86": ("Car Wash — Full Service",      "Income"),
    # Misc
    "87": ("Parking Garage",               "Income"),
    "88": ("Treatment / Rehab Center",     "Cost"),
    "89": ("Assisted Living Center",       "Income"),
    "90": ("Theater",                      "Income"),
    "91": ("Mortuary / Funeral Home",      "Income"),
    "92": ("Country Club",                 "Income"),
    "93": ("Bowling Center",               "Income"),
    "94": ("Health Club",                  "Income"),
    "95": ("Marina",                       "Income"),
    "96": ("Classroom / School",           "Cost"),
    "98": ("Leasehold — Exempt Property",  "N/A"),
    "108": ("Luxury Hi-Rise Apts 100+",   "Income"),
    "120": ("Additional Living Quarter",   "Cost"),
    "483": ("Accessory Dwelling Unit",     "Cost"),
}

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


def use_code_case_sql(classi_col="p.classi_cd", fallback_label="Other"):
    """SQL CASE expression mapping classi_cd -> its USE_CODE_LOOKUP
    description (e.g. '01' -> 'Single-Family Residence'), for `fallback_label`
    when classi_cd is NULL/unrecognized.

    Built for the Market Snapshot "By Property Type" per-sector breakdown
    (Issue B investigation, July 2026). Each sector's ptype_case used to be a
    hand-rolled CASE assuming two-character state_cd1 sub-prefixes like
    'A1%%'/'A2%%'/'A4%%' for Residential, 'B1%%'..'B4%%' for Multi-Family,
    'C1%%'/'C2%%' for Land.

    CORRECTION #1 (flagged by Diego): an earlier version of this comment
    claimed those sub-codes "don't exist" in the Comptroller taxonomy --
    that was wrong. STATE_CD_DESCRIPTIONS (above, sourced from Comptroller
    Rule 9.4001) shows A1/A2/A3/A4/A5/A9, B1-B5, C1/C2, D1-D3, E1-E3, F1-F5
    etc. are all real, official two-character codes. That first correction
    reframed the open question as "are they valid" (yes) vs. "does Travis
    County's parcel.state_cd1 column actually populate at that granularity"
    (unknown at the time).

    CORRECTION #2 (live data, per Diego's check_other_property_type_fix.py
    Section 0 run): that second question is now settled for Commercial --
    state_cd1 IS populated at two-character granularity there: F1 (14,660
    parcels), F2 (472), L1 (41,310), L2 (1,194), 57,000+ real parcels with
    genuine sub-codes. So for Commercial specifically, the old
    state_cd1-sub-prefix approach this function replaced was never
    "impossible" -- it would have worked, just at coarser granularity
    (2 buckets: "Commercial Improved" / "Commercial Land or RE" instead of
    4 real sub-codes). Residential/Multi-Family/Land/Agricultural's actual
    granularity is still pending the same live check for those prefixes --
    don't assume the Commercial finding generalizes without checking.

    Given that, classi_cd is used here not because state_cd1 sub-codes
    don't work, but because it's the MORE DESCRIPTIVE grouping field for
    this specific breakdown: even where a real Comptroller sub-code exists,
    it doesn't carry a use-code description a homeowner/investor would
    recognize the way USE_CODE_LOOKUP does, and it wouldn't match
    /api/benchmark/meta's use_codes_by_type (Search's Use Code filter),
    which is the specific reuse Diego asked for.

    classi_cd (the TCAD numeric use code, populated from IMP_INFO.TXT and
    already displayed on the property-detail page) is the real subtype
    signal that actually exists in this data -- it's exactly what
    /api/benchmark/meta's use_codes_by_type groups by for Search's Use Code
    filter. Reusing the same USE_CODE_LOOKUP descriptions here means a
    sector's breakdown table and its Use Code filter can never show
    different subtypes for the same underlying data.

    Vacant land and some agricultural parcels genuinely have no improvement
    record (classi_cd is NULL by design -- see KNOWN_LIMITATIONS.md's
    "classi_cd source" note), so those sectors legitimately collapsing
    toward fallback_label for a large share of parcels is expected, real
    behavior, not a bug -- unlike Residential/Commercial/Multi-Family, which
    have populated classi_cd for the large majority of parcels.
    """
    def _sql_escape(s):
        """Escape a literal string for embedding in an f-string SQL that
        will be passed through cur.execute(sql, params) -- even with an
        empty/None params tuple, psycopg2 still runs %-style substitution
        over the whole query string, so any bare '%' in embedded text (not
        just quotes) has to be doubled to '%%' or it gets misread as a
        format placeholder. Root-caused by Diego: four USE_CODE_LOOKUP
        descriptions contain a literal '%' (classi_cd 60/64/65/66, e.g.
        "Industrial 20K+ SF (25-49%)") and were crashing query_no_nestloop()
        with "IndexError: tuple index out of range" -- psycopg2 counting
        the stray '%' as an extra substitution slot against the empty
        params tuple. Quotes need doubling for the same reason CASE/THEN
        string literals always do (an embedded ' would otherwise close the
        SQL string early)."""
        return s.replace("'", "''").replace("%", "%%")

    whens = "\n".join(
        f"""                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) = '{code}' """
        f"""THEN '{_sql_escape(desc)}'"""
        for code, (desc, _method) in USE_CODE_LOOKUP.items()
    )
    fb = _sql_escape(fallback_label)
    return f"""CASE
{whens}
                ELSE '{fb}'
            END"""


# ═══════════════════════════════════════════════════════════════════════════
# MARKET SNAPSHOT — SCOPED 8-SECTOR TAXONOMY (July 2026)
#
# THIS IS AN INTENTIONAL, SCOPED EXCEPTION — NOT AN OVERSIGHT, AND NOT THE
# SAME "two classifiers drifted apart" BUG FOUND AND FIXED THREE TIMES THIS
# SESSION (api_benchmark_meta()'s hand-rolled CASE, _PTYPE_SC1_FILTER's stale
# label dict, and Issue B's assumed-but-nonexistent state_cd1 sub-prefixes).
# Those were accidental duplicates of the SAME canonical classification that
# silently fell out of sync. This is different: it is a SECOND, DELIBERATELY
# SEPARATE classification system, used ONLY for Market Snapshot's own tab
# routing and breakdown display, because Diego wants Market Snapshot to show
# a finer split (Retail vs. Industrial vs. Office vs. Hotel) than the
# canonical 5-category system is designed to express.
#
# What stays on the canonical 5-category system, UNTOUCHED by anything below:
#   - tax_logic/classify.py (property_type_label, label_case_sql,
#     _STATE_PREFIX_LABEL, MULTI_FAMILY_CODES, COMMERCIAL_CODES)
#   - The global nav sector dropdown (templates/base.html)
#   - Search's Property Type filter (templates/search.html, /api/benchmark/meta)
#   - loaders/compute_metrics.py's county_benchmark table
#   - property_detail()'s bench_label (property.html's Homeowner-mode gating,
#     "How You Compare" peer group, etc.)
# Anyone reusing the SNAPSHOT_*_CODES constants / _snapshot_taxonomy_sql()
# outside app.py's /snapshot, /snapshot/neighborhood/<code>, and /parcels
# routes is almost certainly reaching for the wrong function — reach for
# classify.py's label_case_sql() / property_type_label() instead.
#
# Starting point: the project's original pre-5-category documentation
# researched a classi_cd-based 4-bucket split (Multi-Family / Commercial-
# Retail / Industrial / Hospitality-Other). That was real prior research, not
# a guess, but (a) it was a 4-bucket split and Diego wants Commercial/Retail
# divided into Retail vs. Office, and Hospitality/Other divided into Hotel
# vs. Other, and (b) cross-checking it against the live USE_CODE_LOOKUP
# descriptions below turned up real problems in the old doc, not just gaps:
#   - codes "107", "36", "38" don't exist in USE_CODE_LOOKUP at all (typos /
#     stale references from before the current use-code table was built) --
#     omitted here, not guessed at.
#   - "37" (Motel — Extended Stay) was listed in BOTH the old doc's
#     Commercial/Retail bucket AND its Hospitality/Other bucket -- an
#     internal contradiction in the source doc. The real description is
#     unambiguously lodging, so it's classified Hotel here, resolving the
#     contradiction with evidence rather than picking one arbitrarily.
#   - Office had NO bucket at all in the old 4-way doc -- codes 23/26/50-59
#     (the "Office condos" / "Office" comment groups in USE_CODE_LOOKUP)
#     would have fallen through to the state_cd1 F/L "Commercial" fallback
#     under the old scheme. Added here as their own Office bucket since
#     that's the whole point of this round's split.
#   - The old doc's Hospitality/Other bucket (34,35,37,92,95,96) actually
#     splits cleanly on real evidence: 34/35/37 are genuine lodging (Hotel);
#     92 (Country Club), 95 (Marina), 96 (Classroom/School) are not lodging
#     at all and land in Other instead.
#
# Every USE_CODE_LOOKUP code (not just the ones in the old doc / today's
# MULTI_FAMILY_CODES / COMMERCIAL_CODES) is classified below, evidence-first
# from its real description -- leaving codes unmapped here would just
# recreate the exact "hidden Other bucket" bug this session already found
# and fixed once (Issue A), one level down. See the accompanying report for
# the full code -> tab mapping table with real descriptions, for review as
# a real classification decision.
#
# JUDGMENT CALLS — RESOLVED (August 2026): the two judgment calls originally
# flagged here, plus a handful of related single-code placements, were sent
# to Diego as a full raw USE_CODE_LOOKUP export (every code, every
# description, no grouping) for individual manual review. Diego confirmed 9
# explicit moves against the original proposal below; everything else in
# this file's original proposal was confirmed correct as-is. These are now
# reviewed, deliberate decisions, not open questions:
#   - 02 (Duplex) stays Residential; 03 (Triplex) and 04 (Fourplex) move to
#     Multi-Family -- Diego's own split within the old "judgment call #1"
#     group, not a full move of all three the way the original proposal's
#     two options framed it.
#   - 17 (Clubhouse) moves from Multi-Family to Other.
#   - 10 (Manufactured Commercial Bldg) moves from Retail to Other.
#   - 24 (Commercial Space Condos) moves from Retail to Office.
#   - Old "judgment call #2" (Auto/service codes 80-86) is resolved as:
#     80 (Auto Dealership) and 86 (Car Wash Full Service) stay Retail
#     (unchanged); 81 (Service Station) moves from Industrial to Other;
#     82 (Self-Service Car Wash Booth), 83 (Service/Repair Garage), and 84
#     (Mini-Lube/Tune-Up) move from Industrial to Retail. Industrial no
#     longer contains any of the six Auto/service codes.
# 09 (Special Residential, F-V), 76 (Retirement Center), 89 (Assisted
# Living), and 78 (Day Care Center) were part of the same review and
# confirmed to stay exactly where the original proposal had them
# (Multi-Family/Multi-Family/Multi-Family/Retail respectively) -- reviewed
# and confirmed, not carried forward as open questions either.
# ═══════════════════════════════════════════════════════════════════════════

SNAPSHOT_RESIDENTIAL_CODES = (
    "01",   # Single-Family Residence
    "02",   # Duplex -- reviewed and confirmed Residential (see note above)
    "11", "12", "13", "14",  # Mobile Home Single/Double, PP + Real
    "15",   # Condominium (Stacked)
    "16",   # Townhome
    "21",   # Garage Apartment
    "120",  # Additional Living Quarter
    "483",  # Accessory Dwelling Unit
)

SNAPSHOT_MULTIFAMILY_CODES = (
    "03", "04",  # Triplex, Fourplex -- moved here from Residential per Diego's review
    "05", "06", "07", "08",  # Apartment 5-25 / 26-49 / 50-100 / 100+ Units
    "09",   # Special Residential (F-V) -- reviewed and confirmed
    "22",   # Hi-Rise Condo / Apartment
    "72", "73", "74",  # Fraternity/Sorority, Dormitory, Dormitory Hi-Rise
    "76",   # Retirement Center -- reviewed and confirmed
    "89",   # Assisted Living Center -- reviewed and confirmed
    "108",  # Luxury Hi-Rise Apts 100+
    "SYNUP",  # synthetic/aggregated multi-family upgrade (not a real TCAD code)
)

SNAPSHOT_RETAIL_CODES = (
    "20",   # Small Store (<10,000 SF)
    "30", "31", "32", "33", "39",  # Strip Center, Night Club/Bar, Restaurant, Fast Food, Restaurant(SFR Conv)
    "40", "41", "42", "43", "44", "45", "46",  # Shopping centers, Grocery, Dept/Discount Store
    "47", "48",  # Retail Store, Convenience Store
    "78",   # Day Care Center -- reviewed and confirmed Retail
    "80",   # Auto Dealership -- unchanged, see resolved Auto/service note above
    "82", "83", "84",  # Car Wash Booth, Repair Garage, Mini-Lube -- moved here from Industrial per Diego's review
    "86",   # Car Wash Full Service -- unchanged
    "90",   # Theater
    "91",   # Mortuary / Funeral Home
    "93",   # Bowling Center
    "94",   # Health Club
    "4RS",  # synthetic retail code (not a real TCAD code)
)

SNAPSHOT_OFFICE_CODES = (
    "23", "26",  # Small / Large Office Condo
    "24",   # Commercial Space Condos -- moved here from Retail per Diego's review
    "50", "51", "52", "53",  # Office Hi-Rise / Large / Medium / Small
    "54", "55",  # Medical Office Small / Large
    "56", "57", "58",  # Bank Office / Drive-Thru / Branch Office
    "59",   # Office / Retail (SFR Conv.)
)

SNAPSHOT_INDUSTRIAL_CODES = (
    "27", "28", "29",  # Major Industrial -- Office/Eng./Mfg. (see note: "Office" in the
                        # name refers to a support building within a major industrial
                        # property class, not a standalone office building)
    "60", "61", "63", "64", "65", "66",  # Industrial 20K+ SF tiers, Warehouse, Mini-Warehouse/Self-Storage
    "67",   # Computer / Data Center
    "68",   # Transit Warehouse
    "69",   # Mfg / Eng / Lab Industrial
    # 81/82/83/84 (Auto/service) all moved out per Diego's review -- see
    # resolved Auto/service note above. No Auto/service codes remain here.
)

SNAPSHOT_HOTEL_CODES = (
    "34", "35",  # Hotel Full/Limited Service
    "37",   # Motel Extended Stay (resolves old doc's internal contradiction, see above)
    "49",   # Bed & Breakfast
)

# Explicit classi_cd -> Other: institutional/civic/leisure use codes that are
# real, recognized TCAD categories but don't cleanly sort into any of the
# other 6 classi_cd-driven buckets above. Combined with the canonical
# unclassified state_cd1 residual (O/G/J, see classify.py) and any F/L
# state_cd1 parcel whose classi_cd doesn't land in any bucket above, this is
# the full "Other" tab per Diego's definition -- one tab, not split further.
SNAPSHOT_OTHER_CODES = (
    "10",   # Manufactured Commercial Bldg -- moved here from Retail per Diego's review
    "17",   # Clubhouse -- moved here from Multi-Family per Diego's review
    "19",   # Special (No Depreciation) -- too vague to sort confidently
    "70",   # Religious Facility
    "77",   # Hospital
    "81",   # Service Station -- moved here from Industrial per Diego's review
    "87",   # Parking Garage
    "88",   # Treatment / Rehab Center
    "92",   # Country Club
    "95",   # Marina
    "96",   # Classroom / School
    "98",   # Leasehold -- Exempt Property
)

# ─── Land/Vacant + Agricultural within-sector subtype breakdown (August 2026) ───
#
# FINDING: Land/Vacant and Agricultural have no meaningful classi_cd subtype
# data, and this is structural, not a data-quality gap. classi_cd is sourced
# entirely from IMP_INFO.TXT (see loaders/backfill_classi_cd.py's own
# docstring: "classi_cd = TCAD internal improvement use code ... Source:
# IMP_INFO.TXT") -- it only exists for parcels that have an IMPROVEMENT (a
# building) on file. Every one of the 91 USE_CODE_LOOKUP descriptions
# describes a building type (Single-Family Residence, Warehouse, Hotel...);
# there is no "vacant lot" or "open pasture" entry because IMP_INFO.TXT has
# no row to produce one from. A vacant Land parcel, by definition, has
# nothing built on it -- so it structurally has no classi_cd, not a missing
# or mis-tagged one. This is exactly why use_code_case_sql() collapsed both
# sectors to a single ELSE row identical to the grand total (confirmed by
# Diego's live run: 24,113 / 5,763 parcels, 1 row each) -- there was never a
# real per-code subtype signal underneath to group by for these two sectors,
# unlike Residential/Commercial/etc. where classi_cd IS reliably populated.
# Agricultural parcels CAN carry a classi_cd (a barn or farmhouse on ag land
# does have an improvement row), so it may not be quite as uniformly empty
# as Land/Vacant -- see the diagnostic script's new NULL-rate section for
# the real live numbers; either way it's not the reliable, well-populated
# signal it is for the other sectors.
#
# ALTERNATIVE, REAL DIMENSION: parcel.land_sqft. Already loaded (not new --
# see loaders/load_parcel_attrs.py, sourced from LAND_DET.TXT, and already
# used elsewhere in this app: the /parcels drill-through's land_min/land_max
# filter, property.html's Land Size field), and explicitly documented by its
# own loader as "RELIABLE ... always square feet regardless of the parcel's
# pricing unit (SF / AC / LOT / FF)". Size (acreage) is also a genuinely
# meaningful way land and agricultural parcels are actually discussed and
# compared -- not a fabricated category. Used here as a size-TIER breakdown
# in place of a use-code breakdown for these two sectors only.
#
# Tier boundaries are reasoned defaults (same discipline as
# SNAPSHOT_SUBTYPE_CAP=7), not measured against the real live distribution
# (no DB access this round either -- see Part 0 in the report). Agricultural
# tiers are set coarser than Land/Vacant's on the reasoning that open-space
# ag valuation (1-d-1) typically applies to larger tracts than a residential
# vacant lot -- Diego should sanity-check both against the real per-tier
# counts the extended diagnostic script now prints, and adjust the
# boundaries below if the live distribution says otherwise.
#
# Format: ascending list of (upper_bound_sqft, label). The LAST entry's
# upper_bound is ignored (it's the catch-all/largest tier) -- so it can be
# None for clarity. 1 acre = 43,560 SF.
SNAPSHOT_LAND_SIZE_TIERS = (
    (10_890,    "Under 1/4 Acre"),        # < 0.25 ac -- typical small residential/urban lot
    (21_780,    "1/4 - 1/2 Acre"),        # 0.25-0.5 ac
    (43_560,    "1/2 - 1 Acre"),          # 0.5-1 ac
    (217_800,   "1 - 5 Acres"),           # 1-5 ac
    (871_200,   "5 - 20 Acres"),          # 5-20 ac
    (None,      "20+ Acres"),             # catch-all
)

SNAPSHOT_AG_SIZE_TIERS = (
    (217_800,   "Under 5 Acres"),         # < 5 ac
    (871_200,   "5 - 20 Acres"),          # 5-20 ac
    (2_178_000, "20 - 50 Acres"),         # 20-50 ac
    (8_712_000, "50 - 200 Acres"),        # 50-200 ac
    (None,      "200+ Acres"),            # catch-all
)


def _size_tier_case_sql(land_col, tiers):
    """SQL CASE expression bucketing `land_col` (a land_sqft-style numeric
    column) into the ascending (upper_bound_sqft, label) tiers above.
    NULL land_sqft (no LAND_DET.TXT row for this parcel) gets its own
    honest 'Size Not Available' label rather than being silently dropped
    into whichever tier a NULL comparison happens to fall through to."""
    whens = "\n                ".join(
        f"WHEN {land_col} < {upper} THEN '{label}'"
        for upper, label in tiers if upper is not None
    )
    catch_all_label = tiers[-1][1]
    return f"""CASE
                WHEN {land_col} IS NULL THEN 'Size Not Available'
                {whens}
                ELSE '{catch_all_label}'
            END"""


def _snapshot_taxonomy_sql(classi_col="p.classi_cd", state_col="p.state_cd1"):
    """SQL CASE expression for Market Snapshot's scoped 8-tab-plus-Other
    taxonomy (see the SNAPSHOT_*_CODES constants and the large comment block
    above). classi_cd overrides first (evidence-based sector assignment),
    then state_cd1 fallback for parcels with no recognized classi_cd
    override, matching the same fallback structure classify.py uses for the
    canonical 5-category system -- but this is NOT classify.py's
    label_case_sql(); the two are deliberately separate and can legitimately
    disagree about a given parcel's bucket (e.g. a stacked condo with
    classi_cd='15' lands in Residential here regardless of state_cd1, where
    it might land in Commercial under the canonical system if its state_cd1
    happens to be 'F'/'L' and it has no MULTI_FAMILY_CODES/COMMERCIAL_CODES
    override there). That divergence is expected and scoped to Market
    Snapshot's own display -- it does not change property_type_label() or
    any other canonical-classifier consumer.

    F/L (Commercial-by-state_cd1) parcels whose classi_cd doesn't land in
    Retail/Industrial/Office/Hotel above fall through to 'Other' here --
    there is no "generic Commercial" tab in this taxonomy to catch them, and
    guessing which of the 4 they are without a real classi_cd would be
    exactly the kind of invented-fact this session's discipline is against.
    """
    def _in_list(codes):
        return ", ".join(f"'{c}'" for c in codes)

    return f"""CASE
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_RESIDENTIAL_CODES)}) THEN 'Residential'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_MULTIFAMILY_CODES)}) THEN 'Multi-Family'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_RETAIL_CODES)}) THEN 'Retail'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_OFFICE_CODES)}) THEN 'Office'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_INDUSTRIAL_CODES)}) THEN 'Industrial'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_HOTEL_CODES)}) THEN 'Hotel'
                WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({_in_list(SNAPSHOT_OTHER_CODES)}) THEN 'Other'
                WHEN LEFT(UPPER({state_col}), 1) IN ('A', 'M') THEN 'Residential'
                WHEN LEFT(UPPER({state_col}), 1) = 'B'          THEN 'Multi-Family'
                WHEN LEFT(UPPER({state_col}), 1) = 'C'          THEN 'Land/Vacant'
                WHEN LEFT(UPPER({state_col}), 1) IN ('D', 'E')  THEN 'Agricultural'
                ELSE 'Other'
            END"""


_SNAPSHOT_TAB_ORDER = (
    "Residential", "Multi-Family", "Retail", "Industrial", "Office", "Hotel",
    "Land/Vacant", "Agricultural", "Other",
)


def _snapshot_taxonomy_sort_case_sql(label_expr):
    """Sort-order CASE for the Overall tab's own breakdown table, using the
    same fixed sector order as the new tab bar (_SNAPSHOT_TAB_ORDER).
    Mirrors classify.py's label_sort_case_sql() pattern, but for this
    module's separate 9-way Market Snapshot taxonomy -- not a duplicate of
    that function, a parallel one scoped to this taxonomy."""
    whens = "\n".join(
        f"            WHEN ({label_expr}) = '{lbl}' THEN {i + 1}"
        for i, lbl in enumerate(_SNAPSHOT_TAB_ORDER)
    )
    return f"CASE\n{whens}\n            ELSE 99\n        END"


# The 8 new Market-Snapshot-only sector tabs (Overall + these 8 + Other = the
# 10 tabs on the page). Deliberately a SEPARATE dict from
# _SNAPSHOT_VIEW_PROP_TYPE_LABEL (canonical 5-category, kept below unchanged)
# -- that dict, and the "commercial" view value it still recognizes, stay
# fully intact so the untouched nav sector dropdown (templates/base.html,
# links to /snapshot?view=commercial) and Search's canonical Property Type
# -> Snapshot link (search.html's SNAPSHOT_VIEW_BY_LABEL) keep working
# exactly as before, even though the new tab bar itself no longer shows a
# "Commercial" button (superseded by Retail/Industrial/Office/Hotel).
_SNAPSHOT_SECTOR_VIEWS = {
    "residential":  "Residential",
    "multifamily":  "Multi-Family",
    "retail":       "Retail",
    "industrial":   "Industrial",
    "office":       "Office",
    "hotel":        "Hotel",
    "land":         "Land/Vacant",
    "agricultural": "Agricultural",
    "other":        "Other",
}

# Full set of valid /snapshot ?view= values: "overall" + the 8 new tabs +
# "other" (all via _SNAPSHOT_SECTOR_VIEWS) + the legacy "commercial" view
# (old deep links only, see _snapshot_view_where()'s docstring). Shared by
# county_snapshot() and snapshot_neighborhood() so the two routes can never
# disagree about which view values are valid.
_SNAPSHOT_VALID_VIEWS = {"overall", "commercial"} | set(_SNAPSHOT_SECTOR_VIEWS)

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


app = Flask(__name__)
app.secret_key = config.FLASK_SECRET


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
    return {"mode": _resolve_mode()}


@app.after_request
def persist_mode(resp):
    """When ?mode= is present and valid, remember it for 30 days."""
    m = (request.args.get("mode") or "").strip().lower()
    if m in _MODES:
        resp.set_cookie(_MODE_COOKIE, m, max_age=30 * 24 * 3600, samesite="Lax")
    return resp


# ── DB helper ─────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS,
    )


def query(sql, params=None, one=False):
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def query_no_nestloop(sql, params=None, one=False):
    """
    Like query(), but disables Nested Loop plans for this one query via a
    transaction-scoped `SET LOCAL enable_nestloop = off`.

    Reserved ONLY for the three Market Snapshot queries in
    _compute_snapshot_data() (breakdown, Part 4 aggregate, neighborhoods)
    that each join parcel_tax_year twice — once for tax_year=2025, once for
    tax_year=2026. On this deployment (Postgres 15), the planner
    consistently mis-chooses a Nested Loop doing ~407,000 individual
    per-row index probes against parcel_tax_year_pkey for that second join,
    instead of a Hash/Merge Join — confirmed NOT to be a cache-timing
    illusion by running each query 4x (on/off/on/off) via
    task_staging/snapshot_perf/check_snapshot_nestloop_off.command: forcing
    the join off beat the planner's own choice every single time, at every
    position in the run order:
        breakdown:      480-1489ms (off)  vs 3008-9644ms (on, planner default)
        Part 4 aggregate: 299-535ms (off) vs  974-2491ms (on)
        neighborhoods:    361-362ms (off) vs 2382-2393ms (on) — this one
            especially clean: the "on" plan was rock-steady ~2.4s on both
            of its runs, so there's no cache-order ambiguity to explain away.
    This is NOT a blanket "nested loop is always bad" opinion, and it must
    not be copy-pasted onto other queries without the same on/off
    measurement — for a query where Postgres's own Nested Loop choice is
    actually correct, this override would make things slower, not faster.
    It is intentionally scoped two ways so it can't leak beyond its
    purpose: (1) SET LOCAL only affects the current transaction, never the
    session or server; (2) this helper opens its own connection and is
    never committed — the connection is closed (implicit rollback) right
    after fetching results, so nothing persists beyond this one query call.
    Do not "clean this up" into a session-wide `SET enable_nestloop = off`
    or apply it to query() generally — see the investigation history above
    before touching this.
    """
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL enable_nestloop = off")
            cur.execute(sql, params or ())
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    error = None

    if q:
        geo_id = normalize_parcel_id(q)

        # Try exact geo_id match first
        parcel = query(
            "SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True
        )

        # Fall back to prop_id match (user entered the short integer)
        if not parcel and q.isdigit():
            parcel = query(
                "SELECT * FROM parcel WHERE prop_id = %s", (int(q),), one=True
            )

        if parcel:
            return redirect(url_for("property_detail", geo_id=parcel["geo_id"]))

        # Address-like query (contains letters) — show disambiguation list
        elif any(c.isalpha() for c in q):
            q_norm = " ".join(q.upper().split())
            addr_matches = query("""
                SELECT geo_id, situs_address, owner_name
                FROM   parcel
                WHERE  UPPER(situs_address) ILIKE %(pattern)s
                ORDER  BY situs_address
                LIMIT  20
            """, {"pattern": f"%{q_norm}%"})
            if addr_matches:
                return render_template(
                    "index.html",
                    q=q,
                    error=None,
                    addr_matches=[dict(r) for r in addr_matches],
                )
            error = (
                f"No parcels found matching address \"{q}\". "
                "Try a shorter street name or use the 10-digit TCAD account number. "
            )

        else:
            error = (
                f"We couldn't find a parcel matching \"{q}\". "
                "Double-check the format — the 10-digit TCAD account number works most reliably. "
                "The 14-digit Tax Office account and short prop_id integer are also accepted."
            )

    return render_template("index.html", q=q, error=error)


@app.route("/search")
def search_page():
    """Task 13 — dedicated search page with a US coverage map (visual only).
    Not an interactive GIS map; just communicates current coverage (Travis County)."""
    return render_template("search.html")


@app.route("/parcel/<geo_id>")
def property_detail(geo_id):
    # Core parcel
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
    if not parcel:
        return render_template(
            "index.html",
            q=geo_id,
            error=(
                f"We couldn't find parcel \"{geo_id}\". "
                "Double-check the format — the 10-digit TCAD account number works most reliably."
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
        LEFT JOIN tax_billing   tb  ON tb.geo_id   = pty.geo_id
                                   AND tb.tax_year = pty.tax_year
        WHERE  pty.geo_id = %s
        ORDER  BY pty.tax_year
    """, (geo_id,))

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
        LEFT JOIN county_tax_rate  ctr_prev ON ctr_prev.entity_code = tbe.entity_code
                                           AND ctr_prev.tax_year    = 2024
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id,))

    # Delinquency
    delinquent = query(
        "SELECT * FROM tax_delinquent WHERE geo_id = %s", (geo_id,), one=True
    )

    # Current year snapshot (2025)
    current = next((r for r in history if r["tax_year"] == 2025), None)

    # If total_tax is NULL in tax_billing (common when TOTAL_TAX field is blank in
    # TaxCurOpenData), derive it from the sum of entity amounts.
    #
    # Provenance tag (Estimate-as-Fact investigation, July 2026): this sum
    # silently skips any entity with a NULL amount_due (`if e["amount_due"]`),
    # so it can understate the true total when entity-level data is
    # incomplete -- and until now, nothing downstream could tell this figure
    # apart from a single authoritative tax_billing.total_tax value. Every
    # consumer (Homeowner mode's "What you paid in taxes" card, Investor
    # mode's Value History table) treated it identically to real, complete
    # billing data. total_tax_derived is the provenance marker that lets
    # templates apply the correct confidence framing instead of assuming
    # "present in total_tax" == "fully confirmed real bill" -- same
    # provenance-tracking principle as billing_source == 'portal_scrape'
    # already gets a Partial badge, this fallback should too.
    #
    # NOTE (flagged, not fixed here): `current` is the same dict object as
    # the matching row inside `history`, so this key also becomes visible to
    # the Investor mode Value History table's 2025 row -- which currently
    # does NOT check it, and unconditionally badges every 2025 row
    # "Verified" regardless of provenance. That's a real, separate masking
    # bug in Investor mode, out of scope for this Homeowner-mode-copy brief
    # -- flagged in the report rather than silently changed.
    if current is not None and not current.get("total_tax") and entity_detail:
        derived_tax = sum(e["amount_due"] for e in entity_detail if e["amount_due"])
        if derived_tax:
            current["total_tax"] = derived_tax
            current["total_tax_derived"] = True

    # Historical combined tax rate for this parcel's entities (for trend projection)
    rate_history = query("""
        SELECT ctr.tax_year, SUM(ctr.rate) AS total_rate
        FROM   county_tax_rate ctr
        WHERE  ctr.entity_code IN (
                   SELECT entity_code FROM tax_billing_entity
                   WHERE  geo_id = %s AND tax_year = 2025
               )
        AND    ctr.tax_year BETWEEN 2021 AND 2025
        GROUP  BY ctr.tax_year
        ORDER  BY ctr.tax_year
    """, (geo_id,))

    # ── Computed historical tax (feature flag: COMPUTED_HIST_TAX_ENABLED) ────────
    # When enabled, rows where total_tax is NULL (2021–2024 without billing data)
    # receive a computed estimate: taxable_value × combined_rate / 100.
    # Stored as computed_total_tax (separate key) — never overwrites real billing data.
    # Label: "computed from certified value × rate; billing unconfirmed"
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
        WHERE  ctr.entity_code IN (
                   SELECT entity_code FROM tax_billing_entity
                   WHERE  geo_id = %s AND tax_year = 2025
               )
        AND    ctr.tax_year BETWEEN 2016 AND 2025
        ORDER  BY ctr.entity_code, ctr.tax_year
    """, (geo_id,))

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
            "SELECT * FROM parcel_metrics WHERE geo_id = %s ORDER BY tax_year", (geo_id,)
        ):
            metrics_by_year[m["tax_year"]] = m

        # classi_cd-first (Task 1): pull the benchmark row matching the parcel's
        # *actual* use, not just its state_cd1 prefix.
        bench_label = property_type_label(parcel.get("classi_cd"), parcel.get("state_cd1"))
        if bench_label:
            for b in query("""
                SELECT * FROM county_benchmark
                WHERE property_type_label = %s ORDER BY tax_year
            """, (bench_label,)):
                benchmark_by_year[b["tax_year"]] = b
    except Exception:
        pass  # Phase 2 tables not yet populated — skip metrics sections

    # 2026 preliminary row (for the preliminary callout card)
    current_2026 = next((r for r in history if r["tax_year"] == 2026), None)

    # Estimated 2026 total tax: taxable_value_2026 × blended 2025 entity rates
    # Uses this parcel's specific entity mix (not county-wide avg) for accuracy.
    # Only computed when taxable_value is available for 2026 — never falls back to MV.
    estimated_tax_2026 = None
    assumed_rate_2026 = None
    if current_2026 and current_2026.get("taxable_value") and entity_detail:
        tv26 = current_2026["taxable_value"]
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
        kpi["market_value_source"] = "preliminary"
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
    elif current_2026 and current_2026.get("assessed_value") and current_2026.get("market_value"):
        kpi["assessment_ratio"]      = round(current_2026["assessed_value"] / current_2026["market_value"] * 100, 1)
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
    elif insights and insights.get("total_rate_2025"):
        # Fallback: if no billing data, show the combined rate as an approximation
        kpi["rate_approx"] = round(float(insights["total_rate_2025"]), 4)

    # Est. 2026 ETR (Estimated badge) — only when we could estimate 2026 tax.
    if est_etr_2026 is not None:
        kpi["effective_tax_rate_2026_est"] = est_etr_2026

    # ── Narrative + annual trends ──────────────────────────────────────────────
    narrative     = generate_property_narrative(parcel, history, metrics_by_year,
                                                benchmark_by_year, insights, projections)
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
        hs_potential_savings = _tx_hs_savings(
            entity_detail, current.get("assessed_value") if current else None
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
        rate_history=rate_history,
        current=current,
        current_2026=current_2026,
        entity_detail=entity_detail,
        delinquent=delinquent,
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
        narrative=narrative,
        annual_trends=annual_trends,
        hs_potential_savings=hs_potential_savings,
    )



# Display order for the Rate Trends entity selector's category groups
# (Part 3 — see categorize_entity() docstring for how membership is decided).
ENTITY_CATEGORY_ORDER = ["School District", "County", "City", "Hospital District", "MUD/WCID", "Other"]


def categorize_entity(code, name):
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
    """
    n = (name or "").upper()
    if "ISD" in n:
        return "School District"
    if n == "TRAVIS COUNTY":
        return "County"
    if "CITY OF" in n or "VILLAGE OF" in n:
        return "City"
    if "HEALTH" in n:                 # THD = "Travis Central Health", the county hospital district
        return "Hospital District"
    if "MUD" in n or "WCID" in n or "WSID" in n or "UTILITY DISTRICT" in n:
        return "MUD/WCID"
    return "Other"   # ESDs, road districts, limited districts, disannexed
                      # entries, Austin Community College, and anything else
                      # that doesn't match a bucket above.


@app.route("/rates")
def tax_rates():
    """Tax rate trend page — county-level, no parcel required."""
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
    rates = query("""
        SELECT entity_code, entity_name, tax_year, rate
        FROM   county_tax_rate
        ORDER  BY entity_code, tax_year
    """)

    # Build {entity_code: [{year, rate}, …]} structure for JS
    by_entity = {}
    entity_names = {}
    for r in rates:
        code = r["entity_code"]
        entity_names[code] = r["entity_name"]
        by_entity.setdefault(code, []).append({
            "year": r["tax_year"],
            "rate": float(r["rate"]) if r["rate"] else None,
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
                "category": categorize_entity(code, entity_names[code]),
            }
            for code in by_entity.keys()
        ],
        key=lambda e: (category_rank.get(e["category"], 999), e["name"] or "", e["code"]),
    )
    entity_category = {e["code"]: e["category"] for e in all_entities}

    return render_template(
        "rates.html",
        by_entity_json=json.dumps(by_entity),
        entity_names_json=json.dumps(entity_names),
        entity_category_json=json.dumps(entity_category),
        all_entities=all_entities,
        entity_category_order=ENTITY_CATEGORY_ORDER,
        key_entities=KEY_ENTITIES,
        year_min=year_min,
        year_max=year_max,
        default_year_from=default_year_from,
    )


@app.route("/api/parcel_entities")
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
    parcel = query(
        "SELECT geo_id, situs_address FROM parcel WHERE geo_id = %s", (geo_id,), one=True
    )
    if not parcel and q.isdigit():
        parcel = query(
            "SELECT geo_id, situs_address FROM parcel WHERE prop_id = %s", (int(q),), one=True
        )
    if not parcel:
        return jsonify({"ok": False, "error": f"No parcel found matching \"{q}\"."})

    rows = query("""
        SELECT DISTINCT entity_code
        FROM   tax_billing_entity
        WHERE  geo_id = %s AND tax_year = 2025
    """, (parcel["geo_id"],))
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
# CANONICAL_PARCEL_EXCL — verified against loaders/compute_metrics.py's
# BENCHMARK_EXCLUDE_PREFIXES = ["X", "N"] (the filter
# compute_county_benchmarks() itself uses) plus its own
# "AND p.geo_id NOT LIKE 'AJR%%'" — matches /api/benchmark's excl_filter
# exactly (commented there as "mirrors compute_metrics.py").
CANONICAL_PARCEL_EXCL = "AND p.state_cd1 NOT LIKE 'X%%' AND p.state_cd1 NOT LIKE 'N%%' AND p.geo_id NOT LIKE 'AJR%%'"

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


@app.route("/snapshot")
def county_snapshot():
    """County Market Snapshot — 2026 preliminary vs 2025 certified.
    Supports ?view=overall|residential|multifamily|retail|industrial|office|
    hotel|land|agricultural|other (the 10 tabs), plus the legacy
    ?view=commercial (default: overall) -- see _SNAPSHOT_VALID_VIEWS.
    """
    view = request.args.get("view", "overall")
    if view not in _SNAPSHOT_VALID_VIEWS:
        view = "overall"
    # Homeowner mode only sees residential home values.
    if _resolve_mode() == "homeowner":
        view = "residential"

    # _compute_snapshot_data(view) is purely a function of `view` (and
    # current DB state) — mode only changes which template text renders,
    # not the query results — so it's safe to cache by view alone, shared
    # across homeowner/investor mode.
    cached = _SNAPSHOT_CACHE.get(view)
    if cached and (time.time() - cached["ts"]) < _SNAPSHOT_CACHE_TTL_SECONDS:
        payload = cached["payload"]
    else:
        payload = _compute_snapshot_data(view)
        _SNAPSHOT_CACHE[view] = {"payload": payload, "ts": time.time()}

    return render_template("snapshot.html", view=view, **payload)


def _compute_snapshot_data(view):
    """
    Runs the Market Snapshot queries for one sector view. Split out from
    county_snapshot() so the route can short-circuit via the cache above
    without duplicating this logic.

    Part 1 performance fix: `rows` (per-property-type breakdown) and
    `totals` (the grand-total row) used to be two separate, near-duplicate
    queries, each independently JOINing parcel_tax_year's 2025 and 2026
    rows for the whole sector — the same expensive join computed twice per
    request. Confirmed via check_snapshot_perf.command: TOTALS alone took
    ~840ms on a Nested Loop plan (407,967 loops) while ROWS used an
    efficient Hash Join for the same shape of work. They're now ONE query
    using GROUP BY GROUPING SETS ((ptype, sort_key), ()) — one pass over
    the joined data produces both the per-sector breakdown AND the single
    grand-total row (ptype IS NULL marks the total row; split out below).

    Investigating the merge surfaced a second, real bug, not just a
    performance one: the two queries' parcel-eligibility filters weren't
    actually identical. ROWS excluded only state_cd1 X* (plus N* only for
    the "overall" view, via the old view_where special-case) and filtered
    AJR* on the parcel table; TOTALS hardcoded X* + N* and filtered AJR* on
    the tax_year table instead. They happened to produce the same row count
    in the check_snapshot_perf.command run — by coincidence of current data
    (no N*-prefix parcel fell in the tested view), not by logical
    equivalence. For the 5 non-"overall" sector views, ROWS was NOT
    excluding the 3 known N*-prefix personal-property accounts that TOTALS
    was, meaning the per-sector breakdown and the grand total could
    silently disagree by those parcels whenever one fell in-sector.

    The correct, canonical filter — confirmed against the actual source of
    truth rather than picked ad hoc between the two ad hoc versions — is
    loaders/compute_metrics.py's BENCHMARK_EXCLUDE_PREFIXES = ["X", "N"]
    (the exact set compute_county_benchmarks() uses to build
    county_benchmark itself, with documented parcel counts and reasoning
    per prefix) plus its own "AND p.geo_id NOT LIKE 'AJR%%'". This is also
    exactly what /api/benchmark's excl_filter already uses, commented there
    as "mirrors compute_metrics.py" — so this brings /snapshot in line with
    both. Applied unconditionally below (canonical_excl), not per-view, so
    "overall" no longer needs its own N-exclusion special-case in
    view_where.
    """
    # classi_cd-first membership (Task 1): a parcel is placed by its actual
    # improvement use (apartments -> Multi-Family) before its state_cd1 prefix.
    # NOTE: ptype_case/sort_case below reference "p." (the parcel table
    # alias used directly in the flattened breakdown query, Part 1 round 3
    # fix). They previously referenced "y25." back when this data came
    # through an intermediate y25 CTE; p.state_cd1/p.classi_cd are the same
    # columns the old y25 CTE passed through unchanged, so this is a
    # rename only, not a logic change.
    #
    # view_where is now computed once via the shared _snapshot_view_where()
    # helper (module-level, near CANONICAL_PARCEL_EXCL above) rather than
    # per-branch below, so snapshot_neighborhood()'s route can reuse the
    # exact same view -> property-type scoping instead of re-deriving it.
    view_where = _snapshot_view_where(view)
    # Issue B fix (July 2026): the five sector branches below used to be
    # hand-rolled CASEs assuming two-character state_cd1 sub-prefixes
    # (A1/A2/A4, B1-B4, C1/C2, ...).
    #
    # CORRECTION (Diego caught this via check_other_property_type_fix.py's
    # Section 0 live output): an earlier version of this comment claimed
    # those sub-prefixes "don't exist in the real data" -- that's factually
    # wrong for Commercial specifically. state_cd1 IS populated at
    # two-character granularity there: F1 (14,660 parcels), F2 (472), L1
    # (41,310), L2 (1,194) -- over 57,000 real parcels with genuine,
    # populated sub-codes. The old Commercial branch (LEFT(state_cd1,1)='F'
    # / 'L') would in fact have worked fine at the 1-character level it
    # checked; it just collapsed F1/F2 and L1/L2 into two coarse buckets
    # ("Commercial Improved" / "Commercial Land / RE") instead of the four
    # real sub-codes. (Residential/Multi-Family/Land/Agricultural's actual
    # granularity is still pending the same live check -- don't assume the
    # Commercial finding generalizes to them without checking.)
    #
    # So the justification for switching to classi_cd here is NOT "the old
    # approach was impossible" -- for Commercial it plainly wasn't. It's
    # that the real TCAD use code (classi_cd) is a MORE DESCRIPTIVE
    # breakdown than state_cd1 sub-codes would be even where those exist:
    # classi_cd is what /api/benchmark/meta's use_codes_by_type already
    # groups by for Search's Use Code filter (specific use descriptions like
    # "Office Small (<10,000 SF)", not just "Commercial Improved"), so
    # reusing it here means a sector's breakdown table and its Use Code
    # filter show the same subtypes for the same data, and every sector
    # (not just the ones with populated state_cd1 sub-codes) gets a
    # consistent, genuinely descriptive breakdown -- vacant land and some
    # agricultural parcels have no classi_cd at all (see
    # use_code_case_sql()'s docstring), so state_cd1 sub-codes may still be
    # the better signal for those specific sectors once Section 0's numbers
    # are in; flagging that as worth a follow-up look, not deciding it here.
    #
    # sort_case reuses ptype_case itself (not a numeric placeholder) for
    # these dynamically-discovered-subtype views, since there's no fixed
    # canonical order the way the 5-category "overall" branch has one.
    # IMPORTANT: do not replace this with a bare integer literal like "0" --
    # Postgres treats a bare integer constant inside GROUP BY / GROUPING
    # SETS as an ordinal reference to a SELECT-list column position (see
    # https://www.postgresql.org/docs/current/queries-table-expressions.html:
    # "the name or ordinal number of an output column ... or an arbitrary
    # expression"), and "0" isn't a valid position (1-based), which is
    # exactly what crashed this query with "GROUP BY position 0 is not in
    # select list" -- confirmed against the Postgres docs and a matching
    # failure report in another ORM (linq2db#4349, same class of bug: "const
    # is part of grouping" hitting ordinal-position parsing). Reusing
    # ptype_case is a real expression, never an integer literal, so it can
    # never be misparsed as a position reference -- and since it's the exact
    # same expression already in the grouping tuple, grouping by
    # (ptype_case, ptype_case) is identical in effect to grouping by
    # (ptype_case) alone; no behavior change beyond fixing the crash. These
    # views sort by parcel count instead of sort_key, see order_by_expr
    # below.
    order_by_expr = "sort_key NULLS LAST"
    # Part 1 (this round): the 8 new Market-Snapshot-scoped sector tabs, plus
    # "Other", replace the old 5-branch if/elif below. "commercial" is kept
    # as its own legacy branch (byte-identical to before this round) for old
    # deep links -- see _snapshot_view_where()'s docstring. "overall" now
    # groups by the new taxonomy too, for consistency with the tab bar it
    # sits above (see that branch's own comment).
    if view in _SNAPSHOT_SECTOR_VIEWS:
        sector_label = _SNAPSHOT_SECTOR_VIEWS[view]
        fallback = "Uncategorized" if sector_label == "Other" else f"Other {sector_label}"
        # bench_trends source: county_benchmark (compute_metrics.py,
        # untouched) only has the canonical 5-category labels. Retail/
        # Industrial/Office/Hotel all borrow the canonical "Commercial"
        # trend -- the real historical data available at that granularity
        # covers the whole Commercial category, not this specific sub-tab;
        # the template must caveat this explicitly, not present it as
        # sub-tab-specific history. "Other" has no canonical equivalent at
        # all (a residual across several canonical categories, not one of
        # them) -- no trend shown, honestly, rather than guessing which
        # canonical bucket to borrow from.
        bench_labels = {
            "Residential":  ["Residential"],
            "Multi-Family": ["Multi-Family"],
            "Retail":       ["Commercial"],
            "Industrial":   ["Commercial"],
            "Office":       ["Commercial"],
            "Hotel":        ["Commercial"],
            "Land/Vacant":  ["Land/Vacant"],
            "Agricultural": ["Agricultural"],
            "Other":        [],
        }[sector_label]
        # Land/Ag fix (August 2026): classi_cd is structurally absent for
        # vacant land (it's sourced entirely from IMP_INFO.TXT -- only
        # parcels WITH a building improvement get one; see the big comment
        # above SNAPSHOT_LAND_SIZE_TIERS for the full evidence). use_code_
        # case_sql() collapsed both sectors to one ELSE row identical to the
        # grand total for exactly that reason. These two sectors use
        # land_sqft size tiers instead of a use-code breakdown; every other
        # sector is unaffected.
        if view == "land":
            ptype_case = _size_tier_case_sql("p.land_sqft", SNAPSHOT_LAND_SIZE_TIERS)
        elif view == "agricultural":
            ptype_case = _size_tier_case_sql("p.land_sqft", SNAPSHOT_AG_SIZE_TIERS)
        else:
            ptype_case = use_code_case_sql("p.classi_cd", fallback)
        sort_case = ptype_case
        order_by_expr = "n_parcels DESC NULLS LAST"
    elif view == "commercial":
        # Legacy view -- unchanged from before this round, kept working for
        # old deep links (nav dropdown, Search's canonical Property Type
        # filter). Not one of the 10 tabs on the page. Still subject to the
        # same Part 2 subtype cap applied below (after the query), so a user
        # who does reach it via an old link doesn't see the ~40-row wall of
        # subtypes this session already fixed once for the new tabs.
        bench_labels  = ["Commercial"]
        ptype_case = use_code_case_sql("p.classi_cd", "Other Commercial")
        sort_case = ptype_case
        order_by_expr = "n_parcels DESC NULLS LAST"
    else:  # overall
        # view_where is already "" for "overall" via _snapshot_view_where()
        # above (N* exclusion lives in canonical_excl instead, applied
        # unconditionally to every view).
        #
        # Part 1 (this round): Overall's own "By Property Type" breakdown now
        # uses the SAME new 9-way Market Snapshot taxonomy as the 8 sector
        # tabs beneath it, not the old 5-category canonical split. Kept
        # consistent deliberately -- showing Overall in the old 5-category
        # scheme right next to a tab bar of 9 different sectors would
        # reproduce the exact "two classifiers disagree" confusion this
        # session has spent all day finding and fixing, one level up.
        # bench_trends below still pulls the full canonical 5-category set --
        # that's a different chart (the multi-year county trend), and the
        # canonical categories are still the right granularity for a
        # multi-year historical comparison; only THIS breakdown table
        # switched taxonomies.
        bench_labels  = ["Residential", "Multi-Family", "Commercial", "Land/Vacant", "Agricultural"]
        _ov_tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
        ptype_case = _ov_tax
        sort_case  = _snapshot_taxonomy_sort_case_sql(_ov_tax)

    # Canonical parcel-eligibility filter — now the module-level
    # CANONICAL_PARCEL_EXCL (see its docstring above), so snapshot_neighborhood()
    # reuses the exact same constant rather than a second copy of this
    # literal. Applied uniformly below to every query in this function —
    # the rows/totals merge, the Part 4 aggregate query, and the
    # neighborhoods query.
    canonical_excl = CANONICAL_PARCEL_EXCL

    # Part 1 performance fix — full history, corrected as later rounds
    # falsified earlier hypotheses:
    #   - Round 2: merged rows+totals into one query via GROUPING SETS.
    #     Real fix, held up.
    #   - Round 3: hypothesized the CTE structure itself was the problem
    #     and flattened it to a plain FROM/JOIN ("Variant B"). This turned
    #     out to be a NO-OP: Postgres 15 auto-inlines non-recursive,
    #     singly-referenced CTEs by default, so the CTE and flat forms are
    #     byte-identical in plan cost. The apparent "35-45% win" was a
    #     cache-warming artifact from running EXPLAIN ANALYZE variants
    #     back-to-back in one session — confirmed wrong when a fresh,
    #     standalone run reproduced the original slow timings exactly.
    #   - Round 4: identified the REAL bottleneck — Postgres's planner
    #     consistently chooses a Nested Loop with ~407,000 individual
    #     per-row index probes against parcel_tax_year_pkey for the second
    #     (tax_year=2026) join, instead of a Hash/Merge Join. Confirmed via
    #     task_staging/snapshot_perf/check_snapshot_nestloop_off.command:
    #     each query run 4x (on/off/on/off) to rule out cache-order bias.
    #     Forcing the join off beat the planner's own choice every time:
    #     breakdown 480-1489ms (off) vs 3008-9644ms (on); Part 4 aggregate
    #     299-535ms (off) vs 974-2491ms (on); neighborhoods 361-362ms (off)
    #     vs 2382-2393ms (on, rock-steady both runs — no cache ambiguity).
    #     Fix: query_no_nestloop() (defined near query(), see its docstring
    #     for the full scoping rationale) applies a transaction-scoped
    #     SET LOCAL enable_nestloop = off to exactly these three queries —
    #     not a session- or server-wide change, and not to be copied onto
    #     other queries without the same on/off verification.
    # rows (per-property-type breakdown) and totals (grand-total row) stay
    # merged via GROUPING SETS ((ptype, sort_key), ()) — one pass over the
    # join produces both; that part of the round-2 fix held up throughout.
    # A real ptype value is never NULL (every ptype_case branch above has
    # an ELSE / COALESCE), so "ptype IS NULL" unambiguously identifies the
    # total row below.
    breakdown = query_no_nestloop(f"""
        SELECT
            ({ptype_case})                                                                  AS ptype,
            ({sort_case})                                                                    AS sort_key,
            COUNT(*)                                                                        AS n_parcels,
            SUM(CASE WHEN t26.market_value > t25.market_value THEN 1 ELSE 0 END)            AS n_up,
            SUM(CASE WHEN t26.market_value < t25.market_value THEN 1 ELSE 0 END)            AS n_down,
            SUM(CASE WHEN t26.market_value = t25.market_value THEN 1 ELSE 0 END)            AS n_flat,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS median_pct,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS p25_pct,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (
                ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
            )::NUMERIC * 100, 2)                                                            AS p75_pct,
            ROUND(SUM(t25.market_value)::NUMERIC / 1e9, 3)                                  AS total_mv25_b,
            ROUND(SUM(t26.market_value)::NUMERIC / 1e9, 3)                                  AS total_mv26_b
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          {canonical_excl}
          {view_where}
        GROUP BY GROUPING SETS ((({ptype_case}), ({sort_case})), ())
        ORDER BY {order_by_expr}
    """)

    rows = [r for r in breakdown if r["ptype"] is not None]
    # Part 2 fix: cap the within-tab subtype breakdown to the top
    # SNAPSHOT_SUBTYPE_CAP rows by parcel count for sector-scoped views
    # (the new 8 tabs, plus the legacy "commercial" view) -- see
    # _cap_subtype_rows()'s docstring for exactly what is and isn't safe to
    # combine across the rolled-up rows. Not applied to "overall": that
    # breakdown is already just the 9 sector labels themselves (one row per
    # tab), never more than SNAPSHOT_SUBTYPE_CAP+2 rows, so there's nothing
    # to cap.
    if view in _SNAPSHOT_SECTOR_VIEWS or view == "commercial":
        # Land/Ag fix: their "fallback" concept is the largest size tier's
        # own label ("20+ Acres" / "200+ Acres"), not an "Other <Sector>"
        # use-code bucket -- matters only if the tier lists above ever grow
        # past SNAPSHOT_SUBTYPE_CAP (currently they're 6/5 tiers + "Size Not
        # Available" = 7/6 rows max, so capping is a no-op today either way).
        if view == "land":
            _fallback_label = SNAPSHOT_LAND_SIZE_TIERS[-1][1]
        elif view == "agricultural":
            _fallback_label = SNAPSHOT_AG_SIZE_TIERS[-1][1]
        else:
            _fallback_label = "Uncategorized" if view == "other" else f"Other {_SNAPSHOT_SECTOR_VIEWS.get(view) or 'Commercial'}"
        rows = _cap_subtype_rows(rows, _fallback_label)
    _total_row = next((r for r in breakdown if r["ptype"] is None), None)
    totals = None
    if _total_row:
        totals = {
            "n_total":      _total_row["n_parcels"],
            "n_up":         _total_row["n_up"],
            "n_down":       _total_row["n_down"],
            "n_flat":       _total_row["n_flat"],
            "total_mv25_b": _total_row["total_mv25_b"],
            "total_mv26_b": _total_row["total_mv26_b"],
            "median_pct":   _total_row["median_pct"],
        }

    # ── County Benchmark Annual Trends for the selected view ─────────────────
    # Pull from county_benchmark for the relevant property_type_label(s).
    bench_trends = []
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
            WHERE property_type_label IN ({fmt_labels})
            ORDER BY tax_year, property_type_label
        """)

    # ── New aggregate features (Part 4) — all read-only aggregation over
    # data that's already computed/loaded; no new pipeline. Same
    # canonical_excl/view_where scoping as the breakdown query above, so
    # "in the current sector" means the identical population already shown
    # in the table.
    #
    # Round 4 performance fix: this query was the single biggest cost on
    # the page (4322ms). Confirmed via check_snapshot_nestloop_off.command
    # that the cause is the planner's Nested Loop misjudgment on the
    # tax_year=2026 join (see query_no_nestloop()'s docstring for the full
    # on/off evidence) — not the CTE-vs-flat syntax. Uses
    # query_no_nestloop() rather than query() for exactly this reason.
    new_construction_count = 0
    risk_flagged_count = 0
    if totals:
        agg = query_no_nestloop(f"""
            SELECT
                -- "Recent" = same cutoff as the Search page's New Construction
                -- Quick Filter (runQuickFilter() in search.html): tax_year - 3.
                -- Here tax_year is this page's own preliminary year, 2026, so
                -- 2026 - 3 = 2023 — reusing that exact rule, not a new cutoff.
                COUNT(*) FILTER (WHERE p.year_built >= 2023)              AS n_new_construction,
                COUNT(*) FILTER (WHERE pm.risk_large_value_jump = TRUE)   AS n_risk_flagged
            FROM parcel p
            JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
            JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
            LEFT JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = 2026
            WHERE t25.market_value > 0
              AND t26.market_value > 0
              {canonical_excl}
              {view_where}
        """, one=True)
        if agg:
            new_construction_count = int(agg["n_new_construction"] or 0)
            risk_flagged_count = int(agg["n_risk_flagged"] or 0)

    # Top/bottom moving neighborhoods within the current sector. county_benchmark
    # has no neighborhood_cd column (confirmed via schema.sql — it's county-wide
    # only, PRIMARY KEY county_code/tax_year/property_type_label), so this is a
    # new read aggregation grouped by neighborhood_cd, not a new data pipeline.
    # HAVING COUNT(*) >= 10 excludes tiny neighborhoods (a 2-parcel neighborhood
    # with one outlier would otherwise dominate the "biggest mover" list with a
    # noisy, not-representative swing) — a judgment call, flagged rather than
    # silently baked in.
    #
    # Round 4 performance fix: isolated at 2122ms via
    # check_snapshot_other_queries.command — the same planner Nested Loop
    # misjudgment as the other two queries (query_no_nestloop()'s docstring
    # has the full on/off evidence). This one was the cleanest signal:
    # the default plan was rock-steady ~2.4s across repeated runs, so
    # there's no cache-order ambiguity in that comparison.
    top_neighborhoods = []
    bottom_neighborhoods = []
    if totals:
        nb_rows = query_no_nestloop(f"""
            SELECT
                p.neighborhood_cd,
                COUNT(*) AS n_parcels,
                ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY (t26.market_value - t25.market_value)::FLOAT / t25.market_value
                )::NUMERIC * 100, 2) AS median_pct
            FROM parcel p
            JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
            JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
            WHERE t25.market_value > 0
              AND t26.market_value > 0
              {canonical_excl}
              AND p.neighborhood_cd IS NOT NULL AND p.neighborhood_cd != ''
              {view_where}
            GROUP BY p.neighborhood_cd
            HAVING COUNT(*) >= 10
            ORDER BY median_pct DESC
        """)
        if nb_rows:
            top_neighborhoods = [dict(r) for r in nb_rows[:5]]
            bottom_neighborhoods = [dict(r) for r in nb_rows[-5:]][::-1]

    return {
        "rows": rows,
        "totals": totals,
        "bench_trends": bench_trends,
        "new_construction_count": new_construction_count,
        "risk_flagged_count": risk_flagged_count,
        "subtype_cap": SNAPSHOT_SUBTYPE_CAP,
        "top_neighborhoods": top_neighborhoods,
        "bottom_neighborhoods": bottom_neighborhoods,
    }


@app.route("/snapshot/neighborhood/<code>")
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
    that DO need it. Measured via
    task_staging/neighborhood_drilldown/check_neighborhood_drilldown_perf.command:
    for this query, Nested Loop is 15-100x FASTER than forcing it off
    (3-5ms vs 79-367ms), the opposite of those other three. The difference
    is selectivity: this query filters to one neighborhood_cd via an index
    first, narrowing to ~79 rows before the two-year join, where an
    indexed point-lookup Nested Loop is the correct, fast plan — unlike
    the whole-county queries that fix targeted, where the planner's own
    Nested Loop choice was the actual problem. query_no_nestloop() exists
    for a specific, measured misjudgment, not as a general "always avoid
    Nested Loop" switch — see its docstring, which warns against exactly
    this kind of over-generalization.
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
            ({ptype_case}) AS prop_type,
            (t26.market_value - t25.market_value)::FLOAT / t25.market_value * 100 AS pct_chg,
            COUNT(*) OVER() AS total_count
        FROM parcel p
        JOIN parcel_tax_year t25 ON t25.geo_id = p.geo_id AND t25.tax_year = 2025
        JOIN parcel_tax_year t26 ON t26.geo_id = p.geo_id AND t26.tax_year = 2026
        WHERE t25.market_value > 0
          AND t26.market_value > 0
          AND p.neighborhood_cd = %(code)s
          {CANONICAL_PARCEL_EXCL}
          {view_where}
        ORDER BY pct_chg DESC
        LIMIT {SEARCH_FILTER_PAGE_SIZE} OFFSET %(offset)s
    """, params={"code": code, "offset": offset})

    total = int(rows[0]["total_count"]) if rows else 0
    total_pages = (total + SEARCH_FILTER_PAGE_SIZE - 1) // SEARCH_FILTER_PAGE_SIZE if total else 0
    parcels = [dict(r) for r in rows]

    return render_template(
        "snapshot_neighborhood.html",
        code=code,
        view=view,
        view_prop_type=view_prop_type,
        page=page,
        total=total,
        total_pages=total_pages,
        parcels=parcels,
    )


@app.route("/api/rates")
def api_rates():
    """JSON endpoint for rate data (for dynamic chart filtering)."""
    rates = query("""
        SELECT entity_code, entity_name, tax_year, rate
        FROM   county_tax_rate
        WHERE  tax_year >= 2006
        ORDER  BY entity_code, tax_year
    """)
    return jsonify([dict(r) for r in rates])


@app.route("/api/benchmark")
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
    # Exclude non-real-property accounts from all live benchmark queries (mirrors compute_metrics.py).
    # Only X (exempt) and N (personal property, 3 parcels) excluded from state_cd1.
    # M (manufactured homes) and O (other real property) are kept — confirmed real property in Travis CAD.
    # AJR* geo_ids = personal property supplement accounts loaded from AJR (not real estate); excluded.
    excl_filter = "AND p.state_cd1 NOT LIKE 'X%%' AND p.state_cd1 NOT LIKE 'N%%' AND p.geo_id NOT LIKE 'AJR%%'"

    if classi_cd and classi_cd != "all":
        # ── On-the-fly aggregation by classi_cd ──────────────────────────
        params_cc = [year, classi_cd]
        if neighborhood:
            params_cc.append(neighborhood)
        row = query(f"""
            SELECT
                COUNT(*)                                                               AS n_parcels,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value)           AS median_market_value,
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY t.market_value)          AS p25_market_value,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY t.market_value)          AS p75_market_value
            FROM parcel p
            JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = %s
            WHERE p.classi_cd = %s
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
            prev_params = [prev_year, classi_cd]
            if neighborhood:
                prev_params.append(neighborhood)
            prev_row = query(f"""
                SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value) AS prev_med
                FROM parcel p
                JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = %s
                WHERE p.classi_cd = %s
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
                "is_preliminary": year == 2026,
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
            params_2026 = (neighborhood,) if neighborhood else None
            row = query(f"""
                SELECT
                    COUNT(*)                                                            AS n_parcels,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.market_value)        AS median_market_value,
                    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY t.market_value)       AS p25_market_value,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY t.market_value)       AS p75_market_value
                FROM parcel p
                JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2026
                WHERE ({like_parts})
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
                    "is_preliminary": True,
                })
            return jsonify({"ok": False, "error": "No 2026 preliminary data for this property type."})
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


@app.route("/api/benchmark/meta")
def api_benchmark_meta():
    """Return available property types and use codes with ≥10 parcels (for filter dropdowns)."""
    prop_types_raw = query("""
        SELECT DISTINCT property_type_label
        FROM county_benchmark WHERE tax_year = 2025
        ORDER BY property_type_label
    """)
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
        GROUP BY p.classi_cd, prop_type
        HAVING COUNT(*) >= 10
        ORDER BY prop_type, n DESC
    """)

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
        GROUP BY neighborhood_cd
        HAVING COUNT(*) >= 5
        ORDER BY n DESC
    """)
    total_parcels = query("SELECT COUNT(*) AS n FROM parcel", one=True)["n"]
    nb_non_null = query(
        "SELECT COUNT(*) AS n FROM parcel WHERE neighborhood_cd IS NOT NULL AND neighborhood_cd != ''",
        one=True
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


def _row_confidence(data_source):
    """Confidence tier for a single parcel_tax_year row, given its data_source.

    Mirrors the property page's per-year confidence badge logic exactly
    (templates/property.html, "Property-level confidence badge" block,
    ~line 572-593) — same certified/preliminary/else-Partial branching — so
    a parcel shown here reads the same confidence it would show on its own
    detail page. Not a shared call site (that logic is inline Jinja on the
    property page); if that block changes, this needs a matching update.

    Only 3 of the site's 5 confidence tiers are reachable from this function:
      - "verified"    — data_source == 'certified'
      - "preliminary" — data_source == 'preliminary' (2026 preliminary roll)
      - "partial"     — anything else non-null (e.g. 'ajr', 'ajr_2021',
                         'ajr_pir_2022', 'cert_2021', or legacy NULL rows —
                         see loaders/load_cert_2021.py, load_certified_historical.py)
    "not_available" is impossible here: this route INNER JOINs parcel_tax_year
    on the selected tax_year, so a result can only exist if a row is present
    (property.html's "Not Available" branch fires when NO row exists at all —
    structurally excluded from a result set that required a join match).
    "estimated" is also not produced by this function: on the property page,
    Estimated tags *computed/derived* figures (projections, billing-derived
    tax estimates) — never a stored parcel_tax_year row's own data_source.
    Since this route surfaces stored market_value/data_source directly (not
    a computed figure), there is no case where "estimated" legitimately
    applies to a row here under the site's existing confidence logic.
    """
    if data_source == "certified":
        return "verified"
    if data_source == "preliminary":
        return "preliminary"
    return "partial"


@app.route("/api/search_filter")
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
    tax_year        = _i("tax_year") or 2025
    page            = max(1, _i("page") or 1)

    if county != "travis":
        return jsonify({"ok": False, "error": f"Unknown county '{county}'. Only Travis County, TX is available today."}), 400

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
        large_value_jump, homestead, verified_only,
    ])
    if not has_real_filter:
        return jsonify({
            "ok": False,
            "error": ("Select at least one filter beyond County to run a search — County alone "
                      "would match all 508,000+ Travis County parcels."),
        }), 400

    where = ["1=1"]
    params = {"tax_year": tax_year}

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
            where.append("(pty.exemption_codes IS NULL OR pty.exemption_codes !~ %(hs_re)s)")

    if verified_only:
        # Direct mapping onto parcel_tax_year.data_source = 'certified' (the
        # actual stored confidence signal for values). Does not additionally
        # require verified BILLING data — see brief report for this scoping note.
        where.append("pty.data_source = 'certified'")

    where_sql = " AND ".join(where)
    offset = (page - 1) * SEARCH_FILTER_PAGE_SIZE
    params["offset"] = offset

    sql = f"""
        SELECT
            p.geo_id, p.situs_address, p.neighborhood_cd,
            ({_ptype_sql}) AS prop_type_label,
            pty.market_value, pty.data_source, pty.tax_year,
            COUNT(*) OVER() AS total_count
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = %(tax_year)s
        LEFT JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = %(tax_year)s
        WHERE {where_sql}
        ORDER BY p.situs_address NULLS LAST, p.geo_id
        LIMIT {SEARCH_FILTER_PAGE_SIZE} OFFSET %(offset)s
    """

    rows = query(sql, params)
    total = int(rows[0]["total_count"]) if rows else 0

    results = []
    for r in rows:
        confidence = _row_confidence(r["data_source"])
        results.append({
            "geo_id": r["geo_id"],
            "situs_address": r["situs_address"],
            "neighborhood_cd": r["neighborhood_cd"],
            "prop_type": r["prop_type_label"],
            "market_value": float(r["market_value"]) if r["market_value"] is not None else None,
            "tax_year": r["tax_year"],
            "confidence": confidence,
        })

    total_pages = (total + SEARCH_FILTER_PAGE_SIZE - 1) // SEARCH_FILTER_PAGE_SIZE if total else 0
    return jsonify({
        "ok": True,
        "results": results,
        "total": total,
        "page": page,
        "page_size": SEARCH_FILTER_PAGE_SIZE,
        "total_pages": total_pages,
    })


@app.route("/api/estimate_acq/<geo_id>")
def api_estimate_acq(geo_id):
    """
    Post-acquisition tax estimator API (Task 1).
    Query params:
      price  int   purchase price (required, no commas)
      buyer  str   'non_owner_occupant' (default) | 'owner_occupant'
    """
    price_raw    = request.args.get("price", "").strip().replace(",", "").replace("$", "")
    buyer_status = request.args.get("buyer", "non_owner_occupant").strip()
    rate_mode    = request.args.get("rate_mode", "certified").strip()

    if buyer_status not in ("non_owner_occupant", "owner_occupant"):
        buyer_status = "non_owner_occupant"
    if rate_mode not in ("certified", "projected"):
        rate_mode = "certified"

    if not price_raw or not re.fullmatch(r"\d+", price_raw):
        return jsonify({"ok": False, "error": "price must be a positive integer (no commas or $)"})

    purchase_price = int(price_raw)
    if purchase_price <= 0:
        return jsonify({"ok": False, "error": "price must be positive"})

    parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    current_yr_row = query("""
        SELECT market_value, assessed_value, taxable_value, hs_cap_loss, exemption_codes
        FROM   parcel_tax_year
        WHERE  geo_id = %s AND tax_year = 2025
    """, (geo_id,), one=True)

    if not current_yr_row or not current_yr_row.get("market_value"):
        return jsonify({"ok": False, "error": "No 2025 certified market value for this parcel"})

    entity_detail = query("""
        SELECT tbe.entity_code, ctr.entity_name, ctr.rate, tbe.amount_due
        FROM   tax_billing_entity tbe
        LEFT JOIN county_tax_rate ctr
               ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id,))

    if not entity_detail:
        return jsonify({"ok": False, "error": "No 2025 entity billing data for this parcel"})

    # Per-entity rate history (for the projected-rate scenario)
    codes = tuple({e["entity_code"] for e in entity_detail})
    entity_rate_history = {}
    if codes:
        for r in query(
            "SELECT entity_code, tax_year, rate FROM county_tax_rate "
            "WHERE entity_code IN %s AND tax_year >= 2016 ORDER BY tax_year",
            (codes,),
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
        WHERE geo_id = %s AND market_value IS NOT NULL AND tax_year <= 2026
        ORDER BY tax_year
    """, (geo_id,))
    market_growth = None
    pts = [(r["tax_year"], float(r["market_value"])) for r in mkt_hist if r["market_value"]]
    if len(pts) >= 2 and pts[0][1] > 0:
        span = pts[-1][0] - pts[0][0]
        if span > 0:
            cagr = (pts[-1][1] / pts[0][1]) ** (1.0 / span) - 1.0
            market_growth = max(-0.05, min(0.12, cagr))   # allow decline; mirror main projection

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



@app.route("/api/address_search")
def api_address_search():
    """
    Address typeahead API (Task 2).
    Returns up to 10 matching parcels for a partial address query.
    Query params:
      q   str   partial address string (min 3 chars)
    """
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "results": []})

    # Normalise: collapse whitespace, uppercase for consistent matching
    q_norm = " ".join(q.upper().split())

    # AJR* geo_ids = personal property supplement accounts loaded from AJR
    # (not real, situs-addressable property) — same convention used
    # elsewhere in this file (see the ~8 other `NOT LIKE 'AJR%%'` call
    # sites), excluded here too since this is an address lookup for real
    # parcels, not placeholder accounts.
    #
    # Relevance ranking: the match condition stays a substring ILIKE (so a
    # mid-address search like "Lamar" still works), but results where
    # situs_address STARTS WITH the query now rank above ones that only
    # contain it elsewhere, via a CASE-based sort key, before falling back
    # to alphabetical within each tier. Without this, e.g. "1201 S LAMAR
    # BLVD" (searching "1201") could rank behind any other address merely
    # containing "1201" somewhere, sorted alphabetically — which is exactly
    # what was happening before this fix.
    #
    # Note: geo_id/parcel-ID matching (normalize_parcel_id()) is a
    # completely separate code path used only by the "/" route's parcel-ID
    # search — this endpoint has never done geo_id matching and doesn't
    # need to preserve any such behavior here.
    #
    # pg_trgm index (idx_parcel_situs_trgm) will be used if installed;
    # ILIKE works correctly either way — just slower without the index.
    rows = query("""
        SELECT geo_id, situs_address, owner_name, state_cd1, neighborhood_cd
        FROM   parcel
        WHERE  UPPER(situs_address) ILIKE %(pattern)s
          AND  geo_id NOT LIKE 'AJR%%'
        ORDER  BY
            CASE WHEN UPPER(situs_address) LIKE %(prefix_pattern)s THEN 0 ELSE 1 END,
            situs_address
        LIMIT  10
    """, {"pattern": f"%{q_norm}%", "prefix_pattern": f"{q_norm}%"})

    results = [
        {
            "geo_id":       r["geo_id"],
            "address":      r["situs_address"] or "",
            "owner":        r["owner_name"] or "",
            "state_cd1":    r["state_cd1"] or "",
            "neighborhood": r["neighborhood_cd"] or "",
        }
        for r in rows
    ]
    return jsonify({"ok": True, "results": results})



@app.route("/api/peer_benchmark_local/<geo_id>")
def api_peer_benchmark_local(geo_id):
    """
    Neighborhood + type + size-band peer benchmark (Task 3).
    Peer set: same neighborhood_cd, same state_cd1 prefix, 2025 MV within ±50%.
    Returns peer count, median MV, p25/p75 MV, median total_tax, this parcel's rank.
    """
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    mv_row = query("""
        SELECT market_value FROM parcel_tax_year WHERE geo_id = %s AND tax_year = 2025
    """, (geo_id,), one=True)

    neighborhood = (parcel.get("neighborhood_cd") or "").strip()
    state_cd1    = (parcel.get("state_cd1") or "").strip()[:1]
    this_mv      = float(mv_row["market_value"]) if mv_row and mv_row.get("market_value") else None

    if not neighborhood or not this_mv:
        return jsonify({"ok": False, "error":
            "Peer benchmark requires neighborhood code and 2025 market value"})

    mv_lo = this_mv * 0.50
    mv_hi = this_mv * 1.50

    # Peer set: same neighborhood, same state_cd1 prefix, MV band ±50%
    # tb.total_tax is 0.00 (not NULL) for ~93% of 2025 tax_billing rows at the
    # source (see KNOWN_LIMITATIONS.md) — entity_tax_sum is the per-geo_id
    # SUM(amount_due) from tax_billing_entity, used as a fallback below so a
    # real, verified figure isn't silently dropped for the median/percentile.
    peers = query("""
        SELECT
            p.geo_id,
            pty.market_value,
            pty.assessed_value,
            tb.total_tax,
            tbe.entity_tax_sum
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
        LEFT JOIN tax_billing  tb  ON tb.geo_id  = p.geo_id AND tb.tax_year  = 2025
        LEFT JOIN (
            SELECT geo_id, SUM(amount_due) AS entity_tax_sum
            FROM   tax_billing_entity
            WHERE  tax_year = 2025
            GROUP  BY geo_id
        ) tbe ON tbe.geo_id = p.geo_id
        WHERE  p.neighborhood_cd = %(nb)s
          AND  LEFT(p.state_cd1, 1) = %(sc1)s
          AND  pty.market_value BETWEEN %(lo)s AND %(hi)s
          AND  p.geo_id NOT LIKE 'AJR%%'
          AND  pty.market_value > 0
        ORDER  BY pty.market_value
    """, {"nb": neighborhood, "sc1": state_cd1, "lo": mv_lo, "hi": mv_hi})

    n = len(peers)
    if n < 3:
        # Fallback: relax to neighborhood + type only, drop MV band
        peers = query("""
            SELECT p.geo_id, pty.market_value, pty.assessed_value,
                   tb.total_tax, tbe.entity_tax_sum
            FROM   parcel p
            JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
            LEFT JOIN tax_billing  tb  ON tb.geo_id  = p.geo_id AND tb.tax_year  = 2025
            LEFT JOIN (
                SELECT geo_id, SUM(amount_due) AS entity_tax_sum
                FROM   tax_billing_entity
                WHERE  tax_year = 2025
                GROUP  BY geo_id
            ) tbe ON tbe.geo_id = p.geo_id
            WHERE  p.neighborhood_cd = %(nb)s
              AND  LEFT(p.state_cd1, 1) = %(sc1)s
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  pty.market_value > 0
            ORDER  BY pty.market_value
        """, {"nb": neighborhood, "sc1": state_cd1})
        n = len(peers)
        band_note = "Size band relaxed (neighbourhood + type only — fewer than 3 ±50% MV peers)"
    else:
        band_note = f"Neighbourhood {neighborhood}, {state_cd1}-type, MV within ±50% of this parcel"

    if n == 0:
        return jsonify({"ok": False, "error": "No peers found in this neighbourhood + property type"})

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


@app.route("/api/peer_benchmark_sf/<geo_id>")
def api_peer_benchmark_sf(geo_id):
    """
    Per-SF peer benchmark (Task B).
    Peer set: same neighborhood_cd + state_cd1 prefix + living_area_sqft size band.
    Size band starts at ±40%; relaxes to ±60% then unconstrained if fewer than 5 peers.
    Returns assessed $/SF and market $/SF percentiles for this parcel vs peers.
    Parcels with null/zero living_area_sqft return ok=False with error='no_sf_basis'.
    """
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    parcel_data = query("""
        SELECT p.living_area_sqft,
               p.gross_building_area_sqft,
               pty.market_value,
               pty.assessed_value
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
        WHERE  p.geo_id = %s
    """, (geo_id,), one=True)

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
        return jsonify({"ok": False, "error": "No neighbourhood code for this parcel"})

    this_market_psf   = round(this_mv / sqft, 2) if this_mv   else None
    this_assessed_psf = round(this_av / sqft, 2) if this_av   else None

    # Progressively relax size band until ≥ 5 peers
    band_attempts = [0.40, 0.60, None]   # ±40%, ±60%, unconstrained
    peers = []
    band_note = ""

    for band in band_attempts:
        if band is not None:
            sqft_lo = sqft * (1.0 - band)
            sqft_hi = sqft * (1.0 + band)
            size_clause = "AND p.living_area_sqft BETWEEN %(sqft_lo)s AND %(sqft_hi)s"
            params = {
                "nb": neighborhood, "sc1": state_cd1,
                "sqft_lo": sqft_lo, "sqft_hi": sqft_hi,
            }
        else:
            size_clause = ""
            params = {"nb": neighborhood, "sc1": state_cd1}

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
            WHERE  p.neighborhood_cd  = %(nb)s
              AND  LEFT(p.state_cd1, 1) = %(sc1)s
              AND  p.living_area_sqft > 0
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  pty.market_value   > 0
              AND  pty.assessed_value > 0
              {size_clause}
            ORDER  BY p.living_area_sqft
        """, params)

        n = len(peers)
        if n >= 5:
            if band is not None:
                band_note = (
                    f"Neighbourhood {neighborhood}, {state_cd1}-type, "
                    f"SF within ±{int(band * 100)}% of {sqft:,.0f} SF"
                )
            else:
                band_note = (
                    f"Neighbourhood {neighborhood}, {state_cd1}-type, "
                    f"all SF sizes (size band relaxed — fewer than 5 peers in ±60% band)"
                )
            break

    n = len(peers)
    if n < 3:
        return jsonify({
            "ok": False,
            "error": "Fewer than 3 SF peers in this neighbourhood + property type",
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
_NEWS_QUERIES = {
    "homeowner":    "Travis County homestead exemption OR Austin property tax homeowner OR Austin school tax",
    "Residential":  "Travis County homestead exemption OR Austin residential property tax",
    "Multi-Family": "Austin multifamily property tax OR Austin apartment market",
    "Commercial":   "Travis County commercial property tax",
    "Land/Vacant":  "Travis County property tax TCAD",
    "Agricultural": "Travis County agricultural property tax",
}
_NEWS_GENERIC = "Travis County property tax OR Travis Central Appraisal District"


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
        return items
    except Exception:
        return None


@app.route("/api/news")
def api_news():
    """Real, property-type-aware Travis County property-tax news.

    ?type=<property_type_label> selects a tailored query (cached per type, not per
    parcel). Falls back to the generic query, then to an honest 'unavailable' —
    never fabricates headlines.
    """
    import time as _time
    ptype = (request.args.get("type", "") or "").strip()
    query = _NEWS_QUERIES.get(ptype, _NEWS_GENERIC)
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
    if not items and query != _NEWS_GENERIC:
        items = _cached(_NEWS_GENERIC) or _fetch_news(_NEWS_GENERIC)
        if items:
            _NEWS_CACHE[_NEWS_GENERIC] = {"ts": now, "items": items}
            query = _NEWS_GENERIC
    if not items:
        return jsonify({"ok": False, "error": "news_unavailable"})
    return jsonify({"ok": True, "items": items, "query_type": ptype or "generic"})


@app.route("/api/geocode/<geo_id>")
def api_geocode(geo_id):
    """Return {lat, lng} for a parcel — for the satellite map.

    Uses cached parcel.latitude/longitude when present; otherwise geocodes the
    situs address via the free U.S. Census geocoder (no key) and caches the
    result. Returns ok=False (no fabricated coordinates) on any failure.
    """
    import urllib.request, urllib.parse, json as _json
    row = query("SELECT latitude, longitude, situs_address FROM parcel WHERE geo_id = %s",
                (geo_id,), one=True)
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
                cur.execute("UPDATE parcel SET latitude=%s, longitude=%s WHERE geo_id=%s",
                            (lat, lng, geo_id))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return jsonify({"ok": True, "lat": lat, "lng": lng, "cached": False})
    except Exception as e:
        return jsonify({"ok": False, "error": "geocode_failed", "detail": str(e)[:100]})


@app.route("/api/peer_set/<geo_id>")
def api_peer_set(geo_id):
    """Task 7 — 5 comparable parcels for the Submarket Position section.

    Same classi_cd-first property type, same neighborhood (relaxed to same
    state_cd1 prefix in the neighborhood if too few), 2025 market value within
    ±25% of the subject. Excludes the subject and AJR* accounts.
    """
    parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
    if not parcel:
        return jsonify({"ok": False, "error": "Parcel not found"})

    subj = query("""
        SELECT pty.market_value
        FROM parcel_tax_year pty WHERE pty.geo_id = %s AND pty.tax_year = 2025
    """, (geo_id,), one=True)
    if not subj or not subj.get("market_value"):
        return jsonify({"ok": False, "error": "No 2025 market value for subject"})

    subj_label = property_type_label(parcel.get("classi_cd"), parcel.get("state_cd1"))
    nb         = (parcel.get("neighborhood_cd") or "").strip()
    sc1        = (parcel.get("state_cd1") or "").strip()[:1]
    subj_mv    = float(subj["market_value"])
    lbl_sql    = label_case_sql("p.classi_cd", "p.state_cd1")

    base_select = f"""
        SELECT p.geo_id, p.prop_id, p.situs_address, p.classi_cd,
               p.living_area_sqft, p.land_sqft, p.year_built,
               pty.market_value, pty.assessed_value,
               ROUND(pty.assessed_value::numeric / NULLIF(pty.market_value, 0), 4) AS assessment_ratio,
               (SELECT SUM(ctr.rate)
                  FROM tax_billing_entity tbe
                  JOIN county_tax_rate ctr
                    ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
                 WHERE tbe.geo_id = p.geo_id AND tbe.tax_year = 2025) AS total_tax_rate
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
        WHERE p.geo_id <> %(geo)s
          AND p.geo_id NOT LIKE 'AJR%%'
          AND pty.market_value BETWEEN %(lo)s AND %(hi)s
          AND ({lbl_sql}) IS NOT DISTINCT FROM %(lbl)s
    """
    params = {"geo": geo_id, "lo": subj_mv * 0.75, "hi": subj_mv * 1.25, "lbl": subj_label, "nb": nb, "sc1": sc1}

    peers = []
    if nb:
        peers = query(base_select + " AND p.neighborhood_cd = %(nb)s"
                      " ORDER BY ABS(pty.market_value - %(mv)s) LIMIT 5",
                      {**params, "mv": subj_mv})
    if len(peers) < 5:
        # relax: same state_cd1 prefix, any neighborhood
        peers = query(base_select + " AND LEFT(p.state_cd1,1) = %(sc1)s"
                      " ORDER BY ABS(pty.market_value - %(mv)s) LIMIT 5",
                      {**params, "mv": subj_mv})

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
    return jsonify({"ok": True, "subject_label": subj_label, "neighborhood": nb, "peers": out, "count": len(out)})


# ── On-demand billing fetch ────────────────────────────────────────────────────
@app.route("/api/billing/<geo_id>")
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
    conn = get_db()
    try:
        # 1. Already fetched? (real data or sentinel both count)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM tax_billing "
                "WHERE geo_id = %s AND data_source = 'portal_scrape'",
                (geo_id,)
            )
            already_fetched = cur.fetchone()["cnt"] > 0

        # 2. Portal fetch (only if not cached)
        if not already_fetched:
            html, status = fetch_html(geo_id)
            if html is not None and status == HTTP_OK:
                receipts = parse_receipts(html)
                target   = [r for r in receipts if r["tax_year"] in _BILLING_TARGET_YEARS]
                if target:
                    records = [
                        {
                            "geo_id":     geo_id,
                            "tax_year":   r["tax_year"],
                            "total_tax":  r["payment_amount"],
                            "total_paid": r["payment_amount"],
                        }
                        for r in target
                    ]
                    upsert_billing_rows(conn, records)
                else:
                    # Portal has this account but no 2021-2024 receipts — sentinel
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO tax_billing "
                            "  (geo_id, tax_year, data_source, confidence_level) "
                            "VALUES (%s, %s, 'portal_scrape', 'partial') "
                            "ON CONFLICT (geo_id, tax_year) DO NOTHING",
                            (geo_id, _BILLING_SENTINEL_YEAR)
                        )
                    conn.commit()
            # Network/429/5xx → don't cache, let next page visit retry

        # 3. Return 2021-2024 portal_scrape rows (sentinel excluded by year range)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT tax_year, total_tax, total_paid, data_source, confidence_level "
                "FROM tax_billing "
                "WHERE geo_id = %s "
                "  AND tax_year BETWEEN 2021 AND 2024 "
                "  AND data_source = 'portal_scrape' "
                "ORDER BY tax_year",
                (geo_id,)
            )
            rows = [dict(r) for r in cur.fetchall()]

        # psycopg2 returns Decimal — convert for JSON
        for row in rows:
            for k in ("total_tax", "total_paid"):
                if row[k] is not None:
                    row[k] = float(row[k])

        return jsonify({"status": "ok", "cached": already_fetched, "rows": rows})

    except Exception as exc:
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


@app.route("/parcels")
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

    # Build alias-safe filter: join alias is 'y25', parcel alias is 'p'
    rows = query(f"""
        WITH y25 AS (
            SELECT p.geo_id, p.state_cd1, p.classi_cd, p.situs_address, p.owner_name,
                   t.market_value AS mv25
            FROM   parcel p
            JOIN   parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
            WHERE  t.market_value > 0
              AND  p.state_cd1 NOT LIKE 'X%%'
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  ({where_fragment})
        )
        SELECT
            y25.geo_id,
            y25.situs_address  AS address,
            y25.owner_name     AS owner,
            y25.mv25,
            t26.market_value   AS mv26
        FROM  y25
        LEFT JOIN parcel_tax_year t26
               ON t26.geo_id = y25.geo_id AND t26.tax_year = 2026
        ORDER BY y25.mv25 DESC NULLS LAST
        LIMIT 500
    """, {"ptype": ptype, "rolled": rolled})

    return render_template(
        "parcel_list.html",
        view=view,
        ptype=ptype or "All",
        parcels=[dict(r) for r in rows],
    )


@app.route("/compare")
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

    parcels = []
    for geo_id in geo_ids:
        parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
        if not parcel:
            continue

        current = query("""
            SELECT market_value, assessed_value, taxable_value, hs_cap_loss, data_source, exemption_codes
            FROM   parcel_tax_year WHERE geo_id = %s AND tax_year = 2025
        """, (geo_id,), one=True)

        current_2026 = query("""
            SELECT market_value, assessed_value
            FROM   parcel_tax_year WHERE geo_id = %s AND tax_year = 2026
        """, (geo_id,), one=True)

        billing = query("""
            SELECT total_tax, total_paid, total_due, is_delinquent
            FROM   tax_billing WHERE geo_id = %s AND tax_year = 2025
        """, (geo_id,), one=True)

        sc1 = (parcel.get("state_cd1") or "").strip()[:1]
        type_map = {
            "A": "Residential", "B": "Multi-Family", "C": "Land/Vacant",
            "D": "Agricultural", "E": "Agricultural", "F": "Commercial",
        }

        parcels.append({
            "geo_id":        geo_id,
            "address":       parcel.get("situs_address") or "Unknown",
            "prop_type":     type_map.get(sc1, sc1 or "Unknown"),
            "parcel":        dict(parcel),
            "current":       dict(current) if current else {},
            "current_2026":  dict(current_2026) if current_2026 else {},
            "billing":       dict(billing) if billing else {},
        })

    if not parcels:
        return render_template("compare.html", parcels=[], error="No valid parcels found for the provided IDs.")

    return render_template("compare.html", parcels=parcels, error=None)

@app.route("/info")
def info():
    """Informational reference page -- topic sections (starting with Homestead
    Exemptions) filtered by state / county. Static content today (Texas /
    Travis County only), no parcel or DB data involved -- structured so more
    topics/states/counties can be added later without a route change."""
    return render_template("info.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/styleguide")
def styleguide():
    """Design-system reference: renders every token and component.
    Single source of truth for the visual language — review here before
    restyling real pages. Not linked in primary nav."""
    return render_template("styleguide.html")


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT)
