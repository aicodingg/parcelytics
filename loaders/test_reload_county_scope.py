#!/usr/bin/env python3
"""
loaders/test_reload_county_scope.py — PARTITION-2-IMPLEMENT, Verification
item 2.

Proves reload_county_scope()'s real transaction boundary using a fake
connection/cursor that can be configured to raise on either statement --
this is the strongest proof obtainable without a live Postgres instance
(this sandbox has none, same disclosure as every other test this project
has built this week): it proves the CODE correctly delineates the
transaction (commit only after BOTH statements succeed; rollback + re-raise
on ANY failure), which is what makes Postgres's own real atomicity
guarantee apply to this call at all. It does not prove genuine concurrent-
transaction isolation under real production load -- that's Postgres's own,
already-proven behavior, not something a fake connection can meaningfully
exercise.

Run: python3 loaders/test_reload_county_scope.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.reload_county_scope import reload_county_scope, build_county_scope_insert_sql

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        action = self.conn.behavior(" ".join(sql.split()))
        if isinstance(action, Exception):
            raise action
        self.rowcount = action

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, behavior):
        self.behavior = behavior
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_build_county_scope_insert_sql_shape():
    sql = build_county_scope_insert_sql("group_stats", ["county_code", "neighborhood_cd_key", "count"],
                                         "group_stats_dallas_staging")
    check("targets the real table", sql.startswith("INSERT INTO group_stats"), sql)
    check("explicit column list on both sides (never SELECT *)",
          "(county_code, neighborhood_cd_key, count)" in sql and "SELECT county_code, neighborhood_cd_key, count"
          in sql, sql)
    check("reads from the caller-provided staged source",
          sql.strip().endswith("FROM group_stats_dallas_staging"), sql)


def test_reload_county_scope_commits_once_both_statements_succeed():
    def behavior(sql):
        if sql.startswith("DELETE FROM group_stats WHERE county_code"):
            return 42  # rows deleted
        if sql.startswith("INSERT INTO group_stats"):
            return 57  # rows inserted
        raise AssertionError(f"unexpected SQL: {sql!r}")

    conn = _FakeConn(behavior)
    result = reload_county_scope(conn, "group_stats", "DALLAS",
                                  "INSERT INTO group_stats (a) SELECT a FROM staging", verbose=False)

    check("both statements executed in order (DELETE then INSERT)",
          [s for s, _ in conn.executed] == [
              "DELETE FROM group_stats WHERE county_code = %s",
              "INSERT INTO group_stats (a) SELECT a FROM staging",
          ], conn.executed)
    check("commit called exactly once", conn.commits == 1, conn.commits)
    check("rollback never called on the success path", conn.rollbacks == 0, conn.rollbacks)
    check("real n_deleted/n_inserted returned", result["n_deleted"] == 42 and result["n_inserted"] == 57, result)
    check("county_code passed through to the DELETE as a real bound parameter (not string-interpolated)",
          conn.executed[0][1] == ("DALLAS",), conn.executed[0])


def test_reload_county_scope_rolls_back_and_never_commits_when_insert_fails():
    """THE core proof this verification item asks for: if the INSERT half
    fails for any reason, the DELETE's effect must NOT be left standing --
    proven here by asserting commit() was NEVER called (so Postgres's own
    real transaction semantics mean nothing this fake connection did takes
    effect) and rollback() WAS called explicitly."""

    def behavior(sql):
        if sql.startswith("DELETE FROM group_stats WHERE county_code"):
            return 42
        if sql.startswith("INSERT INTO group_stats"):
            return RuntimeError("simulated real failure -- e.g. a constraint violation "
                                 "in the caller-supplied INSERT")
        raise AssertionError(f"unexpected SQL: {sql!r}")

    conn = _FakeConn(behavior)
    try:
        reload_county_scope(conn, "group_stats", "DALLAS",
                             "INSERT INTO group_stats (a) SELECT a FROM staging", verbose=False)
        check("an INSERT failure propagates (is not swallowed)", False, "no exception raised")
    except RuntimeError:
        check("an INSERT failure propagates (is not swallowed)", True)

    check("commit() was NEVER called -- the DELETE's effect is not left standing",
          conn.commits == 0, conn.commits)
    check("rollback() WAS called exactly once", conn.rollbacks == 1, conn.rollbacks)
    check("both statements were still attempted (DELETE ran before the INSERT failed)",
          len(conn.executed) == 2, conn.executed)


def test_reload_county_scope_rolls_back_when_delete_itself_fails():
    """The other real failure mode -- the DELETE itself fails (e.g. a lock
    conflict). The INSERT must never even be attempted, and no commit."""

    def behavior(sql):
        if sql.startswith("DELETE FROM group_stats WHERE county_code"):
            return RuntimeError("simulated lock conflict on DELETE")
        raise AssertionError(f"INSERT should never be attempted if DELETE failed: {sql!r}")

    conn = _FakeConn(behavior)
    try:
        reload_county_scope(conn, "group_stats", "DALLAS",
                             "INSERT INTO group_stats (a) SELECT a FROM staging", verbose=False)
        check("a DELETE failure propagates", False, "no exception raised")
    except RuntimeError:
        check("a DELETE failure propagates", True)

    check("commit() was never called", conn.commits == 0, conn.commits)
    check("rollback() was called exactly once", conn.rollbacks == 1, conn.rollbacks)
    check("the INSERT was never even attempted", len(conn.executed) == 1, conn.executed)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL reload_county_scope.py TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
