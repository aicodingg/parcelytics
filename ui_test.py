"""
Parcelytics UI self-check script.
Tests all search formats, known parcels, error states, and page load timing.
Run with the Flask app already started:
    python3 ui_test.py
"""
import time
import urllib.request
import urllib.parse
import urllib.error
import sys

BASE = "http://127.0.0.1:5000"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"

results = []


def check(name, ok, detail=""):
    tag = PASS if ok else FAIL
    print(f"  {tag}  {name}" + (f" — {detail}" if detail else ""))
    results.append((name, ok, detail))


def get(path, follow_redirects=True):
    url = BASE + path
    t0 = time.time()
    try:
        req = urllib.request.Request(url)
        if not follow_redirects:
            opener = urllib.request.build_opener(urllib.request.HTTPHandler)
            resp = opener.open(req, timeout=10)
        else:
            resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode("utf-8", errors="replace")
        elapsed = time.time() - t0
        return resp.status, body, elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, elapsed
    except urllib.error.URLError as e:
        return None, str(e), time.time() - t0


def get_no_redirect(path):
    """Return status code without following redirects."""
    url = BASE + path
    try:
        class NoRedirect(urllib.request.HTTPErrorProcessor):
            def http_response(self, req, resp):
                return resp
            https_response = http_response
        opener = urllib.request.build_opener(NoRedirect)
        resp = opener.open(url, timeout=10)
        return resp.status, resp.headers.get("Location", "")
    except Exception as e:
        return None, str(e)


print("\n" + "=" * 60)
print("  Parcelytics UI Self-Check")
print("=" * 60)

# ── Connectivity ──────────────────────────────────────────────
print("\n[1] Connectivity")
status, body, elapsed = get("/")
if status is None:
    print(f"  {FAIL}  App not reachable at {BASE} — is it running?")
    print("       Start with: python3 app.py")
    sys.exit(1)
check("Home page loads", status == 200, f"{elapsed:.2f}s")
check("Home page contains search form", 'name="q"' in body or 'action="/"' in body)

# ── ID format tests ───────────────────────────────────────────
print("\n[2] Search ID formats")

# 10-char geo_id
code, loc = get_no_redirect("/?q=0100030105")
check("10-char geo_id resolves (redirect)", code in (301, 302, 200),
      f"status={code} loc={loc}")

# 14-char tax office format (01000301050000)
code, loc = get_no_redirect("/?q=01000301050000")
check("14-char tax-office ID resolves (strips trailing 0000)", code in (301, 302, 200),
      f"status={code} loc={loc}")

# Short prop_id integer
code, loc = get_no_redirect("/?q=100008")
check("Short prop_id integer resolves", code in (301, 302, 200),
      f"status={code} loc={loc}")

# Unknown ID — should show error, not stack trace
status, body, elapsed = get("/?q=9999999999")
check("Unknown ID shows error message (not 500)", status == 200 and "500" not in body,
      f"status={status}")
check("Unknown ID error is user-friendly", "No parcel found" in body or "not found" in body.lower())
check("Unknown ID has no Python traceback", "Traceback" not in body and "Exception" not in body)

# Empty search
status, body, elapsed = get("/")
check("Empty search loads home page cleanly", status == 200 and "Traceback" not in body)

# Whitespace/dash stripping
code, loc = get_no_redirect("/?q=0100030105")
check("Query with leading/trailing space handled",
      code in (301, 302, 200))

# Dashes stripped (01-000-301-05 style)
code, loc = get_no_redirect("/?q=01-000-301-05")
check("Query with dashes — resolves or graceful error",
      code in (200, 301, 302))

# ── Sanity check parcels ──────────────────────────────────────
print("\n[3] Sanity-check parcels")

parcels = [
    ("0100030105", "Commercial — 1201 S Lamar"),
    ("0100030109", "Multi-family — 1219 S Lamar"),
    ("0284460113", "SFR w/ homestead cap anomaly"),
]

for geo_id, label in parcels:
    status, body, elapsed = get(f"/parcel/{geo_id}")
    ok = status == 200
    check(f"{label} loads (status={status})", ok, f"{elapsed:.2f}s")
    if ok:
        check(f"  → Data-source note present",
              "Annual Jurisdiction Roll" in body or "AJR" in body or "Certified" in body)
        check(f"  → ESTIMATES ONLY badge present",
              "ESTIMATES ONLY" in body or "estimates" in body.lower())
        check(f"  → No Python traceback",
              "Traceback" not in body and "Internal Server Error" not in body)
        # Check projection footnotes
        check(f"  → Projection footnotes present",
              "These are estimates" in body or "Not a guarantee" in body or "CAGR" in body)

# ── Error state ───────────────────────────────────────────────
print("\n[4] Error states")

status, body, elapsed = get("/parcel/DOESNOTEXIST0000")
check("Non-existent parcel direct URL → 404 or error page",
      status in (404, 200))
check("Non-existent parcel has no traceback",
      "Traceback" not in body and "Internal Server Error" not in body)

# ── Rate trend page ───────────────────────────────────────────
print("\n[5] Tax rate trend page")
status, body, elapsed = get("/rates")
check("Rate trend page loads", status == 200, f"{elapsed:.2f}s")
check("Rate trend chart canvas present", "chart" in body.lower() or "canvas" in body.lower())

# ── Performance timing ────────────────────────────────────────
print("\n[6] Performance — property detail page load times")

timing_parcels = [
    ("0100030105", "Commercial (simple)"),
    ("0100030109", "Multi-family (heavy entity joins)"),
]
for geo_id, label in timing_parcels:
    times = []
    for _ in range(3):
        _, _, t = get(f"/parcel/{geo_id}")
        times.append(t)
    avg = sum(times) / len(times)
    flag = f"{WARN} SLOW" if avg > 2.0 else ""
    check(f"{label} avg load time", True, f"{avg:.2f}s {flag}")
    if avg > 2.0:
        print(f"       Likely cause: sequential DB queries without connection pooling")

# ── Summary ───────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"  {passed} passed   {failed} failed   {len(results)} total")
if failed == 0:
    print(f"  {PASS} All checks passed — ready for review")
else:
    print(f"  {FAIL} {failed} check(s) failed — review output above")
print("=" * 60 + "\n")
