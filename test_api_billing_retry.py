"""
test_api_billing_retry.py — fixture tests for the BILLING-DIAG-1 fix to
api_billing() (/<county_slug>/api/billing/<geo_id>).

Real bug found this brief: a single, un-retried fetch_html(geo_id) call
(REQUEST_TIMEOUT=20s, no retry) was the live route's only attempt to reach
the Travis County Tax portal. Confirmed via live evidence (curl + a direct
SQL check against production on two real parcels -- neither had so much as
a sentinel row) that this fails often enough in production to matter, and
the failure branch was a silent no-op (no logging, no Sentry signal) --
invisible until a manual curl+SQL cross-check. Fix: 2 attempts, 10s each
(NOT the CLI loader's 3x20s pattern -- would risk exceeding gunicorn's 30s
default worker timeout and reintroducing the WORKER TIMEOUT/SIGKILL class
of incident this codebase already hit once), plus a low-noise Sentry
message on exhausted-retry failure so this class of bug is visible next
time instead of requiring another manual cross-check.

Sandbox has no Flask/psycopg2 (confirmed unavailable, same constraint as
every other slice-and-exec test in this codebase). Uses the same technique
already established here: extract api_billing()'s REAL source text out of
app.py between two markers and exec() it against a minimal namespace of
fakes (fake `request`/`g`, a fake `conn`/cursor that records SQL, a fake
`fetch_html` that returns a scripted sequence of (html, status) per call so
the retry loop's real behavior can be observed, real HTTP_OK/HTTP_NOT_FOUND/
HTTP_NETWORK_ERR sentinels, a fake `sentry_sdk.capture_message` that records
calls). This tests the ACTUAL function body that ships in app.py, not a
reimplementation of it -- which is exactly what caught a real NameError
(HTTP_NETWORK_ERR referenced but not imported in app.py) during this
brief's own work, before it could reach production.

What these tests check:
  1. fetch_html() succeeds on the FIRST attempt: called exactly once, real
     records written via upsert_billing_rows(), no Sentry message.
  2. fetch_html() fails once (network error) then succeeds on the 2nd
     attempt: called exactly twice, real records still written, no Sentry
     message (a transient blip that self-corrects is NOT reported --
     only a fully-exhausted retry is).
  3. fetch_html() fails BOTH attempts (network error each time): called
     exactly twice, NO write attempted (no upsert_billing_rows call, no
     sentinel INSERT), a Sentry warning-level message IS sent naming the
     geo_id and last status, and the route still returns a clean
     {"status":"ok","rows":[]} response (not an exception) -- reproducing
     the exact live symptom from this brief's evidence trail.
  4. fetch_html() returns HTTP_NOT_FOUND on the first attempt: called
     exactly ONCE (no retry for a genuine 404 -- retrying an account that
     doesn't exist wastes the retry budget), no Sentry message (a 404 is
     an expected, not exceptional, outcome), no write.

Run: python3 test_api_billing_retry.py
"""
import re
import sys

sys.path.insert(0, ".")

APP_PY = open("app.py").read()

START_MARKER = "\ndef api_billing(geo_id):"
END_MARKER = "\n\n\n# ── Task 5: ptype label → SQL WHERE fragments"

start = APP_PY.index(START_MARKER)
end = APP_PY.index(END_MARKER, start)
FUNC_SRC = APP_PY[start:end]

assert "def api_billing" in FUNC_SRC
assert "for _attempt in range(2):" in FUNC_SRC, "sanity: slice must contain the retry loop"
assert "sentry_sdk.capture_message" in FUNC_SRC, "sanity: slice must contain the new logging"

HTTP_OK = 0
HTTP_NOT_FOUND = 404
HTTP_NETWORK_ERR = -1
_BILLING_TARGET_YEARS = {2021, 2022, 2023, 2024}
_BILLING_SENTINEL_YEAR = 9999


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
    fetch/retry path, not the cache-hit path, which has its own, unrelated
    existing coverage need)."""
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


class FakeSentry:
    def __init__(self):
        self.messages = []

    def capture_message(self, msg, level=None):
        self.messages.append((msg, level))


def run_api_billing(geo_id, fetch_sequence):
    """
    fetch_sequence: list of (html, status) tuples, one per fetch_html() call
    (consumed in order; raises IndexError if the real code calls it more
    times than scripted -- which is itself a useful assertion).
    """
    fetch_calls = []

    def fake_fetch_html(gid, timeout=None):
        fetch_calls.append((gid, timeout))
        return fetch_sequence[len(fetch_calls) - 1]

    def fake_parse_receipts(html):
        # Real receipts shape, matching BILLING-DIAG-1's own evidence trail.
        return [
            {"tax_year": 2021, "payment_amount": 64459.78},
            {"tax_year": 2022, "payment_amount": 62522.55},
            {"tax_year": 2023, "payment_amount": 76601.36},
            {"tax_year": 2024, "payment_amount": 85848.63},
            {"tax_year": 2025, "payment_amount": 90000.00},  # outside target range
        ]

    upsert_calls = []

    def fake_upsert_billing_rows(conn, records):
        upsert_calls.append(records)

    def fake_jsonify(payload):
        return payload

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
        "HTTP_OK": HTTP_OK,
        "HTTP_NOT_FOUND": HTTP_NOT_FOUND,
        "HTTP_NETWORK_ERR": HTTP_NETWORK_ERR,
        "_BILLING_TARGET_YEARS": _BILLING_TARGET_YEARS,
        "_BILLING_SENTINEL_YEAR": _BILLING_SENTINEL_YEAR,
        # psycopg2.extras.RealDictCursor is referenced as a cursor_factory
        # kwarg value only -- FakeConn.cursor() ignores it, so any sentinel
        # object works.
        "psycopg2": type("FakePsycopg2", (), {"extras": type("FakeExtras", (), {"RealDictCursor": object()})})(),
    }
    exec(compile(FUNC_SRC, "app.py (sliced api_billing)", "exec"), namespace)
    result = namespace["api_billing"](geo_id)
    return {
        "payload": result,
        "fetch_calls": fetch_calls,
        "upsert_calls": upsert_calls,
        "sentry_messages": fake_sentry.messages,
        "conn_closed": fake_conn.closed,
        "execute_log": fake_conn.execute_log,
    }


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


all_ok = True

# ── 1. First attempt succeeds ────────────────────────────────────────────
print("Test 1: fetch_html succeeds on the first attempt")
r = run_api_billing("0100030105", [("<html>real receipts</html>", HTTP_OK)])
all_ok &= check("fetch_html called exactly once", len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows called with real target-year records",
                len(r["upsert_calls"]) == 1 and len(r["upsert_calls"][0]) == 4)
all_ok &= check("records carry county_code", all(rec["county_code"] == "TRAVIS" for rec in r["upsert_calls"][0]))
all_ok &= check("no Sentry message sent", r["sentry_messages"] == [])
all_ok &= check("route returns status ok", r["payload"]["status"] == "ok")
all_ok &= check("connection closed in finally", r["conn_closed"] is True)

# ── 2. First attempt fails (network error), second succeeds ─────────────
print("Test 2: fetch_html fails once, then succeeds on retry")
r = run_api_billing("0254402034", [(None, HTTP_NETWORK_ERR), ("<html>real receipts</html>", HTTP_OK)])
all_ok &= check("fetch_html called exactly twice", len(r["fetch_calls"]) == 2)
all_ok &= check("second call still uses the 10s live-route timeout",
                r["fetch_calls"][1][1] == 10)
all_ok &= check("upsert_billing_rows still called (transient blip self-corrected)",
                len(r["upsert_calls"]) == 1)
all_ok &= check("no Sentry message sent for a transient, self-corrected blip",
                r["sentry_messages"] == [])

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

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
