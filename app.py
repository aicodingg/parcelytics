"""
Travis County Property Tax Platform — Flask Web Application
Phase 1: Parcel search + 5-year history + tax rate trends
"""
import os
import sys
import json
import re
from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(__file__))
import config

from tax_logic.texas import estimate_post_acquisition as _tx_estimate


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

    # Property type
    ptype = (parcel["prop_type_cd"] or "").strip()
    sc    = (parcel["state_cd1"] or "").strip()
    if sc.startswith("A"):
        out["prop_class"] = "Single-family residential"
    elif sc.startswith("B"):
        out["prop_class"] = "Multi-family residential"
    elif sc.startswith("F"):
        out["prop_class"] = "Commercial"
    elif sc.startswith("D"):
        out["prop_class"] = "Agricultural"
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
        label="Effective Tax Rate",
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


app = Flask(__name__)
app.secret_key = config.FLASK_SECRET


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
    if current is not None and not current.get("total_tax") and entity_detail:
        derived_tax = sum(e["amount_due"] for e in entity_detail if e["amount_due"])
        if derived_tax:
            current["total_tax"] = derived_tax

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

        sc1 = ((parcel.get("state_cd1") or "")).strip()[:1]
        _label_map = {
            "A": "Residential", "B": "Multi-Family", "C": "Land/Vacant",
            "D": "Agricultural", "E": "Agricultural", "F": "Commercial",
        }
        bench_label = _label_map.get(sc1)
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
    if current_2026 and current_2026.get("taxable_value") and entity_detail:
        tv26 = current_2026["taxable_value"]
        blended_rate_2025 = sum(
            float(e["rate"]) for e in entity_detail if e.get("rate") is not None
        )
        if blended_rate_2025 > 0:
            estimated_tax_2026 = round(tv26 * blended_rate_2025 / 100.0, 2)

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
        kpi["effective_tax_rate"]   = round(float(_m25["effective_tax_rate"]) * 100, 4)
    elif insights and insights.get("total_rate_2025"):
        # Fallback: if no billing data, show the combined rate as an approximation
        kpi["rate_approx"] = round(float(insights["total_rate_2025"]), 4)

    # ── Narrative + annual trends ──────────────────────────────────────────────
    narrative     = generate_property_narrative(parcel, history, metrics_by_year,
                                                benchmark_by_year, insights, projections)
    annual_trends = compute_annual_trends(history, metrics_by_year, projections)

    return render_template(
        "property.html",
        parcel=parcel,
        history=history,
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
        kpi=kpi,
        narrative=narrative,
        annual_trends=annual_trends,
    )


@app.route("/rates")
def tax_rates():
    """Tax rate trend page — county-level, no parcel required."""
    # Key entities to highlight in the main chart
    KEY_ENTITIES = ["TCO", "IAU", "CAT", "THD", "ACT"]

    rates = query("""
        SELECT entity_code, entity_name, tax_year, rate
        FROM   county_tax_rate
        WHERE  tax_year >= 2006
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

    # All available entities for the selector
    all_entities = [
        {"code": code, "name": entity_names[code]}
        for code in sorted(by_entity.keys())
    ]

    return render_template(
        "rates.html",
        by_entity_json=json.dumps(by_entity),
        entity_names_json=json.dumps(entity_names),
        all_entities=all_entities,
        key_entities=KEY_ENTITIES,
    )


@app.route("/snapshot")
def county_snapshot():
    """County Market Snapshot — 2026 preliminary vs 2025 certified.
    Supports ?view=overall|residential|commercial (default: overall).
    """
    view = request.args.get("view", "overall")
    if view not in ("overall", "residential", "commercial", "multifamily", "land", "agricultural"):
        view = "overall"

    # ── View-specific WHERE clause applied to the y25 CTE ───────────────────
    # Base exclusions always apply: X-prefix (exempt), AJR* (personal property supplements).
    if view == "residential":
        view_where    = "AND LEFT(p.state_cd1,1) = 'A'"
        bench_labels  = ["Residential"]
        ptype_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'A1%%' THEN 'Single-Family'
                WHEN y25.state_cd1 LIKE 'A2%%' OR y25.state_cd1 LIKE 'A4%%' THEN 'Condo / Townhome'
                ELSE 'Other Residential'
            END"""
        sort_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'A1%%' THEN 1
                WHEN y25.state_cd1 LIKE 'A2%%' OR y25.state_cd1 LIKE 'A4%%' THEN 2
                ELSE 3
            END"""
    elif view == "commercial":
        view_where    = "AND LEFT(p.state_cd1,1) IN ('F','L')"
        bench_labels  = ["Commercial"]
        ptype_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'F' THEN 'Commercial Improved'
                WHEN LEFT(y25.state_cd1,1) = 'L' THEN 'Commercial Land / RE'
                ELSE 'Other'
            END"""
        sort_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'F' THEN 1
                WHEN LEFT(y25.state_cd1,1) = 'L' THEN 2
                ELSE 3
            END"""
    elif view == "multifamily":
        view_where    = "AND LEFT(p.state_cd1,1) = 'B'"
        bench_labels  = ["Multi-Family"]
        ptype_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'B1%%' THEN 'Multifamily (5+ units)'
                WHEN y25.state_cd1 LIKE 'B2%%' THEN 'Duplex'
                WHEN y25.state_cd1 LIKE 'B3%%' THEN 'Triplex'
                WHEN y25.state_cd1 LIKE 'B4%%' THEN 'Fourplex'
                ELSE 'Other Multi-Family'
            END"""
        sort_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'B1%%' THEN 1
                WHEN y25.state_cd1 LIKE 'B2%%' THEN 2
                WHEN y25.state_cd1 LIKE 'B3%%' THEN 3
                WHEN y25.state_cd1 LIKE 'B4%%' THEN 4
                ELSE 5
            END"""
    elif view == "land":
        view_where    = "AND LEFT(p.state_cd1,1) = 'C'"
        bench_labels  = ["Land/Vacant"]
        ptype_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'C1%%' THEN 'Vacant Lot'
                WHEN y25.state_cd1 LIKE 'C2%%' THEN 'Colonia'
                ELSE 'Other Vacant'
            END"""
        sort_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'C1%%' THEN 1
                WHEN y25.state_cd1 LIKE 'C2%%' THEN 2
                ELSE 3
            END"""
    elif view == "agricultural":
        view_where    = "AND LEFT(p.state_cd1,1) IN ('D','E')"
        bench_labels  = ["Agricultural"]
        ptype_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'D' THEN 'Open-Space Ag (1-d-1)'
                WHEN LEFT(y25.state_cd1,1) = 'E' THEN 'Rural Land (non-ag)'
                ELSE 'Other Agricultural'
            END"""
        sort_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'D' THEN 1
                WHEN LEFT(y25.state_cd1,1) = 'E' THEN 2
                ELSE 3
            END"""
    else:  # overall
        view_where    = "AND p.state_cd1 NOT LIKE 'N%%'"
        bench_labels  = ["Residential", "Multi-Family", "Commercial", "Land/Vacant", "Agricultural"]
        ptype_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'A'        THEN 'Residential'
                WHEN LEFT(y25.state_cd1,1) = 'B'        THEN 'Multi-Family'
                WHEN LEFT(y25.state_cd1,1) IN ('F','L') THEN 'Commercial'
                WHEN LEFT(y25.state_cd1,1) = 'C'        THEN 'Land/Vacant'
                WHEN LEFT(y25.state_cd1,1) IN ('D','E') THEN 'Agricultural'
                ELSE 'Other'
            END"""
        sort_case = """
            CASE
                WHEN LEFT(y25.state_cd1,1) = 'A'        THEN 1
                WHEN LEFT(y25.state_cd1,1) = 'B'        THEN 2
                WHEN LEFT(y25.state_cd1,1) IN ('F','L') THEN 3
                WHEN LEFT(y25.state_cd1,1) = 'C'        THEN 4
                WHEN LEFT(y25.state_cd1,1) IN ('D','E') THEN 5
                ELSE 6
            END"""

    rows = query(f"""
        WITH y25 AS (
            SELECT p.geo_id, p.state_cd1, t.market_value AS mv25
            FROM parcel p
            JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
            WHERE t.market_value > 0
              AND p.state_cd1 NOT LIKE 'X%%'
              AND p.geo_id NOT LIKE 'AJR%%'
              {view_where}
        ),
        y26 AS (
            SELECT geo_id, market_value AS mv26
            FROM parcel_tax_year
            WHERE tax_year = 2026 AND market_value > 0
        ),
        joined AS (
            SELECT
                y25.state_cd1,
                y25.mv25,
                y26.mv26,
                (y26.mv26 - y25.mv25)::FLOAT / y25.mv25 AS pct_chg,
                ({ptype_case}) AS ptype,
                ({sort_case}) AS sort_key
            FROM y25 JOIN y26 USING (geo_id)
        )
        SELECT
            ptype,
            sort_key,
            COUNT(*)                                                                        AS n_parcels,
            SUM(CASE WHEN mv26 > mv25 THEN 1 ELSE 0 END)                                   AS n_up,
            SUM(CASE WHEN mv26 < mv25 THEN 1 ELSE 0 END)                                   AS n_down,
            SUM(CASE WHEN mv26 = mv25 THEN 1 ELSE 0 END)                                   AS n_flat,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pct_chg)::NUMERIC * 100, 2)  AS median_pct,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY pct_chg)::NUMERIC * 100, 2) AS p25_pct,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY pct_chg)::NUMERIC * 100, 2) AS p75_pct,
            ROUND(SUM(mv25)::NUMERIC / 1e9, 2)                                             AS total_mv25_b,
            ROUND(SUM(mv26)::NUMERIC / 1e9, 2)                                             AS total_mv26_b
        FROM joined
        GROUP BY ptype, sort_key
        ORDER BY sort_key
    """)

    # Totals row (same view filter applied)
    totals = query(f"""
        WITH y25 AS (
            SELECT t.geo_id, market_value AS mv25
            FROM parcel_tax_year t
            JOIN parcel p ON p.geo_id = t.geo_id
            WHERE tax_year = 2025 AND market_value > 0
              AND p.state_cd1 NOT LIKE 'X%%'
              AND p.state_cd1 NOT LIKE 'N%%'
              AND t.geo_id NOT LIKE 'AJR%%'
              {view_where}
        ),
        y26 AS (
            SELECT geo_id, market_value AS mv26
            FROM parcel_tax_year
            WHERE tax_year = 2026 AND market_value > 0
              AND geo_id NOT LIKE 'AJR%%'
        )
        SELECT
            COUNT(*)                                                                        AS n_total,
            SUM(CASE WHEN mv26 > mv25 THEN 1 ELSE 0 END)                                   AS n_up,
            SUM(CASE WHEN mv26 < mv25 THEN 1 ELSE 0 END)                                   AS n_down,
            ROUND(SUM(mv25)::NUMERIC / 1e9, 3)                                             AS total_mv25_b,
            ROUND(SUM(mv26)::NUMERIC / 1e9, 3)                                             AS total_mv26_b,
            ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY (mv26 - mv25)::FLOAT / mv25
            )::NUMERIC * 100, 2)                                                           AS median_pct
        FROM y25 JOIN y26 USING (geo_id)
    """, one=True)

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

    return render_template(
        "snapshot.html",
        rows=rows,
        totals=totals,
        view=view,
        bench_trends=bench_trends,
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

    use_codes_raw = query("""
        SELECT
            p.classi_cd,
            CASE
                WHEN LEFT(p.state_cd1,1) = 'A'        THEN 'Residential'
                WHEN LEFT(p.state_cd1,1) = 'B'        THEN 'Multi-Family'
                WHEN LEFT(p.state_cd1,1) IN ('F','L') THEN 'Commercial'
                WHEN LEFT(p.state_cd1,1) = 'C'        THEN 'Land/Vacant'
                WHEN LEFT(p.state_cd1,1) IN ('D','E') THEN 'Agricultural'
                ELSE 'Other'
            END AS prop_type,
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

    # Neighborhoods with ≥5 parcels (sorted by count desc, capped at 500 to avoid huge dropdown)
    nb_raw = query("""
        SELECT neighborhood_cd, COUNT(*) AS n
        FROM parcel
        WHERE neighborhood_cd IS NOT NULL AND neighborhood_cd != ''
        GROUP BY neighborhood_cd
        HAVING COUNT(*) >= 5
        ORDER BY n DESC
        LIMIT 500
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

    # Parcel market-growth assumption from its own certified history (clamped)
    mkt_hist = query("""
        SELECT tax_year, market_value FROM parcel_tax_year
        WHERE geo_id = %s AND market_value IS NOT NULL AND tax_year <= 2025
        ORDER BY tax_year
    """, (geo_id,))
    market_growth = None
    pts = [(r["tax_year"], float(r["market_value"])) for r in mkt_hist if r["market_value"]]
    if len(pts) >= 2 and pts[0][1] > 0:
        span = pts[-1][0] - pts[0][0]
        if span > 0:
            cagr = (pts[-1][1] / pts[0][1]) ** (1.0 / span) - 1.0
            market_growth = max(0.0, min(0.08, cagr))   # clamp 0–8%

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

    # pg_trgm index (idx_parcel_situs_trgm) will be used if installed;
    # ILIKE works correctly either way — just slower without the index.
    rows = query("""
        SELECT geo_id, situs_address, owner_name, state_cd1, neighborhood_cd
        FROM   parcel
        WHERE  UPPER(situs_address) ILIKE %(pattern)s
        ORDER  BY situs_address
        LIMIT  10
    """, {"pattern": f"%{q_norm}%"})

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
    peers = query("""
        SELECT
            p.geo_id,
            pty.market_value,
            pty.assessed_value,
            tb.total_tax
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
        LEFT JOIN tax_billing  tb  ON tb.geo_id  = p.geo_id AND tb.tax_year  = 2025
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
            SELECT p.geo_id, pty.market_value, pty.assessed_value, tb.total_tax
            FROM   parcel p
            JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
            LEFT JOIN tax_billing  tb  ON tb.geo_id  = p.geo_id AND tb.tax_year  = 2025
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
    taxes = sorted([float(r["total_tax"]) for r in peers if r.get("total_tax")])

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
               pty.market_value,
               pty.assessed_value
        FROM   parcel p
        JOIN   parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
        WHERE  p.geo_id = %s
    """, (geo_id,), one=True)

    if not parcel_data:
        return jsonify({"ok": False, "error": "No 2025 data for this parcel"})

    sqft    = float(parcel_data["living_area_sqft"]) if parcel_data.get("living_area_sqft") else None
    this_mv = float(parcel_data["market_value"])     if parcel_data.get("market_value")     else None
    this_av = float(parcel_data["assessed_value"])   if parcel_data.get("assessed_value")   else None

    if not sqft or sqft <= 0:
        return jsonify({
            "ok": False, "error": "no_sf_basis",
            "message": "No living area SF for this parcel (vacant land, exempt-only, or loader not run)"
        })

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
                CAST(pty.market_value   AS FLOAT) / p.living_area_sqft          AS market_psf,
                CAST(pty.assessed_value AS FLOAT) / p.living_area_sqft          AS assessed_psf
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
        "this_market_psf":        this_market_psf,
        "this_assessed_psf":      this_assessed_psf,
        "this_market_psf_rank":   this_market_psf_rank,
        "this_assessed_psf_rank": this_assessed_psf_rank,
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
    })


# ── Task 5: ptype label → SQL WHERE fragments ──────────────────────────────────
# task5_drill_through
_PTYPE_SC1_FILTER = {
    # Overall
    "Residential":            "LEFT(p.state_cd1,1) = 'A'",
    "Multi-Family":           "LEFT(p.state_cd1,1) = 'B'",
    "Commercial":             "LEFT(p.state_cd1,1) IN ('F','L')",
    "Land/Vacant":            "LEFT(p.state_cd1,1) = 'C'",
    "Agricultural":           "LEFT(p.state_cd1,1) IN ('D','E')",
    # Residential sub-types
    "Single-Family":          "y25.state_cd1 LIKE 'A1%'",
    "Condo / Townhome":       "(y25.state_cd1 LIKE 'A2%' OR y25.state_cd1 LIKE 'A4%')",
    "Other Residential":      "(LEFT(y25.state_cd1,1) = 'A' AND y25.state_cd1 NOT LIKE 'A1%' AND y25.state_cd1 NOT LIKE 'A2%' AND y25.state_cd1 NOT LIKE 'A4%')",
    # Commercial sub-types
    "Commercial Improved":    "LEFT(y25.state_cd1,1) = 'F'",
    "Commercial Land / RE":   "LEFT(y25.state_cd1,1) = 'L'",
    # Multi-family sub-types
    "Multifamily (5+ units)": "y25.state_cd1 LIKE 'B1%'",
    "Duplex":                 "y25.state_cd1 LIKE 'B2%'",
    "Triplex":                "y25.state_cd1 LIKE 'B3%'",
    "Fourplex":               "y25.state_cd1 LIKE 'B4%'",
    "Other Multi-Family":     "LEFT(y25.state_cd1,1) = 'B'",
    # Land sub-types
    "Vacant Lot":             "y25.state_cd1 LIKE 'C1%'",
    "Colonia":                "y25.state_cd1 LIKE 'C2%'",
    "Other Vacant":           "LEFT(y25.state_cd1,1) = 'C'",
    # Agricultural
    "Open-Space Ag (1-d-1)":  "LEFT(y25.state_cd1,1) = 'D'",
    "Rural Land (non-ag)":    "LEFT(y25.state_cd1,1) = 'E'",
    "Other Agricultural":     "LEFT(y25.state_cd1,1) IN ('D','E')",
}



@app.route("/parcels")
def parcel_list():
    """
    Drill-through parcel list (Task 5).
    Query params:
      view  str   snapshot view (residential/commercial/etc.)
      ptype str   ptype label from snapshot rows (e.g. 'Single-Family')
    Returns up to 500 matching parcels with 2025 + 2026 market values.
    """
    view  = request.args.get("view", "overall")
    ptype = request.args.get("ptype", "").strip()

    sc1_filter = _PTYPE_SC1_FILTER.get(ptype)
    if not sc1_filter:
        sc1_filter = "1=1"   # no filter — show all (shouldn't happen)

    # Build alias-safe filter: join alias is 'y25', parcel alias is 'p'
    rows = query(f"""
        WITH y25 AS (
            SELECT p.geo_id, p.state_cd1, p.situs_address, p.owner_name,
                   t.market_value AS mv25
            FROM   parcel p
            JOIN   parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = 2025
            WHERE  t.market_value > 0
              AND  p.state_cd1 NOT LIKE 'X%%'
              AND  p.geo_id NOT LIKE 'AJR%%'
              AND  ({sc1_filter})
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
    """)

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
