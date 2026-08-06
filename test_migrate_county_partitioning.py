#!/usr/bin/env python3
"""
test_migrate_county_partitioning.py — PARTITION-2-IMPLEMENT, Verification
item 1 (+ a real bonus check for Part 5's schema.sql/loader consolidation).

Two layers, per this project's established sandbox-vs-live discipline (no
live Postgres connection here — same disclosure as every other loader test
this project has built):

1. PURE-FUNCTION tests (no DB at all) — proves the DDL-generation and
   reconciliation-LOGIC functions are correct against synthetic
   information_schema-shaped input, exactly the same "prove the logic
   without a live connection" technique refresh_group_stats.py's own
   PERCENTILE_CONT reference reimplementation uses.
2. CONTROL-FLOW tests, via a fake connection/cursor harness (_FakeConn/
   _FakeCursor below) that records every SQL statement actually executed —
   proves migrate_table() genuinely REFUSES to issue any RENAME/swap SQL
   when reconciliation fails (the core, non-negotiable requirement this
   brief's own verification section names), and genuinely DOES swap +
   drop the default when reconciliation passes. This proves the CODE'S
   control flow is right; it does not prove the real SQL text executes
   correctly against real Postgres, or that a real backfill completes in
   reasonable time on real production-scale data — Diego's real run
   (table by table, with real backups first) is what proves that.

Run: python3 test_migrate_county_partitioning.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from migrate_county_partitioning import (
    _column_ddl_type,
    build_create_new_table_sql,
    build_backfill_insert_sql,
    reconcile_counts,
    reconcile_sums,
    reconcile_spot_check,
    migrate_table,
    migrate_add_column_table,
    migrate_default_only_table,
)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 — pure functions, no DB
# ═══════════════════════════════════════════════════════════════════════════

def test_column_ddl_type_variants():
    check("varchar with length", _column_ddl_type(
        {"data_type": "character varying", "udt_name": "varchar", "character_maximum_length": 20}
    ) == "VARCHAR(20)")
    check("numeric with precision/scale", _column_ddl_type(
        {"data_type": "numeric", "udt_name": "numeric", "numeric_precision": 14, "numeric_scale": 2}
    ) == "NUMERIC(14,2)")
    check("bigint via udt_name int8", _column_ddl_type(
        {"data_type": "bigint", "udt_name": "int8"}
    ) == "BIGINT")
    check("boolean via udt_name bool", _column_ddl_type(
        {"data_type": "boolean", "udt_name": "bool"}
    ) == "BOOLEAN")
    check("text passthrough", _column_ddl_type(
        {"data_type": "text", "udt_name": "text"}
    ) == "TEXT")
    check("timestamptz", _column_ddl_type(
        {"data_type": "timestamp with time zone", "udt_name": "timestamptz"}
    ) == "TIMESTAMPTZ")
    try:
        _column_ddl_type({"data_type": "ARRAY", "udt_name": "_text", "column_name": "x"})
        check("ARRAY type raises NotImplementedError", False, "did not raise")
    except NotImplementedError:
        check("ARRAY type raises NotImplementedError", True)


_SYNTH_COLUMNS = [
    {"column_name": "geo_id", "data_type": "character varying", "udt_name": "varchar",
     "character_maximum_length": 20, "is_nullable": "NO", "column_default": None},
    {"column_name": "market_value", "data_type": "bigint", "udt_name": "int8",
     "is_nullable": "YES", "column_default": None},
    {"column_name": "is_delinquent", "data_type": "boolean", "udt_name": "bool",
     "is_nullable": "YES", "column_default": "false"},
]


def test_build_create_new_table_sql_shape():
    sql = build_create_new_table_sql("tax_billing", _SYNTH_COLUMNS, ["county_code", "geo_id"])
    check("CREATE TABLE targets _new", sql.startswith("CREATE TABLE tax_billing_new"), sql)
    check("every real column present", all(c["column_name"] in sql for c in _SYNTH_COLUMNS), sql)
    check("county_code column added", "county_code  VARCHAR(20) NOT NULL DEFAULT 'TRAVIS'" in sql, sql)
    check("composite PK present", "PRIMARY KEY (county_code, geo_id)" in sql, sql)
    check("NOT NULL preserved for geo_id", "geo_id  VARCHAR(20) NOT NULL" in sql, sql)
    check("original column default preserved", "is_delinquent  BOOLEAN DEFAULT false" in sql, sql)

    sql_dallas = build_create_new_table_sql("tax_billing", _SYNTH_COLUMNS, ["county_code", "geo_id"],
                                             county_value="DALLAS")
    check("county_value parameter honored", "DEFAULT 'DALLAS'" in sql_dallas, sql_dallas)


def test_build_create_new_table_sql_raises_on_serial_default():
    serial_columns = [
        {"column_name": "id", "data_type": "bigint", "udt_name": "int8",
         "is_nullable": "NO", "column_default": "nextval('ingest_audit_id_seq'::regclass)"},
    ]
    try:
        build_create_new_table_sql("ingest_audit", serial_columns, ["county_code", "id"])
        check("SERIAL default raises NotImplementedError", False, "did not raise")
    except NotImplementedError:
        check("SERIAL default raises NotImplementedError", True)


def test_build_backfill_insert_sql_shape():
    sql = build_backfill_insert_sql("tax_billing", _SYNTH_COLUMNS)
    check("targets _new table", "INSERT INTO tax_billing_new" in sql, sql)
    check("county_code leads the column list", "(county_code, geo_id, market_value, is_delinquent)" in sql, sql)
    check("SELECT uses %(county_value)s placeholder, not a literal", "%(county_value)s" in sql, sql)
    check("SELECT reads from the OLD table, unqualified", sql.strip().endswith("FROM tax_billing"), sql)
    check("never uses SELECT *", "SELECT *" not in sql, sql)


def test_reconcile_counts():
    ok, detail = reconcile_counts(1000, 1000)
    check("counts match -> pass", ok, detail)
    ok, detail = reconcile_counts(1000, 998)
    check("counts differ -> fail", not ok, detail)
    check("failure reason names both counts", "1,000" in detail["reason"] and "998" in detail["reason"], detail)


def test_reconcile_sums():
    ok, detail = reconcile_sums({"market_value": 500_000, "assessed_value": 400_000},
                                 {"market_value": 500_000, "assessed_value": 400_000})
    check("sums match exactly -> pass", ok, detail)

    ok, detail = reconcile_sums({"market_value": 500_000, "assessed_value": 400_000},
                                 {"market_value": 500_001, "assessed_value": 400_000})
    check("a 1-unit sum drift -> fail (no tolerance, unlike AGGPRECOMP consistency check)", not ok, detail)
    check("mismatch names the exact column", detail["mismatches"][0]["column"] == "market_value", detail)

    ok, detail = reconcile_sums({"x": None}, {"x": None})
    check("both-None sum -> pass (real empty-table case)", ok, detail)


def test_reconcile_spot_check():
    old_rows = [{"geo_id": "A", "market_value": 100}, {"geo_id": "B", "market_value": 200}]
    new_rows = [{"geo_id": "A", "market_value": 100}, {"geo_id": "B", "market_value": 200}]
    ok, detail = reconcile_spot_check(old_rows, new_rows, ["geo_id", "market_value"], ["geo_id"])
    check("identical sampled rows -> pass", ok, detail)

    tampered_new = [{"geo_id": "A", "market_value": 100}, {"geo_id": "B", "market_value": 999}]
    ok, detail = reconcile_spot_check(old_rows, tampered_new, ["geo_id", "market_value"], ["geo_id"])
    check("one tampered value -> fail", not ok, detail)
    check("mismatch names the real column + real key", detail["mismatches"][0]["column"] == "market_value"
          and detail["mismatches"][0]["key"] == {"geo_id": "B"}, detail)

    ok, detail = reconcile_spot_check(old_rows, [old_rows[0], None], ["geo_id", "market_value"], ["geo_id"])
    check("a row missing entirely from <table>_new -> fail", not ok, detail)

    ok, detail = reconcile_spot_check([], [], ["geo_id"], ["geo_id"])
    check("empty sample (e.g. a 0-row table) -> vacuously passes", ok, detail)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 — control flow, via a fake connection/cursor
# ═══════════════════════════════════════════════════════════════════════════

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.executed_sql.append(" ".join(sql.split()))
        result = self.conn.handler(" ".join(sql.split()), params)
        if isinstance(result, Exception):
            raise result
        colnames, rows = result
        self.description = [(c,) for c in colnames] if colnames else None
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, handler):
        self.handler = handler
        self.executed_sql = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


_WIDGET_SPEC = {"name": "widget", "old_pk": ["id"], "new_pk": ["county_code", "id"],
                "fk_drop": [], "fk_add": []}
_WIDGET_COLS_ROWS = [
    ("id", "integer", "int4", None, None, None, "NO", None),
    ("value", "numeric", "numeric", None, 14, 2, "YES", None),
]


def _widget_handler(old_count=3, new_count=3, old_sums=(6, 30), new_sums=(6, 30), tamper_spot_check=False):
    def handler(norm, params):
        if "information_schema.columns" in norm:
            colnames = ["column_name", "data_type", "udt_name", "character_maximum_length",
                        "numeric_precision", "numeric_scale", "is_nullable", "column_default"]
            return colnames, _WIDGET_COLS_ROWS
        if "DROP TABLE IF EXISTS widget_new" in norm:
            return None, []
        if norm.startswith("CREATE TABLE widget_new"):
            return None, []
        if norm.startswith("INSERT INTO widget_new"):
            return None, []
        if "SELECT COUNT(*) FROM widget_new" in norm:
            return None, [(new_count,)]
        if "SELECT COUNT(*) FROM widget" in norm:
            return None, [(old_count,)]
        if "FROM widget_new" in norm and "SUM(id)" in norm:
            return ["id", "value"], [new_sums]
        if "FROM widget" in norm and "SUM(id)" in norm:
            return ["id", "value"], [old_sums]
        if "ORDER BY RANDOM() LIMIT" in norm:
            return ["id"], [(1,), (2,), (3,)]
        if "FROM widget_new WHERE id = %s" in norm:
            val = 999 if tamper_spot_check else 10
            return ["id", "value"], [(params[0], val)]
        if "FROM widget WHERE id = %s" in norm:
            return ["id", "value"], [(params[0], 10)]
        if norm.startswith("ALTER TABLE widget RENAME TO widget_old_pre_partition"):
            return None, []
        if norm.startswith("ALTER TABLE widget_new RENAME TO widget"):
            return None, []
        if "ALTER TABLE widget ALTER COLUMN county_code DROP DEFAULT" in norm:
            return None, []
        if "information_schema.table_constraints" in norm:
            return ["constraint_name"], []
        raise AssertionError(f"unhandled SQL in fake widget handler: {norm!r}")
    return handler


def test_migrate_table_stops_before_swap_on_row_count_mismatch():
    conn = _FakeConn(_widget_handler(old_count=3, new_count=2))
    report = migrate_table(conn, _WIDGET_SPEC, verbose=False)
    check("row-count mismatch: reconciliation_passed is False", report["reconciliation_passed"] is False, report)
    check("row-count mismatch: swapped is False", report["swapped"] is False, report)
    check("row-count mismatch: NO rename SQL was ever issued",
          not any("RENAME TO" in s for s in conn.executed_sql), conn.executed_sql)
    check("row-count mismatch: DROP DEFAULT was never issued",
          not any("DROP DEFAULT" in s for s in conn.executed_sql), conn.executed_sql)


def test_migrate_table_stops_before_swap_on_dollar_sum_mismatch():
    conn = _FakeConn(_widget_handler(old_count=3, new_count=3, old_sums=(6, 30), new_sums=(6, 31)))
    report = migrate_table(conn, _WIDGET_SPEC, verbose=False)
    check("sum mismatch: reconciliation_passed is False", report["reconciliation_passed"] is False, report)
    check("sum mismatch: swapped is False", report["swapped"] is False, report)
    check("sum mismatch: NO rename SQL was ever issued",
          not any("RENAME TO" in s for s in conn.executed_sql), conn.executed_sql)
    check("sum mismatch detail names the real drift",
          report["reconciliation"]["dollar_sums"]["mismatches"][0]["column"] == "value", report)


def test_migrate_table_stops_before_swap_on_spot_check_mismatch():
    conn = _FakeConn(_widget_handler(old_count=3, new_count=3, tamper_spot_check=True))
    report = migrate_table(conn, _WIDGET_SPEC, verbose=False)
    check("spot-check mismatch: reconciliation_passed is False", report["reconciliation_passed"] is False, report)
    check("spot-check mismatch: swapped is False", report["swapped"] is False, report)
    check("spot-check mismatch: NO rename SQL was ever issued",
          not any("RENAME TO" in s for s in conn.executed_sql), conn.executed_sql)


def test_migrate_table_completes_full_migration_when_reconciliation_passes():
    conn = _FakeConn(_widget_handler())
    report = migrate_table(conn, _WIDGET_SPEC, verbose=False)
    check("clean run: reconciliation_passed is True", report["reconciliation_passed"] is True, report)
    check("clean run: swapped is True", report["swapped"] is True, report)
    check("clean run: default_dropped is True", report["default_dropped"] is True, report)
    check("clean run: RENAME TO widget_old_pre_partition issued",
          any("RENAME TO widget_old_pre_partition" in s for s in conn.executed_sql), conn.executed_sql)
    check("clean run: RENAME TO widget (from widget_new) issued",
          any(s.startswith("ALTER TABLE widget_new RENAME TO widget") for s in conn.executed_sql), conn.executed_sql)
    check("clean run: DROP DEFAULT issued AFTER the swap",
          conn.executed_sql.index(next(s for s in conn.executed_sql if "DROP DEFAULT" in s)) >
          conn.executed_sql.index(next(s for s in conn.executed_sql if s.startswith("ALTER TABLE widget_new RENAME TO widget"))),
          conn.executed_sql)
    check("clean run: old_table_retained_as recorded (never dropped by this script)",
          report["old_table_retained_as"] == "widget_old_pre_partition", report)
    check("clean run: no DROP TABLE ...widget_old... ever issued (retained per §5 step 6)",
          not any(s.startswith("DROP TABLE widget_old") for s in conn.executed_sql), conn.executed_sql)


def test_migrate_add_column_table_backfills_and_sets_not_null():
    calls = {"count_calls": 0}

    def handler(norm, params):
        if norm.startswith("ALTER TABLE load_batch ADD COLUMN"):
            return None, []
        if norm.startswith("UPDATE load_batch SET county_code"):
            return None, []
        if norm.startswith("SELECT COUNT(*) FROM load_batch WHERE county_code IS NULL"):
            return None, [(0,)]
        if norm.startswith("ALTER TABLE load_batch ALTER COLUMN county_code SET NOT NULL"):
            return None, []
        if norm == "SELECT COUNT(*) FROM load_batch":
            calls["count_calls"] += 1
            return None, [(5,)]
        raise AssertionError(f"unhandled SQL: {norm!r}")

    conn = _FakeConn(handler)
    report = migrate_add_column_table(conn, "load_batch", verbose=False)
    check("add_column mode: no DEFAULT ever set", not any("DEFAULT" in s for s in conn.executed_sql), conn.executed_sql)
    check("add_column mode: SET NOT NULL issued", any("SET NOT NULL" in s for s in conn.executed_sql), conn.executed_sql)
    check("add_column mode: report reflects success", report["old_row_count"] == 5 and report["new_row_count"] == 5, report)


def test_migrate_default_only_table_refuses_on_unexpected_county_code():
    def handler(norm, params):
        if "WHERE county_code IS NULL OR county_code != %s" in norm:
            return None, [(3,)]  # 3 unexpected rows -- should refuse
        raise AssertionError(f"unhandled SQL: {norm!r}")

    conn = _FakeConn(handler)
    try:
        migrate_default_only_table(conn, "county_benchmark", verbose=False)
        check("default_only mode refuses on unexpected county_code rows", False, "did not raise")
    except RuntimeError as e:
        check("default_only mode refuses on unexpected county_code rows", True, str(e))
    check("default_only mode: DROP DEFAULT never issued when refusing",
          not any("DROP DEFAULT" in s for s in conn.executed_sql), conn.executed_sql)


def test_migrate_default_only_table_drops_default_when_clean():
    def handler(norm, params):
        if "WHERE county_code IS NULL OR county_code != %s" in norm:
            return None, [(0,)]
        if "DROP DEFAULT" in norm:
            return None, []
        raise AssertionError(f"unhandled SQL: {norm!r}")

    conn = _FakeConn(handler)
    report = migrate_default_only_table(conn, "county_benchmark", verbose=False)
    check("default_only mode: drops default when 0 unexpected rows", report["default_dropped"] is True, report)


# ═══════════════════════════════════════════════════════════════════════════
# Part 5 bonus check — schema.sql's tax_billing_quarantine definition stays
# column-for-column identical to loaders/quarantine_contamination.py's own
# defensive copy (promised explicitly in schema.sql's own new comment).
# ═══════════════════════════════════════════════════════════════════════════

def _extract_create_table_columns(sql_text, table_name):
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table_name} \((.*?)\n\);", sql_text, re.DOTALL)
    assert m, f"could not find CREATE TABLE IF NOT EXISTS {table_name} block"
    body = m.group(1)
    col_names = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith("PRIMARY KEY"):
            continue
        col_names.append(line.split()[0])
    return col_names


def test_tax_billing_quarantine_definition_matches_schema_sql_and_loader():
    repo_root = os.path.dirname(__file__)
    with open(os.path.join(repo_root, "schema.sql")) as f:
        schema_sql_text = f.read()
    with open(os.path.join(repo_root, "loaders", "quarantine_contamination.py")) as f:
        loader_text = f.read()

    schema_cols = _extract_create_table_columns(schema_sql_text, "tax_billing_quarantine")
    loader_cols = _extract_create_table_columns(loader_text, "tax_billing_quarantine")

    check("tax_billing_quarantine: schema.sql defines it at all", len(schema_cols) > 0, schema_cols)
    check("tax_billing_quarantine: loader's defensive copy defines it at all", len(loader_cols) > 0, loader_cols)
    check("tax_billing_quarantine: schema.sql and the loader's copy list the SAME columns, in the SAME order "
          "(this is the real regression guard promised in schema.sql's own comment -- catches future drift "
          "between the two copies instead of trusting them to never diverge silently)",
          schema_cols == loader_cols, f"schema.sql={schema_cols}  loader={loader_cols}")


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL migrate_county_partitioning.py TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
