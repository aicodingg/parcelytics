#!/usr/bin/env python3
"""
loaders/test_px_20260901_02.py — PX-20260901-02 HOTFIX fixture tests.

Same sandbox-limitation disclosure as loaders/test_px_20260831_02_tasks_3_4_5.py:
psycopg2 isn't installed here, so importing loaders.compute_metrics requires a
fake psycopg2 registered in sys.modules first.

This file proves:

  1. TASK 1 (county_benchmark prev_count scoping): source-text assertion
     against compute_metrics.py's REAL shipping source (same technique as
     Task 5's join-scoping checks in test_px_20260831_02_tasks_3_4_5.py) --
     proving compute_county_benchmarks()'s prev_count SELECT now carries
     `WHERE county_code = %s`, matching compute_parcel_metrics()'s
     already-correct prev_count pattern exactly. Also proves, by ABSENCE,
     that refresh_group_stats.py and refresh_snapshot_summary.py contain no
     `_assert_row_count_sane`-style prev_count comparison at all -- both
     files rebuild via a one-pass, all-counties shadow-swap with no
     drop-detection gate, so there was no second unscoped call site to fix
     in either file (the audit's actual finding, not assumed).

  2. TASK 1 (--benchmarks-only): source-text assertion that
     --benchmarks-only's branch in main() returns immediately after calling
     compute_county_benchmarks() alone, never calling compute_parcel_metrics()
     anywhere in that branch -- the re-run must not touch the already-committed
     3.58M Dallas parcel_metrics rows.

  3. TASK 3 (explain_compute_metrics_passes.py --county flag): source-text
     assertion that the script's argparse defines --county and that it is
     used, not ignored, to bind every EXPLAINed statement's params.

  4. TASK 3 (fake-psycopg2 guard): exercises
     explain_compute_metrics_passes.py's real _install_fake_psycopg2()
     function directly against a FAKE "real" psycopg2 module pre-registered
     in sys.modules, proving the guard's `try: import psycopg2 ... return`
     early-exit means it NEVER overwrites an already-importable driver with
     the fake stub -- the exact failure mode PM's brief calls out avoiding.
     A second case proves the fallback stub still installs correctly when no
     real driver is importable (the sandbox's own actual condition).

Run: python3 loaders/test_px_20260901_02.py
"""
import importlib
import importlib.util
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _read(relpath):
    with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as f:
        return f.read()


_CM_SRC = _read("loaders/compute_metrics.py")
_CM_NORM = " ".join(_CM_SRC.split())
_RGS_SRC = _read("loaders/refresh_group_stats.py")
_RSS_SRC = _read("loaders/refresh_snapshot_summary.py")
_EXPLAIN_SRC = _read("loaders/explain_compute_metrics_passes.py")


# ── Task 1: county_benchmark prev_count scoping ─────────────────────────────

def test_county_benchmark_prev_count_is_scoped():
    check(
        "compute_county_benchmarks()'s prev_count SELECT now carries "
        "WHERE county_code = %s (was a bare, unscoped COUNT(*) before this hotfix)",
        re.search(
            r"SELECT COUNT\(\*\) FROM county_benchmark WHERE county_code = %s",
            _CM_NORM,
        ) is not None,
    )
    check(
        "the fixed prev_count query is passed (county_code,) as its params",
        re.search(
            r'SELECT COUNT\(\*\) FROM county_benchmark WHERE county_code = %s"\s*,\s*\(county_code,\)\)',
            _CM_NORM,
        ) is not None,
    )


def test_parcel_metrics_prev_count_still_scoped():
    """Confirms the OTHER _assert_row_count_sane call site (already fixed by
    PX-20260828-16-followup, before this hotfix) hasn't regressed."""
    check(
        "compute_parcel_metrics()'s prev_count SELECT still carries WHERE county_code = %s",
        re.search(
            r"SELECT COUNT\(\*\) FROM parcel_metrics WHERE county_code = %s",
            _CM_NORM,
        ) is not None,
    )


def test_only_two_assert_row_count_sane_call_sites_exist():
    """The audit's actual scope: _assert_row_count_sane() is called exactly
    twice in compute_metrics.py (parcel_metrics, county_benchmark) -- both
    are checked above. No third call site exists to miss."""
    call_sites = re.findall(r'_assert_row_count_sane\("(\w+)"', _CM_SRC)
    check("exactly two _assert_row_count_sane call sites: parcel_metrics and county_benchmark",
          sorted(call_sites) == ["county_benchmark", "parcel_metrics"], call_sites)


def test_refresh_group_stats_has_no_unscoped_prev_count():
    """refresh_group_stats.py rebuilds group_stats in one pass across ALL
    counties (per-row county_code derivation, PX-20260831-02) with no
    prior-vs-new row-count drop assertion at all -- confirmed by absence of
    both the assertion helper and any prev_count-style COUNT(*) comparison.
    Nothing to fix here; this proves the audit didn't miss a second site."""
    check("no _assert_row_count_sane call in refresh_group_stats.py",
          "_assert_row_count_sane" not in _RGS_SRC)
    check("no prev_count variable/pattern in refresh_group_stats.py",
          "prev_count" not in _RGS_SRC)


def test_refresh_snapshot_summary_has_no_unscoped_prev_count():
    """Same shape of confirmation for refresh_snapshot_summary.py -- also a
    one-pass, all-counties shadow-swap rebuild (PX-20260831-02 Task 1), no
    drop-detection gate to have gotten wrong."""
    check("no _assert_row_count_sane call in refresh_snapshot_summary.py",
          "_assert_row_count_sane" not in _RSS_SRC)
    check("no prev_count variable/pattern in refresh_snapshot_summary.py",
          "prev_count" not in _RSS_SRC)


def test_benchmarks_only_never_calls_compute_parcel_metrics():
    """--benchmarks-only's branch in main(): find the block from
    `if args.benchmarks_only:` to the next `return`, and assert
    compute_parcel_metrics is never named inside it."""
    m = re.search(
        r"if args\.benchmarks_only:(.*?)\n\s*return\n",
        _CM_SRC, re.DOTALL,
    )
    check("found the --benchmarks-only branch in main()", m is not None)
    if m:
        branch = m.group(1)
        check("compute_parcel_metrics is never called inside the --benchmarks-only branch",
              "compute_parcel_metrics(" not in branch, branch)
        check("compute_county_benchmarks IS called inside the --benchmarks-only branch",
              "compute_county_benchmarks(" in branch)


# ── Task 3: explain_compute_metrics_passes.py --county flag ────────────────

def test_explain_script_has_county_flag_and_uses_it():
    check('explain_compute_metrics_passes.py argparse defines --county',
          '"--county"' in _EXPLAIN_SRC)
    check('main() falls back to DEFAULT_COUNTY only when --county is not passed',
          "county = args.county or cm.DEFAULT_COUNTY" in _EXPLAIN_SRC)
    check('the resolved county is printed in the pre-run proof output '
          '(so the rendered plans are attributable to a specific county)',
          "county_code bound to every statement" in _EXPLAIN_SRC)
    # Precise assertion: every params tuple in `ordered` is built from the
    # `county` variable, never a literal string like "DALLAS".
    ordered_block = re.search(r"ordered = \[(.*?)\n    \]", _EXPLAIN_SRC, re.DOTALL)
    check("found the `ordered` statement list", ordered_block is not None)
    if ordered_block:
        block = ordered_block.group(1)
        check("no hardcoded county literal (e.g. 'DALLAS') inside the ordered statement params",
              "'DALLAS'" not in block and '"DALLAS"' not in block, block)
        check("all five entries bind params via the `county` variable",
              block.count("(county,") + block.count("(county, county)") >= 5
              or block.count("county") >= 5, block)


# ── Task 3: fake-psycopg2 guard never shadows a real driver ────────────────

def _load_explain_module_fresh():
    """Imports explain_compute_metrics_passes.py as a standalone module
    (not via `import loaders...`, since this script lives in loaders/ and
    the test needs to call its private _install_fake_psycopg2() directly)."""
    spec = importlib.util.spec_from_file_location(
        "explain_compute_metrics_passes_under_test",
        os.path.join(REPO_ROOT, "loaders", "explain_compute_metrics_passes.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_psycopg2_guard_never_shadows_real_driver():
    # Save/restore sys.modules state around this test -- it deliberately
    # mutates sys.modules["psycopg2"] and must not leak into other tests or
    # the surrounding test run.
    saved = {k: sys.modules.get(k) for k in ("psycopg2", "psycopg2.extras")}
    try:
        for k in ("psycopg2", "psycopg2.extras"):
            sys.modules.pop(k, None)

        # Case A: a "real" psycopg2 is already importable (this is the live
        # finding PM's brief references -- PM's own environment DOES have a
        # real driver, and the guard must never clobber it).
        real_marker = object()
        fake_real_pg2 = types.ModuleType("psycopg2")
        fake_real_pg2.extras = types.ModuleType("psycopg2.extras")
        fake_real_pg2._IS_THE_REAL_ONE = real_marker
        sys.modules["psycopg2"] = fake_real_pg2
        sys.modules["psycopg2.extras"] = fake_real_pg2.extras

        mod = _load_explain_module_fresh()
        mod._install_fake_psycopg2()

        check("when a real psycopg2 is already importable, sys.modules['psycopg2'] "
              "is left completely untouched (identity-equal to the original object)",
              sys.modules["psycopg2"] is fake_real_pg2)
        check("...and its _IS_THE_REAL_ONE marker survives unchanged",
              getattr(sys.modules["psycopg2"], "_IS_THE_REAL_ONE", None) is real_marker)

        # Case B: no real driver importable (the sandbox's actual condition)
        # -- the fallback stub must still install so the rest of the script
        # can be imported/tested at all.
        for k in ("psycopg2", "psycopg2.extras"):
            sys.modules.pop(k, None)

        mod2 = _load_explain_module_fresh()
        mod2._install_fake_psycopg2()

        check("with no real driver importable, the fallback stub is installed",
              "psycopg2" in sys.modules
              and not hasattr(sys.modules["psycopg2"], "_IS_THE_REAL_ONE"))
        check("the fallback stub exposes psycopg2.extras.RealDictCursor and psycopg2.Error "
              "(what loaders/db.py and compute_metrics.py actually import)",
              hasattr(sys.modules["psycopg2"].extras, "RealDictCursor")
              and hasattr(sys.modules["psycopg2"], "Error"))
    finally:
        for k in ("psycopg2", "psycopg2.extras"):
            sys.modules.pop(k, None)
            if saved.get(k) is not None:
                sys.modules[k] = saved[k]


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL PX-20260901-02 FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
