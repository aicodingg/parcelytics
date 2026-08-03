#!/usr/bin/env python3
"""
verify_classification_map_coverage.py — the standing harness assertion
DATA_LIFECYCLE.md Stage 1 and Section 9.4a call for: "the map is one
committed file, shared by every loader and asserted by the harness:
production must contain zero rows outside the allowlist, ever."

Same pattern as verify_parcel_filters_coverage.py (this repo, same
session): a real, callable, independently fixture-testable script, not a
promise in a comment. Two things this script does NOT do, disclosed here
rather than silently:

  1. It does not run itself against live production -- this sandbox has
     no live database connection (same standing disclosure as every other
     script in this repo that says so). --live mode (below) is real code,
     written to run correctly against Postgres, but has only been
     exercised in this session via --fixture mode against synthetic rows,
     never against the real `parcel` table. Diego must run --live himself.

  2. It is not wired into CI or a pre-deploy check. Per this brief's own
     scope ("ideally something that could be wired into CI/a pre-deploy
     check eventually -- not required to wire that up in this brief"),
     this script is built to be CI-wireable (single process, clean exit
     code, no interactive prompts) but that wiring is not done here.

Two modes:

  --fixture   Runs the built-in synthetic pass/fail scenarios (no DB
              needed). This is what proves the *mechanism* works.
  --live      Connects to the real database (via config.py / loaders/db.py,
              same connection convention as every other script in this
              repo) and asserts zero real `parcel` rows have a state_cd1
              value that classifies to anything other than REAL_PROPERTY
              (matching NULL_STATE_CD1_BUCKET's own REAL_PROPERTY value for
              NULL rows) or that raises UnknownClassCodeError. This is the
              actual DATA_LIFECYCLE.md-mandated production assertion --
              Diego's job to run, disclosed above.

Run:
    python3 verify_classification_map_coverage.py --fixture
    python3 verify_classification_map_coverage.py --live   (Diego only, needs real DB)
"""
import sys

from classification_map import (
    CLASSIFICATION_MAP,
    REAL_PROPERTY,
    PERSONAL_PROPERTY,
    EXEMPT_SYNTHETIC,
    UnknownClassCodeError,
    classify_state_cd1,
    enforce_real_property_only,
    classification_report,
)

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# ── Fixture scenarios ─────────────────────────────────────────────────────

def _rows(*state_cds):
    """Build synthetic row dicts: state_cd1 + a nominal market_value so
    classification_report()'s summed-value path is exercised too."""
    return [{"state_cd1": sc, "market_value": 100_000.0} for sc in state_cds]


def test_all_mapped_codes_classify_without_raising():
    """Every key in CLASSIFICATION_MAP must classify cleanly via
    classify_state_cd1() -- this is a self-consistency check on the map's
    own data structure, not a live-data check."""
    ok = True
    for code, expected_bucket in CLASSIFICATION_MAP.items():
        try:
            got = classify_state_cd1(code)
        except UnknownClassCodeError:
            ok = check(f"'{code}' classifies without raising", False,
                       "raised UnknownClassCodeError for its own map key")
            continue
        ok = check(f"'{code}' classifies to its own mapped bucket",
                    got == expected_bucket, f"got {got}, expected {expected_bucket}") and ok
    return ok


def test_every_bucket_value_is_valid():
    """Every value in CLASSIFICATION_MAP must be one of the three real
    buckets -- UNKNOWN must never appear as a map VALUE (see module
    docstring: UNKNOWN is a lookup-miss outcome, not an assignable bucket)."""
    ok = True
    for code, bucket in CLASSIFICATION_MAP.items():
        ok = check(f"'{code}' bucket is not UNKNOWN", bucket != "UNKNOWN") and ok
        ok = check(f"'{code}' bucket is a real bucket constant",
                    bucket in (REAL_PROPERTY, PERSONAL_PROPERTY, EXEMPT_SYNTHETIC)) and ok
    return ok


def test_known_real_property_batch_passes_cleanly():
    """A batch of only known REAL_PROPERTY codes must pass
    enforce_real_property_only() with a count equal to the row count, and
    must not raise."""
    rows = _rows("A", "A1", "B", "C1", "F", "F2", "O", "M1")
    try:
        n = enforce_real_property_only(rows)
    except Exception as e:
        return check("known real-property batch passes cleanly", False, repr(e))
    return check("known real-property batch passes cleanly", n == len(rows),
                 f"got n={n}, expected {len(rows)}")


def test_null_state_cd1_passes_cleanly():
    """NULL state_cd1 must pass (NULL_STATE_CD1_BUCKET == REAL_PROPERTY),
    not raise UnknownClassCodeError -- see module docstring on why NULL is
    handled distinctly from an unrecognized string."""
    rows = _rows(None, None, "A")
    try:
        n = enforce_real_property_only(rows)
    except Exception as e:
        return check("NULL state_cd1 batch passes cleanly", False, repr(e))
    return check("NULL state_cd1 batch passes cleanly", n == 3, f"got n={n}")


def test_deliberate_unknown_code_halts_the_load():
    """THE deliberate-failure test this brief's verification section asks
    for: a genuine, unmapped code must raise UnknownClassCodeError, not be
    silently dropped or silently counted as passing. Uses 'Q9', a value
    guaranteed absent from CLASSIFICATION_MAP (no Texas Comptroller code
    starts with Q in any documentation this codebase carries)."""
    rows = _rows("A", "A1", "Q9", "F")
    try:
        enforce_real_property_only(rows)
    except UnknownClassCodeError as e:
        has_code = "Q9" in str(e)
        has_count = "x1" in str(e) or "x1," in str(e) or "'Q9' x1" in str(e)
        ok = check("unknown code 'Q9' halts the load (raises UnknownClassCodeError)", True)
        ok = check("halt error message names the offending code", has_code, str(e)) and ok
        return ok
    except Exception as e:
        return check("unknown code 'Q9' halts the load (raises UnknownClassCodeError)",
                      False, f"raised wrong exception type: {repr(e)}")
    return check("unknown code 'Q9' halts the load (raises UnknownClassCodeError)", False,
                 "no exception raised -- the load was NOT halted, this is the exact "
                 "failure mode Section 9.4a exists to prevent")


def test_multiple_distinct_unknown_codes_all_reported():
    """The halt error must name EVERY distinct unknown code found, not
    just the first one -- 'a clear, actionable error', per this brief's
    1c requirement, means a human doesn't have to fix-rerun-discover-fix
    one code at a time."""
    rows = _rows("A", "Q9", "Q9", "Z7", "F")
    try:
        enforce_real_property_only(rows)
    except UnknownClassCodeError as e:
        msg = str(e)
        ok = check("both distinct unknown codes named in one halt", "Q9" in msg and "Z7" in msg, msg)
        ok = check("repeated unknown code's count reflected (Q9 x2)", "x2" in msg, msg) and ok
        return ok
    return check("both distinct unknown codes named in one halt", False, "no exception raised")


def test_personal_property_code_reaching_enforcement_raises_value_error():
    """A batch that includes a PERSONAL_PROPERTY-classified code (L1) must
    NOT silently pass enforce_real_property_only() -- that function's
    contract is 'this batch should already be 100% real property'; a
    known-but-wrong-bucket code reaching it is a caller-side scoping bug
    and gets ValueError, distinct from the UNKNOWN halt path."""
    rows = _rows("A", "L1", "F")
    try:
        enforce_real_property_only(rows)
    except UnknownClassCodeError:
        return check("known personal-property code raises ValueError not UnknownClassCodeError",
                     False, "raised UnknownClassCodeError -- L1 IS mapped, this is the wrong path")
    except ValueError as e:
        return check("known personal-property code raises ValueError, names L1",
                     "L1" in str(e) and "PERSONAL_PROPERTY" in str(e), str(e))
    return check("personal-property code reaching enforcement raises", False, "no exception raised")


def test_exempt_synthetic_code_reaching_enforcement_raises_value_error():
    rows = _rows("A", "X", "F")
    try:
        enforce_real_property_only(rows)
    except ValueError as e:
        return check("known exempt code raises ValueError, names X",
                     "X" in str(e) and "EXEMPT_SYNTHETIC" in str(e), str(e))
    return check("exempt-synthetic code reaching enforcement raises ValueError", False,
                 "wrong or no exception raised")


def test_classification_report_buckets_and_sums_correctly():
    """classification_report() -- the PM checklist's 'counts + summed
    value per prefix per bucket' -- must group correctly and sum
    market_value correctly, including the UNKNOWN bucket for an
    unrecognized code (report mode does not raise, by design -- see its
    own docstring)."""
    rows = [
        {"state_cd1": "A", "market_value": 100_000.0},
        {"state_cd1": "A", "market_value": 200_000.0},
        {"state_cd1": "L1", "market_value": 50_000.0},
        {"state_cd1": "Q9", "market_value": 999.0},
        {"state_cd1": None, "market_value": 10_000.0},
    ]
    report = classification_report(rows)
    ok = check("report has REAL_PROPERTY bucket with 'A' code",
               "A" in report.get(REAL_PROPERTY, {}))
    ok = check("report 'A' count is 2", report[REAL_PROPERTY]["A"]["count"] == 2) and ok
    ok = check("report 'A' summed market_value is 300,000",
               report[REAL_PROPERTY]["A"]["market_value"] == 300_000.0) and ok
    ok = check("report has PERSONAL_PROPERTY bucket with 'L1' code",
               "L1" in report.get(PERSONAL_PROPERTY, {})) and ok
    ok = check("report has UNKNOWN bucket with 'Q9' code",
               "Q9" in report.get("UNKNOWN", {})) and ok
    ok = check("report NULL row lands under REAL_PROPERTY '(null/blank)'",
               "(null/blank)" in report.get(REAL_PROPERTY, {})) and ok
    return ok


def run_fixture_mode():
    print("=" * 70)
    print("  Classification Map coverage — FIXTURE mode (no DB)")
    print("=" * 70)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} FIXTURE CHECKS PASSED")
    return 0


# ── Live mode — Diego's job, not run from this sandbox ───────────────────

def run_live_mode():
    """Connects to the real database and asserts zero `parcel` rows fall
    outside the REAL_PROPERTY allowlist (per this map's classification),
    printing the same counts+summed-value classification_report() shape
    the Stage-1 PM checklist asks for. Written to be correct, never
    executed against a live connection from this sandbox -- see module
    docstring's disclosure."""
    import config
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
                             dbname=config.DB_NAME, user=config.DB_USER,
                             password=config.DB_PASS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    print("=" * 70)
    print("  Classification Map coverage — LIVE mode (real production `parcel` table)")
    print("=" * 70)
    cur.execute("SELECT state_cd1, market_value FROM parcel")
    rows = cur.fetchall()
    conn.close()
    print(f"\n  Rows examined: {len(rows):,}")

    report = classification_report(rows, get_state_cd1=lambda r: r["state_cd1"],
                                    get_market_value=lambda r: r["market_value"])
    unknown = report.get("UNKNOWN", {})
    non_real = {b: codes for b, codes in report.items() if b not in ("UNKNOWN", REAL_PROPERTY)}

    print("\n  By bucket:")
    for bucket, codes in sorted(report.items()):
        total_n = sum(c["count"] for c in codes.values())
        total_mv = sum(c["market_value"] for c in codes.values())
        print(f"    {bucket:<20} {total_n:>10,} rows   ${total_mv:>20,.0f}")
        for code, stats in sorted(codes.items(), key=lambda kv: -kv[1]["count"]):
            print(f"        {code:<12} {stats['count']:>10,} rows   ${stats['market_value']:>20,.0f}")

    ok = check("zero UNKNOWN rows in production", len(unknown) == 0,
               f"{sum(c['count'] for c in unknown.values()):,} rows across "
               f"{len(unknown)} distinct unrecognized code(s): {list(unknown.keys())}")
    ok = check("zero non-REAL_PROPERTY rows reachable via this query "
               "(this table should already be real-property-scoped; a hit "
               "here means personal/exempt property reached production)",
               len(non_real) == 0, str(non_real)) and ok

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("\nPRODUCTION IS CLEAN: zero rows outside the REAL_PROPERTY allowlist.")
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(run_live_mode())
    sys.exit(run_fixture_mode())
