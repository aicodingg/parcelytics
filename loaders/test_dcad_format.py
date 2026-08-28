"""
loaders/test_dcad_format.py — fixture tests for dcad_format.py
(PX-20260826-03, revised PX-20260826-03-rev1).

HONEST DISCLOSURE (required reading before trusting these as "real-file
excerpts" the way the brief asked for): every row below is CONSTRUCTED from
documented/confirmed field names and shapes (ACCOUNT_NUM, DIVISION_CD,
TOT_VAL/LAND_VAL/IMPR_VAL/HMSTD_CAP_VAL, SPTD_CODE), NOT sampled bytes from
a real CSV -- this session has no access to the actual DCAD delivery (see
dcad_format.py's own module-level disclosure). These fixtures prove this
module's LOGIC is internally consistent and matches the documented shapes
(including the three PM-resolved rulings from rev1); they cannot prove the
remaining UNCONFIRMED field names actually match the real files. That
verification is the FIELD_NAME_VERIFICATION_CHECKLIST's job, not this
file's.

REV1 changes from the original test file: removed the TAXABLE_OBJECT
multi-object / finer-grain tests (that hypothesis was wrong -- see
dcad_format.py's REV1 changelog); added derive_prop_id_geo_id() tests
(confirmed mapping + fail-loud validation); added an sptd_code passthrough
test; renamed the orphan test to use APPLIED_STD_EXEMPT (TAXABLE_OBJECT no
longer participates in the value path at all); updated build_unit_rows()
tests for the new 3-arg signature.

PX-20260826-04 changes ("the bodies"): ACCOUNT_APPRL_YEAR_CSV now includes
COUNTY_TAXABLE_VAL (a real, header-validated column as of this brief --
the fixture would now raise HeaderDriftError without it, which is itself
new, intentional behavior, not a bug). BPP exclusion is now a named G1
skip-bucket ("bpp_excluded") rather than a loader-only filter -- updated
every test that touched ACCOUNT_INFO's skip_reason/bucket counts
accordingly. Added: derive_value_mapping() cases (cap binding, cap present
but not binding, no cap at all), classify_account_sptd() wiring test,
validate_header()/HeaderDriftError fail-loud test, and extended
build_unit_rows() assertions to cover the now-fully-populated
assessed_value/hs_cap_loss/taxable_value/prop_type_cd/benchmark_label
fields (previously deliberately left None pending PM's rulings, now
resolved and asserted for real).

PX-20260826-04 FINDING #2 changes (real dry-run, PM re-ruling): the old
"non-digit ACCOUNT_NUM fails loud" test is REMOVED and REPLACED --
205,049 of 806,563 real ACCOUNT_NUMs are alphanumeric BY DESIGN (letters
are structural block/unit designators), so that old fail-loud expectation
was itself the bug the real dry-run caught. Added: an alphanumeric account
(ACCOUNT_NUM "0000012A456789016") to both ACCOUNT_INFO_CSV and
ACCOUNT_APPRL_YEAR_CSV, exercised end-to-end through build_unit_rows();
test_derive_prop_id_geo_id_alphanumeric_hashed() (bit-62-forced, disjoint-
from-digit-range, deterministic); test_derive_prop_id_geo_id_still_fails_
loud_on_none_and_blank() (the only remaining fail-loud cases);
test_check_in_run_prop_id_collisions_fires_on_forced_collision() (a
synthetic, deliberately-forced same-prop_id/different-geo_id pair --
proving the in-run guard, not relying on an actual SHA-256 collision,
which cannot be engineered on demand); test_find_prop_id_geo_id_conflicts()
(the pure write-time-guard comparison logic, both a clean case and a
forced conflict).

PX-20260826-05 Task 2 changes (PM BLOCKER): added
test_derive_parcel_class_fields() (state_cd1/prop_type_cd derivation via
DCAD_SPTD_CD_XREF_2011, including None/unknown-code degrade-gracefully
cases), test_build_unit_rows_carries_state_cd1() (proving build_unit_rows()
actually folds the derived state_cd1 into each row), and
test_write_parcel_idempotent_on_rerun()/test_write_parcel_dry_run_zero_db_access()
(a FakeConn + monkeypatched psycopg2.extras.execute_batch proving
write_parcel()'s real idempotency comes from PARCEL_SQL's own
ON CONFLICT ... DO UPDATE SET <col>=EXCLUDED.<col> shape, not app-level
dedup logic).

Run: python3 loaders/test_dcad_format.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from loaders import dcad_format  # noqa: E402

PASS_COUNT = 0
FAIL_COUNT = 0


def check(label, condition):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"[PASS] {label}")
    else:
        FAIL_COUNT += 1
        print(f"[FAIL] {label}")


# ── Constructed ACCOUNT_INFO sample: 3 real all-digit accounts + 1 real
# alphanumeric account (PX-20260826-04 finding #2 -- letters are structural
# block/unit designators, not corruption) + 1 BPP + 1 blank ─────────────
#
# PX-20260827-06 REBUILD (standing rule, brief item 4): the OWNER_NAME/
# SITUS_ADDR header this fixture used since PX-20260826-03 was itself the
# hand-typed guess that let the real bug (those two columns don't exist --
# iter_account_info_records() silently returned None for both, forever)
# ship undetected. Rebuilt against the PM's own PX-20260827-06 ruling's
# real field names (STREET_NUM/STREET_HALF_NUM/FULL_STREET_NAME/BLDG_ID/
# UNIT_ID, OWNER_NAME1/OWNER_NAME2/EXCLUDE_OWNER, PROPERTY_ZIPCODE) -- see
# this file's own HONEST DISCLOSURE at the top and the PX-20260827-06
# report's own sandbox-vs-live section for what this rebuild can and can't
# prove: no vault access in this sandbox means the full real header
# (column order, completeness, whether any OTHER situs/owner column
# exists) still cannot be independently verified byte-for-byte against the
# real ACCOUNT_INFO.CSV -- these are the PM's own given field names, not
# re-derived. Row count/order/account_num/division_cd/skip_reason
# semantics are UNCHANGED from the original fixture (same 6 rows, same
# accepted/bpp_excluded/no_account_num bucket shape) -- only the owner/
# situs columns changed shape. Row 4 (the alphanumeric account) now also
# doubles as the "every situs/owner component populated" case (half-
# number, building, unit, AND a second owner) so the full space-join gets
# real coverage without a 7th fixture row.
ACCOUNT_INFO_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,STREET_NUM,STREET_HALF_NUM,FULL_STREET_NAME,BLDG_ID,UNIT_ID,OWNER_NAME1,OWNER_NAME2,EXCLUDE_OWNER,PROPERTY_ZIPCODE
00000123456789012,RES,GISP001,123,,MAIN ST,,,SMITH JOHN,,N,75201
00000123456789013,RES,GISP002,456,,OAK ST,,,DOE JANE,,N,75202
00000123456789014,COM,GISP003,789,,COMMERCE ST,,,ACME LLC,,N,75203
00000123456789015,BPP,,,,,,,BIZCO INC,,N,
0000012A456789016,RES,GISP005,321,1/2,BLOCK-UNIT AVE,BLDG A,UNIT 200,BLOCK OWNER,UNIT CO-OWNER,N,75205
,RES,GISP004,999,,NOWHERE AVE,,,BLANK ACCOUNT,,N,75299
"""

# ── Constructed ACCOUNT_APPRL_YEAR sample -- REV1: includes SPTD_CODE
# (column 47, CONFIRMED classification input). PX-20260826-04: includes
# COUNTY_TAXABLE_VAL (CONFIRMED, PM-verified taxable_value source) -- now
# a header-validated column, see test_header_drift_fails_loud_on_missing_
# confirmed_column() below. Row 1's HMSTD_CAP_VAL (350000) is the CAPPED
# VALUE ITSELF (PX-20260826-04 resolved semantics, not the loss amount
# the pre-rev2 fixture's "50000" value ambiguously suggested under either
# reading -- deliberately changed to 350000 here so the fixture is
# unambiguous proof of the resolved semantics, not compatible with both).
# Row 2's HMSTD_CAP_VAL=0 means no cap at all (assessed_value falls back
# to TOT_VAL, hs_cap_loss is None) -- see test_derive_value_mapping_* below.
# PRE-COMMIT FIX: TAX_YR (a wrong, UNCONFIRMED guess) replaced with the
# real column, APPRAISAL_YR -- all rows here are 2026 to match the fixture
# tests' own implicit --year=2026 run. See ACCOUNT_APPRL_YEAR_MISMATCH_CSV
# below for the dedicated mismatch fixture.
ACCOUNT_APPRL_YEAR_CSV = """ACCOUNT_NUM,APPRAISAL_YR,IMPR_VAL,LAND_VAL,TOT_VAL,HMSTD_CAP_VAL,SPTD_CODE,COUNTY_TAXABLE_VAL
00000123456789012,2026,300000,100000,400000,350000,A11,340000
00000123456789013,2026,200000,80000,280000,0,A11,280000
00000123456789014,2026,600000,300000,900000,0,F10,900000
0000012A456789016,2026,150000,50000,200000,0,A11,200000
"""

# PRE-COMMIT FIX: dedicated fixture for the fail-loud mismatch case -- one
# account whose real APPRAISAL_YR (2025) does not match the run's own
# --year (2026).
ACCOUNT_APPRL_YEAR_MISMATCH_CSV = """ACCOUNT_NUM,APPRAISAL_YR,IMPR_VAL,LAND_VAL,TOT_VAL,HMSTD_CAP_VAL,SPTD_CODE,COUNTY_TAXABLE_VAL
00000123456789012,2025,300000,100000,400000,350000,A11,340000
"""

APPLIED_STD_EXEMPT_CSV = """ACCOUNT_NUM,EXEMPT_CODE
00000123456789012,HS
00000123456789012,OV65
00000123456789013,HS
00000999999999999,HS
"""


def test_account_info_basic_parse_and_bpp_flag():
    rows = list(dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines()))
    check("ACCOUNT_INFO: 6 rows read", len(rows) == 6)
    check("ACCOUNT_INFO: RES account not flagged BPP", rows[0]["is_bpp"] is False)
    check("ACCOUNT_INFO: COM account not flagged BPP", rows[2]["division_cd"] == "COM" and rows[2]["is_bpp"] is False)
    check("ACCOUNT_INFO: BPP account IS flagged BPP", rows[3]["is_bpp"] is True)
    check("ACCOUNT_INFO: PX-20260826-04 -- BPP account gets skip_reason='bpp_excluded' (named G1 skip-bucket, not silent)",
          rows[3]["skip_reason"] == "bpp_excluded")
    check("ACCOUNT_INFO: PX-20260826-04 finding #2 -- alphanumeric ACCOUNT_NUM is a normal, accepted row (letters are structural, not corruption)",
          rows[4]["account_num"] == "0000012A456789016" and rows[4]["skip_reason"] is None)
    check("ACCOUNT_INFO: blank account_num gets skip_reason", rows[5]["skip_reason"] == "no_account_num")
    check("ACCOUNT_INFO: normal rows have skip_reason None", rows[0]["skip_reason"] is None)

    # PX-20260827-06: the actual bug fix -- these were unconditionally None
    # before this brief (OWNER_NAME/SITUS_ADDR don't exist on the real
    # file), regardless of what was in the fixture's own owner/situs
    # columns. Asserting real, non-None values here is the fixture's own
    # proof the derivation is wired to the real field names, not just that
    # the old placeholder lookup still silently returns None.
    check("ACCOUNT_INFO: PX-20260827-06 -- situs_address derived from real STREET_NUM+FULL_STREET_NAME (space-join, no punctuation)",
          rows[0]["situs_address"] == "123 MAIN ST")
    check("ACCOUNT_INFO: PX-20260827-06 -- owner_name derived from real OWNER_NAME1",
          rows[0]["owner_name"] == "SMITH JOHN")
    check("ACCOUNT_INFO: PX-20260827-06 -- zip_code derived from real PROPERTY_ZIPCODE",
          rows[0]["zip_code"] == "75201")
    check("ACCOUNT_INFO: PX-20260827-06 -- not suppressed (EXCLUDE_OWNER=N) has owner_suppressed=False",
          rows[0]["owner_suppressed"] is False)
    check("ACCOUNT_INFO: PX-20260827-06 -- full 5-component situs join (STREET_NUM, STREET_HALF_NUM, "
          "FULL_STREET_NAME, BLDG_ID, UNIT_ID all populated) on the alphanumeric-account row",
          rows[4]["situs_address"] == "321 1/2 BLOCK-UNIT AVE BLDG A UNIT 200")
    check("ACCOUNT_INFO: PX-20260827-06 -- owner_name joins BOTH OWNER_NAME1 and OWNER_NAME2 (co-owner)",
          rows[4]["owner_name"] == "BLOCK OWNER UNIT CO-OWNER")
    check("ACCOUNT_INFO: PX-20260827-06 -- BPP row's owner_name still parses (OWNER_NAME1 present) even "
          "though the row itself is separately BPP-excluded from acceptance",
          rows[3]["owner_name"] == "BIZCO INC")
    check("ACCOUNT_INFO: PX-20260827-06 -- BPP row's situs_address is None (all 5 components blank in fixture)",
          rows[3]["situs_address"] is None)


def test_bpp_exclusion_filters_at_loader_boundary():
    from loaders import load_dallas_certified as loader  # noqa

    records = list(dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines()))
    kept, excluded = loader.filter_bpp_accounts(records)
    check("BPP filter: 1 excluded (BPP account)", excluded == 1)
    check("BPP filter: blank-account_num row also excluded (skip_reason)", "00000123456789015" not in kept)
    check("BPP filter: 4 real accounts kept (2 RES digit + 1 COM digit + 1 RES alphanumeric)", len(kept) == 4)
    check("BPP filter: excluded account not in kept dict", "00000123456789015" not in kept)
    check("BPP filter: alphanumeric account kept (PX-20260826-04 finding #2)", "0000012A456789016" in kept)


def test_derive_prop_id_geo_id_confirmed_mapping():
    """REV1: prop_id = int(ACCOUNT_NUM), geo_id = ACCOUNT_NUM verbatim -- CONFIRMED."""
    prop_id, geo_id = dcad_format.derive_prop_id_geo_id("00000123456789012")
    check("derive_prop_id_geo_id: prop_id is int(ACCOUNT_NUM)", prop_id == 123456789012)
    check("derive_prop_id_geo_id: geo_id is ACCOUNT_NUM verbatim (string, leading zeros preserved)",
          geo_id == "00000123456789012")
    check("derive_prop_id_geo_id: geo_id fits VARCHAR(20) with room to spare", len(geo_id) <= 20)


def test_derive_prop_id_geo_id_alphanumeric_hashed():
    """PX-20260826-04 finding #2: an alphanumeric ACCOUNT_NUM is a REAL,
    loadable account (letters are structural block/unit designators, not
    corruption) -- 205,049 of 806,563 real accounts are this shape,
    including 190,375 loadable RES/COM. prop_id derives via
    _hashed_prop_id(): first 8 bytes of SHA-256('DALLAS:'+ACCOUNT_NUM),
    masked to 62 bits then bit 62 forced set."""
    prop_id, geo_id = dcad_format.derive_prop_id_geo_id("0000012A456789016")
    check("derive_prop_id_geo_id (alphanumeric): geo_id is ACCOUNT_NUM verbatim",
          geo_id == "0000012A456789016")
    check("derive_prop_id_geo_id (alphanumeric): prop_id has bit 62 set",
          prop_id & (1 << 62) != 0)
    check("derive_prop_id_geo_id (alphanumeric): prop_id is disjoint-by-construction from the all-digit range (>= 2**62, far above any 17-digit int)",
          prop_id >= (1 << 62) and prop_id < (1 << 63))
    check("derive_prop_id_geo_id (alphanumeric): prop_id fits a signed BIGINT (< 2**63)",
          prop_id < 2**63)
    check("derive_prop_id_geo_id (alphanumeric): deterministic -- same ACCOUNT_NUM always derives the same prop_id",
          dcad_format.derive_prop_id_geo_id("0000012A456789016")[0] == prop_id)

    prop_id_2, _ = dcad_format.derive_prop_id_geo_id("0000012B456789016")
    check("derive_prop_id_geo_id (alphanumeric): a different ACCOUNT_NUM derives a different prop_id (no accidental collision in this pair)",
          prop_id_2 != prop_id)

    digit_prop_id, _ = dcad_format.derive_prop_id_geo_id("00000123456789012")
    check("derive_prop_id_geo_id: digit-derived and hash-derived prop_ids are disjoint by range (digit range is always < 2**62)",
          digit_prop_id < (1 << 62) <= prop_id)


def test_derive_prop_id_geo_id_still_fails_loud_on_none_and_blank():
    """PX-20260826-04 finding #2: non-digit ACCOUNT_NUM is now a VALID,
    hashed path (see test above) -- the only remaining fail-loud cases are
    a genuinely missing identity: None or blank/whitespace-only."""
    raised_on_none = False
    try:
        dcad_format.derive_prop_id_geo_id(None)
    except ValueError:
        raised_on_none = True
    check("derive_prop_id_geo_id: raises ValueError on None (fail-loud)", raised_on_none)

    raised_on_blank = False
    try:
        dcad_format.derive_prop_id_geo_id("   ")
    except ValueError:
        raised_on_blank = True
    check("derive_prop_id_geo_id: raises ValueError on blank/whitespace-only ACCOUNT_NUM (fail-loud)", raised_on_blank)

    # Confirmed non-regression: a real, previously-tested non-digit case
    # that used to fail loud now succeeds via the hashed path instead.
    no_longer_raises = True
    try:
        dcad_format.derive_prop_id_geo_id("ABC123456789012XY")
    except ValueError:
        no_longer_raises = False
    check("derive_prop_id_geo_id: PX-20260826-04 finding #2 -- a non-digit ACCOUNT_NUM no longer fails loud (hashed instead)",
          no_longer_raises)


def test_check_in_run_prop_id_collisions_fires_on_forced_collision():
    """PX-20260826-04 finding #2, required guard 1 of 2. A real SHA-256
    collision cannot be engineered on demand, so this proves the GUARD
    LOGIC itself fires correctly against a synthetically forced collision:
    two different geo_ids deliberately given the same prop_id."""
    clean_rows = [
        {"prop_id": 111, "geo_id": "AAA"},
        {"prop_id": 222, "geo_id": "BBB"},
    ]
    raised_on_clean = False
    try:
        dcad_format.check_in_run_prop_id_collisions(clean_rows)
    except dcad_format.DuplicatePropIdError:
        raised_on_clean = True
    check("check_in_run_prop_id_collisions: does NOT fire on distinct prop_ids", not raised_on_clean)

    colliding_rows = [
        {"prop_id": 111, "geo_id": "AAA"},
        {"prop_id": 222, "geo_id": "BBB"},
        {"prop_id": 111, "geo_id": "CCC"},  # forced collision: same prop_id as row 1, different geo_id
    ]
    raised_on_collision = False
    try:
        dcad_format.check_in_run_prop_id_collisions(colliding_rows)
    except dcad_format.DuplicatePropIdError:
        raised_on_collision = True
    check("check_in_run_prop_id_collisions: FIRES on a forced same-prop_id/different-geo_id collision", raised_on_collision)

    # Same geo_id repeated with the same prop_id is NOT a collision (just
    # the same account appearing twice, e.g. a duplicate CSV line) --
    # only a DIFFERENT geo_id sharing a prop_id is the real hazard.
    same_account_twice = [
        {"prop_id": 111, "geo_id": "AAA"},
        {"prop_id": 111, "geo_id": "AAA"},
    ]
    raised_on_dup_same_account = False
    try:
        dcad_format.check_in_run_prop_id_collisions(same_account_twice)
    except dcad_format.DuplicatePropIdError:
        raised_on_dup_same_account = True
    check("check_in_run_prop_id_collisions: does NOT fire when the SAME geo_id repeats under the same prop_id",
          not raised_on_dup_same_account)


def test_find_prop_id_geo_id_conflicts():
    """PX-20260826-04 finding #2, required guard 2 of 2 -- the pure
    comparison logic the write-time DB guard is built on
    (load_dallas_certified._fetch_existing_prop_id_geo_id() supplies the
    real DB-sourced existing map; this test supplies a fabricated one)."""
    unit_rows = [
        {"prop_id": 111, "geo_id": "AAA"},
        {"prop_id": 222, "geo_id": "BBB"},
    ]
    existing_clean = {111: "AAA"}  # same account already on file -- fine
    check("find_prop_id_geo_id_conflicts: no conflict when existing geo_id matches incoming",
          dcad_format.find_prop_id_geo_id_conflicts(existing_clean, unit_rows) == [])

    existing_new = {}  # prop_id not seen before -- fine, nothing to conflict with
    check("find_prop_id_geo_id_conflicts: no conflict against an empty existing map",
          dcad_format.find_prop_id_geo_id_conflicts(existing_new, unit_rows) == [])

    existing_conflict = {111: "ZZZ"}  # DIFFERENT geo_id already on file under prop_id 111
    conflicts = dcad_format.find_prop_id_geo_id_conflicts(existing_conflict, unit_rows)
    check("find_prop_id_geo_id_conflicts: detects a real conflict (existing geo_id differs from incoming)",
          conflicts == [(111, "ZZZ", "AAA")])


def test_iter_account_apprl_year_reads_real_appraisal_yr_column():
    """PRE-COMMIT FIX: the real column is APPRAISAL_YR, not the wrong
    "TAX_YR" guess this design previously made (which doesn't exist on the
    real CSV, and always returned None)."""
    rows = list(dcad_format.iter_account_apprl_year_records(lines=ACCOUNT_APPRL_YEAR_CSV.splitlines()))
    check("iter_account_apprl_year_records: appraisal_yr parsed from the real APPRAISAL_YR column",
          all(r["appraisal_yr"] == 2026 for r in rows))
    check("iter_account_apprl_year_records: no stale 'tax_yr' key anywhere in the output",
          all("tax_yr" not in r for r in rows))


def test_validate_appraisal_year_fail_loud_on_mismatch():
    """PRE-COMMIT FIX: validate_appraisal_year() is the one place this
    fail-loud cross-check lives -- no fallback to --year on a mismatch or
    on a missing (None) appraisal_yr."""
    raised_on_match = False
    try:
        dcad_format.validate_appraisal_year(2026, 2026, account_num="00000123456789012")
    except dcad_format.AppraisalYearMismatchError:
        raised_on_match = True
    check("validate_appraisal_year: does NOT raise when appraisal_yr == expected_year", not raised_on_match)

    raised_on_mismatch = False
    try:
        dcad_format.validate_appraisal_year(2025, 2026, account_num="00000123456789012")
    except dcad_format.AppraisalYearMismatchError:
        raised_on_mismatch = True
    check("validate_appraisal_year: FAILS LOUD when appraisal_yr != expected_year (no fallback)", raised_on_mismatch)

    raised_on_none = False
    try:
        dcad_format.validate_appraisal_year(None, 2026, account_num="00000123456789012")
    except dcad_format.AppraisalYearMismatchError:
        raised_on_none = True
    check("validate_appraisal_year: FAILS LOUD on a missing (None) appraisal_yr -- treated exactly like a mismatch, not a soft skip",
          raised_on_none)


def test_build_unit_rows_fails_loud_on_appraisal_year_mismatch():
    """PRE-COMMIT FIX: the real, end-to-end fixture for this brief's
    explicit ask -- one account whose real APPRAISAL_YR (2025) does not
    match the run's own --year (2026) must fail the whole build loud, not
    silently fall back to --year the way the old (wrong) TAX_YR-based
    code did."""
    from loaders import load_dallas_certified as loader  # noqa

    account_info_recs = [r for r in dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines())
                          if r["skip_reason"] is None and not r["is_bpp"]]
    account_info_by_num = {r["account_num"]: r for r in account_info_recs}

    appr_recs = {r["account_num"]: r for r in dcad_format.iter_account_apprl_year_records(
        lines=ACCOUNT_APPRL_YEAR_MISMATCH_CSV.splitlines())}

    raised = False
    try:
        loader.build_unit_rows(account_info_by_num, appr_recs, {}, 2026)
    except dcad_format.AppraisalYearMismatchError:
        raised = True
    check("build_unit_rows: FAILS LOUD end-to-end on a real APPRAISAL_YR/--year mismatch (2025 row, --year=2026)",
          raised)


def test_orphan_account_classified_not_hidden():
    """Mirrors PX-20260826-01's BG4 LEGACY-ONLY test shape: a secondary-table
    row whose account_num has no ACCOUNT_INFO counterpart must be reported
    by name, not silently dropped or blanket-failed. REV1: demonstrated
    against APPLIED_STD_EXEMPT, since TAXABLE_OBJECT no longer participates
    in this design at all."""
    from loaders import load_dallas_certified as loader  # noqa

    account_ledger = dcad_format.scan_table_ledger(
        "ACCOUNT_INFO", dcad_format.iter_account_info_records, lines=ACCOUNT_INFO_CSV.splitlines()
    )
    exempt_ledger = dcad_format.scan_table_ledger(
        "APPLIED_STD_EXEMPT", dcad_format.iter_applied_std_exempt_records, lines=APPLIED_STD_EXEMPT_CSV.splitlines()
    )

    orphans = loader.find_orphan_accounts(account_ledger, exempt_ledger)
    check("ORPHAN-ACCOUNT: exactly 1 orphan account found (...999999999999)",
          orphans == {"00000999999999999"})
    check("ORPHAN-ACCOUNT: real, matched accounts NOT flagged as orphans",
          "00000123456789012" not in orphans)


def test_account_apprl_year_confirmed_value_mapping_and_sptd_code():
    rows = list(dcad_format.iter_account_apprl_year_records(lines=ACCOUNT_APPRL_YEAR_CSV.splitlines()))
    check("ACCOUNT_APPRL_YEAR: 4 rows read", len(rows) == 4)
    check("ACCOUNT_APPRL_YEAR: TOT_VAL parsed as int", rows[0]["tot_val"] == 400000)
    check("ACCOUNT_APPRL_YEAR: LAND_VAL parsed as int", rows[0]["land_val"] == 100000)
    check("ACCOUNT_APPRL_YEAR: IMPR_VAL parsed as int", rows[0]["impr_val"] == 300000)
    check("ACCOUNT_APPRL_YEAR: LAND_VAL + IMPR_VAL == TOT_VAL (internal consistency of the constructed fixture)",
          rows[0]["land_val"] + rows[0]["impr_val"] == rows[0]["tot_val"])
    check("ACCOUNT_APPRL_YEAR: HMSTD_CAP_VAL parsed, semantics RESOLVED (PX-20260826-04: capped value itself)",
          rows[0]["hmstd_cap_val"] == 350000)
    check("ACCOUNT_APPRL_YEAR: SPTD_CODE parsed (REV1, CONFIRMED classification input, col 47)",
          rows[0]["sptd_code"] == "A11")
    check("ACCOUNT_APPRL_YEAR: COUNTY_TAXABLE_VAL parsed (PX-20260826-04, CONFIRMED, PM-verified)",
          rows[0]["county_taxable_val"] == 340000)


def test_derive_value_mapping_cap_binding():
    """PX-20260826-04: HMSTD_CAP_VAL > 0 and < TOT_VAL -- cap present AND
    binding. assessed_value = HMSTD_CAP_VAL, hs_cap_loss = TOT_VAL - HMSTD_CAP_VAL
    (a real, nonzero loss amount)."""
    vm = dcad_format.derive_value_mapping(
        tot_val=400000, land_val=100000, impr_val=300000,
        hmstd_cap_val=350000, county_taxable_val=340000)
    check("derive_value_mapping (cap binding): market_value == TOT_VAL", vm["market_value"] == 400000)
    check("derive_value_mapping (cap binding): land_value == LAND_VAL", vm["land_value"] == 100000)
    check("derive_value_mapping (cap binding): imprv_value == IMPR_VAL", vm["imprv_value"] == 300000)
    check("derive_value_mapping (cap binding): assessed_value == HMSTD_CAP_VAL", vm["assessed_value"] == 350000)
    check("derive_value_mapping (cap binding): hs_cap_loss == TOT_VAL - HMSTD_CAP_VAL", vm["hs_cap_loss"] == 50000)
    check("derive_value_mapping (cap binding): taxable_value == COUNTY_TAXABLE_VAL", vm["taxable_value"] == 340000)


def test_derive_value_mapping_cap_present_not_binding():
    """PX-20260826-04: HMSTD_CAP_VAL > 0 and == TOT_VAL -- cap present but
    NOT currently binding. assessed_value == TOT_VAL (same number, via the
    HMSTD_CAP_VAL branch), hs_cap_loss == 0 (a real, meaningful zero -- NOT
    None/NULL, since a cap genuinely IS present here, per the PM's own
    ruling: 'else TOT when the cap isn't binding')."""
    vm = dcad_format.derive_value_mapping(
        tot_val=400000, land_val=100000, impr_val=300000,
        hmstd_cap_val=400000, county_taxable_val=400000)
    check("derive_value_mapping (cap present, not binding): assessed_value == TOT_VAL == HMSTD_CAP_VAL",
          vm["assessed_value"] == 400000)
    check("derive_value_mapping (cap present, not binding): hs_cap_loss == 0, not None",
          vm["hs_cap_loss"] == 0 and vm["hs_cap_loss"] is not None)


def test_derive_value_mapping_no_cap():
    """PX-20260826-04: HMSTD_CAP_VAL is 0 (or None) -- no cap at all.
    assessed_value falls back to TOT_VAL, hs_cap_loss is None (a genuinely
    absent value, distinct from the real 0 in the not-binding case above)."""
    vm_zero = dcad_format.derive_value_mapping(
        tot_val=900000, land_val=200000, impr_val=700000,
        hmstd_cap_val=0, county_taxable_val=900000)
    check("derive_value_mapping (no cap, HMSTD_CAP_VAL=0): assessed_value == TOT_VAL",
          vm_zero["assessed_value"] == 900000)
    check("derive_value_mapping (no cap, HMSTD_CAP_VAL=0): hs_cap_loss is None (absent, not 0)",
          vm_zero["hs_cap_loss"] is None)

    vm_none = dcad_format.derive_value_mapping(
        tot_val=900000, land_val=200000, impr_val=700000,
        hmstd_cap_val=None, county_taxable_val=900000)
    check("derive_value_mapping (no cap, HMSTD_CAP_VAL=None): assessed_value == TOT_VAL",
          vm_none["assessed_value"] == 900000)
    check("derive_value_mapping (no cap, HMSTD_CAP_VAL=None): hs_cap_loss is None",
          vm_none["hs_cap_loss"] is None)


def test_derive_parcel_class_fields():
    """PX-20260826-05 Task 2 (PM BLOCKER): the SPTD-derived class fields
    write_parcel() needs for `parcel` -- prop_type_cd is the raw SPTD_CODE
    passthrough, state_cd1 is DCAD_SPTD_CD_XREF_2011's own confirmed PTAD
    class code for that SPTD code."""
    a11 = dcad_format.derive_parcel_class_fields("A11")
    check("derive_parcel_class_fields: A11 -> prop_type_cd verbatim", a11["prop_type_cd"] == "A11")
    check("derive_parcel_class_fields: A11 -> state_cd1 'A' (DCAD_SPTD_CD_XREF_2011)", a11["state_cd1"] == "A")

    f10 = dcad_format.derive_parcel_class_fields("F10")
    check("derive_parcel_class_fields: F10 -> state_cd1 'F1'", f10["state_cd1"] == "F1")

    d10 = dcad_format.derive_parcel_class_fields("D10")
    check("derive_parcel_class_fields: D10 -> state_cd1 'D1'", d10["state_cd1"] == "D1")

    lower = dcad_format.derive_parcel_class_fields("a11")
    check("derive_parcel_class_fields: lowercase input still resolves (case-insensitive lookup)",
          lower["state_cd1"] == "A")

    none_case = dcad_format.derive_parcel_class_fields(None)
    check("derive_parcel_class_fields: None sptd_code -> state_cd1 None, prop_type_cd None, no raise",
          none_case["state_cd1"] is None and none_case["prop_type_cd"] is None)

    unknown = dcad_format.derive_parcel_class_fields("ZZ9")
    check("derive_parcel_class_fields: an SPTD code absent from DCAD_SPTD_CD_XREF_2011 degrades to state_cd1=None, not a raise",
          unknown["state_cd1"] is None and unknown["prop_type_cd"] == "ZZ9")


def test_build_unit_rows_carries_state_cd1():
    """PX-20260826-05 Task 2: build_unit_rows() must fold
    derive_parcel_class_fields()'s state_cd1 into each row, since
    write_parcel() reads it straight off unit_rows."""
    from loaders import load_dallas_certified as loader  # noqa

    account_info_recs = [r for r in dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines())
                          if r["skip_reason"] is None and not r["is_bpp"]]
    account_info_by_num = {r["account_num"]: r for r in account_info_recs}
    appr_recs = {r["account_num"]: r for r in dcad_format.iter_account_apprl_year_records(lines=ACCOUNT_APPRL_YEAR_CSV.splitlines())}
    unit_rows = loader.build_unit_rows(account_info_by_num, appr_recs, {}, 2026)
    by_geo = {r["geo_id"]: r for r in unit_rows}

    check("build_unit_rows: state_cd1 present and correct for an A11 (Residential) account",
          by_geo["00000123456789012"]["state_cd1"] == "A")
    check("build_unit_rows: state_cd1 correct for the F10 (Commercial) account",
          by_geo["00000123456789014"]["state_cd1"] == "F1")


class _FakeCursor:
    """Minimal DB-API cursor stub -- records every execute_batch-style call
    (via psycopg2.extras.execute_batch's own call into cur.executemany
    under the hood is bypassed here; this fixture directly monkeypatches
    psycopg2.extras.execute_batch itself, see test below, to avoid needing
    a real psycopg2 install in this sandbox)."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.commits += 1


def test_write_parcel_idempotent_on_rerun():
    """PX-20260826-05 Task 2 (PM ruling: 'fixtures for the parcel write +
    idempotency'). Real idempotency here comes from PARCEL_SQL's own
    ON CONFLICT (county_code, geo_id) DO UPDATE SET <col> = EXCLUDED.<col>
    for every written column (no COALESCE-preserving branch) -- this test
    proves that SQL shape is what's actually shipping, and that calling
    write_parcel() twice with the same unit_rows sends the IDENTICAL batch
    both times (same rows, same SQL, same count) -- exactly the property
    that, combined with a real Postgres ON CONFLICT DO UPDATE, guarantees
    re-running this loader against the same archive converges `parcel` to
    the same values rather than accumulating duplicates or drifting.

    No live DB in this sandbox (no psycopg2 installed) -- psycopg2.extras
    is monkeypatched with a fake execute_batch that just records its
    arguments, same class of mock this session has used for the
    write-time prop_id/geo_id guard's own DB-facing tests.
    """
    import sys as _sys
    import types as _types
    from loaders import load_dallas_certified as loader  # noqa

    calls = []

    def _fake_execute_batch(cur, sql, rows, page_size=None):
        calls.append((sql, list(rows)))

    fake_psycopg2_extras = _types.ModuleType("psycopg2.extras")
    fake_psycopg2_extras.execute_batch = _fake_execute_batch
    fake_psycopg2 = _types.ModuleType("psycopg2")
    fake_psycopg2.extras = fake_psycopg2_extras

    real_psycopg2 = _sys.modules.get("psycopg2")
    real_psycopg2_extras = _sys.modules.get("psycopg2.extras")
    _sys.modules["psycopg2"] = fake_psycopg2
    _sys.modules["psycopg2.extras"] = fake_psycopg2_extras
    try:
        unit_rows = [
            {"county_code": "DALLAS", "geo_id": "00000123456789012", "prop_id": 123456789012,
             "prop_type_cd": "A11", "state_cd1": "A", "owner_name": "SMITH JOHN",
             "situs_address": "123 MAIN ST", "zip_code": "75201"},
            {"county_code": "DALLAS", "geo_id": "00000123456789014", "prop_id": 123456789014,
             "prop_type_cd": "F10", "state_cd1": "F1", "owner_name": "ACME LLC",
             "situs_address": "789 COMMERCE ST", "zip_code": "75203"},
        ]
        conn = _FakeConn()

        n1 = loader.write_parcel(conn, unit_rows, dry_run=False)
        n2 = loader.write_parcel(conn, unit_rows, dry_run=False)

        check("write_parcel: returns len(unit_rows) on first call", n1 == 2)
        check("write_parcel: returns the SAME count on a second, identical call (idempotent count)", n2 == 2)
        check("write_parcel: exactly 2 execute_batch calls total (one per write_parcel() call, single-batch since < BATCH_SIZE)",
              len(calls) == 2)
        check("write_parcel: both calls send the IDENTICAL SQL text", calls[0][0] == calls[1][0])
        check("write_parcel: both calls send the IDENTICAL row batch", calls[0][1] == calls[1][1])
        check("write_parcel: PARCEL_SQL targets the real (county_code, geo_id) conflict key",
              "ON CONFLICT (county_code, geo_id) DO UPDATE" in loader.PARCEL_SQL)
        check("write_parcel: PARCEL_SQL overwrites (EXCLUDED), not COALESCE-preserves -- this loader IS "
              "the authoritative account-layer source for a first Dallas load",
              "COALESCE" not in loader.PARCEL_SQL and "EXCLUDED.prop_type_cd" in loader.PARCEL_SQL)
        check("write_parcel: row tuple column order matches PARCEL_SQL's own column list "
              "(county_code, geo_id, prop_id, prop_type_cd, state_cd1, owner_name, situs_address, "
              "zip_code -- PX-20260827-06 adds zip_code as the 8th column)",
              calls[0][1][0] == ("DALLAS", "00000123456789012", 123456789012, "A11", "A",
                                  "SMITH JOHN", "123 MAIN ST", "75201"))
    finally:
        if real_psycopg2 is not None:
            _sys.modules["psycopg2"] = real_psycopg2
        else:
            _sys.modules.pop("psycopg2", None)
        if real_psycopg2_extras is not None:
            _sys.modules["psycopg2.extras"] = real_psycopg2_extras
        else:
            _sys.modules.pop("psycopg2.extras", None)


def test_write_parcel_dry_run_zero_db_access():
    """dry_run=True must return a count with conn=None and zero DB access --
    same convention as write_prop_unit_and_tax_year()."""
    from loaders import load_dallas_certified as loader  # noqa

    unit_rows = [
        {"county_code": "DALLAS", "geo_id": "00000123456789012", "prop_id": 123456789012,
         "prop_type_cd": "A11", "state_cd1": "A", "owner_name": "SMITH JOHN",
         "situs_address": "123 MAIN ST"},
    ]
    n = loader.write_parcel(conn=None, unit_rows=unit_rows, dry_run=True)
    check("write_parcel: dry_run=True returns len(unit_rows) with conn=None, no raise", n == 1)


def test_classify_account_sptd_wiring():
    """PX-20260826-04 Task 1: verify classify_account_sptd()'s real wiring
    into classification_map_dallas.classify_dallas_sptb_code() against
    real-shaped SPTD values (A11, F10) named explicitly in the brief."""
    check("classify_account_sptd: A11 -> Residential", dcad_format.classify_account_sptd("A11") == "Residential")
    check("classify_account_sptd: F10 -> Commercial", dcad_format.classify_account_sptd("F10") == "Commercial")
    check("classify_account_sptd: None -> unmapped sentinel, never raises",
          dcad_format.classify_account_sptd(None) is not None)


def test_header_drift_fails_loud_on_missing_confirmed_column():
    """PX-20260826-04 Task 1: a real CSV missing one of the CONFIRMED,
    logic-critical columns must raise HeaderDriftError immediately, not
    silently return None for every row. Missing COUNTY_TAXABLE_VAL here
    (the newest CONFIRMED column) as the concrete drift case."""
    bad_csv = ("ACCOUNT_NUM,APPRAISAL_YR,IMPR_VAL,LAND_VAL,TOT_VAL,HMSTD_CAP_VAL,SPTD_CODE\n"
               "00000123456789012,2026,300000,100000,400000,350000,A11\n")
    raised = False
    try:
        list(dcad_format.iter_account_apprl_year_records(lines=bad_csv.splitlines()))
    except dcad_format.HeaderDriftError:
        raised = True
    check("HeaderDriftError: raised when ACCOUNT_APPRL_YEAR header is missing COUNTY_TAXABLE_VAL", raised)

    # A table with no registered EXPECTED_HEADERS entry (e.g. a deliberately-
    # unloaded table) must NOT raise -- validate_header() is a no-op there.
    no_raise = True
    try:
        dcad_format.validate_header("LAND", ["WHATEVER_COLUMNS"])
    except dcad_format.HeaderDriftError:
        no_raise = False
    check("HeaderDriftError: validate_header() is a silent no-op for tables with no registered expectation",
          no_raise)


def test_exemption_codes_aggregation():
    rows = list(dcad_format.iter_applied_std_exempt_records(lines=APPLIED_STD_EXEMPT_CSV.splitlines()))
    by_account = {}
    for r in rows:
        by_account.setdefault(r["account_num"], []).append(r)
    codes_012 = dcad_format.exemption_codes_for_account(by_account["00000123456789012"])
    codes_013 = dcad_format.exemption_codes_for_account(by_account["00000123456789013"])
    check("Exemption codes: account ...012 gets sorted 'HS,OV65'", codes_012 == "HS,OV65")
    check("Exemption codes: account ...013 gets 'HS'", codes_013 == "HS")
    check("Exemption codes: account with no rows gets None", dcad_format.exemption_codes_for_account([]) is None)


def test_g1_style_ledger_conservation():
    ledger = dcad_format.scan_table_ledger(
        "ACCOUNT_INFO", dcad_format.iter_account_info_records, lines=ACCOUNT_INFO_CSV.splitlines()
    )
    bucket_sum = sum(ledger["buckets"].values())
    check("G1-style ledger: bucket sum == total lines (conservation identity)",
          bucket_sum == ledger["total_lines"] == 6)
    check("G1-style ledger: PX-20260826-04 finding #2 -- 4 accepted (incl. the alphanumeric account), 1 bpp_excluded (named bucket, not silent), 1 no_account_num",
          ledger["buckets"].get("accepted") == 4
          and ledger["buckets"].get("bpp_excluded") == 1
          and ledger["buckets"].get("no_account_num") == 1)


def test_table_load_policy_covers_all_14_tables():
    """REV1: TABLE_LOAD_POLICY (generalized from EXEMPTION_TABLE_POLICY)
    must account for every real table in the canonical hash list -- no
    silent gaps in the 'which tables matter' accounting."""
    missing = set(dcad_format.TABLE_FILENAMES) - set(dcad_format.TABLE_LOAD_POLICY)
    check("TABLE_LOAD_POLICY: every one of the 14 real tables has an entry", not missing)
    check("TABLE_LOAD_POLICY: TAXABLE_OBJECT is documented deliberately-unloaded (rev1)",
          "UNLOADED" in dcad_format.TABLE_LOAD_POLICY["TAXABLE_OBJECT"])


def test_build_unit_rows_uses_confirmed_prop_id_geo_id_derivation():
    from loaders import load_dallas_certified as loader  # noqa

    account_info_recs = [r for r in dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines())
                          if r["skip_reason"] is None and not r["is_bpp"]]
    account_info_by_num = {r["account_num"]: r for r in account_info_recs}

    appr_recs = {r["account_num"]: r for r in dcad_format.iter_account_apprl_year_records(lines=ACCOUNT_APPRL_YEAR_CSV.splitlines())}

    exempt_recs = list(dcad_format.iter_applied_std_exempt_records(lines=APPLIED_STD_EXEMPT_CSV.splitlines()))
    exempt_by_account = {}
    for r in exempt_recs:
        exempt_by_account.setdefault(r["account_num"], []).append(r)

    unit_rows = loader.build_unit_rows(account_info_by_num, appr_recs, exempt_by_account, 2026)
    by_geo = {r["geo_id"]: r for r in unit_rows}

    check("build_unit_rows: geo_id == ACCOUNT_NUM verbatim (fits VARCHAR(20) with room to spare)",
          by_geo["00000123456789012"]["geo_id"] == "00000123456789012")
    check("build_unit_rows: PRE-COMMIT FIX -- tax_year == the run's own --year (APPRAISAL_YR asserted equal, no fallback)",
          by_geo["00000123456789012"]["tax_year"] == 2026)
    check("build_unit_rows: prop_id == int(ACCOUNT_NUM) for an all-digit account (REV1, no TAXABLE_OBJECT join)",
          by_geo["00000123456789012"]["prop_id"] == 123456789012)
    check("build_unit_rows: PX-20260826-04 finding #2 -- an alphanumeric account is present and gets a valid, disjoint hashed prop_id",
          "0000012A456789016" in by_geo
          and by_geo["0000012A456789016"]["prop_id"] >= (1 << 62)
          and by_geo["0000012A456789016"]["prop_id"] < (1 << 63))
    check("build_unit_rows: market_value == TOT_VAL", by_geo["00000123456789012"]["market_value"] == 400000)
    check("build_unit_rows: exemption_codes aggregated correctly", by_geo["00000123456789012"]["exemption_codes"] == "HS,OV65")
    check("build_unit_rows: PX-20260826-04 -- assessed_value resolved via derive_value_mapping (cap binding: HMSTD_CAP_VAL)",
          by_geo["00000123456789012"]["assessed_value"] == 350000)
    check("build_unit_rows: PX-20260826-04 -- hs_cap_loss == TOT_VAL - HMSTD_CAP_VAL",
          by_geo["00000123456789012"]["hs_cap_loss"] == 50000)
    check("build_unit_rows: PX-20260826-04 -- taxable_value == COUNTY_TAXABLE_VAL",
          by_geo["00000123456789012"]["taxable_value"] == 340000)
    check("build_unit_rows: PX-20260826-04 -- account ...013 has no cap (HMSTD_CAP_VAL=0): assessed_value==TOT_VAL, hs_cap_loss is None",
          by_geo["00000123456789013"]["assessed_value"] == 280000 and by_geo["00000123456789013"]["hs_cap_loss"] is None)
    check("build_unit_rows: sptd_code carried through for classification (REV1)",
          by_geo["00000123456789012"]["sptd_code"] == "A11")
    check("build_unit_rows: PX-20260826-04 -- prop_type_cd carries the raw SPTD_CODE (mirrors Travis's raw-code convention)",
          by_geo["00000123456789012"]["prop_type_cd"] == "A11")
    check("build_unit_rows: PX-20260826-04 -- benchmark_label computed via classify_account_sptd (A11 -> Residential)",
          by_geo["00000123456789012"]["benchmark_label"] == "Residential")
    check("build_unit_rows: BPP-excluded account never appears", "00000123456789015" not in by_geo)
    check("build_unit_rows: no 'taxable_object' key anywhere in output (rev1 removes the join entirely)",
          all("object_id" not in r and "taxable_object" not in r for r in unit_rows))

    # PX-20260827-06: owner_name/situs_address/zip_code/owner_suppressed
    # must survive build_unit_rows()'s own passthrough (info.get(...)) end
    # to end -- this is the same real fix proven at the iter_account_info_
    # records() layer above, now proven all the way through the function
    # write_parcel()/write_prop_unit_and_tax_year() actually read from.
    check("build_unit_rows: PX-20260827-06 -- owner_name carried through end-to-end",
          by_geo["00000123456789012"]["owner_name"] == "SMITH JOHN")
    check("build_unit_rows: PX-20260827-06 -- situs_address carried through end-to-end",
          by_geo["00000123456789012"]["situs_address"] == "123 MAIN ST")
    check("build_unit_rows: PX-20260827-06 -- zip_code carried through end-to-end",
          by_geo["00000123456789012"]["zip_code"] == "75201")
    check("build_unit_rows: PX-20260827-06 -- owner_suppressed carried through end-to-end (False here, EXCLUDE_OWNER=N)",
          by_geo["00000123456789012"]["owner_suppressed"] is False)


# ══════════════════════════════════════════════════════════════════════
# PX-20260827-06: dedicated fixtures for the situs/owner backfill + the
# new G3_FIELD_COVERAGE gate. Kept as their OWN small CSVs (not folded
# into the shared ACCOUNT_INFO_CSV/ACCOUNT_APPRL_YEAR_CSV pair above) --
# adding more accounts to that shared pair would require matching
# ACCOUNT_APPRL_YEAR_CSV rows too (build_unit_rows() fails loud on any
# account with no APPRAISAL_YR match, by design), which would bloat every
# other test that reuses those two fixtures for something unrelated.
# ══════════════════════════════════════════════════════════════════════

EXCLUDE_OWNER_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,STREET_NUM,STREET_HALF_NUM,FULL_STREET_NAME,BLDG_ID,UNIT_ID,OWNER_NAME1,OWNER_NAME2,EXCLUDE_OWNER,PROPERTY_ZIPCODE
00000200000000001,RES,GISPA01,100,,ALPHA ST,,,YES OWNER,,Y,75001
00000200000000002,RES,GISPA02,200,,BETA ST,,,NO OWNER,,N,75002
00000200000000003,RES,GISPA03,300,,GAMMA ST,,,BLANK OWNER,,,75003
00000200000000004,RES,GISPA04,400,,DELTA ST,,,ONE OWNER,,1,75004
00000200000000005,RES,GISPA05,500,,EPSILON ST,,,ZERO OWNER,,0,75005
"""


def test_exclude_owner_suppression_conservative_encoding():
    """PX-20260827-06 item 2: EXCLUDE_OWNER's real distinct values are
    UNCONFIRMED in this sandbox (no vault access -- see this file's own
    top-level disclosure and the PX-20260827-06 report). is_owner_excluded()
    is deliberately conservative: 'Y'/'1' (and anything else not
    recognizably falsy) suppress owner_name; 'N'/'0'/blank do not."""
    rows = {r["account_num"]: r for r in
            dcad_format.iter_account_info_records(lines=EXCLUDE_OWNER_CSV.splitlines())}

    check("EXCLUDE_OWNER='Y': owner_name suppressed to None, owner_suppressed=True",
          rows["00000200000000001"]["owner_name"] is None
          and rows["00000200000000001"]["owner_suppressed"] is True)
    check("EXCLUDE_OWNER='N': owner_name NOT suppressed",
          rows["00000200000000002"]["owner_name"] == "NO OWNER"
          and rows["00000200000000002"]["owner_suppressed"] is False)
    check("EXCLUDE_OWNER='' (blank): owner_name NOT suppressed (blank treated as falsy)",
          rows["00000200000000003"]["owner_name"] == "BLANK OWNER"
          and rows["00000200000000003"]["owner_suppressed"] is False)
    check("EXCLUDE_OWNER='1': owner_name suppressed (conservative -- unrecognized-but-not-falsy treated as excluded)",
          rows["00000200000000004"]["owner_name"] is None
          and rows["00000200000000004"]["owner_suppressed"] is True)
    check("EXCLUDE_OWNER='0': owner_name NOT suppressed",
          rows["00000200000000005"]["owner_name"] == "ZERO OWNER"
          and rows["00000200000000005"]["owner_suppressed"] is False)
    # situs_address must be entirely unaffected by owner suppression --
    # these are independent fields (Sec. 25.025 covers the OWNER's
    # identity, not the property's situs).
    check("EXCLUDE_OWNER='Y' row still gets a real situs_address (suppression is owner-only)",
          rows["00000200000000001"]["situs_address"] == "100 ALPHA ST")


BLANK_SITUS_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,STREET_NUM,STREET_HALF_NUM,FULL_STREET_NAME,BLDG_ID,UNIT_ID,OWNER_NAME1,OWNER_NAME2,EXCLUDE_OWNER,PROPERTY_ZIPCODE
00000300000000001,RES,GISPB01,,,,,,SOME OWNER,,N,
"""


def test_situs_address_none_when_all_components_blank():
    """A row with all 5 situs components blank must get situs_address=None
    (not an empty string, not a string of stray spaces) -- this is exactly
    the shape the G3_FIELD_COVERAGE gate's denominator has to handle
    correctly (a falsy/None situs_address, not a truthy blank one that
    would silently inflate the coverage percentage)."""
    rows = list(dcad_format.iter_account_info_records(lines=BLANK_SITUS_CSV.splitlines()))
    check("All-blank situs components -> situs_address is None, not '' or whitespace",
          rows[0]["situs_address"] is None)
    check("PROPERTY_ZIPCODE blank -> zip_code is None",
          rows[0]["zip_code"] is None)


NO_ZIPCODE_COLUMN_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,STREET_NUM,STREET_HALF_NUM,FULL_STREET_NAME,BLDG_ID,UNIT_ID,OWNER_NAME1,OWNER_NAME2,EXCLUDE_OWNER
00000400000000001,RES,GISPC01,600,,ZETA ST,,,ZETA OWNER,,N
"""


def test_property_zipcode_is_soft_optional_column():
    """PROPERTY_ZIPCODE is deliberately NOT in EXPECTED_HEADERS (soft/
    optional, per PROPERTY_ZIPCODE_FIELD's own comment) -- a real export
    that omits the column entirely must degrade to zip_code=None, not
    raise HeaderDriftError. Distinct from the hard-validated columns
    (STREET_NUM/FULL_STREET_NAME/OWNER_NAME1/EXCLUDE_OWNER), which DO
    raise on omission -- see test_account_info_header_drift_* below."""
    raised = False
    rows = []
    try:
        rows = list(dcad_format.iter_account_info_records(lines=NO_ZIPCODE_COLUMN_CSV.splitlines()))
    except dcad_format.HeaderDriftError:
        raised = True
    check("PROPERTY_ZIPCODE column entirely absent: does NOT raise HeaderDriftError (soft/optional)",
          not raised)
    check("PROPERTY_ZIPCODE column entirely absent: zip_code degrades to None, situs/owner unaffected",
          rows and rows[0]["zip_code"] is None and rows[0]["situs_address"] == "600 ZETA ST"
          and rows[0]["owner_name"] == "ZETA OWNER")


def test_account_info_header_drift_on_missing_confirmed_situs_owner_column():
    """PX-20260827-06: STREET_NUM/FULL_STREET_NAME/OWNER_NAME1/EXCLUDE_OWNER
    are now hard-validated (promoted from soft/UNCONFIRMED once the PM's
    brief confirmed these real field names -- same treatment APPRAISAL_YR
    got once IT was confirmed). A real ACCOUNT_INFO export missing any one
    of them must fail loud immediately, not silently return None for every
    row's situs_address/owner_name forever -- exactly the failure mode
    this whole brief exists to close off."""
    missing_full_street_name = ("ACCOUNT_NUM,DIVISION_CD,STREET_NUM,OWNER_NAME1,EXCLUDE_OWNER\n"
                                 "00000123456789012,RES,123,SMITH JOHN,N\n")
    raised = False
    try:
        list(dcad_format.iter_account_info_records(lines=missing_full_street_name.splitlines()))
    except dcad_format.HeaderDriftError:
        raised = True
    check("HeaderDriftError: raised when ACCOUNT_INFO header is missing FULL_STREET_NAME",
          raised)

    missing_exclude_owner = ("ACCOUNT_NUM,DIVISION_CD,STREET_NUM,FULL_STREET_NAME,OWNER_NAME1\n"
                              "00000123456789012,RES,123,MAIN ST,SMITH JOHN\n")
    raised2 = False
    try:
        list(dcad_format.iter_account_info_records(lines=missing_exclude_owner.splitlines()))
    except dcad_format.HeaderDriftError:
        raised2 = True
    check("HeaderDriftError: raised when ACCOUNT_INFO header is missing EXCLUDE_OWNER "
          "(the Sec. 25.025 suppression flag itself -- must never silently degrade)",
          raised2)


def test_is_owner_excluded_pure_function():
    """Direct unit coverage of is_owner_excluded()'s own truth table,
    independent of CSV parsing."""
    check("is_owner_excluded('Y') -> True", dcad_format.is_owner_excluded("Y") is True)
    check("is_owner_excluded('y') -> True (case-insensitive)", dcad_format.is_owner_excluded("y") is True)
    check("is_owner_excluded('N') -> False", dcad_format.is_owner_excluded("N") is False)
    check("is_owner_excluded('') -> False", dcad_format.is_owner_excluded("") is False)
    check("is_owner_excluded(None) -> False", dcad_format.is_owner_excluded(None) is False)
    check("is_owner_excluded('  ') -> False (whitespace-only treated as blank)", dcad_format.is_owner_excluded("   ") is False)
    check("is_owner_excluded('TRUE') -> True (unrecognized-but-not-falsy -> conservative exclude)",
          dcad_format.is_owner_excluded("TRUE") is True)


def test_join_nonempty_pure_function():
    """Direct unit coverage of _join_nonempty()'s own behavior."""
    check("_join_nonempty full join", dcad_format._join_nonempty("123", "1/2", "MAIN ST", "BLDG A", "UNIT 1") == "123 1/2 MAIN ST BLDG A UNIT 1")
    check("_join_nonempty skips blanks/None", dcad_format._join_nonempty("123", "", "MAIN ST", None, "") == "123 MAIN ST")
    check("_join_nonempty all blank -> ''", dcad_format._join_nonempty("", None, "   ") == "")


# ── G3_FIELD_COVERAGE gate fixtures ──────────────────────────────────────

def _make_unit_row(geo_id, situs_address, owner_name, owner_suppressed=False):
    return {"geo_id": geo_id, "situs_address": situs_address, "owner_name": owner_name,
            "owner_suppressed": owner_suppressed}


def test_compute_field_coverage_pass_case():
    from loaders import load_dallas_certified as loader  # noqa

    unit_rows = [_make_unit_row(f"G{i}", "100 MAIN ST", "SOME OWNER") for i in range(100)]
    coverage = loader.compute_field_coverage(unit_rows)
    check("compute_field_coverage: 100% situs/owner coverage computed correctly",
          coverage["situs_pct"] == 100.0 and coverage["owner_pct"] == 100.0)
    check("compute_field_coverage: n_rows correct", coverage["n_rows"] == 100)
    check("compute_field_coverage: owner_suppressed count is 0 when none suppressed", coverage["owner_suppressed"] == 0)


def test_compute_field_coverage_fail_on_situs_below_threshold():
    from loaders import load_dallas_certified as loader  # noqa

    # 90 rows with situs, 10 without -- 90% < 99% threshold.
    unit_rows = ([_make_unit_row(f"G{i}", "100 MAIN ST", "OWNER") for i in range(90)]
                 + [_make_unit_row(f"H{i}", None, "OWNER") for i in range(10)])
    coverage = loader.compute_field_coverage(unit_rows)
    g3_passed = (coverage["situs_pct"] >= loader.G3_SITUS_MIN_PCT
                 and coverage["owner_pct"] >= loader.G3_OWNER_MIN_PCT)
    check("compute_field_coverage: situs_pct == 90.0 for 90/100 non-empty", coverage["situs_pct"] == 90.0)
    check("G3_FIELD_COVERAGE: FAILS when situs coverage (90%) is below the 99% threshold", not g3_passed)


def test_compute_field_coverage_pass_with_legitimate_owner_suppression():
    """PX-20260827-06 item 5's own explicit reasoning: the owner threshold
    (95%, lower than situs's 99%) exists precisely so that legitimate
    EXCLUDE_OWNER suppressions don't false-positive a gate failure. 6% of
    rows deliberately suppressed (owner_suppressed=True, owner_name=None)
    should still PASS the 95% floor (94% non-empty is NOT below 95%...
    use a case that's comfortably inside the margin to avoid float-
    rounding ambiguity at the exact boundary)."""
    from loaders import load_dallas_certified as loader  # noqa

    unit_rows = ([_make_unit_row(f"G{i}", "100 MAIN ST", "OWNER") for i in range(97)]
                 + [_make_unit_row(f"S{i}", "100 MAIN ST", None, owner_suppressed=True) for i in range(3)])
    coverage = loader.compute_field_coverage(unit_rows)
    g3_passed = (coverage["situs_pct"] >= loader.G3_SITUS_MIN_PCT
                 and coverage["owner_pct"] >= loader.G3_OWNER_MIN_PCT)
    check("compute_field_coverage: owner_suppressed count == 3", coverage["owner_suppressed"] == 3)
    check("compute_field_coverage: owner_pct == 97.0 (97/100 non-empty, 3 legitimately suppressed)",
          coverage["owner_pct"] == 97.0)
    check("G3_FIELD_COVERAGE: PASSES at 97% owner coverage (>= 95% floor) despite 3 real EXCLUDE_OWNER suppressions",
          g3_passed)


def test_compute_field_coverage_empty_unit_rows_no_crash():
    from loaders import load_dallas_certified as loader  # noqa

    coverage = loader.compute_field_coverage([])
    check("compute_field_coverage: empty unit_rows list does not raise (ZeroDivisionError guard)",
          coverage["n_rows"] == 0 and coverage["situs_pct"] == 100.0 and coverage["owner_pct"] == 100.0)


def test_run_dallas_ingest_gate_includes_g3_check():
    """End-to-end: run_dallas_ingest_gate() must include a G3_FIELD_COVERAGE
    check whose detail string reports BOTH percentages (brief item 6:
    'Dry-run must print both coverage percentages') alongside the existing
    G1/G2 checks, in dry_run mode (conn=None, no DB access)."""
    from loaders import load_dallas_certified as loader  # noqa

    ledgers = {
        "ACCOUNT_INFO": {"total_lines": 2, "buckets": {"accepted": 2}},
    }
    unit_rows = [
        _make_unit_row("G1", "100 MAIN ST", "OWNER ONE"),
        _make_unit_row("G2", None, None, owner_suppressed=True),
    ]
    summary = loader.run_dallas_ingest_gate(
        conn=None, ledgers=ledgers, orphans=set(), unit_rows=unit_rows,
        tax_year=2026, county_code="DALLAS", dry_run=True)

    check("run_dallas_ingest_gate: G3_FIELD_COVERAGE key present in checks", "G3_FIELD_COVERAGE" in summary["checks"])
    g3_passed, g3_detail = summary["checks"]["G3_FIELD_COVERAGE"]
    check("run_dallas_ingest_gate: G3_FIELD_COVERAGE detail string reports situs_address percentage",
          "situs_address" in g3_detail and "%" in g3_detail)
    check("run_dallas_ingest_gate: G3_FIELD_COVERAGE detail string reports owner_name percentage",
          "owner_name" in g3_detail and g3_detail.count("%") >= 2)
    # 1/2 situs non-empty = 50% < 99%, 1/2 owner non-empty = 50% < 95% -- must fail.
    check("run_dallas_ingest_gate: G3_FIELD_COVERAGE correctly FAILS on this 50%-coverage synthetic run",
          g3_passed is False)
    check("run_dallas_ingest_gate: overall gate 'passed' reflects the G3 failure (not masked by G1/G2 passing)",
          summary["passed"] is False)


if __name__ == "__main__":
    test_account_info_basic_parse_and_bpp_flag()
    test_bpp_exclusion_filters_at_loader_boundary()
    test_derive_prop_id_geo_id_confirmed_mapping()
    test_derive_prop_id_geo_id_alphanumeric_hashed()
    test_derive_prop_id_geo_id_still_fails_loud_on_none_and_blank()
    test_check_in_run_prop_id_collisions_fires_on_forced_collision()
    test_find_prop_id_geo_id_conflicts()
    test_iter_account_apprl_year_reads_real_appraisal_yr_column()
    test_validate_appraisal_year_fail_loud_on_mismatch()
    test_build_unit_rows_fails_loud_on_appraisal_year_mismatch()
    test_orphan_account_classified_not_hidden()
    test_account_apprl_year_confirmed_value_mapping_and_sptd_code()
    test_derive_value_mapping_cap_binding()
    test_derive_value_mapping_cap_present_not_binding()
    test_derive_value_mapping_no_cap()
    test_derive_parcel_class_fields()
    test_build_unit_rows_carries_state_cd1()
    test_write_parcel_idempotent_on_rerun()
    test_write_parcel_dry_run_zero_db_access()
    test_classify_account_sptd_wiring()
    test_header_drift_fails_loud_on_missing_confirmed_column()
    test_exemption_codes_aggregation()
    test_g1_style_ledger_conservation()
    test_table_load_policy_covers_all_14_tables()
    test_build_unit_rows_uses_confirmed_prop_id_geo_id_derivation()

    # PX-20260827-06
    test_exclude_owner_suppression_conservative_encoding()
    test_situs_address_none_when_all_components_blank()
    test_property_zipcode_is_soft_optional_column()
    test_account_info_header_drift_on_missing_confirmed_situs_owner_column()
    test_is_owner_excluded_pure_function()
    test_join_nonempty_pure_function()
    test_compute_field_coverage_pass_case()
    test_compute_field_coverage_fail_on_situs_below_threshold()
    test_compute_field_coverage_pass_with_legitimate_owner_suppression()
    test_compute_field_coverage_empty_unit_rows_no_crash()
    test_run_dallas_ingest_gate_includes_g3_check()

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    if FAIL_COUNT == 0:
        print("ALL DCAD_FORMAT FIXTURE TESTS PASSED")
    else:
        sys.exit(1)
