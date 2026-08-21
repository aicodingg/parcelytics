#!/usr/bin/env python3
"""
test_parcel_rollup_hotfix_1.py — PARCEL-ROLLUP-HOTFIX-1 verification.

Real, direct string/regex assertions against the actual shipping source of
parcel_rollup.py, loaders/ears_format.py, and ears_format.py's three real
callers (loaders/load_certified_2025.py, loaders/load_2026_preliminary.py,
loaders/load_certified_historical.py) -- same technique and rigor as
test_dallas_gate_4_county_code.py: every assertion below reads the actual
file on disk, not a reimplementation or a copy-pasted expectation. If the
real source drifts, these tests fail.

Background: verify_county_scoping.py's own real, live run against this
repo (MC2-BUILD-1) found parcel_rollup.py and loaders/ears_format.py had
ZERO county_code awareness anywhere, despite writing to parcel_tax_year /
parcel / prop_unit / prop_unit_tax_year -- all real, already-migrated,
county_code-leading-PK tables in production. Confirmed NOT (yet) silently
corrupting data (county_code is NOT NULL, no default, and zero live rows
are NULL, meaning these writers hadn't actually run to completion since
the migration finished) but the next real run would hard-fail on the NOT
NULL violation. This suite proves both files (and every real caller of
ears_format.py's shared SQL) now carry the correct column lists and
ON CONFLICT targets, and that no stale pre-migration reference survives.

Run: python3 test_parcel_rollup_hotfix_1.py
"""
import re

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
# File 1: parcel_rollup.py
# ─────────────────────────────────────────────────────────────────────────
section("parcel_rollup.py")
src = open("parcel_rollup.py").read()

check("DEFAULT_COUNTY imported from loaders.scrape_billing_history (not redeclared)",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("ROLLUP_SQL: base CTE scoped by y.county_code = %(county_code)s",
      re.search(r"WHERE y\.tax_year = %\(tax_year\)s\s*\n\s*AND y\.county_code = %\(county_code\)s", src) is not None)
check("ROLLUP_SQL: u/y join carries county_code (no cross-county false join)",
      "LEFT JOIN prop_unit u ON u.prop_id = y.prop_id AND u.county_code = y.county_code" in src)
check("ROLLUP_SQL: INSERT column list includes county_code first",
      re.search(r"INSERT INTO parcel_tax_year\s*\n\s*\(county_code, geo_id, tax_year", src) is not None)
check("ROLLUP_SQL: county_code passed as a literal SELECT value",
      "SELECT %(county_code)s, base.geo_id, base.tax_year" in src)
check("ROLLUP_SQL: ON CONFLICT targets the live (county_code, geo_id, tax_year)",
      "ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE" in src)
check("no stale ON CONFLICT (geo_id, tax_year) target remains",
      "ON CONFLICT (geo_id, tax_year)" not in src)
check("DISTINCT_YEARS_SQL scoped by county_code",
      "WHERE county_code = %(county_code)s" in src and "SELECT DISTINCT tax_year FROM prop_unit_tax_year" in src)
check("PROP_ID_REPAIR_SQL: subquery's own MIN(prop_id) scoped by county_code",
      re.search(r"FROM prop_unit\s*\n\s*WHERE county_code = %\(county_code\)s\s*\n\s*GROUP BY geo_id", src) is not None)
check("PROP_ID_REPAIR_SQL: outer UPDATE also scoped by p.county_code",
      "AND p.county_code = %(county_code)s" in src)
check("rollup_tax_year() threads county_code=DEFAULT_COUNTY",
      "def rollup_tax_year(conn, tax_year, county_code=DEFAULT_COUNTY):" in src)
check("distinct_tax_years() threads county_code=DEFAULT_COUNTY",
      "def distinct_tax_years(conn, county_code=DEFAULT_COUNTY):" in src)
check("rollup_all_years() threads county_code=DEFAULT_COUNTY",
      "def rollup_all_years(conn, county_code=DEFAULT_COUNTY):" in src)
check("repair_prop_id() threads county_code=DEFAULT_COUNTY",
      "def repair_prop_id(conn, county_code=DEFAULT_COUNTY):" in src)
check("run() threads county_code=DEFAULT_COUNTY",
      "def run(conn, tax_year=None, county_code=DEFAULT_COUNTY):" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)
check("CLI passes args.county through to run()",
      "result = run(conn, tax_year=args.year, county_code=args.county)" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 2: loaders/ears_format.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/ears_format.py")
src = open("loaders/ears_format.py").read()

check("PROP_UNIT_UPSERT_SQL: INSERT column list includes county_code first",
      re.search(r"INSERT INTO prop_unit\s*\n\s*\(county_code, prop_id, geo_id", src) is not None)
check("PROP_UNIT_UPSERT_SQL: VALUES has 9 placeholders (county_code added)",
      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)" in src)
check("PROP_UNIT_UPSERT_SQL: ON CONFLICT targets the live (county_code, prop_id)",
      "ON CONFLICT (county_code, prop_id) DO UPDATE" in src)
check("no stale ON CONFLICT (prop_id) DO UPDATE target remains",
      "ON CONFLICT (prop_id) DO UPDATE" not in src)
check("PROP_UNIT_TAX_YEAR_UPSERT_SQL: INSERT column list includes county_code first",
      re.search(r"INSERT INTO prop_unit_tax_year\s*\n\s*\(county_code, prop_id, tax_year, geo_id", src) is not None)
check("PROP_UNIT_TAX_YEAR_UPSERT_SQL: VALUES has 12 placeholders (county_code added)",
      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)" in src)
check("PROP_UNIT_TAX_YEAR_UPSERT_SQL: ON CONFLICT targets the live (county_code, prop_id, tax_year)",
      "ON CONFLICT (county_code, prop_id, tax_year) DO UPDATE" in src)
check("no stale ON CONFLICT (prop_id, tax_year) DO UPDATE target remains",
      "ON CONFLICT (prop_id, tax_year) DO UPDATE" not in src)


# ─────────────────────────────────────────────────────────────────────────
# File 3: loaders/load_certified_2025.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_certified_2025.py")
src = open("loaders/load_certified_2025.py").read()

check("DEFAULT_COUNTY imported from loaders.scrape_billing_history",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("load_prop_txt() threads county_code=DEFAULT_COUNTY",
      "def load_prop_txt(conn, cert_dir, county_code=DEFAULT_COUNTY):" in src)
check("unit_rows tuple carries county_code first",
      "unit_rows.append((county_code, rec[\"prop_id\"], rec[\"geo_id\"]" in src)
check("load_prop_ent_txt() threads county_code=DEFAULT_COUNTY",
      "def load_prop_ent_txt(conn, cert_dir, pid_to_geo, county_code=DEFAULT_COUNTY):" in src)
check("rows_to_insert tuple carries county_code first",
      re.search(r"rows_to_insert\.append\(\(\s*\n\s*county_code,", src) is not None)
check("load() threads county_code=DEFAULT_COUNTY",
      "def load(conn, county_code=DEFAULT_COUNTY):" in src)
check("load() passes county_code to load_prop_txt()",
      "load_prop_txt(conn, cert_dir, county_code=county_code)" in src)
check("load() passes county_code to load_prop_ent_txt()",
      "load_prop_ent_txt(conn, cert_dir, pid_to_geo, county_code=county_code)" in src)
check("load() passes county_code to parcel_rollup.run()",
      "result = parcel_rollup.run(conn, tax_year=TAX_YEAR, county_code=county_code)" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)


# ─────────────────────────────────────────────────────────────────────────
# File 4: loaders/load_2026_preliminary.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_2026_preliminary.py")
src = open("loaders/load_2026_preliminary.py").read()

check("DEFAULT_COUNTY imported from loaders.scrape_billing_history",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("load_prop_txt() threads county_code=DEFAULT_COUNTY",
      "def load_prop_txt(conn, county_code=DEFAULT_COUNTY):" in src)
check("unit_rows tuple carries county_code first",
      "unit_rows.append((county_code, rec[\"prop_id\"], rec[\"geo_id\"]" in src)
check("load_prop_ent_txt() threads county_code=DEFAULT_COUNTY",
      "def load_prop_ent_txt(conn, pid_to_geo, county_code=DEFAULT_COUNTY):" in src)
check("rows_to_insert tuple carries county_code first",
      re.search(r"rows_to_insert\.append\(\(\s*\n\s*county_code,", src) is not None)
check("load() threads county_code=DEFAULT_COUNTY",
      "def load(conn, skip_qa=False, county_code=DEFAULT_COUNTY):" in src)
check("load() passes county_code to load_prop_txt()",
      "load_prop_txt(conn, county_code=county_code)" in src)
check("load() passes county_code to load_prop_ent_txt()",
      "load_prop_ent_txt(conn, pid_to_geo, county_code=county_code)" in src)
check("load() passes county_code to parcel_rollup.run()",
      "result = parcel_rollup.run(conn, tax_year=TAX_YEAR, county_code=county_code)" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      '"--county", default=DEFAULT_COUNTY' in src)


# ─────────────────────────────────────────────────────────────────────────
# File 5: loaders/load_certified_historical.py
# ─────────────────────────────────────────────────────────────────────────
section("loaders/load_certified_historical.py")
src = open("loaders/load_certified_historical.py").read()

check("DEFAULT_COUNTY imported from loaders.scrape_billing_history",
      "from loaders.scrape_billing_history import DEFAULT_COUNTY" in src)
check("load_prop_unit() threads county_code=DEFAULT_COUNTY",
      "def load_prop_unit(conn, cert_dir, year, county_code=DEFAULT_COUNTY):" in src)
check("unit_rows tuple carries county_code first",
      "unit_rows.append((county_code, rec[\"prop_id\"], rec[\"geo_id\"]" in src)
check("load_prop_ent() threads county_code=DEFAULT_COUNTY",
      "def load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo, county_code=DEFAULT_COUNTY):" in src)
check("rows_to_insert tuple carries county_code first",
      re.search(r"rows_to_insert\.append\(\(\s*\n\s*county_code,", src) is not None)
check("main() passes args.county to load_prop_unit()",
      "load_prop_unit(conn, cert_dir, year, county_code=args.county)" in src)
check("main() passes args.county to load_prop_ent()",
      "load_prop_ent(conn, cert_dir, year, data_source, pid_to_geo, county_code=args.county)" in src)
check("main() passes args.county to parcel_rollup.run()",
      "result = parcel_rollup.run(conn, tax_year=year, county_code=args.county)" in src)
check("--county CLI flag added, default DEFAULT_COUNTY",
      "'--county', default=DEFAULT_COUNTY" in src)


# ─────────────────────────────────────────────────────────────────────────
# File 6: loaders/run_all.py — the real production call site
# ─────────────────────────────────────────────────────────────────────────
section("loaders/run_all.py")
src = open("loaders/run_all.py").read()

check("parcel_rollup.run() call site threads county_code explicitly",
      'rollup_result = parcel_rollup.run(conn, county_code="TRAVIS")' in src)


# ── Summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("ALL PASS" if all_ok else "SOME CHECKS FAILED")
print("=" * 78)

if __name__ == "__main__":
    import sys
    sys.exit(0 if all_ok else 1)
