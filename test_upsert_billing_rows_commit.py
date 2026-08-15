"""
test_upsert_billing_rows_commit.py — BILLING-DIAG-7 verification requirement #1:
"Real fixture test proving the `with conn:` (or equivalent) pattern actually
commits on the real-write path -- matching the existing pattern already used
for the sentinel branch."

Background: BILLING-DIAG-7's own brief claimed `upsert_billing_rows()` (in
loaders/scrape_billing_history.py) was missing a `conn.commit()` call,
citing 79 real production sentinel rows and zero real target-year rows as
evidence. Direct inspection of the real, current source (confirmed via this
same investigation, independently corroborated by a full-codebase audit of
every other write helper) showed this was factually false: the function
already called `conn.commit()` on every real invocation, for both of its
real callers (this file's own CLI batch loop, and app.py's api_billing()
route). See the BILLING-DIAG-7 report for the full correction.

That said, Fable's review is right that "structural correctness over a
remembered commit" is a real, valid improvement regardless -- so
`upsert_billing_rows()` now uses `with conn:` (commits on clean exit, rolls
back automatically on exception) instead of a bare `conn.commit()` call
after the cursor block. This is the ACTUAL function this codebase ships
(sliced straight out of the real file, same technique as
test_api_billing_retry.py), not a reimplementation -- exercised against a
fake connection that reproduces psycopg2's real `with conn:` semantics
(commit() on clean __exit__, rollback() + re-raise on exception).

What this proves:
  1. A successful call commits (fake_conn.commits == 1) and returns the real
     row count (len(records)), not None.
  2. A DB error during the write (execute_batch raises) triggers an
     automatic rollback (fake_conn.rollbacks == 1) and the SAME exception
     propagates out to the caller -- not swallowed, not converted.
  3. An empty records list is a true no-op: returns 0, opens no cursor,
     calls neither commit() nor rollback().

Run: python3 test_upsert_billing_rows_commit.py
"""
import sys

SRC = open("loaders/scrape_billing_history.py").read()

START_MARKER = "\ndef upsert_billing_rows(conn, records: list[dict]) -> int:"
END_MARKER = "\n\n# ── checkpoint "

start = SRC.index(START_MARKER)
end = SRC.index(END_MARKER, start)
FUNC_SRC = SRC[start:end]

assert "def upsert_billing_rows" in FUNC_SRC
assert "with conn:" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-7 `with conn:` pattern"
assert "return len(records)" in FUNC_SRC, "sanity: slice must contain the real row-count return"


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    """Reproduces psycopg2's real `with conn:` transaction-block semantics:
    commit() on a clean __exit__, rollback() (then re-raise, since __exit__
    returns False/None) on an exception."""
    def __init__(self, boom_exc=None):
        self.commits = 0
        self.rollbacks = 0
        self.cursor_opened = False
        self.boom_exc = boom_exc

    def cursor(self, *a, **kw):
        self.cursor_opened = True
        return FakeCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False  # never suppress — real psycopg2 re-raises


def run_upsert(records, boom_exc=None):
    execute_batch_calls = []

    def fake_execute_batch(cur, sql, records, page_size=500):
        execute_batch_calls.append((sql, records, page_size))
        if boom_exc is not None:
            raise boom_exc

    namespace = {
        "psycopg2": type("FakePsycopg2", (), {
            "extras": type("FakeExtras", (), {"execute_batch": staticmethod(fake_execute_batch)})
        })(),
        "_UPSERT_SQL": "INSERT INTO tax_billing ...",  # real constant not needed for this test
    }
    exec(compile(FUNC_SRC, "scrape_billing_history.py (sliced upsert_billing_rows)", "exec"), namespace)

    fake_conn = FakeConn()
    exc_raised = None
    result = None
    try:
        result = namespace["upsert_billing_rows"](fake_conn, records)
    except Exception as exc:
        exc_raised = exc

    return {
        "result": result,
        "exc_raised": exc_raised,
        "commits": fake_conn.commits,
        "rollbacks": fake_conn.rollbacks,
        "cursor_opened": fake_conn.cursor_opened,
        "execute_batch_calls": execute_batch_calls,
    }


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


all_ok = True
RECORDS = [
    {"geo_id": "0100030105", "tax_year": 2021, "total_tax": 64459.78, "total_paid": 64459.78, "county_code": "TRAVIS"},
    {"geo_id": "0100030105", "tax_year": 2022, "total_tax": 62522.55, "total_paid": 62522.55, "county_code": "TRAVIS"},
]

# ── 1. Successful write: commits, returns the real row count ────────────────
print("Test 1: successful upsert — commits via `with conn:`, returns real row count")
r = run_upsert(RECORDS)
all_ok &= check("no exception raised", r["exc_raised"] is None)
all_ok &= check("execute_batch called exactly once", len(r["execute_batch_calls"]) == 1)
all_ok &= check("execute_batch called with the real records list", r["execute_batch_calls"][0][1] == RECORDS)
all_ok &= check("conn.commit() called exactly once", r["commits"] == 1)
all_ok &= check("conn.rollback() NEVER called on the success path", r["rollbacks"] == 0)
all_ok &= check("returns len(records), not None — the real row count `written` that api_billing() "
                "now logs and Fable's review specifically asked for", r["result"] == 2)

# ── 2. DB error during the write: auto-rollback, real exception propagates ──
print("Test 2: execute_batch raises — `with conn:` auto-rolls-back, exception is NOT swallowed")
boom = RuntimeError("server closed the connection unexpectedly")
r = run_upsert(RECORDS, boom_exc=boom)
all_ok &= check("the SAME real exception propagates to the caller (not swallowed, not converted)",
                r["exc_raised"] is boom)
all_ok &= check("conn.rollback() called exactly once (automatic, via `with conn:`)", r["rollbacks"] == 1)
all_ok &= check("conn.commit() NEVER called on the error path", r["commits"] == 0)

# ── 3. Empty records: true no-op, no cursor, no commit, no rollback ─────────
print("Test 3: empty records list — short-circuits before opening any cursor or transaction")
r = run_upsert([])
all_ok &= check("returns 0", r["result"] == 0)
all_ok &= check("no exception", r["exc_raised"] is None)
all_ok &= check("execute_batch never called", r["execute_batch_calls"] == [])
all_ok &= check("no cursor ever opened", r["cursor_opened"] is False)
all_ok &= check("no commit call", r["commits"] == 0)
all_ok &= check("no rollback call", r["rollbacks"] == 0)

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
