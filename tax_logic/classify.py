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
_STATE_PREFIX_LABEL = {
    "A": "Residential",
    "B": "Multi-Family",
    "C": "Land/Vacant",
    "D": "Agricultural",
    "E": "Agricultural",
    "F": "Commercial",
    "L": "Commercial",
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
