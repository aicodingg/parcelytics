#!/usr/bin/env python3
"""
loaders/test_backfill_prop_unit_tax_year_geoid.py — Task M5-PERYEAR-GEOID,
Verification 2. Fixture tests for loaders/backfill_prop_unit_tax_year_geoid.py.

AC8-style disclosure (same pattern as every other fixture-tested module in
this codebase): no live Postgres or real multi-gigabyte source files are
reachable in this sandbox. This file proves two things without either:

  1. build_pid_to_geo_from_prop_txt() correctly re-derives {prop_id: geo_id}
     from a small fixture PROP.TXT (reusing loaders/test_ears_format.py's
     own build_prop_line() fixture builder — the exact fixed-width format
     every real loader parses — not a hand-rolled parallel format).
  2. backfill_year() — the DB-facing half — selects only rows genuinely
     missing a geo_id, matches them correctly against the source map,
     leaves unmatched prop_ids alone, and issues an UPDATE that touches
     ONLY the geo_id column (never market_value/assessed_value/etc, and
     never rows for a different tax_year or a different prop_id) — proven
     against an in-memory fake connection/cursor (no real DB needed) plus
     a fake psycopg2.extras.execute_batch stand-in (psycopg2 itself is not
     installed in this sandbox at all — confirmed via `import psycopg2`
     raising ModuleNotFoundError — so the real one can't be imported here
     even indirectly).

Run: python3 loaders/test_backfill_prop_unit_tax_year_geoid.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.test_ears_format import build_prop_line
from loaders import backfill_prop_unit_tax_year_geoid as bf

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Fake DB layer (no real psycopg2 available in this sandbox at all) ────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._pending_result = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        if sql.strip().upper().startswith("SELECT"):
            self._pending_result = self.conn.select_result

    def fetchall(self):
        return self._pending_result


class FakeConn:
    def __init__(self, select_result):
        self.select_result = select_result
        self.executed = []
        self.committed = False
        self.batch_calls = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True


def _install_fake_psycopg2():
    """
    Registers fake `psycopg2` / `psycopg2.extras` modules in sys.modules so
    backfill_year()'s inline `import psycopg2.extras` (only reached when
    dry_run=False) succeeds in this sandbox, where the real package is not
    installed. execute_batch() here just records what it was asked to
    write, exactly the shape a real caller would pass, so the test can
    assert on it directly.
    """
    fake_extras = types.ModuleType("psycopg2.extras")

    def fake_execute_batch(cur, sql, rows, page_size=2000):
        cur.conn.batch_calls.append((" ".join(sql.split()), list(rows)))

    fake_extras.execute_batch = fake_execute_batch
    fake_pg2 = types.ModuleType("psycopg2")
    fake_pg2.extras = fake_extras
    sys.modules["psycopg2"] = fake_pg2
    sys.modules["psycopg2.extras"] = fake_extras


# ── 1. Source re-derivation (file-parsing half) ───────────────────────────
def test_build_pid_to_geo_from_prop_txt_fixture():
    lines = [
        build_prop_line(prop_id=100, geo_id="0100030105"),
        build_prop_line(prop_id=200, geo_id="0100030106"),
        build_prop_line(prop_id=300, geo_id=""),   # no geo_id -- must be skipped, not KeyError/None-keyed
    ]
    pid_to_geo = bf.build_pid_to_geo_from_prop_txt(prop_txt_lines=lines)
    check("backfill source parse: resolves prop_id 100 -> geo_id from fixture PROP.TXT",
          pid_to_geo.get(100) == "0100030105", pid_to_geo)
    check("backfill source parse: resolves prop_id 200 -> geo_id from fixture PROP.TXT",
          pid_to_geo.get(200) == "0100030106", pid_to_geo)
    check("backfill source parse: prop_id 300 (blank geo_id) not included",
          300 not in pid_to_geo, pid_to_geo)
    check("backfill source parse: exactly 2 resolvable mappings", len(pid_to_geo) == 2, pid_to_geo)


# ── 2. DB-facing half: selection, matching, UPDATE scope ──────────────────
def test_backfill_year_dry_run_matches_and_leaves_unmatched():
    """dry_run=True must never touch psycopg2/commit -- proves the
    select+match logic in isolation from the write path."""
    pid_to_geo = {100: "0100030105", 200: "0100030106"}
    # DB currently has 3 rows for this year still needing geo_id: 100 and
    # 200 (resolvable from source) and 999 (NOT in this year's source file
    # -- e.g. dropped/supplement-only -- must be left alone, not guessed).
    conn = FakeConn(select_result=[(100,), (200,), (999,)])

    result = bf.backfill_year(conn, 2022, pid_to_geo, dry_run=True, verbose=False)

    check("dry-run: matched count is 2 (100 and 200 resolvable)", result["matched"] == 2, result)
    check("dry-run: unmatched count is 1 (999 not in source)", result["unmatched"] == 1, result)
    check("dry-run: updated count is 0 (no writes in dry-run)", result["updated"] == 0, result)
    check("dry-run: never committed", conn.committed is False, conn.committed)
    check("dry-run: never touched psycopg2.extras.execute_batch (no batch_calls)",
          conn.batch_calls == [], conn.batch_calls)


def test_backfill_year_only_selects_rows_missing_geoid():
    """The SELECT must be scoped to this tax_year AND geo_id IS NULL --
    proven by inspecting the executed SQL text and bound params."""
    pid_to_geo = {100: "0100030105"}
    conn = FakeConn(select_result=[(100,)])
    bf.backfill_year(conn, 2023, pid_to_geo, dry_run=True, verbose=False)

    select_calls = [e for e in conn.executed if e[0].upper().startswith("SELECT")]
    check("exactly one SELECT issued", len(select_calls) == 1, conn.executed)
    sql, params = select_calls[0]
    # PX-20260823-02: county_code added to the SELECT's WHERE too (not just
    # the UPDATE) -- pid_to_geo is built from a single county's source
    # file, so the SELECT must not pull other counties' NULL rows either.
    check("SELECT is scoped to tax_year + county_code params",
          params == (2023, bf.DEFAULT_COUNTY), (sql, params))
    check("SELECT filters geo_id IS NULL (never re-scans already-backfilled rows)",
          "geo_id IS NULL" in sql, sql)
    check("SELECT is scoped to county_code (PX-20260823-02)",
          "county_code" in sql, sql)


def test_backfill_year_live_write_touches_only_geoid_for_matched_rows():
    """
    Full (non-dry-run) path via the fake psycopg2 stand-in: proves the
    UPDATE only ever sets geo_id (never market_value/assessed_value/etc,
    the columns land_and_imprv/prop_ent loaders own), and only for rows
    that actually matched the source -- the unmatched prop_id (999) must
    never appear in what gets written.
    """
    _install_fake_psycopg2()
    pid_to_geo = {100: "0100030105", 200: "0100030106"}
    conn = FakeConn(select_result=[(100,), (200,), (999,)])

    result = bf.backfill_year(conn, 2022, pid_to_geo, dry_run=False, verbose=False)

    check("live write: updated count is 2", result["updated"] == 2, result)
    check("live write: committed", conn.committed is True, conn.committed)
    check("live write: exactly one batch UPDATE issued", len(conn.batch_calls) == 1, conn.batch_calls)

    sql, rows = conn.batch_calls[0]
    check("live write: UPDATE_SQL only SETs geo_id (no other column mentioned after SET)",
          "SET geo_id" in sql and "market_value" not in sql and "assessed_value" not in sql
          and "taxable_value" not in sql and "hs_cap_loss" not in sql
          and "land_value" not in sql and "imprv_value" not in sql
          and "exemption_codes" not in sql and "data_source" not in sql,
          sql)
    check("live write: exactly 2 rows written (100, 200) -- 999 excluded (unmatched)",
          len(rows) == 2, rows)
    # PX-20260823-02: county_code appended as UPDATE_SQL's 4th %s (WHERE ...
    # AND county_code = %s), so each written row tuple now carries it too.
    check("live write: row tuples are (geo_id, prop_id, tax_year, county_code), matching UPDATE_SQL's %s order",
          set(rows) == {("0100030105", 100, 2022, bf.DEFAULT_COUNTY),
                        ("0100030106", 200, 2022, bf.DEFAULT_COUNTY)}, rows)
    check("live write: unmatched prop_id 999 never appears in written rows",
          all(r[1] != 999 for r in rows), rows)
    check("live write: UPDATE_SQL is scoped to county_code (PX-20260823-02)",
          "county_code" in sql, sql)


def test_backfill_year_idempotent_second_run_finds_nothing():
    """A second run (simulating: first run already backfilled 100/200, so
    the live DB's SELECT ... WHERE geo_id IS NULL now only returns 999,
    which still can't be resolved) writes nothing new and doesn't error."""
    pid_to_geo = {100: "0100030105", 200: "0100030106"}
    conn = FakeConn(select_result=[(999,)])  # 100/200 no longer NULL, so DB wouldn't return them
    result = bf.backfill_year(conn, 2022, pid_to_geo, dry_run=True, verbose=False)
    check("idempotent re-run: matched 0 (nothing left to backfill from this source)",
          result["matched"] == 0, result)
    check("idempotent re-run: unmatched 1 (999 still unresolvable)", result["unmatched"] == 1, result)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL BACKFILL_PROP_UNIT_TAX_YEAR_GEOID FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
