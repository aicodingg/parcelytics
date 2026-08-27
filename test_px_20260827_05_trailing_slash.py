"""
test_px_20260827_05_trailing_slash.py — fixture test for PX-20260827-05
(trailing-slash routing gap on the bare county-home route).

Bug: `/<county_slug>` (index(), app.py) is registered without a trailing
slash. Flask's default exact-match routing behavior means a route
registered WITHOUT a trailing slash only ever matches the exact form --
`/dallas-tx` resolves, `/dallas-tx/` 404s. (The reverse direction -- a
route registered WITH a trailing slash auto-redirecting the no-slash form
to it -- is Flask's other, well-known trailing-slash behavior; it doesn't
apply here since this route has no trailing slash in its rule string.)

Fix: `strict_slashes=False` on the @app.route decorator, which makes
Werkzeug's routing accept BOTH forms for this rule without a redirect.

Sandbox constraint (same as every other verify_*/test_* script in this
repo that touches app.py or the Flask/psycopg2 stack): this sandbox has no
Flask installed and no network access to install it (confirmed via `pip
install flask` failing against the proxy allowlist), so this can't be a
live werkzeug Map/MapAdapter routing test. Falls back to a source-level
assertion against the real, shipping app.py -- the same pattern already
used elsewhere in this repo (e.g. TAX-BILLING-REKEY-4's SQL-string
assertions) when the runtime dependency isn't available in-sandbox. This
still catches the real regression classes: the decorator losing
strict_slashes=False again, or the route rule string itself changing
shape without the flag being re-applied.

Run: python3 test_px_20260827_05_trailing_slash.py
"""
import re
import sys

APP_PY = "app.py"

ALL_OK = True


def fail(msg):
    global ALL_OK
    ALL_OK = False
    print(f"FAIL: {msg}")


def ok(msg):
    print(f"PASS: {msg}")


def main():
    src = open(APP_PY).read()

    # Find the index() route's own @app.route(...) decorator line -- the
    # one immediately preceding "def index():" -- rather than matching any
    # route string, so this stays correct if other routes are added/moved
    # around it.
    m = re.search(
        r'@app\.route\((?P<args>[^)]*)\)\s*\n(?:@[^\n]*\n)*def index\(\):',
        src,
    )
    if not m:
        fail("could not locate index()'s @app.route(...) decorator in app.py -- route may have moved/been renamed")
        print()
        print("SOME CHECKS FAILED" if not ALL_OK else "ALL CHECKS PASSED")
        return 1

    decorator_args = m.group("args")
    ok(f"located index()'s @app.route decorator: @app.route({decorator_args})")

    if '"/<county_slug>"' not in decorator_args and "'/<county_slug>'" not in decorator_args:
        fail(f"index()'s route rule string is no longer '/<county_slug>' -- got: {decorator_args!r}")
    else:
        ok("index() is still registered at the bare '/<county_slug>' rule (the route this bug is about)")

    if "strict_slashes=False" in decorator_args:
        ok("index()'s @app.route carries strict_slashes=False -- both '/dallas-tx' and '/dallas-tx/' now match this rule")
    else:
        fail("index()'s @app.route is missing strict_slashes=False -- the trailing-slash form will 404 again")

    # Negative control: confirm this isn't a blanket flag search-and-replace
    # artifact -- a couple of OTHER, unrelated county-anchored routes
    # (multi-segment, not affected by this specific bug) should NOT have
    # picked up strict_slashes=False as a side effect of this fix.
    for other_route, other_endpoint in [
        ('"/<county_slug>/search"', "def search_page"),
        ('"/<county_slug>/info"', "def info"),
    ]:
        m2 = re.search(
            r'@app\.route\((?P<args>[^)]*)\)\s*\n(?:@[^\n]*\n)*' + re.escape(other_endpoint) + r'\(',
            src,
        )
        if not m2:
            # Not fatal to this test's purpose -- just skip if the route
            # name/shape has changed since; the real assertion above
            # already covers the actual bug.
            continue
        if "strict_slashes=False" in m2.group("args"):
            fail(f"{other_endpoint} unexpectedly also has strict_slashes=False -- this fix should be scoped to index() only, not applied blanket")
        else:
            ok(f"{other_endpoint} correctly untouched (no strict_slashes=False) -- fix is scoped to index() only")

    print()
    if ALL_OK:
        print("ALL CHECKS PASSED")
        return 0
    else:
        print("SOME CHECKS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
