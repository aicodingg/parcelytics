"""
snapshot_taxonomy.py — Market Snapshot's scoped 8-tab-plus-Other taxonomy and
view-scoping SQL-fragment builders. Extracted from app.py (AGGPRECOMP-2, Aug
2026) so this pure, DB-connection-free logic has exactly ONE home, importable
from both app.py (the live /snapshot route, unchanged behavior) and
loaders/refresh_snapshot_summary.py (the new Tier 1 refresh script) --
without the refresh script importing app.py itself.

Why this extraction, and why now: no loaders/*.py file in this codebase has
ever imported app.py (confirmed by grep before writing this module) --
app.py creates a Flask app, initializes Sentry, and requires FLASK_SECRET in
production at IMPORT TIME, none of which a batch/loader script should drag
in as a side effect just to reuse a handful of pure SQL-string builders.
This mirrors the exact pattern parcel_filters.py and tax_logic/classify.py
already established in this codebase (a shared, Flask-free module importable
from both app.py and loaders/) -- not a new pattern, an extension of one
already proven here. The alternative (hand-copying this taxonomy into the
refresh script) is exactly the kind of "two copies of the truth" drift this
codebase's own history warns against (CANONICAL_PARCEL_EXCL existed as four
independent copies before parcel_filters.py; label_case_sql() drifted from
hand-rolled duplicates more than once) -- duplicating a QUERY is one thing,
duplicating this taxonomy's ~90-code classification table is a much larger
and much easier place for the two copies to silently disagree.

Every function/constant below is byte-identical in behavior to its prior
app.py definition -- this is a pure move, not a rewrite. app.py now imports
these names from here instead of defining them locally (see the import line
near the top of app.py and the removed originals).
"""

# ── TCAD internal numeric use code -> (description, valuation_method) ──────────
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


def use_code_case_sql(classi_col="p.classi_cd", fallback_label="Other"):
    """SQL CASE expression mapping classi_cd -> its USE_CODE_LOOKUP
    description (e.g. '01' -> 'Single-Family Residence'), for `fallback_label`
    when classi_cd is NULL/unrecognized.

    Built for the Market Snapshot "By Property Type" per-sector breakdown.
    classi_cd (the TCAD numeric use code, populated from IMP_INFO.TXT and
    already displayed on the property-detail page) is the real subtype
    signal that actually exists in this data -- it's exactly what
    /api/benchmark/meta's use_codes_by_type groups by for Search's Use Code
    filter. Reusing the same USE_CODE_LOOKUP descriptions here means a
    sector's breakdown table and its Use Code filter can never show
    different subtypes for the same underlying data.

    Vacant land and some agricultural parcels genuinely have no improvement
    record (classi_cd is NULL by design), so those sectors legitimately
    collapsing toward fallback_label for a large share of parcels is
    expected, real behavior, not a bug.
    """
    def _sql_escape(s):
        """Escape a literal string for embedding in an f-string SQL that
        will be passed through cur.execute(sql, params) -- even with an
        empty/None params tuple, psycopg2 still runs %-style substitution
        over the whole query string, so any bare '%' in embedded text (not
        just quotes) has to be doubled to '%%' or it gets misread as a
        format placeholder. Four USE_CODE_LOOKUP descriptions contain a
        literal '%' (classi_cd 60/64/65/66). Quotes need doubling for the
        same reason CASE/THEN string literals always do."""
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
# THIS IS AN INTENTIONAL, SCOPED EXCEPTION — a SECOND, DELIBERATELY SEPARATE
# classification system from tax_logic/classify.py's canonical 5-category
# one, used ONLY for Market Snapshot's own tab routing and breakdown display.
# What stays on the canonical 5-category system, UNTOUCHED by anything below:
#   - tax_logic/classify.py (property_type_label, label_case_sql,
#     _STATE_PREFIX_LABEL, MULTI_FAMILY_CODES, COMMERCIAL_CODES)
#   - The global nav sector dropdown (templates/base.html)
#   - Search's Property Type filter (templates/search.html, /api/benchmark/meta)
#   - loaders/compute_metrics.py's county_benchmark table
#   - property_detail()'s bench_label
# Anyone reusing the SNAPSHOT_*_CODES constants / _snapshot_taxonomy_sql()
# outside the Market Snapshot page (or its Tier 1 refresh) is almost
# certainly reaching for the wrong function — reach for classify.py's
# label_case_sql() / property_type_label() instead.
#
# Every USE_CODE_LOOKUP code is classified below, evidence-first from its
# real description. See app.py's git history (this comment block's prior
# home) for the full code -> tab mapping review trail with Diego's
# confirmed decisions -- unchanged by this move, quoted here only in
# summary form to avoid two copies of the same historical narrative drifting
# apart:
#   - 02 (Duplex) stays Residential; 03/04 (Triplex/Fourplex) are Multi-Family.
#   - 17 (Clubhouse) is Other. 10 (Manufactured Commercial Bldg) is Other.
#   - 24 (Commercial Space Condos) is Office.
#   - 80/86 (Auto Dealership/Car Wash Full Service) are Retail; 81 (Service
#     Station) is Other; 82/83/84 (Car Wash Booth/Repair Garage/Mini-Lube)
#     are Retail. No Auto/service codes remain in Industrial.
#   - 09/76/89/78 (Special Residential, Retirement Center, Assisted Living,
#     Day Care) are Multi-Family/Multi-Family/Multi-Family/Retail respectively.
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
# classi_cd is sourced entirely from IMP_INFO.TXT -- it only exists for
# parcels that have an IMPROVEMENT (a building) on file, so vacant Land
# parcels structurally have no classi_cd, not a missing/mis-tagged one.
# ALTERNATIVE, REAL DIMENSION: parcel.land_sqft (LAND_DET.TXT, "RELIABLE ...
# always square feet"). Used here as a size-TIER breakdown in place of a
# use-code breakdown for these two sectors only. Tier boundaries are
# reasoned defaults (same discipline as SNAPSHOT_SUBTYPE_CAP=7 in app.py),
# not measured against the real live distribution.
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
    disagree about a given parcel's bucket. That divergence is expected and
    scoped to Market Snapshot's own display -- it does not change
    property_type_label() or any other canonical-classifier consumer.

    F/L (Commercial-by-state_cd1) parcels whose classi_cd doesn't land in
    Retail/Industrial/Office/Hotel above fall through to 'Other' here --
    there is no "generic Commercial" tab in this taxonomy to catch them.
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
# disagree about which view values are valid -- and now also by
# loaders/refresh_snapshot_summary.py, which refreshes every one of these
# view values, not just whichever ones happen to be requested live.
_SNAPSHOT_VALID_VIEWS = {"overall", "commercial"} | set(_SNAPSHOT_SECTOR_VIEWS)

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
    both still deep-link to it.

    "overall" returns "" since it spans every type, unrestricted (same as
    before).
    """
    from tax_logic.classify import label_case_sql

    if view in _SNAPSHOT_SECTOR_VIEWS:
        label = _SNAPSHOT_SECTOR_VIEWS[view]
        _tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
        return f"AND ({_tax}) = '{label}'"
    if view == "commercial":
        label = _SNAPSHOT_VIEW_PROP_TYPE_LABEL["commercial"]
        _lbl = label_case_sql("p.classi_cd", "p.state_cd1")
        return f"AND ({_lbl}) = '{label}'"
    return ""


def ptype_and_sort_case_for_view(view):
    """
    Returns (ptype_case, sort_case, bench_labels, order_by_expr,
    fallback_label) for a given view -- the exact per-view branching
    _compute_snapshot_data() (app.py) used to do inline, factored out here
    so both the live route and the Tier 1 refresh script build the IDENTICAL
    per-view SQL expressions from one place. `fallback_label` is None for
    "overall" (no capping applied there).
    """
    if view in _SNAPSHOT_SECTOR_VIEWS:
        sector_label = _SNAPSHOT_SECTOR_VIEWS[view]
        fallback = "Uncategorized" if sector_label == "Other" else f"Other {sector_label}"
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
        if view == "land":
            ptype_case = _size_tier_case_sql("p.land_sqft", SNAPSHOT_LAND_SIZE_TIERS)
            fallback_label = SNAPSHOT_LAND_SIZE_TIERS[-1][1]
        elif view == "agricultural":
            ptype_case = _size_tier_case_sql("p.land_sqft", SNAPSHOT_AG_SIZE_TIERS)
            fallback_label = SNAPSHOT_AG_SIZE_TIERS[-1][1]
        else:
            ptype_case = use_code_case_sql("p.classi_cd", fallback)
            fallback_label = "Uncategorized" if view == "other" else f"Other {sector_label}"
        sort_case = ptype_case
        order_by_expr = "n_parcels DESC NULLS LAST"
    elif view == "commercial":
        bench_labels = ["Commercial"]
        ptype_case = use_code_case_sql("p.classi_cd", "Other Commercial")
        sort_case = ptype_case
        order_by_expr = "n_parcels DESC NULLS LAST"
        fallback_label = "Other Commercial"
    else:  # overall
        bench_labels = ["Residential", "Multi-Family", "Commercial", "Land/Vacant", "Agricultural"]
        _ov_tax = _snapshot_taxonomy_sql("p.classi_cd", "p.state_cd1")
        ptype_case = _ov_tax
        sort_case = _snapshot_taxonomy_sort_case_sql(_ov_tax)
        order_by_expr = "sort_key NULLS LAST"
        fallback_label = None  # "overall" is never subtype-capped
    return ptype_case, sort_case, bench_labels, order_by_expr, fallback_label
