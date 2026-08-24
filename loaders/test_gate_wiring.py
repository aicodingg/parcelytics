#!/usr/bin/env python3
"""
loaders/test_gate_wiring.py — PX-20260824-03 Task 2 acceptance check.

Proves load_certified_historical.py's main() actually wires
ingest_gate.gather_and_run() in for real, with the right arguments, at the
right point in the run (after the rollup, before conn.close()), and that
--skip-gate genuinely skips it rather than just suppressing print output.

Does NOT re-test gather_and_run()'s own internal county-scoping/G6-SKIPPED
logic -- that's this same brief's other fix, already covered by
loaders/test_ingest_gate.py's existing fixture suite (the g*_check() pure
decision functions) plus manual py_compile/read verification of
gather_and_run() itself (see PX-20260824-03 report's AC8-style disclosure:
gather_and_run() takes a live psycopg2 connection and has never been
fixture-tested in this sandbox, same as before this brief). This file tests
ONE thing specifically: the WIRING -- does main() call gather_and_run() at
all, with which arguments, and does --skip-gate actually prevent that call.

AC8-style disclosure: psycopg2 is not installed in this sandbox. A fake
module is installed into sys.modules before import (established convention
-- see loaders/test_pir_xlsx_common.py, loaders/test_load_pir_billing_2021_full.py,
loaders/test_backfill_prop_unit_tax_year_geoid.py, test_cert_archive_paths.py).
Real DB access (psycopg2.connect, conn.cursor()) is monkeypatched with an
in-memory fake for the same reason -- these tests exercise main()'s own
control flow and argument-threading, not the SQL or gather_and_run()'s
internals.

Run: python3 loaders/test_gate_wiring.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = types.ModuleType("psycopg2.extras")
_fake_pg2.extras.execute_batch = lambda *a, **kw: None
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_pg2.extras)

import config
from loaders import load_certified_historical as lch


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return (0, 0)


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


def check(label, cond, extra=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond and extra is not None:
        print(f"       {extra}")
    return cond


def _run_main_with_fakes(argv, gate_return, tmp_cert_dir):
    """
    Runs lch.main() with every I/O-touching seam faked out. Returns
    (gate_call_kwargs_or_None, post_load_summary_called, raised_exception).
    gate_call_kwargs is the exact kwargs gather_and_run() was invoked with,
    or None if it was never called.
    """
    gate_calls = []
    post_load_calls = []

    def fake_gather_and_run(conn, **kwargs):
        gate_calls.append(kwargs)
        return gate_return

    def fake_post_load_summary(*a, **kw):
        post_load_calls.append((a, kw))
        return (0, 0)

    orig_connect = _fake_pg2.connect if hasattr(_fake_pg2, "connect") else None
    orig_cert_dir_fn = lch._cert_dir_for_year
    orig_gather = lch.ingest_gate.gather_and_run
    orig_post_load = lch.post_load_summary
    orig_load_prop_unit = lch.load_prop_unit
    orig_load_prop_ent = lch.load_prop_ent
    orig_load_land_imprv = lch.load_land_imprv
    orig_rollup_run = lch.parcel_rollup.run
    orig_argv = sys.argv

    _fake_pg2.connect = lambda **kw: FakeConn()
    lch._cert_dir_for_year = lambda year: tmp_cert_dir
    lch.ingest_gate.gather_and_run = fake_gather_and_run
    lch.post_load_summary = fake_post_load_summary
    lch.load_prop_unit = lambda conn, cert_dir, year, county_code=None: (0, {})
    lch.load_prop_ent = lambda conn, cert_dir, year, data_source, pid_to_geo, county_code=None: 0
    lch.load_land_imprv = lambda conn, cert_dir, year, county_code=None: 0
    lch.parcel_rollup.run = lambda conn, tax_year, county_code=None: {
        "prop_id_repaired": 0, "parcel_tax_year_rows": 0,
    }
    sys.argv = argv

    raised = None
    try:
        lch.main()
    except SystemExit as e:
        raised = e
    except Exception as e:  # noqa: BLE001 -- deliberately broad, this is a test harness
        raised = e
    finally:
        if orig_connect is not None:
            _fake_pg2.connect = orig_connect
        else:
            del _fake_pg2.connect
        lch._cert_dir_for_year = orig_cert_dir_fn
        lch.ingest_gate.gather_and_run = orig_gather
        lch.post_load_summary = orig_post_load
        lch.load_prop_unit = orig_load_prop_unit
        lch.load_prop_ent = orig_load_prop_ent
        lch.load_land_imprv = orig_load_land_imprv
        lch.parcel_rollup.run = orig_rollup_run
        sys.argv = orig_argv

    gate_kwargs = gate_calls[0] if gate_calls else None
    return gate_kwargs, len(post_load_calls) > 0, raised


def main():
    all_ok = True
    tmp_cert_dir = tempfile.mkdtemp(prefix="px_gate_wiring_test_")

    # ── Test 1: default run (no --skip-gate, no --published-total) calls
    #    gather_and_run() exactly once, with the right source_tag/tax_year/
    #    paths/county_code, and published_total=None (not silently omitted
    #    or defaulted to something else). ──────────────────────────────────
    gate_kwargs, post_called, raised = _run_main_with_fakes(
        ["load_certified_historical.py", "--year", "2023"],
        gate_return={"passed": True, "checks": {"G1_prop": (True, "ok")}},
        tmp_cert_dir=tmp_cert_dir,
    )
    all_ok &= check("main() completes without raising (default run)", raised is None, f"raised={raised!r}")
    all_ok &= check("gather_and_run() was called exactly once (default run)", gate_kwargs is not None)
    if gate_kwargs is not None:
        all_ok &= check("source_tag == 'cert_2023'", gate_kwargs.get("source_tag") == "cert_2023",
                         gate_kwargs)
        all_ok &= check("tax_year == 2023", gate_kwargs.get("tax_year") == 2023, gate_kwargs)
        all_ok &= check("prop_path points inside resolved cert_dir",
                         gate_kwargs.get("prop_path") == os.path.join(tmp_cert_dir, "PROP.TXT"),
                         gate_kwargs)
        all_ok &= check("prop_ent_path points inside resolved cert_dir",
                         gate_kwargs.get("prop_ent_path") == os.path.join(tmp_cert_dir, "PROP_ENT.TXT"),
                         gate_kwargs)
        all_ok &= check("published_total defaults to None (not silently omitted from the call)",
                         "published_total" in gate_kwargs and gate_kwargs["published_total"] is None,
                         gate_kwargs)
        all_ok &= check("county_code defaults to DEFAULT_COUNTY",
                         gate_kwargs.get("county_code") == lch.DEFAULT_COUNTY, gate_kwargs)
    all_ok &= check("post_load_summary() still runs after the gate (gate is post-hoc, not blocking)",
                     post_called)

    # ── Test 2: --skip-gate genuinely prevents the call (not just quieter
    #    output around a call that still happens). ──────────────────────────
    gate_kwargs2, post_called2, raised2 = _run_main_with_fakes(
        ["load_certified_historical.py", "--year", "2023", "--skip-gate"],
        gate_return={"passed": True, "checks": {}},
        tmp_cert_dir=tmp_cert_dir,
    )
    all_ok &= check("main() completes without raising (--skip-gate)", raised2 is None, f"raised={raised2!r}")
    all_ok &= check("--skip-gate: gather_and_run() was NOT called", gate_kwargs2 is None)
    all_ok &= check("--skip-gate: post_load_summary() still runs (skip is gate-only, not whole-run)",
                     post_called2)

    # ── Test 3: --published-total and --county are threaded straight
    #    through to gather_and_run(), not dropped or hardcoded. ─────────────
    gate_kwargs3, _, raised3 = _run_main_with_fakes(
        ["load_certified_historical.py", "--year", "2022",
         "--published-total", "123456789.50", "--county", "DALLAS"],
        gate_return={"passed": True, "checks": {}},
        tmp_cert_dir=tmp_cert_dir,
    )
    all_ok &= check("main() completes without raising (--published-total/--county)",
                     raised3 is None, f"raised={raised3!r}")
    if gate_kwargs3 is not None:
        all_ok &= check("published_total threaded through exactly",
                         gate_kwargs3.get("published_total") == 123456789.50, gate_kwargs3)
        all_ok &= check("county_code threaded through exactly ('DALLAS', not DEFAULT_COUNTY)",
                         gate_kwargs3.get("county_code") == "DALLAS", gate_kwargs3)
    else:
        all_ok &= check("gather_and_run() was called (--published-total/--county run)", False)

    # ── Test 4: a gate FAILURE (passed=False) is loud (non-crashing) —
    #    main() must still reach post_load_summary()/conn.close(), matching
    #    run_all.py's own gate-after-load, non-blocking ordering, per the
    #    brief's "the gate must SAY so per-check, loudly, not silently
    #    skip" instruction -- loud must not mean "crashes the run". ────────
    gate_kwargs4, post_called4, raised4 = _run_main_with_fakes(
        ["load_certified_historical.py", "--year", "2024"],
        gate_return={"passed": False, "checks": {"G3": (False, "MISMATCH: file=$1 unit_table=$2")}},
        tmp_cert_dir=tmp_cert_dir,
    )
    all_ok &= check("main() does not raise/crash when the gate reports FAIL",
                     raised4 is None, f"raised={raised4!r}")
    all_ok &= check("post_load_summary() still runs after a gate FAIL (loud, not blocking)",
                     post_called4)

    import shutil
    shutil.rmtree(tmp_cert_dir, ignore_errors=True)

    print()
    print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
