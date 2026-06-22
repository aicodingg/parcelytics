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
        use_code_lookup=USE_CODE_LOOKUP,
        val_method=(
            USE_CODE_LOOKUP.get(parcel.get("classi_cd") or "", ("", ""))[1]
            or get_valuation_method(parcel.get("state_cd1") or "")
        ),
        entity_rate_by_code=entity_rate_by_code,
        chart_entity_data=chart_entity_data,
        chart_years=chart_years,
        estimated_tax_2026=estimated_tax_2026,
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
    if view not in ("overall", "residential", "commercial"):
        view = "overall"

    # ── View-specific WHERE clause applied to the y25 CTE ───────────────────
    # Base exclusions always apply: X-prefix (exempt), AJR* (personal property supplements).
    if view == "residential":
        view_where = "AND LEFT(p.state_cd1,1) = 'A'"
        ptype_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'A1%%' THEN 'Single-Family'
                WHEN y25.state_cd1 LIKE 'A2%%' THEN 'Condo / Townhome'
                WHEN y25.state_cd1 LIKE 'A4%%' THEN 'Condo / Townhome'
                ELSE 'Other Residential'
            END"""
        sort_case = """
            CASE
                WHEN y25.state_cd1 LIKE 'A1%%' THEN 1
                WHEN y25.state_cd1 LIKE 'A2%%' OR y25.state_cd1 LIKE 'A4%%' THEN 2
                ELSE 3
            END"""
    elif view == "commercial":
        view_where = "AND LEFT(p.state_cd1,1) IN ('F','L')"
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
    else:  # overall
        view_where = "AND p.state_cd1 NOT LIKE 'N%%'"
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

    return render_template(
        "snapshot.html",
        rows=rows,
        totals=totals,
        view=view,
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


if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT)
