"""
classification_map_harris.py — STRUCTURAL SKELETON ONLY, per DATA_LIFECYCLE.md
Stage 1 (CLASSIFY) and the HARRIS-ONBOARD-1 brief's Ask 3.

REAL, HONEST STATUS: DRAFT SKELETON. NOT BUILT. NOT ENFORCEABLE. NOT WIRED
INTO ANY LOADER. Mirrors `classification_map_dallas.py`'s own just-proven
shape exactly, per this brief's explicit instruction not to deviate from
it. Contains ZERO real Harris class codes, because none are known yet —
see "WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN" below. HCAD's own
property-type/use-code conventions are not confirmed to match TCAD's
`classi_cd`/`state_cd1` scheme, and this sandbox has no live web access to
check HCAD's real export format or any public documentation of it.

WHY THIS FILE EXISTS AT ALL, GIVEN IT HAS NO REAL CONTENT
Same reasoning as `classification_map_dallas.py`: DATA_LIFECYCLE.md Stage 1
describes the Classification Map as "one committed file, shared by every
loader." This skeleton commits the real STRUCTURE now (four-bucket
vocabulary, per-code evidence-citation convention, hard-stop-on-UNKNOWN
philosophy, FLAGGED_FOR_REVIEW convention) so that once a real HCAD sample
export exists, filling this in is a matter of adding evidenced entries to
an already-agreed shape.

WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN
- Any actual HCAD class-code value. No live web access in this sandbox to
  inspect a real HCAD export or any public HCAD code documentation.
  Presenting a guess with the same visual confidence as
  classification_map.py's real, evidenced Travis entries would be exactly
  the fabrication DATA_LIFECYCLE.md's onboarding gate exists to prevent.
- Any claim about which column HCAD's export uses for property-type
  classification, or whether it even has a directly analogous field to
  TCAD's state_cd1. Unconfirmed until a real file exists.
- Any assumption that HCAD's scale (~1.9M parcels, several times Travis's
  517,614 — see the Harris County Profile entry's own scale discussion)
  changes anything about THIS file's structure. It doesn't: classification
  is per-code, not per-row-count. Scale is a real, separate consideration
  for the pipeline/partitioning design, not for this module's shape.

WHAT DIEGO NEEDS TO DO BEFORE THIS FILE CAN HOLD REAL CONTENT (in order of
leverage) — identical in kind to Dallas's list
  1. Obtain a real HCAD sample export — even a small, old, or partial one.
     Would answer the real classification field name(s), the real code
     vocabulary, and (via a GROUP BY count, the same technique
     query_state_cd1_prefixes.py used for Travis) the real population
     distribution per code, all at once.
  2. Absent a sample file: check whether HCAD publishes its own code/use-
     type documentation publicly. This sandbox cannot browse HCAD's site
     to check.
  3. Once real codes are known: classify each one the way
     classification_map.py's Travis entries were built — cite a specific
     source (a real population count from Harris's own data, a Texas
     Comptroller rule if HCAD's codes follow the same statewide rule
     numbering TCAD's do, or HCAD's own documentation) for every bucket
     assignment. Texas Comptroller Rule 9.4001 defines a statewide
     state_cd1 vocabulary most TX CADs are expected to follow — a real,
     checkable reason to START from a hypothesis that HCAD's codes
     resemble TCAD's, same as noted for Dallas — but "expected to follow a
     statewide rule" is not "confirmed," and this file takes no position
     until a real file or HCAD's own documentation confirms it.
  4. Every real assignment then needs Fable's review before Phase-1
     enforcement can trust it, same standing rule as Travis's and Dallas's
     maps (DATA_LIFECYCLE.md §9.4).

STRUCTURE (mirrors classification_map.py's real, already-shipped shape and
classification_map_dallas.py's own skeleton exactly; bucket constants
imported from classification_map.py rather than redeclared, so all three
counties' maps share one vocabulary and can never define it differently by
accident)
"""
from classification_map import (
    REAL_PROPERTY,
    PERSONAL_PROPERTY,
    EXEMPT_SYNTHETIC,
    UNKNOWN,
    VALID_BUCKETS,
    UnknownClassCodeError,
)

# The real field name HCAD's export uses for property-type/use-code
# classification is not yet known (see module docstring, item 2). This is
# a placeholder, not a confirmed value -- every consumer of this constant
# must fail loudly rather than silently assume it is correct.
HARRIS_CLASS_FIELD_NAME = None  # NEEDS DIEGO'S RESEARCH -- see module docstring.

# Intentionally empty. Per the module docstring: no real Harris class code
# is known yet, and this file will not present a guess as if it were an
# evidenced entry the way every key in classification_map.py's real
# CLASSIFICATION_MAP is. Fill this in only once a real code, with a real,
# checkable source, is available -- one entry at a time, each cited the
# same way classification_map.py's own entries are.
CLASSIFICATION_MAP_HARRIS = {}

# Mirrors classification_map.py's own FLAGGED_FOR_REVIEW convention -- kept
# here, empty, as a structural placeholder so the review workflow is
# already agreed before the first real entry needs it.
FLAGGED_FOR_REVIEW_HARRIS = {}


def classify_harris_code(value):
    """Structural placeholder for classification_map.py's real
    classify_state_cd1(). Deliberately NOT implemented -- raises
    NotImplementedError unconditionally, rather than returning a plausible-
    looking but meaningless result, because CLASSIFICATION_MAP_HARRIS is
    empty and HARRIS_CLASS_FIELD_NAME is unconfirmed. Do not stub this out
    with a fallback that silently passes rows through; per DATA_LIFECYCLE.md
    Stage 1, an unclassifiable code must halt a load, and a function that
    can't yet classify ANYTHING must say so just as loudly.
    """
    raise NotImplementedError(
        "classify_harris_code() is a structural skeleton with no real "
        "Harris class-code data (CLASSIFICATION_MAP_HARRIS is empty and "
        "HARRIS_CLASS_FIELD_NAME is unconfirmed). Do not wire this into a "
        "loader or call it against real data -- see this module's own "
        "docstring for what Diego needs to research first."
    )
