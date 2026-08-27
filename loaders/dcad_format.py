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
PX-20260826-04 ("the bodies") — implements the rev1 skeleton fully, per PM
value-mapping rulings verified against real 2026 rows. Changelog:

  1. HMSTD_CAP_VAL semantics RESOLVED (was the pre-rev1 unresolved gap):
     it is the CAPPED VALUE ITSELF (post-cap assessed value), not a loss
     amount — CONFIRMED, PM-verified. assessed_value = HMSTD_CAP_VAL when
     HMSTD_CAP_VAL > 0 (cap present, whether or not it's currently
     binding), else TOT_VAL (no cap at all). hs_cap_loss is derived, not
     sourced directly: TOT_VAL − HMSTD_CAP_VAL when a cap is present
     (this is 0, not missing, when the cap isn't binding — HMSTD_CAP_VAL
     == TOT_VAL in that case), else NULL. See derive_value_mapping()
     below — this is now the one place this arithmetic lives.
  2. Per-jurisdiction taxable_value shape RESOLVED for the one jurisdiction
     this design needs: taxable_value = COUNTY_TAXABLE_VAL (CONFIRMED,
     PM-verified) — the county-entity taxable value, the same real-world
     quantity Travis's own TCO_ENTITY_CODES check picks out of PROP_ENT.TXT.
     The other five jurisdictions' own taxable/ceiling/split columns remain
     DELIBERATELY UNLOADED (documented in TABLE_LOAD_POLICY below) — this
     design only ever populates the one column prop_unit_tax_year has room
     for.
  3. SPTD_CODE → classification_map_dallas.classify_dallas_sptb_code()
     wired for real (classify_account_sptd() below) — verified its
     interface: the function takes a bare code string (e.g. "A11", "F10")
     and matches on the first character only, uppercased; SPTD_CODE's own
     real values are exactly that shape, so no adapter is needed. ONE real
     terminology inconsistency surfaced and is flagged, not silently
     smoothed over: classification_map_dallas.py's own
     DALLAS_SPTB_FIELD_NAME constant says the field is named "SPTB CLASS
     CODE", while this brief (and DCAD's own ACCOUNT_APPRL_YEAR table,
     confirmed rev1) calls it SPTD_CODE — SPTB vs SPTD, a one-letter
     difference across two different DCAD-sourced documents (the module's
     own SPTD_CD_XREF.pdf citation actually also says "SPTD", suggesting
     "SPTB" in that module's field-name constant may itself be the
     drifted one). The DATA VALUES match either way (A11/F10-shaped
     codes); this is a documentation-terminology question, not a data-
     shape mismatch, so it does not block wiring — flagged in this
     brief's own report as the one open item for PM to confirm against
     the real data dictionary.
  4. Real CSV header validation added (validate_header() below): each
     loaded table's iter_*_records() function now checks the real CSV's
     header row against an EXPECTED_HEADERS set before yielding any row,
     raising HeaderDriftError (fail-loud) if a column this design's LOGIC
     depends on (not just a soft, optional field) is missing — the
     concrete backstop for the "wrong field name silently returns None
     forever, no gate ever fires" hazard this module's own top-level
     disclosure has warned about since PX-20260826-03. Only CONFIRMED,
     logic-critical columns are in each table's expected set — the
     still-UNCONFIRMED optional fields (owner/situs names, EXEMPT_CODE)
     are deliberately NOT hard-failed on, consistent with this module's
     existing tiered-confidence discipline (a wrong optional-field name
     degrades gracefully to None; a wrong identity/value-critical one
     stops the load).
  5. BPP exclusion is now a NAMED G1 skip-bucket ("bpp_excluded"), not a
     later, loader-only filter step — per this brief's own explicit
     instruction. iter_account_info_records() now sets skip_reason=
     "bpp_excluded" for DIVISION_CD-BPP rows directly, so scan_table_ledger()
     surfaces the excluded count as its own named bucket in G1's real
     output, same as "no_account_num" — visible in the conservation
     ledger itself, not just in filter_bpp_accounts()'s return value one
     layer up. (Supersedes the pre-rev1 docstring's stated design, which
     deliberately kept BPP OUT of skip_reason — this brief overrides that
     with a direct instruction; filter_bpp_accounts() in
     load_dallas_certified.py is updated to match.)
══════════════════════════════════════════════════════════════════════════
PX-20260826-04 FINDING #2 (real dry-run, PM re-ruling) — the "ACCOUNT_NUM
must be all-digits or fail loud" rule above (rev1's own CONFIRMED item 3)
was WRONG, not just incomplete: 205,049 of 806,563 real ACCOUNT_NUMs are
alphanumeric — letters are STRUCTURAL block/unit designators, not
corruption — including 190,375 loadable RES/COM accounts. Failing loud on
all of them, as the pre-this-finding code did, would have silently thrown
away 23.6% of the real, loadable population disguised as a data-integrity
stop. PM re-ruling, now the confirmed design:

  geo_id  — UNCHANGED: ACCOUNT_NUM verbatim (text).
  prop_id — now two-tier:
      all-digit ACCOUNT_NUM    -> int(ACCOUNT_NUM)                  (unchanged)
      alphanumeric ACCOUNT_NUM -> first 8 bytes of SHA-256('DALLAS:'
                                   + ACCOUNT_NUM), with bit 62 forced
                                   set — disjoint-by-construction from
                                   the all-digit numeric range. See
                                   _hashed_prop_id() below for the exact
                                   bit-layout this instruction requires
                                   (one necessary, disclosed
                                   concretization: bit 63 must also be
                                   masked off, or the hashed prop_id
                                   would overflow/negate a signed BIGINT
                                   for roughly half of all alphanumeric
                                   accounts — see that function's own
                                   docstring).

Two REQUIRED fail-loud guards accompany this (an identity collision is a
qualitatively different risk once prop_id is no longer a 1:1 deterministic
function of a validated-numeric string):
  1. In-run duplicate-prop_id detection across a single build — see
     DuplicatePropIdError and check_in_run_prop_id_collisions() below.
  2. A persistent, write-time guard rejecting a write whose prop_id
     already exists in prop_unit under a DIFFERENT geo_id — see
     find_prop_id_geo_id_conflicts() below for the pure comparison logic
     (the DB-querying wrapper lives in load_dallas_certified.py, keeping
     this module's own "no DB access" purity).
══════════════════════════════════════════════════════════════════════════
PX-20260826-05 Task 2 (PM BLOCKER, real dry-run runbook finding) —
load_dallas_certified.py never wrote `parcel` at all before this: only
Travis's three legacy loaders (load_ajr.py, load_certified_2025.py,
load_2026_preliminary.py) ever issued an INSERT INTO parcel. That's a
real gap, not a stylistic one — app.py's _county_has_data() and every
route gated on it query `parcel` directly, so a fully successful,
gate-PASS Dallas load into prop_unit/prop_unit_tax_year/parcel_tax_year
would still present as "hasn't been loaded yet" everywhere. Fixed by
adding derive_parcel_class_fields() below (the SPTD-derived state_cd1/
prop_type_cd class fields parcel needs) — the write itself
(write_parcel(), PARCEL_SQL) lives in load_dallas_certified.py, mirroring
load_certified_2025.py's own PROP.TXT -> parcel account-layer write.
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
import hashlib


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
# Header validation — PX-20260826-04 Task 1: real, fail-loud backstop
# against a drifted/wrong field name silently returning None for every
# row of a column this design's LOGIC (not just optional metadata)
# depends on. Only CONFIRMED, logic-critical columns are listed per
# table — see this module's own PX-20260826-04 changelog (top of file)
# for why the still-UNCONFIRMED optional fields are deliberately excluded
# from this hard-fail set.
# ══════════════════════════════════════════════════════════════════════
class HeaderDriftError(RuntimeError):
    """Raised when a real CSV's header row is missing a column this
    module's logic depends on. A real identity/value-integrity condition,
    not a per-row data-quality skip — mirrors derive_prop_id_geo_id()'s
    own fail-loud convention (see that function's docstring for the same
    "soft skip vs. hard stop" distinction)."""
    pass


EXPECTED_HEADERS = {
    "ACCOUNT_INFO": {ACCOUNT_NUM_FIELD, DIVISION_CD_FIELD},
    "ACCOUNT_APPRL_YEAR": {
        ACCOUNT_NUM_FIELD, "TOT_VAL", "LAND_VAL", "IMPR_VAL",
        "HMSTD_CAP_VAL", "SPTD_CODE", "COUNTY_TAXABLE_VAL", "APPRAISAL_YR",
    },
    "APPLIED_STD_EXEMPT": {ACCOUNT_NUM_FIELD},
}
# PRE-COMMIT FIX (real, disclosed correction): the field this design
# guessed as "TAX_YR" does not exist on the real CSV -- it was UNCONFIRMED
# and always came back None, which is why 100% of rows were silently
# falling back to the run's own --year argument. The real column is
# APPRAISAL_YR. Now CONFIRMED and promoted into EXPECTED_HEADERS above
# (hard-validated, HeaderDriftError on drift) -- no more soft degrade.
# The --year fallback is DELETED outright: every row's APPRAISAL_YR is now
# asserted to equal the run's own --year, fail-loud on mismatch (see
# AppraisalYearMismatchError / validate_appraisal_year() below, and
# build_unit_rows()'s own call site in load_dallas_certified.py).


def validate_header(table_name, fieldnames):
    """
    Fail loud if the real CSV's header row (fieldnames, as returned by
    csv.DictReader) is missing any column EXPECTED_HEADERS[table_name]
    requires. No entry for table_name (e.g. a deliberately-unloaded table)
    is a silent no-op, not an error — this function only ever validates
    tables this module actually parses.
    """
    expected = EXPECTED_HEADERS.get(table_name)
    if expected is None:
        return
    actual = set(fieldnames or [])
    missing = expected - actual
    if missing:
        raise HeaderDriftError(
            f"{table_name}: real CSV header is missing expected column(s) "
            f"{sorted(missing)} — got header {sorted(actual) if actual else '(empty)'}. "
            f"This is a fail-loud stop (PX-20260826-04 Task 1), not a soft "
            f"skip: a wrong/drifted field name would otherwise silently "
            f"return None for every row of that column, with no downstream "
            f"signal until a much harder-to-diagnose failure later."
        )


# ══════════════════════════════════════════════════════════════════════
# Low-level CSV primitive — generic across all 14 tables, mirroring
# ears_format.py's `_resolve_lines` + per-line dict-yielding shape, adapted
# for a header-having, comma-delimited, multi-column source instead of a
# fixed-width one.
# ══════════════════════════════════════════════════════════════════════
def iter_csv_rows(path=None, lines=None, table_name=None):
    """
    Yield one OrderedDict per data row (csv.DictReader shape) plus a
    1-based `_lineno` key (data rows only — the header is consumed, not
    counted as a data line). Either `path` (opened internally, utf-8-sig
    to tolerate a possible BOM — a real, common Excel-export artifact,
    unconfirmed either way for these files) or `lines` (an iterable of
    already-decoded CSV text lines, for fixture tests) must be given.

    table_name (PX-20260826-04): if given, the real header row is checked
    against EXPECTED_HEADERS[table_name] via validate_header() before any
    row is yielded — fail-loud on drift. Optional and defaults to None
    (no validation) so this primitive stays usable for ad hoc/exploratory
    reads of tables with no registered expectation.

    Deliberately thin otherwise: no further column validation, no type
    coercion — that's each per-table iter_*_records() function's job,
    same division of labor as ears_format.py's iter_prop_lines vs
    iter_prop_records.
    """
    if lines is not None:
        reader = csv.DictReader(lines)
    elif path is not None:
        f = open(path, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(f)
    else:
        raise ValueError("iter_csv_rows requires either path= or lines=")

    if table_name is not None:
        validate_header(table_name, reader.fieldnames)

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
      None             — normal, usable, non-BPP row
      'no_account_num' — blank/missing ACCOUNT_NUM (mirrors ears_format's
                         'no_geo_id' bucket — a genuinely unusable row)
      'bpp_excluded'   — PX-20260826-04: DIVISION_CD marks this account as
                         Business Personal Property. REVISED from
                         PX-20260826-03-rev1's design (which deliberately
                         kept BPP OUT of skip_reason, treating it as a
                         separate, later loader-level filter only) per
                         this brief's own explicit instruction: BPP
                         exclusion must be a NAMED G1 skip-bucket, visible
                         directly in scan_table_ledger()'s own bucket
                         counts, not just in filter_bpp_accounts()'s return
                         value one layer up in load_dallas_certified.py.

    Field name confidence: ACCOUNT_NUM (CONFIRMED), DIVISION_CD (CONFIRMED).
    GIS_PARCEL_ID (UNCONFIRMED — named in the brief's own open question
    "does GIS_PARCEL_ID play the geo_id role" but never independently
    confirmed as the literal column header; kept here as the best-guess
    field name pending a real header read). owner_name/situs_address field
    names are UNCONFIRMED entirely — ACCOUNT_INFO's brief description says
    it "includes identity/owner/situs/legal" but names no specific columns;
    OWNER_NAME/SITUS_ADDR below are placeholders, not evidenced strings.
    """
    for row in iter_csv_rows(path, lines, table_name="ACCOUNT_INFO"):
        account_num = (row.get(ACCOUNT_NUM_FIELD) or "").strip() or None
        division_cd = (row.get(DIVISION_CD_FIELD) or "").strip() or None
        is_bpp = is_bpp_division(division_cd)
        if not account_num:
            skip_reason = "no_account_num"
        elif is_bpp:
            skip_reason = "bpp_excluded"
        else:
            skip_reason = None
        yield {
            "_lineno": row["_lineno"],
            "skip_reason": skip_reason,
            "account_num": account_num,
            "division_cd": division_cd,
            "is_bpp": is_bpp,
            "gis_parcel_id": (row.get("GIS_PARCEL_ID") or "").strip() or None,       # UNCONFIRMED field name
            "owner_name": (row.get("OWNER_NAME") or "").strip() or None,             # UNCONFIRMED field name
            "situs_address": (row.get("SITUS_ADDR") or "").strip() or None,          # UNCONFIRMED field name
        }


# ══════════════════════════════════════════════════════════════════════
# prop_id / geo_id derivation — PX-20260826-04 finding #2 PM re-ruling
# (supersedes rev1's "all-digit or fail loud" rule; see this module's own
# changelog block at the top for why that rule was wrong, not just
# incomplete). geo_id = ACCOUNT_NUM verbatim (text — UNCHANGED, satisfies
# MC-1 rule 3 exactly, VARCHAR(20) already fits 17 chars). prop_id is now
# two-tier: int(ACCOUNT_NUM) for all-digit accounts, a truncated-SHA-256
# derivation for alphanumeric ones — both fit BIGINT with zero schema
# change, and are disjoint-by-construction from each other (see
# _hashed_prop_id() below for the exact bit-layout reasoning).
# ══════════════════════════════════════════════════════════════════════
class DuplicatePropIdError(RuntimeError):
    """Raised when two different ACCOUNT_NUMs (geo_ids) derive the same
    prop_id — an identity collision. Two independent, required guards use
    this: check_in_run_prop_id_collisions() (within a single build, pure)
    and load_dallas_certified.py's write-time guard (against what's
    already durably written, built on find_prop_id_geo_id_conflicts()
    below). Both are genuine identity-integrity stops, not per-row data-
    quality skips — same class of hard-stop as classification_map.py's
    UNKNOWN convention."""
    pass


_HASH_PREFIX = "DALLAS:"
_HASH_BIT62 = 1 << 62             # forces the hashed prop_id into [2**62, 2**63-1]
_HASH_LOW62_MASK = _HASH_BIT62 - 1  # low 62 bits of the truncated hash


def _hashed_prop_id(account_num):
    """
    PX-20260826-04 finding #2: prop_id derivation for an alphanumeric
    ACCOUNT_NUM. Per the PM's ruling: first 8 bytes of
    SHA-256('DALLAS:' + ACCOUNT_NUM), with bit 62 forced set — disjoint-
    by-construction from the all-digit numeric range (a 17-digit
    ACCOUNT_NUM maxes out under 10**17, i.e. well under 2**57 — far below
    2**62).

    One necessary, disclosed concretization of that instruction: the raw
    8 bytes are also masked down to their low 62 bits BEFORE bit 62 is
    OR'd on. Without that mask, bit 63 (the sign bit of Postgres's signed
    64-bit BIGINT) would be left at whatever the hash naturally produced —
    roughly half of all alphanumeric accounts would then derive a prop_id
    that either overflows BIGINT's positive range or, if inserted as a
    Python int, silently becomes negative depending on the driver's
    handling. Masking to the low 62 bits first guarantees every hashed
    prop_id lands in [2**62, 2**63-1]: always positive, always fits
    BIGINT, and the disjointness the PM asked for is preserved exactly
    (only which sub-range within "definitely not the digit range" changes,
    not whether it's disjoint). Flagged for PM sign-off same as any other
    real, load-bearing interpretation of an instruction that wasn't fully
    pinned down to the bit.
    """
    digest = hashlib.sha256((_HASH_PREFIX + account_num).encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big")
    return (raw & _HASH_LOW62_MASK) | _HASH_BIT62


def derive_prop_id_geo_id(account_num):
    """
    PX-20260826-04 finding #2 (PM re-ruling, supersedes PX-20260826-03-
    rev1's "all-digit or fail loud" rule — see this module's own changelog
    at the top for why that rule was wrong: 205,049 of 806,563 real
    ACCOUNT_NUMs are alphanumeric by design, not corruption).

    geo_id — UNCHANGED: ACCOUNT_NUM verbatim (text).
    prop_id — two-tier:
        all-digit ACCOUNT_NUM    -> int(ACCOUNT_NUM)
        alphanumeric ACCOUNT_NUM -> _hashed_prop_id(ACCOUNT_NUM) (see that
                                     function's own docstring)

    The only remaining fail-loud case here is a None/blank ACCOUNT_NUM —
    a genuinely missing identity, not a shape question hashing can
    answer. Duplicate-prop_id detection is NOT this function's job (it
    has no visibility across calls) — see DuplicatePropIdError,
    check_in_run_prop_id_collisions() below, and
    load_dallas_certified.py's write-time guard.

    Returns (prop_id: int, geo_id: str).
    """
    if account_num is None:
        raise ValueError("derive_prop_id_geo_id: ACCOUNT_NUM is None")
    geo_id = str(account_num).strip()
    if not geo_id:
        raise ValueError("derive_prop_id_geo_id: ACCOUNT_NUM is blank")
    if geo_id.isdigit():
        prop_id = int(geo_id)
    else:
        prop_id = _hashed_prop_id(geo_id)
    return prop_id, geo_id


def check_in_run_prop_id_collisions(unit_rows):
    """
    PX-20260826-04 finding #2, required guard 1 of 2: fail-loud, in-run
    duplicate-prop_id detection across a single build_unit_rows() output.
    Raises DuplicatePropIdError the instant two DIFFERENT geo_ids (i.e.
    different real ACCOUNT_NUMs) derive the same prop_id within this one
    run — before any row reaches the DB. Pure, no I/O; expects a list of
    dicts each with at least "prop_id" and "geo_id" keys (the
    build_unit_rows() row shape).

    This is deliberately separate from find_prop_id_geo_id_conflicts()
    below (which checks against rows already durably written in a PRIOR
    run) — a real collision could, in principle, happen within either
    scope, and this module's stance is to guard both rather than assume
    one implies the other.
    """
    seen = {}
    for row in unit_rows:
        prop_id = row["prop_id"]
        geo_id = row["geo_id"]
        prior_geo_id = seen.get(prop_id)
        if prior_geo_id is not None and prior_geo_id != geo_id:
            raise DuplicatePropIdError(
                f"In-run prop_id collision: ACCOUNT_NUM {geo_id!r} and "
                f"{prior_geo_id!r} both derive prop_id={prop_id} — "
                f"fail-loud per PX-20260826-04 finding #2's required "
                f"in-run duplicate-prop_id guard."
            )
        seen[prop_id] = geo_id


class AppraisalYearMismatchError(RuntimeError):
    """Raised when an ACCOUNT_APPRL_YEAR row's real APPRAISAL_YR column
    does not equal the run's own --year argument. PRE-COMMIT FIX (real
    correction): this design previously guessed the column was named
    "TAX_YR" (UNCONFIRMED, and wrong -- that column does not exist), so
    every row's value came back None and silently fell back to --year.
    That fallback masked what should have been a fail-loud signal: a real
    mismatch here means either the wrong archive folder was pointed at
    (e.g. --year 2026 against a 2025 extract) or a genuine per-account
    data anomaly worth surfacing, not papering over. The fallback is now
    DELETED outright -- see validate_appraisal_year() below."""
    pass


def validate_appraisal_year(appraisal_yr, expected_year, account_num=None):
    """
    PRE-COMMIT FIX: fail-loud, per-row cross-check between ACCOUNT_APPRL_
    YEAR.APPRAISAL_YR and the run's own --year argument. No fallback --
    a None/missing appraisal_yr (e.g. an account with no matching
    ACCOUNT_APPRL_YEAR row at all) is treated exactly the same as a real
    mismatch: it does not equal expected_year, so it raises. Pure, no I/O.
    """
    if appraisal_yr != expected_year:
        acct = f" (ACCOUNT_NUM={account_num!r})" if account_num else ""
        raise AppraisalYearMismatchError(
            f"APPRAISAL_YR mismatch{acct}: row's real APPRAISAL_YR is "
            f"{appraisal_yr!r}, but this run's --year is {expected_year!r}. "
            f"Fail-loud, no fallback (PRE-COMMIT FIX) -- either the wrong "
            f"archive folder is being loaded for this --year, or this is a "
            f"genuine per-account anomaly that must be investigated, not "
            f"silently coerced."
        )


def find_prop_id_geo_id_conflicts(existing_prop_id_geo_id, unit_rows):
    """
    PX-20260826-04 finding #2, required guard 2 of 2: pure comparison
    logic for the persistent, write-time identity guard.
    existing_prop_id_geo_id is a {prop_id: geo_id} map of what's ALREADY
    on file in prop_unit for this county (the caller — see
    load_dallas_certified.py's _fetch_existing_prop_id_geo_id() — does the
    actual DB query; this function stays DB-free, matching this module's
    own "no DB access" purity). unit_rows is this run's own
    build_unit_rows() output.

    A real conflict here means one of two things: (a) a genuine SHA-256
    collision between two different alphanumeric ACCOUNT_NUMs
    (astronomically unlikely at this population size, not provably
    impossible), or (b) — far more likely in practice — this run is about
    to silently corrupt an unrelated, already-loaded account's geo_id via
    PROP_UNIT_UPSERT_SQL's own ON CONFLICT (county_code, prop_id) DO
    UPDATE, which has no way to know the two geo_ids belong to different
    real-world accounts and would simply overwrite in place. Either way
    this is a fail-loud stop, not a per-row skip.

    Returns a list of (prop_id, existing_geo_id, incoming_geo_id) tuples —
    empty if clean.
    """
    conflicts = []
    for row in unit_rows:
        prop_id = row["prop_id"]
        incoming_geo_id = row["geo_id"]
        existing_geo_id = existing_prop_id_geo_id.get(prop_id)
        if existing_geo_id is not None and existing_geo_id != incoming_geo_id:
            conflicts.append((prop_id, existing_geo_id, incoming_geo_id))
    return conflicts


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
    Yields dicts with keys: account_num, appraisal_yr, impr_val, land_val,
    tot_val, hmstd_cap_val, sptd_code, county_taxable_val, skip_reason.

    appraisal_yr (PRE-COMMIT FIX, real correction): the real column is
    APPRAISAL_YR — CONFIRMED against the real file, replacing the earlier
    UNCONFIRMED "TAX_YR" guess, which does not exist on the real CSV (it
    always came back None, silently masked by a --year fallback that has
    now been deleted). Now header-validated (see EXPECTED_HEADERS) and
    cross-checked per row against the run's own --year argument — see
    validate_appraisal_year() below and build_unit_rows()'s own call site
    in load_dallas_certified.py.

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

    VALUE-COLUMN MAPPING TO THE UNIT MODEL — PX-20260826-04: PM-verified
    against real 2026 rows, baked in here rather than re-derived:
      TOT_VAL           -> prop_unit_tax_year.market_value  (direct analog)
      LAND_VAL          -> prop_unit_tax_year.land_value     (direct analog)
      IMPR_VAL          -> prop_unit_tax_year.imprv_value     (direct analog)
      HMSTD_CAP_VAL      -> RESOLVED (was the pre-rev1 unresolved semantic
                       gap): confirmed to be the CAPPED VALUE ITSELF (the
                       post-cap assessed value), not a loss amount, unlike
                       Travis's own hs_cap_loss column (which stores the
                       loss amount directly). See derive_value_mapping()
                       below for the actual arithmetic this resolution
                       implies: assessed_value = HMSTD_CAP_VAL when > 0
                       (cap present — whether or not currently binding),
                       else TOT_VAL; hs_cap_loss = TOT_VAL − HMSTD_CAP_VAL
                       when a cap is present (0 when not binding, i.e.
                       HMSTD_CAP_VAL == TOT_VAL), else NULL.
      COUNTY_TAXABLE_VAL -> prop_unit_tax_year.taxable_value (RESOLVED, PM-
                       verified): the county-jurisdiction taxable value,
                       matching Travis's own county-entity semantic (see
                       ears_format.py's TCO_ENTITY_CODES/is_tco logic for
                       the Travis-side analog of "which jurisdiction is
                       the county itself"). The other five jurisdictions'
                       own taxable/ceiling/split columns remain
                       DELIBERATELY UNLOADED (TABLE_LOAD_POLICY below) —
                       prop_unit_tax_year has room for exactly one
                       taxable_value column, and COUNTY_TAXABLE_VAL is it.
      SPTD_CODE          -> classification input, see classify_account_sptd()
                       below and this module's PX-20260826-04 changelog
                       for the interface-reconciliation note (SPTB vs
                       SPTD naming).
    """
    for row in iter_csv_rows(path, lines, table_name="ACCOUNT_APPRL_YEAR"):
        account_num = (row.get(ACCOUNT_NUM_FIELD) or "").strip() or None
        skip_reason = "no_account_num" if not account_num else None
        yield {
            "_lineno": row["_lineno"],
            "skip_reason": skip_reason,
            "account_num": account_num,
            "appraisal_yr": _int_or_none(row.get("APPRAISAL_YR")),   # CONFIRMED field name (PRE-COMMIT FIX -- replaces the wrong "TAX_YR" guess)
            "impr_val": _int_or_none(row.get("IMPR_VAL")),           # CONFIRMED field name
            "land_val": _int_or_none(row.get("LAND_VAL")),           # CONFIRMED field name
            "tot_val": _int_or_none(row.get("TOT_VAL")),             # CONFIRMED field name
            "hmstd_cap_val": _int_or_none(row.get("HMSTD_CAP_VAL")), # CONFIRMED field name + semantics (rev2/PX-20260826-04): capped VALUE, not loss
            "sptd_code": (row.get("SPTD_CODE") or "").strip() or None,  # CONFIRMED field name + location (col 47), rev1
            "county_taxable_val": _int_or_none(row.get("COUNTY_TAXABLE_VAL")),  # CONFIRMED field name (PX-20260826-04, PM-verified)
        }


# ══════════════════════════════════════════════════════════════════════
# derive_value_mapping — PX-20260826-04: the one place the PM's confirmed
# value-mapping arithmetic lives, so build_unit_rows() (and any future
# caller) never has to re-derive it. Pure function, no I/O.
# ══════════════════════════════════════════════════════════════════════
def derive_value_mapping(tot_val, land_val, impr_val, hmstd_cap_val, county_taxable_val):
    """
    Returns {market_value, land_value, imprv_value, assessed_value,
    hs_cap_loss, taxable_value} per the PM's confirmed PX-20260826-04
    rulings (bake in, don't re-derive):

      market_value   = TOT_VAL
      land_value     = LAND_VAL
      imprv_value    = IMPR_VAL
      assessed_value = HMSTD_CAP_VAL if HMSTD_CAP_VAL > 0 else TOT_VAL
      hs_cap_loss    = TOT_VAL - HMSTD_CAP_VAL where a cap is present
                       (HMSTD_CAP_VAL > 0), else NULL. A present-but-not-
                       binding cap (HMSTD_CAP_VAL == TOT_VAL) correctly
                       yields 0, not NULL — a real, meaningful zero, not
                       an absent value.
      taxable_value  = COUNTY_TAXABLE_VAL, verbatim

    None-safe: a None tot_val alongside a real (>0) hmstd_cap_val cannot
    produce a numeric hs_cap_loss (nothing to subtract from) — returns
    None for hs_cap_loss in that genuinely-incomplete-row case rather than
    raising or guessing. This is an ordinary data-quality gap (some
    expected fields missing on some rows), not the identity-integrity
    class of problem derive_prop_id_geo_id() fails loud on.
    """
    market_value = tot_val
    has_cap = hmstd_cap_val is not None and hmstd_cap_val > 0
    if has_cap:
        assessed_value = hmstd_cap_val
        hs_cap_loss = (tot_val - hmstd_cap_val) if tot_val is not None else None
    else:
        assessed_value = tot_val
        hs_cap_loss = None
    return {
        "market_value": market_value,
        "land_value": land_val,
        "imprv_value": impr_val,
        "assessed_value": assessed_value,
        "hs_cap_loss": hs_cap_loss,
        "taxable_value": county_taxable_val,
    }


# ══════════════════════════════════════════════════════════════════════
# classify_account_sptd — PX-20260826-04 Task 1: real wiring of
# ACCOUNT_APPRL_YEAR.SPTD_CODE into classification_map_dallas.py. Lazy
# import (not a module-level import) so this module keeps its own stated
# "no DB access, no config import" purity for callers that never need
# classification — matches this codebase's existing convention of keeping
# cross-cutting imports out of a pure-parsing module's top level wherever
# reasonably possible.
# ══════════════════════════════════════════════════════════════════════
def classify_account_sptd(sptd_code):
    """
    Classify one account's SPTD_CODE into a benchmark label (or the
    UNMAPPED_DALLAS sentinel) via classification_map_dallas.
    classify_dallas_sptb_code(). Verified interface (PX-20260826-04 Task
    1): that function takes a bare code string and matches on the FIRST
    CHARACTER only, uppercased — SPTD_CODE's own real values (A11, F10,
    etc.) are exactly that shape, so this is a direct pass-through, no
    adapter needed. See this module's own PX-20260826-04 changelog for
    the one open SPTB-vs-SPTD field-name terminology question this
    wiring surfaced (a documentation inconsistency, not a data-shape
    mismatch — does not block this wiring).
    """
    import classification_map_dallas as _cmd
    return _cmd.classify_dallas_sptb_code(sptd_code)


# ══════════════════════════════════════════════════════════════════════
# derive_parcel_class_fields — PX-20260826-05 Task 2 (PM BLOCKER): the
# SPTD-derived class fields for `parcel`, the account-layer table
# load_dallas_certified.py's write_parcel() upserts (see that module's
# own PX-20260826-05 changelog for why `parcel` was never written before
# this — a real, load-bearing gap the PX-20260826-05 runbook surfaced:
# _county_has_data() and every route gated on it query `parcel` directly,
# not prop_unit/prop_unit_tax_year).
# ══════════════════════════════════════════════════════════════════════
def derive_parcel_class_fields(sptd_code):
    """
    Returns {"prop_type_cd": ..., "state_cd1": ...} for one account's real
    SPTD_CODE, mirroring Travis's own parcel.prop_type_cd / parcel.state_cd1
    columns so Dallas's `parcel` rows carry the same real-world class
    information Travis's do, sourced from DCAD's own confirmed data rather
    than guessed:

    prop_type_cd -> the raw SPTD_CODE itself, verbatim. Same convention
        prop_unit.prop_type_cd already uses (build_unit_rows() sets
        "prop_type_cd": sptd_code there too) — mirrors Travis's own
        parcel.prop_type_cd, which stores a raw source code the same way
        (R/P/MH/MN, straight from PROP.TXT, per schema.sql's own comment).

    state_cd1 -> the real PTAD property class code DCAD_SPTD_CD_XREF_2011
        (classification_map_dallas.py's own real, official DCAD cross-
        reference — see that module's docstring) maps this SPTD code to,
        e.g. "A11" -> "A", "F10" -> "F1", "D10" -> "D1". This is the direct
        Dallas analog of Travis's own parcel.state_cd1 (AJR's own
        ptd_state_cd field — schema.sql's comment: "PTD state property
        code (e.g. A, F1, B)") — the SAME real-world quantity, the SAME
        statewide Comptroller/PTAD taxonomy both counties' EARS/DCAD
        submissions follow, sourced here from DCAD's own confirmed
        cross-reference rather than re-derived or guessed.

    None-safe: a None/blank sptd_code, or one absent from
    DCAD_SPTD_CD_XREF_2011 (e.g. a real SPTD code observed in the wild
    that isn't in the 2011 document's own list — see that dict's own
    DALLAS_SPTB_DOCUMENTED_BUT_UNOBSERVED sibling for known gaps), returns
    state_cd1=None rather than raising. This is a display-field
    derivation, not the identity-integrity class of check
    derive_prop_id_geo_id() fails loud on — an unrecognized class code
    should degrade to an absent field, not block the load.
    """
    import classification_map_dallas as _cmd
    state_cd1 = None
    if sptd_code:
        entry = _cmd.DCAD_SPTD_CD_XREF_2011.get(str(sptd_code).strip().upper())
        if entry:
            state_cd1 = entry[0]
    return {"prop_type_cd": sptd_code, "state_cd1": state_cd1}


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
    for row in iter_csv_rows(path, lines, table_name="APPLIED_STD_EXEMPT"):
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
        "columns (TOT_VAL/LAND_VAL/IMPR_VAL/HMSTD_CAP_VAL/COUNTY_TAXABLE_VAL) "
        "AND the classification code (SPTD_CODE, column 47). One row per "
        "account. PX-20260826-04: HMSTD_CAP_VAL semantics and the "
        "taxable_value column are now both RESOLVED (see "
        "derive_value_mapping()) -- COUNTY_TAXABLE_VAL is the one "
        "per-jurisdiction column this design loads; the other five "
        "jurisdictions' own taxable/ceiling/split columns remain outside "
        "this table's own loaded-column set (this table is still LOADED "
        "overall -- only those specific extra columns are not read)."
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
PX-20260826-04 UPDATE: HMSTD_CAP_VAL semantics and the taxable_value
column (COUNTY_TAXABLE_VAL) are now RESOLVED (PM-verified against real
2026 rows) and REMOVED from this list, alongside rev1's three prior
resolutions (TAXABLE_OBJECT's grain/key, SPTD_CODE's location, prop_id/
geo_id derivation).

PRE-COMMIT FIX UPDATE: TAX_YR is also REMOVED from this list -- it was
never the real column name (a wrong, UNCONFIRMED guess). The real column
is APPRAISAL_YR, now CONFIRMED, hard-validated via EXPECTED_HEADERS, and
cross-checked per row against --year (fail-loud on mismatch, no fallback
-- see validate_appraisal_year()). What remains genuinely open:

Run against the real, extracted DCAD2026_CERTIFIED_07232026/ folder
(PARCELYTICS_ARCHIVE_ROOT/dallas/certified_roll/2026-08-26/2026 Certified/...),
one command per table, before load_dallas_certified.py touches a live row:

  head -1 ACCOUNT_INFO.CSV        # confirm GIS_PARCEL_ID, owner/situs field names (soft -- not header-validated, see EXPECTED_HEADERS)
  head -1 APPLIED_STD_EXEMPT.CSV  # confirm EXEMPT_CODE's real name (soft -- not header-validated)

The six CONFIRMED/logic-critical columns per loaded table (ACCOUNT_NUM,
DIVISION_CD; TOT_VAL/LAND_VAL/IMPR_VAL/HMSTD_CAP_VAL/SPTD_CODE/
COUNTY_TAXABLE_VAL/APPRAISAL_YR) are now hard-validated at load time by
validate_header() -- a real header mismatch on any of THOSE fails loud
immediately, rather than needing this manual checklist run first. This
checklist now covers only the remaining soft/optional fields, which
degrade gracefully (None) rather than blocking the load if their real
names differ.

One open reconciliation item for PM (PX-20260826-04, Task 1): confirm
whether classification_map_dallas.py's own DALLAS_SPTB_FIELD_NAME
constant ("SPTB CLASS CODE") and this session's confirmed ACCOUNT_APPRL_
YEAR.SPTD_CODE are the same real DCAD field under two different names
across two different source documents (SPTB vs SPTD) -- see this module's
own PX-20260826-04 changelog for the full reasoning; does not block
wiring (classify_account_sptd() works either way on the real code
VALUES), but the naming discrepancy itself should be closed out.
"""
