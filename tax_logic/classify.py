"""
classify.py — canonical property-type classification for Travis County parcels.

Single source of truth for mapping a parcel to one of the five benchmark
categories.  Used by both the web app (app.py) and the metrics builder
(loaders/compute_metrics.py) so the property-detail page, the market snapshot,
and county_benchmark can never disagree.

Rule (per Task 1 — classi_cd-first):
  1. classi_cd identifies the *actual* improvement use and overrides state_cd1.
     - Multi-family improvement codes  -> "Multi-Family"
     - Commercial/retail improvement codes -> "Commercial"
  2. Only when classi_cd is null / '00' / not a recognized override code do we
     fall back to the Texas Comptroller state_cd1 prefix (A/B/C/D/E/F/L).

Why: parcels such as 192427 carry classi_cd '08' (Apartments 100+) but a
state_cd1 of 'A'.  Bucketing on state_cd1 alone mislabels them Single-Family,
which double-counts them as residential and starves the Multi-Family category.
"""

# Improvement use codes (classi_cd, from IMP_INFO.TXT [28:38]) that force a
# category regardless of the Comptroller state_cd1 prefix.
MULTI_FAMILY_CODES = (
    "05", "06", "07", "08", "107", "108",   # apartments 5-25 ... luxury hi-rise 100+
    "72", "73", "74",                        # fraternity/sorority, dormitory, dorm hi-rise
    "17",                                    # clubhouse (apartment-complex amenity)
    "SYNUP",                                 # synthetic/aggregated multi-family upgrade
)

COMMERCIAL_CODES = (
    "20", "30", "31", "32", "33",            # small store, strip, bar, restaurant, fast food
    "40", "41", "42", "43", "44", "45",      # shopping centers, grocery, dept store
    "47", "48",                              # retail store, convenience store
    "4RS",                                   # retail (synthetic)
)

# State-code prefix fallback (first character of state_cd1) -> label.
#
# "M" (manufactured/mobile homes) added per the "Other" bucket investigation
# (Market Snapshot Issue A): 10,699 parcels (2.1% of the county, per
# KNOWN_LIMITATIONS.md's state_cd1 population table) carry state_cd1 = M and
# were previously falling through to NULL here, then COALESCE'd to "Other" by
# every caller. Manufactured homes are real property under Texas law once
# affixed to land (see KNOWN_LIMITATIONS.md: "Manufactured homes (real
# property under TX law, kept in benchmarks)"), and app.py's own
# VALUATION_METHOD_BY_CLASS already documents "M": "Cost" alongside the other
# residential-style cost-approach classes (A). classi_cd 13/14 ("Mobile Home
# — Single/Double (Real)" in USE_CODE_LOOKUP) corroborate: TCAD itself treats
# real-property mobile/manufactured homes as a residential improvement type.
# Mapping M -> Residential is therefore a reasoned call, not a guess.
#
# "O" ("Other real property" — 19,986 parcels, 3.9% of the county) is
# deliberately left UNMAPPED, pending Diego's decision (not decided here).
# Unlike M, there was no converging evidence for a single correct category
# at the state_cd1 level: VALUATION_METHOD_BY_CLASS calls O a "TCAD
# catch-all — no standard valuation method", and KNOWN_LIMITATIONS.md only
# describes it as "Other real property" with no further breakdown.
#
# UPDATE (live data, per Diego's check_other_property_type_fix.py run):
# within state_cd1='O', classi_cd is NOT evenly spread — 76% of parcels and
# 81% of value are classi_cd '01' (Single-Family Residence). That's real,
# specific evidence, not "no information" — it's the same shape of signal
# that already justifies the classi_cd-first override used for Multi-Family/
# Commercial (MULTI_FAMILY_CODES / COMMERCIAL_CODES below): classi_cd
# identifies actual improvement use and can override a state_cd1 that
# doesn't reflect it. Whether that pattern SHOULD be extended to reclassify
# at least classi_cd='01' within O (and possibly other dominant codes) —
# rather than leaving the whole bucket as one undifferentiated "Other" — is
# a real, open question, not something to resolve unilaterally here. It
# changes the meaning of "O" from "some genuinely unknown catch-all" to
# "mostly recognizable single-family parcels carrying a Comptroller code
# that doesn't say so", which argues for reclassifying at least the
# dominant codes. Still stays unmapped until that call is made.
#
# Two more prefixes also surfaced in the live "Other" bucket check that
# weren't part of the original brief: G (minerals/oil & gas — 2 parcels in
# the live, market-value-filtered check; 6 countywide per
# KNOWN_LIMITATIONS.md) and J (industrial/utility real property — 122 in
# the live check; 1,524 countywide). Both are real, distinct top-level
# Comptroller categories (see STATE_CD_DESCRIPTIONS in app.py: G1-G3,
# J1-J9), not sub-types of any of the five benchmark categories — same
# "don't force a fit" reasoning as O applies, left unmapped rather than
# silently folded into Commercial or anywhere else.
#
# Flagged as open judgment calls in the Issue A/B report — see
# task_staging/other_property_type/.
_STATE_PREFIX_LABEL = {
    "A": "Residential",
    "B": "Multi-Family",
    "C": "Land/Vacant",
    "D": "Agricultural",
    "E": "Agricultural",
    "F": "Commercial",
    "L": "Commercial",
    "M": "Residential",
}

# The five benchmark categories, in display/sort order.
BENCHMARK_LABELS = ("Residential", "Multi-Family", "Commercial", "Land/Vacant", "Agricultural")
_LABEL_SORT = {lbl: i + 1 for i, lbl in enumerate(BENCHMARK_LABELS)}


def property_type_label(classi_cd, state_cd1):
    """Return one of BENCHMARK_LABELS, or None if the parcel is not real
    property we benchmark (e.g. personal-property / exempt prefixes)."""
    cc = (classi_cd or "").strip().upper()
    if cc in MULTI_FAMILY_CODES:
        return "Multi-Family"
    if cc in COMMERCIAL_CODES:
        return "Commercial"
    sc = (state_cd1 or "").strip().upper()[:1]
    return _STATE_PREFIX_LABEL.get(sc)


def label_sort_key(label):
    """Stable sort order for the five categories (Other -> 99)."""
    return _LABEL_SORT.get(label, 99)


def label_case_sql(classi_col="p.classi_cd", state_col="p.state_cd1"):
    """Return a SQL CASE expression that mirrors property_type_label().

    Emits no '%' characters, so it is safe to embed in psycopg2 f-string SQL
    that is executed without params (the surrounding query may still use '%%'
    for LIKE clauses elsewhere — this expression adds none).
    Evaluates to one of the five labels or NULL.
    """
    mf = ", ".join(f"'{c}'" for c in MULTI_FAMILY_CODES)
    cm = ", ".join(f"'{c}'" for c in COMMERCIAL_CODES)
    return f"""CASE
            WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({mf}) THEN 'Multi-Family'
            WHEN UPPER(TRIM(COALESCE({classi_col}, ''))) IN ({cm}) THEN 'Commercial'
            WHEN LEFT(UPPER({state_col}), 1) = 'A'          THEN 'Residential'
            WHEN LEFT(UPPER({state_col}), 1) = 'B'          THEN 'Multi-Family'
            WHEN LEFT(UPPER({state_col}), 1) = 'C'          THEN 'Land/Vacant'
            WHEN LEFT(UPPER({state_col}), 1) IN ('D', 'E')  THEN 'Agricultural'
            WHEN LEFT(UPPER({state_col}), 1) IN ('F', 'L')  THEN 'Commercial'
            WHEN LEFT(UPPER({state_col}), 1) = 'M'          THEN 'Residential'
            -- 'O' ("Other real property"), 'G' (minerals/oil & gas), and
            -- 'J' (industrial/utility real property) are intentionally NOT
            -- mapped here. See the _STATE_PREFIX_LABEL comment above: no
            -- single category is well-supported for any of the three (each
            -- is its own distinct Comptroller top-level category, not a
            -- sub-type of the five benchmark ones), so they fall through to
            -- NULL / the literal "Other" label rather than being forced
            -- into one of the five benchmark categories.
            ELSE NULL
        END"""


def label_sort_case_sql(label_expr):
    """Return a SQL CASE giving sort order 1..5 for a label expression, 6 for
    anything else (Other / NULL)."""
    whens = "\n".join(
        f"            WHEN ({label_expr}) = '{lbl}' THEN {i + 1}"
        for i, lbl in enumerate(BENCHMARK_LABELS)
    )
    return f"CASE\n{whens}\n            ELSE 6\n        END"
