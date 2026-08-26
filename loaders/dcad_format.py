"""
loaders/dcad_format.py — DESIGN + SKELETON ONLY (PX-20260826-03). Sibling to
ears_format.py: same pure-functions-over-files discipline (file path or
in-memory `lines` in, plain dicts out, no DB access, no config import).
Parses DCAD's relational, comma-delimited certified-roll product (14 CSV
tables, confirmed by dcad_20260826_hashes_final.txt — see vault_manifest.md's
Migration 4 section) — NOT the 467-field fixed-width `.DAT` roll
PX-20260824-05 originally scoped against. That verdict is superseded; see
this file's own module-level disclosures below for exactly what is and
is not verified against a real file.

══════════════════════════════════════════════════════════════════════════
REV1 (PX-20260826-03-rev1) — PM resolved three flagged unknowns against the
real files; this module is revised to match. Changelog, not silently edited:

  1. TAXABLE_OBJECT is a building-component link table (per the real data
     dictionary), NOT a finer unit grain. DCAD's unit grain is the ACCOUNT
     itself — unit_count=1 everywhere, no collision mechanism, unlike
     Travis's own prop_id/geo_id split. The earlier "TAXABLE_OBJECT may be
     prop_id's role" hypothesis in this module (built from row-count
     evidence alone, 822,355 vs 806,564) is WRONG and removed below.
     TAXABLE_OBJECT is dropped from the value path entirely and documented
     as deliberately unloaded, same treatment as LAND/RES_DETAIL/COM_DETAIL/
     RES_ADDL/MULTI_OWNER/ACCOUNT_TIF (component/ownership detail already
     summarized at the account level DCAD itself computes into
     ACCOUNT_APPRL_YEAR — this design does not re-derive it).
  2. Classification reads ACCOUNT_APPRL_YEAR.SPTD_CODE (column 47) —
     CONFIRMED, not TAXABLE_OBJECT.SPTD_CD (the prior UNCONFIRMED guess).
     ACCOUNT_APPRL_YEAR is confirmed as the hub table: both the value
     columns AND the classification code live on the same one-row-per-
     account table.
  3. prop_id = int(ACCOUNT_NUM), geo_id = ACCOUNT_NUM verbatim (as text) —
     CONFIRMED. ACCOUNT_NUM is validated all-digits with a fail-loud
     ValueError on anything else (see derive_prop_id_geo_id() below) —
     this is a real identity-integrity check, not a soft skip, since a
     non-numeric ACCOUNT_NUM would silently corrupt the BIGINT cast rather
     than just drop one row. This also fully resolves the schema-widening
     risk the pre-rev1 version of this module flagged: prop_id stays
     BIGINT with zero schema change, geo_id fits VARCHAR(20) with zero
     schema change. prop_id remains an internal integer surrogate (already
     composite-keyed with county_code in production) — geo_id (text) is
     Dallas's real natural key, satisfying MC-1 rule 3 exactly the same
     way ACCOUNT_NUM's own 17 chars already do.

Everything else in the pre-rev1 design stands as approved (BPP exclusion
policy, exemption-table handling, the two still-open value-mapping
semantic gaps on HMSTD_CAP_VAL and per-jurisdiction taxable value).
══════════════════════════════════════════════════════════════════════════
HONEST, LOAD-BEARING DISCLOSURE — READ BEFORE TRUSTING ANY FIELD NAME BELOW
══════════════════════════════════════════════════════════════════════════
This sandbox has NO direct access to the real DCAD delivery — not the 14
CSVs, not `DCAD Data Dictionary.rtf`, not `TABLES AND FIELD NAMES.xlsx`,
not `SPTD_CD_XREF.pdf` as a file (its CONTENT is already transcribed
verbatim in classification_map_dallas.py's DCAD_SPTD_CD_XREF_2011, from a
prior session that did have `pdftotext` access to the real PDF — that one
document's content is real and citable; the others are not, in this
session). `PARCELYTICS_ARCHIVE_ROOT` is an external drive, unmounted here;
the mirrored `data/dallas/certified_roll/` folder in the workspace is
empty. Every column name below is one of three kinds, and each is labeled
inline with a `# CONFIRMED` / `# DOCUMENTED` / `# UNCONFIRMED` tag:

  CONFIRMED    — named directly in PX-20260826-03's own brief text (which
                 the PM states was verified against the real 2026 sample
                 files) or in classification_map_dallas.py's real,
                 evidenced content.
  DOCUMENTED   — named in prior-session Notion research (DALLAS-CLASS-1/
                 2, 2026-08-20/21) that DID have direct file access at the
                 time, but not independently re-verified against a file in
                 THIS session.
  UNCONFIRMED  — this module's own reasonable inference from row counts,
                 table names, and Texas-appraisal-district convention,
                 NOT confirmed against any real file or document. Every
                 UNCONFIRMED field name is a placeholder a real csv.DictReader
                 header read must verify before this module touches a real
                 row. Get one of these wrong and every downstream function
                 silently returns None for that column, exactly the kind of
                 "no gate ever fires because a field name was already wrong"
                 hazard SPEC_UNIT_MODEL_AND_INGEST_GATE.md's own tested-alarm
                 principle exists to catch — see FIELD_NAME_VERIFICATION_
                 CHECKLIST at the bottom of this file for the one command
                 Diego should run against each real CSV's header row before
                 any of this code touches production.

Per MC-1 rule 1 (identity is county-scoped until proven otherwise) and this
session's own honesty norms: nothing below is asserted with more confidence
than its tag allows.
"""
import csv


# ══════════════════════════════════════════════════════════════════════
# Table roster — CONFIRMED directly from dcad_20260826_hashes_final.txt
# (the canonical hash list) and the brief's own PM-verified row counts.
# ══════════════════════════════════════════════════════════════════════
TABLE_FILENAMES = {
    "ACCOUNT_INFO": "ACCOUNT_INFO.CSV",             # CONFIRMED, 806,564 rows
    "ACCOUNT_APPRL_YEAR": "ACCOUNT_APPRL_YEAR.CSV", # CONFIRMED
    "LAND": "LAND.CSV",                             # CONFIRMED, 695,976 rows
    "RES_DETAIL": "RES_DETAIL.CSV",                 # CONFIRMED, 628,544 rows
    "RES_ADDL": "RES_ADDL.CSV",                     # CONFIRMED (present in hash list; NOT named in brief's own verified list — a 14th table this session found only via the hash list itself, unverified content)
    "COM_DETAIL": "COM_DETAIL.CSV",                 # CONFIRMED, 92,934 rows
    "TAXABLE_OBJECT": "TAXABLE_OBJECT.CSV",         # CONFIRMED, 822,355 rows
    "MULTI_OWNER": "MULTI_OWNER.CSV",               # CONFIRMED (named, not row-counted)
    "ACCOUNT_TIF": "ACCOUNT_TIF.CSV",               # CONFIRMED (named, not row-counted)
    "ABATEMENT_EXEMPT": "ABATEMENT_EXEMPT.CSV",     # CONFIRMED (named, exemption table)
    "ACCT_EXEMPT_VALUE": "ACCT_EXEMPT_VALUE.CSV",   # CONFIRMED (named, exemption table)
    "APPLIED_STD_EXEMPT": "APPLIED_STD_EXEMPT.CSV", # CONFIRMED (named, exemption table)
    "FREEPORT_EXEMPTION": "FREEPORT_EXEMPTION.CSV", # CONFIRMED (named, exemption table)
    "TOTAL_EXEMPTION": "TOTAL_EXEMPTION.CSV",       # CONFIRMED (named, exemption table)
}
# NOTE: the brief's own text says "6 exemption tables" exist; the canonical
# hash list (ground truth for THIS acquisition) shows 5 exemption-named
# tables (ABATEMENT_EXEMPT, ACCT_EXEMPT_VALUE, APPLIED_STD_EXEMPT,
# FREEPORT_EXEMPTION, TOTAL_EXEMPTION). Flagged as a real, small discrepancy
# between the brief's own count and the attached ground-truth file, not
# silently reconciled by guessing a 6th table into existence.

# ── Join key — CONFIRMED by the brief's own text ("join key on ACCOUNT_NUM,
# 17-char"), present on every one of the 14 tables per relational-CSV
# convention. ─────────────────────────────────────────────────────────────
ACCOUNT_NUM_FIELD = "ACCOUNT_NUM"  # CONFIRMED

# ── DIVISION_CD — CONFIRMED to exist on ACCOUNT_INFO (brief's own text:
# "includes BPP via DIVISION_CD"), and CONFIRMED by Notion research
# (DALLAS-CLASS-1/2) to span RES/COM/BPP. The literal code VALUES are
# UNCONFIRMED — inferred from the RES/COM/BPP description, not seen as raw
# strings in any real row. A real header+sample read should confirm whether
# they are exactly "RES"/"COM"/"BPP" or some other short code. ────────────
DIVISION_CD_FIELD = "DIVISION_CD"          # CONFIRMED (field name)
DIVISION_BPP_VALUES = frozenset({"BPP"})   # UNCONFIRMED (literal values)

# ── Travis's own documented BPP stance, mirrored here per the brief's
# explicit instruction ("mirror Travis's documented BPP stance"): Travis's
# pipeline scope is real-property-only — BPP is a separate, not-yet-acquired
# product (PX-20260824-05's own Task 1: "BPP Detail — separate product,
# confirmed to exist, not yet obtained. Out of scope for this brief (real-
# property parcels only, matching Travis's own current scope)"). This is an
# EXCLUSION-FROM-LOAD policy, not merely an unmapped-classification policy —
# stronger than classification_map_dallas.py's G/J/O "unmapped but still
# loaded" treatment. Applied here: DIVISION_CD == BPP rows are excluded at
# the loader boundary, before classification ever runs on them. ──────────
def is_bpp_division(division_cd):
    """True if this ACCOUNT_INFO row's DIVISION_CD marks it as Business
    Personal Property — the exclude-from-load boundary, mirroring Travis's
    own real-property-only scope. None/blank is NOT treated as BPP (a
    missing division code is not evidence of BPP; conservative default,
    same reasoning as classify_dallas_sptb_code()'s None handling)."""
    if division_cd is None:
        return False
    return str(division_cd).strip().upper() in DIVISION_BPP_VALUES


# ══════════════════════════════════════════════════════════════════════
# Low-level CSV primitive — generic across all 14 tables, mirroring
# ears_format.py's `_resolve_lines` + per-line dict-yielding shape, adapted
# for a header-having, comma-delimited, multi-column source instead of a
# fixed-width one.
# ══════════════════════════════════════════════════════════════════════
def iter_csv_rows(path=None, lines=None):
    """
    Yield one OrderedDict per data row (csv.DictReader shape) plus a
    1-based `_lineno` key (data rows only — the header is consumed, not
    counted as a data line). Either `path` (opened internally, utf-8-sig
    to tolerate a possible BOM — a real, common Excel-export artifact,
    unconfirmed either way for these files) or `lines` (an iterable of
    already-decoded CSV text lines, for fixture tests) must be given.

    Deliberately thin: no column validation, no type coercion — that's
    each per-table iter_*_records() function's job, same division of
    labor as ears_format.py's iter_prop_lines vs iter_prop_records.
    """
    if lines is not None:
        reader = csv.DictReader(lines)
    elif path is not None:
        f = open(path, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(f)
    else:
        raise ValueError("iter_csv_rows requires either path= or lines=")

    for lineno, row in enumerate(reader, 1):
        row["_lineno"] = lineno
        yield row


# ══════════════════════════════════════════════════════════════════════
# ACCOUNT_INFO — CONFIRMED grain: one row per account (806,564 rows ==
# the brief's own PM-verified account population). Analogous to EARS'
# PROP.TXT. REV1: CONFIRMED against the real data dictionary that the
# ACCOUNT is DCAD's own unit grain outright — unit_count=1 everywhere,
# no finer-grained table feeds the value path (TAXABLE_OBJECT is a
# building-component link table, deliberately unloaded — see
# TABLE_LOAD_POLICY below). No prop_id/geo_id collision mechanism exists
# for Dallas the way it does for Travis's own PARCEL-grain data.
# ══════════════════════════════════════════════════════════════════════
def iter_account_info_records(path=None, lines=None):
    """
    Yields dicts with keys: account_num, division_cd, is_bpp, gis_parcel_id,
    owner_name, situs_address, skip_reason.

    skip_reason is one of:
      None            — normal, usable row (has an ACCOUNT_NUM)
      'no_account_num'— blank/missing ACCOUNT_NUM (mirrors ears_format's
                        'no_geo_id' bucket — a genuinely unusable row, not
                        a BPP exclusion, which is a separate, later
                        loader-level filter, not a G1 skip bucket, since a
                        BPP row still has a real, well-formed ACCOUNT_NUM.

    Field name confidence: ACCOUNT_NUM (CONFIRMED), DIVISION_CD (CONFIRMED).
    GIS_PARCEL_ID (UNCONFIRMED — named in the brief's own open question
    "does GIS_PARCEL_ID play the geo_id role" but never independently
    confirmed as the literal column header; kept here as the best-guess
    field name pending a real header read). owner_name/situs_address field
    names are UNCONFIRMED entirely — ACCOUNT_INFO's brief description says
    it "includes identity/owner/situs/legal" but names no specific columns;
    OWNER_NAME/SITUS_ADDR below are placeholders, not evidenced strings.
    """
    for row in iter_csv_rows(path, lines):
        account_num = (row.get(ACCOUNT_NUM_FIELD) or "").strip() or None
        division_cd = (row.get(DIVISION_CD_FIELD) or "").strip() or None
        skip_reason = "no_account_num" if not account_num else None
        yield {
            "_lineno": row["_lineno"],
            "skip_reason": skip_reason,
            "account_num": account_num,
            "division_cd": division_cd,
            "is_bpp": is_bpp_division(division_cd),
            "gis_parcel_id": (row.get("GIS_PARCEL_ID") or "").strip() or None,       # UNCONFIRMED field name
            "owner_name": (row.get("OWNER_NAME") or "").strip() or None,             # UNCONFIRMED field name
            "situs_address": (row.get("SITUS_ADDR") or "").strip() or None,          # UNCONFIRMED field name
        }


# ══════════════════════════════════════════════════════════════════════
# prop_id / geo_id derivation — REV1, CONFIRMED against the real files.
# geo_id = ACCOUNT_NUM verbatim (text — satisfies MC-1 rule 3 exactly,
# VARCHAR(20) already fits 17 chars). prop_id = int(ACCOUNT_NUM) (an
# internal BIGINT surrogate, same role as Travis's own "TCAD short
# integer ID" — no schema change needed either side).
# ══════════════════════════════════════════════════════════════════════
def derive_prop_id_geo_id(account_num):
    """
    CONFIRMED (PX-20260826-03-rev1): prop_id = int(ACCOUNT_NUM), geo_id =
    ACCOUNT_NUM verbatim.

    Fail-loud validation, by design: ACCOUNT_NUM must be all-digits or
    this raises ValueError rather than returning a soft skip_reason. This
    is an identity-integrity check, not a per-row data-quality skip — a
    non-numeric ACCOUNT_NUM would silently corrupt prop_id's BIGINT cast
    (Postgres would reject it at write time anyway, but with a much less
    diagnosable error, potentially mid-batch) rather than cleanly drop one
    bad row up front. Mirrors this codebase's existing hard-stop
    convention for identity-breaking conditions (e.g. classification_map.py's
    UNKNOWN hard-stop) rather than its soft, ledger-counted skip_reason
    convention (which is for expected, countable exceptions, not identity
    corruption).

    Returns (prop_id: int, geo_id: str).
    """
    if account_num is None:
        raise ValueError("derive_prop_id_geo_id: ACCOUNT_NUM is None")
    geo_id = str(account_num).strip()
    if not geo_id.isdigit():
        raise ValueError(
            f"derive_prop_id_geo_id: ACCOUNT_NUM {account_num!r} is not all-digits — "
            f"fail-loud per PX-20260826-03-rev1's confirmed prop_id=int(ACCOUNT_NUM) rule"
        )
    return int(geo_id), geo_id


# ══════════════════════════════════════════════════════════════════════
# TAXABLE_OBJECT — REV1: CONFIRMED (per the real data dictionary) to be a
# building-component link table, NOT a finer unit grain. DCAD's unit
# grain is the ACCOUNT itself (unit_count=1 everywhere) — no collision
# mechanism exists here the way Travis's PARCEL/geo_id split has one.
# DELIBERATELY UNLOADED — no iter_*_records function for this table.
# Its earlier row-count-based "finer grain" hypothesis in this module
# (822,355 vs 806,564, ~2.0% gap) is WRONG and has been removed; see this
# file's REV1 changelog at the top for the full correction. Dropped from
# the value path entirely, same treatment as LAND/RES_DETAIL/COM_DETAIL/
# RES_ADDL/MULTI_OWNER/ACCOUNT_TIF — see TABLE_LOAD_POLICY below.
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# ACCOUNT_APPRL_YEAR — CONFIRMED value columns (named directly in the
# brief's own PM-verified text): IMPR_VAL, LAND_VAL, TOT_VAL,
# HMSTD_CAP_VAL, plus per-jurisdiction *_TAXABLE_VAL / *_CEILING_VALUE /
# *_SPLIT_PCT. The per-jurisdiction column SET (how many, which
# jurisdictions, row-based vs repeated-column-based) is UNCONFIRMED — the
# brief names the *shape* of the value ("per-jurisdiction") without giving
# the literal column names, which is the real ambiguity flagged below.
#
# REV1: CONFIRMED against the real files — this table is DCAD's hub table.
# Classification reads ACCOUNT_APPRL_YEAR.SPTD_CODE (column 47), not
# TAXABLE_OBJECT.SPTD_CD (the prior UNCONFIRMED guess, now removed). Both
# the value columns AND the classification code live on this one,
# one-row-per-account table.
# ══════════════════════════════════════════════════════════════════════
def iter_account_apprl_year_records(path=None, lines=None):
    """
    Yields dicts with keys: account_num, tax_yr, impr_val, land_val,
    tot_val, hmstd_cap_val, sptd_code, skip_reason.

    sptd_code (CONFIRMED, PX-20260826-03-rev1): column 47 of this table,
    field name SPTD_CODE. This is the real input to
    classification_map_dallas.classify_dallas_sptb_code() for this
    relational product — resolves the brief's own open question
    ("TAXABLE_OBJECT.SPTD_CD or wherever it lives") in favor of THIS
    table. classification_map_dallas.py's own code is unchanged by this
    revision (its logic operates on a bare code string regardless of
    which table it came from) — only the wiring (which table/column
    feeds it) changes. Where the classification result is written for
    Dallas (prop_unit's own prop_type_cd analog, or a separate
    classi_cd-style column) is a still-separate, not-yet-made decision,
    same as it was pre-rev1 — this field just makes the INPUT side no
    longer a guess.

    VALUE-COLUMN MAPPING TO THE UNIT MODEL — the brief's own required
    decision, stated here with its honest gap:
      TOT_VAL      -> prop_unit_tax_year.market_value   (CONFIRMED-shape:
                       DCAD's own total appraised value; direct analog)
      LAND_VAL     -> prop_unit_tax_year.land_value       (direct analog)
      IMPR_VAL     -> prop_unit_tax_year.imprv_value       (direct analog)
      HMSTD_CAP_VAL -> UNRESOLVED SEMANTIC GAP, flagged loudly per the
                       brief's own instruction: Travis's hs_cap_loss column
                       stores the CAP LOSS AMOUNT (market value MINUS the
                       capped assessed value — see ears_format.py's own
                       PROP_ENT_SLICES/exemption derivation). DCAD's
                       HMSTD_CAP_VAL name suggests it may instead be the
                       CAPPED VALUE ITSELF (the post-cap assessed value),
                       not the loss amount — the same field NAME pattern,
                       a materially different NUMBER if so (a $400,000
                       home capped at $350,000 would report a $50,000 loss
                       under Travis's semantics but a $350,000 capped
                       value under the literal DCAD name). This is exactly
                       the class of gap MC-7.1 exists to catch before it
                       becomes a wrong-answer bug with a press release —
                       NOT resolved here; requires either the real data
                       dictionary text or a direct row-level sanity check
                       (HMSTD_CAP_VAL should be LESS than TOT_VAL for a
                       capped homestead account if it's a value; roughly
                       TOT_VAL minus a plausible assessed value if it's a
                       loss amount) once a real file is available.
      assessed_value/taxable_value -> UNRESOLVED SHAPE GAP: Travis's
                       assessed_value/taxable_value are single, county-
                       level numbers (picked from the TCO entity's own
                       PROP_ENT.TXT row — see ears_format.py's
                       iter_prop_ent_aggregates()'s is_tco logic). DCAD's
                       own "per-jurisdiction *_TAXABLE_VAL" phrasing
                       implies EITHER a repeated column per jurisdiction
                       (e.g. one column per taxing entity, wide-format) OR
                       a separate per-jurisdiction row keyed by a
                       jurisdiction code (long-format, more like PROP_ENT's
                       own per-entity-line shape). Which shape applies,
                       and which literal column/jurisdiction-code
                       identifies "Dallas County" itself (the county-level
                       entity, analogous to Travis's TCO_ENTITY_CODES
                       check), is UNCONFIRMED and NOT guessed at here —
                       this function intentionally does NOT populate
                       assessed_value/taxable_value at all, leaving that
                       mapping as an explicit, named follow-up rather than
                       a silently wrong placeholder value.
    """
    for row in iter_csv_rows(path, lines):
        account_num = (row.get(ACCOUNT_NUM_FIELD) or "").strip() or None
        skip_reason = "no_account_num" if not account_num else None
        yield {
            "_lineno": row["_lineno"],
            "skip_reason": skip_reason,
            "account_num": account_num,
            "tax_yr": _int_or_none(row.get("TAX_YR")),               # UNCONFIRMED exact field name
            "impr_val": _int_or_none(row.get("IMPR_VAL")),           # CONFIRMED field name
            "land_val": _int_or_none(row.get("LAND_VAL")),           # CONFIRMED field name
            "tot_val": _int_or_none(row.get("TOT_VAL")),             # CONFIRMED field name
            "hmstd_cap_val": _int_or_none(row.get("HMSTD_CAP_VAL")), # CONFIRMED field name, UNCONFIRMED semantics (see docstring)
            "sptd_code": (row.get("SPTD_CODE") or "").strip() or None,  # CONFIRMED field name + location (col 47), rev1
        }


# ══════════════════════════════════════════════════════════════════════
# APPLIED_STD_EXEMPT — the exemption-codes analog to ears_format.py's
# EXEMPTION_FIELDS derivation (Travis derives codes from non-zero PROP_ENT
# amount fields; DCAD appears to ship a dedicated applied-exemptions table
# instead, a cleaner source if the field names below hold up).
# ══════════════════════════════════════════════════════════════════════
def iter_applied_std_exempt_records(path=None, lines=None):
    """
    Yields dicts with keys: account_num, exempt_code, skip_reason.
    EXEMPT_CODE field name UNCONFIRMED — chosen as the most plausible name
    for "which standard exemption applies to this account," analogous to
    Travis's hs/ov65/dp/dv/... exemption_codes string. This table (and
    ACCT_EXEMPT_VALUE, its dollar-amount sibling) is the design's chosen
    source for prop_unit_tax_year.exemption_codes -- see
    TABLE_LOAD_POLICY below for which of the 14 real tables this design
    loads versus deliberately leaves unloaded.
    """
    for row in iter_csv_rows(path, lines):
        account_num = (row.get(ACCOUNT_NUM_FIELD) or "").strip() or None
        skip_reason = "no_account_num" if not account_num else None
        yield {
            "_lineno": row["_lineno"],
            "skip_reason": skip_reason,
            "account_num": account_num,
            "exempt_code": (row.get("EXEMPT_CODE") or "").strip() or None,  # UNCONFIRMED field name
        }


def exemption_codes_for_account(applied_std_exempt_rows):
    """
    Pure aggregation, mirroring ears_format.py's per-prop_id exemption
    union: given every APPLIED_STD_EXEMPT row for one account_num, return
    a comma-joined sorted string of exempt_code values (or None if empty)
    -- the same shape prop_unit_tax_year.exemption_codes already expects.
    """
    codes = {r["exempt_code"] for r in applied_std_exempt_rows if r.get("exempt_code")}
    return ",".join(sorted(codes)) or None


# ══════════════════════════════════════════════════════════════════════
# TABLE_LOAD_POLICY — REV1: generalized from the pre-rev1 EXEMPTION_TABLE_
# POLICY to cover all 14 tables, now that TAXABLE_OBJECT has joined the
# deliberately-unloaded set. Every one of the 14 real tables in
# TABLE_FILENAMES gets an entry here — "which tables matter" is now a
# complete, explicit accounting, not just the exemption subset.
# ══════════════════════════════════════════════════════════════════════
TABLE_LOAD_POLICY = {
    "ACCOUNT_INFO": (
        "LOADED. The account roster + BPP filter boundary "
        "(DIVISION_CD) + geo_id/prop_id source (ACCOUNT_NUM, see "
        "derive_prop_id_geo_id())."
    ),
    "ACCOUNT_APPRL_YEAR": (
        "LOADED. REV1: confirmed hub table -- carries both the value "
        "columns (TOT_VAL/LAND_VAL/IMPR_VAL/HMSTD_CAP_VAL) AND the "
        "classification code (SPTD_CODE, column 47). One row per account."
    ),
    "APPLIED_STD_EXEMPT": (
        "LOADED. Direct analog to Travis's exemption_codes column -- the "
        "applied-code-per-account shape exemption_codes_for_account() "
        "consumes. Field names UNCONFIRMED (see that function's own "
        "docstring) but the table's ROLE in the design is settled."
    ),
    "TAXABLE_OBJECT": (
        "DELIBERATELY UNLOADED. REV1: CONFIRMED (per the real data "
        "dictionary) to be a building-component link table, not a finer "
        "unit grain -- DCAD's unit grain is the ACCOUNT itself "
        "(unit_count=1 everywhere). Dropped from the value path entirely; "
        "no iter_*_records function exists for it in this module. See "
        "this file's REV1 changelog at the top for the full correction "
        "of this module's earlier, row-count-based wrong hypothesis."
    ),
    "LAND": (
        "DELIBERATELY UNLOADED. Land-segment detail; presumed already "
        "summarized into ACCOUNT_APPRL_YEAR.LAND_VAL, mirroring how "
        "ears_format.py's LAND_DET.TXT detail is separately summed "
        "(land_totals()) rather than joined into the per-unit row -- "
        "except DCAD appears to compute this rollup itself, so no "
        "separate summation step is needed here at all. UNCONFIRMED "
        "whether LAND_VAL is genuinely LAND.CSV's own rollup; not "
        "verified against a real file."
    ),
    "RES_DETAIL": (
        "DELIBERATELY UNLOADED. Residential building-component detail "
        "(the RES-division analog of TAXABLE_OBJECT/COM_DETAIL); no "
        "existing Travis-side schema column at this granularity."
    ),
    "COM_DETAIL": (
        "DELIBERATELY UNLOADED. Commercial building-component detail, "
        "same reasoning as RES_DETAIL."
    ),
    "RES_ADDL": (
        "DELIBERATELY UNLOADED. Additional residential detail; not named "
        "in the brief's own PM-verified table list, found only via the "
        "hash list itself -- content entirely unverified in this session."
    ),
    "MULTI_OWNER": (
        "DELIBERATELY UNLOADED. Multiple-ownership detail; prop_unit has "
        "a single owner_name column, matching Travis's own single-owner "
        "display grain -- no existing column for a list of co-owners."
    ),
    "ACCOUNT_TIF": (
        "DELIBERATELY UNLOADED. Tax Increment Financing zone detail; no "
        "Travis-side equivalent column exists to map it to."
    ),
    "ACCT_EXEMPT_VALUE": (
        "DELIBERATELY UNLOADED. Presumed to carry the DOLLAR amount per "
        "exemption (the DCAD analog of Travis's own per-exemption amount "
        "fields in PROP_ENT.TXT, which ears_format.py also does NOT "
        "expose individually -- Travis's own design only derives the "
        "boolean/code-presence signal, not the dollar breakdown, into "
        "exemption_codes). Matches existing Travis precedent for "
        "minimalism (not every EARS collateral file is parsed -- e.g. "
        "ARBITRATION.TXT, LAWSUIT.TXT are vaulted but never read by a "
        "loader); documented here as a deliberate parity decision, not "
        "an oversight."
    ),
    "TOTAL_EXEMPTION": (
        "DELIBERATELY UNLOADED. Presumed to be an account-level summary "
        "total, useful as an external cross-check for the eventual "
        "conservation gate (see the ingest-gate redefinition section of "
        "the PX-20260826-03 report) but not itself a column "
        "prop_unit_tax_year needs -- TOT_VAL from ACCOUNT_APPRL_YEAR is "
        "presumed already net of exemptions where relevant (UNCONFIRMED; "
        "see that table's own docstring for the same class of ambiguity)."
    ),
    "FREEPORT_EXEMPTION": (
        "DELIBERATELY UNLOADED. Freeport (goods-in-transit) exemptions "
        "are, per Texas Tax Code Ch. 11.253 convention, overwhelmingly a "
        "Business Personal Property concept -- consistent with this "
        "design's own DIVISION_CD BPP exclusion policy above. Loading it "
        "would mostly describe accounts already excluded from load "
        "entirely."
    ),
    "ABATEMENT_EXEMPT": (
        "DELIBERATELY UNLOADED. Real-property-relevant (economic "
        "development tax abatement agreements can apply to real "
        "property) but Travis's own pipeline carries no equivalent "
        "column today -- no existing schema target to map it to. Named "
        "here as a real, future candidate once/if a Travis-side "
        "abatement column exists, not silently dropped without a reason."
    ),
}


# ══════════════════════════════════════════════════════════════════════
# G1-style conservation ledger — generic across all 14 tables, mirroring
# ingest_gate.py's scan_prop_ledger() shape, generalized per-table.
# ══════════════════════════════════════════════════════════════════════
def scan_table_ledger(table_name, iter_fn, path=None, lines=None):
    """
    Run any of this module's iter_*_records() functions and build a G1-
    style ledger: {"table": name, "total_lines": int,
    "buckets": {"accepted": n, <skip_reason>: n, ...},
    "account_nums": set(...)}. This is the per-table primitive the
    proposed multi-table G1 (see the PX-20260826-03 report's ingest-gate
    section) calls once per table, then combines.
    """
    buckets = {"accepted": 0}
    account_nums = set()
    total = 0
    for rec in iter_fn(path, lines):
        total += 1
        bucket = rec.get("skip_reason") or "accepted"
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket == "accepted" and rec.get("account_num"):
            account_nums.add(rec["account_num"])
    return {"table": table_name, "total_lines": total, "buckets": buckets, "account_nums": account_nums}


def _int_or_none(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", "")
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


# ══════════════════════════════════════════════════════════════════════
# FIELD_NAME_VERIFICATION_CHECKLIST — the one concrete, cheap step this
# module recommends BEFORE any of the UNCONFIRMED field names above are
# trusted with a real row. Each is a single `head -1 <file>.CSV` (or
# equivalent) against the real, mounted vault files Diego has and this
# sandbox does not.
# ══════════════════════════════════════════════════════════════════════
FIELD_NAME_VERIFICATION_CHECKLIST = """
REV1 UPDATE: three items PM already resolved against the real files are
REMOVED from this list -- TAXABLE_OBJECT's grain/key (moot; the table is
deliberately unloaded), SPTD_CD's location (confirmed: ACCOUNT_APPRL_YEAR.
SPTD_CODE, column 47), and prop_id/geo_id derivation (confirmed: int(
ACCOUNT_NUM) / ACCOUNT_NUM verbatim). What remains genuinely open:

Run against the real, extracted DCAD2026_CERTIFIED_07232026/ folder
(PARCELYTICS_ARCHIVE_ROOT/dallas/certified_roll/2026-07-23/...), one
command per table, before load_dallas_certified.py touches a live row:

  head -1 ACCOUNT_INFO.CSV        # confirm GIS_PARCEL_ID, owner/situs field names
  head -1 ACCOUNT_APPRL_YEAR.CSV  # confirm TAX_YR name + the per-jurisdiction *_TAXABLE_VAL column shape + HMSTD_CAP_VAL semantics (value vs loss amount)
  head -1 APPLIED_STD_EXEMPT.CSV  # confirm EXEMPT_CODE's real name

Also open DCAD Data Dictionary.rtf and TABLES AND FIELD NAMES.xlsx directly
for the two still-open value-mapping semantic gaps (HMSTD_CAP_VAL,
per-jurisdiction assessed_value/taxable_value shape) -- neither was
accessible to this session (see this module's top-level HONEST,
LOAD-BEARING DISCLOSURE).
"""
