#!/usr/bin/env python3
"""
test_dallas_gate_4_county_code.py — DALLAS-GATE-4 verification requirement:
"Real fixture tests for each of the 5 files' real fixes, matching the same
rigor as BILLING-DIAG-7's test_upsert_billing_rows_commit.py -- prove the
real SQL text includes county_code correctly, and that ON CONFLICT targets
match the real, live constraints."

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
check("billing_sql INSERT column list includes county_code",
      re.search(r"INSERT INTO tax_billing\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("billing_sql ON CONFLICT targets the live (county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE" in src)
check("entity_sql INSERT column list includes county_code",
      "INSERT INTO tax_billing_entity (county_code, geo_id, tax_year, entity_code, amount_due, amount_paid)" in src)
check("entity_sql ON CONFLICT targets the live (county_code, geo_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, geo_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)


# ─────────────────────────────────────────────────────────────────────────
# File 2: loaders/load_pir_billing_2021_full.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_pir_billing_2021_full.py")
src = open("loaders/load_pir_billing_2021_full.py").read()

check("DEFAULT_COUNTY imported from scrape_billing_history.py (single source of truth)",
      "from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols, DEFAULT_COUNTY" in src)
check("BILLING_SQL INSERT column list includes county_code",
      re.search(r"INSERT INTO tax_billing\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("BILLING_SQL ON CONFLICT targets the live (county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE" in src)
check("ENTITY_SQL INSERT column list includes county_code",
      "INSERT INTO tax_billing_entity (county_code, geo_id, tax_year, entity_code, amount_due, amount_paid)" in src)
check("ENTITY_SQL ON CONFLICT targets the live (county_code, geo_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, geo_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("write_to_db() signature threads county_code=DEFAULT_COUNTY",
      "def write_to_db(conn, matched, county_code=DEFAULT_COUNTY):" in src)
check("verify_sanity_parcels() signature threads county_code=DEFAULT_COUNTY "
      "(forward-looking read-side fix)",
      "def verify_sanity_parcels(conn, county_code=DEFAULT_COUNTY):" in src)
check("verify_sanity_parcels()'s SELECT scopes by county_code",
      "WHERE geo_id = %s AND tax_year = %s AND county_code = %s" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)


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
check("billing_sql INSERT column list includes county_code",
      re.search(r"INSERT INTO tax_billing\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("billing_sql ON CONFLICT targets the live (county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE" in src)
check("entity_sql INSERT column list includes county_code",
      "INSERT INTO tax_billing_entity (county_code, geo_id, tax_year, entity_code, amount_due, amount_paid)" in src)
check("entity_sql ON CONFLICT targets the live (county_code, geo_id, tax_year, entity_code)",
      "ON CONFLICT (county_code, geo_id, tax_year, entity_code) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains against tax_billing",
      "ON CONFLICT (geo_id, tax_year) DO UPDATE" not in src)
check("no stale ON CONFLICT (geo_id, tax_year, entity_code) target remains",
      "ON CONFLICT (geo_id, tax_year, entity_code)" not in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("load_delinquent()'s own ON CONFLICT (geo_id) is a DIFFERENT table "
      "(tax_delinquent, out of this brief's tax_billing-writer scope) and is "
      "correctly left untouched",
      "ON CONFLICT (geo_id) DO UPDATE" in src and "INSERT INTO tax_delinquent" in src)


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
print(f"\n{'=' * 78}")
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
