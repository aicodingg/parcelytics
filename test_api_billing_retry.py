"""
test_api_billing_retry.py — fixture tests for the BILLING-DIAG-1/2 fixes to
api_billing() (/<county_slug>/api/billing/<geo_id>).

BILLING-DIAG-1: a single, un-retried fetch_html(geo_id) call
(REQUEST_TIMEOUT=20s, no retry) was the live route's only attempt to reach
the Travis County Tax portal. Confirmed via live evidence (curl + a direct
SQL check against production on two real parcels -- neither had so much as
a sentinel row) that this fails often enough in production to matter, and
the failure branch was a silent no-op (no logging, no Sentry signal) --
invisible until a manual curl+SQL cross-check. Fix: 2 attempts, 10s each
(NOT the CLI loader's 3x20s pattern -- would risk exceeding gunicorn's 30s
default worker timeout and reintroducing the WORKER TIMEOUT/SIGKILL class
of incident this codebase already hit once), plus a low-noise Sentry
message on exhausted-retry failure.

BILLING-DIAG-2 (post-deploy: the symptom didn't resolve, and the new Sentry
warning never fired either): new evidence (Render's own docs + community
threads -- outbound IPs are SHARED across ALL Render customers in a region
and other customers report 403s/WAF blocks reaching third-party sites from
those shared IPs) raised a real possibility this diagnosis hadn't
considered -- a WAF/bot-detection layer in front of travis.go2gov.net could
return a real HTTP 200 with an HTML block/CAPTCHA page instead of a clean
403, which the OLD `status == HTTP_OK` check alone would have wrongly
trusted as a genuine, successful, empty fetch -- writing a PERMANENT wrong
sentinel row and explains why the warning never fired (the code believed it
had succeeded). Fix: a real, distinctive marker string
(_BILLING_PORTAL_MARKER = "Travis County Tax", confirmed present in
BILLING-DIAG-1's own direct inspection of a genuine successful fetch) must
also be present before a 200 response is trusted; its absence is now
treated as a failure (retried, then reported), not "genuinely fetched, no
data." Also added: an explicit sentry_sdk.flush(timeout=2) after the
warning, since sentry_sdk queues events to a background thread by default
and does not guarantee delivery before the request returns or the process
is recycled -- a real, low-risk hardening regardless of which BILLING-
DIAG-2 hypothesis turns out to be the actual cause.

Sandbox has no Flask/psycopg2 (confirmed unavailable, same constraint as
every other slice-and-exec test in this codebase). Uses the same technique
already established here: extract api_billing()'s REAL source text out of
app.py between two markers and exec() it against a minimal namespace of
fakes (fake `request`/`g`, a fake `conn`/cursor that records SQL, a fake
`fetch_html` that returns a scripted sequence of (html, status) per call so
the retry loop's real behavior can be observed, real HTTP_OK/HTTP_NOT_FOUND/
HTTP_NETWORK_ERR sentinels, a fake `sentry_sdk` recording capture_message()
and flush() calls). This tests the ACTUAL function body that ships in
app.py, not a reimplementation of it -- which is exactly what caught a real
NameError (HTTP_NETWORK_ERR referenced but not imported in app.py) during
BILLING-DIAG-1's own work, before it could reach production.

What these tests check:
  1. fetch_html() succeeds on the FIRST attempt (real marker present):
     called exactly once, real records written via upsert_billing_rows(),
     no Sentry message.
  2. fetch_html() fails once (network error) then succeeds on the 2nd
     attempt: called exactly twice, real records still written, no Sentry
     message (a transient blip that self-corrects is NOT reported --
     only a fully-exhausted retry is).
  3. fetch_html() fails BOTH attempts (network error each time): called
     exactly twice, NO write attempted (no upsert_billing_rows call, no
     sentinel INSERT), a Sentry warning-level message IS sent naming the
     geo_id and last status, sentry_sdk.flush() is called, and the route
     still returns a clean {"status":"ok","rows":[]} response (not an
     exception) -- reproducing the exact live symptom from this brief's
     evidence trail.
  4. fetch_html() returns HTTP_NOT_FOUND on the first attempt: called
     exactly ONCE (no retry for a genuine 404 -- retrying an account that
     doesn't exist wastes the retry budget), no Sentry message (a 404 is
     an expected, not exceptional, outcome), no write.
  5. (BILLING-DIAG-2, new) fetch_html() returns HTTP_OK with html content
     BOTH attempts, but the real portal marker is missing both times (the
     WAF/block-page scenario): treated as a failure, NOT a genuine empty
     fetch -- called exactly twice, NO sentinel written, a Sentry warning
     IS sent. Proves the fix actually closes the "permanent wrong
     sentinel" risk this brief specifically raised.

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
assert "_BILLING_PORTAL_MARKER" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-2 marker check"
assert "sentry_sdk.flush" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-2 flush() call"
assert "sentry_sdk.capture_exception" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-3 exception capture"
assert "BILLING-DIAG-3" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-3 diagnostic breadcrumb"
assert "BILLING-DIAG-4" in FUNC_SRC, "sanity: slice must contain the BILLING-DIAG-4 flush() fix"
assert FUNC_SRC.count("sentry_sdk.flush") == 3, \
    "sanity: exactly 3 flush() calls expected (breadcrumb, warning branch, exception handler)"

HTTP_OK = 0
HTTP_NOT_FOUND = 404
HTTP_NETWORK_ERR = -1
_BILLING_TARGET_YEARS = {2021, 2022, 2023, 2024}
_BILLING_SENTINEL_YEAR = 9999
_BILLING_PORTAL_MARKER = "Travis County Tax"
REAL_PAGE = f"<html><head><title>{_BILLING_PORTAL_MARKER}</title></head><body>real receipts</body></html>"
BLOCK_PAGE = "<html><head><title>Access Denied</title></head><body>Please verify you are human.</body></html>"


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
    existing coverage need).

    BILLING-DIAG-3: optional `boom_on_sql_substring` simulates a connection
    that dies somewhere between the fetch and a later use of the same `conn`
    (e.g. an idle-connection reap by a pooler/proxy while the request was
    blocked on the outbound HTTP call) -- execute() raises the given
    exception the first time it's called with SQL containing that substring.
    """
    def __init__(self, log, boom_on_sql_substring=None, boom_exc=None):
        self.log = log
        self.boom_on_sql_substring = boom_on_sql_substring
        self.boom_exc = boom_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if self.boom_on_sql_substring and self.boom_on_sql_substring in sql:
            self.boom_on_sql_substring = None  # only once
            raise self.boom_exc

    def fetchone(self):
        return {"cnt": 0}

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, boom_on_sql_substring=None, boom_exc=None):
        self.execute_log = []
        self.commits = 0
        self.closed = False
        self.boom_on_sql_substring = boom_on_sql_substring
        self.boom_exc = boom_exc

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.execute_log, self.boom_on_sql_substring, self.boom_exc)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeSentry:
    """BILLING-DIAG-4: `calls` is an ORDERED log of every capture_message/
    capture_exception/flush call, so tests can assert that each capture is
    immediately followed by a flush() -- not just that flush() was called
    *some* number of times, which wouldn't catch a specific capture call
    missing its own flush() the way BILLING-DIAG-4's real bug was structured
    (two of the three real Sentry call sites got a flush(), one didn't)."""
    def __init__(self):
        self.messages = []
        self.flush_calls = []
        self.exceptions = []
        self.calls = []

    def capture_message(self, msg, level=None):
        self.messages.append((msg, level))
        self.calls.append(("capture_message", msg, level))

    def capture_exception(self, exc=None):
        self.exceptions.append(exc)
        self.calls.append(("capture_exception", exc))

    def flush(self, timeout=None):
        self.flush_calls.append(timeout)
        self.calls.append(("flush", timeout))


def run_api_billing(geo_id, fetch_sequence, boom_on_sql_substring=None, boom_exc=None):
    """
    fetch_sequence: list of (html, status) tuples, one per fetch_html() call
    (consumed in order; raises IndexError if the real code calls it more
    times than scripted -- which is itself a useful assertion).

    boom_on_sql_substring/boom_exc: BILLING-DIAG-3 -- simulate a real
    exception raised on a specific SQL statement (see FakeCursor).
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

    fake_conn = FakeConn(boom_on_sql_substring, boom_exc)
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
        "_BILLING_PORTAL_MARKER": _BILLING_PORTAL_MARKER,
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
        "sentry_flush_calls": fake_sentry.flush_calls,
        "sentry_exceptions": fake_sentry.exceptions,
        "sentry_calls": fake_sentry.calls,
        "conn_closed": fake_conn.closed,
        "execute_log": fake_conn.execute_log,
    }


def info_msgs(messages):
    return [m for m in messages if m[1] == "info"]


def warning_msgs(messages):
    return [m for m in messages if m[1] == "warning"]


def every_capture_immediately_flushed(calls):
    """BILLING-DIAG-4: for every capture_message/capture_exception entry in
    the ordered call log, the very next entry must be a flush() -- this is
    the real regression guard for BILLING-DIAG-4's exact bug (a capture call
    with no flush() after it)."""
    for i, c in enumerate(calls):
        if c[0] in ("capture_message", "capture_exception"):
            if i + 1 >= len(calls) or calls[i + 1][0] != "flush":
                return False
    return True


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


all_ok = True

# ── 1. First attempt succeeds ────────────────────────────────────────────
print("Test 1: fetch_html succeeds on the first attempt")
r = run_api_billing("0100030105", [(REAL_PAGE, HTTP_OK)])
all_ok &= check("fetch_html called exactly once", len(r["fetch_calls"]) == 1)
all_ok &= check("upsert_billing_rows called with real target-year records",
                len(r["upsert_calls"]) == 1 and len(r["upsert_calls"][0]) == 4)
all_ok &= check("records carry county_code", all(rec["county_code"] == "TRAVIS" for rec in r["upsert_calls"][0]))
all_ok &= check("no warning-level Sentry message sent", warning_msgs(r["sentry_messages"]) == [])
all_ok &= check("BILLING-DIAG-3 info breadcrumb sent (exactly 1, post-retry-loop)",
                len(info_msgs(r["sentry_messages"])) == 1)
all_ok &= check("no capture_exception call (no exception occurred)", r["sentry_exceptions"] == [])
all_ok &= check("BILLING-DIAG-4: breadcrumb's capture_message() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))
all_ok &= check("exactly 1 flush() call total (just the breadcrumb's)", len(r["sentry_flush_calls"]) == 1)
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
all_ok &= check("no warning-level Sentry message for a transient, self-corrected blip",
                warning_msgs(r["sentry_messages"]) == [])
all_ok &= check("BILLING-DIAG-3 info breadcrumb still sent (exactly 1)",
                len(info_msgs(r["sentry_messages"])) == 1)
all_ok &= check("BILLING-DIAG-4: breadcrumb's capture_message() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))

# ── 3. Both attempts fail (network error) — the real, live bug's exact shape
print("Test 3: fetch_html fails BOTH attempts (reproduces the live bug's exact symptom)")
r = run_api_billing("0100030105", [(None, HTTP_NETWORK_ERR), (None, HTTP_NETWORK_ERR)])
all_ok &= check("fetch_html called exactly twice (bounded retry, not unbounded)",
                len(r["fetch_calls"]) == 2)
all_ok &= check("NO upsert_billing_rows call (nothing written)", r["upsert_calls"] == [])
all_ok &= check("NO sentinel INSERT (execute_log has only the cache-check + final SELECT, no INSERT)",
                not any("INSERT" in (sql or "") for sql, _ in r["execute_log"]))
all_ok &= check("a Sentry warning-level message WAS sent",
                len(warning_msgs(r["sentry_messages"])) == 1)
all_ok &= check("Sentry warning names the real geo_id", "0100030105" in warning_msgs(r["sentry_messages"])[0][0])
all_ok &= check("BILLING-DIAG-3 info breadcrumb also sent, reporting the exhausted-retry state",
                len(info_msgs(r["sentry_messages"])) == 1
                and "html_is_none=True" in info_msgs(r["sentry_messages"])[0][0]
                and "attempts_made=2" in info_msgs(r["sentry_messages"])[0][0])
all_ok &= check("sentry_sdk.flush() called twice (BILLING-DIAG-4: breadcrumb + warning, each flushed)",
                len(r["sentry_flush_calls"]) == 2)
all_ok &= check("BILLING-DIAG-4: every capture_message() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))
all_ok &= check("route STILL returns a clean status:ok (not an exception) — matches live evidence exactly",
                r["payload"]["status"] == "ok" and r["payload"]["rows"] == [])
all_ok &= check("connection closed in finally even on total fetch failure", r["conn_closed"] is True)

# ── 4. HTTP_NOT_FOUND — genuine 404, no retry, no Sentry noise ───────────
print("Test 4: fetch_html returns HTTP_NOT_FOUND (genuine 404) — no retry, no Sentry noise")
r = run_api_billing("9999999999", [(None, HTTP_NOT_FOUND)])
all_ok &= check("fetch_html called exactly once (no retry for a genuine 404)",
                len(r["fetch_calls"]) == 1)
all_ok &= check("no warning-level Sentry message for an expected 404", warning_msgs(r["sentry_messages"]) == [])
all_ok &= check("BILLING-DIAG-3 info breadcrumb still sent (fires unconditionally after the retry loop)",
                len(info_msgs(r["sentry_messages"])) == 1)
all_ok &= check("BILLING-DIAG-4: breadcrumb's capture_message() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))
all_ok &= check("no upsert call", r["upsert_calls"] == [])

# ── 5. WAF/block-page scenario (BILLING-DIAG-2's own new hypothesis) ─────
print("Test 5: fetch_html returns HTTP_OK with html BOTH times, but it's a block page (no real marker)")
r = run_api_billing("0100030105", [(BLOCK_PAGE, HTTP_OK), (BLOCK_PAGE, HTTP_OK)])
all_ok &= check("fetch_html called exactly twice (block page treated as a failure, retried)",
                len(r["fetch_calls"]) == 2)
all_ok &= check("NO upsert_billing_rows call", r["upsert_calls"] == [])
all_ok &= check("NO sentinel INSERT written (this is the exact 'permanent wrong sentinel' risk this brief raised)",
                not any("INSERT" in (sql or "") for sql, _ in r["execute_log"]))
all_ok &= check("a Sentry warning-level message WAS sent (block page correctly classified as a failure)",
                len(warning_msgs(r["sentry_messages"])) == 1)
all_ok &= check("BILLING-DIAG-3 info breadcrumb also sent",
                len(info_msgs(r["sentry_messages"])) == 1)
all_ok &= check("sentry_sdk.flush() called twice (BILLING-DIAG-4: breadcrumb + warning, each flushed)",
                len(r["sentry_flush_calls"]) == 2)
all_ok &= check("BILLING-DIAG-4: every capture_message() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))
all_ok &= check("route still returns a clean status:ok/rows:[]",
                r["payload"]["status"] == "ok" and r["payload"]["rows"] == [])

# ── 6. BILLING-DIAG-3: a real exception mid-request now reaches Sentry ────
print("Test 6: a real exception after a successful fetch (e.g. a dropped/reaped DB "
      "connection surfacing on the next query) — previously silently invisible to Sentry")
boom = RuntimeError("server closed the connection unexpectedly")
r = run_api_billing(
    "0100030105", [(REAL_PAGE, HTTP_OK)],
    boom_on_sql_substring="BETWEEN 2021 AND 2024",  # the final SELECT in step 3
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
all_ok &= check("BILLING-DIAG-3 fix: sentry_sdk.capture_exception(exc) WAS called with the real exception",
                len(r["sentry_exceptions"]) == 1 and r["sentry_exceptions"][0] is boom)
all_ok &= check("sentry_sdk.flush() called twice (BILLING-DIAG-4: breadcrumb + exception handler, each flushed)",
                len(r["sentry_flush_calls"]) == 2)
all_ok &= check("BILLING-DIAG-4: every capture_message()/capture_exception() is immediately followed by flush()",
                every_capture_immediately_flushed(r["sentry_calls"]))
all_ok &= check("connection still closed in finally even on this exception path", r["conn_closed"] is True)

print()
if all_ok:
    print("ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("SOME CHECKS FAILED")
    sys.exit(1)
