#!/usr/bin/env python3
"""
loaders/explain_compute_metrics_passes.py — PX-20260901-01 HOTFIX Task 1,
live proof script for Diego.

Same pattern as loaders/explain_snapshot_summary_county_derivation.py:
read-only, zero writes, zero row-returning execution of the real
aggregation bodies. Renders each of compute_parcel_metrics()'s statements
(the main INSERT, Pass 2, Pass 3's two UPDATEs, and Pass 4's rewritten
temp-table + UPDATE pair) via AST extraction directly from
loaders/compute_metrics.py's real source -- not retyped -- so this script
can never silently drift from the shipped SQL (same convention this
codebase already uses in loaders/test_param_sql_placeholder_safety.py and
loaders/explain_snapshot_summary_county_derivation.py). Bound to
county_code='DALLAS' by default (the county whose Pass 4 run was cancelled
after 13h07m on 2026-09-01), runs `EXPLAIN` (COSTS OFF, no ANALYZE) inside a
transaction that is ALWAYS rolled back, never committed.

Why this exists (context for whoever runs this): Pass 4
(cumulative_value_growth_pct) ran active, no wait event, for 13h07m before
being cancelled. The PX-20260901-01 incident report's source-level diagnosis
(confirmed against `git show 0fcddcd:loaders/compute_metrics.py`, the
pre-PX-20260831-02-Task-5 version) is that Pass 4's inner earliest-year
aggregation had no `county_code = %s` filter in its own WHERE clause, so it
aggregated MIN(tax_year) across BOTH counties' complete parcel_tax_year
history (~6.4M rows) on every run, regardless of which single county was
being computed. The fix (already applied to compute_metrics.py) materializes
that aggregation into a temp table SCOPED to county_code = %s inside its own
WHERE, before GROUP BY. This script is how Diego confirms that diagnosis and
that fix against the REAL planner and REAL production statistics, before
trusting either -- the source-level read alone is disclosed everywhere as
"reasoned, not verified" until this script's output says otherwise.

What this proves, and what it does NOT prove:
  - PROVES: the real plan shape for each statement against real production
    statistics/indexes -- in particular, whether Pass 4's earliest-year
    aggregation plans as a DALLAS-scoped index/bitmap scan (the fix working
    as intended) or still touches rows outside county_code='DALLAS' (the fix
    not landing, or a different bottleneck entirely). Also surfaces the
    INSERT's and Pass 2/3's plans, in case PM wants a baseline for "was
    anything else here already slow and just never singled out."
  - DOES NOT PROVE: that a real Pass 4 run completes in a reasonable time --
    EXPLAIN alone gives the planner's chosen strategy and its ESTIMATED cost/
    row counts, not a measured wall-clock duration. If Diego wants that too,
    EXPLAIN ANALYZE would be needed instead -- deliberately NOT what this
    script runs, because that would require letting the real aggregation
    execute to completion, exactly the risk this whole incident is about.
  - Pass 4's temp table (_pass4_earliest_year) is created here as an EMPTY
    shell (same columns as the real one, zero rows, no ANALYZE) rather than
    actually populated -- see _create_empty_pass4_shell()'s docstring below
    for exactly why, and the caveat that follows from it: the join-side
    EXPLAIN for Pass 4b's UPDATE will show planner ESTIMATES that assume an
    empty/near-empty table, not real per-county cardinality. Pass 4a's own
    EXPLAIN -- run directly as a SELECT, never materialized -- is the one
    that matters most: it is what shows whether the fix's
    `WHERE county_code = %s` actually lets the planner restrict the scan to
    one county, which is the entire point of this diagnosis.

Usage (per this repo's standing convention -- always /usr/bin/python3, run
from the repo root, DATABASE_URL already exported to point at the intended
target):

    /usr/bin/python3 loaders/explain_compute_metrics_passes.py
    /usr/bin/python3 loaders/explain_compute_metrics_passes.py --county DALLAS
"""
import argparse
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_COMPUTE_METRICS_PATH = os.path.join(REPO_ROOT, "loaders", "compute_metrics.py")


def _install_fake_psycopg2():
    """compute_metrics.py does `import psycopg2.extras` at module level;
    psycopg2 isn't installed in every environment this script might be
    sanity-checked from (it wasn't in the sandbox this hotfix was built in).
    Same stub pattern as test_param_sql_placeholder_safety.py -- only used
    for the AST-extraction import below, never for the real EXPLAIN
    execution, which always uses the real loaders.db.get_conn()/psycopg2 on
    whatever machine actually runs this against a live target."""
    import types
    try:
        import psycopg2 as _real_pg, psycopg2.extras as _real_pg_extras  # noqa: F401
        return  # real driver importable -- never shadow it (PX-20260901-01 live finding)
    except ImportError:
        pass
    if "psycopg2" in sys.modules:
        return
    fake = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")

    class _FakeRealDictCursor:
        pass

    fake_extras.RealDictCursor = _FakeRealDictCursor
    fake.extras = fake_extras

    class _FakeError(Exception):
        pass

    fake.Error = _FakeError
    sys.modules["psycopg2"] = fake
    sys.modules["psycopg2.extras"] = fake_extras


def _find_execute_calls(tree, src):
    """Every `cur.execute(...)` call in the module, as
    (sql_node, params_node_or_None, rendered_source_segment)."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args):
            sql_node = node.args[0]
            params_node = node.args[1] if len(node.args) > 1 else None
            segment = ast.get_source_segment(src, sql_node) or ""
            out.append((sql_node, params_node, segment))
    return out


def _render_joinedstr(node, namespace):
    return eval(compile(ast.Expression(body=node), "<explain>", "eval"), {}, namespace)


def _extract_statements():
    """AST-extracts the five real statement bodies from the actual current
    loaders/compute_metrics.py source: the main INSERT (Pass 1, a JoinedStr
    needing COMPUTATION_VERSION), Pass 2's UPDATE (a JoinedStr needing
    jump_threshold), Pass 3's two UPDATEs (plain Constants), and Pass 4's
    CREATE TEMP TABLE (the fixed, county-scoped aggregation -- a plain
    Constant) and its UPDATE (also a plain Constant). Returns a dict keyed
    by label -> raw SQL text (f-strings already rendered where needed)."""
    _install_fake_psycopg2()
    import loaders.compute_metrics as cm

    src = open(_COMPUTE_METRICS_PATH, encoding="utf-8").read()
    tree = ast.parse(src, filename=_COMPUTE_METRICS_PATH)
    calls = _find_execute_calls(tree, src)

    out = {}
    for sql_node, params_node, segment in calls:
        if isinstance(sql_node, ast.JoinedStr):
            if "INSERT INTO parcel_metrics" in segment:
                out["main_insert"] = _render_joinedstr(
                    sql_node, {"COMPUTATION_VERSION": cm.COMPUTATION_VERSION}
                )
            elif "risk_large_value_jump" in segment and "SET" in segment:
                # jump_threshold is a plain numeric interpolation -- any
                # value renders a structurally identical statement (it's a
                # literal >{n} in the WHERE, not a placeholder), so 0 is
                # fine for rendering purposes; the real run binds the real
                # per-county threshold via _large_jump_threshold_for_county().
                out["pass2"] = _render_joinedstr(sql_node, {"jump_threshold": 0})
        elif isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
            text = sql_node.value
            if "cap_step_up_exposure = TRUE" in text:
                out["pass3_step_up"] = text
            elif "cap_expiry_signal = TRUE" in text:
                out["pass3_expiry"] = text
            elif "CREATE TEMP TABLE _pass4_earliest_year" in text:
                out["pass4a_create"] = text
            elif "cumulative_value_growth_pct = sub.cum_pct" in text:
                out["pass4b_update"] = text

    missing = [k for k in (
        "main_insert", "pass2", "pass3_step_up", "pass3_expiry",
        "pass4a_create", "pass4b_update",
    ) if k not in out]
    if missing:
        raise RuntimeError(
            f"FATAL: could not AST-locate statement(s) {missing!r} in "
            f"{_COMPUTE_METRICS_PATH} -- compute_metrics.py's source has "
            f"moved/changed shape since this script was written and needs "
            f"a matching update before it can be trusted."
        )
    return out


def _pass4a_select_only(create_table_sql):
    """Pass 4a's CREATE TEMP TABLE statement is `CREATE TEMP TABLE
    _pass4_earliest_year ON COMMIT DROP AS <SELECT ...>` -- Postgres CAN
    EXPLAIN a CREATE TABLE AS SELECT directly (this is one of the statement
    types EXPLAIN supports without ever executing the underlying plan), but
    running it as a bare SELECT instead is simpler to read and identical in
    plan shape for the aggregation itself, so this strips the `CREATE TEMP
    TABLE ... AS` prefix and EXPLAINs the SELECT alone."""
    marker = "AS\n"
    idx = create_table_sql.index(marker)
    return create_table_sql[idx + len(marker):]


def _plan_text(cur, sql, params=None):
    """Runs EXPLAIN (COSTS OFF) on `sql` inside the caller's already-open
    transaction and returns the plan as one newline-joined string. COSTS OFF
    keeps output deterministic/diffable across runs (no cost estimates that
    shift with autovacuum/ANALYZE timing) -- same convention as
    explain_snapshot_summary_county_derivation.py."""
    if params is not None:
        cur.execute(f"EXPLAIN (COSTS OFF) {sql}", params)
    else:
        cur.execute(f"EXPLAIN (COSTS OFF) {sql}")
    return "\n".join(row[0] for row in cur.fetchall())


def _create_empty_pass4_shell(cur):
    """Creates _pass4_earliest_year as an EMPTY shell (zero rows, no
    ANALYZE) so Pass 4b's UPDATE can be EXPLAINed at all -- Postgres can't
    plan a join against a table that doesn't exist. This is cheap, harmless
    DDL (a static column list, no SELECT body), not an execution of the real
    aggregation -- the real aggregation (Pass 4a) is EXPLAINed separately,
    on its own, and NEVER run to completion or materialized anywhere. Rolled
    back like everything else in this script's transaction.

    Caveat, disclosed not hidden: because the shell is empty, Pass 4b's
    EXPLAIN output will show planner ESTIMATES built on zero real rows (e.g.
    "rows=1" placeholders) -- not the real per-county cardinality Pass 4a's
    own EXPLAIN shows against the real, populated table. Read Pass 4b's plan
    for STRUCTURE (join order, index usage on the parcel_tax_year side) and
    Pass 4a's plan for the real row-estimate story."""
    cur.execute("""
        CREATE TEMP TABLE _pass4_earliest_year (
            geo_id VARCHAR(20),
            earliest_year SMALLINT
        )
    """)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", default=None,
                     help="county_code to bind every statement to (default: "
                          "loaders.compute_metrics.DEFAULT_COUNTY). PX-20260901-01's incident "
                          "was against DALLAS -- pass --county DALLAS to see the exact plan the "
                          "cancelled run would have used.")
    args = ap.parse_args()

    statements = _extract_statements()

    import loaders.compute_metrics as cm
    county = args.county or cm.DEFAULT_COUNTY
    jump_threshold = cm._large_jump_threshold_for_county(county)

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}\n"
          f"county_code bound to every statement: {county!r}\n"
          f"Every statement below is EXPLAIN only (no ANALYZE, no execution of the real "
          f"aggregation bodies) inside a transaction that is always ROLLBACK'd -- zero writes, "
          f"zero row-returning execution.\n")

    ordered = [
        ("Pass 1: main INSERT", statements["main_insert"], (county,)),
        (f"Pass 2: risk_large_value_jump (real threshold for {county!r} is "
         f"{jump_threshold}, rendered here as a placeholder -- see code comment)",
         statements["pass2"], (county,)),
        ("Pass 3a: cap_step_up_exposure", statements["pass3_step_up"], (county, county)),
        ("Pass 3b: cap_expiry_signal", statements["pass3_expiry"], (county, county)),
        ("Pass 4a: earliest-year aggregation (THE FIX -- does this scan stay "
         f"inside county_code = {county!r}?)",
         _pass4a_select_only(statements["pass4a_create"]), (county,)),
    ]

    try:
        with conn.cursor() as cur:
            for label, sql, params in ordered:
                plan = _plan_text(cur, sql, params)
                print(f"── {label} ──")
                print(plan)
                print()

            # Pass 4b needs the shell table to exist first -- see
            # _create_empty_pass4_shell()'s docstring for why this is safe
            # and what it does NOT tell you.
            _create_empty_pass4_shell(cur)
            plan = _plan_text(cur, statements["pass4b_update"], (county, county, county))
            print("── Pass 4b: UPDATE parcel_metrics FROM _pass4_earliest_year join "
                  "(CAVEAT: shell table is empty -- see module docstring) ──")
            print(plan)
            print()
    finally:
        conn.rollback()  # belt-and-suspenders: EXPLAIN alone never executes, but never COMMIT regardless.

    print("Read Pass 4a's plan first: if its scan is restricted to "
          f"county_code = {county!r} (an index/bitmap scan bounded by that predicate, not a "
          "sequential scan of the whole parcel_tax_year table), the fix is doing what it's "
          "supposed to. Compare against Pass 1/2/3's plans as a baseline for whether anything "
          "else here is also worth a second look.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
