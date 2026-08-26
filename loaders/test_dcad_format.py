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


# ── Constructed ACCOUNT_INFO sample: 3 real accounts + 1 BPP + 1 blank ────
ACCOUNT_INFO_CSV = """ACCOUNT_NUM,DIVISION_CD,GIS_PARCEL_ID,OWNER_NAME,SITUS_ADDR
00000123456789012,RES,GISP001,SMITH JOHN,123 MAIN ST
00000123456789013,RES,GISP002,DOE JANE,456 OAK ST
00000123456789014,COM,GISP003,ACME LLC,789 COMMERCE ST
00000123456789015,BPP,,BIZCO INC,
,RES,GISP004,BLANK ACCOUNT,999 NOWHERE
"""

# ── Constructed ACCOUNT_APPRL_YEAR sample -- REV1: includes SPTD_CODE
# (column 47, CONFIRMED classification input) alongside the confirmed
# value columns. ───────────────────────────────────────────────────────
ACCOUNT_APPRL_YEAR_CSV = """ACCOUNT_NUM,TAX_YR,IMPR_VAL,LAND_VAL,TOT_VAL,HMSTD_CAP_VAL,SPTD_CODE
00000123456789012,2026,300000,100000,400000,50000,A11
00000123456789013,2026,200000,80000,280000,0,A11
"""

APPLIED_STD_EXEMPT_CSV = """ACCOUNT_NUM,EXEMPT_CODE
00000123456789012,HS
00000123456789012,OV65
00000123456789013,HS
00000999999999999,HS
"""


def test_account_info_basic_parse_and_bpp_flag():
    rows = list(dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines()))
    check("ACCOUNT_INFO: 5 rows read", len(rows) == 5)
    check("ACCOUNT_INFO: RES account not flagged BPP", rows[0]["is_bpp"] is False)
    check("ACCOUNT_INFO: COM account not flagged BPP", rows[2]["division_cd"] == "COM" and rows[2]["is_bpp"] is False)
    check("ACCOUNT_INFO: BPP account IS flagged BPP", rows[3]["is_bpp"] is True)
    check("ACCOUNT_INFO: blank account_num gets skip_reason", rows[4]["skip_reason"] == "no_account_num")
    check("ACCOUNT_INFO: normal rows have skip_reason None", rows[0]["skip_reason"] is None)


def test_bpp_exclusion_filters_at_loader_boundary():
    from loaders import load_dallas_certified as loader  # noqa

    records = list(dcad_format.iter_account_info_records(lines=ACCOUNT_INFO_CSV.splitlines()))
    kept, excluded = loader.filter_bpp_accounts(records)
    check("BPP filter: 1 excluded (BPP account)", excluded == 1)
    check("BPP filter: blank-account_num row also excluded (skip_reason)", "00000123456789015" not in kept)
    check("BPP filter: 3 real accounts kept (2 RES + 1 COM)", len(kept) == 3)
    check("BPP filter: excluded account not in kept dict", "00000123456789015" not in kept)


def test_derive_prop_id_geo_id_confirmed_mapping():
    """REV1: prop_id = int(ACCOUNT_NUM), geo_id = ACCOUNT_NUM verbatim -- CONFIRMED."""
    prop_id, geo_id = dcad_format.derive_prop_id_geo_id("00000123456789012")
    check("derive_prop_id_geo_id: prop_id is int(ACCOUNT_NUM)", prop_id == 123456789012)
    check("derive_prop_id_geo_id: geo_id is ACCOUNT_NUM verbatim (string, leading zeros preserved)",
          geo_id == "00000123456789012")
    check("derive_prop_id_geo_id: geo_id fits VARCHAR(20) with room to spare", len(geo_id) <= 20)


def test_derive_prop_id_geo_id_fails_loud_on_non_digit():
    """REV1: fail-loud ValueError on a non-numeric ACCOUNT_NUM -- an identity-
    integrity check, not a soft per-row skip."""
    raised = False
    try:
        dcad_format.derive_prop_id_geo_id("ABC123456789012XY")
    except ValueError:
        raised = True
    check("derive_prop_id_geo_id: raises ValueError on non-digit ACCOUNT_NUM (fail-loud)", raised)

    raised_on_none = False
    try:
        dcad_format.derive_prop_id_geo_id(None)
    except ValueError:
        raised_on_none = True
    check("derive_prop_id_geo_id: raises ValueError on None (fail-loud)", raised_on_none)


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
    check("ACCOUNT_APPRL_YEAR: 2 rows read", len(rows) == 2)
    check("ACCOUNT_APPRL_YEAR: TOT_VAL parsed as int", rows[0]["tot_val"] == 400000)
    check("ACCOUNT_APPRL_YEAR: LAND_VAL parsed as int", rows[0]["land_val"] == 100000)
    check("ACCOUNT_APPRL_YEAR: IMPR_VAL parsed as int", rows[0]["impr_val"] == 300000)
    check("ACCOUNT_APPRL_YEAR: LAND_VAL + IMPR_VAL == TOT_VAL (internal consistency of the constructed fixture)",
          rows[0]["land_val"] + rows[0]["impr_val"] == rows[0]["tot_val"])
    check("ACCOUNT_APPRL_YEAR: HMSTD_CAP_VAL parsed but semantics UNRESOLVED (see docstring)",
          rows[0]["hmstd_cap_val"] == 50000)
    check("ACCOUNT_APPRL_YEAR: SPTD_CODE parsed (REV1, CONFIRMED classification input, col 47)",
          rows[0]["sptd_code"] == "A11")


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
          bucket_sum == ledger["total_lines"] == 5)
    check("G1-style ledger: 4 accepted, 1 no_account_num",
          ledger["buckets"].get("accepted") == 4 and ledger["buckets"].get("no_account_num") == 1)


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

    unit_rows = loader.build_unit_rows(account_info_by_num, appr_recs, exempt_by_account)
    by_geo = {r["geo_id"]: r for r in unit_rows}

    check("build_unit_rows: geo_id == ACCOUNT_NUM verbatim (fits VARCHAR(20) with room to spare)",
          by_geo["00000123456789012"]["geo_id"] == "00000123456789012")
    check("build_unit_rows: prop_id == int(ACCOUNT_NUM) (REV1, CONFIRMED, no TAXABLE_OBJECT join)",
          by_geo["00000123456789012"]["prop_id"] == 123456789012)
    check("build_unit_rows: market_value == TOT_VAL", by_geo["00000123456789012"]["market_value"] == 400000)
    check("build_unit_rows: exemption_codes aggregated correctly", by_geo["00000123456789012"]["exemption_codes"] == "HS,OV65")
    check("build_unit_rows: assessed_value deliberately left None (unresolved gap, not guessed)",
          by_geo["00000123456789012"]["assessed_value"] is None)
    check("build_unit_rows: sptd_code carried through for classification (REV1)",
          by_geo["00000123456789012"]["sptd_code"] == "A11")
    check("build_unit_rows: BPP-excluded account never appears", "00000123456789015" not in by_geo)
    check("build_unit_rows: no 'taxable_object' key anywhere in output (rev1 removes the join entirely)",
          all("object_id" not in r and "taxable_object" not in r for r in unit_rows))


if __name__ == "__main__":
    test_account_info_basic_parse_and_bpp_flag()
    test_bpp_exclusion_filters_at_loader_boundary()
    test_derive_prop_id_geo_id_confirmed_mapping()
    test_derive_prop_id_geo_id_fails_loud_on_non_digit()
    test_orphan_account_classified_not_hidden()
    test_account_apprl_year_confirmed_value_mapping_and_sptd_code()
    test_exemption_codes_aggregation()
    test_g1_style_ledger_conservation()
    test_table_load_policy_covers_all_14_tables()
    test_build_unit_rows_uses_confirmed_prop_id_geo_id_derivation()

    print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
    if FAIL_COUNT == 0:
        print("ALL DCAD_FORMAT FIXTURE TESTS PASSED")
    else:
        sys.exit(1)
