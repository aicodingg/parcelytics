#!/usr/bin/env python3
"""
migrate_county_partitioning.py — PARTITION-2-IMPLEMENT, Part 1.

Real, reusable per-table migration script implementing
SPEC_COUNTY_PARTITIONING.md §5's exact shadow-table-then-atomic-swap
procedure, generalized to real, POPULATED production tables, for every
table named in that spec's §4.3 (plus tax_billing_quarantine, folded in
here per finding 9.10 / this brief's Part 5, and county_benchmark/
ingest_audit/load_batch, which get the lighter treatments §4.2/§4.3's own
text already specifies for them — see "THREE MIGRATION MODES" below).

DOES NOT RUN AGAINST PRODUCTION BY ITSELF. This script only executes when
Diego runs it, against whichever DB loaders/db.py's get_conn() is
configured to point at — same discipline as every other production script
this project has built (vault_backfill.py, refresh_group_stats.py,
refresh_snapshot_summary.py, quarantine_contamination.py). Per this
project's standing rules: confirm inet_server_addr() before trusting a
production run (printed automatically below), and take a real backup
before any schema change, every time, no exceptions — this script does NOT
take that backup for you.

── THREE MIGRATION MODES (per SPEC_COUNTY_PARTITIONING.md §4.3 / §9.4) ────
1. composite_pk (most tables): full shadow-table-then-atomic-swap.
   county_code becomes a NEW, LEADING column of the table's PRIMARY KEY.
   Used for every geo_id/prop_id-keyed core table, the four Tier 1/3
   precomputed tables, county_tax_rate (ruled in §9.3 — county_code joins
   as (county_code, entity_code, tax_year), NOT dropping tax_year), and
   tax_billing_quarantine (folded into this migration's real scope per
   finding 9.10).
2. default_only (county_benchmark): ALREADY has county_code as its
   leading PK column, with a DEFAULT 'TRAVIS' (the real prior art
   SPEC_COUNTY_PARTITIONING.md §2 found). The only real gap left is that
   DEFAULT itself — finding 9.4's point: a surviving default is a
   contamination vector, not cleanup. No shadow-swap, no backfill, no PK
   change — just a real, asserted-safe DROP DEFAULT.
3. add_column (ingest_audit, load_batch): §4.3's own explicit "plain
   (non-PK) column" treatment for these two append-only, surrogate-keyed
   audit tables. ADD COLUMN + explicit backfill UPDATE + SET NOT NULL — no
   PK change, no shadow-swap, and (unlike mode 1) no DEFAULT is ever added
   in the first place, so there's nothing to drop afterward.

UPDATE (DALLAS-GATE-2, Aug 15 2026, later same day): parcel_2026_
preliminary_snapshot is now IN this script's scope, reversing the
retirement recommendation below — recorded here rather than deleted, so
the real reasoning shift stays visible. §1.1's "retire it" recommendation
predated M4-2026-PRELIM-SNAPSHOT Part 3 (property_detail()'s real,
shipped 2026 Preliminary→Certified comparison card, both Homeowner and
Investor modes) and g6_reconciliation.py's run_2026_prelim_vs_cert() —
both real, currently-live consumers confirmed via a direct repo grep
before making this call, not assumed. A table that's genuinely dead is
safe to retire instead of migrate; a table a live UI feature reads from
every property-detail page load is not — retiring it now would silently
break that feature, a product-scope decision this brief isn't the place
to make unilaterally. The table's own real shape (schema.sql's
"NOT-kept-in-sync, populated once" framing) is otherwise unchanged and
still accurate — it's Mode 1 like every other single-column-PK table
below, just written to less often.

Prior text (now superseded, kept for the historical record): "Explicitly
NOT migrated by this script (see SPEC_COUNTY_PARTITIONING.md §1.1's own
inventory): parcel_2026_preliminary_snapshot — that table's own schema.sql
comment already documents it as a one-time, NOT-kept-in-sync snapshot
never meant to be a durable structure; §1.1 recommends retiring it, not
migrating it. Not in TABLE_SPECS/MIGRATION_ORDER below; out of scope per
this brief's own 'no changes to anything not named in §4.3's table list'
instruction."

── REAL PER-TABLE PROCEDURE (mode 1), matching §5 exactly ─────────────────
  1. CREATE <table>_new — every real LIVE column (via information_schema,
     NOT schema.sql — see "WHY INFORMATION_SCHEMA, NOT schema.sql" below)
     reproduced with its real type/nullability/default, PLUS a new
     county_code VARCHAR(20) NOT NULL DEFAULT '<county value>' column, PLUS
     the target composite PRIMARY KEY.
  2. Explicit backfill: INSERT INTO <table>_new (county_code, <every real
     column>) SELECT '<county value>', <every real column> FROM <table> —
     a real, auditable statement (both sides list every column by name,
     never SELECT *), not something relying on the new column's DEFAULT to
     do the work quietly (finding 9.4).
  3. Reconciliation, BEFORE any swap — exact row-count match, exact sum
     match on every real numeric column, plus a real 20-row (default;
     --sample-size) spot check comparing every column, old vs. new, for
     the same real entities. If ANY check fails, this script STOPS: no
     swap, <table>_new is left in place for inspection, live <table> is
     completely untouched.
  4. Atomic swap, one transaction: ALTER TABLE <table> RENAME TO
     <table>_old_pre_partition; ALTER TABLE <table>_new RENAME TO <table>
     — the exact pattern swap_shadow_in() already proved twice this
     session (group_stats, snapshot_*), generalized to a real populated
     table. FK handling: see "FK HANDLING" below.
  5. Only after the swap is confirmed correct: DROP DEFAULT on
     county_code (finding 9.4 — a required step, not optional cleanup).
  6. <table>_old_pre_partition is RETAINED — this script never drops it.
     That's a real, later, deliberate, human decision (matching this
     project's established "nothing gets dropped same-day after a
     real-data migration" pattern — see VAULT-COPY-FIX-1, AGGPRECOMP-1/2).

── WHY INFORMATION_SCHEMA, NOT schema.sql ──────────────────────────────────
schema.sql is documented, in its own comments, to have drifted from live
reality on at least two `parcel` columns (classi_cd, year_built — both
referenced by real indexes/ALTERs later in that file, but never introduced
by any CREATE TABLE or ADD COLUMN statement inside it — they were added
directly to production out-of-band at some point). Building <table>_new's
column list by reading the LIVE table's real information_schema.columns is
the only way this script can guarantee it captures every real column, not
just the ones schema.sql still happens to describe. Every DDL-generation
function below is exercised in test_migrate_county_partitioning.py against
synthetic information_schema-shaped rows — no live DB needed to prove the
DDL-building LOGIC is right (see that file for the honest sandbox-vs-live
split: this proves the SQL text is correctly built, not that it executes
correctly against real Postgres).

── FK HANDLING ─────────────────────────────────────────────────────────────
parcel_metrics.geo_id REFERENCES parcel(geo_id) is the one real FK
constraint among every table in TABLE_SPECS (confirmed via
SPEC_COUNTY_PARTITIONING.md §1.1's inventory). Postgres tracks FK
constraints by object OID, not by name — renaming `parcel` does NOT retarget
parcel_metrics' existing FK to whatever is subsequently named `parcel`; it
keeps pointing at the SAME physical table, now named
`parcel_old_pre_partition`. Left alone, this creates a real, live risk
during the (possibly long — this migration is designed to run table-by-table,
per --only) window between parcel's migration and parcel_metrics' own: any
NEW parcel row written to the newly-live `parcel` table during that window
is NOT a valid FK target yet for parcel_metrics, because the stale
constraint still validates against the frozen `parcel_old_pre_partition`
snapshot — a real write failure risk for anything inserting parcel_metrics
rows (compute_metrics.py) for a parcel added after parcel's own migration
but before parcel_metrics'.
Fix (spec's own explicit instruction, §5 step 4): parcel's own migration
step DROPS the stale FK on parcel_metrics (found dynamically via
information_schema, never hardcoded — see _find_fk_constraint_name()'s own
docstring for why) as part of its swap. parcel_metrics' OWN migration step
(later, per TABLE_SPECS' dependency order) ADDS BACK a real composite FK —
(county_code, geo_id) REFERENCES parcel (county_code, geo_id) — once
parcel_metrics itself has the composite key that FK needs to reference, and
once `parcel` (already migrated, earlier in dependency order) genuinely has
a (county_code, geo_id) key for it to reference.

── SANDBOX-VS-LIVE DISCLOSURE (same pattern as every loader this project has
built) ─────────────────────────────────────────────────────────────────────
This sandbox has no live Postgres connection. Every pure DDL/reconciliation
-LOGIC function below (build_create_new_table_sql, build_backfill_insert_sql,
reconcile_counts, reconcile_sums, reconcile_spot_check, and the orchestration
control-flow in migrate_table()/migrate_add_column_table()/
migrate_default_only_table()) is proven correct via fixture tests against a
fake connection/cursor (test_migrate_county_partitioning.py) — this proves
the SQL text is right and the control flow genuinely refuses to swap on a
failed reconciliation, but does NOT prove the actual SQL executes correctly
against real Postgres, or that a real multi-hundred-thousand-row backfill
completes in the time this script's own comments assume. Diego's real run,
table by table, with real backups first, is the only thing that proves that.

Usage:
    python3 migrate_county_partitioning.py --dry-run
    python3 migrate_county_partitioning.py --dry-run --only parcel,parcel_metrics
    python3 migrate_county_partitioning.py --only parcel
    python3 migrate_county_partitioning.py --only parcel --report-out ledger_parcel.json
    python3 migrate_county_partitioning.py                     # every table, real dependency order
"""
import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_COUNTY = "TRAVIS"

# ── Table specs, mode 1 (composite_pk), in real dependency order ───────────
# Each spec:
#   name    -- live table name
#   old_pk  -- current real primary key columns (spot-check join key)
#   new_pk  -- target composite primary key, county_code LEADING per finding
#              9.2(a) -- no exceptions, every table below follows this.
#   fk_drop -- child tables whose FK constraint referencing THIS table must
#              be dropped as part of THIS table's own swap (see "FK
#              HANDLING" above)
#   fk_add  -- composite FK(s) THIS table itself should gain, added right
#              after THIS table's own swap (THIS table is the CHILD here)
TABLE_SPECS = [
    {"name": "parcel", "old_pk": ["geo_id"], "new_pk": ["county_code", "geo_id"],
     "fk_drop": ["parcel_metrics"], "fk_add": []},
    {"name": "parcel_tax_year", "old_pk": ["geo_id", "tax_year"],
     "new_pk": ["county_code", "geo_id", "tax_year"], "fk_drop": [], "fk_add": []},
    {"name": "tax_billing", "old_pk": ["geo_id", "tax_year"],
     "new_pk": ["county_code", "geo_id", "tax_year"], "fk_drop": [], "fk_add": []},
    {"name": "tax_billing_entity", "old_pk": ["geo_id", "tax_year", "entity_code"],
     "new_pk": ["county_code", "geo_id", "tax_year", "entity_code"], "fk_drop": [], "fk_add": []},
    {"name": "tax_delinquent", "old_pk": ["geo_id"], "new_pk": ["county_code", "geo_id"],
     "fk_drop": [], "fk_add": []},
    # Folded in per finding 9.10 / this brief's Part 5 — same real key
    # change as its sibling geo_id-keyed billing tables.
    {"name": "tax_billing_quarantine", "old_pk": ["geo_id", "tax_year"],
     "new_pk": ["county_code", "geo_id", "tax_year"], "fk_drop": [], "fk_add": []},
    {"name": "prop_unit", "old_pk": ["prop_id"], "new_pk": ["county_code", "prop_id"],
     "fk_drop": [], "fk_add": []},
    {"name": "prop_unit_tax_year", "old_pk": ["prop_id", "tax_year"],
     "new_pk": ["county_code", "prop_id", "tax_year"], "fk_drop": [], "fk_add": []},
    {"name": "parcel_metrics", "old_pk": ["geo_id", "tax_year"],
     "new_pk": ["county_code", "geo_id", "tax_year"], "fk_drop": [],
     "fk_add": [{"constraint": "parcel_metrics_county_geo_fkey",
                 "columns": ["county_code", "geo_id"],
                 "ref_table": "parcel", "ref_columns": ["county_code", "geo_id"]}]},
    # Ruled in SPEC_COUNTY_PARTITIONING.md §9.3: (county_code, entity_code,
    # tax_year) — county_code joins as the new LEADING column; entity_code
    # and tax_year are retained exactly as the original key already had
    # them (nothing about tax_year's presence in the key was ever in
    # question — see §9.3's own clarification).
    {"name": "county_tax_rate", "old_pk": ["entity_code", "tax_year"],
     "new_pk": ["county_code", "entity_code", "tax_year"], "fk_drop": [], "fk_add": []},
    {"name": "group_stats", "old_pk": ["neighborhood_cd_key", "state_cd1_class", "classi_cd_key", "tax_year"],
     "new_pk": ["county_code", "neighborhood_cd_key", "state_cd1_class", "classi_cd_key", "tax_year"],
     "fk_drop": [], "fk_add": []},
    {"name": "snapshot_breakdown", "old_pk": ["view", "ptype"],
     "new_pk": ["county_code", "view", "ptype"], "fk_drop": [], "fk_add": []},
    {"name": "snapshot_totals", "old_pk": ["view"],
     "new_pk": ["county_code", "view"], "fk_drop": [], "fk_add": []},
    {"name": "snapshot_neighborhood_movers", "old_pk": ["view", "neighborhood_cd"],
     "new_pk": ["county_code", "view", "neighborhood_cd"], "fk_drop": [], "fk_add": []},
    # DALLAS-GATE-2: added, reversing the original "retire, don't migrate"
    # recommendation -- see this file's own module docstring UPDATE note
    # above for the real reasoning (a live UI feature and a live
    # reconciliation tool both genuinely read this table today). Plain
    # single-column PK, no FKs reference it (confirmed via a direct
    # schema.sql grep before adding this entry), so it's an ordinary
    # Mode 1 table -- no special handling needed beyond what every other
    # entry in this list already gets.
    {"name": "parcel_2026_preliminary_snapshot", "old_pk": ["geo_id"],
     "new_pk": ["county_code", "geo_id"], "fk_drop": [], "fk_add": []},
]
SPEC_BY_NAME = {s["name"]: s for s in TABLE_SPECS}

# Mode 2: already has county_code as the leading PK column with a DEFAULT —
# only the default itself needs to go (finding 9.4).
DEFAULT_ONLY_TABLES = ["county_benchmark"]

# Mode 3: plain (non-PK) column add — §4.3's own explicit treatment for
# these two append-only, surrogate-keyed audit tables.
ADD_COLUMN_TABLES = ["ingest_audit", "load_batch"]

MIGRATION_ORDER = [s["name"] for s in TABLE_SPECS] + DEFAULT_ONLY_TABLES + ADD_COLUMN_TABLES


# ═══════════════════════════════════════════════════════════════════════════
# PURE FUNCTIONS — no DB access, fully unit-testable (see
# test_migrate_county_partitioning.py)
# ═══════════════════════════════════════════════════════════════════════════

_TYPE_MAP = {
    "int8": "BIGINT", "int4": "INTEGER", "int2": "SMALLINT",
    "bool": "BOOLEAN", "text": "TEXT", "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ", "date": "DATE", "float8": "DOUBLE PRECISION",
    "float4": "REAL",
}


def _column_ddl_type(col):
    """Pure — reconstructs a real SQL type string from one
    information_schema.columns row (column_name, data_type, udt_name,
    character_maximum_length, numeric_precision, numeric_scale,
    is_nullable, column_default). Covers every real column type present
    across every real TABLE_SPECS table today (checked against schema.sql's
    own DDL for each); raises on an array type since none of TABLE_SPECS'
    tables have one today and silently mis-typing a future one would be
    worse than failing loudly."""
    dt = col["data_type"]
    udt = col["udt_name"]
    if dt == "character varying":
        n = col.get("character_maximum_length")
        return f"VARCHAR({n})" if n else "VARCHAR"
    if dt == "character":
        n = col.get("character_maximum_length")
        return f"CHAR({n})" if n else "CHAR"
    if dt == "numeric":
        p, s = col.get("numeric_precision"), col.get("numeric_scale")
        if p is not None and s is not None:
            return f"NUMERIC({p},{s})"
        return "NUMERIC"
    if dt == "ARRAY":
        raise NotImplementedError(
            f"column {col.get('column_name')!r} is an ARRAY type (udt_name={udt!r}) — "
            f"no TABLE_SPECS table has one today; this builder doesn't handle it. "
            f"Extend _column_ddl_type() deliberately if a future table needs this, "
            f"rather than guessing at the element type."
        )
    if udt in _TYPE_MAP:
        return _TYPE_MAP[udt]
    # Fallback: bare uppercase udt_name covers everything else real tables
    # here use (e.g. 'numeric' already handled above). Anything reaching
    # here is genuinely unexpected — surfaced via the returned string being
    # visibly wrong in the generated DDL rather than a silent guess, and
    # every real table's column set is exercised by this project's own
    # fixture tests before this ever runs live.
    return udt.upper()


def build_create_new_table_sql(table, columns, new_pk, county_value=DEFAULT_COUNTY):
    """Pure — builds `CREATE TABLE <table>_new (...)`: every real live
    column (as returned by _get_live_columns()) reproduced with its real
    type + nullability + (non-serial) default, PLUS a new
    county_code VARCHAR(20) NOT NULL DEFAULT '<county_value>' column, PLUS
    the target composite PRIMARY KEY. The DEFAULT exists ONLY so the
    explicit backfill INSERT (build_backfill_insert_sql()) has something
    to fall back on if it's ever run with the column omitted by mistake —
    it is REQUIRED to be dropped once the swap is confirmed correct
    (finding 9.4; see migrate_table()'s own final step). Raises if any
    column's default references a sequence (nextval(...)) — copying that
    verbatim onto a new table would silently reference the OLD table's
    sequence, which is wrong; no TABLE_SPECS table has a SERIAL/BIGSERIAL
    column today (confirmed: only ingest_audit.id/load_batch.batch_id are,
    and both use the separate add_column mode, which never calls this
    function) — this guard exists so a future table added here fails
    loudly instead of getting subtly-wrong DDL."""
    col_lines = []
    for col in columns:
        default = col.get("column_default")
        if default and "nextval(" in default:
            raise NotImplementedError(
                f"{table}.{col['column_name']} has a sequence-owned default ({default!r}) — "
                f"build_create_new_table_sql() does not handle SERIAL/BIGSERIAL columns "
                f"(see this function's own docstring). Extend deliberately if needed."
            )
        ddl_type = _column_ddl_type(col)
        nullability = "" if col["is_nullable"] == "YES" else " NOT NULL"
        default_sql = f" DEFAULT {default}" if default else ""
        col_lines.append(f"    {col['column_name']}  {ddl_type}{nullability}{default_sql}")
    col_lines.append(f"    county_code  VARCHAR(20) NOT NULL DEFAULT '{county_value}'")
    pk_sql = ", ".join(new_pk)
    body = ",\n".join(col_lines)
    return f"CREATE TABLE {table}_new (\n{body},\n    PRIMARY KEY ({pk_sql})\n)"


def build_backfill_insert_sql(table, columns):
    """Pure — builds the explicit, auditable
    INSERT INTO <table>_new (county_code, col1, col2, ...)
    SELECT %(county_value)s, col1, col2, ... FROM <table>
    statement. Explicit column list on BOTH sides (never SELECT *) so this
    is immune to column-ordering differences between the old and new
    tables, and — per finding 9.4's own instruction — a real, auditable
    statement rather than something relying on the new column's DEFAULT to
    do the backfill work quietly."""
    col_names = [c["column_name"] for c in columns]
    cols_sql = ", ".join(col_names)
    return (
        f"INSERT INTO {table}_new (county_code, {cols_sql}) "
        f"SELECT %(county_value)s, {cols_sql} FROM {table}"
    )


def reconcile_counts(old_count, new_count):
    """Pure. Exact match required."""
    ok = old_count == new_count
    return ok, {
        "check": "row_count", "old_count": old_count, "new_count": new_count,
        "reason": "exact match" if ok else f"MISMATCH: old={old_count:,} new={new_count:,}",
    }


def reconcile_sums(old_sums, new_sums):
    """Pure. old_sums/new_sums: {column_name: numeric-or-None}. Exact
    equality required — unlike AGGPRECOMP-2-FIX-2's cross-table consistency
    assertion (which tolerates independent-rounding drift between two
    SEPARATELY computed aggregates), this is a literal row-for-row copy of
    the SAME data — any difference at all, however small, is real evidence
    of a copy bug, not benign drift, so no tolerance is applied here."""
    all_cols = sorted(set(old_sums) | set(new_sums))
    mismatches = [
        {"column": c, "old_sum": old_sums.get(c), "new_sum": new_sums.get(c)}
        for c in all_cols if old_sums.get(c) != new_sums.get(c)
    ]
    ok = len(mismatches) == 0
    return ok, {
        "check": "dollar_sums", "columns_checked": all_cols, "mismatches": mismatches,
        "reason": ("all numeric column sums match exactly" if ok else
                   f"{len(mismatches)} column sum mismatch(es) — see 'mismatches'"),
    }


def reconcile_spot_check(old_rows, new_rows, compare_columns, pk_columns=None):
    """Pure. old_rows/new_rows: lists of dicts, already matched by the SAME
    real entity (same index = same row identity — see fetch_spot_check_rows()
    for how that pairing is built). compare_columns: real old-table column
    names to compare (county_code is deliberately excluded — it doesn't
    exist on the old side by construction). pk_columns: just for labeling
    which real entity a mismatch belongs to in the report; doesn't affect
    the comparison itself. A missing new_row (None — the key wasn't found
    in <table>_new at all) is itself a real mismatch, not skipped."""
    pk_columns = pk_columns or []
    mismatches = []
    for i, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
        key = {k: old_row.get(k) for k in pk_columns}
        if new_row is None:
            mismatches.append({"row_index": i, "key": key,
                                "reason": "no matching row found in <table>_new at all"})
            continue
        for col in compare_columns:
            if old_row.get(col) != new_row.get(col):
                mismatches.append({
                    "row_index": i, "key": key, "column": col,
                    "old_value": old_row.get(col), "new_value": new_row.get(col),
                })
    ok = len(mismatches) == 0 and len(old_rows) == len(new_rows)
    return ok, {
        "check": "spot_check_sample", "sample_size": len(old_rows), "mismatches": mismatches,
        "reason": ("all sampled rows match column-for-column" if ok else
                   (f"{len(mismatches)} mismatch(es) in sample" if mismatches else
                    f"sample size mismatch: old={len(old_rows)} new={len(new_rows)}")),
    }


# ═══════════════════════════════════════════════════════════════════════════
# DB-TOUCHING FUNCTIONS — thin wrappers around the pure logic above, plus
# real introspection/orchestration. Not unit-tested directly (no live DB in
# this sandbox); exercised via a fake-connection harness in
# test_migrate_county_partitioning.py to prove CONTROL FLOW only (e.g. "a
# failed reconciliation really does stop before any swap SQL is issued").
# ═══════════════════════════════════════════════════════════════════════════

def _get_live_columns(conn, table):
    """Real column introspection via information_schema — see module
    docstring's "WHY INFORMATION_SCHEMA, NOT schema.sql" for why this,
    not a hardcoded column list, is what CREATE TABLE <table>_new is
    built from."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, udt_name, character_maximum_length,
                   numeric_precision, numeric_scale, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table,))
        colnames = [d[0] for d in cur.description]
        return [dict(zip(colnames, row)) for row in cur.fetchall()]


def _find_fk_constraint_name(conn, child_table, parent_table):
    """Real, dynamic FK-constraint lookup — deliberately NOT a hardcoded
    assumed name (e.g. 'parcel_metrics_geo_id_fkey'). schema.sql is already
    documented (this file's own module docstring) to have drifted from live
    reality on real columns; assuming a constraint name follows the
    "obvious" Postgres auto-naming convention would be the same class of
    mistake. Returns None if no such FK exists (e.g. already dropped by a
    prior partial run — safe to no-op)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.constraint_schema = ccu.constraint_schema
            WHERE tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_name = %s
        """, (child_table, parent_table))
        row = cur.fetchone()
        return row[0] if row else None


def fetch_row_count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]


def fetch_numeric_sums(conn, table, columns):
    """Sums every real numeric-typed column present in `columns` (as
    returned by _get_live_columns() against the OLD table — used for BOTH
    the old and new table's sum fetch, so the exact same real column set is
    compared on both sides)."""
    numeric_cols = [c["column_name"] for c in columns
                    if c["data_type"] in ("bigint", "integer", "smallint", "numeric",
                                           "double precision", "real")]
    if not numeric_cols:
        return {}
    select_sql = ", ".join(f"SUM({c}) AS {c}" for c in numeric_cols)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {select_sql} FROM {table}")
        row = cur.fetchone()
        return dict(zip(numeric_cols, row))


def fetch_spot_check_rows(conn, table, old_pk, columns, sample_size=20):
    """Real random sample by the OLD table's primary key, fetched from BOTH
    <table> and <table>_new using the SAME key values, so old_rows[i]/
    new_rows[i] refer to the identical real entity. ORDER BY RANDOM() LIMIT
    N (not TABLESAMPLE — which samples pages, not rows, and can return zero
    rows on a small table) is the simple, correct choice at this project's
    real per-table scale (largest table per SPEC_COUNTY_PARTITIONING.md
    §1.2: ~517K rows for `parcel` — ORDER BY RANDOM() is a known-bad idea
    at tens of millions of rows, not at this size)."""
    col_names = [c["column_name"] for c in columns]
    cols_sql = ", ".join(col_names)
    pk_sql = ", ".join(old_pk)
    where_sql = " AND ".join(f"{k} = %s" for k in old_pk)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {pk_sql} FROM {table} ORDER BY RANDOM() LIMIT %s", (sample_size,))
        keys = cur.fetchall()
    old_rows, new_rows = [], []
    with conn.cursor() as cur:
        for key in keys:
            cur.execute(f"SELECT {cols_sql} FROM {table} WHERE {where_sql}", key)
            old_rows.append(dict(zip(col_names, cur.fetchone())))
            cur.execute(f"SELECT {cols_sql} FROM {table}_new WHERE {where_sql}", key)
            new_row = cur.fetchone()
            new_rows.append(dict(zip(col_names, new_row)) if new_row else None)
    return old_rows, new_rows


def migrate_table(conn, spec, county_value=DEFAULT_COUNTY, dry_run=False, verbose=True, sample_size=20):
    """Mode 1 (composite_pk) — full per-table migration, §5's exact
    procedure. Returns a real, structured "table report" dict (Part 6 — see
    build_ledger_report())."""
    table = spec["name"]

    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    columns = _get_live_columns(conn, table)
    if not columns:
        raise RuntimeError(f"{table}: no columns found via information_schema — "
                            f"does this table exist on the target DB?")

    old_count = fetch_row_count(conn, table)
    _log(f"  [{table}] live row count: {old_count:,}")

    create_sql = build_create_new_table_sql(table, columns, spec["new_pk"], county_value)
    backfill_sql = build_backfill_insert_sql(table, columns)

    if dry_run:
        _log(f"  [DRY RUN] {table}: would create {table}_new, backfill {old_count:,} row(s) "
             f"tagged county_code='{county_value}', reconcile (counts + sums + "
             f"{sample_size}-row spot check), then swap.")
        return {"table": table, "dry_run": True, "old_row_count": old_count,
                "create_sql": create_sql, "backfill_sql": backfill_sql}

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}_new")
        cur.execute(create_sql)
        cur.execute(backfill_sql, {"county_value": county_value})
    conn.commit()
    _log(f"  [{table}] {table}_new created + backfilled  [{time.time() - t0:.1f}s]")

    new_count = fetch_row_count(conn, f"{table}_new")
    count_ok, count_detail = reconcile_counts(old_count, new_count)

    old_sums = fetch_numeric_sums(conn, table, columns)
    new_sums = fetch_numeric_sums(conn, f"{table}_new", columns)
    sums_ok, sums_detail = reconcile_sums(old_sums, new_sums)

    old_rows, new_rows = fetch_spot_check_rows(conn, table, spec["old_pk"], columns, sample_size)
    compare_cols = [c["column_name"] for c in columns]
    spot_ok, spot_detail = reconcile_spot_check(old_rows, new_rows, compare_cols, spec["old_pk"])

    reconciliation = {"row_count": count_detail, "dollar_sums": sums_detail, "spot_check": spot_detail}
    all_ok = count_ok and sums_ok and spot_ok

    report = {
        "table": table, "dry_run": False, "mode": "composite_pk",
        "old_row_count": old_count, "new_row_count": new_count,
        "reconciliation": reconciliation, "reconciliation_passed": all_ok,
        "swapped": False, "default_dropped": False,
    }

    if not all_ok:
        _log(f"  [{table}] RECONCILIATION FAILED — {table}_new left in place for inspection, "
             f"live {table} is UNTOUCHED, NO SWAP performed.")
        for name, detail in reconciliation.items():
            if not detail["reason"].startswith(("exact match", "all")):
                _log(f"    {name}: {detail['reason']}")
        return report

    _log(f"  [{table}] reconciliation PASSED (row count + dollar sums + "
         f"{sample_size}-row spot check)")

    dropped_fks = []
    for child_table in spec.get("fk_drop", []):
        fk_name = _find_fk_constraint_name(conn, child_table, table)
        if fk_name:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {child_table} DROP CONSTRAINT {fk_name}")
            conn.commit()
            dropped_fks.append({"child_table": child_table, "constraint": fk_name})
            _log(f"  [{table}] dropped stale FK {fk_name} on {child_table} (re-added, pointing "
                 f"at the new composite key, when {child_table} undergoes its own migration)")

    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} RENAME TO {table}_old_pre_partition")
        cur.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    conn.commit()
    report["swapped"] = True
    _log(f"  [{table}] swap committed  [{time.time() - t1:.3f}s]")

    added_fks = []
    for fk in spec.get("fk_add", []):
        cols_sql = ", ".join(fk["columns"])
        ref_cols_sql = ", ".join(fk["ref_columns"])
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {fk['constraint']} "
                f"FOREIGN KEY ({cols_sql}) REFERENCES {fk['ref_table']} ({ref_cols_sql})"
            )
        conn.commit()
        added_fks.append(fk["constraint"])
        _log(f"  [{table}] added composite FK {fk['constraint']} -> {fk['ref_table']}({ref_cols_sql})")

    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN county_code DROP DEFAULT")
    conn.commit()
    report["default_dropped"] = True
    _log(f"  [{table}] DEFAULT '{county_value}' dropped from county_code — future writes must "
         f"supply it explicitly (finding 9.4)")

    report["fk_drops"] = dropped_fks
    report["fk_adds"] = added_fks
    report["old_table_retained_as"] = f"{table}_old_pre_partition"
    report["duration_s"] = round(time.time() - t0, 1)
    _log(f"  [{table}] MIGRATION COMPLETE  [{report['duration_s']}s total]  — "
         f"{table}_old_pre_partition retained (not dropped by this script)")
    return report


def migrate_add_column_table(conn, table, county_value=DEFAULT_COUNTY, dry_run=False, verbose=True):
    """Mode 3 — ingest_audit, load_batch. §4.3's own explicit 'plain
    (non-PK) column' treatment: no shadow-swap (PK doesn't change, these
    are surrogate-keyed append-only tables) — ADD COLUMN (nullable),
    explicit backfill UPDATE, then SET NOT NULL. No DEFAULT is ever added
    at all (unlike mode 1) — there's a real, if brief, nullable window
    between ADD COLUMN and the backfill UPDATE, and this script asserts
    zero NULLs remain before setting NOT NULL, so there's nothing to
    remember to drop afterward and no contamination-vector default is ever
    live on these two tables."""

    def _log(msg):
        if verbose:
            print(msg)

    old_count = fetch_row_count(conn, table)
    if dry_run:
        _log(f"  [DRY RUN] {table}: would ADD COLUMN county_code (nullable), backfill "
             f"{old_count:,} existing row(s) to '{county_value}', then SET NOT NULL. "
             f"No default ever set (finding 9.4 avoided by construction).")
        return {"table": table, "dry_run": True, "mode": "add_column", "old_row_count": old_count}

    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS county_code VARCHAR(20)")
        cur.execute(f"UPDATE {table} SET county_code = %s WHERE county_code IS NULL", (county_value,))
        n_backfilled = cur.rowcount
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE county_code IS NULL")
        remaining_nulls = cur.fetchone()[0]
        if remaining_nulls:
            conn.rollback()
            raise RuntimeError(
                f"{table}: {remaining_nulls:,} row(s) still NULL after backfill UPDATE — "
                f"this should be impossible (the UPDATE targets exactly WHERE county_code IS "
                f"NULL). STOPPING before SET NOT NULL; investigate before re-running."
            )
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN county_code SET NOT NULL")
    conn.commit()

    new_count = fetch_row_count(conn, table)
    count_ok, count_detail = reconcile_counts(old_count, new_count)
    if not count_ok:
        raise RuntimeError(f"{table}: row count changed during an ADD COLUMN/UPDATE "
                            f"({count_detail}) — should be impossible; investigate.")

    _log(f"  [{table}] county_code added, {n_backfilled:,} row(s) backfilled to "
         f"'{county_value}', column set NOT NULL (no default ever set)")
    return {"table": table, "dry_run": False, "mode": "add_column",
            "old_row_count": old_count, "new_row_count": new_count,
            "rows_backfilled": n_backfilled, "default_dropped": "n/a — never set"}


def migrate_default_only_table(conn, table, county_value=DEFAULT_COUNTY, dry_run=False, verbose=True):
    """Mode 2 — county_benchmark. Already has county_code as its leading PK
    column with a DEFAULT (the real prior art SPEC_COUNTY_PARTITIONING.md
    §2 found) — the only real gap is the DEFAULT itself (finding 9.4). No
    shadow-swap, no PK change, no backfill needed in the general case —
    every existing row should already carry a real, explicit 'TRAVIS'
    value (there has only ever been one county writing here). Asserted,
    not assumed: this function counts any row with a DIFFERENT or NULL
    county_code before touching anything, and refuses to proceed if that
    count is nonzero (a real finding that would mean this table needs the
    full composite_pk treatment instead, not silently papered over)."""

    def _log(msg):
        if verbose:
            print(msg)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE county_code IS NULL OR county_code != %s",
            (county_value,),
        )
        n_unexpected = cur.fetchone()[0]

    if dry_run:
        _log(f"  [DRY RUN] {table}: {n_unexpected:,} row(s) with a county_code other than "
             f"'{county_value}' found. Would DROP DEFAULT only if this is 0.")
        return {"table": table, "dry_run": True, "mode": "default_only",
                "unexpected_county_code_rows": n_unexpected}

    if n_unexpected:
        raise RuntimeError(
            f"{table}: {n_unexpected:,} row(s) have a county_code other than the expected "
            f"'{county_value}' — this contradicts the 'only Travis has ever written here' "
            f"assumption mode 2 relies on. STOPPING — investigate before proceeding; this "
            f"table may need the full composite_pk treatment instead."
        )

    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN county_code DROP DEFAULT")
    conn.commit()
    _log(f"  [{table}] confirmed 0 unexpected county_code values; DEFAULT '{county_value}' "
         f"dropped (finding 9.4) — PK/shape otherwise unchanged (already correct)")
    return {"table": table, "dry_run": False, "mode": "default_only", "default_dropped": True}


def run_migration(conn, only=None, county_value=DEFAULT_COUNTY, dry_run=False, verbose=True, sample_size=20):
    """Real orchestrator across every targeted table, in MIGRATION_ORDER
    (real dependency order). Stops the WHOLE RUN — does not proceed to the
    next table — the moment any table fails reconciliation (mode 1) or its
    own safety assertion (modes 2/3), so a bad table never leaves later
    tables migrated on top of an unresolved problem."""
    targets = [t for t in MIGRATION_ORDER if (only is None or t in only)]
    if only:
        unknown = set(only) - set(MIGRATION_ORDER)
        if unknown:
            raise ValueError(f"--only named unknown table(s): {sorted(unknown)}. "
                              f"Real, migratable tables: {MIGRATION_ORDER}")

    reports = []
    for table in targets:
        print(f"\n{'=' * 70}\n  {table}\n{'=' * 70}")
        if table in DEFAULT_ONLY_TABLES:
            report = migrate_default_only_table(conn, table, county_value, dry_run, verbose)
        elif table in ADD_COLUMN_TABLES:
            report = migrate_add_column_table(conn, table, county_value, dry_run, verbose)
        else:
            report = migrate_table(conn, SPEC_BY_NAME[table], county_value, dry_run, verbose, sample_size)
        reports.append(report)
        if (not dry_run) and report.get("reconciliation_passed") is False:
            print(f"\n  STOPPING migration run: {table} failed reconciliation. No further "
                  f"tables will be migrated this run — investigate {table}_new, then re-run "
                  f"with --only {table} once ready.")
            break
    return reports


def build_ledger_report(reports, county_value, started_at, finished_at):
    """PARTITION-2-IMPLEMENT, Part 6 — real, structured summary for a real
    DATA_LIFECYCLE.md Vintage Ledger supersede entry (finding 9.6). NOT the
    Ledger entry itself (that's Notion, handled separately, per the brief)
    — this is everything the PM needs to write that entry without
    reconstructing it from console scrollback: which tables actually
    changed shape, real before/after row counts, whether reconciliation
    passed, whether the swap + default-drop completed, and the retained
    old-table names for rollback reference."""
    return {
        "migration": "county_partitioning (PARTITION-2-IMPLEMENT)",
        "spec_doc": "SPEC_COUNTY_PARTITIONING.md",
        "county_value_applied": county_value,
        "started_at": started_at, "finished_at": finished_at,
        "tables": reports,
        "tables_migrated": [
            r["table"] for r in reports
            if r.get("swapped") or r.get("mode") in ("add_column", "default_only")
            and r.get("dry_run") is False
        ],
        "tables_stopped_before_swap": [
            r["table"] for r in reports if r.get("reconciliation_passed") is False
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                     help="Report what would happen for every targeted table; no writes.")
    ap.add_argument("--only", default=None,
                     help="Comma-separated table names to restrict this run to "
                          "(e.g. 'parcel,parcel_metrics'). Omit to run every real "
                          "table, in real dependency order.")
    ap.add_argument("--county", default=DEFAULT_COUNTY,
                     help=f"county_code value to backfill existing rows with (default: {DEFAULT_COUNTY}).")
    ap.add_argument("--sample-size", type=int, default=20,
                     help="Spot-check sample size per table (default: 20, per §5 step 3).")
    ap.add_argument("--report-out", default=None,
                     help="Write the structured Ledger-ready JSON report to this path.")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}  — confirm this is the environment you intend BEFORE any write commits.")
    print(f"county_code value for backfill: '{args.county}'")
    if not args.dry_run:
        print("\nTHIS WILL MODIFY LIVE SCHEMA on real, populated tables. Confirmed real backup "
              "taken? (This project's standing rule: a real pg_dump before touching production "
              "structure, every time, no exceptions.) Ctrl-C now if not.\n")

    started_at = datetime.datetime.now().isoformat()
    reports = run_migration(conn, only=only, county_value=args.county,
                             dry_run=args.dry_run, sample_size=args.sample_size)
    finished_at = datetime.datetime.now().isoformat()
    conn.close()

    ledger_report = build_ledger_report(reports, args.county, started_at, finished_at)

    print(f"\n{'=' * 70}\n  SUMMARY\n{'=' * 70}")
    for r in reports:
        table = r["table"]
        if r.get("dry_run"):
            cnt = r.get('old_row_count')
            cnt_str = f"{cnt:,}" if isinstance(cnt, int) else str(cnt)
            print(f"  [DRY RUN] {table}: {cnt_str} row(s) would be migrated")
        elif r.get("reconciliation_passed") is False:
            print(f"  {table}: RECONCILIATION FAILED — NOT swapped, {table}_new left for inspection")
        else:
            cnt = r.get('old_row_count')
            cnt_str = f"{cnt:,}" if isinstance(cnt, int) else str(cnt)
            print(f"  {table}: migrated ({cnt_str} row(s))"
                  + (f", {table}_old_pre_partition retained" if r.get("swapped") else ""))

    if args.report_out:
        with open(args.report_out, "w") as f:
            json.dump(ledger_report, f, indent=2, default=str)
        print(f"\nStructured Ledger-ready report written: {args.report_out}")

    failed = any(r.get("reconciliation_passed") is False for r in reports)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
