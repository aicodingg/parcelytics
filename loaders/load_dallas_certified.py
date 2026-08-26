"""
loaders/load_dallas_certified.py — DESIGN + SKELETON ONLY (PX-20260826-03,
revised PX-20260826-03-rev1). NOT wired to run_all.py. NOT executed against
any database in this brief -- per the brief's own rules ("Live loads are a
later brief with a runbook").

Orchestration shape mirrors load_certified_historical.py (the closest
existing precedent: a --county-flagged, --dry-run-capable, single-source
historical-year loader) and load_ajr.py's own upsert-and-gate pattern, NOT
the 2025/2026 Travis-specific loaders (which are hardwired to the two
current-vintage files).

══════════════════════════════════════════════════════════════════════════
REV1 (PX-20260826-03-rev1): PM resolved all three unknowns the original
skeleton flagged, against the real files. This version matches:

    prop_unit.geo_id  (VARCHAR(20), already fits 17-char ACCOUNT_NUM,
                        ZERO schema change) <- ACCOUNT_INFO.ACCOUNT_NUM,
                        verbatim (text).
    prop_unit.prop_id (BIGINT PRIMARY KEY, "TCAD short integer ID" --
                        an internal surrogate, not a natural key) <-
                        int(ACCOUNT_NUM), fail-loud if non-numeric. See
                        dcad_format.derive_prop_id_geo_id().

TAXABLE_OBJECT is CONFIRMED (per the real data dictionary) to be a
building-component link table, not a finer unit grain -- DCAD's own unit
grain is the ACCOUNT (unit_count=1 everywhere, no collision mechanism).
It is dropped from the value path entirely and does not appear below;
see dcad_format.TABLE_LOAD_POLICY["TAXABLE_OBJECT"] for the documented
"deliberately unloaded" entry. The pre-rev1 version of this file's
ORPHAN-OBJECT cross-table check was built around TAXABLE_OBJECT's now-
retracted "finer grain" role; it is kept below as a GENERAL reusable
cross-table-reference check (still real and useful -- e.g. for
APPLIED_STD_EXEMPT rows whose ACCOUNT_NUM should trace back to a real
ACCOUNT_INFO row), demonstrated against APPLIED_STD_EXEMPT instead.

Classification wiring: ACCOUNT_APPRL_YEAR.SPTD_CODE (column 47, CONFIRMED)
is the real input to classification_map_dallas.classify_dallas_sptb_code()
for this relational product -- see build_unit_rows()'s own comment for
where that value is carried through (a display/benchmark concern, same as
Travis's own classi_cd handling -- NOT a prop_unit_tax_year column).

county_code = 'DALLAS' on every row, all writes into the EXISTING,
already-migrated prop_unit / prop_unit_tax_year / parcel / parcel_tax_year
tables (per MC-1 rule 4, "born partitioned" is already satisfied --
PARCEL-ROLLUP-HOTFIX-1 + migrate_county_partitioning.py's TABLE_SPECS
already made these tables county_code-led; no new CREATE TABLE needed,
matching PX-20260824-05's own finding).
══════════════════════════════════════════════════════════════════════════
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from loaders import dcad_format  # noqa: E402

DATA_SOURCE = "dcad_certified"   # the literal data_source column value this loader would write
COUNTY_CODE = "DALLAS"


# ══════════════════════════════════════════════════════════════════════
# Step 1 — scan every table's conservation ledger (pure, no DB access).
# This IS the proposed multi-table G1 (see report's ingest-gate section):
# a per-table ledger for each of the 14 tables, not one file's line count.
# ══════════════════════════════════════════════════════════════════════
def scan_all_tables(table_dir):
    """
    table_dir: the extracted DCAD2026_CERTIFIED_07232026/ folder (or a
    canary-slice folder with the same per-table filenames -- MC-4's
    per-table-slice design, since this is a relational source, not a
    single-file one; see report).

    Returns {table_name: ledger_dict} for every table this module knows
    how to iterate (ACCOUNT_INFO, ACCOUNT_APPRL_YEAR, APPLIED_STD_EXEMPT
    -- REV1: TAXABLE_OBJECT removed, it is deliberately unloaded per
    dcad_format.TABLE_LOAD_POLICY. The other 10 tables have no
    iter_*_records function either, per this brief's own scope: only
    the tables that feed a real prop_unit/prop_unit_tax_year column are
    parsed for v1; the rest are named in TABLE_LOAD_POLICY / left for a
    future brief).
    """
    ledgers = {}
    table_iterators = {
        "ACCOUNT_INFO": dcad_format.iter_account_info_records,
        "ACCOUNT_APPRL_YEAR": dcad_format.iter_account_apprl_year_records,
        "APPLIED_STD_EXEMPT": dcad_format.iter_applied_std_exempt_records,
    }
    for table_name, iter_fn in table_iterators.items():
        path = os.path.join(table_dir, dcad_format.TABLE_FILENAMES[table_name])
        ledgers[table_name] = dcad_format.scan_table_ledger(table_name, iter_fn, path=path)
    return ledgers


# ══════════════════════════════════════════════════════════════════════
# Step 2 — cross-table conservation: any secondary table's ACCOUNT_NUM
# should trace back to a real ACCOUNT_INFO row. A miss here is this
# design's ORPHAN class -- deliberately modeled on BG4's LEGACY-ONLY
# classification from PX-20260826-01: reported by name and count, NOT
# silently folded into a blanket FAIL, unless it also disagrees on a
# VALUE (which would still be a genuine mismatch, not an orphan).
#
# REV1: this is now a GENERAL reusable check, not specifically tied to
# TAXABLE_OBJECT (which no longer participates in the value path at all
# -- see module docstring). Demonstrated below against APPLIED_STD_EXEMPT,
# the one secondary table still actually in the load path.
# ══════════════════════════════════════════════════════════════════════
def find_orphan_accounts(account_info_ledger, other_table_ledger):
    """
    Pure set-difference, no DB access. Returns the set of account_nums
    present in `other_table_ledger` (e.g. APPLIED_STD_EXEMPT) but absent
    from `account_info_ledger` (ACCOUNT_INFO) -- the Dallas analog of
    BG4's "key in tax_billing but no tax_billing_account counterpart"
    case.

    Per the brief's own instruction (echoing PX-20260826-01's own
    "must be listed in output, not hidden" requirement): this set must be
    named and counted in any real gate output, never dropped silently.
    """
    return other_table_ledger["account_nums"] - account_info_ledger["account_nums"]


# ══════════════════════════════════════════════════════════════════════
# Step 3 — build the prop_unit / prop_unit_tax_year row shape (pure
# transform; NO SQL executed by this skeleton -- upsert SQL is sketched
# in comments only, per the brief's "no production writes" rule).
# ══════════════════════════════════════════════════════════════════════
def build_unit_rows(account_info_rows, appraisal_year_rows, exempt_rows_by_account):
    """
    account_info_rows: {account_num: dict} from iter_account_info_records,
        BPP-excluded already (see filter_bpp_accounts()).
    appraisal_year_rows: {account_num: dict} from iter_account_apprl_year_records
        -- REV1: also carries sptd_code (confirmed, column 47), the
        classification input.
    exempt_rows_by_account: {account_num: [dict, ...]} from
        iter_applied_std_exempt_records, consumed via
        exemption_codes_for_account().

    REV1: prop_id/geo_id are now derived directly from ACCOUNT_NUM via
    dcad_format.derive_prop_id_geo_id() -- CONFIRMED, fail-loud on a
    non-numeric ACCOUNT_NUM (an identity-integrity condition, not a
    per-row skip; see that function's own docstring). This replaces the
    pre-rev1 version's TAXABLE_OBJECT-sourced object_id, which is no
    longer part of this design at all.

    Returns a list of dicts shaped like PROP_UNIT_TAX_YEAR_UPSERT_SQL's
    real column list (ears_format.py) -- NOT executed here.
    """
    rows = []
    for account_num, info in account_info_rows.items():
        appr = appraisal_year_rows.get(account_num, {})
        prop_id, geo_id = dcad_format.derive_prop_id_geo_id(account_num)

        # Classification: ACCOUNT_APPRL_YEAR.SPTD_CODE (CONFIRMED, rev1)
        # is the real input to classification_map_dallas.
        # classify_dallas_sptb_code(). Carried through here as
        # `sptd_code` for the caller to classify -- WHERE the result is
        # stored (a prop_unit.prop_type_cd analog, or a separate
        # classi_cd-style column) remains a separate, not-yet-made
        # decision, same as pre-rev1; classification is a display/
        # benchmark concern layered on top of the unit row, not itself
        # a prop_unit_tax_year column (mirrors how Travis's own classi_cd
        # lives outside prop_unit_tax_year too).
        rows.append({
            "county_code": COUNTY_CODE,
            "prop_id": prop_id,                          # CONFIRMED, rev1: int(ACCOUNT_NUM)
            "geo_id": geo_id,                             # CONFIRMED, rev1: ACCOUNT_NUM verbatim
            "tax_year": appr.get("tax_yr"),
            "market_value": appr.get("tot_val"),          # CONFIRMED mapping
            "land_value": appr.get("land_val"),           # CONFIRMED mapping
            "imprv_value": appr.get("impr_val"),          # CONFIRMED mapping
            "hs_cap_loss": appr.get("hmstd_cap_val"),      # UNCONFIRMED semantics -- see dcad_format.py docstring; likely WRONG as a direct pass-through, flagged not fixed
            "assessed_value": None,   # UNRESOLVED -- see dcad_format.py's per-jurisdiction-shape gap; deliberately left unpopulated rather than guessed
            "taxable_value": None,    # UNRESOLVED -- same gap
            "exemption_codes": dcad_format.exemption_codes_for_account(exempt_rows_by_account.get(account_num, [])),
            "data_source": DATA_SOURCE,
            "owner_name": info.get("owner_name"),
            "situs_address": info.get("situs_address"),
            "sptd_code": appr.get("sptd_code"),  # classification input, passthrough -- see comment above
        })
    return rows

    # Real upsert SQL this design proposes reusing UNCHANGED (schema
    # already county_code-led; no new SQL needed beyond what ears_format.py
    # already ships) -- sketched here as a comment, NOT executed:
    #
    #   ears_format.PROP_UNIT_UPSERT_SQL
    #   ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL
    #
    # Both already accept county_code as their first bound parameter and
    # already use ON CONFLICT (county_code, prop_id[, tax_year]) -- exactly
    # Dallas's own real, live constraint shape. The only real new work is
    # producing correctly-shaped Python dicts to bind into them, which is
    # this function's job.


def filter_bpp_accounts(account_info_records):
    """
    Applies the DIVISION_CD BPP exclusion policy (dcad_format.is_bpp_division)
    at the loader boundary, before any downstream join or classification.
    Returns (kept: {account_num: dict}, excluded_count: int).
    """
    kept = {}
    excluded = 0
    for rec in account_info_records:
        if rec["skip_reason"] is not None:
            continue
        if rec["is_bpp"]:
            excluded += 1
            continue
        kept[rec["account_num"]] = rec
    return kept, excluded


# ══════════════════════════════════════════════════════════════════════
# CLI skeleton -- mirrors load_certified_historical.py's --county/--dry-run
# shape. NOT wired to write anything in this brief.
# ══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table-dir", required=True, help="Path to the extracted DCAD2026_CERTIFIED_07232026/-style folder")
    ap.add_argument("--dry-run", action="store_true", default=True,
                     help="This skeleton only supports --dry-run (scan + report). Live writes are a later brief.")
    args = ap.parse_args()

    print("PX-20260826-03-rev1 SKELETON -- design/scan only, no writes.")
    ledgers = scan_all_tables(args.table_dir)
    for name, ledger in ledgers.items():
        print(f"{name}: {ledger['total_lines']:,} lines, buckets={ledger['buckets']}")

    orphans = find_orphan_accounts(ledgers["ACCOUNT_INFO"], ledgers["APPLIED_STD_EXEMPT"])
    if orphans:
        print(f"ORPHAN-ACCOUNT (n={len(orphans)}): APPLIED_STD_EXEMPT rows with no ACCOUNT_INFO counterpart -- reported, not silently dropped: {sorted(orphans)[:10]}{'...' if len(orphans) > 10 else ''}")
    else:
        print("ORPHAN-ACCOUNT: none found.")


if __name__ == "__main__":
    main()
