"""
classification_map_dallas.py — Dallas SPTB-to-benchmark overlay, per
MC-5 ("Classification as an Evidenced Diff against a Canonical Root"),
DALLAS-CLASS-1-rev.

SUPERSEDES the prior DRAFT SKELETON version of this file (which contained
zero real content — see git history — because no real DCAD sample export
existed yet). Real, evidenced content now exists: a real DCAD roll export
distribution (705,536 records) was directly examined, per DALLAS-CLASS-1's
original ask and DALLAS-CLASS-1-rev's correction (Fable's review of the
first send caught one real arithmetic error — G's total dropped its G30
component — and one real precedent inconsistency — the original send's
O -> Residential proposal did not match Travis's own current code, corrected
below). Every number and mapping decision in this file is traceable to that
real distribution or to tax_logic/classify.py's own already-shipped,
already-live Travis precedent — never a plausible-sounding guess.

MC-5's rule, in full (this file's whole design follows it):
  1. One canonical root: a single statewide Comptroller-scheme taxonomy is
     the base artifact. Each county gets an OVERLAY (its diff against the
     root), not a chain of county-to-county diffs.
  2. Every divergence carries direct evidence: a code-distribution
     comparison, county documentation citation, or measured sample.
  3. Unknown codes are conserved, not dropped: any code that maps to
     neither root nor overlay lands in an explicit `unmapped` bucket that
     the conservation gate counts and reports.
  4. Source Registry + County Profile + Classification Map remain
     Fable-reviewed before any new county data load.

REAL, DIRECT TRAVIS-PARITY EVIDENCE (the prerequisite Fable required before
this could route) — checked verbatim against tax_logic/classify.py's real,
current, already-live label_case_sql():

    -- 'O' ("Other real property"), 'G' (minerals/oil & gas), 'J'
    -- (industrial/utility real property), and 'L' (Personal
    -- Property...) are intentionally NOT mapped here. 'O'/'G'/'J' are
    -- each a distinct Comptroller top-level category, not a
    -- sub-type of the five benchmark ones... All four fall through to
    -- NULL / the literal "Other" label rather than being forced into one
    -- of the five real-estate benchmark categories.

Travis's own established, currently-live precedent leaves O, G, and J all
deliberately unmapped — not just G and J. This is the direct evidence that
corrected DALLAS-CLASS-1's original O -> Residential proposal (which would
have created exactly the cross-county drift Fable's review exists to catch)
and makes the standing default for this file unambiguous: G/J/O route to
the real, gate-counted `unmapped` bucket below, matching Travis exactly,
none of the three force-mapped. Per MC-5 rule 3, this is NOT the same thing
as an unrecognized/never-seen code — every one of G/J/O is a real, known,
distinct Comptroller top-level category that this overlay deliberately
declines to fold into one of the five benchmark categories, the same
"don't force a fit" reasoning Travis's own module already applies.

If a future, real, platform-wide decision changes this (e.g. adding a
sixth benchmark category), that is its own separate, Travis-touching brief
— never a Dallas-only edit. (DALLAS-CLASS-1-rev item 3.)

CANONICAL ROOT
Texas Comptroller Property Classification Guide (PTAD, Publication #96-313,
January 2022) — the real, official, statewide first-letter scheme every
Texas CAD's EARS submission follows. Travis's own state_cd1 mapping
(tax_logic/classify.py) is itself one CAD's overlay on this same root, not
a separate scheme — this module overlays the same real root directly, per
MC-5 rule 1, reusing Travis's own BENCHMARK_LABELS vocabulary (imported
below) rather than redeclaring it, so the two counties' benchmark taxonomy
can never drift apart by accident.

DCAD FIELD: the real field name is "SPTB CLASS CODE" (per MC-5's own
provenance note). Real observed codes are two-letter-plus-digit (e.g.
"A11", "F10") — the SPTB scheme's own sub-type granularity, one level more
granular than Travis's bare-letter state_cd1 convention.

REAL, OFFICIAL DCAD CROSS-REFERENCE (DALLAS-CLASS-2) — subcategory-level
mapping confirmed directly by DCAD's own `SPTD_CD_XREF.pdf` (document
dated 2011-07-25, distributed inside DCAD's current 2026 Current+Supplemental
product; verified 2026-08-21). This upgrades the disclosure immediately
below from "not confirmed at the subcategory level" to a real, closing
citation — the gap that disclosure flagged is now answered, not just
deleted. See DCAD_SPTD_CD_XREF_2011 below for the full, verbatim
cross-reference table this module now cites directly, and
DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED for the real codes this document
surfaces that do not (yet) appear in the certified roll's own observed
distribution.

Real, required date honesty (Fable's second-pass review, 2026-08-21): the
document itself is dated 2011-07-25, not 2026-current. It is real,
still-live evidence — DCAD continues to distribute it unchanged, bundled
inside the current 2026 Current+Supplemental product (current-by-inclusion,
not stale-by-neglect) — but every citation of it in this module says so
explicitly rather than implying fresh 2026 verification. This distinction
is load-bearing for the D20 finding below (see DALLAS_SPTB_DOCUMENTED_BUT_
UNOBSERVED["D20"]): the document's own definition of D2/D20 predates a
real, subsequent statewide PTAD scheme change, and the two sources
genuinely disagree on that one code.

Real, worth-noting convergence (Fable's own observation, DALLAS-CLASS-2
item 6): DCAD's own cross-reference independently routes its Watercraft
(M10) and Aircraft (M20) codes to PTAD class L1 — the SAME letter Travis's
own tax_logic/classify.py deliberately leaves unmapped as Business
Personal Property (see that module's "'L' is the Comptroller's own
Personal Property code" comment). Two counties, two independent real
sources, converging on the same real canonical-root letter for the same
real property type — direct, real evidence the MC-5 overlay model (one
canonical root, county-specific diffs recorded against it) is working as
designed, not just a hoped-for property of it.

HONEST DISCLOSURE, UPDATED PER DALLAS-CLASS-2 (this module's classification
LOGIC is unchanged — see "Real, explicit non-changes" in this brief's own
final report): this module still classifies on the FIRST CHARACTER of the
SPTB code only, exactly like Travis's own LEFT(UPPER(state_cd1), 1)
convention (tax_logic/classify.py's label_case_sql()) — subcategory-level
confirmation from DCAD_SPTD_CD_XREF_2011 does not change that grain
decision, it closes the evidentiary gap behind it: every A-prefix,
B-prefix, etc. sub-code observed in the real distribution is now directly
confirmed (not merely inferred) to belong to the Comptroller category its
first letter already implies. The one real exception this closer look
surfaced — M10/M20 (Watercraft/Aircraft) mapping to PTAD L1 rather than
M1, unlike M31/M32 which genuinely are M1 — is documented explicitly in
DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED below rather than silently absorbed,
since neither M10 nor M20 has been observed in the certified roll's own
distribution (only M31 has) and no behavior change is required until one
does.

REAL, CORRECTED SPTB DISTRIBUTION (705,536 records total, independently
re-summed and confirmed exact — see test_classification_map_dallas.py
for the full 21-code breakdown and the fixture test that encodes this
calibration):

    Mapped total:   697,564
      A (Residential):    569,020  (A11+A13+A12+A20)
      B (Multi-Family):    19,840  (B12+B11)
      C (Land/Vacant):     56,400  (C11+C12+C13+C14)
      F (Commercial):      38,223  (F10+F20)
      D/E (Agricultural):   2,382  (D10+E11)
      M (Residential):     11,699  (M31)
    Unmapped total:  7,972  (~1.13% of the file — NOT the original brief's
                     incorrect "~0.6%"; the O-correction below roughly
                     doubled the real unmapped share; still a real, small,
                     non-blocking fraction)
      G:  3,198  (G10+G30 — G30 was DROPPED in the original send; fixed)
      J:  1,176  (J30+J51)
      O:  3,598  (O10+O11 — moved here from the original send's incorrect
                  Residential mapping, per the Travis-parity evidence above)
    697,564 + 7,972 = 705,536 -- matches the source file's own real total
    exactly, the conservation identity Fable's review required.
"""
from tax_logic.classify import BENCHMARK_LABELS

# ── DCAD_SPTD_CD_XREF_2011 — the real, official cross-reference, cited
# directly (DALLAS-CLASS-2) ───────────────────────────────────────────────
# Source: "DCAD SPTD Code / PTAD Property Class Code Cross-Reference List"
# (SPTD_CD_XREF.pdf), a real, official DCAD document. Document itself dated
# 2011-07-25 -- NOT a 2026-current document, but real, still-live evidence:
# DCAD continues to distribute it unchanged, bundled inside the current
# 2026 Current+Supplemental product (DCAD2026_CURRENT.ZIP, alongside the
# real, current 2026-07-08 TABLES AND FIELD NAMES.xlsx) -- current by
# inclusion in DCAD's own current product, not stale by neglect. Verified
# directly via `pdftotext` against the real document (2026-08-21); every
# row below is transcribed verbatim, not re-derived or summarized. Every
# citation of this table elsewhere in this module names the 2011-07-25
# date explicitly, per Fable's own required framing -- see module
# docstring's "REAL, OFFICIAL DCAD CROSS-REFERENCE" section.
#
# Shape: {dcad_sptd_code: (ptad_property_class_code, description), ...}.
# Order matches the source document's own row order (not alphabetized),
# including its own real quirk of interleaving M10/M20 between L10 and L20
# -- preserved here rather than "corrected" into a tidier order, since this
# is a direct citation, not a paraphrase.
DCAD_SPTD_CD_XREF_2011 = {
    "A11": ("A",  "SINGLE FAMILY RESIDENCES"),
    "A12": ("A",  "SFR - TOWNHOUSES"),
    "A13": ("A",  "SFR - CONDOMINIUMS"),
    "A20": ("A",  "MOBILE HOME ON OWNERS LAND"),
    "B11": ("B",  "MFR - APARTMENTS"),
    "B12": ("B",  "MFR - DUPLEXES"),
    "C11": ("C",  "SFR - VACANT LOTS/TRACTS"),
    "C12": ("C",  "COMMERCIAL - VACANT PLOTTED LOTS/TRACTS"),
    "C13": ("C",  "INDUSTRIAL - VACANT PLOTTED LOTS/TRACTS"),
    "C14": ("C",  "RURAL VACANT - LESS THAN 5 ACRES"),
    "D10": ("D1", "QUALIFIED AGRICULTURAL LAND"),
    "D20": ("D2", "NON-QUALIFIED LAND"),
    "E11": ("E",  "RANCH IMPROVEMENTS"),
    "E12": ("E",  "FARM IMPROVEMENTS"),
    "F10": ("F1", "COMMERCIAL IMPROVEMENTS"),
    "F20": ("F2", "INDUSTRIAL IMPROVEMENTS"),
    "G10": ("G1", "OIL, GAS AND MINERAL RESERVES"),
    "G30": ("G3", "MINERALS, NON-PRODUCING"),
    "J10": ("J",  "PRIVATE WATER SYSTEMS"),
    "J20": ("J",  "GAS COMPANIES"),
    "J30": ("J",  "ELECTRIC COMPANIES"),
    "J40": ("J",  "TELEPHONE COMPANIES"),
    "J51": ("J",  "RAILROAD CORRIDOR"),
    "J52": ("J",  "RAILROAD ROLLING STOCK"),
    "J60": ("J",  "PIPELINES"),
    "J70": ("J",  "CABLE COMPANIES"),
    "L10": ("L1", "COMMERCIAL BPP"),
    "M10": ("L1", "WATERCRAFT"),
    "M20": ("L1", "AIRCRAFT"),
    "L20": ("L2", "INDUSTRIAL BPP"),
    "M31": ("M1", "MOBILE HOMES ON LEASED SPACES"),
    "M32": ("M1", "MOBILE HOMES FOR SALE (ON LOTS)"),
    "N10": ("N",  "INTANGIBLES"),
    "O10": ("O",  "RESIDENTIAL - VACANT LOTS AS INVENTORY"),
    "O11": ("O",  "RESIDENTIAL - IMPROVEMENTS AS INVENTORY"),
    "S10": ("S",  "SPECIAL INVENTORY"),
}

# ── DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED — real codes DCAD_SPTD_CD_XREF_2011
# confirms exist, that do NOT appear in the certified roll's own real
# 705,536-record observed distribution (DALLAS-CLASS-2). Documentation
# only -- per this brief's own explicit non-changes, none of these change
# DALLAS_SPTB_TO_BENCHMARK, classify_dallas_sptb_code()'s logic, or
# test_classification_map_dallas.py's expected totals. Kept here, keyed by
# the real DCAD SPTD code, so a future session hitting one of these for
# real (a different Dallas product, a later roll vintage) finds the
# reasoning already recorded rather than re-deriving it under time
# pressure. Ordered by Fable's own explicit priority ranking (D20 first
# and most important; L10/L20 more practically urgent than N/S; M10/M20
# real-but-not-yet-observed; N10/S10 least urgent).
DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED = {
    "D20": (
        "REAL, MOST IMPORTANT ENTRY -- a genuine semantic-drift caution, "
        "not merely an unconfirmed code (per Fable's own explicit ranking, "
        "ahead of every other entry in this ledger). DCAD_SPTD_CD_XREF_2011 "
        "(dated 2011-07-25) defines D20/D2 as 'NON-QUALIFIED LAND' -- but "
        "the modern, current statewide PTAD scheme has since moved "
        "non-qualified rural land to Category E, and redefined D2 as "
        "improvements on qualified open-space land instead. This is the "
        "one code where this module's two real evidence sources (the 2011 "
        "DCAD document and the current statewide PTAD guide) genuinely "
        "disagree with each other. It interacts directly with the overlay: "
        "first-character 'D' -> Agricultural is correct for the real, "
        "observed D10 population (Qualified Agricultural Land), but a "
        "future, real D20 record would classify as Agricultural under "
        "this module's current first-character logic while actually "
        "being, under EITHER definition, something else -- non-qualified "
        "land per the 2011 doc, or an improvement per current PTAD. Only "
        "D10 appears in the certified roll's real, observed distribution "
        "-- no behavior change required now -- but this is the one code "
        "in this entire ledger where blind first-character classification "
        "is KNOWN to be wrong if it ever fires, unlike every other entry "
        "below, which is merely unconfirmed rather than known-wrong."
    ),
    "M10": (
        "Real, documented-but-unobserved nuance (DALLAS-CLASS-2 item 3): "
        "DCAD_SPTD_CD_XREF_2011 maps M10 ('WATERCRAFT') to PTAD class L1, "
        "NOT M1 -- unlike M31/M32 (actual mobile homes), which correctly "
        "map to PTAD's M1 and correctly benchmark as Residential via this "
        "module's 'M' -> 'Residential' entry. DCAD's own M-prefix codes "
        "are therefore NOT uniform: a naive 'first character M -> "
        "Residential' rule is wrong for M10/M20 specifically. This "
        "module's current logic already handles the REAL, OBSERVED data "
        "correctly, since only M31 appears in the certified roll's actual "
        "distribution -- but if a future, real Dallas product (a "
        "different export, a later vintage) ever surfaces an M10 record, "
        "classify_dallas_sptb_code('M10') would currently return "
        "'Residential', which is wrong -- it is genuinely PTAD L1 "
        "(Personal Property), the same category as L10/L20 below. Real, "
        "worth-noting convergence: DCAD independently routes Watercraft "
        "to the same PTAD 'L' letter Travis's own tax_logic/classify.py "
        "deliberately leaves unmapped as Business Personal Property -- "
        "direct, real evidence the MC-5 overlay model is working (see "
        "module docstring)."
    ),
    "M20": (
        "Same real nuance as M10 immediately above -- DCAD_SPTD_CD_XREF_2011 "
        "maps M20 ('AIRCRAFT') to PTAD class L1, not M1. Not observed in "
        "the certified roll's real distribution; no behavior change "
        "required now. See M10's entry for the full reasoning (both codes "
        "share the identical real risk if either is ever observed)."
    ),
    "L10": (
        "Real, more practically urgent than N10/S10 below (per Fable's "
        "own review) -- unlike M10/M20, L10 is NOT hypothetical. "
        "DCAD_SPTD_CD_XREF_2011 maps L10 ('COMMERCIAL BPP') to PTAD class "
        "L1. Confirmed present in Dallas TRW billing data, 2026-08-17 "
        "vintage (trwfile_748978.zip): 565,970 real records. "
        "First-character 'L' already correctly falls through to this "
        "module's unmapped bucket (L is absent from "
        "DALLAS_SPTB_TO_BENCHMARK) -- Travis parity holds, this is the "
        "same 'L' tax_logic/classify.py's own comment already names as "
        "Business Personal Property, deliberately unmapped there too. No "
        "behavior change required. A future session building the real "
        "Dallas billing loader will hit real L codes on day one -- this "
        "ledger entry is the first place they should look."
    ),
    "L20": (
        "Same real category as L10 immediately above -- DCAD_SPTD_CD_XREF_2011 "
        "maps L20 ('INDUSTRIAL BPP') to PTAD class L2. First-character "
        "'L' already correctly falls through to the unmapped bucket, "
        "matching Travis's own L-unmapped precedent. No behavior change "
        "required. Real population not yet separately confirmed the way "
        "L10's 565,970-record TRW figure is (out of scope for this "
        "documentation-only brief to go measure)."
    ),
    "N10": (
        "Real, documented-but-unobserved category (DALLAS-CLASS-2 item 5) "
        "-- DCAD_SPTD_CD_XREF_2011 confirms N10 ('INTANGIBLES') exists in "
        "DCAD's own official scheme, PTAD class N. Not observed in the "
        "certified roll's real distribution. First-character 'N' already "
        "correctly falls through to the unmapped bucket (N is absent from "
        "DALLAS_SPTB_TO_BENCHMARK, matching G/J/O's own already-"
        "established unmapped treatment). No behavior change required -- "
        "documentation addition only."
    ),
    "S10": (
        "Real, documented-but-unobserved category (DALLAS-CLASS-2 item 5) "
        "-- DCAD_SPTD_CD_XREF_2011 confirms S10 ('SPECIAL INVENTORY') "
        "exists in DCAD's own official scheme, PTAD class S. Not observed "
        "in the certified roll's real distribution. First-character 'S' "
        "already correctly falls through to the unmapped bucket, same "
        "treatment as G/J/N/O. No behavior change required -- "
        "documentation addition only."
    ),
}

# ── The unmapped sentinel (MC-5 rule 3: "conserved, not dropped") ────────
# Deliberately NOT one of BENCHMARK_LABELS -- a code that classifies here is
# a real, known Comptroller top-level category this overlay declines to
# force into one of the five benchmark categories, not an error and not a
# genuinely unrecognized code. Lowercase, matching MC-5's own wording
# ("an explicit `unmapped` bucket").
UNMAPPED_DALLAS = "unmapped"

# The real DCAD field this module classifies (per MC-5's own provenance
# note, quoted in the module docstring). Supersedes the prior skeleton's
# unconfirmed `DALLAS_CLASS_FIELD_NAME = None` placeholder.
DALLAS_SPTB_FIELD_NAME = "SPTB CLASS CODE"

# ── DALLAS_SPTB_TO_BENCHMARK — the overlay itself ─────────────────────────
# First-letter grain only (see module docstring's HONEST DISCLOSURE). Every
# key here is a real Comptroller top-level category confirmed present in
# the real 705,536-record distribution this brief examined, AND (per
# DALLAS-CLASS-2) directly confirmed at the sub-code level by DCAD's own
# official DCAD_SPTD_CD_XREF_2011 table above (document dated 2011-07-25;
# see module docstring for the required date-honesty framing). G, J, and O
# are deliberately ABSENT from this dict -- see classify_dallas_sptb_code()
# below for how their absence routes to UNMAPPED_DALLAS, matching Travis's
# own live precedent exactly (see module docstring's Travis-parity section).
DALLAS_SPTB_TO_BENCHMARK = {
    # Category A -- Real Property: Single-Family Residential. Real
    # distribution: A11=508,295, A13=39,486, A12=19,885, A20=1,354 ->
    # 569,020 total, the single largest population in the file (80.7%).
    # Matches tax_logic/classify.py's own "A" -> "Residential" precedent
    # exactly (_STATE_PREFIX_LABEL). Per DALLAS-CLASS-2: all four sub-codes
    # confirmed directly by DCAD_SPTD_CD_XREF_2011 -- A11/A12/A13/A20 all
    # PTAD class "A", all genuinely single-family-residential-shaped
    # descriptions (residences, townhouses, condos, mobile-home-on-owned-
    # land), no surprise sub-code hiding a different real category.
    "A": "Residential",

    # Category B -- Real Property: Multifamily Residential. Real
    # distribution: B12=15,298, B11=4,542 -> 19,840 total. Matches
    # Travis's own "B" -> "Multi-Family" precedent exactly. Per
    # DALLAS-CLASS-2: both sub-codes confirmed by DCAD_SPTD_CD_XREF_2011 --
    # B11/B12 both PTAD class "B" (apartments, duplexes).
    "B": "Multi-Family",

    # Category C -- Real Property: Vacant Lots and Tracts. Real
    # distribution: C11=28,799, C12=24,533, C13=1,985, C14=1,083 -> 56,400
    # total. Matches Travis's own "C" -> "Land/Vacant" precedent exactly.
    # Per DALLAS-CLASS-2: all four sub-codes confirmed by
    # DCAD_SPTD_CD_XREF_2011 -- all PTAD class "C" (SFR/commercial/
    # industrial vacant lots, rural vacant under 5 acres) -- genuinely
    # vacant-land-shaped across every observed sub-code, including the
    # two (C12/C13) whose descriptions name "commercial"/"industrial" but
    # which the real document confirms are vacant PLATTED LOTS, not
    # improved commercial/industrial property.
    "C": "Land/Vacant",

    # Category D -- Real Property: Qualified Open-Space/Ag Land. Real
    # distribution: D10=2,304. Matches Travis's own "D" -> "Agricultural"
    # precedent exactly. Per DALLAS-CLASS-2: D10 confirmed by
    # DCAD_SPTD_CD_XREF_2011 as PTAD class "D1", "QUALIFIED AGRICULTURAL
    # LAND" -- a clean, direct match, unlike D20 (see
    # DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED["D20"] for the one real
    # semantic-drift caution this closer look surfaced -- D20 is NOT
    # observed in the real distribution, so it does not affect this
    # module's current behavior, but the caution is real and recorded).
    "D": "Agricultural",

    # Category E -- DOCUMENTED DIVERGENCE, not a direct root match (per
    # Fable's review, DALLAS-CLASS-1-rev refinement 1). PTAD's own guide
    # defines Category E as "rural land, not qualified for open-space
    # appraisal, and residential improvements" -- NOT agricultural land
    # itself. This mapping (E -> Agricultural) is for real, platform
    # consistency with Travis's own already-established D/E -> Agricultural
    # treatment (tax_logic/classify.py line 171: `LEFT(UPPER(state_col), 1)
    # IN ('D', 'E') THEN 'Agricultural'`), NOT because the Comptroller
    # guide itself supports a direct E -> Agricultural read. Real
    # distribution: E11=78 (de minimis -- 0.011% of the file). Flagged here
    # explicitly so a future reviewer does not mistake this line for "the
    # root says E is Agricultural" -- it does not; Travis's overlay does,
    # and Dallas matches Travis's overlay, per MC-5 rule 1 (one canonical
    # root, diffs recorded against it, not a fresh taxonomy). Per
    # DALLAS-CLASS-2: E11/E12 both confirmed by DCAD_SPTD_CD_XREF_2011 as
    # PTAD class "E" ("RANCH IMPROVEMENTS"/"FARM IMPROVEMENTS") -- real,
    # direct confirmation that E11 (the only sub-code actually observed) is
    # what it claims to be; the divergence documented above is about
    # Category E's meaning under the current statewide PTAD guide, not
    # about whether DCAD's own sub-code labeling is accurate.
    "E": "Agricultural",

    # Category F -- Real Property: Commercial. Real distribution:
    # F10=37,412, F20=811 -> 38,223 total. Matches Travis's own
    # "F" -> "Commercial" precedent exactly. Per DALLAS-CLASS-2: both
    # sub-codes confirmed by DCAD_SPTD_CD_XREF_2011 -- F10 PTAD class "F1"
    # (Commercial Improvements), F20 PTAD class "F2" (Industrial
    # Improvements) -- both genuinely Commercial-shaped.
    "F": "Commercial",

    # Category M -- Mobile Homes. Real distribution: M31=11,699 (1.66% of
    # the file). Matches Travis's own bare-"M" -> "Residential" precedent
    # exactly (tax_logic/classify.py's _STATE_PREFIX_LABEL: "M": "Residential").
    # Per DALLAS-CLASS-2: M31 confirmed by DCAD_SPTD_CD_XREF_2011 as PTAD
    # class "M1", "MOBILE HOMES ON LEASED SPACES" -- a real, direct match.
    # IMPORTANT, closer-look finding: DCAD's own M-prefix codes are NOT
    # uniform -- M10 (Watercraft) and M20 (Aircraft) map to PTAD class L1
    # (Personal Property), NOT M1, per the same cross-reference. Neither is
    # observed in the certified roll's real distribution, so this module's
    # current first-character logic is correct for the data actually seen
    # -- but see DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED["M10"] for the real
    # risk if either is ever observed in a future Dallas product.
    #
    # REAL, DOCUMENTED FIELD-SEMANTICS QUIRK (DALLAS-CLASS-1-rev refinement
    # 2, belongs in the Dallas County Profile per MC-7.1's field-semantics-
    # baseline requirement -- see test_classification_map_dallas.py's
    # module docstring for the exact one-line note to add there, and this
    # module's own module docstring for why the local repo has no committed
    # copy of that Notion-hosted document to edit directly): 11,699 real
    # M-category records appear in a DCAD roll export file whose own name
    # states "real property only." This is a semantic quirk of how DCAD
    # composes this specific roll export, not a data error -- worth one
    # documented line in the Dallas County Profile specifically because
    # Harris may compose its own roll differently, and a future session
    # should not have to rediscover this from scratch.
    "M": "Residential",
}

# ── Real evidence notes for the three refinements Fable's review required
# (DALLAS-CLASS-1-rev items 1-3) -- kept as a structured, greppable trail
# alongside the inline comments above, mirroring classification_map.py's
# own FLAGGED_FOR_REVIEW convention. These are RESOLVED decisions (this
# rev IS the Fable-reviewed correction), not open flags -- kept here so a
# future reviewer can find the reasoning without re-deriving it.
DALLAS_SPTB_EVIDENCE_NOTES = {
    "E": (
        "E -> Agricultural is a documented DIVERGENCE from the canonical "
        "root, not a direct match. PTAD Publication #96-313 defines "
        "Category E as 'rural land, not qualified for open-space "
        "appraisal, and residential improvements' -- not agricultural "
        "land per se. Mapped to Agricultural here ONLY for platform "
        "consistency with Travis's own established D/E -> Agricultural "
        "treatment (tax_logic/classify.py line 171). Do not cite the PTAD "
        "guide itself as support for a direct E -> Agricultural read."
    ),
    "M": (
        "M31 (11,699 real records) appears in a DCAD roll export file "
        "whose own name states 'real property only' -- a real, documented "
        "semantic quirk of how DCAD composes this specific export, not a "
        "data error. Belongs in the Dallas County Profile (MC-7.1's "
        "field-semantics baseline) as one documented line, since Harris "
        "may compose its own roll differently and a future session "
        "shouldn't have to rediscover this. Not yet added there as of "
        "this file's own commit -- the Dallas County Profile lives in "
        "Notion, not this repo, per this module's own docstring; see "
        "DALLAS-CLASS-1-rev's final report for the exact line and its "
        "delivery status."
    ),
    "G/J/O": (
        "Real, direct Travis-parity evidence (checked verbatim against "
        "tax_logic/classify.py's live label_case_sql()) confirms Travis's "
        "own current code leaves G, J, AND O all deliberately unmapped -- "
        "not just G and J. This corrected DALLAS-CLASS-1's original send, "
        "which had proposed O -> Residential (a real, more significant "
        "precedent inconsistency than the original send's dropped-G30 "
        "arithmetic error). The bucket-policy framing is now single, not "
        "split: this is Diego's call in principle, but the real, "
        "confirmed Travis precedent makes the standing default "
        "unambiguous -- G/J/O unmapped, matching Travis exactly. A future "
        "real, platform-wide decision to add a sixth benchmark category "
        "would be its own separate, Travis-touching brief, never a "
        "Dallas-only edit."
    ),
}


def classify_dallas_sptb_code(value):
    """Classify one real DCAD SPTB code into a benchmark label or the
    UNMAPPED_DALLAS sentinel. NEVER raises -- every first letter this
    overlay might see is either a mapped benchmark category or a real,
    known-but-deliberately-unmapped Comptroller category (G/J/O) or an
    absent/blank value, none of which are an "UNKNOWN, halt the load"
    situation the way classification_map.py's Stage-1 REAL_PROPERTY
    allowlist gate is -- this is a display/benchmark-taxonomy overlay
    (same purpose as tax_logic/classify.py's property_type_label()), not
    a load-time hard-stop gate.

    `value` is matched on its FIRST CHARACTER ONLY, uppercased, after
    stripping whitespace -- see module docstring's HONEST DISCLOSURE.
    None/blank returns UNMAPPED_DALLAS (conservative default -- a genuinely
    absent SPTB value is not evidence it belongs in any of the five
    benchmark categories, so it is conserved in the unmapped bucket per
    MC-5 rule 3 rather than silently guessed into one).
    """
    if value is None:
        return UNMAPPED_DALLAS
    key = str(value).strip().upper()
    if not key:
        return UNMAPPED_DALLAS
    first = key[0]
    return DALLAS_SPTB_TO_BENCHMARK.get(first, UNMAPPED_DALLAS)


def classify_dallas_distribution(counts_by_sptb_code):
    """MC-5 rule 3's conservation gate, made real and callable: takes a
    {sptb_code: count} distribution (the same shape as a GROUP BY SPTB
    query against a real DCAD export, or this brief's own real 21-code
    distribution -- see test_classification_map_dallas.py) and
    returns a structured report proving every real row is accounted for,
    either mapped to a benchmark category or conserved in the unmapped
    bucket -- never silently dropped.

    Returns:
        {
            "total": int,
            "mapped_total": int,
            "unmapped_total": int,
            "by_benchmark": {benchmark_label: count, ...},
            "by_unmapped_letter": {first_letter: count, ...},
            "conserved": bool,  # mapped_total + unmapped_total == total
        }
    """
    by_benchmark = {label: 0 for label in BENCHMARK_LABELS}
    by_unmapped_letter = {}
    total = 0
    for code, count in counts_by_sptb_code.items():
        total += count
        label = classify_dallas_sptb_code(code)
        if label == UNMAPPED_DALLAS:
            key = str(code).strip().upper()[:1] if code else "(blank)"
            by_unmapped_letter[key] = by_unmapped_letter.get(key, 0) + count
        else:
            by_benchmark[label] = by_benchmark.get(label, 0) + count

    mapped_total = sum(by_benchmark.values())
    unmapped_total = sum(by_unmapped_letter.values())
    return {
        "total": total,
        "mapped_total": mapped_total,
        "unmapped_total": unmapped_total,
        "by_benchmark": by_benchmark,
        "by_unmapped_letter": by_unmapped_letter,
        "conserved": (mapped_total + unmapped_total) == total,
    }
