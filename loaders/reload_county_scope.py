#!/usr/bin/env python3
"""
loaders/reload_county_scope.py — PARTITION-2-IMPLEMENT, Part 2.

Real, reusable implementation of SPEC_COUNTY_PARTITIONING.md §9.2(c)'s
county-scoped reload procedure: the recurring-refresh replacement for the
full-table shadow-swap pattern, for the SHARED, multi-county aggregate
tables (group_stats, snapshot_breakdown, snapshot_totals,
snapshot_neighborhood_movers, county_benchmark).

── WHY THIS EXISTS (§9.2(c)'s own real gap) ────────────────────────────────
build_shadow()/swap_shadow_in() (loaders/refresh_group_stats.py,
loaders/refresh_snapshot_summary.py — proven twice this session) rebuild an
ENTIRE table, every county's rows, on every refresh. That's fine today
because there's only one county. Once these tables hold multiple counties'
rows (after migrate_county_partitioning.py's mode-1 migration adds
county_code to them), a Dallas-only data change would force a full rebuild
of Travis's and Harris's already-fresh rows too, just to refresh Dallas —
real wasted work, and it needlessly widens every refresh's blast radius (a
bug in one county's computation would risk corrupting rows for counties
that were never touched by that refresh run).

── WHAT THIS IS NOT ────────────────────────────────────────────────────────
This is NOT used by tonight's migration — Travis is the only county with
real data, so the existing full-table shadow-swap in refresh_group_stats.py
/refresh_snapshot_summary.py remains exactly correct and untouched by this
file. This module is the proven, tested mechanism a FUTURE per-county-aware
version of those refresh scripts will call once a second county's data
exists — built and tested now, per the brief's own explicit instruction,
so it's already proven before it's needed under real time pressure.

── WHAT THIS FUNCTION IS (STEP 3 of §9.2(c)'s 5-step procedure) ────────────
§9.2(c) describes 5 steps: (1) compute the county's new rows into staging,
(2) reconcile the staged rows against source-of-truth aggregates for that
county BEFORE touching the live table, (3) promote — DELETE + INSERT in one
transaction, (4) accept whatever real duration that takes under the
existing hold-the-flip banner, (5) write the per-county freshness stamp.

reload_county_scope() below implements STEP 3 ONLY — the promotion
transaction. Steps 1-2 (computing + reconciling a county's staged rows) are
the CALLER's responsibility, using the same reconciliation primitives
migrate_county_partitioning.py already built (reconcile_counts/
reconcile_sums) — reused, not reinvented, for exactly the same reason this
project reuses parcel_filters.py's exclusion fragments instead of
retyping them per call site. Step 5 (the freshness stamp) is naturally
satisfied by whatever `refreshed_at`/`source_import_batch_id` values the
caller's own INSERT statement writes into the new rows — no separate action
needed here.

── THE ATOMICITY GUARANTEE ─────────────────────────────────────────────────
DELETE FROM <table> WHERE county_code = %s;
<caller's INSERT statement, writing ONLY that county's new rows>;
— both inside ONE transaction, explicit try/except/rollback (not left to
the caller's own connection-lifecycle discipline): if the INSERT raises for
ANY reason, this function catches it, calls conn.rollback(), and re-raises
— the DELETE's effect is undone along with it, so the table is NEVER
observed missing that county's rows, and NEVER observed with a mix of
half-deleted/never-inserted rows for that county. This is real Postgres
transactional atomicity, not a home-rolled substitute for it — the code's
only job is to make sure BOTH statements are inside the same transaction
and that any failure triggers an explicit rollback rather than leaving the
connection in a half-committed, ambiguous state.

── SANDBOX-VS-LIVE DISCLOSURE ───────────────────────────────────────────────
This sandbox has no live Postgres connection. The transaction-boundary
CONTROL FLOW (commit only after both statements succeed; rollback + re-raise
on any failure) is proven via a fake-connection fixture harness in
test_reload_county_scope.py — this proves the CODE correctly delineates the
transaction, but the actual atomicity guarantee under real concurrent
traffic is Postgres's own, and needs Diego's real verification once this is
wired into a real per-county refresh script (not yet — see module docstring
above).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Tables this procedure is designed for — the real, shared, multi-county
# aggregate tables named in §9.2(c). Not enforced as a hard allowlist inside
# reload_county_scope() itself (that would make the function needlessly
# rigid for legitimate future callers), but documented here as the real,
# intended scope, and asserted against in this module's own tests.
COUNTY_SCOPED_RELOAD_TABLES = (
    "group_stats",
    "snapshot_breakdown",
    "snapshot_totals",
    "snapshot_neighborhood_movers",
    "county_benchmark",
)


def build_county_scope_insert_sql(table, columns, source):
    """Pure — convenience builder for the common real shape:
    INSERT INTO <table> (<columns>) SELECT <columns> FROM <source>
    where `source` has already computed/staged ONLY the target county's new
    rows (a per-county-scoped aggregation query, or a real staging table a
    future refresh script builds — either way, this function doesn't care
    which, it just needs a FROM-able source). Explicit column list on both
    sides, matching migrate_county_partitioning.py's own
    build_backfill_insert_sql() discipline — never SELECT *."""
    cols_sql = ", ".join(columns)
    return f"INSERT INTO {table} ({cols_sql}) SELECT {cols_sql} FROM {source}"


def reload_county_scope(conn, table, county_code, insert_sql, insert_params=None, verbose=True):
    """
    STEP 3 of §9.2(c) — the real promotion transaction. Real preconditions
    the CALLER is responsible for (steps 1-2, not enforced here): the new
    county's rows have already been computed and reconciled against
    source-of-truth aggregates for that county BEFORE this function is
    ever called — this function does not compute anything or verify
    anything about the data's correctness, it only guarantees the
    delete-then-insert SWAP itself is atomic.

    Parameters:
      table         -- real table name (one of COUNTY_SCOPED_RELOAD_TABLES
                        in practice, though not hard-enforced — see module
                        docstring).
      county_code   -- the real county_code value being reloaded (e.g.
                        'DALLAS'). Every row with this exact value is
                        deleted, then replaced by whatever insert_sql
                        writes.
      insert_sql    -- a real, caller-provided INSERT statement (typically
                        built via build_county_scope_insert_sql() above)
                        that writes ONLY this county's new rows. THIS
                        FUNCTION DOES NOT VALIDATE that insert_sql is
                        actually scoped to `county_code` — that's the
                        caller's real responsibility (same as how
                        migrate_county_partitioning.py's callers are
                        responsible for passing the right table name); a
                        caller-supplied INSERT that writes some OTHER
                        county's rows here would be a real bug in the
                        CALLER, not something this function can detect
                        from the SQL text alone.
      insert_params -- params dict/tuple for insert_sql, if any.

    Returns a real, structured result dict: {"table", "county_code",
    "n_deleted", "n_inserted"}.

    Raises whatever the underlying INSERT/DELETE raised, AFTER rolling
    back — callers should let this propagate (same convention as every
    other real write path in this project); it is not swallowed here.
    """

    def _log(msg):
        if verbose:
            print(msg)

    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE county_code = %s", (county_code,))
            n_deleted = cur.rowcount
            cur.execute(insert_sql, insert_params or {})
            n_inserted = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        _log(f"  [{table}] county_code={county_code!r}: RELOAD FAILED — transaction rolled "
             f"back; {table} is left exactly as it was before this call (neither the "
             f"delete nor the insert took effect).")
        raise

    _log(f"  [{table}] county_code={county_code!r}: {n_deleted:,} row(s) deleted, "
         f"{n_inserted:,} row(s) inserted, committed atomically.")
    return {"table": table, "county_code": county_code, "n_deleted": n_deleted, "n_inserted": n_inserted}


if __name__ == "__main__":
    print(__doc__)
    print(f"Real, intended-scope tables for this procedure: {', '.join(COUNTY_SCOPED_RELOAD_TABLES)}")
    print("This module has no standalone CLI mode of its own — it's a real, tested library "
          "function (reload_county_scope()) for a future per-county-aware refresh_group_stats.py "
          "/ refresh_snapshot_summary.py to call, not a script Diego runs directly today. "
          "See test_reload_county_scope.py for the fixture proof of its transaction boundary.")
