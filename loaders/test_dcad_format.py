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
ACCOUNT_INFO_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,OWNER_NAME,SITUS_ADDR
00000123456789012,RES,GISP001,SMITH JOHN,123 MAIN ST
00000123456789013,RES,GISP002,DOE JANE,456 OAK ST
00000123456789014,COM,GISP003,ACME LLC,789 COMMERCE ST
00000123456789015,BPP,,BIZCO INC,
0000012A456789016,RES,GISP005,BLOCK-UNIT OWNER,321 BLOCK-UNIT AVE
,RES,GISP004,BLANK ACCOUNT,999 NOWHERE
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
    test_classify_account_sptd_wiring()
    test_header_drift_fails_loud_on_missing_confirmed_column()
    test_exemption_codes_aggregation()
    test_g1_style_ledger_conservation()
    test_table_load_policy_covers_all_14_tables()
    test_build_unit_rows_uses_confirmed_prop_id_geo_id_derivation()

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    if FAIL_COUNT == 0:
        print("ALL DCAD_FORMAT FIXTURE TESTS PASSED")
    else:
        sys.exit(1)
