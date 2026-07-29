#!/usr/bin/env python3
"""
test_app_units_logic.py — AC7 fixture tests for the two pieces of app.py's
Migration M2 changes that can't be exercised via a template render (see
verify_property_html_render.py's new "Migration M2: multi-unit panel
real-render check" section for the panel-rendering half of AC7, which IS a
real Jinja render): the prop_id-fallback resolution helper, and the
homestead-signal unit_count gating.

AC8 disclosure: app.py cannot be imported directly in this sandbox
(psycopg2 is not installed and there is no network access to install it —
confirmed earlier this session). So this file does NOT call app.py's real
resolve_prop_id_to_geo_id() function. Instead:
  1. It defines a hand-written Python mirror of that function's logic
     (prop_unit lookup first, parcel fallback second) and tests the
     mirror against injected fixture data.
  2. It greps app.py's actual source to confirm the real function's
     control flow structurally matches the mirror (same lookup order,
     same table names) — a mechanical tie between the mirror and the
     real code, not a substitute for actually running it.
  3. Live-DB verification of the real function (a real multi-unit geo_id,
     searched by a losing prop_id) is listed in this migration's final
     report as an AC-L item for Diego to run, not claimed as done here.

The homestead-gating half is tested the same way: a pure-Python mirror of
the `unit_count = 1 OR unit_count IS NULL` gating condition, plus a grep
confirming that exact SQL fragment exists in both of
loaders/compute_metrics.py's two UPDATE queries.

Run: python3 test_app_units_logic.py
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── 1. Prop_id fallback resolution — pure-Python mirror ─────────────────
def resolve_prop_id_to_geo_id_mirror(prop_id, prop_unit_table, parcel_table):
    """
    Mirrors app.py's resolve_prop_id_to_geo_id(prop_id): look in
    prop_unit_table first (dict of {prop_id: geo_id}), fall back to
    parcel_table (same shape) only if prop_unit has no entry.
    """
    if prop_id in prop_unit_table:
        return prop_unit_table[prop_id]
    if prop_id in parcel_table:
        return parcel_table[prop_id]
    return None


def test_prop_id_fallback_finds_losing_unit_via_prop_unit():
    # The scenario this migration exists to fix: prop_id 900002 is a
    # "losing" unit that never made it into `parcel.prop_id` (only the
    # MIN-representative winner, 900001, is there) -- but IS in prop_unit,
    # since prop_unit holds every unit.
    prop_unit_table = {900001: "G1", 900002: "G1", 900003: "G1"}
    parcel_table = {900001: "G1"}  # only the repaired MIN representative
    geo_id = resolve_prop_id_to_geo_id_mirror(900002, prop_unit_table, parcel_table)
    check("prop_id fallback: losing unit resolves via prop_unit", geo_id == "G1", geo_id)


def test_prop_id_fallback_defensive_parcel_fallback():
    # Defensive path: prop_id not in prop_unit at all (e.g. before any M2
    # loader has run) but IS in parcel -- old behavior preserved.
    prop_unit_table = {}
    parcel_table = {555: "G9"}
    geo_id = resolve_prop_id_to_geo_id_mirror(555, prop_unit_table, parcel_table)
    check("prop_id fallback: defensive parcel-table fallback still works", geo_id == "G9", geo_id)


def test_prop_id_fallback_miss_returns_none():
    geo_id = resolve_prop_id_to_geo_id_mirror(999, {}, {})
    check("prop_id fallback: total miss returns None", geo_id is None, geo_id)


def test_app_py_source_matches_mirror_structure():
    """Mechanical tie: confirm app.py's real resolve_prop_id_to_geo_id()
    queries prop_unit BEFORE parcel (same order this mirror encodes),
    and that both call sites (resolve_exact_parcel + the ~line 3500
    ajax lookup) call the shared helper rather than re-typing the
    fallback inline."""
    app_path = os.path.join(REPO_ROOT, "app.py")
    with open(app_path, encoding="utf-8") as f:
        src = f.read()

    m = re.search(r"def resolve_prop_id_to_geo_id\(prop_id\):(.*?)\ndef ", src, re.DOTALL)
    check("app.py defines resolve_prop_id_to_geo_id()", m is not None)
    if m:
        body = m.group(1)
        prop_unit_pos = body.find("FROM prop_unit")
        parcel_pos = body.find("FROM parcel")
        check("app.py: prop_unit queried before parcel fallback",
              prop_unit_pos != -1 and parcel_pos != -1 and prop_unit_pos < parcel_pos,
              (prop_unit_pos, parcel_pos))

    call_count = len(re.findall(r"resolve_prop_id_to_geo_id\(", src))
    # 1 def + at least 2 call sites (resolve_exact_parcel, ajax lookup)
    check("app.py: shared helper called from at least 2 sites (no re-typed fallback)",
          call_count >= 3, f"found {call_count} occurrences (expect def + >=2 calls)")


# ── 2. Homestead-signal unit_count gating — pure-Python mirror ──────────
def homestead_signal_eligible_mirror(unit_count):
    """Mirrors the `AND (pty.unit_count = 1 OR pty.unit_count IS NULL)`
    gating clause added to both cap_step_up_exposure and cap_expiry_signal
    in loaders/compute_metrics.py."""
    return unit_count == 1 or unit_count is None


def test_homestead_gating_single_unit_eligible():
    check("homestead gating: unit_count=1 is eligible", homestead_signal_eligible_mirror(1) is True)


def test_homestead_gating_legacy_null_eligible():
    check("homestead gating: unit_count=None (legacy, pre-rollup) is eligible",
          homestead_signal_eligible_mirror(None) is True)


def test_homestead_gating_multi_unit_excluded():
    for n in (2, 3, 24):
        check(f"homestead gating: unit_count={n} (multi-unit) is excluded",
              homestead_signal_eligible_mirror(n) is False)


def test_compute_metrics_source_has_gating_clause():
    """Mechanical tie: confirm the exact gating clause landed in BOTH
    UPDATE queries in compute_metrics.py, not just one."""
    cm_path = os.path.join(REPO_ROOT, "loaders", "compute_metrics.py")
    with open(cm_path, encoding="utf-8") as f:
        src = f.read()

    step_up_gate = "pty.unit_count = 1 OR pty.unit_count IS NULL" in src
    expiry_gate = "pty25.unit_count = 1 OR pty25.unit_count IS NULL" in src
    check("compute_metrics.py: cap_step_up_exposure gated by unit_count", step_up_gate)
    check("compute_metrics.py: cap_expiry_signal gated by unit_count", expiry_gate)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL APP-UNITS-LOGIC FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
