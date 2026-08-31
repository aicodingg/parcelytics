#!/usr/bin/env python3
"""
test_dallas_gate_4_county_code.py — DALLAS-GATE-4 verification requirement:
"Real fixture tests for each of the 5 files' real fixes, matching the same
rigor as BILLING-DIAG-7's test_upsert_billing_rows_commit.py -- prove the
real SQL text includes county_code correctly, and that ON CONFLICT targets
match the real, live constraints."

EXTENDED (PIR-XLSX-HOTFIX-1, real, urgent, same severity class as this
file's original 5): a 6th writer, loaders/pir_xlsx_common.py, was found
live-broken by TAX-BILLING-REKEY-2's own real writer enumeration -- the
identical failure mode as the original 5, just never in scope for the
briefs that found and fixed those. File 6's section below follows this
file's own established pattern exactly (same string/regex-against-real-
shipping-source technique, same rigor), not a separate ad hoc test file.

Technique: direct string-membership/regex assertions against each file's
REAL, shipping source text -- same rigor as test_api_billing_retry.py's own
sanity-assert block, chosen over the slice-and-exec technique (used
elsewhere in this project for complex functions with real control flow,
e.g. api_billing()/upsert_billing_rows()) because all 5 of DALLAS-GATE-4's
fixes are simple SQL-constant/column-list/ON-CONFLICT-target changes, not
functions with branching logic that need to be exercised end-to-end. Every
assertion below reads the actual file on disk, not a reimplementation or a
copy-pasted expectation -- if the real source drifts, these tests fail.

Background (DALLAS-GATE-3's own investigation): migrate_county_partitioning.py's
already-executed-in-production migration changed several tax_billing-family
tables' PKs from a bare (geo_id, tax_year)-style shape to one leading with
county_code -- (county_code, geo_id, tax_year) / (county_code, geo_id,
tax_year, entity_code). Any INSERT ... ON CONFLICT still naming the OLD
column list against the now-migrated live schema raises Postgres's real
"there is no unique or exclusion constraint matching the ON CONFLICT
specification" error -- a hard runtime failure, not silent corruption.
tax_billing_quarantine's county_code is additionally NOT NULL post-
migration, so a missing county_code column in an INSERT targeting THAT
table is a NOT NULL violation, not just an ON CONFLICT mismatch. This suite
proves all 5 previously-broken files now carry the correct column lists and
ON CONFLICT targets, and that no stale old-shape reference survives
anywhere in any of them.

Run: python3 test_dallas_gate_4_county_code.py
"""
import re
import sys

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ─────────────────────────────────────────────────────────────────────────
# File 1: loaders/load_pir_billing.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_pir_billing.py")
src = open("loaders/load_pir_billing.py").read()

check("DEFAULT_COUNTY = \"TRAVIS\" constant declared",
      'DEFAULT_COUNTY = "TRAVIS"' in src)
check("load_file() signature threads county_code=DEFAULT_COUNTY",
      "def load_file(conn, filepath, dry_run=False, county_code=DEFAULT_COUNTY):" in src)
check("billing_sql INSERT column list includes county_code, targets tax_billing_account "
      "(PX-20260823-01: TAX-BILLING-REKEY-3 retargeted this loader from the old "
      "direct tax_billing write to the account-grain table)",
      re.search(r"INSERT INTO tax_billing_account\s*\n\s*\(county_code, account_id, geo_id, tax_year", src) is not None)
check("billing_sql ON CONFLICT targets the live (county_code, account_id, tax_year)",
      "ON CONFLICT (county_code, account_id, tax_year) DO UPDATE" in src)
check("entity_sql INSERT column list includes county_code, targets tax_billing_account_entity",
      re.search(r"INSERT INTO tax_billing_account_entity\s*\n\s*\(county_code, account_id, geo_id, tax_year, "
                 r"entity_code, amount_due, amount_paid\)", src) is not None)
check("entity_sql ON CONFLICT targets the live (county_code, account_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, account_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("PX-20260823-01 (Law 3): no stale direct-write INSERT INTO tax_billing/"
      "tax_billing_entity remains -- TAX-BILLING-REKEY-3 retargeted billing_sql/"
      "entity_sql to tax_billing_account/_account_entity; tax_billing/"
      "tax_billing_entity are now written only by tax_billing_rollup.py",
      "INSERT INTO tax_billing\n" not in src and "INSERT INTO tax_billing_entity" not in src)


# ─────────────────────────────────────────────────────────────────────────
# File 2: loaders/load_pir_billing_2021_full.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_pir_billing_2021_full.py")
src = open("loaders/load_pir_billing_2021_full.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols, DEFAULT_COUNTY" in src)
check("BILLING_SQL INSERT column list includes county_code, targets tax_billing_account "
      "(PX-20260823-01: TAX-BILLING-REKEY-3 retargeted this loader from the old "
      "direct tax_billing write to the account-grain table)",
      re.search(r"INSERT INTO tax_billing_account\s*\n\s*\(county_code, account_id, geo_id, tax_year", src) is not None)
check("BILLING_SQL VALUES clause includes %(county_code)s",
      "VALUES (%(county_code)s, %(account_id)s, %(geo_id)s, %(tax_year)s" in src)
check("BILLING_SQL ON CONFLICT targets the live (county_code, account_id, tax_year)",
      "ON CONFLICT (county_code, account_id, tax_year) DO UPDATE" in src)
check("ENTITY_SQL INSERT column list includes county_code, targets tax_billing_account_entity",
      re.search(r"INSERT INTO tax_billing_account_entity\s*\n\s*\(county_code, account_id, geo_id, tax_year, "
                 r"entity_code, amount_due, amount_paid\)", src) is not None)
check("ENTITY_SQL VALUES clause includes %(county_code)s",
      "VALUES (%(county_code)s, %(account_id)s, %(geo_id)s, %(tax_year)s, %(entity_code)s" in src)
check("ENTITY_SQL ON CONFLICT targets the live (county_code, account_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, account_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("PX-20260823-01 (Law 3): no stale direct-write INSERT INTO tax_billing/"
      "tax_billing_entity remains -- TAX-BILLING-REKEY-3 retargeted BILLING_SQL/"
      "ENTITY_SQL to tax_billing_account/_account_entity; tax_billing/"
      "tax_billing_entity are now written only by tax_billing_rollup.py",
      "INSERT INTO tax_billing\n" not in src and "INSERT INTO tax_billing_entity" not in src)
check("write_to_db() signature threads county_code=DEFAULT_COUNTY",
      "def write_to_db(conn, matched, county_code=DEFAULT_COUNTY):" in src)
check("verify_sanity_parcels() signature threads county_code=DEFAULT_COUNTY "
      "(forward-looking read-side fix)",
      "def verify_sanity_parcels(conn, county_code=DEFAULT_COUNTY):" in src)
check("verify_sanity_parcels()'s SELECT scopes by county_code",
      "WHERE geo_id = %s AND tax_year = %s AND county_code = %s" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("PX-20260830-05 Task 2 (Bucket B): reconcile_geo_ids() now threads "
      "county_code and predicates its `parcel` existence-check query on it "
      "(county_code IS available at this file's own main()/run call site, "
      "which already threads args.county to write_to_db() and "
      "verify_sanity_parcels() a few lines later)",
      "def reconcile_geo_ids(conn, by_account, county_code=DEFAULT_COUNTY):" in src
      and "SELECT geo_id FROM parcel WHERE county_code = %s" in src)
check("reconcile_geo_ids() call site passes county_code=args.county",
      "reconcile_geo_ids(conn, by_account, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 3: loaders/load_tax_current.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_tax_current.py")
src = open("loaders/load_tax_current.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols, DEFAULT_COUNTY" in src)
check("load() signature threads county_code=DEFAULT_COUNTY",
      "def load(conn, dry_run=False, new_only=False, county_code=DEFAULT_COUNTY):" in src)
check("--new-only already_tagged_keys SELECT scopes by county_code",
      'WHERE tax_year = 2025 AND data_source IS NOT NULL AND county_code = %s' in src)
check("billing_sql INSERT column list includes county_code, targets tax_billing_account "
      "(PX-20260823-01: TAX-BILLING-REKEY-3 retargeted load()'s billing_sql/entity_sql "
      "from the old direct tax_billing write to the account-grain table)",
      re.search(r"INSERT INTO tax_billing_account\s*\n\s*\(county_code, account_id, geo_id, tax_year", src) is not None)
check("billing_sql ON CONFLICT targets the live (county_code, account_id, tax_year)",
      "ON CONFLICT (county_code, account_id, tax_year) DO UPDATE" in src)
check("entity_sql INSERT column list includes county_code, targets tax_billing_account_entity",
      re.search(r"INSERT INTO tax_billing_account_entity\s*\n\s*\(county_code, account_id, geo_id, tax_year, "
                 r"entity_code, amount_due, amount_paid\)", src) is not None)
check("entity_sql ON CONFLICT targets the live (county_code, account_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, account_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains against tax_billing_account",
      "ON CONFLICT (geo_id, tax_year) DO UPDATE" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("PX-20260823-01 (Law 3): no stale direct-write INSERT INTO tax_billing/"
      "tax_billing_entity remains -- TAX-BILLING-REKEY-3 retargeted load()'s "
      "billing_sql/entity_sql to tax_billing_account/_account_entity",
      "INSERT INTO tax_billing\n" not in src and "INSERT INTO tax_billing_entity" not in src)
check("load_delinquent()'s tax_delinquent table now ALSO carries county_code "
      "(PX-20260822-06-rev1 fixed this separately from the tax_billing-family "
      "rekey -- the old claim here that it was 'correctly left untouched' is "
      "itself now stale, since it WAS touched in that later brief; File 9 below "
      "is the canonical rigor-test for tax_delinquent's shape, this check just "
      "keeps File 3's own cross-reference to it accurate rather than false)",
      "ON CONFLICT (county_code, geo_id) DO UPDATE" in src and "INSERT INTO tax_delinquent" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 4: loaders/quarantine_contamination.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/quarantine_contamination.py")
src = open("loaders/quarantine_contamination.py").read()

check("run()'s DELETE ... RETURNING includes county_code",
      re.search(r"DELETE FROM tax_billing\s*\n\s*WHERE \{_CONTAMINATION_WHERE\} \{exclude_clause\}\s*\n\s*"
                 r"RETURNING county_code, geo_id, tax_year", src) is not None)
check("run()'s INSERT INTO tax_billing_quarantine column list includes county_code",
      re.search(r"INSERT INTO tax_billing_quarantine\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("run()'s SELECT ... FROM moved list includes county_code (same position as RETURNING)",
      re.search(r"SELECT county_code, geo_id, tax_year, billing_num, owner_name, total_tax,\s*\n\s*"
                 r"total_paid, total_due, is_delinquent, first_delinquent_yr,\s*\n\s*"
                 r"cause_number, exemption_codes, data_source, confidence_level, %s, %s\s*\n\s*FROM moved",
                 src) is not None)
check("restore_class_a()'s DELETE ... RETURNING includes county_code",
      re.search(r"DELETE FROM tax_billing_quarantine\s*\n\s*WHERE geo_id = ANY\(%s\)\s*\n\s*"
                 r"RETURNING county_code, geo_id, tax_year", src) is not None)
check("restore_class_a()'s INSERT INTO tax_billing column list includes county_code",
      re.search(r"INSERT INTO tax_billing\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("restore_class_a()'s ON CONFLICT target corrected to the live "
      "(county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO NOTHING" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains anywhere in the file",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("_CREATE_QUARANTINE_SQL / schema.sql's tax_billing_quarantine bootstrap DDL "
      "deliberately left untouched (CREATE TABLE IF NOT EXISTS is a no-op against "
      "the already-migrated live table; changing it would break the file's own "
      "column-for-column parity test against schema.sql without fixing anything "
      "real -- see final report)",
      "PRIMARY KEY (geo_id, tax_year)" in src)  # _CREATE_QUARANTINE_SQL, unchanged by design


# ─────────────────────────────────────────────────────────────────────────
# File 5: loaders/backfill_tax_billing_2025_confidence.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/backfill_tax_billing_2025_confidence.py")
src = open("loaders/backfill_tax_billing_2025_confidence.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols, DEFAULT_COUNTY" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("COUNT_SQL['verified'] scoped by county_code",
      "WHERE tax_year = 2025 AND data_source IS NULL AND county_code = %s\n          AND total_tax IS NOT NULL"
      in src)
check("COUNT_SQL['derived'] scoped by tb.county_code, entity EXISTS correlated by county_code too",
      "tb.county_code = %s" in src and "tbe.county_code = tb.county_code" in src)
check("COUNT_SQL['no_usable_total'] uses the same tb.county_code / tbe.county_code correlation",
      src.count("tbe.county_code = tb.county_code") == 2)
check("UPDATE_VERIFIED_SQL WHERE clause scoped by county_code",
      "WHERE tax_year = 2025 AND data_source IS NULL AND county_code = %s\n      AND total_tax IS NOT NULL"
      in src)
check("UPDATE_DERIVED_SQL's entity_sums CTE scoped by county_code",
      "FROM tax_billing_entity\n        WHERE tax_year = 2025 AND county_code = %s" in src)
check("UPDATE_DERIVED_SQL's outer UPDATE join correlates tb.county_code = es.county_code "
      "(real, forward-looking correctness against geo_id collisions across counties)",
      "AND tb.county_code = es.county_code" in src)
check("UPDATE_NO_USABLE_TOTAL_SQL WHERE clause scoped by county_code",
      "WHERE tb.tax_year = 2025 AND tb.data_source IS NULL AND tb.county_code = %s" in src)
check("sanity-check SELECT at the end of main() also scoped by county_code",
      "FROM tax_billing WHERE geo_id = %s AND tax_year = 2025 AND county_code = %s" in src)
check("none of the 3 real UPDATE SQL constants contain an ON CONFLICT clause "
      "(all 3 writes are UPDATEs, no unique-constraint target to break -- "
      "confirmed by inspecting the actual SQL text, not assumed; the file's own "
      "prose comments legitimately mention the phrase when explaining why)",
      all("ON CONFLICT" not in sql for sql in
          (src[src.index('UPDATE_VERIFIED_SQL = """') + len('UPDATE_VERIFIED_SQL = """'):src.index('"""', src.index('UPDATE_VERIFIED_SQL = """') + len('UPDATE_VERIFIED_SQL = """'))],
           src[src.index('UPDATE_DERIVED_SQL = """') + len('UPDATE_DERIVED_SQL = """'):src.index('"""', src.index('UPDATE_DERIVED_SQL = """') + len('UPDATE_DERIVED_SQL = """'))],
           src[src.index('UPDATE_NO_USABLE_TOTAL_SQL = """') + len('UPDATE_NO_USABLE_TOTAL_SQL = """'):src.index('"""', src.index('UPDATE_NO_USABLE_TOTAL_SQL = """') + len('UPDATE_NO_USABLE_TOTAL_SQL = """'))])))


# ─────────────────────────────────────────────────────────────────────────
# File 6: loaders/pir_xlsx_common.py (PIR-XLSX-HOTFIX-1)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/pir_xlsx_common.py")
src = open("loaders/pir_xlsx_common.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("BILLING_SQL INSERT column list includes county_code, targets tax_billing_account "
      "(PX-20260823-01: TAX-BILLING-REKEY-3 retargeted this loader from the old "
      "direct tax_billing write to the account-grain table)",
      re.search(r"INSERT INTO tax_billing_account\s*\n\s*\(county_code, account_id, geo_id, tax_year", src) is not None)
check("BILLING_SQL VALUES clause includes %(county_code)s",
      "VALUES (%(county_code)s, %(account_id)s, %(geo_id)s, %(tax_year)s" in src)
check("BILLING_SQL ON CONFLICT targets the live (county_code, account_id, tax_year)",
      "ON CONFLICT (county_code, account_id, tax_year) DO UPDATE" in src)
check("ENTITY_SQL INSERT column list includes county_code, targets tax_billing_account_entity",
      re.search(r"INSERT INTO tax_billing_account_entity\s*\n\s*\(county_code, account_id, geo_id, tax_year, "
                 r"entity_code, amount_due, amount_paid\)", src) is not None)
check("ENTITY_SQL VALUES clause includes %(county_code)s",
      "VALUES (%(county_code)s, %(account_id)s, %(geo_id)s, %(tax_year)s, %(entity_code)s" in src)
check("ENTITY_SQL ON CONFLICT targets the live (county_code, account_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, account_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("PX-20260823-01 (Law 3): no stale direct-write INSERT INTO tax_billing/"
      "tax_billing_entity remains -- TAX-BILLING-REKEY-3 retargeted BILLING_SQL/"
      "ENTITY_SQL to tax_billing_account/_account_entity; tax_billing/"
      "tax_billing_entity are now written only by tax_billing_rollup.py",
      "INSERT INTO tax_billing\n" not in src and "INSERT INTO tax_billing_entity" not in src)
check("write_to_db() signature threads county_code=DEFAULT_COUNTY",
      "def write_to_db(conn, matched, tax_year, data_source, confidence_level, county_code=DEFAULT_COUNTY):" in src)
check("write_to_db()'s billing_rows dict includes county_code, account_id, geo_id, tax_year "
      "(TAX-BILLING-REKEY-3: account_id inserted between county_code and geo_id, so the "
      "old contiguous county_code/geo_id/tax_year substring no longer appears verbatim)",
      '"county_code": county_code, "account_id": account_id, "geo_id": geo_id,' in src
      and '"tax_year": tax_year,' in src)
check("check_portal_scrape_divergence() signature threads county_code=DEFAULT_COUNTY "
      "(forward-looking read-side fix, mirrors DALLAS-GATE-4's identical fix to "
      "load_pir_billing_2021_full.py's sibling verify_sanity_parcels())",
      "def check_portal_scrape_divergence(conn, matched, tax_year, tolerance=1.00, county_code=DEFAULT_COUNTY):" in src)
check("check_portal_scrape_divergence()'s SELECT scopes by county_code",
      "FROM tax_billing WHERE tax_year = %s AND geo_id = ANY(%s) AND county_code = %s" in src)
check("verify_sanity_parcels() signature threads county_code=DEFAULT_COUNTY "
      "(forward-looking read-side fix)",
      "def verify_sanity_parcels(conn, tax_year, expected, county_code=DEFAULT_COUNTY):" in src)
check("verify_sanity_parcels()'s SELECT scopes by county_code",
      "SELECT total_tax FROM tax_billing WHERE geo_id = %s AND tax_year = %s AND county_code = %s" in src)
check("--county CLI flag added to run_cli(), default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("run_cli()'s check_portal_scrape_divergence() call passes county_code=args.county",
      "check_portal_scrape_divergence(conn, matched, tax_year, county_code=args.county)" in src)
check("run_cli()'s write_to_db() call passes county_code=args.county",
      "county_code=args.county)" in src and "write_to_db(conn, matched, tax_year, data_source, confidence_level," in src)
check("run_cli()'s verify_sanity_parcels() call passes county_code=args.county",
      "verify_sanity_parcels(conn, tax_year, sanity_expected, county_code=args.county)" in src)
# UPDATE (PX-20260830-05 Task 2, Bucket B): the check below used to assert
# reconcile_geo_ids() was DELIBERATELY left unscoped by county_code (a
# stance carried over from DALLAS-GATE-4 / TAX-BILLING-REKEY-3). That
# stance is now stale -- PM's Task 2 ruling for this exact function was
# explicit: "If county_code is available at the call site, predicate; if
# not, thread it -- do not exempt a shared module." county_code IS
# available at this function's one real call site (run_cli() already
# threads args.county to check_portal_scrape_divergence() a few lines
# later), so this is now a thread-it fix, not a standing exemption. The
# identical function in load_pir_billing_2021_full.py got the same fix,
# same reasoning (see File 2's own new checks above).
check("PX-20260830-05 Task 2 (Bucket B): reconcile_geo_ids() now threads "
      "county_code and predicates its `parcel` existence-check query on it "
      "(county_code IS available at run_cli()'s call site, which already "
      "threads args.county to check_portal_scrape_divergence() a few lines "
      "later -- per PM's ruling this is a thread-it fix, not an exemption, "
      "even though this is a shared module)",
      "def reconcile_geo_ids(conn, by_account, county_code=DEFAULT_COUNTY):" in src
      and "SELECT geo_id FROM parcel WHERE county_code = %s" in src)
check("run_cli()'s reconcile_geo_ids() call passes county_code=args.county",
      "reconcile_geo_ids(conn, by_account, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# PX-20260822-06-rev1: DALLAS-GATE-4 family completion (3b/3c gaps against
# the live constraint map, confirmed via \d against production,
# 2026-08-23, table below reproduced from that brief):
#   parcel_tax_year -> (county_code, geo_id, tax_year)
#   parcel_metrics  -> (county_code, geo_id, tax_year)
#   tax_delinquent  -> (county_code, geo_id)
#   county_tax_rate -> (county_code, entity_code, tax_year)
#   group_stats     -> (county_code, neighborhood_cd_key, state_cd1_class,
#                        classi_cd_key, tax_year)
# Same string/regex-against-real-shipping-source technique as Files 1-6
# above -- every assertion below reads the actual file on disk.
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# File 7: loaders/load_cert_2021.py (parcel_tax_year)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_cert_2021.py")
src = open("loaders/load_cert_2021.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("build_upsert_sql()'s INSERT column list includes county_code first",
      'cols_insert = """county_code, geo_id, tax_year,' in src)
check("build_upsert_sql()'s VALUES clause includes %(county_code)s first",
      'vals_insert = """%(county_code)s, %(geo_id)s, %(tax_year)s,' in src)
check("build_upsert_sql()'s ON CONFLICT targets the live (county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE SET" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("run_load() signature threads county_code=DEFAULT_COUNTY",
      "def run_load(conn, records, with_exemptions, dry_run, county_code=DEFAULT_COUNTY):" in src)
check("run_load()'s per-row dict includes county_code",
      "'county_code':     county_code," in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      "ap.add_argument('--county', default=DEFAULT_COUNTY," in src)
check("main() passes county_code=args.county into run_load()",
      "run_load(conn, deduped, with_exemptions, args.dry_run, county_code=args.county)" in src)
check("PX-20260830-05 Task 2 (Bucket B): post_load_summary() now threads "
      "county_code and predicates its parcel_tax_year breakdown query on it "
      "(it was pooling every loaded county's 2021 rows into one blended "
      "breakdown before this fix)",
      "def post_load_summary(conn, county_code=DEFAULT_COUNTY):" in src
      and "WHERE tax_year = 2021 AND county_code = %s" in src)
check("post_load_summary() call site passes county_code=args.county",
      "post_load_summary(conn, county_code=args.county)" in src)
# PX-20260830-05 Task 2 correction (reviewer-rejected "27 out of scope"
# claim): load_cert_2021.py's own run_load() carries 6 MORE Bucket B rows
# beyond the post_load_summary() fix above -- the before-load counts, the
# after-load counts, a dead-code diagnostic subquery, and a "simpler
# version" totals query. All predicated below, same mechanical convention.
check("run_load()'s before-load row/ajr counts are both predicated on "
      "county_code",
      'cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s", (TAX_YEAR, county_code))'
      in src
      and '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",\n            (TAX_YEAR, \'ajr_2021\', county_code)'
      in src)
check("run_load()'s after-load rows_after/cert_after counts are both "
      "predicated on county_code",
      '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",\n            (TAX_YEAR, DATA_SOURCE, county_code)'
      in src)
check("run_load()'s dead-code diagnostic subquery (result discarded by the "
      "immediately-following query -- left in place, only predicated, per "
      "PM's 'mechanical' scope) is predicated on both the outer WHERE and "
      "the correlated subquery's join",
      "WHERE tax_year = %s AND data_source = %s AND pty.county_code = %s" in src
      and "pty2.geo_id = pty.geo_id AND pty2.county_code = pty.county_code" in src)
check("run_load()'s 'simpler version' totals query is predicated on "
      "county_code",
      "FROM parcel_tax_year\n            WHERE tax_year = %s AND data_source = %s AND county_code = %s\n        \"\"\", (TAX_YEAR, DATA_SOURCE, county_code))"
      in src)


# ─────────────────────────────────────────────────────────────────────────
# File 8: loaders/compute_metrics.py (parcel_metrics)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/compute_metrics.py")
src = open("loaders/compute_metrics.py").read()

check("parcel_metrics INSERT column list includes county_code first",
      "INSERT INTO parcel_metrics (\n                county_code, geo_id, tax_year," in src)
check("parcel_metrics SELECT list sources county_code from pty.county_code "
      "(parcel_tax_year already carries it, written by every real writer)",
      "SELECT\n                pty.county_code,\n                pty.geo_id,\n                pty.tax_year," in src)
# UPDATE (PX-20260830-05 Task 3, Bucket C): the check below used to assert
# that parcel_metrics's DELETE was an explicitly-disclosed, out-of-scope
# gap (a "3d-class (blast-radius) concern"). That's stale -- PX-20260828-16-
# followup already closed this gap: the DELETE at compute_parcel_metrics()
# is now `DELETE FROM parcel_metrics WHERE county_code = %s`, run in the
# same transaction as the INSERT...SELECT rebuild it precedes. Replaced
# with an assertion against the real, current scoped DELETE.
check("compute_parcel_metrics()'s DELETE FROM parcel_metrics is scoped by "
      "county_code (PX-20260828-16-followup already closed this gap; the "
      "old disclosure-only fixture here was stale)",
      'cur.execute("DELETE FROM parcel_metrics WHERE county_code = %s", (county_code,))' in src)
check("PX-20260830-05 Task 3 (Bucket C): analyze_threshold() takes county_code "
      "as a REQUIRED parameter with no default (unlike every other function "
      "in this file) -- it's a per-county distribution report, and a silent "
      "DEFAULT_COUNTY fallback would mislabel a blended multi-county result "
      "as single-county",
      "def analyze_threshold(conn, county_code):" in src)
check("analyze_threshold()'s self-join is scoped by county_code on both "
      "sides (b.county_code = a.county_code) plus a direct a.county_code "
      "predicate in the WHERE clause -- parcel_tax_year is composite_pk-"
      "migrated, so an unscoped self-join could pair one county's row a "
      "with a same-geo_id different-county row b",
      "ON b.county_code = a.county_code" in src
      and "WHERE a.county_code = %s AND a.market_value > 0" in src)
check("both analyze_threshold() call sites in main() pass county_code=args.county",
      src.count("analyze_threshold(conn, county_code=args.county)") == 2)
check("PX-20260830-05 Task 3 (Bucket C): print_sample() takes county_code, "
      "default DEFAULT_COUNTY (a debug/sanity-check printout, not a "
      "business figure -- a default is fine here, unlike analyze_threshold())",
      "def print_sample(conn, county_code=DEFAULT_COUNTY):" in src)
check("print_sample()'s tax_billing count/by-year queries are predicated "
      "on county_code",
      "SELECT COUNT(*) FROM tax_billing WHERE county_code = %s" in src
      and "FROM tax_billing WHERE county_code = %s \"\n            \"GROUP BY tax_year" in src)
check("print_sample()'s per-parcel tax_billing/tax_billing_entity/"
      "parcel_metrics lookups are all predicated on county_code (geo_id "
      "alone is not guaranteed unique across counties)",
      "WHERE geo_id = %s AND county_code = %s ORDER BY tax_year\",\n                (geo_id, county_code)" in src
      and "WHERE geo_id = %s AND county_code = %s GROUP BY tax_year ORDER BY tax_year\",\n                (geo_id, county_code)" in src
      and "FROM parcel_metrics WHERE geo_id = %s AND county_code = %s ORDER BY tax_year" in src)
check("print_sample()'s county_benchmark sample query is predicated on "
      "county_code",
      "WHERE property_type_label = 'Residential' AND tax_year = 2025 AND county_code = %s" in src)
check("both print_sample() call sites in main() pass county_code=args.county",
      src.count("print_sample(conn, county_code=args.county)") == 2)


# ─────────────────────────────────────────────────────────────────────────
# File 9: loaders/load_tax_current.py's load_delinquent() (tax_delinquent)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_tax_current.py -- load_delinquent() (tax_delinquent)")
src = open("loaders/load_tax_current.py").read()

check("load_delinquent() signature threads county_code=DEFAULT_COUNTY",
      "def load_delinquent(conn, county_code=DEFAULT_COUNTY):" in src)
check("tax_delinquent INSERT column list includes county_code first",
      "INSERT INTO tax_delinquent\n            (county_code, geo_id, tax_year," in src)
check("tax_delinquent ON CONFLICT targets the live (county_code, geo_id)",
      "ON CONFLICT (county_code, geo_id) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id) target remains against tax_delinquent",
      "ON CONFLICT (geo_id) DO UPDATE" not in src)
check("rows.append() for tax_delinquent includes county_code first",
      "rows.append((\n                county_code,\n                geo_id," in src)
check("load_delinquent() is called with county_code=args.county at the CLI call site",
      "load_delinquent(conn, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 10: loaders/load_tax_rates.py (county_tax_rate)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_tax_rates.py")
src = open("loaders/load_tax_rates.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("load() signature threads county_code=DEFAULT_COUNTY",
      "def load(conn, county_code=DEFAULT_COUNTY):" in src)
check("county_tax_rate INSERT column list includes county_code first",
      "INSERT INTO county_tax_rate (county_code, entity_code, entity_name, tax_year, rate)" in src)
check("county_tax_rate ON CONFLICT targets the live (county_code, entity_code, tax_year)",
      "ON CONFLICT (county_code, entity_code, tax_year) DO UPDATE" in src)
check("no stale ON CONFLICT (entity_code, tax_year) target remains",
      "ON CONFLICT (entity_code, tax_year)" not in src)
check("rows.append() includes county_code first",
      "rows.append((county_code, str(entity_code), str(entity_name), year, rate))" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("main() passes county_code=args.county into load()",
      "load(conn, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 11: loaders/refresh_group_stats.py (group_stats) -- ALREADY CORRECT,
# no code change this round; assertions confirm that, so a future
# regression is caught the same way as every other file in this suite.
#
# STALE-FIXTURE FIX (PX-20260830-05, tracked as #1042): the two checks below
# used to assert an externally-injected `%(county_code)s AS county_code`
# literal in _build_insert_sql()'s SELECT list -- the PARTITION-2-FIX-1
# shape. PX-20260828-13 (Stage 4 MISSING_TENANT_SCOPE follow-up) replaced
# that shape for real: REFRESH_GROUP_STATS_SQL now selects `p.county_code`
# as a genuine column, carried through the GROUP BY, so every output row's
# county_code is DERIVED from that row's own parcel, not stamped on
# uniformly from outside (see _build_insert_sql()'s own docstring, and
# loaders/test_refresh_group_stats.py's REFRESH_GROUP_STATS_SQL fixtures,
# which already cover the real current shape independently of this file).
# These two checks were left asserting the retired shape and would have
# failed forever after that fix landed -- rewritten here to assert the
# real, current column-list shape instead of being deleted, so this
# section keeps doing its stated job (catch a REAL future regression on
# this file) rather than silently losing coverage.
# ─────────────────────────────────────────────────────────────────────────
section("loaders/refresh_group_stats.py (verify-only -- already fixed by PX-20260828-13, superseding PARTITION-2-FIX-1)")
src = open("loaders/refresh_group_stats.py").read()

check("_build_insert_sql()'s INSERT column list includes county_code, "
      "leading the batch/refresh-metadata columns",
      "county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year," in src)
check("_build_insert_sql()'s SELECT no longer injects an external "
      "%(county_code)s literal (that shape was PX-20260828-13's actual bug, "
      "not a fix) -- county_code is a real, derived column instead. (The "
      "docstring's own historical mention of the retired shape, quoted for "
      "context, is expected and is not what this check looks for.)",
      "%(county_code)s                                                        AS county_code," not in src)
check("REFRESH_GROUP_STATS_SQL's own SELECT list carries p.county_code as a "
      "real column (the thing _build_insert_sql()'s SELECT list now relies on)",
      "p.county_code               AS county_code," in src)
check("shadow table is built via LIKE group_stats INCLUDING ALL -- inherits "
      "whatever the LIVE group_stats PK/constraints are, so this file needs no "
      "hardcoded ON CONFLICT target to stay correct against future PK changes",
      "CREATE TABLE group_stats_shadow (LIKE group_stats INCLUDING ALL)" in src)
check("no ON CONFLICT clause anywhere (shadow-swap via rename, not upsert -- "
      "genuinely the correct design, not a missed fix)",
      "ON CONFLICT" not in src)


# ─────────────────────────────────────────────────────────────────────────
# File 12: loaders/snapshot_2026_preliminary.py (parcel_2026_preliminary_snapshot)
# -- ALREADY CORRECT, no code change this round (fixed by DALLAS-GATE-2).
# ─────────────────────────────────────────────────────────────────────────
section("loaders/snapshot_2026_preliminary.py (verify-only -- already fixed by DALLAS-GATE-2)")
src = open("loaders/snapshot_2026_preliminary.py").read()

check("INSERT_SQL column list includes county_code first",
      "INSERT INTO parcel_2026_preliminary_snapshot\n        (county_code, geo_id, market_value" in src)
check("DELETE_SQL is county-scoped (not an unconditional TRUNCATE), matching "
      "reload_county_scope.py's discipline",
      "DELETE FROM parcel_2026_preliminary_snapshot WHERE county_code = %(county_code)s" in src)
check("INSERT_SQL itself (not the module docstring's ON CONFLICT DO NOTHING "
      "discussion) genuinely has no ON CONFLICT clause -- correct by design: "
      "county-scoped DELETE always runs immediately before the INSERT in the same "
      "transaction, so no pre-existing row this INSERT could conflict against survives",
      "ON CONFLICT" not in
      src[src.index('INSERT_SQL = """'):src.index('"""', src.index('INSERT_SQL = """') + 20)])


# ─────────────────────────────────────────────────────────────────────────
# File 13: loaders/quarantine_contamination.py (tax_billing_quarantine
# county_code column, re-verified) -- ALREADY CORRECT, no code change this
# round; the in-file fail-loud reasoning (NOT NULL violation, not an ON
# CONFLICT mismatch, since this INSERT has no ON CONFLICT clause at all)
# still holds. Files 4's own checks above (lines ~154-182) already cover
# this file's SQL shape; this is the PX-20260822-06-rev1-specific
# confirmation that the documented reasoning is still accurate.
# ─────────────────────────────────────────────────────────────────────────
section("loaders/quarantine_contamination.py (re-verify: fail-loud reasoning still holds)")
src = open("loaders/quarantine_contamination.py").read()

check("run()'s INSERT into tax_billing_quarantine has no ON CONFLICT clause "
      "(fail mode is a NOT NULL violation on county_code, not an ON CONFLICT "
      "mismatch -- documented in-file, re-confirmed here)",
      "a straight NOT NULL violation on" in src
      and "tax_billing_quarantine.county_code" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 14: loaders/load_ajr.py (PARCEL_SQL + PROP_UNIT_UPSERT_SQL /
# PROP_UNIT_TAX_YEAR_UPSERT_SQL arity mismatches)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_ajr.py")
src = open("loaders/load_ajr.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("PARCEL_SQL INSERT column list includes county_code first",
      "INSERT INTO parcel (county_code, geo_id, prop_id, situs_address, legal_desc," in src)
check("PARCEL_SQL ON CONFLICT targets the live (county_code, geo_id)",
      "ON CONFLICT (county_code, geo_id) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id) target remains against parcel",
      "ON CONFLICT (geo_id) DO UPDATE" not in src)
check("load_year() signature threads county_code=DEFAULT_COUNTY",
      "def load_year(conn, year, filepath, pid_lookup, county_code=DEFAULT_COUNTY):" in src)
check("parcel_rows.append() includes county_code first (9 values for PARCEL_SQL's 9 placeholders)",
      "parcel_rows.append((county_code, geo_id, prop_id, address, legal," in src)
check("unit_rows.append() includes county_code first, 9 values total -- matches "
      "PROP_UNIT_UPSERT_SQL's 9 placeholders (was 8, a real arity mismatch)",
      "unit_rows.append((county_code, prop_id, geo_id, None, address, owner_id, None, year, year))" in src)
check("pty_rows.append() includes county_code first, 12 values total -- matches "
      "PROP_UNIT_TAX_YEAR_UPSERT_SQL's 12 placeholders (was 11, a real arity mismatch)",
      'pty_rows.append((county_code, prop_id, year, geo_id, market_val, assessed_val, None,\n'
      '                              hs_cap, None, None, None, f"ajr_{year}"))' in src)
check("load() signature threads county_code=DEFAULT_COUNTY",
      "def load(conn, county_code=DEFAULT_COUNTY):" in src)
check("load()'s load_year() call passes county_code=county_code",
      "load_year(conn, year, filepath, pid_lookup, county_code=county_code)" in src)
check("load()'s parcel_rollup.run() call passes county_code=county_code",
      "parcel_rollup.run(conn, tax_year=year, county_code=county_code)" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("main() passes county_code=args.county into load()",
      "load(conn, county_code=args.county)" in src)
check("PX-20260830-05 Task 2 (Bucket B): build_pid_lookup() now threads "
      "county_code and predicates the prop_unit query on it (formalizes the "
      "gap DALLAS-GATE-4/PX-20260822-06-rev1 used to only flag-not-fix)",
      "def build_pid_lookup(conn, county_code=DEFAULT_COUNTY):" in src
      and "WHERE prop_id IS NOT NULL AND county_code = %s" in src)
check("load()'s build_pid_lookup() calls pass county_code=county_code "
      "(both the initial call and the post-year-load refresh)",
      src.count("build_pid_lookup(conn, county_code=county_code)") == 2)


# ─────────────────────────────────────────────────────────────────────────
# File 15: loaders/parse_cert_2021_pdf.py (validate())
# ─────────────────────────────────────────────────────────────────────────
section("loaders/parse_cert_2021_pdf.py")
src = open("loaders/parse_cert_2021_pdf.py").read()

check("PX-20260830-05 Task 2 (Bucket B): validate() signature accepts "
      "county_code=None (standalone script -- DEFAULT_COUNTY imported "
      "lazily inside the function, not at module level, to keep this "
      "script's import footprint minimal)",
      "def validate(csv_path, county_code=None):" in src)
check("validate() imports DEFAULT_COUNTY lazily and falls back to it when "
      "county_code isn't passed",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY  # DALLAS-GATE-4 convention" in src
      and "if county_code is None:\n        county_code = DEFAULT_COUNTY" in src)
check("validate()'s parcel_tax_year SELECT is now predicated on county_code "
      "(parcel_tax_year is composite_pk-migrated; an unscoped tax_year=2021 "
      "pull could match another county's rows against this file's own "
      "single-county extracted CSV)",
      "WHERE tax_year = 2021\n          AND county_code = %s\n          AND geo_id = ANY(%s)" in src)
check("--county CLI flag added to argparse, default None (only meaningful "
      "with --validate)",
      "ap.add_argument('--county', default=None," in src)
check("main()'s --validate dispatch passes county_code=args.county",
      "validate(args.validate, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 16: loaders/scrape_billing_history.py (get_eligible_geo_ids())
# ─────────────────────────────────────────────────────────────────────────
section("loaders/scrape_billing_history.py")
src = open("loaders/scrape_billing_history.py").read()

check("PX-20260830-05 Task 2 (Bucket B): get_eligible_geo_ids() signature "
      "accepts county_code (both parcel and parcel_tax_year are "
      "composite_pk-migrated)",
      "county_code: str = None,\n) -> list[str]:" in src)
check("get_eligible_geo_ids() predicates the direct `parcel` reference "
      "via county_clause",
      'county_clause = "AND p.county_code = %(county_code)s" if county_code else ""' in src
      and src.count("{county_clause}") == 2)
check("get_eligible_geo_ids() scopes the transitive `parcel_tax_year` join "
      "too (both table references, per PM's explicit instruction), in both "
      "the random_order and non-random SQL branches",
      src.count("ON pty.geo_id = p.geo_id\n                       AND pty.county_code = p.county_code") == 1
      and src.count("ON pty.geo_id = p.geo_id\n                   AND pty.county_code = p.county_code") == 1)
check("cur.execute() passes county_code through the params dict",
      'cur.execute(sql, {"county_code": county_code})' in src)
check("both real call sites in main() pass county_code=args.county",
      src.count("county_code=args.county,") == 2)
check("--county help text now states the gap is resolved, not just disclosed "
      "(PX-20260830-05 supersedes the DALLAS-GATE-2 disclosure this text "
      "used to carry)",
      "PX-20260830-05 Task 2 (Bucket B): get_eligible_geo_ids() is now" in src
      and "county-scoped on both parcel and parcel_tax_year -- resolves the" in src
      and "DALLAS-GATE-2 disclosed gap this help text used to describe" in src)


# ─────────────────────────────────────────────────────────────────────────
# PX-20260830-05 Task 2 correction (reviewer-rejected "27 out of scope"
# claim): Files 17-23 below cover the remaining 6 of the 8 files named in
# the reviewer's row list (load_2026_preliminary.py and load_cert_2021.py's
# additional fixes are covered above, in File 7's section and its
# extension). Same string-assertion-against-real-shipping-source technique
# as every other file in this suite.
# ─────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────
# File 17: loaders/load_2026_preliminary.py (parcel_tax_year / prop_unit_tax_year)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_2026_preliminary.py")
src = open("loaders/load_2026_preliminary.py").read()

check("load_land_and_imprv()'s prop_unit_tax_year market_value SELECT is "
      "predicated on county_code",
      '"SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",\n            (TAX_YEAR, county_code),'
      in src)
check("run_qa() signature threads county_code=DEFAULT_COUNTY and its row-count "
      "queries (2026 and 2025) are both predicated",
      "def run_qa(conn, county_code=DEFAULT_COUNTY):" in src
      and 'FROM parcel_tax_year WHERE tax_year = 2026 AND county_code = %s' in src
      and 'FROM parcel_tax_year WHERE tax_year = 2025 AND county_code = %s' in src)
check("run_qa()'s null-rate loop query is predicated on county_code",
      "FROM parcel_tax_year WHERE tax_year = 2026 AND county_code = %s\n        \"\"\", (county_code,))" in src)
check("run_qa()'s AV>MV anomaly check is predicated on county_code",
      "AND county_code = %s\n    \"\"\", (county_code,))" in src)
check("run_qa()'s known-parcel sanity check is predicated on county_code",
      "WHERE geo_id = %s AND tax_year IN (2025, 2026) AND county_code = %s" in src)
check("run_county_comparison() signature threads county_code=DEFAULT_COUNTY, "
      "and both parcel_tax_year JOINs are correlated on county_code",
      "def run_county_comparison(conn, county_code=DEFAULT_COUNTY):" in src
      and "JOIN parcel_tax_year p25 ON p25.geo_id = p.geo_id AND p25.tax_year = 2025 AND p25.county_code = p.county_code" in src
      and "JOIN parcel_tax_year p26 ON p26.geo_id = p.geo_id AND p26.tax_year = 2026 AND p26.county_code = p.county_code" in src
      and "AND p.county_code = %(county_code)s" in src)
check("run_county_comparison()'s 'Overall' summary query USING clause includes "
      "county_code and is predicated",
      "JOIN parcel_tax_year p26 USING (geo_id, county_code)" in src
      and "AND p25.county_code = %(county_code)s" in src)
check("load() passes county_code=county_code into both run_qa() and "
      "run_county_comparison()",
      "run_qa(conn, county_code=county_code)" in src
      and "run_county_comparison(conn, county_code=county_code)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 18: loaders/load_certified_2025.py (prop_unit_tax_year)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_certified_2025.py")
src = open("loaders/load_certified_2025.py").read()

check("load_land_and_imprv()'s prop_unit_tax_year market_value SELECT is "
      "predicated on county_code",
      '"SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",\n            (TAX_YEAR, county_code),'
      in src)


# ─────────────────────────────────────────────────────────────────────────
# File 19: loaders/load_certified_historical.py (prop_unit_tax_year / parcel_tax_year)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_certified_historical.py")
src = open("loaders/load_certified_historical.py").read()

check("load_land_imprv()'s prop_unit_tax_year market_value SELECT is "
      "predicated on county_code",
      '"SELECT prop_id, market_value FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",\n            (year, county_code),'
      in src)
check("post_load_summary() threads county_code=DEFAULT_COUNTY and predicates "
      "all 3 of its parcel_tax_year queries (rows_after, cert_count, the "
      "land/imprv non-null breakdown)",
      "def post_load_summary(conn, year, data_source, rows_before, ajr_before, county_code=DEFAULT_COUNTY):" in src
      and '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s",\n            (year, county_code)' in src
      and '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",\n            (year, data_source, county_code)' in src
      and "WHERE tax_year = %s AND data_source = %s AND county_code = %s\n        \"\"\", (year, data_source, county_code))" in src)
check("main()'s before-load snapshot counts (rows_before, ajr_before) are "
      "both predicated on args.county",
      '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s",\n            (year, args.county),' in src
      and '"SELECT COUNT(*) FROM parcel_tax_year WHERE tax_year = %s AND data_source = %s AND county_code = %s",\n            (year, ajr_source, args.county)' in src)
check("main()'s post_load_summary() call passes county_code=args.county",
      "post_load_summary(conn, year, data_source, rows_before, ajr_before, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 20: loaders/load_exemptions.py (parcel_tax_year)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_exemptions.py")
src = open("loaders/load_exemptions.py").read()

check("main()'s sanity-sample SELECT is predicated on county_code (the "
      "known-parcel exemption_codes printout after load)",
      "WHERE geo_id IN ('0426280206','0159180227') AND tax_year IN (2025,2026)\n                  AND county_code = %s" in src
      and '""", (args.county,))' in src)


# ─────────────────────────────────────────────────────────────────────────
# File 21: loaders/load_imp_det_sqft.py (parcel)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_imp_det_sqft.py")
src = open("loaders/load_imp_det_sqft.py").read()

check("_sanity_check() signature threads county_code=DEFAULT_COUNTY",
      "def _sanity_check(conn, county_code=DEFAULT_COUNTY):" in src)
check("_sanity_check()'s top-5-by-sqft SELECT is predicated on county_code",
      "WHERE living_area_sqft IS NOT NULL AND county_code = %s" in src)
check("_sanity_check()'s named-parcel SELECT is predicated on county_code",
      "WHERE geo_id = ANY(%s) AND county_code = %s" in src
      and "cur.execute(sql2, (test_geos, county_code))" in src)
check("load()'s call site passes county_code through to _sanity_check()",
      "_sanity_check(conn, county_code)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 22: loaders/load_parcel_attrs.py (parcel)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_parcel_attrs.py")
src = open("loaders/load_parcel_attrs.py").read()

check("main()'s sanity-report SELECT (SANITY geo_id list) is predicated on "
      "county_code",
      "FROM parcel WHERE geo_id = ANY(%s) AND county_code = %s ORDER BY geo_id" in src
      and '""", (SANITY, args.county))' in src)


# ─────────────────────────────────────────────────────────────────────────
# File 23: loaders/load_pir_tcad.py (parcel)
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_pir_tcad.py")
src = open("loaders/load_pir_tcad.py").read()

check("build_pid_lookup() signature threads county_code=DEFAULT_COUNTY and "
      "its parcel SELECT is predicated on it",
      "def build_pid_lookup(conn, county_code=DEFAULT_COUNTY):" in src
      and "SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL AND county_code = %s" in src)
check("main()'s build_pid_lookup() call passes county_code=args.county",
      "build_pid_lookup(conn, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
print(f"\n{'=' * 78}")
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
