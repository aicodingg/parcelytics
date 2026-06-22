"""
Travis County Property Tax Platform — Flask Web Application
Phase 1: Parcel search + 5-year history + tax rate trends
"""
import os
import sys
import json
from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(__file__))
import config


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

    rows = []
    for i in range(1, years_ahead + 1):
        proj_year   = base_year + i
        proj_market = round(base_market * (1 + value_cagr) ** i)
        proj_rate   = max(0, current_rate + avg_rate_change * i)

        if has_hs_cap:
            # Assessed capped at 10% per year increase
            proj_assessed = round(min(base_assessed * (1.10 ** i), proj_market))
        else:
            proj_assessed = proj_market

        est_tax = round(proj_assessed * proj_rate / 100)

        rows.append({
            "year":         proj_year,
            "market":       proj_market,
            "assessed":     proj_assessed,
            "rate":         round(proj_rate, 6),
            "est_tax":      est_tax,
            "value_change": round((proj_market - base_market) / base_market * 100, 1),
        })

    return rows, baseline_label

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
# Source: TCAD Property Use Code matrix.
# These codes appear in PROP.TXT (classi_cd field — not currently loaded).
# Loaded here for future use once that field is added to the schema.
# Key = numeric string as it appears in the TCAD export.
USE_CODE_LOOKUP = {
    # Residential
    "10": ("Single-Family Residence", "Cost"),
    "11": ("Single-Family (Acreage)", "Cost"),
    "12": ("Condominium", "Cost"),
    "13": ("Townhouse", "Cost"),
    "14": ("Duplex", "Cost"),
    "15": ("Triplex / Fourplex", "Cost"),
    "16": ("Single-Family (Manufactured)", "Cost"),
    # Multi-Family
    "20": ("Apartment — Garden (Low-Rise)", "Income"),
    "21": ("Apartment — Mid-Rise", "Income"),
    "22": ("Apartment — High-Rise", "Income"),
    "23": ("Mobile Home Park", "Income"),
    "24": ("Mixed-Use Residential", "Income"),
    # Vacant Land
    "25": ("Residential Lot (Vacant)", "Cost"),
    "26": ("Commercial Lot (Vacant)", "Cost"),
    "27": ("Agricultural Land (Vacant)", "Cost"),
    # Office / Medical
    "30": ("Strip Center / Small Office", "Income"),
    "31": ("Neighborhood Shopping Center", "Income"),
    "32": ("Medium Office", "Cost"),
    "33": ("Large Office / Campus", "Income"),
    "34": ("Medical Office", "Income"),
    "35": ("Medical Clinic / Outpatient", "Income"),
    "36": ("Hospital", "Income"),
    "37": ("Restaurant / Fast Food", "Income"),
    # Retail
    "38": ("Retail — Freestanding", "Income"),
    "39": ("Big-Box Retail", "Income"),
    "40": ("Auto Dealership", "Income"),
    "41": ("Service Station / Gas", "Income"),
    "42": ("Car Wash", "Income"),
    "43": ("Auto Repair / Service", "Income"),
    # Industrial / Warehouse
    "44": ("Warehouse / Distribution", "Income"),
    "45": ("Light Industrial", "Cost"),
    "46": ("Heavy Industrial / Manufacturing", "Cost"),
    "47": ("Flex Space", "Income"),
    # Hospitality / Special
    "48": ("Convenience Store", "Income"),
    "49": ("Hotel / Motel", "Income"),
    "50": ("Bed & Breakfast", "Income"),
    "51": ("Country Club / Golf", "Income"),
    "52": ("Theater / Entertainment", "Income"),
    "53": ("Church / Religious", "Cost"),
    "54": ("School / Educational", "Cost"),
    "55": ("Government / Civic", "Cost"),
    "56": ("Cemetery / Mortuary", "Cost"),
    "57": ("Parking Garage / Lot", "Income"),
    "58": ("Self-Storage / Mini-Warehouse", "Income"),
    "59": ("Data Center / Tech", "Income"),
    # Agricultural
    "60": ("Cropland — Open Space", "Cost"),
    "61": ("Improved Pasture", "Cost"),
    "62": ("Native Pasture / Range", "Cost"),
    "63": ("Orchard / Vineyard", "Cost"),
    "64": ("Timber", "Cost"),
    "65": ("Wildlife Management", "Cost"),
    # Misc
    "70": ("Utility Infrastructure", "Cost"),
    "71": ("Pipeline / Easement", "Cost"),
    "72": ("Oil / Gas Surface", "Income"),
    "80": ("Exempt — Government", "N/A"),
    "81": ("Exempt — Religious", "N/A"),
    "82": ("Exempt — Educational", "N/A"),
    "90": ("Personal Property", "Cost"),
    "91": ("Business Personal Property", "Cost"),
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
               tb.exemption_codes  AS billing_exemptions
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
    projections, proj_baseline = build_projections(
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
        proj_baseline=proj_baseline,
        metrics_by_year=metrics_by_year,
        benchmark_by_year=benchmark_by_year,
        bench_label=bench_label,
        state_cd_descriptions=STATE_CD_DESCRIPTIONS,
        val_method=get_valuation_method(parcel.get("state_cd1") or ""),
        entity_rate_by_code=entity_rate_by_code,
        chart_entity_data=chart_entity_data,
        chart_years=chart_years,
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


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT)
