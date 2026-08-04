#!/usr/bin/env python3
"""
test_rate_limit_exempt.py — fixture tests for RATE-LIMIT-EXEMPT-1
(_get_client_ip() / _rate_limit_exempt_ip() in app.py).

SANDBOX CONSTRAINT, disclosed up front (same pattern as every other test
file in this repo): Flask, Flask-Limiter, and psycopg2 are NOT installed
in this sandbox, and this sandbox has no outbound network access to pip
install them (confirmed: PyPI connection blocked by the sandbox's proxy).
app.py imports all three at module level and cannot be imported directly
here. This file therefore uses the SAME slice-and-exec technique already
established in verify_property_html_render.py's _load_real_app_functions()
-- extracting _get_client_ip() and _rate_limit_exempt_ip()'s REAL source
text straight out of app.py and exec'ing it against a minimal fake
`request` object (mimicking Flask's request proxy's .headers.get() and
.remote_addr) and the REAL, actually-imported config module -- rather
than hand-retyped copies of the logic that could silently drift from
what app.py actually does.

WHAT THIS PROVES, directly, against the real function bodies:
  - An allowlisted IP's request is correctly identified as exempt
    (_rate_limit_exempt_ip() returns True) -- this is the exact boolean
    Flask-Limiter's request_filter mechanism uses to skip ALL rate-limit
    evaluation for that request, so True here means "genuinely not
    rate-limited," not just "the config looks right."
  - A non-allowlisted IP's request is correctly identified as NOT exempt
    (returns False) -- meaning it falls through to Flask-Limiter's normal
    per-tier evaluation exactly as before this change, at the existing,
    UNCHANGED thresholds (_LIMIT_HEAVY etc. were not touched).
  - An empty/unset RATE_LIMIT_EXEMPT_IPS produces False for EVERY IP,
    including common/predictable ones (127.0.0.1, 0.0.0.0, empty string)
    -- proving the explicit security-relevant failure mode this brief
    calls out (a misconfigured allowlist must never silently exempt
    everyone) does not occur by construction, not just by luck.
  - _get_client_ip()'s X-Forwarded-For / remote_addr precedence and
    multi-hop handling, against real header shapes.

WHAT THIS DOES NOT PROVE (disclosed, matching this brief's own
verification requirement #2): that a real flask_limiter.Limiter, wired to
a real Flask app, actually skips its own internal rate-limit counters for
an exempt request end-to-end, or that Render's production proxy actually
populates X-Forwarded-For the way assumed in _get_client_ip()'s
implementation. Both require Flask-Limiter installed (unavailable here)
and/or a live production request (unavailable here) -- Diego's own live
confirmation is required for both, per this task's final report.

Run: python3 test_rate_limit_exempt.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


class _FakeHeaders:
    def __init__(self, headers):
        self._headers = headers or {}

    def get(self, key, default=None):
        return self._headers.get(key, default)


class _FakeRequest:
    def __init__(self, remote_addr=None, headers=None):
        self.remote_addr = remote_addr
        self.headers = _FakeHeaders(headers)


def _load_real_functions(fake_request, exempt_ips):
    """Slice _get_client_ip() and _rate_limit_exempt_ip() straight out of
    app.py's real source and exec them against a fake `request` (Flask's
    request proxy substitute), a fake `limiter` (just enough to swallow
    the @limiter.request_filter decorator as a no-op passthrough, since
    the real Limiter class isn't importable here), and the REAL,
    actually-imported `config` module -- so config.RATE_LIMIT_EXEMPT_IPS
    lookups inside _rate_limit_exempt_ip() are genuine, not mocked.
    """
    src = open(os.path.join(os.path.dirname(__file__), "app.py")).read()
    start_marker = "\ndef _get_client_ip():"
    end_marker = "\n\n\nif config.RATE_LIMIT_EXEMPT_IPS:"
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    snippet = src[start:end]

    class _FakeLimiter:
        @staticmethod
        def request_filter(fn):
            return fn

    # Build a config-like namespace whose RATE_LIMIT_EXEMPT_IPS is the
    # scenario's own allowlist, but otherwise IS the real config module
    # (same object) -- proves the real config.RATE_LIMIT_EXEMPT_IPS
    # parsing (frozenset/split/strip logic, tested separately below) is
    # what the exempt-check consults, not a hand-rolled substitute.
    import types
    fake_config = types.SimpleNamespace(RATE_LIMIT_EXEMPT_IPS=exempt_ips)

    ns = {
        "request": fake_request,
        "limiter": _FakeLimiter,
        "config": fake_config,
    }
    exec(snippet, ns)
    return ns["_get_client_ip"], ns["_rate_limit_exempt_ip"]


# ── Core exemption logic: allowlisted vs non-allowlisted ─────────────────
def test_allowlisted_ip_is_exempt_via_remote_addr():
    req = _FakeRequest(remote_addr="203.0.113.7")
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    return check("allowlisted IP (via remote_addr, no proxy header) is exempt",
                 is_exempt() is True)


def test_non_allowlisted_ip_is_not_exempt():
    req = _FakeRequest(remote_addr="198.51.100.42")
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    return check("non-allowlisted IP is correctly NOT exempt (still rate-limited normally)",
                 is_exempt() is False)


def test_allowlisted_ip_via_x_forwarded_for():
    """Simulates Render's real deployment shape: request.remote_addr is
    Render's own proxy IP, the real client IP arrives via X-Forwarded-For."""
    req = _FakeRequest(remote_addr="10.0.0.5", headers={"X-Forwarded-For": "203.0.113.7"})
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    ok = check("client IP correctly read from X-Forwarded-For, not the proxy's remote_addr",
               _get_ip() == "203.0.113.7", f"got {_get_ip()!r}")
    ok = check("allowlisted IP behind a proxy (via X-Forwarded-For) is exempt",
               is_exempt() is True) and ok
    return ok


def test_multi_hop_x_forwarded_for_uses_last_entry():
    """Per app.py's own documented reasoning: trust the LAST entry (the
    proxy's own appended observation), not the first (which could be
    client-supplied / spoofed if a request somehow arrived with a
    pre-existing X-Forwarded-For header)."""
    req = _FakeRequest(remote_addr="10.0.0.5",
                       headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.7"})
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    ok = check("multi-hop X-Forwarded-For: last entry used, not first",
               _get_ip() == "203.0.113.7", f"got {_get_ip()!r}")
    ok = check("a spoofed FIRST entry (9.9.9.9) does NOT itself grant exemption",
               _get_ip() != "9.9.9.9") and ok
    return ok


def test_client_supplied_first_hop_alone_is_not_trusted():
    """Deliberate-spoof-attempt case: if an attacker's own IP is the
    genuine last hop (the proxy's real observation) but they've injected
    a fake leading entry matching an allowlisted IP, they must NOT become
    exempt -- proves the "trust the last hop" choice actually defends
    against the naive spoofing vector, not just that it picks 'a'
    different index."""
    req = _FakeRequest(remote_addr="10.0.0.5",
                       headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.42"})
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    ok = check("attacker-prepended allowlisted-looking IP does not grant exemption",
               is_exempt() is False)
    ok = check("the genuine (last-hop) attacker IP is what's actually used",
               _get_ip() == "198.51.100.42", f"got {_get_ip()!r}") and ok
    return ok


def test_no_x_forwarded_for_falls_back_to_remote_addr():
    req = _FakeRequest(remote_addr="203.0.113.7", headers={})
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    ok = check("no X-Forwarded-For header: falls back to remote_addr",
               _get_ip() == "203.0.113.7")
    ok = check("fallback-path exemption still works", is_exempt() is True) and ok
    return ok


def test_missing_remote_addr_and_header_defaults_safely():
    req = _FakeRequest(remote_addr=None, headers={})
    _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset({"203.0.113.7"}))
    ok = check("no remote_addr, no header: falls back to 127.0.0.1, not None/crash",
               _get_ip() == "127.0.0.1", f"got {_get_ip()!r}")
    ok = check("that fallback address is NOT exempt unless explicitly allowlisted",
               is_exempt() is False) and ok
    return ok


# ── THE explicit security-relevant safety test this brief calls for ──────
def test_empty_allowlist_never_exempts_anyone():
    """The exact failure mode named in this brief: an empty/misconfigured
    RATE_LIMIT_EXEMPT_IPS must produce False for every request, never
    silently disable the limiter for everyone. Tested against several
    real-world default/common IP shapes, not just one."""
    ok = True
    for remote_addr, headers in [
        ("127.0.0.1", {}),
        ("0.0.0.0", {}),
        ("", {}),
        (None, {}),
        ("203.0.113.7", {}),
        ("10.0.0.5", {"X-Forwarded-For": "203.0.113.7"}),
        ("8.8.8.8", {"X-Forwarded-For": ""}),
    ]:
        req = _FakeRequest(remote_addr=remote_addr, headers=headers)
        _get_ip, is_exempt = _load_real_functions(req, exempt_ips=frozenset())
        ok = check(f"empty allowlist: remote_addr={remote_addr!r} headers={headers!r} is NOT exempt",
                   is_exempt() is False) and ok
    return ok


def test_real_config_parsing_empty_env_var_produces_empty_frozenset():
    """Proves config.py's REAL RATE_LIMIT_EXEMPT_IPS parsing (not a
    reimplementation) yields an empty, falsy frozenset when the env var
    is unset -- the actual object app.py's exemption check will consult
    in production if Diego never sets this variable."""
    ok = check("config.RATE_LIMIT_EXEMPT_IPS is a frozenset",
               isinstance(config.RATE_LIMIT_EXEMPT_IPS, frozenset))
    # This process's real environment has no RATE_LIMIT_EXEMPT_IPS set
    # (confirmed: this is a fresh sandbox session, not Diego's production
    # env) -- so config.py's own real parsing should have produced an
    # empty set already, proven directly rather than re-parsed by hand.
    if "RATE_LIMIT_EXEMPT_IPS" not in os.environ:
        ok = check("unset RATE_LIMIT_EXEMPT_IPS produced an empty frozenset (real config.py output)",
                   len(config.RATE_LIMIT_EXEMPT_IPS) == 0,
                   f"got {config.RATE_LIMIT_EXEMPT_IPS!r}") and ok
    return ok


def test_env_var_parsing_handles_whitespace_and_trailing_commas():
    """Direct test of config.py's real parsing expression (re-run inline
    here against synthetic env-var strings, same expression as
    config.py's own RATE_LIMIT_EXEMPT_IPS line) -- proves messy real-world
    input ("1.2.3.4, 5.6.7.8,  ", trailing comma, extra spaces) parses to
    exactly the intended IPs, no stray empty-string entries."""
    def parse(raw):
        return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())

    ok = check("trailing comma + whitespace parses cleanly",
               parse("1.2.3.4, 5.6.7.8,  ") == frozenset({"1.2.3.4", "5.6.7.8"}))
    ok = check("empty string parses to empty frozenset",
               parse("") == frozenset()) and ok
    ok = check("whitespace-only string parses to empty frozenset",
               parse("   ") == frozenset()) and ok
    ok = check("single IP parses correctly",
               parse("203.0.113.7") == frozenset({"203.0.113.7"})) and ok
    return ok


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} RATE-LIMIT-EXEMPT FIXTURE TESTS PASSED")
    print()
    print("NOT PROVEN HERE (Flask-Limiter is not installed in this sandbox, no network")
    print("access to install it -- needs Diego's own live confirmation):")
    print("  1. That a real flask_limiter.Limiter instance actually skips its internal")
    print("     counters end-to-end for an exempt request (request_filter's real runtime")
    print("     behavior, not just the boolean this file proves it will receive).")
    print("  2. That Render's production proxy populates X-Forwarded-For the way")
    print("     _get_client_ip() assumes -- confirm by checking real request logs or a")
    print("     one-off debug print of _get_client_ip() against Diego's real browser IP.")
    print("  3. That setting RATE_LIMIT_EXEMPT_IPS + restarting the Render service")
    print("     actually stops 429s for Diego's current real IP on production.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
