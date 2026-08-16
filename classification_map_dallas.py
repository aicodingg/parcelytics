"""
classification_map_dallas.py — STRUCTURAL SKELETON ONLY, per DATA_LIFECYCLE.md
Stage 1 (CLASSIFY) and the DALLAS-ONBOARD-1 brief's Ask 3.

REAL, HONEST STATUS: DRAFT SKELETON. NOT BUILT. NOT ENFORCEABLE. NOT WIRED
INTO ANY LOADER. This module exists only to give Diego and Fable a concrete
shape to react to, matching classification_map.py's real, already-shipped
Travis structure (four buckets, per-code evidence citations, a
FLAGGED_FOR_REVIEW list, a hard-stop-on-UNKNOWN enforcement function). It
contains ZERO real Dallas class codes, because none are known yet — see
"WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN" below. Populating this file
with plausible-sounding but unverified codes would be exactly the kind of
fabrication DATA_LIFECYCLE.md's onboarding gate exists to prevent (per
SPEC_COUNTY_PARTITIONING.md's own repeated disclosure: "Dallas's real
source-file structure — unknowable until real files exist").

WHY THIS FILE EXISTS AT ALL, GIVEN IT HAS NO REAL CONTENT
DATA_LIFECYCLE.md Stage 1 describes the Classification Map as "one
committed file, shared by every loader." Rather than leave that as an
abstract future task, this skeleton commits the REAL STRUCTURE now — the
four-bucket vocabulary, the per-code evidence-citation convention, the
hard-stop-on-UNKNOWN philosophy, the FLAGGED_FOR_REVIEW convention for
Fable's review — so that once a real DCAD sample export exists, filling
this in is a matter of adding evidenced entries to an already-agreed shape,
not inventing the shape under time pressure at the same moment real codes
are being classified for the first time.

WHAT THIS FILE DELIBERATELY DOES NOT CONTAIN
- Any actual DCAD class-code value. This sandbox has no live web access and
  cannot inspect a real DCAD export or DCAD's own public code documentation
  (if published). Every code assignment in classification_map.py's real
  Travis map cites a specific, checkable source (a live query population
  count, a Comptroller rule citation, an existing codebase comment) — this
  file has no equivalent evidence to cite for Dallas, so it makes no
  assignments at all rather than presenting a guess with the same visual
  confidence as Travis's evidenced entries.
- Any claim about which column DCAD's export even uses for property-type
  classification. TCAD's state_cd1 (this codebase's whole classification
  vocabulary is keyed on it) is a TCAD-specific field name from PROP.TXT —
  DCAD's real export may use a differently-named field, a differently-
  shaped code (numeric vs. alphanumeric), or a different number of
  classification fields entirely (TCAD itself has both classi_cd and
  state_cd1, serving different purposes — see classification_map.py's own
  cross-referencing of both). Do not assume "the Dallas equivalent of
  state_cd1" is a meaningful phrase until a real file confirms it.

WHAT DIEGO NEEDS TO DO BEFORE THIS FILE CAN HOLD REAL CONTENT (in order of
leverage)
  1. Obtain a real DCAD sample export — even a small, old, or partial one.
     This single artifact would answer nearly everything below at once:
     the real classification field name(s), the real code vocabulary, and
     (via a GROUP BY count, the same technique query_state_cd1_prefixes.py
     and this file's own future "1a query" would use for Travis) the real
     population distribution per code.
  2. Absent a sample file: check whether DCAD publishes its own code/use-
     type documentation publicly (many Texas CADs publish a data dictionary
     or field-layout guide alongside their bulk-export offering — TCAD's
     own PROP.TXT layout is documented this way). This sandbox cannot
     browse DCAD's site to check.
  3. Once real codes are known: classify each one the same way
     classification_map.py's Travis entries were built — cite a specific
     source (a real population count from Dallas's own data, a Texas
     Comptroller rule if DCAD's codes follow the same statewide rule
     numbering TCAD's do, or DCAD's own documentation) for every bucket
     assignment, not a plausible-sounding guess. Do NOT assume DCAD's codes
     follow TCAD's A/B/C/D/E/F/G/J/L/M/N/O/S/X letter scheme even if some
     labels look similar — Texas Comptroller Rule 9.4001 defines a
     statewide state_cd1 vocabulary that most TX CADs are expected to
     follow, which is a real, checkable reason to START from a hypothesis
     that DCAD's codes resemble TCAD's — but "expected to follow a
     statewide rule" is not the same as "confirmed," and this file takes
     no position on it until a real file or DCAD's own documentation
     confirms it one way or the other.
  4. Every real assignment then needs Fable's review before Phase-1
     enforcement (county_code-aware, per this session's DALLAS-GATE work)
     can trust it — same standing rule DATA_LIFECYCLE.md §9.4 already
     applies to the Travis map.

STRUCTURE (mirrors classification_map.py's real, already-shipped shape;
bucket constants imported from there rather than redeclared, so the two
counties' maps can never define the vocabulary differently by accident)
"""
from classification_map import (
    REAL_PROPERTY,
    PERSONAL_PROPERTY,
    EXEMPT_SYNTHETIC,
    UNKNOWN,
    VALID_BUCKETS,
    UnknownClassCodeError,
)

# The real field name DCAD's export uses for property-type/use-code
# classification is not yet known (see module docstring, item 2). This is
# a placeholder, not a confirmed value -- every consumer of this constant
# must fail loudly rather than silently assume it is correct.
DALLAS_CLASS_FIELD_NAME = None  # NEEDS DIEGO'S RESEARCH -- see module docstring.

# Intentionally empty. Per the module docstring: no real Dallas class code
# is known yet, and this file will not present a guess as if it were an
# evidenced entry the way every key in classification_map.py's real
# CLASSIFICATION_MAP is. Fill this in only once a real code, with a real,
# checkable source, is available -- one entry at a time, each cited the
# same way classification_map.py's own entries are.
CLASSIFICATION_MAP_DALLAS = {}

# Mirrors classification_map.py's own FLAGGED_FOR_REVIEW convention -- kept
# here, empty, as a structural placeholder so the review workflow is
# already agreed before the first real entry needs it.
FLAGGED_FOR_REVIEW_DALLAS = {}


def classify_dallas_code(value):
    """Structural placeholder for classification_map.py's real
    classify_state_cd1(). Deliberately NOT implemented -- raises
    NotImplementedError unconditionally, rather than returning a plausible-
    looking but meaningless result, because CLASSIFICATION_MAP_DALLAS is
    empty and DALLAS_CLASS_FIELD_NAME is unconfirmed. Do not stub this out
    with a fallback that silently passes rows through; per DATA_LIFECYCLE.md
    Stage 1, an unclassifiable code must halt a load, and a function that
    can't yet classify ANYTHING must say so just as loudly.
    """
    raise NotImplementedError(
        "classify_dallas_code() is a structural skeleton with no real "
        "Dallas class-code data (CLASSIFICATION_MAP_DALLAS is empty and "
        "DALLAS_CLASS_FIELD_NAME is unconfirmed). Do not wire this into a "
        "loader or call it against real data -- see this module's own "
        "docstring for what Diego needs to research first."
    )
