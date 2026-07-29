#!/usr/bin/env python3
"""
test_verify_parcel_filters_coverage.py — fixture tests for
verify_parcel_filters_coverage.py (Brief 4, "Build the actual
parcel-exclusion regression test").

Same pattern as loaders/test_ingest_gate.py: every check_* function in
verify_parcel_filters_coverage.py is a pure function (an optional
`files_override` dict of {relpath: source_text} in, `(passed, detail,
offenders)` out — no real-repo dependency required), so it's fixture-
tested directly here rather than only exercised against the live repo.

Per the brief's explicit requirement ("confirm it would fail if you
temporarily reintroduce a re-typed copy somewhere — a deliberate-
corruption test, same pattern as the ingest gate's own alarm tests"),
this file includes FOUR deliberate-corruption cases that must FAIL (not
just pass-case sanity tests):
  1. test_excl_consumers_deliberate_corruption_retyped_literal — a
     consumer function that stopped importing/referencing
     CANONICAL_PARCEL_EXCL and instead re-typed the raw fragment inline.
  2. test_no_retyped_exclusion_deliberate_corruption_full_copy — a file
     containing a hand-retyped copy of all three exclusion legs (X%, N%,
     AJR%) in the same statement, exactly the "drift" scenario
     parcel_filters.py's own docstring describes as the original bug.
  3. test_no_retyped_peer_match_deliberate_corruption_raw_comparison — a
     file containing a raw, non-NULL-safe `LEFT(state_cd1,1) = %(sc1)s`
     peer-matching comparison instead of a peer_state_cd1_match_sql() call.
  4. test_no_duplicate_symbols_deliberate_corruption_redefined_constant —
     a second file that redefines CANONICAL_PARCEL_EXCL itself.

Each corruption fixture is paired with a "clean" counterpart proving the
same check function PASSES on well-formed input, so a corruption-case
failure can't be explained away as "the check always fails."

Run: python3 test_verify_parcel_filters_coverage.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_parcel_filters_coverage as vpfc

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Check 1: consumers reference the canonical symbol ──────────────────────
CLEAN_APP_PY = """
from parcel_filters import CANONICAL_PARCEL_EXCL, peer_state_cd1_match_sql

def _compute_snapshot_data(view):
    canonical_excl = CANONICAL_PARCEL_EXCL
    query(f"... {canonical_excl} ...")

@app.route("/snapshot/neighborhood/<code>")
def snapshot_neighborhood(code):
    query(f"... {CANONICAL_PARCEL_EXCL} ...")

@app.route("/api/benchmark")
def api_benchmark():
    excl_filter = CANONICAL_PARCEL_EXCL

@app.route("/parcels")
def parcel_list():
    query(f"... {CANONICAL_PARCEL_EXCL} ...")
"""

CORRUPTED_APP_PY_RETYPED_LITERAL = """
from parcel_filters import CANONICAL_PARCEL_EXCL, peer_state_cd1_match_sql

def _compute_snapshot_data(view):
    # DRIFT: someone "helpfully" inlined the fragment instead of importing it
    canonical_excl = "AND state_cd1 NOT LIKE 'X%%' AND state_cd1 NOT LIKE 'N%%' AND geo_id NOT LIKE 'AJR%%'"
    query(f"... {canonical_excl} ...")

@app.route("/snapshot/neighborhood/<code>")
def snapshot_neighborhood(code):
    query(f"... {CANONICAL_PARCEL_EXCL} ...")

@app.route("/api/benchmark")
def api_benchmark():
    excl_filter = CANONICAL_PARCEL_EXCL

@app.route("/parcels")
def parcel_list():
    query(f"... {CANONICAL_PARCEL_EXCL} ...")
"""

CLEAN_COMPUTE_METRICS_PY = """
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE

def _exclude_clause():
    return f"AND ({CANONICAL_PARCEL_EXCL_BARE})"

def compute_county_benchmarks():
    excl = _exclude_clause()
    query(f"WHERE ... {excl}")
"""


def test_excl_consumers_pass_on_clean_fixture():
    files = {"app.py": CLEAN_APP_PY, "loaders/compute_metrics.py": CLEAN_COMPUTE_METRICS_PY}
    passed, detail, problems = vpfc.check_excl_consumers_reference_canonical(files)
    check("CLEAN fixture: all 5 exclusion consumers verified", passed, detail)


def test_excl_consumers_deliberate_corruption_retyped_literal():
    """DELIBERATE CORRUPTION CASE 1: _compute_snapshot_data stops
    referencing CANONICAL_PARCEL_EXCL and re-types the fragment inline
    instead. This MUST fail the check."""
    files = {"app.py": CORRUPTED_APP_PY_RETYPED_LITERAL, "loaders/compute_metrics.py": CLEAN_COMPUTE_METRICS_PY}
    passed, detail, problems = vpfc.check_excl_consumers_reference_canonical(files)
    check(
        "CORRUPTION CASE: retyped-literal consumer correctly FAILS",
        passed is False and any(p[1] == "_compute_snapshot_data" for p in problems),
        detail,
    )


def test_excl_consumers_wrapper_indirection_still_passes():
    """Confirms the KNOWN_WRAPPER_INDIRECTIONS fix (bug #1 from the first
    real run) actually works: compute_county_benchmarks() only references
    CANONICAL_PARCEL_EXCL_BARE through its _exclude_clause() helper, not
    directly -- this must still PASS, not be a false failure."""
    files = {"app.py": CLEAN_APP_PY, "loaders/compute_metrics.py": CLEAN_COMPUTE_METRICS_PY}
    passed, detail, problems = vpfc.check_excl_consumers_reference_canonical(files)
    check(
        "Wrapper-indirection fixture (compute_county_benchmarks -> _exclude_clause) passes, not a false failure",
        passed,
        detail,
    )


def test_excl_consumers_wrapper_indirection_broken_case_still_fails():
    """If the wrapper itself stops referencing the canonical symbol (the
    indirection is real but now points at nothing), the check must still
    catch it -- proving the wrapper-indirection fix isn't a blanket pass."""
    broken_wrapper = """
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE

def _exclude_clause():
    return "AND state_cd1 NOT LIKE 'X%%'"  # BROKEN: no longer uses the canonical symbol

def compute_county_benchmarks():
    excl = _exclude_clause()
    query(f"WHERE ... {excl}")
"""
    files = {"app.py": CLEAN_APP_PY, "loaders/compute_metrics.py": broken_wrapper}
    passed, detail, problems = vpfc.check_excl_consumers_reference_canonical(files)
    check(
        "CORRUPTION CASE: wrapper indirection pointing at nothing correctly FAILS",
        passed is False and any(p[1] == "compute_county_benchmarks" for p in problems),
        detail,
    )


# ── Check 2: peer-match consumers reference peer_state_cd1_match_sql() ─────
CLEAN_PEER_APP_PY = """
from parcel_filters import peer_state_cd1_match_sql

@app.route("/api/peer_benchmark_local/<geo_id>")
def api_peer_benchmark_local(geo_id):
    _peer_match = peer_state_cd1_match_sql()
    query(f"... {_peer_match} ...")
    query(f"... {_peer_match} ...")

@app.route("/api/peer_benchmark_sf/<geo_id>")
def api_peer_benchmark_sf(geo_id):
    _peer_match = peer_state_cd1_match_sql()
    query(f"... {_peer_match} ...")

@app.route("/api/peer_set/<geo_id>")
def api_peer_set(geo_id):
    _peer_match_upper = peer_state_cd1_match_sql(upper=True)
    query(f"... {_peer_match_upper} ...")
"""

CORRUPTED_PEER_APP_PY = """
from parcel_filters import peer_state_cd1_match_sql

@app.route("/api/peer_benchmark_local/<geo_id>")
def api_peer_benchmark_local(geo_id):
    # DRIFT: someone reverted to a raw, non-NULL-safe comparison
    query("... WHERE LEFT(p.state_cd1, 1) = %(sc1)s ...")

@app.route("/api/peer_benchmark_sf/<geo_id>")
def api_peer_benchmark_sf(geo_id):
    _peer_match = peer_state_cd1_match_sql()
    query(f"... {_peer_match} ...")

@app.route("/api/peer_set/<geo_id>")
def api_peer_set(geo_id):
    _peer_match_upper = peer_state_cd1_match_sql(upper=True)
    query(f"... {_peer_match_upper} ...")
"""


def test_peer_match_consumers_pass_on_clean_fixture():
    files = {"app.py": CLEAN_PEER_APP_PY}
    passed, detail, problems = vpfc.check_peer_match_consumers_reference_canonical(files)
    check("CLEAN fixture: all 3 peer-match consumers verified", passed, detail)


def test_peer_match_consumers_deliberate_corruption_reverted_to_raw():
    """DELIBERATE CORRUPTION CASE: api_peer_benchmark_local reverts to a
    raw LEFT(state_cd1,1)=%(sc1)s comparison instead of calling
    peer_state_cd1_match_sql(). This MUST fail the check."""
    files = {"app.py": CORRUPTED_PEER_APP_PY}
    passed, detail, problems = vpfc.check_peer_match_consumers_reference_canonical(files)
    check(
        "CORRUPTION CASE: reverted-to-raw-comparison consumer correctly FAILS",
        passed is False and any(p[1] == "api_peer_benchmark_local" for p in problems),
        detail,
    )


# ── Check 3: no retyped exclusion-fragment copy ─────────────────────────────
def test_no_retyped_exclusion_pass_on_clean_fixture():
    files = {"app.py": CLEAN_APP_PY, "some_other_script.py": "print('AJR%% mentioned but no full copy')"}
    passed, detail, offenders = vpfc.check_no_retyped_exclusion_fragment(files)
    check("CLEAN fixture: no retyped exclusion-fragment copy found", passed, detail)


def test_no_retyped_exclusion_deliberate_corruption_full_copy():
    """DELIBERATE CORRUPTION CASE 2: a new standalone script hand-retypes
    all three legs of the exclusion fragment in one statement -- exactly
    the historical drift parcel_filters.py's own docstring describes
    (the /parcels route's pre-fix copy that was missing a leg, generalized
    here to a full three-leg retype). This MUST fail the check."""
    corrupted_script = """
def get_county_total():
    query('''
        WHERE state_cd1 NOT LIKE 'X%%'
          AND state_cd1 NOT LIKE 'N%%'
          AND geo_id NOT LIKE 'AJR%%'
    ''')
"""
    files = {"some_new_script.py": corrupted_script}
    passed, detail, offenders = vpfc.check_no_retyped_exclusion_fragment(files)
    check(
        "CORRUPTION CASE: full three-leg retyped copy correctly FAILS",
        passed is False and any(o[0] == "some_new_script.py" for o in offenders),
        detail,
    )


# ── Check 4: no retyped peer-match fragment ─────────────────────────────────
def test_no_retyped_peer_match_pass_on_clean_fixture():
    files = {"app.py": CLEAN_PEER_APP_PY}
    passed, detail, offenders = vpfc.check_no_retyped_peer_match_fragment(files)
    check("CLEAN fixture: no retyped peer-match fragment found", passed, detail)


def test_no_retyped_peer_match_deliberate_corruption_raw_comparison():
    """DELIBERATE CORRUPTION CASE 3: a new standalone script hand-writes a
    raw, non-NULL-safe LEFT(state_cd1,1)=%(sc1)s peer-matching comparison
    instead of importing peer_state_cd1_match_sql(). This MUST fail."""
    corrupted_script = """
def get_peer_pool():
    query("SELECT * FROM parcel WHERE LEFT(state_cd1, 1) = %(sc1)s")
"""
    files = {"some_new_peer_script.py": corrupted_script}
    passed, detail, offenders = vpfc.check_no_retyped_peer_match_fragment(files)
    check(
        "CORRUPTION CASE: raw peer-match comparison correctly FAILS",
        passed is False and any(o[0] == "some_new_peer_script.py" for o in offenders),
        detail,
    )


def test_no_retyped_peer_match_classification_case_expression_not_flagged():
    """Confirms the bug #3 regex fix (require a param/variable RHS, not a
    quoted string literal) actually works: a type-CLASSIFICATION CASE
    expression like `LEFT(state_cd1,1) = 'A'` (query_2026_vs_2025.py's real
    pattern) is a different construct from peer-matching and must NOT be
    flagged -- proving this isn't just a blanket string-literal allowlist
    that would also hide a genuine corruption."""
    classification_script = """
def label_rows():
    query('''
        CASE WHEN LEFT(state_cd1,1) = 'A' THEN 'Residential (A)'
             WHEN LEFT(state_cd1,1) = 'B' THEN 'Multi-Family (B)'
        END
    ''')
"""
    files = {"some_report_script.py": classification_script}
    passed, detail, offenders = vpfc.check_no_retyped_peer_match_fragment(files)
    check(
        "Type-classification CASE expression (quoted-letter RHS) correctly NOT flagged",
        passed,
        detail,
    )


# ── Check 5: no duplicate symbol definitions ────────────────────────────────
def test_no_duplicate_symbols_pass_on_clean_fixture():
    files = {"app.py": CLEAN_APP_PY}
    passed, detail, offenders = vpfc.check_no_duplicate_symbol_definitions(files)
    check("CLEAN fixture: no duplicate symbol definitions found", passed, detail)


def test_no_duplicate_symbols_deliberate_corruption_redefined_constant():
    """DELIBERATE CORRUPTION CASE 4: a second file redefines
    CANONICAL_PARCEL_EXCL itself (e.g. a copy-pasted local override that
    shadows the import). This MUST fail the check."""
    corrupted_script = """
CANONICAL_PARCEL_EXCL = "AND state_cd1 NOT LIKE 'X%%'"

def use_it():
    query(f"WHERE ... {CANONICAL_PARCEL_EXCL}")
"""
    files = {"some_shadowing_script.py": corrupted_script}
    passed, detail, offenders = vpfc.check_no_duplicate_symbol_definitions(files)
    check(
        "CORRUPTION CASE: redefined CANONICAL_PARCEL_EXCL constant correctly FAILS",
        passed is False and any(o[0] == "some_shadowing_script.py" for o in offenders),
        detail,
    )


# ── Check import requirements ───────────────────────────────────────────────
def test_import_requirements_pass_on_clean_fixture():
    files = {"app.py": CLEAN_APP_PY, "loaders/compute_metrics.py": CLEAN_COMPUTE_METRICS_PY}
    passed, detail, missing = vpfc.check_import_requirements(files)
    check("CLEAN fixture: import requirements satisfied", passed, detail)


def test_import_requirements_deliberate_corruption_missing_import():
    """DELIBERATE CORRUPTION CASE: app.py stops importing
    peer_state_cd1_match_sql from parcel_filters entirely (as if someone
    deleted the import while leaving stale call sites, or vice versa).
    This MUST fail."""
    files = {
        "app.py": "from parcel_filters import CANONICAL_PARCEL_EXCL\n# peer_state_cd1_match_sql import removed\n",
        "loaders/compute_metrics.py": CLEAN_COMPUTE_METRICS_PY,
    }
    passed, detail, missing = vpfc.check_import_requirements(files)
    check(
        "CORRUPTION CASE: missing required import correctly FAILS",
        passed is False and any(m[0] == "app.py" for m in missing),
        detail,
    )


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print()
    if not FAILURES:
        print(f"ALL {len(tests)} FIXTURE TESTS PASSED")
        return 0
    else:
        print(f"{len(FAILURES)}/{len(tests)} FIXTURE TESTS FAILED: {FAILURES}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
