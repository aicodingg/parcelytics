"""
classification_map.py — the Classification Map, per DATA_LIFECYCLE.md
Stage 1 (CLASSIFY) and Section 9.4a ("Classification Map enforcement must
land across all loaders at once with its harness assertion — an allowlist
with a bypass is a guarantee that lies").

WHAT THIS IS
Every observed `state_cd1` value in Travis County's real property table is
assigned to exactly one of four buckets:

    REAL_PROPERTY      — the allowlist. Only these rows may ever reach
                          production per DATA_LIFECYCLE.md Principle 2
                          ("Real property only, enforced at the door").
    PERSONAL_PROPERTY  — Comptroller-classified personal property (BPP,
                          equipment, inventory). Never loaded.
    EXEMPT_SYNTHETIC   — tax-exempt real accounts and non-taxable/synthetic
                          codes. Never loaded into the real-property table.
    UNKNOWN            — not a bucket a code can be assigned to. It is the
                          RUNTIME OUTCOME of looking up a code this module
                          has never seen. See enforce_real_property_only()
                          below — this is a hard stop, not a silent drop.

WHY THIS MODULE, NOT parcel_filters.py
parcel_filters.py (repo root) is query-time SQL scoping: it hands back SQL
fragments (CANONICAL_PARCEL_EXCL, exclude_non_real_property_gap_sql, ...)
that existing routes/loaders splice into WHERE clauses against data that is
*already* in production. This module is upstream of that: it is the Stage-1
CLASSIFY artifact DATA_LIFECYCLE.md describes — a per-row, load-time gate
that a *loader* consults BEFORE a row is written, expressed in Python (not
SQL) so it can raise and halt a load, not just filter a SELECT. Conflating
the two would mean the "allowlist enforced at the door" and "allowlist
enforced in some queries" are the same artifact wearing two hats — exactly
the false-confidence failure mode Section 9.4a warns against. They are
being kept as two modules so a future call site can migrate onto this one
without also inheriting parcel_filters.py's SQL-fragment API shape it does
not need. (parcel_filters.py itself is a real, flagged future consumer of
this module — see "CALL SITES THAT WOULD NEED TO CHANGE" below — that
migration is explicitly out of scope for this brief.)

GRAIN: FULL STRING, NOT FIRST CHARACTER — A DELIBERATE DEPARTURE
Every existing exclusion/classification mechanism in this codebase already
in production (CANONICAL_PARCEL_EXCL's `NOT LIKE 'X%%'`, label_case_sql()'s
`LEFT(UPPER(state_cd1), 1)`, query_state_cd1_prefixes.py's own
`LEFT(COALESCE(state_cd1,''), 1)` grouping) all operate on the FIRST
CHARACTER of state_cd1. This module deliberately does NOT follow that
precedent — it keys on the full observed string. Reasoning, found while
cross-referencing app.py's STATE_CD_DESCRIPTIONS (the fullest documented
enumeration of real Comptroller codes in this codebase) against the
first-char scheme:

    "ER": "Exempt — Religious" collapses to first-character "E" under the
    existing scheme — which is also the first character of "E": "Rural
    Land (Not Qualified for Open-Space Appraisal)", a REAL_PROPERTY code
    already kept in every benchmark. If ER genuinely appears as a literal
    state_cd1 value in production, first-character classification would
    silently let an exempt religious property through as if it were
    ordinary taxable rural land — the same shape of bug as the L-class
    contamination incident this whole document exists to prevent, just
    hiding behind a different letter. This has NOT been confirmed against
    live data (see "WHAT STILL NEEDS DIEGO'S LIVE 1a QUERY" below) — it is
    flagged here as exactly the kind of risk a full-string map is designed
    to catch structurally rather than trust a prefix scheme to avoid.

    Separately: STATE_CD_DESCRIPTIONS documents "M1": "Mobile Home" (real
    property — a manufactured home affixed to land, matches classi_cd
    13/14's "(Real)" designation already used elsewhere in this codebase)
    against "M2": "Other Tangible Personal Property" (NOT real property).
    The existing first-character scheme (tax_logic/classify.py's
    _STATE_PREFIX_LABEL: `"M": "Residential"`) cannot distinguish these —
    an M2 row would classify identically to an M1 row today. This is a
    SECOND potential contamination class of the same shape as the L-class
    incident, found by this same cross-reference, not previously
    documented anywhere in this codebase. Flagged prominently in this
    brief's report — also unconfirmed against live data (M2's real
    population in `parcel` is unknown from this sandbox).

Building the map at full-string grain costs nothing structurally (a dict
lookup is a dict lookup regardless of key length) and closes both of the
above as a matter of design rather than as a patch applied after a third
incident. New codes at any grain — a bare "Q" nobody has seen, or a "M3"
sub-code Travis starts using in a future year, or Dallas/Harris's own,
possibly entirely different code set — are UNKNOWN and halt the load
either way; the full-string grain only changes what happens with a
technically-known-but-differently-typed variant of an already-mapped
prefix, which is exactly the ER/M2 risk above.

WHAT STILL NEEDS DIEGO'S LIVE 1a QUERY (cannot be done from this sandbox —
no live DB access)
This map's domain (the 48 keys below) is built from the fullest documented
enumeration already in this codebase (app.py's STATE_CD_DESCRIPTIONS,
cross-referenced against KNOWN_LIMITATIONS.md's `query_state_cd1_prefixes.py`
population table and loaders/compute_metrics.py's TYPE_GROUPS comment) — not
invented, but also not a live query result. Three things need Diego's own
run of the real query below before this map can be trusted as ground truth
rather than "best available documentation, cross-referenced":

  1. `query_state_cd1_prefixes.py` (already in this repo) only ever grouped
     by `LEFT(state_cd1, 1)` — it can only tell you "no unrecognized FIRST
     CHARACTERS were found," which is a materially weaker claim than "no
     unrecognized VALUES were found." It cannot rule out ER, M2, or any
     other two-character code actually being present in the live column.
     Run this instead (full-string distinct, matching this map's grain):

         SELECT state_cd1, COUNT(*) AS cnt, SUM(market_value) AS total_mv
         FROM parcel
         GROUP BY state_cd1
         ORDER BY cnt DESC;

     Every non-NULL state_cd1 value returned must appear as a key in
     CLASSIFICATION_MAP below, or classify_state_cd1() will (correctly)
     raise UnknownClassCodeError on it. A value NOT in
     query_state_cd1_prefixes.py's original single-character breakdown at
     all (a genuinely new first character) would already have been caught
     by that script; a value that IS one of the 14 known first characters
     but a full string this map has never seen (the ER/M2 risk) would not
     have been — this query is what actually closes that gap.

  2. If ER and/or M2 (or any other 2-char sub-code) appear with nonzero
     rows: confirm which of REAL_PROPERTY/PERSONAL_PROPERTY/EXEMPT_SYNTHETIC
     each actually belongs in against the real rows found (e.g., pull a
     handful of ER geo_ids and check their exemption_codes / classi_cd, the
     same evidence-gathering pattern this session's classify.py L-class fix
     used), rather than trusting this module's provisional bucket blind.

  3. Per DATA_LIFECYCLE.md Stage 1's own PM checklist ("eyeball the
     PERSONAL_PROPERTY share against last year's... attach the report to
     the Ledger row") and Section 9.4's Fable-review requirement for new
     classification decisions — FLAGGED_FOR_REVIEW below lists every
     bucket assignment in this map that rests on secondary or conflicting
     documentation rather than a direct, unambiguous source, so Fable's
     review (required by DATA_LIFECYCLE.md §9.4 before Phase-1 enforcement
     is trusted) has a concrete, named starting list instead of having to
     re-derive it from scratch.

NULL state_cd1 IS NOT "UNKNOWN"
~17,175 Travis parcels (as of the June 2026 recount) have NULL state_cd1 —
every parcel newer than the 2021-2024 AJR extract this field is sourced
from. parcel_filters.py's own NULL-safety fix (this session, July 2026)
established the deliberate precedent that a NULL state_cd1 "isn't confirmed
exempt or personal property; we just don't know its exact type yet" and
should SURVIVE exclusion rather than be silently dropped. This module
preserves that precedent rather than silently reversing it: NULL is handled
as its own explicit case by classify_state_cd1() (see NULL_POLICY below),
not funneled through the UNKNOWN-hard-stop path. Reclassifying ~17K live,
real parcels as a load-blocking condition would be a real, material
behavior change this brief was not asked to make (out of scope: "Do NOT
refactor every existing real-property-exclusion call site") — flagged here
as a deliberate choice, not an oversight.

AJR-prefixed geo_id IS A SEPARATE MECHANISM, NOT PART OF THIS MAP
CANONICAL_PARCEL_EXCL's `geo_id NOT LIKE 'AJR%%'` leg operates on `geo_id`,
not `state_cd1` — a different column entirely, and this map's domain is
state_cd1 class-code prefixes per DATA_LIFECYCLE.md Stage 1's own wording
("every class prefix"). They overlap heavily in practice (per
KNOWN_LIMITATIONS.md's July 2026 L-class writeup, 42,082 of 42,293
state_cd1='L' rows already carry a synthetic AJR-prefixed geo_id, and the
remaining 211 non-AJR L rows are STILL state_cd1='L' — meaning this map's
"L1"/"L2" -> PERSONAL_PROPERTY entries below already catch every
state_cd1-classifiable instance of the AJR contamination pattern
structurally, once wired into a loader) but they are not the same
mechanism and this module does not attempt to absorb geo_id-prefix scoping.
Flagged, not solved, here.
"""

# ── The four buckets ─────────────────────────────────────────────────────
REAL_PROPERTY = "REAL_PROPERTY"
PERSONAL_PROPERTY = "PERSONAL_PROPERTY"
EXEMPT_SYNTHETIC = "EXEMPT_SYNTHETIC"
UNKNOWN = "UNKNOWN"  # never a dict value below — see module docstring.

VALID_BUCKETS = (REAL_PROPERTY, PERSONAL_PROPERTY, EXEMPT_SYNTHETIC)


class UnknownClassCodeError(Exception):
    """Raised by enforce_real_property_only() when a load contains one or
    more state_cd1 values absent from CLASSIFICATION_MAP. Per
    DATA_LIFECYCLE.md Stage 1: "UNKNOWN is a blocker, not a bucket: any
    prefix in the file absent from the map halts the load until a human
    classifies it and commits the map update." This exception IS that
    halt — callers must not catch-and-continue past it in a loader context;
    doing so silently re-creates exactly the failure mode this module
    exists to prevent."""
    pass


# ── The Classification Map ──────────────────────────────────────────────
# Domain = every state_cd1 value documented in app.py's STATE_CD_DESCRIPTIONS
# (the fullest existing enumeration in this codebase), cross-referenced
# against KNOWN_LIMITATIONS.md's live query_state_cd1_prefixes.py population
# table and loaders/compute_metrics.py's TYPE_GROUPS/_exclude_clause()
# comments. NOT itself a live query result — see the module docstring's
# "WHAT STILL NEEDS DIEGO'S LIVE 1a QUERY" section. Every entry below cites
# the evidence it rests on so a reviewer (Fable, per DATA_LIFECYCLE.md
# §9.4) can check the reasoning without re-deriving it.
CLASSIFICATION_MAP = {
    # ── Residential (REAL_PROPERTY) ──────────────────────────────────────
    # Bare "A": 334,227 parcels (64.6% of the county) per KNOWN_LIMITATIONS.md's
    # live query_state_cd1_prefixes.py table — the single largest population
    # in the database. A1-A9 are documented Comptroller sub-codes (Rule
    # 9.4001, per app.py's STATE_CD_DESCRIPTIONS comment) for the same
    # Residential category; A2 ("Manufactured Home") and A4/A5 ("Condominium")
    # are real-property sub-types, not personal property.
    "A":  REAL_PROPERTY, "A1": REAL_PROPERTY, "A2": REAL_PROPERTY,
    "A3": REAL_PROPERTY, "A4": REAL_PROPERTY, "A5": REAL_PROPERTY,
    "A9": REAL_PROPERTY,

    # ── Multi-Family (REAL_PROPERTY) ─────────────────────────────────────
    # Bare "B": 12,981 parcels per the live table. B1-B5 sub-codes per
    # STATE_CD_DESCRIPTIONS (Multifamily/Duplex/Triplex/Four-Plex/w-HS).
    "B":  REAL_PROPERTY, "B1": REAL_PROPERTY, "B2": REAL_PROPERTY,
    "B3": REAL_PROPERTY, "B4": REAL_PROPERTY, "B5": REAL_PROPERTY,

    # ── Land / Vacant (REAL_PROPERTY) ────────────────────────────────────
    # Bare "C": 38,719 parcels per the live table.
    "C": REAL_PROPERTY, "C1": REAL_PROPERTY, "C2": REAL_PROPERTY,

    # ── Agricultural (REAL_PROPERTY) ─────────────────────────────────────
    # Bare "D": 5,078 parcels; bare "E": 4,831 parcels, per the live table.
    # D1-D3/E1-E3 are documented open-space/non-qualified sub-codes, all
    # real property (Ag land and farm/ranch improvements on it).
    "D": REAL_PROPERTY, "D1": REAL_PROPERTY, "D2": REAL_PROPERTY, "D3": REAL_PROPERTY,
    "E": REAL_PROPERTY, "E1": REAL_PROPERTY, "E2": REAL_PROPERTY, "E3": REAL_PROPERTY,

    # ── Commercial (REAL_PROPERTY) ───────────────────────────────────────
    # Bare "F": 15,132 parcels per the live table. F1 (14,660) and F2 (472)
    # sub-code populations independently confirmed live per app.py's
    # use_code_case_sql() docstring (Diego's check_other_property_type_fix.py
    # Section 0 run) — real, populated, real property.
    "F":  REAL_PROPERTY, "F1": REAL_PROPERTY, "F2": REAL_PROPERTY,
    "F3": REAL_PROPERTY, "F4": REAL_PROPERTY, "F5": REAL_PROPERTY,

    # ── Minerals / Utilities (REAL_PROPERTY) ─────────────────────────────
    # "G" (6 parcels, de minimis) and "J" (1,524 parcels) per the live
    # table — compute_metrics.py's comment explicitly characterizes both as
    # real property ("government-assessed parcels", "industrial / utility
    # real property"), just not yet mapped into one of the 5 DISPLAY
    # benchmark categories (Residential/Multi-Family/Commercial/Land/Ag) —
    # that is a separate, cosmetic taxonomy question from "is this real
    # property that belongs in production at all," which is this map's
    # only job. G1-G3/J1-J9 sub-codes per STATE_CD_DESCRIPTIONS.
    "G1": REAL_PROPERTY, "G2": REAL_PROPERTY, "G3": REAL_PROPERTY,
    "J1": REAL_PROPERTY, "J2": REAL_PROPERTY, "J3": REAL_PROPERTY,
    "J4": REAL_PROPERTY, "J5": REAL_PROPERTY, "J6": REAL_PROPERTY,
    "J7": REAL_PROPERTY, "J8": REAL_PROPERTY, "J9": REAL_PROPERTY,

    # ── Manufactured homes — REAL vs PERSONAL split, NOT uniform ─────────
    # "M1": "Mobile Home" -- real property once affixed to land under TX
    # law (classi_cd 13/14 "(Real)" corroborates, per tax_logic/classify.py's
    # own reasoning for the bare-"M" -> Residential mapping it already
    # ships). "M2": "Other Tangible Personal Property" -- NOT real
    # property, per STATE_CD_DESCRIPTIONS's own label. These two do NOT
    # share a bucket -- see the module docstring's "GRAIN" section for why
    # this is a genuine, newly-found risk in the existing first-character
    # scheme (which cannot make this distinction) and FLAGGED_FOR_REVIEW
    # below. Bare "M": 10,699 parcels per the live table -- kept here as
    # REAL_PROPERTY for continuity with the existing, already-shipped
    # tax_logic/classify.py mapping, on the working assumption the live
    # count is dominated by M1-equivalent rows; unconfirmed whether the
    # live column ever actually populates bare "M" vs. always "M1"/"M2".
    "M":  REAL_PROPERTY,
    "M1": REAL_PROPERTY,
    "M2": PERSONAL_PROPERTY,

    # ── Personal Property (PERSONAL_PROPERTY) ────────────────────────────
    # L1/L2: 42,504 parcels (8.2% of the county) per the live table --
    # confirmed PERSONAL_PROPERTY (not Commercial real estate) by this
    # session's own July 2026 investigation (KNOWN_LIMITATIONS.md's
    # "state_cd1='L' is Personal Property, not Commercial real estate"
    # section) -- the exact contamination class this whole document exists
    # to make structurally impossible going forward.
    "L1": PERSONAL_PROPERTY,
    "L2": PERSONAL_PROPERTY,

    # ── Exempt / Non-taxable / Synthetic (EXEMPT_SYNTHETIC) ──────────────
    # "X"/"X1": 13,998 parcels per the live table -- tax-exempt (churches,
    # government, nonprofits per parcel_filters.py's own docstring, which
    # further notes real underlying sub-codes seen in the live column
    # include XV/XB/XU/XI/XJ/XR/XD/XG/XO/XL/XN/XA -- NONE of which are
    # separately enumerated as map keys here since STATE_CD_DESCRIPTIONS
    # only documents bare "X"/"X1"; see FLAGGED_FOR_REVIEW for this gap).
    # "ER": "Exempt -- Religious" per STATE_CD_DESCRIPTIONS -- see the
    # module docstring's GRAIN section for why this is kept SEPARATE from
    # "E" (Agricultural, REAL_PROPERTY) rather than collapsed by first
    # character.
    "X":  EXEMPT_SYNTHETIC,
    "X1": EXEMPT_SYNTHETIC,
    "ER": EXEMPT_SYNTHETIC,

    # "N": 3 parcels (de minimis) per the live table. STATE_CD_DESCRIPTIONS
    # (sourced from Comptroller Rule 9.4001) labels it "Non-Taxable" --
    # parcel_filters.py's own docstring instead calls it "personal property
    # -- but only 3 parcels, a different Comptroller code than L". These
    # two characterizations disagree on WHY it's excluded, though not on
    # THAT it's excluded (both keep it out of REAL_PROPERTY either way).
    # Bucketed EXEMPT_SYNTHETIC here on the Comptroller-sourced label's
    # authority; flagged in FLAGGED_FOR_REVIEW since the two sources
    # disagree and only 3 real rows exist to check it against.
    "N": EXEMPT_SYNTHETIC,

    # "O": 19,986 parcels (3.9%) per the live table -- TCAD's own
    # "Other/Unclassified" catch-all. NOT tax-exempt, NOT personal
    # property -- KNOWN_LIMITATIONS.md's live table explicitly calls it
    # "Other real property (real estate, kept in benchmarks)", and
    # tax_logic/classify.py's own live-data investigation found 76% of "O"
    # parcels (81% of value) are classi_cd '01' Single-Family Residence --
    # real, specific evidence of real-property character, not "unknown
    # type." Kept as REAL_PROPERTY -- it is simply not yet mapped to one
    # of the 5 DISPLAY benchmark categories, a separate, cosmetic question
    # this map does not need to resolve (see classify.py's own open
    # question about whether to reclassify dominant O sub-populations,
    # explicitly left to Diego there, unaffected by this map).
    "O": REAL_PROPERTY,

    # "S": 751 parcels (0.1%) per the live table. STATE_CD_DESCRIPTIONS
    # alone is ambiguous ("Special / State Property"), but
    # loaders/compute_metrics.py's TYPE_GROUPS comment explicitly
    # characterizes it as "state-assessed utility real property" -- state-
    # assessed (centrally valued by the Comptroller rather than locally)
    # utilities are still real property under the Texas system (same
    # category as the J-prefix utility codes above, just centrally rather
    # than locally assessed). Kept REAL_PROPERTY on that more specific
    # characterization; flagged since STATE_CD_DESCRIPTIONS's own label
    # alone would not have been sufficient evidence.
    "S": REAL_PROPERTY,
}

# ── NULL state_cd1 policy — see module docstring ─────────────────────────
# NULL is not a key in CLASSIFICATION_MAP (it cannot be -- dict keys can't
# be the SQL NULL a Python None represents ambiguity around, and "no value
# present" is a different condition from "a value present that's
# unrecognized"). classify_state_cd1(None) below returns this bucket
# directly, preserving parcel_filters.py's own established NULL-safety
# precedent (NULL survives exclusion; it is not confirmed non-real-property)
# rather than treating an absent value as an unrecognized one.
NULL_STATE_CD1_BUCKET = REAL_PROPERTY

# ── Flagged for Fable review (DATA_LIFECYCLE.md §9.4: "Fable reviews the
# classification decisions once, deliberately" for new classification
# maps) — every bucket assignment above that rests on secondary, indirect,
# or internally-disagreeing documentation rather than a single unambiguous
# source, plus the two genuinely new risks this cross-reference surfaced.
# Not a hedge on the whole map -- the other ~40 entries each cite a live
# population count plus an unambiguous real-property/personal-property/
# exempt characterization from an existing, already-verified investigation.
FLAGGED_FOR_REVIEW = {
    "ER": (
        "Classified EXEMPT_SYNTHETIC. Real risk if it exists in live data: "
        "collapses to first-character 'E' (Agricultural, REAL_PROPERTY) under "
        "every OTHER classification mechanism in this codebase today "
        "(CANONICAL_PARCEL_EXCL, label_case_sql(), query_state_cd1_prefixes.py). "
        "Not confirmed to actually appear as a literal state_cd1 value in "
        "production -- query_state_cd1_prefixes.py's live table only ever "
        "grouped by first character, so it cannot confirm OR rule this out. "
        "Needs Diego's live 1a query (full-string GROUP BY) before this entry "
        "can be trusted as anything more than 'documented as a real Comptroller "
        "code, bucket assigned by the label's plain meaning.'"
    ),
    "M2": (
        "Classified PERSONAL_PROPERTY, split from 'M'/'M1' (REAL_PROPERTY). "
        "Real risk if it exists in live data with nonzero population: every "
        "OTHER classification mechanism in this codebase today collapses M2 "
        "into first-character 'M' -> Residential (tax_logic/classify.py's "
        "_STATE_PREFIX_LABEL), meaning M2 rows -- if any exist -- may currently "
        "be counted in the Residential benchmark despite being personal "
        "property. This is a newly-found candidate second instance of the "
        "L-class contamination pattern, discovered via this cross-reference, "
        "not previously documented or measured anywhere in this codebase. "
        "Needs Diego's live 1a query to determine real M2 population before "
        "any claim about actual benchmark impact can be made -- do not treat "
        "this as a confirmed incident, treat it as an unconfirmed, plausible "
        "one worth checking first."
    ),
    "N": (
        "Classified EXEMPT_SYNTHETIC on STATE_CD_DESCRIPTIONS's 'Non-Taxable' "
        "label. parcel_filters.py's own docstring instead calls this code "
        "'personal property.' Only 3 live rows exist (per the June 2026 "
        "recount) so the practical stakes are low and the outcome (excluded "
        "from REAL_PROPERTY) doesn't change either way -- but the two sources "
        "disagree on WHY, and Fable should pick one characterization as "
        "canonical rather than this module silently picking for them."
    ),
    "S": (
        "Classified REAL_PROPERTY on loaders/compute_metrics.py's characterization "
        "('state-assessed utility real property'), NOT on STATE_CD_DESCRIPTIONS's "
        "own label ('Special / State Property'), which alone is ambiguous enough "
        "that 'Special' could plausibly mean something else. 751 live rows -- "
        "worth Fable's confirmation given the two-source-disagreement pattern "
        "recurring here as it did with 'N' above."
    ),
    "X-subcodes": (
        "Only bare 'X' and 'X1' are keys in this map. parcel_filters.py's own "
        "docstring lists real sub-codes observed in the live column's 'X%' "
        "population -- XV, XB, XU, XI, XJ, XR, XD, XG, XO, XL, XN, XA -- none "
        "of which are individually enumerated here because STATE_CD_DESCRIPTIONS "
        "(this map's source enumeration) never documented them at that "
        "granularity. Under the CURRENT first-character exclusion mechanism "
        "(CANONICAL_PARCEL_EXCL's 'X%%' LIKE pattern) these all correctly "
        "exclude today regardless. Under THIS map's full-string enforcement, "
        "every one of those 12 sub-codes will be a genuine, expected UNKNOWN "
        "hard-stop the first time enforce_real_property_only() runs against "
        "real data, until each is added as its own EXEMPT_SYNTHETIC entry. "
        "This is disclosed here so that first real run is not mistaken for a "
        "bug -- it is the harness doing exactly what Section 9.4a asked for "
        "(new codes get caught, not silently passed), surfaced immediately "
        "on first real use rather than hidden by the map's incomplete X-family "
        "enumeration. Needs a live full-string query (1a) to enumerate these "
        "12 (or more) codes precisely and add each one before this map is "
        "run against real production data in enforcement mode."
    ),
}


def classify_state_cd1(value):
    """Classify one state_cd1 value into REAL_PROPERTY / PERSONAL_PROPERTY /
    EXEMPT_SYNTHETIC, or raise UnknownClassCodeError.

    `value` is matched EXACTLY as given after stripping surrounding
    whitespace and uppercasing (state_cd1 is a short alphanumeric code;
    case/whitespace variance is a data-entry artifact, not a distinct
    class -- consistent with every existing consumer in this codebase,
    which all UPPER()/TRIM() before comparing). None (SQL NULL) returns
    NULL_STATE_CD1_BUCKET directly -- see module docstring.
    """
    if value is None:
        return NULL_STATE_CD1_BUCKET
    key = str(value).strip().upper()
    if key == "":
        return NULL_STATE_CD1_BUCKET
    try:
        return CLASSIFICATION_MAP[key]
    except KeyError:
        raise UnknownClassCodeError(
            f"state_cd1 value {key!r} is not in CLASSIFICATION_MAP "
            f"(classification_map.py). Per DATA_LIFECYCLE.md Stage 1: "
            f"'UNKNOWN is a blocker, not a bucket' -- halt this load, "
            f"classify {key!r} against real source rows (exemption_codes, "
            f"classi_cd, prop_type_cd -- the same evidence-gathering pattern "
            f"used for the L-class and M2 entries in this file), add it to "
            f"CLASSIFICATION_MAP with its evidence cited, and re-run."
        )


def enforce_real_property_only(rows, get_state_cd1=lambda r: r.get("state_cd1")):
    """The real, callable Stage-1 loader gate. `rows` is any iterable of
    row-like objects (dicts by default; pass a custom `get_state_cd1` for
    other row shapes, e.g. psycopg2 RealDictRow or a namedtuple).

    Returns the number of rows checked (all of which classified as
    REAL_PROPERTY -- see below) on success.

    Raises UnknownClassCodeError, listing EVERY distinct unrecognized code
    found and its row count (not just the first hit -- "a clear, actionable
    error", per this brief's own 1c requirement), if any row's state_cd1
    is not in CLASSIFICATION_MAP.

    Raises ValueError, listing every distinct non-REAL_PROPERTY bucket and
    code found with counts, if any row classifies to PERSONAL_PROPERTY or
    EXEMPT_SYNTHETIC -- a loader calling this function is asserting "this
    batch should be 100% real property"; a classified-but-wrong-bucket row
    getting this far is a loader-scoping bug (e.g. forgot to pre-filter
    personal property before calling this), not an UNKNOWN-code situation,
    and deserves a different, more specific error message.

    This function does not touch a database or any I/O -- it is pure
    Python over whatever `rows` the caller already has in memory, which is
    exactly what makes it independently fixture-testable (see
    test_classification_map.py) without a live connection.
    """
    unknown_counts = {}
    wrong_bucket_counts = {}
    n_ok = 0
    for row in rows:
        raw = get_state_cd1(row)
        try:
            bucket = classify_state_cd1(raw)
        except UnknownClassCodeError:
            key = "(null/blank)" if raw is None or str(raw).strip() == "" else str(raw).strip().upper()
            unknown_counts[key] = unknown_counts.get(key, 0) + 1
            continue
        if bucket != REAL_PROPERTY:
            key = (bucket, "(null/blank)" if raw is None else str(raw).strip().upper())
            wrong_bucket_counts[key] = wrong_bucket_counts.get(key, 0) + 1
            continue
        n_ok += 1

    if unknown_counts:
        detail = ", ".join(f"{code!r} x{count:,}" for code, count in
                            sorted(unknown_counts.items(), key=lambda kv: -kv[1]))
        raise UnknownClassCodeError(
            f"Load halted: {sum(unknown_counts.values()):,} row(s) carry a "
            f"state_cd1 value not in CLASSIFICATION_MAP: {detail}. Per "
            f"DATA_LIFECYCLE.md Stage 1, this is expected behavior on a new "
            f"code (e.g. a future year's export, or a different county's own "
            f"class-code taxonomy) -- classify each against real source rows, "
            f"add it to classification_map.py's CLASSIFICATION_MAP with its "
            f"evidence cited, and re-run. Do not bypass this by catching and "
            f"discarding this exception in a loader -- see this function's "
            f"own docstring."
        )
    if wrong_bucket_counts:
        detail = ", ".join(
            f"{bucket} {code!r} x{count:,}"
            for (bucket, code), count in
            sorted(wrong_bucket_counts.items(), key=lambda kv: -kv[1])
        )
        raise ValueError(
            f"Load halted: {sum(wrong_bucket_counts.values()):,} row(s) "
            f"classify to a non-REAL_PROPERTY bucket and reached "
            f"enforce_real_property_only() anyway: {detail}. This function "
            f"asserts the batch it's given should be 100% real property -- "
            f"pre-filter PERSONAL_PROPERTY/EXEMPT_SYNTHETIC rows out (or "
            f"route them to their own pipeline, per DATA_LIFECYCLE.md "
            f"Stage 1: 'if a BPP product ever exists, it gets its own "
            f"pipeline') before calling this, rather than relying on it to "
            f"do that filtering silently."
        )
    return n_ok


def classification_report(rows, get_state_cd1=lambda r: r.get("state_cd1"),
                           get_market_value=lambda r: r.get("market_value")):
    """The PM checklist's own ask, DATA_LIFECYCLE.md Stage 1: "run the
    classification report (counts + summed value per prefix per bucket)".
    Does NOT raise on UNKNOWN -- this is the diagnostic/reporting form,
    meant to be run BEFORE enforce_real_property_only() to see the full
    picture (including what would halt the load and why), not a substitute
    for the hard-stop gate itself.

    Returns a dict: {bucket_or_"UNKNOWN": {code: {"count": int, "market_value": float}}}
    """
    report = {}
    for row in rows:
        raw = get_state_cd1(row)
        mv = get_market_value(row) or 0
        try:
            bucket = classify_state_cd1(raw)
        except UnknownClassCodeError:
            bucket = UNKNOWN
        code = "(null/blank)" if raw is None or str(raw).strip() == "" else str(raw).strip().upper()
        bucket_report = report.setdefault(bucket, {})
        entry = bucket_report.setdefault(code, {"count": 0, "market_value": 0})
        entry["count"] += 1
        entry["market_value"] += mv
    return report
