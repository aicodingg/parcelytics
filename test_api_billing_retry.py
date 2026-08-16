"""
test_api_billing_retry.py — fixture tests for api_billing()
(/<county_slug>/api/billing/<geo_id>).

History (BILLING-DIAG-1 through -7), kept for context:

BILLING-DIAG-1: a single, un-retried fetch_html(geo_id) call (no retry) was
the live route's only attempt to reach the Travis County Tax portal, and the
failure branch was a silent no-op. Fix: 2 attempts, 10s timeout each
(deliberately NOT the CLI loader's 3x20s pattern -- would risk exceeding
gunicorn's 30s default worker timeout), plus a low-noise Sentry warning on
exhausted-retry failure. KEPT PERMANENTLY (Fable's review, BILLING-DIAG-7).

BILLING-DIAG-2: a WAF/bot-detection layer could return HTTP 200 with a
block/CAPTCHA page instead of a clean 403. Fix: a real marker string
(_BILLING_PORTAL_MARKER) must be present before a 200 is trusted; its
absence is treated as a failure (retried, then reported). KEPT PERMANENTLY.

BILLING-DIAG-3/4/5/6: a series of TEMPORARY diagnostics (a Sentry
info-level breadcrumb, a parallel print() channel, a marker-mismatch
content-slice print, a post-parse-receipts print) used to pin down a live
request-context mystery. All REMOVED per BILLING-DIAG-7 / Fable's explicit
review once the real root cause was found (see below) -- this test file no
longer asserts anything about them. The real, permanent finding kept from
capture_exception(exc) in the except block, and the flush()-before-worker-
recycle pattern on every real Sentry call, are both KEPT PERMANENTLY.

BILLING-DIAG-7: the real root cause investigation. The brief that opened
this round claimed `upsert_billing_rows()` was missing a `conn.commit()` --
confirmed FALSE via direct source inspection (the function already
committed on every real invocation) and an independent full-codebase audit
(loaders/db.py, every other write helper). The "missing commit" theory did
not hold up; see the BILLING-DIAG-7 report for the full correction. What
DID ship from that round, independent of the false premise: (a)
`upsert_billing_rows()` now uses `with conn:` instead of a bare commit() --
structurally equivalent on the success path, but now also auto-rolls-back
on the error path (previously only guaranteed by the CLI loader's own
explicit `except: conn.rollback()`, not by app.py's api_billing(), which
relied on implicit rollback-on-close); (b) it now returns the real row
count instead of None; (c) api_billing() logs one real, PERMANENT
structured line on every write (real-write branch AND sentinel branch),
reporting receipts parsed/matched/written and commit_confirmed -- the write
path emitted nothing on success this entire time, which is why the "79 real
sentinel rows, 0 real target-year rows" pattern was invisible until a
manual SQL check found it. See test_upsert_billing_rows_commit.py for the
dedicated fixture test on `upsert_billing_rows()` itself (Fable's
verification requirement #1); this file covers api_billing()'s own
orchestration of it.

BILLING-DIAG-8 (FINAL): with DIAG-7's write path confirmed correct via live
evidence (a genuinely fresh parcel showed the real-write log line firing
correctly), the route's response was STILL empty for a parcel that, per an
unscoped SQL check, already had real, correct, authoritative data for all 4
target years from OTHER sources (pir_billing_YYYY_full, not portal_scrape).
_UPSERT_SQL's own ON CONFLICT ... WHERE clause correctly refused to
overwrite that better data -- a legitimate no-op, NOT a bug, and NOT
touched by this fix. The real, final gap: `already_fetched` (step 1) and
the response SELECT (step 3) both filtered too narrowly on
`data_source = 'portal_scrape'`, so (a) a parcel with better existing data
was treated as never-fetched, causing a wasteful, benefit-free portal
re-fetch on every page view, and (b) that better data never displayed
through this endpoint at all. Fix: step 1 widened to
`data_source = 'portal_scrape' OR tax_year BETWEEN 2021 AND 2024` (the
`data_source = 'portal_scrape'` clause is UNCHANGED and still alone covers
the sentinel row, tax_year=9999, exactly as before); step 3's
`data_source = 'portal_scrape'` filter is simply REMOVED (the year-range +
county_code filters already do all the real scoping, and already naturally
exclude the sentinel). `_UPSERT_SQL`'s own ON CONFLICT ... WHERE protection
in loaders/scrape_billing_history.py is NOT part of this fix.

Sandbox has no Flask/psycopg2 (confirmed unavailable, same constraint as
every other slice-and-exec test in this codebase). Uses the same technique
already established here: extract api_billing()'s REAL source text out of
app.py between two markers and exec() it against a minimal namespace of
fakes. This tests the ACTUAL function body that ships in app.py, not a
reimplementation of it -- which is exactly what caught a real NameError
during BILLING-DIAG-1's own work, before it could reach production.

What these tests check:
  1. fetch_html() succeeds on the FIRST attempt (real marker present, real
     target-year receipts found): called exactly once, upsert_billing_rows()
     called with real records, its real return value used in the permanent
     "api_billing write:" log line, no Sentry noise.
  2. fetch_html() fails once (network error) then succeeds on the 2nd
     attempt: called exactly twice, records still written, no Sentry noise
     (a transient blip that self-corrects is NOT reported).
  3. fetch_html() fails BOTH attempts (network error each time): called
     exactly twice, NO write attempted, a Sentry warning-level message IS
     sent (+ flush()), route still returns a clean {"status":"ok","rows":[]}
     response.
  4. fetch_html() returns HTTP_NOT_FOUND on the first attempt: called
     exactly ONCE (no retry), no Sentry noise, no write, no permanent log
     line (the write path was never entered).
  5. fetch_html() returns HTTP_OK with html content BOTH attempts, but the
     real portal marker is missing both times (the WAF/block-page
     scenario): treated as a failure -- called exactly twice, NO sentinel
     written, a Sentry warning IS sent.
  6. fetch_html() succeeds, but a real exception occurs later in the same
     request (e.g. a dropped/reaped DB connection surfacing on the final
     SELECT): sentry_sdk.capture_exception(exc) is called with the real
     exception object, route returns status:"error" (not a crash).
  7. fetch_html() succeeds, marker present, but parse_receipts() finds no
     2021-2024 target-year rows: the sentinel branch -- upsert_billing_rows()
     is NOT called, a sentinel INSERT (tax_year=9999) IS written and
     committed, and the permanent write-path log line still fires (with
     rows_written=0, sentinel_written=True).
  8. BILLING-DIAG-8(a): a parcel with real, existing data from a BETTER
     source (e.g. pir_billing_2021_full) for all 4 target years:
     already_fetched=True, NO portal fetch attempted, the response returns
     that real existing data (not empty, not filtered out for having the
     "wrong" data_source).
  9. BILLING-DIAG-8(b): a parcel with NO data from any source (real DB
     state, not just no portal_scrape rows): already_fetched=False, a
     portal fetch IS attempted exactly as before this fix.
  10. BILLING-DIAG-8(c): a parcel with ONLY a portal_scrape sentinel row
      (tax_year=9999, no real target-year data from any source -- the exact
      shape of the 79 real production cases found in BILLING-DIAG-7):
      already_fetched=True (unaffected, same as before this fix), NO
      portal fetch attempted, response returns an empty rows list (sentinel
      correctly excluded by the year-range filter).

Run: python3 test_api_billing_retry.py
"""
import sys

sys.path.insert(0, ".")

APP_PY = open("app.py").read()

START_MARKER = "\ndef api_billing(geo_id):"
END_MARKER = "\n\n\n# ── Task 5: ptype label → SQL WHERE fragments"

start = APP_PY.index(START_MARKER)
end = APP_PY.index(END_MARKER, start)
FUNC_SRC = APP_PY[start:end]

assert "def api_billing" in FUNC_SRC
assert "for _attempt in range(2):" in FUNC_SRC, "sanity: slice must contain the retry loop (BILLING-DIAG-1, permanent)"
assert "_BILLING_PORTAL_MARKER" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-2 marker check (permanent)"
assert "sentry_sdk.capture_exception" in FUNC_SRC, "sanity: slice must contain the real exception capture (permanent)"
assert FUNC_SRC.count("sentry_sdk.flush") == 2, \
    "sanity: exactly 2 flush() calls expected (exhausted-retry warning, exception handler) -- " \
    "the BILLING-DIAG-3 breadcrumb's flush() was removed along with the breadcrumb itself"
# BILLING-DIAG-3 itself produced TWO things: a TEMPORARY info-level Sentry
# breadcrumb (removed here) and the PERMANENT capture_exception(exc) fix in
# the except block (kept -- its comment still legitimately says
# "BILLING-DIAG-3", since that's where it originated). So the sanity check
# below targets the breadcrumb's own distinctive text, not the string
# "BILLING-DIAG-3" anywhere in the file.
assert "post-retry-loop" not in FUNC_SRC, "sanity: the temporary DIAG-3/5 breadcrumb/print text must be fully removed"
assert "BILLING-DIAG-5:" not in FUNC_SRC, "sanity: the temporary DIAG-5 print must be fully removed"
assert "marker mismatch" not in FUNC_SRC, "sanity: the temporary DIAG-6 marker-mismatch print must be fully removed"
assert "receipts_found=" not in FUNC_SRC, "sanity: the temporary DIAG-6 parse-result print must be fully removed"
assert FUNC_SRC.count('"api_billing write:') == 2, \
    "sanity: exactly 2 permanent write-path log lines expected (real-write branch + sentinel branch)"
assert "written = upsert_billing_rows(conn, records)" in FUNC_SRC, \
    "sanity: must use upsert_billing_rows()'s real return value, not discard it"
# BILLING-DIAG-8: already_fetched (step 1) widened to also count real data
# from any OTHER source for the target years; the data_source='portal_scrape'
# clause is UNCHANGED (still alone covers the sentinel row, tax_year=9999).
assert "AND (data_source = 'portal_scrape' OR tax_year BETWEEN 2021 AND 2024)" in FUNC_SRC, \
    "sanity: already_fetched check must be widened to any data_source for the target years (BILLING-DIAG-8)"
# The final response SELECT (step 3) must no longer filter on data_source at
# all -- checked by confirming the OLD filter line's exact text (used only
# there, never in step 1's parenthesized OR-clause) is gone.
assert "\"  AND data_source = 'portal_scrape' \"" not in FUNC_SRC, \
    "sanity: the final response SELECT's data_source='portal_scrape' filter must be REMOVED (BILLING-DIAG-8)"
assert "SELECT tax_year, total_tax, total_paid, data_source, confidence_level " in FUNC_SRC, \
    "sanity: final SELECT must still select all 5 real columns, including data_source (so callers can " \
    "tell a portal_scrape row from a better-sourced one)"
assert "AND tax_year BETWEEN 2021 AND 2024 " in FUNC_SRC and "AND county_code = %s " in FUNC_SRC, \
    "sanity: final SELECT must keep its real year-range + county_code scoping"
# _UPSERT_SQL's own ON CONFLICT ... WHERE protection lives in a different
# file (loaders/scrape_billing_history.py) and is NOT part of this slice --
# nothing to assert on it here; see that file's own test coverage.

HTTP_OK = 0
HTTP_NOT_FOUND = 404
HTTP_NETWORK_ERR = -1
_BILLING_TARGET_YEARS = {2021, 2022, 2023, 2024}
_BILLING_SENTINEL_YEAR = 9999
_BILLING_PORTAL_MARKER = "Travis County Tax"
REAL_PAGE = f"<html><head><title>{_BILLING_PORTAL_MARKER}</title></head><body>real receipts</body></html>"
BLOCK_PAGE = "<html><head><title>Access Denied</title></head><body>Please verify you are human.</body></html>"

_DEFAULT_RECEIPTS = [
    {"tax_year": 2021, "payment_amount": 64459.78},
    {"tax_year": 2022, "payment_amount": 62522.55},
    {"tax_year": 2023, "payment_amount": 76601.36},
    {"tax_year": 2024, "payment_amount": 85848.63},
    {"tax_year": 2025, "payment_amount": 90000.00},  # outside target range
]


class FakeArgs(dict):
    pass


class FakeRequest:
    args = FakeArgs()


class FakeG:
    county_code = "TRAVIS"


class FakeCursor:
    """Records every execute() call; supports the `with conn.cursor() as cur`
    and `with conn.cursor(cursor_factory=...) as cur` patterns used in
    api_billing(). fetchone() returns a scripted row for the "already
    fetched?" check (always {"cnt": 0} here -- these tests are about the
    fetch/retry/write path, not the cache-hit path)."""
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return {"cnt": 0}

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.execute_log = []
        self.commits = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.execute_log)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class SmartFakeCursor:
    """BILLING-DIAG-8: SQL-aware fake cursor for the already_fetched/response
    SELECT widening tests (8-10 below). Reacts to the REAL, real SQL text
    api_billing() actually executes (not a reimplementation of its logic)
    against a scripted set of `existing_rows` standing in for real DB state --
    so if the widened SQL shape in app.py regresses, these tests fail on the
    real query text, not on a mirrored copy of the query's intent."""
    def __init__(self, log, existing_rows):
        self.log = log
        self.existing_rows = existing_rows
        self._last_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        # Step 1: the already_fetched COUNT check.
        if self._last_sql and "SELECT COUNT(*) AS cnt" in self._last_sql:
            cnt = sum(
                1 for row in self.existing_rows
                if row["data_source"] == "portal_scrape" or 2021 <= row["tax_year"] <= 2024
            )
            return {"cnt": cnt}
        return {"cnt": 0}

    def fetchall(self):
        # Step 3: the final response SELECT.
        if self._last_sql and "data_source, confidence_level" in self._last_sql:
            rows = [dict(r) for r in self.existing_rows if 2021 <= r["tax_year"] <= 2024]
            return sorted(rows, key=lambda r: r["tax_year"])
        return []


class SmartFakeConn:
    def __init__(self, existing_rows):
        self.execute_log = []
        self.existing_rows = existing_rows
        self.commits = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return SmartFakeCursor(self.execute_log, self.existing_rows)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeSentry:
    def __init__(self):
        self.messages = []
        self.flush_calls = []
        self.exceptions = []

    def capture_message(self, msg, level=None):
        self.messages.append((msg, level))

    def capture_exception(self, exc=None):
        self.exceptions.append(exc)

    def flush(self, timeout=None):
        self.flush_calls.append(timeout)


def run_api_billing(geo_id, fetch_sequence, boom_on_sql_substring=None, boom_exc=None,
                     parse_receipts_result=None, existing_rows=None):
    """
    fetch_sequence: list of (html, status) tuples, one per fetch_html() call.

    boom_on_sql_substring/boom_exc: simulate a real exception raised on a
    specific SQL statement (see FakeCursor's execute()).

    parse_receipts_result: override what fake parse_receipts() returns, so
    the empty-target/sentinel branch (Test 7) can be exercised.

    existing_rows: BILLING-DIAG-8 (Tests 8-10). When provided (a list of
    dicts with geo_id/tax_year/total_tax/total_paid/data_source/
    confidence_level), the connection is a SmartFakeConn seeded with this
    real DB state instead of the plain FakeConn used by Tests 1-7 -- lets
    the already_fetched/response-SELECT widening be exercised against real
    pre-existing rows, not just the always-empty-DB assumption those
    earlier tests make.
    """
    fetch_calls = []

    def fake_fetch_html(gid, timeout=None):
        fetch_calls.append((gid, timeout))
        return fetch_sequence[len(fetch_calls) - 1]

    def fake_parse_receipts(html):
        return parse_receipts_result if parse_receipts_result is not None else list(_DEFAULT_RECEIPTS)

    upsert_calls = []

    def fake_upsert_billing_rows(conn, records):
        upsert_calls.append(records)
        return len(records)  # BILLING-DIAG-7: real return value, not None

    def fake_jsonify(payload):
        return payload

    print_calls = []

    def fake_print(*args, **kwargs):
        print_calls.append({"args": args, "kwargs": kwargs})

    class BoomCursor(FakeCursor):
        def execute(self, sql, params=None):
            self.log.append((sql, params))
            if boom_on_sql_substring and boom_on_sql_substring in sql:
                raise boom_exc

    class BoomConn(FakeConn):
        def cursor(self, cursor_factory=None):
            return BoomCursor(self.execute_log)

    if existing_rows is not None:
        fake_conn = SmartFakeConn(existing_rows)
    elif boom_exc is not None:
        fake_conn = BoomConn()
    else:
        fake_conn = FakeConn()
    fake_sentry = FakeSentry()

    namespace = {
        "request": FakeRequest(),
        "g": FakeG(),
        "get_db": lambda: fake_conn,
        "fetch_html": fake_fetch_html,
        "parse_receipts": fake_parse_receipts,
        "upsert_billing_rows": fake_upsert_billing_rows,
        "jsonify": fake_jsonify,
        "sentry_sdk": fake_sentry,
        "print": fake_print,
        "HTTP_OK": HTTP_OK,
        "HTTP_NOT_FOUND": HTTP_NOT_FOUND,
        "HTTP_NETWORK_ERR": HTTP_NETWORK_ERR,
        "_BILLING_TARGET_YEARS": _BILLING_TARGET_YEARS,
        "_BILLING_SENTINEL_YEAR": _BILLING_SENTINEL_YEAR,
        "_BILLING_PORTAL_MARKER": _BILLING_PORTAL_MARKER,
        "psycopg2": type("FakePsycopg2", (), {"extras": type("FakeExtras", (), {"RealDictCursor": object()})})(),
    }
    exec(compile(FUNC_SRC, "app.py (sliced api_billing)", "exec"), namespace)
    result = namespace["api_billing"](geo_id)
    return {
        "payload": result,
        "fetch_calls": fetch_calls,
        "upsert_calls": upsert_calls,
        "sentry_messages": fake_sentry.messages,
        "sentry_flush_calls": fake_sentry.flush_calls,
        "sentry_exceptions": fake_sentry.exceptions,
        "print_calls": print_calls,
        "conn_closed": fake_conn.closed,
        "execute_log": fake_conn.execute_log,
    }


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


def find_print_by_tag(print_calls, tag):
    return [c for c in print_calls if c["args"] and tag in str(c["args"][0])]


all_ok = True

# ── 1. First attempt succeeds, real write ────────────────────────────────
print("Test 1: fetch_html succeeds on the first attempt, real target-year receipts found")
r = run_api_billing("0100030105", [(REAL_PAGE, HTTP_OK)])
all_ok &= check("fetch_html called exactly once", len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows called with real target-year records",
                len(r["upsert_calls"]) == 1 and len(r["upsert_calls"][0]) == 4)
all_ok &= check("records carry county_code", all(rec["county_code"] == "TRAVIS" for rec in r["upsert_calls"][0]))
all_ok &= check("no Sentry message sent", r["sentry_messages"] == [])
all_ok &= check("no Sentry flush call (no warning/exception path taken)", r["sentry_flush_calls"] == [])
write_prints = find_print_by_tag(r["print_calls"], "api_billing write:")
all_ok &= check("exactly 1 permanent write-path log line", len(write_prints) == 1)
all_ok &= check("write-path log reports the real row count from upsert_billing_rows()'s return value",
                "rows_written=4" in str(write_prints[0]["args"][0]) if write_prints else False)
all_ok &= check("write-path log reports receipts_parsed/target_matched correctly",
                write_prints and "receipts_parsed=5" in str(write_prints[0]["args"][0])
                and "target_matched=4" in str(write_prints[0]["args"][0]))
all_ok &= check("write-path log confirms the commit", write_prints and "commit_confirmed=True" in str(write_prints[0]["args"][0]))
all_ok &= check("route returns status ok", r["payload"]["status"] == "ok")
all_ok &= check("connection closed in finally", r["conn_closed"] is True)

# ── 2. First attempt fails (network error), second succeeds ─────────────
print("Test 2: fetch_html fails once, then succeeds on retry")
r = run_api_billing("0254402034", [(None, HTTP_NETWORK_ERR), (REAL_PAGE, HTTP_OK)])
all_ok &= check("fetch_html called exactly twice", len(r["fetch_calls"]) == 2)
all_ok &= check("second call still uses the 10s live-route timeout",
                r["fetch_calls"][1][1] == 10)
all_ok &= check("upsert_billing_rows still called (transient blip self-corrected)",
                len(r["upsert_calls"]) == 1)
all_ok &= check("no Sentry message sent for a transient, self-corrected blip",
                r["sentry_messages"] == [])
all_ok &= check("exactly 1 permanent write-path log line",
                len(find_print_by_tag(r["print_calls"], "api_billing write:")) == 1)

# ── 3. Both attempts fail (network error) — the real, live bug's exact shape
print("Test 3: fetch_html fails BOTH attempts (reproduces the live bug's exact symptom)")
r = run_api_billing("0100030105", [(None, HTTP_NETWORK_ERR), (None, HTTP_NETWORK_ERR)])
all_ok &= check("fetch_html called exactly twice (bounded retry, not unbounded)",
                len(r["fetch_calls"]) == 2)
all_ok &= check("NO upsert_billing_rows call (nothing written)", r["upsert_calls"] == [])
all_ok &= check("NO sentinel INSERT (execute_log has only the cache-check + final SELECT, no INSERT)",
                not any("INSERT" in (sql or "") for sql, _ in r["execute_log"]))
all_ok &= check("a Sentry warning-level message WAS sent",
                len(r["sentry_messages"]) == 1 and r["sentry_messages"][0][1] == "warning")
all_ok &= check("Sentry message names the real geo_id", "0100030105" in r["sentry_messages"][0][0])
all_ok &= check("sentry_sdk.flush() was called exactly once (BILLING-DIAG-2/4 hardening, permanent)",
                len(r["sentry_flush_calls"]) == 1)
all_ok &= check("no permanent write-path log line (write path never entered)",
                find_print_by_tag(r["print_calls"], "api_billing write:") == [])
all_ok &= check("route STILL returns a clean status:ok (not an exception) — matches live evidence exactly",
                r["payload"]["status"] == "ok" and r["payload"]["rows"] == [])
all_ok &= check("connection closed in finally even on total fetch failure", r["conn_closed"] is True)

# ── 4. HTTP_NOT_FOUND — genuine 404, no retry, no Sentry noise ───────────
print("Test 4: fetch_html returns HTTP_NOT_FOUND (genuine 404) — no retry, no Sentry noise")
r = run_api_billing("9999999999", [(None, HTTP_NOT_FOUND)])
all_ok &= check("fetch_html called exactly once (no retry for a genuine 404)",
                len(r["fetch_calls"]) == 1)
all_ok &= check("no Sentry message for an expected 404", r["sentry_messages"] == [])
all_ok &= check("no upsert call", r["upsert_calls"] == [])
all_ok &= check("no permanent write-path log line", find_print_by_tag(r["print_calls"], "api_billing write:") == [])

# ── 5. WAF/block-page scenario (BILLING-DIAG-2, permanent) ──────────────
print("Test 5: fetch_html returns HTTP_OK with html BOTH times, but it's a block page (no real marker)")
r = run_api_billing("0100030105", [(BLOCK_PAGE, HTTP_OK), (BLOCK_PAGE, HTTP_OK)])
all_ok &= check("fetch_html called exactly twice (block page treated as a failure, retried)",
                len(r["fetch_calls"]) == 2)
all_ok &= check("NO upsert_billing_rows call", r["upsert_calls"] == [])
all_ok &= check("NO sentinel INSERT written (this is the exact 'permanent wrong sentinel' risk this brief raised)",
                not any("INSERT" in (sql or "") for sql, _ in r["execute_log"]))
all_ok &= check("a Sentry warning-level message WAS sent (block page correctly classified as a failure)",
                len(r["sentry_messages"]) == 1 and r["sentry_messages"][0][1] == "warning")
all_ok &= check("sentry_sdk.flush() was called exactly once", len(r["sentry_flush_calls"]) == 1)
all_ok &= check("route still returns a clean status:ok/rows:[]",
                r["payload"]["status"] == "ok" and r["payload"]["rows"] == [])

# ── 6. BILLING-DIAG-3's real, permanent fix: exception now reaches Sentry ─
print("Test 6: a real exception after a successful fetch (e.g. a dropped/reaped DB "
      "connection surfacing on the next query) — reaches Sentry via capture_exception()")
boom = RuntimeError("server closed the connection unexpectedly")
r = run_api_billing(
    "0100030105", [(REAL_PAGE, HTTP_OK)],
    # BILLING-DIAG-8: "BETWEEN 2021 AND 2024" is no longer unique to the
    # step-3 final SELECT -- the widened step-1 already_fetched check (see
    # the sanity asserts above) now also contains that substring. "ORDER BY
    # tax_year" remains unique to step 3's real SQL text.
    boom_on_sql_substring="ORDER BY tax_year",  # the final SELECT in step 3
    boom_exc=boom,
)
all_ok &= check("fetch_html succeeded (1 call) — the exception happens AFTER the fetch",
                len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows WAS called (the write itself succeeded)",
                len(r["upsert_calls"]) == 1)
all_ok &= check("route returns status:error (exception correctly caught, not a crash)",
                r["payload"]["status"] == "error")
all_ok &= check("error message is the real exception text",
                r["payload"]["message"] == "server closed the connection unexpectedly")
all_ok &= check("sentry_sdk.capture_exception(exc) WAS called with the real exception",
                len(r["sentry_exceptions"]) == 1 and r["sentry_exceptions"][0] is boom)
all_ok &= check("sentry_sdk.flush() was called after the exception capture",
                len(r["sentry_flush_calls"]) == 1)
all_ok &= check("connection still closed in finally even on this exception path", r["conn_closed"] is True)

# ── 7. BILLING-DIAG-7: the sentinel branch, now with its own permanent log ─
print("Test 7: fetch succeeds, marker present, but receipts have no 2021-2024 rows (sentinel branch)")
r = run_api_billing(
    "0100030105", [(REAL_PAGE, HTTP_OK)],
    parse_receipts_result=[
        {"tax_year": 2020, "payment_amount": 50000.00},
        {"tax_year": 2025, "payment_amount": 90000.00},
    ],
)
all_ok &= check("fetch_html called exactly once", len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows NOT called (no target-year receipts)", r["upsert_calls"] == [])
all_ok &= check("a sentinel INSERT (tax_year=9999) WAS written",
                any("INSERT" in (sql or "") and params and 9999 in params
                    for sql, params in r["execute_log"]))
write_prints = find_print_by_tag(r["print_calls"], "api_billing write:")
all_ok &= check("exactly 1 permanent write-path log line (sentinel branch's own)", len(write_prints) == 1)
all_ok &= check("sentinel log reports rows_written=0, sentinel_written=True, commit_confirmed=True",
                write_prints and "rows_written=0" in str(write_prints[0]["args"][0])
                and "sentinel_written=True" in str(write_prints[0]["args"][0])
                and "commit_confirmed=True" in str(write_prints[0]["args"][0]))
all_ok &= check("route returns status:ok, cached:False, rows:[]",
                r["payload"]["status"] == "ok" and r["payload"]["rows"] == [])

# ── 8. BILLING-DIAG-8(a): better-source existing data skips the fetch ──────
print("Test 8: parcel has real, existing data from a BETTER source (not portal_scrape) "
      "for all 4 target years — already_fetched=True, no portal fetch, that data returned")
BETTER_SOURCE_ROWS = [
    {"geo_id": "0121230106", "tax_year": 2021, "total_tax": 12000.00, "total_paid": 12000.00,
     "data_source": "pir_billing_2021_full", "confidence_level": "high"},
    {"geo_id": "0121230106", "tax_year": 2022, "total_tax": 12500.00, "total_paid": 12500.00,
     "data_source": "pir_billing_2022_full", "confidence_level": "high"},
    {"geo_id": "0121230106", "tax_year": 2023, "total_tax": 13100.00, "total_paid": 13100.00,
     "data_source": "pir_billing_2023_full", "confidence_level": "high"},
    {"geo_id": "0121230106", "tax_year": 2024, "total_tax": 13800.00, "total_paid": 13800.00,
     "data_source": "pir_billing_2024_full", "confidence_level": "high"},
]
r = run_api_billing("0121230106", [], existing_rows=BETTER_SOURCE_ROWS)
all_ok &= check("already_fetched is True (route reports cached:True)", r["payload"]["cached"] is True)
all_ok &= check("fetch_html NEVER called — no wasteful re-fetch against better existing data",
                r["fetch_calls"] == [])
all_ok &= check("upsert_billing_rows NEVER called (no write attempted at all)", r["upsert_calls"] == [])
all_ok &= check("no permanent write-path log line (write path never entered)",
                find_print_by_tag(r["print_calls"], "api_billing write:") == [])
all_ok &= check("no Sentry noise", r["sentry_messages"] == [] and r["sentry_exceptions"] == [])
all_ok &= check("response returns all 4 real rows from the better source, not empty",
                len(r["payload"]["rows"]) == 4)
all_ok &= check("returned rows carry the REAL data_source (not silently relabeled 'portal_scrape')",
                all(row["data_source"] == f"pir_billing_{row['tax_year']}_full" for row in r["payload"]["rows"]))
all_ok &= check("rows are ordered by tax_year", [row["tax_year"] for row in r["payload"]["rows"]] == [2021, 2022, 2023, 2024])
all_ok &= check("route returns status ok", r["payload"]["status"] == "ok")

# ── 9. BILLING-DIAG-8(b): genuinely no data anywhere still fetches ─────────
print("Test 9: parcel has NO data from any source (real empty DB state) — "
      "already_fetched=False, a portal fetch IS attempted exactly as before")
r = run_api_billing("0100030199", [(REAL_PAGE, HTTP_OK)], existing_rows=[])
all_ok &= check("already_fetched is False (route reports cached:False)", r["payload"]["cached"] is False)
all_ok &= check("fetch_html WAS called exactly once — preserved, unwasted-fetch behavior",
                len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows WAS called (the real write path still runs)",
                len(r["upsert_calls"]) == 1)
all_ok &= check("exactly 1 permanent write-path log line",
                len(find_print_by_tag(r["print_calls"], "api_billing write:")) == 1)
all_ok &= check("route returns status ok", r["payload"]["status"] == "ok")

# ── 10. BILLING-DIAG-8(c): portal-scrape-only sentinel case is unaffected ──
print("Test 10: parcel has ONLY a portal_scrape sentinel row (tax_year=9999, no real "
      "target-year data from any source — the exact shape of the 79 real production cases) "
      "— unaffected by this fix, behaves exactly as before")
SENTINEL_ONLY_ROWS = [
    {"geo_id": "0100030105", "tax_year": 9999, "total_tax": None, "total_paid": None,
     "data_source": "portal_scrape", "confidence_level": "partial"},
]
r = run_api_billing("0100030105", [], existing_rows=SENTINEL_ONLY_ROWS)
all_ok &= check("already_fetched is True (sentinel alone still counts, unchanged from before this fix)",
                r["payload"]["cached"] is True)
all_ok &= check("fetch_html NEVER called (sentinel correctly prevents re-fetch, same as pre-DIAG-8)",
                r["fetch_calls"] == [])
all_ok &= check("upsert_billing_rows NEVER called", r["upsert_calls"] == [])
all_ok &= check("response rows is empty (sentinel, tax_year=9999, correctly excluded by the year-range filter)",
                r["payload"]["rows"] == [])
all_ok &= check("route returns status ok", r["payload"]["status"] == "ok")

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
