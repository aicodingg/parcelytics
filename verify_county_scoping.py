#!/usr/bin/env python3
"""
verify_county_scoping.py — MC2-BUILD-1: the real MC-2 audit.

Implements MULTI_COUNTY_ONBOARDING_STANDARDS.md's MC-2 section (already
fully specified there -- this is the "build it" instruction, not a
re-derivation). Built in direct response to FOUR real, confirmed incidents
of the same bug class (a writer omits county_code from an INSERT's column
list and/or its ON CONFLICT target, or an UPDATE/DELETE has no
county-scoped predicate), each found live, one at a time, after the fact:

  1. DALLAS-GATE-4 (commit d983e0b)      -- 5 real tax_billing-family writers.
  2. PIR-XLSX-HOTFIX-1 (commit decd438)  -- loaders/pir_xlsx_common.py,
     missed because a prior audit was scoped to a file list, not the whole
     repo.
  3. load_delinquent() / tax_delinquent  -- found live during the real M7
     backfill. NO historical fix commit exists for this one -- see the
     "REAL, IMPORTANT DISCLOSURE" section below. This is a currently
     live, unpatched bug, not a resolved incident.
  4. BILLING-GATE-HOTFIX-1 (commit 443af9f) -- ingest_gate.py's shared
     _write_audit() function, affecting both billing_gate.py and
     ingest_gate.py's own appraisal-side gate.

MC-2 exists to end the "found one at a time, after the fact" pattern by
checking EVERY real writer of EVERY real county_code-scoped table, every
time this script runs, rather than relying on the next engineer to
remember to grep before shipping.

── REAL, IMPORTANT DISCLOSURE: load_delinquent() is NOT historically fixed ──
The brief that requested this tool asked to find "the real commit that
fixed load_delinquent()" via git log. There isn't one. Direct inspection
of loaders/load_tax_current.py:423-467 (current, as of this tool's build)
shows load_delinquent() still has NO county_code parameter, no county_code
in its INSERT column list, and ON CONFLICT (geo_id) DO UPDATE -- the
pre-migration shape. commit d983e0b (DALLAS-GATE-4) explicitly carves this
table OUT of its own scope via its own fixture test ("load_delinquent()'s
own ON CONFLICT (geo_id) is a DIFFERENT table (tax_delinquent, out of this
brief's tax_billing-writer scope)"); commit 443af9f later names
load_delinquent() as the "same bug class" as its own three predecessors,
but never fixes it. tax_delinquent IS one of migrate_county_partitioning.py's
real TABLE_SPECS (Mode 1: county_code added as the new leading PK column),
so this is a real, live, currently-unpatched NOT NULL violation waiting to
fire on tax_delinquent's next real load run against the migrated schema --
not a hypothetical, and not something this tool invented. Confirmed
running this audit against the real, current repo (see below): it DOES
fire, right now.

Per Diego's own explicit go-ahead: this tool's own fixture test for this
incident (test_verify_county_scoping.py) uses the REAL, CURRENT broken
load_delinquent() code as the failing-shape half of the fixture (proving
the tool catches a real, live bug -- not a synthetic one), and a
hand-written, clearly-labeled HYPOTHETICAL corrected version (matching the
exact pattern DALLAS-GATE-4 used for the other 4 real fixes: county_code
parameter, added to the INSERT column list, ON CONFLICT target corrected
to (county_code, geo_id)) as the passing-shape half. That hypothetical fix
is NOT applied to the real loaders/load_tax_current.py in this brief --
MC2-BUILD-1's scope is the audit tool, not this specific fix. Applying it
for real is a separate, follow-up brief; this tool's own real run against
the live repo (see "REAL RUN AGAINST THE ACTUAL REPO" below) is the honest
record that the bug is still there today.

── The real, full MC-2 rule 3 (verbatim, from MULTI_COUNTY_ONBOARDING_
   STANDARDS.md -- NOT just the brief's own condensed 3-part summary,
   which combines (b)+(c) and doesn't separately name (d)) ────────────────
For every real writer of every real county_code-scoped table:
  (a) the writer is in the table's REGISTERED, CLOSED allowed-writer set --
      an unlisted writer is a failure even if it happens to be correctly
      scoped. (Merges this check with the existing single-writer-per-table
      discipline, THE_FABLE_METHOD.md §5 -- verify_tax_billing_rollup_
      canonical.py's own 4-file closed writer set is the direct precedent.)
  (b) county_code appears in the INSERT's column list.
  (c) county_code appears in the ON CONFLICT / key target.
  (d) UPDATE/DELETE statements carry a county-scoped predicate, OR a
      documented exemption.
Both real confirmed incidents (load_delinquent(), _write_audit()) involved
the exact (b)+(c) combination; this tool implements all four, including
(d), since the brief explicitly instructs reading MC-2 in full as
authoritative over its own condensed restatement.

── The real, complete table registry: 21 tables, not the brief's 9 ──────────
The brief named 9 tables and explicitly warned not to trust its own list
as exhaustive. Reconstructed offline (no live DB in this sandbox -- same
disclosure as every other tool here) from migrate_county_partitioning.py's
own TABLE_SPECS/DEFAULT_ONLY_TABLES/ADD_COLUMN_TABLES (the authoritative
source; schema.sql's CREATE TABLE bodies for the Mode-1 tables are
CONFIRMED STALE, per verify_index_coverage.py's own prior finding) plus
direct schema.sql inspection for the 3 newer tables designed with
county_code from creation (never migrated, never stale). This turned up
12 more real county_code-scoped tables than the brief named: tax_billing_
quarantine, prop_unit, prop_unit_tax_year, parcel_metrics, county_tax_rate,
group_stats, snapshot_breakdown, snapshot_totals, snapshot_neighborhood_
movers, parcel_2026_preliminary_snapshot, county_benchmark, and load_batch
(load_batch's own real NOT NULL county_code column was ALSO missing from
schema.sql entirely -- see the schema.sql patch shipped alongside this
tool). Diego's own explicit instruction: cover all 21, not just the named
9 or the incident-family subset.

Three real write architectures found among these 21 tables, each needing
a DIFFERENT shape of checks (b)/(c) -- discovered by reading the real
writer code, not assumed:
  "upsert"       -- real INSERT ... ON CONFLICT (...) DO UPDATE/DO NOTHING.
                    Both (b) and (c) apply directly.
  "insert_only"  -- real, plain, append-only INSERT with NO ON CONFLICT at
                    all (ingest_audit, load_batch -- surrogate BIGSERIAL PK,
                    every row is new). (b) applies; (c) is a documented N/A,
                    not a failure -- there is no ON CONFLICT target to have.
  "shadow_swap"  -- group_stats and the three snapshot_* tables (all via
                    refresh_group_stats.py / refresh_snapshot_summary.py).
                    The real, live write path INSERTs into a freshly built
                    "{table}_shadow" table (CREATE TABLE ... LIKE {table}
                    INCLUDING ALL), then does an atomic ALTER TABLE RENAME
                    swap -- the LIVE table name is never directly INSERTed
                    into. (b) is checked against the {table}_shadow INSERT
                    (the only place row-level county_code actually gets
                    written); (c) is a documented N/A (a freshly created,
                    empty shadow table can't conflict with anything yet).

── AST-based extraction, NOT naive regex/grep (per the brief's explicit
   instruction to follow verify_index_coverage.py's structural pattern) ──
verify_index_coverage.py's own extraction is scoped to specific known
call-shapes (query()/query_no_nestloop()/*.execute()) at fixed argument
positions. A dedicated investigation (this task, before writing any code)
found this codebase's real SQL-execution call conventions are far more
varied than that: psycopg2.extras.execute_batch(cur, sql, rows),
cur.executemany(sql, rows), batch_upsert(conn, sql, rows) (SQL at position
1), a bespoke query(sql, ...) in app.py (SQL at position 0, implicit
connection) vs. a DIFFERENT, same-named query(conn, sql, ...) in
verify_pid_fix.py (SQL at position 1 -- same name, different signature),
upsert_billing_rows(conn, records) (no SQL argument at the call site at
all -- the SQL lives inside the function body as a module constant),
reload_county_scope(conn, table, county_code, insert_sql, ...) (SQL at
position 3 plus an internally built dynamic f-string), run_from_queries
(two SQL arguments). Enumerating every wrapper-function-name +
argument-position combination would itself be exactly the fragile
"scoped list" failure mode MC-2 exists to eliminate (verify_index_
coverage.py's own call-name allowlist is already too narrow for this
codebase's real conventions, confirmed directly).

This tool sidesteps the problem entirely: it does not look at CALL SITES
at all. It walks every string-literal / f-string / module-or-function-
level-constant-assignment AST node in a file (reusing the same
_resolve_string_node / _collect_simple_string_assignments technique
verify_index_coverage.py already proved out for this exact codebase),
regardless of what function (if any) ultimately receives that string as an
argument, then filters to only the reconstructed texts that contain a real
INSERT INTO / UPDATE ... SET / DELETE FROM keyword for one of the 21
registered tables (or a table's own "_shadow" sibling). It does not matter
whether that string is later passed to query(), batch_upsert(),
execute_batch(), a bespoke wrapper, or nothing at all yet (a module-level
SQL constant assigned but not yet used) -- if the county-scoping bug is IN
the SQL text, this catches it independent of how it's eventually executed.

── Sandbox-vs-live disclosure (same as every other tool here) ──────────────
No live DB in this sandbox. This tool needs none for what it actually
checks -- everything here is real, static AST/regex analysis of real
source files, not a live query. Its output against the real repo (see
below) is real, not a fixture demonstration.

── REAL RUN AGAINST THE ACTUAL REPO (recorded here, not just claimed) ──────
Run via `python3 verify_county_scoping.py` from this directory. As of this
build, the real run surfaces:
  - The real, live load_delinquent() gap described above (rule (b) and (c)
    both fail: no county_code in the INSERT column list, ON CONFLICT
    (geo_id) instead of (county_code, geo_id)).
  - Several real UPDATE/DELETE statements with no county-scoped WHERE
    predicate that this tool flags rather than pre-assumes safe (rule (d))
    -- e.g. loaders/quarantine_contamination.py's incident-remediation
    DELETE FROM tax_billing / DELETE FROM tax_billing_quarantine
    statements, scoped only by _CONTAMINATION_WHERE / geo_id = ANY(%s),
    with no county_code term at all. This tool does NOT assume these are
    safe just because they're old, deliberate, incident-response code --
    that is exactly the kind of judgment call this task's own final report
    flags for Diego rather than deciding unilaterally. See the final
    report for the complete, real finding list.

Usage:
    cd ~/Desktop/Claude\\ Files/parcel_app
    python3 verify_county_scoping.py                # full repo scan
    python3 verify_county_scoping.py --only loaders/load_tax_current.py
    python3 verify_county_scoping.py --table tax_delinquent
"""
import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Files excluded from the "real writer" scan -- matches this codebase's
# established convention (verify_tax_billing_rollup_canonical.py,
# verify_index_coverage.py) for distinguishing production writers from
# test fixtures / other audit tools. A test file that contains
# "INSERT INTO tax_billing" as a string literal inside an assertion is not
# a real writer.
EXCLUDED_NAME_PREFIXES = ("test_", "validate_", "verify_")
EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", "task_staging"}


# ═══════════════════════════════════════════════════════════════════════════
# The real, complete county_code-scoped table registry (21 tables)
# ═══════════════════════════════════════════════════════════════════════════

# write_mode: "upsert" | "insert_only" | "shadow_swap" -- see module
# docstring for what each means and why they need different (b)/(c) checks.
#
# allowed_writers: the REGISTERED, CLOSED set of real production files
# permitted to write this table (rule 3(a)). Built from a real, fresh grep
# of every real INSERT INTO / UPDATE / DELETE FROM reference to each table
# across the whole repo (excluding test_*/validate_*/verify_*), not
# trusted from any prior brief's own writer list -- matching verify_tax_
# billing_rollup_canonical.py's own precedent of finding a writer missing
# from an original spec's table via its own fresh grep. Each entry is a
# one-line documented reason, not a bare filename.
COUNTY_SCOPED_TABLES = {
    "parcel": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "parcel_rollup.py": "the canonical rollup writer",
            "app.py": "an inline write path (confirmed via grep; not further disambiguated by this tool)",
            "loaders/load_parcel_attrs.py": "attribute loader",
            "loaders/load_ajr.py": "AJR loader",
            "loaders/backfill_classi_cd.py": "classi_cd backfill",
            "loaders/load_tax_current.py": "current-year loader (parcel-touching path)",
            "loaders/load_imp_det_sqft.py": "improvement sqft loader",
            "loaders/load_certified_2025.py": "2025 certified loader",
            "loaders/load_2026_preliminary.py": "2026 preliminary loader",
        },
    },
    "parcel_tax_year": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "parcel_rollup.py": "the canonical rollup writer",
            "loaders/load_exemptions.py": "exemptions loader",
            "loaders/load_2026_preliminary.py": "2026 preliminary loader",
            "loaders/load_pir_tcad.py": "PIR TCAD loader",
            "loaders/load_cert_2021.py": "2021 certified loader",
        },
    },
    "tax_billing": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); "
                   "re-keyed further by TAX-BILLING-REKEY-3 (now a derived rollup)",
        "allowed_writers": {
            "tax_billing_rollup.py": "the canonical rollup writer (TAX-BILLING-REKEY-3)",
            "loaders/delete_confirmed_absent_taxcur_rows.py": "tight-scoped confirmed-absent-row deletion",
            "loaders/quarantine_contamination.py": "incident-remediation quarantine/restore paths (DALLAS-GATE-4 fixed the INSERT/ON CONFLICT sides; see rule (d) finding for its DELETE side)",
            "loaders/backfill_tax_billing_2025_confidence.py": "2025 confidence-field backfill (UPDATE-only, no ON CONFLICT target)",
        },
    },
    "tax_billing_entity": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); re-keyed by TAX-BILLING-REKEY-3",
        "allowed_writers": {
            "tax_billing_rollup.py": "the canonical rollup writer",
            "loaders/delete_confirmed_absent_taxcur_rows.py": "tight-scoped confirmed-absent-row deletion",
        },
    },
    "tax_delinquent": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "loaders/load_tax_current.py": "load_delinquent() -- REAL, LIVE, CURRENTLY UNPATCHED gap (see module docstring)",
        },
    },
    "tax_billing_quarantine": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1, folded in per finding 9.10)",
        "allowed_writers": {
            "loaders/quarantine_contamination.py": "the only real writer -- quarantine/restore paths",
        },
    },
    "prop_unit": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "loaders/ears_format.py": "shared unit-write path used by all real per-year loaders",
        },
    },
    "prop_unit_tax_year": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "loaders/ears_format.py": "shared unit-year write path",
            "loaders/load_certified_historical.py": "historical certified loader",
            "loaders/backfill_prop_unit_tax_year_geoid.py": "per-year geo_id backfill",
            "loaders/load_certified_2025.py": "2025 certified loader",
            "loaders/load_2026_preliminary.py": "2026 preliminary loader",
        },
    },
    "parcel_metrics": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1, with a composite FK to parcel)",
        "allowed_writers": {
            "loaders/load_pir_billing.py": "confirmed via grep -- not further disambiguated by this tool",
            "loaders/compute_metrics.py": "the canonical metrics builder",
        },
    },
    "county_tax_rate": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1)",
        "allowed_writers": {
            "loaders/load_tax_rates.py": "the only real writer",
        },
    },
    "group_stats": {
        "write_mode": "shadow_swap", "shadow_of": "group_stats",
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); real write path is the shadow-swap in loaders/refresh_group_stats.py",
        "allowed_writers": {
            "loaders/refresh_group_stats.py": "the only real writer -- shadow-table build + atomic rename swap",
        },
    },
    "snapshot_breakdown": {
        "write_mode": "shadow_swap", "shadow_of": "snapshot_breakdown",
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); real write path is the shadow-swap in loaders/refresh_snapshot_summary.py",
        "allowed_writers": {
            "loaders/refresh_snapshot_summary.py": "the only real writer -- shadow-table build + atomic rename swap",
        },
    },
    "snapshot_totals": {
        "write_mode": "shadow_swap", "shadow_of": "snapshot_totals",
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); real write path is the shadow-swap in loaders/refresh_snapshot_summary.py",
        "allowed_writers": {
            "loaders/refresh_snapshot_summary.py": "the only real writer -- shadow-table build + atomic rename swap",
        },
    },
    "snapshot_neighborhood_movers": {
        "write_mode": "shadow_swap", "shadow_of": "snapshot_neighborhood_movers",
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1); real write path is the shadow-swap in loaders/refresh_snapshot_summary.py",
        "allowed_writers": {
            "loaders/refresh_snapshot_summary.py": "the only real writer -- shadow-table build + atomic rename swap",
        },
    },
    "parcel_2026_preliminary_snapshot": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py TABLE_SPECS (Mode 1, DALLAS-GATE-2 addition)",
        "allowed_writers": {
            "loaders/snapshot_2026_preliminary.py": "the only real writer",
        },
    },
    "county_benchmark": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "migrate_county_partitioning.py DEFAULT_ONLY_TABLES (Mode 2 -- already county_code-leading, DEFAULT dropped)",
        "allowed_writers": {
            "loaders/compute_metrics.py": "the only real writer",
        },
    },
    "ingest_audit": {
        "write_mode": "insert_only", "shadow_of": None,
        "source": "migrate_county_partitioning.py ADD_COLUMN_TABLES (Mode 3); schema.sql was stale for this table's county_code column entirely until this task's own schema.sql patch",
        "allowed_writers": {
            "loaders/ingest_gate.py": "_write_audit() -- shared by ingest_gate.py's own gate and billing_gate.py (BILLING-GATE-HOTFIX-1 fixed this)",
        },
    },
    "load_batch": {
        "write_mode": "insert_only", "shadow_of": None,
        "source": "migrate_county_partitioning.py ADD_COLUMN_TABLES (Mode 3); schema.sql was stale for this table's county_code column entirely until this task's own schema.sql patch -- this table was ALSO missing from the brief's own named 9",
        "allowed_writers": {
            "loaders/refresh_group_stats.py": "_mint_batch()",
            "loaders/refresh_snapshot_summary.py": "its own batch-minting path (same real function name/pattern, confirmed via grep)",
        },
    },
    "tax_billing_account": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "schema.sql, designed with county_code from creation (TAX-BILLING-REKEY-3) -- never migrated, never stale",
        "allowed_writers": {
            "loaders/load_pir_billing.py": "PIR billing loader",
            "loaders/load_tax_current.py": "current-year loader (account-grain path)",
            "loaders/load_pir_billing_2021_full.py": "2021 full PIR loader",
            "loaders/pir_xlsx_common.py": "shared 2022/2023/2024 PIR module",
        },
    },
    "tax_billing_account_entity": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "schema.sql, designed with county_code from creation (TAX-BILLING-REKEY-3) -- never migrated, never stale",
        "allowed_writers": {
            "loaders/load_pir_billing.py": "PIR billing loader",
            "loaders/load_tax_current.py": "current-year loader (account-grain path)",
            "loaders/load_pir_billing_2021_full.py": "2021 full PIR loader",
            "loaders/pir_xlsx_common.py": "shared 2022/2023/2024 PIR module",
        },
    },
    "tax_billing_portal_scrape": {
        "write_mode": "upsert", "shadow_of": None,
        "source": "schema.sql, designed with county_code from creation (TAX-BILLING-REKEY-3) -- never migrated, never stale",
        "allowed_writers": {
            "app.py": "an inline sentinel write path (confirmed via grep; not further disambiguated by this tool)",
            "loaders/scrape_billing_history.py": "the real portal-scrape writer",
        },
    },
}

# Reverse index: "{table}_shadow" -> real table name, for shadow_swap tables.
_SHADOW_TABLE_NAMES = {
    f"{t}_shadow": t
    for t, spec in COUNTY_SCOPED_TABLES.items()
    if spec["write_mode"] == "shadow_swap"
}
_ALL_MATCHABLE_TABLE_NAMES = set(COUNTY_SCOPED_TABLES) | set(_SHADOW_TABLE_NAMES)


# ═══════════════════════════════════════════════════════════════════════════
# Stage 1 — Extraction: every string-literal / f-string / constant-assignment
# node in a file, NOT scoped to specific call sites (see module docstring).
# Reuses the _resolve_string_node / _collect_simple_string_assignments
# technique proven out in verify_index_coverage.py for this same codebase.
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExtractedStatement:
    filepath: str
    lineno: int
    stmt_kind: str      # "INSERT" | "UPDATE" | "DELETE"
    table: str           # the real table name (shadow suffix stripped)
    is_shadow: bool
    sql_text: str
    resolution: str


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _resolve_string_node(node, assignments):
    """Best-effort resolution of an AST expression node to text. Same
    technique as verify_index_coverage.py's own function of the same name
    (literal / f-string / string-concat / same-file-variable) -- see this
    file's module docstring for why call-site argument position is
    deliberately NOT part of this resolution."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "literal"

    if isinstance(node, ast.JoinedStr):
        parts = []
        any_dynamic = False
        any_resolved_dynamic = False
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                # PX-20260823-04 Task 1: a FormattedValue whose value is a
                # bare Name -- e.g. an f-string's `{columns}` -- used to
                # ALWAYS render as the literal placeholder text "{columns}"
                # below, even though `assignments` (built by
                # _collect_simple_string_assignments, walking the same file)
                # may already hold that name's real, statically-resolved
                # string value. That was the actual bug: the lookup table
                # existed, this branch just never consulted it. Bounded on
                # purpose -- only a bare ast.Name is attempted (matching the
                # brief's "FormattedValue whose value is a Name node"), not
                # arbitrary expressions inside {}; anything else (a call, an
                # attribute access, a reassigned/ambiguous name not present
                # in `assignments`) still falls through to the original
                # literal-placeholder rendering, unchanged from before this
                # fix. No guessing, no execution -- a static lookup against
                # the exact same single-assignment table this file already
                # trusted for top-level `NAME = f"..."` constants.
                if isinstance(v.value, ast.Name) and v.value.id in assignments:
                    parts.append(assignments[v.value.id])
                    any_resolved_dynamic = True
                else:
                    any_dynamic = True
                    parts.append("{" + _unparse(v.value) + "}")
            else:
                any_dynamic = True
                parts.append("{?}")
        if any_dynamic:
            kind = "fstring_dynamic"
        elif any_resolved_dynamic:
            kind = "fstring_dynamic_resolved"
        else:
            kind = "fstring_static"
        return "".join(parts), kind

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left_text, left_kind = _resolve_string_node(node.left, assignments)
        right_text, right_kind = _resolve_string_node(node.right, assignments)
        combined = left_text + right_text
        if "unresolved" in (left_kind, right_kind) or "variable_unresolved" in (left_kind, right_kind):
            return combined, "unresolved"
        return combined, "concat"

    if isinstance(node, ast.Name):
        if node.id in assignments:
            return assignments[node.id], "variable"
        return f"{{{node.id}}}", "variable_unresolved"

    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        # PX-20260823-04 Task 1: explicitly named in the brief as in-scope
        # ("a static expression of string constants (including ", ".join
        # ([...]) over a constant list") even though none of the three real
        # cases actually use it -- supported here so a future writer using
        # this real, idiomatic Python pattern for a column list doesn't
        # reopen the same class of tool-limitation gap. Deliberately narrow:
        # separator must itself be a literal string, and every list/tuple
        # element must be a plain string constant -- an element that's
        # itself a Name/Call/anything non-constant makes the whole
        # expression unresolved (no partial credit, no guessing at what an
        # unresolvable element might contain).
        sep = node.func.value.value
        items = []
        for el in node.args[0].elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                items.append(el.value)
            else:
                return "{<unresolved-join>}", "unresolved"
        return sep.join(items), "join"

    return "{<unresolved-expr>}", "unresolved"


def _collect_simple_string_assignments(tree):
    """Same first-pass technique as verify_index_coverage.py: `name = <string
    expr>` anywhere in the file (module or function scope), last-assignment-
    wins. Not full data-flow analysis -- good enough to resolve this
    codebase's real `BILLING_SQL = f\"\"\"...\"\"\"` / `_UPSERT_SQL = \"\"\"...\"\"\"`
    module-constant convention, confirmed dominant across this repo."""
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            text, kind = _resolve_string_node(node.value, assignments)
            if kind not in ("unresolved", "variable_unresolved"):
                assignments[node.targets[0].id] = text
    return assignments


_INSERT_TABLE_RE = re.compile(r'\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)', re.IGNORECASE)
_UPDATE_TABLE_RE = re.compile(r'\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)', re.IGNORECASE)
_DELETE_TABLE_RE = re.compile(r'\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)', re.IGNORECASE)


def _classify_statement(text):
    """Returns (stmt_kind, raw_table_name) for the FIRST real INSERT/UPDATE/
    DELETE keyword found referencing one of the 21 registered tables (or a
    _shadow sibling), or None if this string isn't a write statement against
    a table this tool cares about. A string can legitimately contain more
    than one keyword (e.g. a module docstring listing several tables) --
    this only fires on a match against a REAL registered table name, not on
    the keyword alone, to avoid false positives on prose."""
    for m in _INSERT_TABLE_RE.finditer(text):
        name = m.group(1)
        if name in _ALL_MATCHABLE_TABLE_NAMES:
            return "INSERT", name
    for m in _UPDATE_TABLE_RE.finditer(text):
        name = m.group(1)
        if name in _ALL_MATCHABLE_TABLE_NAMES:
            return "UPDATE", name
    for m in _DELETE_TABLE_RE.finditer(text):
        name = m.group(1)
        if name in _ALL_MATCHABLE_TABLE_NAMES:
            return "DELETE", name
    return None, None


def _docstring_constant_ids(tree):
    """PX-20260823-04 (discovered during Task 1/2 verification, not
    originally scoped by the brief -- see final report): id()s of every
    ast.Constant string node that is a REAL docstring -- the first
    statement of a Module/FunctionDef/AsyncFunctionDef/ClassDef body, when
    that statement is a bare `Expr(Constant(str))`. This is the exact same
    definition ast.get_docstring() uses; not a heuristic.

    Why this matters here: _build_insert_sql()'s docstring in
    loaders/refresh_group_stats.py explains the function's SQL shape in
    prose that legitimately quotes 'INSERT INTO group_stats_shadow (...)'
    (literal ellipsis, not real columns) -- a real, registered table name
    sitting right next to a real INSERT keyword, so it passes
    _classify_statement()'s keyword+table-name filter and gets extracted
    as if it were an actual SQL statement. It never was one; scanning it
    for county_code produces a permanent, unfixable-by-resolution FAIL
    (there IS no column list to resolve -- the "columns" are the literal
    text "..."). This was already a latent gap in the extractor before
    PX-20260823-04 -- Task 1's FormattedValue fix made the file's ONE
    real statement resolve correctly, which is what exposed this second,
    unrelated statement as a standalone finding for the first time (it
    was previously masked by sharing an EXEMPTIONS key with the real one).
    Skipping true docstrings from extraction is the correct, narrow fix:
    it doesn't touch FormattedValue resolution (Task 1's actual scope) and
    doesn't add a new EXEMPTIONS entry for what was never a real SQL
    statement in the first place."""
    ids = set()
    docstring_owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if isinstance(node, docstring_owners) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def extract_statements_from_source(source_text, filepath):
    """Walks EVERY string-literal/f-string node AND every simple constant
    assignment in the file -- not scoped to any particular call site or
    function-argument position (see module docstring for why). Returns
    every ExtractedStatement referencing a registered table.

    Excludes genuine docstrings (see _docstring_constant_ids) -- a
    docstring is prose ABOUT code, never itself an executed SQL string, so
    it can never be a real 3a/3b/3c/3d finding; treating one as SQL only
    ever produces a false positive."""
    tree = ast.parse(source_text, filename=filepath)
    assignments = _collect_simple_string_assignments(tree)
    docstring_ids = _docstring_constant_ids(tree)
    results = []
    seen_line_table_kind = set()

    def _maybe_add(node, text, resolution):
        stmt_kind, raw_table = _classify_statement(text)
        if stmt_kind is None:
            return
        is_shadow = raw_table in _SHADOW_TABLE_NAMES
        real_table = _SHADOW_TABLE_NAMES[raw_table] if is_shadow else raw_table
        key = (getattr(node, "lineno", -1), real_table, stmt_kind, is_shadow)
        if key in seen_line_table_kind:
            return
        seen_line_table_kind.add(key)
        results.append(ExtractedStatement(
            filepath, getattr(node, "lineno", -1), stmt_kind, real_table,
            is_shadow, text, resolution,
        ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            _maybe_add(node, node.value, "literal")
        elif isinstance(node, ast.JoinedStr):
            text, resolution = _resolve_string_node(node, assignments)
            _maybe_add(node, text, resolution)

    return results


def extract_statements_from_tree(root_paths):
    """root_paths: files/directories, repo-root-relative or absolute.
    Excludes test_*/validate_*/verify_* files and EXCLUDED_DIRS, matching
    this codebase's established convention for distinguishing production
    writers from test fixtures and other audit tools. Returns
    (list[ExtractedStatement], list[(file, error)])."""
    files = []
    for p in root_paths:
        full = p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    if fn.startswith(EXCLUDED_NAME_PREFIXES):
                        continue
                    files.append(os.path.join(dirpath, fn))
        elif os.path.isfile(full):
            fn = os.path.basename(full)
            if not fn.startswith(EXCLUDED_NAME_PREFIXES):
                files.append(full)

    extracted = []
    errors = []
    for f in sorted(set(files)):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            extracted.extend(extract_statements_from_source(source, os.path.relpath(f, REPO_ROOT)))
        except SyntaxError as e:
            errors.append((os.path.relpath(f, REPO_ROOT), str(e)))
    return extracted, errors


# ═══════════════════════════════════════════════════════════════════════════
# Stage 2 — Real SQL-shape checks for rule 3(b)/(c)/(d)
# ═══════════════════════════════════════════════════════════════════════════

def _find_matching_paren_end(text, open_idx):
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_insert_columns(text):
    """Returns the list of column names in an `INSERT INTO table (col1,
    col2, ...)` statement's column list, or None if no parenthesized column
    list immediately follows the table name (e.g. INSERT INTO table
    SELECT ... with no explicit column list -- reported separately as
    UNRESOLVED_COLUMNS, not silently treated as 'missing county_code')."""
    m = _INSERT_TABLE_RE.search(text)
    if not m:
        return None
    rest = text[m.end():]
    paren_m = re.match(r'\s*\(', rest)
    if not paren_m:
        return None
    open_idx = paren_m.end() - 1
    close_idx = _find_matching_paren_end(rest, open_idx)
    if close_idx == -1:
        return None
    col_text = rest[open_idx + 1:close_idx]
    cols = [c.strip().strip('"').lower() for c in col_text.split(",")]
    return [c for c in cols if c]


_ON_CONFLICT_RE = re.compile(r'\bON\s+CONFLICT\s*\(([^)]*)\)', re.IGNORECASE)
_ON_CONFLICT_NO_TARGET_RE = re.compile(r'\bON\s+CONFLICT\s+DO\s+(NOTHING|UPDATE)\b', re.IGNORECASE)


def _extract_on_conflict_target(text):
    """Returns (target_cols, found) -- found=False means no ON CONFLICT
    clause at all in this statement (fine for insert_only/shadow_swap
    tables, a real gap for upsert tables); target_cols=[] with found=True
    covers the rare `ON CONFLICT DO NOTHING` with no explicit target list."""
    m = _ON_CONFLICT_RE.search(text)
    if m:
        cols = [c.strip().strip('"').lower() for c in m.group(1).split(",")]
        return [c for c in cols if c], True
    if _ON_CONFLICT_NO_TARGET_RE.search(text):
        return [], True
    return [], False


def _where_clause_text(text):
    """Returns the substring from the first top-level WHERE keyword to the
    end of the statement (or empty string if none) -- good enough for a
    simple 'does county_code appear in the predicate' check, matching this
    tool's own stated scope (flag for human review, not a full SQL
    grammar)."""
    m = re.search(r'\bWHERE\b', text, re.IGNORECASE)
    if not m:
        return ""
    return text[m.start():]


_COUNTY_CODE_TOKEN_RE = re.compile(r'\bcounty_code\b', re.IGNORECASE)


def statement_has_county_code_token(text):
    return bool(_COUNTY_CODE_TOKEN_RE.search(text))


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 — Cross-reference: the real 4-part rule 3 (a)-(d)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    filepath: str
    lineno: int
    table: str
    stmt_kind: str
    rule: str            # "3a" | "3b" | "3c" | "3d"
    severity: str         # "FAIL" | "N/A" | "PASS"
    detail: str


def audit_extracted(extracted):
    findings = []
    for stmt in extracted:
        spec = COUNTY_SCOPED_TABLES[stmt.table]
        allowed = spec["allowed_writers"]
        write_mode = spec["write_mode"]

        # Rule 3(a): registered, closed writer set.
        if stmt.filepath not in allowed:
            findings.append(Finding(
                stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3a", "FAIL",
                f"{stmt.filepath} is NOT in {stmt.table}'s registered allowed-writer set "
                f"({sorted(allowed)}) -- unlisted writer, per MC-2 rule 3(a).",
            ))
        else:
            findings.append(Finding(
                stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3a", "PASS",
                f"{stmt.filepath} is a registered writer ({allowed[stmt.filepath]}).",
            ))

        if stmt.stmt_kind == "INSERT":
            cols = _extract_insert_columns(stmt.sql_text)
            if cols is None:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3b", "FAIL",
                    "No parenthesized column list found immediately after the table name "
                    "(e.g. INSERT INTO ... SELECT, or a dynamically resolved fragment) -- "
                    "cannot confirm county_code is present; treated as a finding for human review.",
                ))
            elif "county_code" in cols:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3b", "PASS",
                    "county_code present in the INSERT column list.",
                ))
            else:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3b", "FAIL",
                    f"county_code MISSING from the INSERT column list: {cols}",
                ))

            target_cols, found = _extract_on_conflict_target(stmt.sql_text)
            if write_mode in ("insert_only", "shadow_swap"):
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3c", "N/A",
                    f"write_mode={write_mode!r}: no ON CONFLICT target is expected for this "
                    f"table's real write architecture (see module docstring).",
                ))
            elif not found:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3c", "FAIL",
                    "No ON CONFLICT clause found on an 'upsert'-mode table's INSERT -- "
                    "either a real gap or this INSERT is genuinely conflict-free (e.g. "
                    "restoring into a table guaranteed empty of the row); flagged for review.",
                ))
            elif not target_cols:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3c", "FAIL",
                    "ON CONFLICT DO NOTHING/UPDATE with no explicit target column list -- "
                    "cannot confirm county_code is part of the real conflict target.",
                ))
            elif "county_code" in target_cols:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3c", "PASS",
                    f"county_code present in the ON CONFLICT target: {target_cols}",
                ))
            else:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3c", "FAIL",
                    f"county_code MISSING from the ON CONFLICT target: {target_cols}",
                ))

        elif stmt.stmt_kind in ("UPDATE", "DELETE"):
            where_text = _where_clause_text(stmt.sql_text)
            if statement_has_county_code_token(where_text):
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3d", "PASS",
                    "county_code referenced in the WHERE clause.",
                ))
            else:
                findings.append(Finding(
                    stmt.filepath, stmt.lineno, stmt.table, stmt.stmt_kind, "3d", "FAIL",
                    "No county_code reference found in this UPDATE/DELETE's WHERE clause, "
                    "and no documented exemption is registered for this statement -- flagged "
                    "for review (a real cross-county blast-radius risk once >1 county's data "
                    "coexists in this table, not necessarily a bug today with Travis-only data).",
                ))
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# Exemption registry — PX-20260823-02 Part 1.
#
# Every real UPDATE/DELETE either gets county_code scoping (the default,
# applied everywhere it was cheap to do so — see PX-20260823-02's Part 2
# file-by-file fixes) or a registered, documented exemption in THIS tool —
# never comment-only. A comment in the loader source is invisible to CI and
# to the next engineer running this script; a registry entry here is not.
#
# Key: (filepath, table, stmt_kind) — repo-root-relative filepath, the real
# table name (shadow suffix already stripped by extraction), and one of
# "INSERT" / "UPDATE" / "DELETE". Deliberately file+table+kind grain, not
# per-line: a single root cause (e.g. one f-string-resolution tool
# limitation) can produce more than one FAIL at different line numbers in
# the same file against the same table, and one exemption entry should
# cover all of them, not force a duplicate entry per line.
#
# Each entry MUST carry:
#   reason      -- human-readable justification (mandatory, never blank).
#   approved_by -- the PX brief ID that approved this exemption.
#
# General-purpose across rules 3b/3c/3d, not narrowly 3d-only: the brief's
# own Not-in-scope section requires BOTH a 3d exemption (the DELETE) and a
# 3c exemption (the INSERT's missing ON CONFLICT) for the same
# quarantine_contamination.py file, so a 3d-only registry couldn't honestly
# cover what this brief itself asks for.
#
# Law 3, enforced by _apply_exemptions() below: an exemption that matches
# ZERO findings on a given run is itself a loud FAILURE ("stale exemption"),
# not a silent pass. Exemptions can't outlive the code they excuse.
# ═══════════════════════════════════════════════════════════════════════════

EXEMPTIONS = {
    # PX-20260823-04 Task 2: the two entries formerly registered here --
    # ("loaders/refresh_group_stats.py", "group_stats", "INSERT") and
    # ("loaders/load_cert_2021.py", "parcel_tax_year", "INSERT") -- are
    # REMOVED, not left in place stale. Both existed solely to document-
    # around a real tool limitation (this auditor's f-string resolver
    # couldn't see through a FormattedValue naming a same-file variable),
    # not to excuse an actual gap in county_code scoping. That limitation is
    # now fixed directly in _resolve_string_node's ast.JoinedStr branch
    # (bounded ast.Name-in-`assignments` lookup) -- both statements now
    # resolve their real column lists (which already, correctly, include
    # county_code) and PASS on their own merits. Per Law 3 below, an
    # exemption that no longer matches any finding must not be left sitting
    # in the registry to go stale; the correct move for a genuinely-obsolete
    # entry is deletion, which is what happened here -- not the deletion
    # itself triggering the stale-exemption alarm, since a deleted key can
    # never be "matched zero times" (it isn't examined at all).
    ("loaders/snapshot_2026_preliminary.py", "parcel_2026_preliminary_snapshot", "INSERT"): {
        "reason": (
            "Correct-by-design, not a gap: this INSERT always runs immediately "
            "after a full DELETE of the same table in the same uncommitted "
            "transaction (a whole-table rebuild) -- no ON CONFLICT is needed "
            "or correct for a table guaranteed empty of every row right "
            "before the INSERT. Verified under PX-20260822-06-rev1."
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/compute_metrics.py", "parcel_metrics", "DELETE"): {
        "reason": (
            "Deliberate same-transaction DELETE+INSERT whole-table rebuild in "
            "compute_parcel_metrics(). Scoping only the DELETE to one county "
            "would BREAK the rebuild, not just leave it unscoped: the paired "
            "INSERT ... SELECT (next registry entry) has no ON CONFLICT and "
            "reads parcel_tax_year with no county filter, so un-deleted "
            "other-county rows would collide on duplicate keys the moment "
            ">1 county's data coexists. This is the brief's own named "
            "exemption pattern: 'a same-transaction DELETE+INSERT rebuild "
            "where the DELETE is deliberately whole-table.'"
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/compute_metrics.py", "parcel_metrics", "INSERT"): {
        "reason": (
            "Paired with the parcel_metrics DELETE exemption above, same "
            "compute_parcel_metrics() whole-table rebuild -- correct-by-design "
            "absence of ON CONFLICT, matching the snapshot_2026_preliminary.py "
            "precedent (also registered above)."
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/delete_confirmed_absent_taxcur_rows.py", "tax_billing", "DELETE"): {
        "reason": (
            "Group 4 downgrade per this brief's own explicit Not-in-scope "
            "section: a tightly-scoped, one-time incident-remediation script "
            "(confirmed-absent-row deletion, built for the July 2026 62-row "
            "gap investigation), not a general-purpose loader -- excluded "
            "from further loader-side county_code changes by the brief itself."
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/delete_confirmed_absent_taxcur_rows.py", "tax_billing_entity", "DELETE"): {
        "reason": (
            "Same script, same Group 4 downgrade as this file's tax_billing "
            "DELETE exemption above -- see that entry for the full reason."
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/quarantine_contamination.py", "tax_billing", "DELETE"): {
        "reason": (
            "Group 4 downgrade per this brief's own explicit Not-in-scope "
            "section: incident-remediation quarantine/restore path. "
            "DALLAS-GATE-4 already fixed this file's INSERT/ON CONFLICT sides "
            "(see this table's allowed_writers note); this DELETE is its "
            "remaining unscoped side, deliberately left as-is per the brief."
        ),
        "approved_by": "PX-20260823-02",
    },
    ("loaders/quarantine_contamination.py", "tax_billing_quarantine", "INSERT"): {
        "reason": (
            "Correct-by-design, not a gap: this INSERT deliberately carries no "
            "ON CONFLICT -- tax_billing_quarantine.county_code is NOT NULL, so "
            "a row that somehow arrived without county_code would fail loud "
            "(a constraint violation) at write time rather than silently "
            "upserting/corrupting data. Already documented as correct-by-design "
            "in PX-20260822-06-rev1's test suite."
        ),
        "approved_by": "PX-20260823-02",
    },
}


def _apply_exemptions(findings):
    """
    Converts FAIL findings that match a registered EXEMPTIONS entry to
    severity "EXEMPT" (reported, not a failure). Then, per Law 3 (an
    exemption can't silently outlive the code it excuses), any registry
    entry that matched ZERO findings on this run becomes a NEW loud FAIL
    ("stale exemption") -- so if the code an exemption was written for gets
    rewritten, deleted, or genuinely fixed, the registry entry itself goes
    loud instead of quietly excusing nothing forever.
    """
    matched_keys = set()
    result = []
    for f in findings:
        key = (f.filepath, f.table, f.stmt_kind)
        if f.severity == "FAIL" and key in EXEMPTIONS:
            entry = EXEMPTIONS[key]
            matched_keys.add(key)
            result.append(Finding(
                f.filepath, f.lineno, f.table, f.stmt_kind, f.rule, "EXEMPT",
                f"EXEMPT ({entry['reason']}) -- approved by {entry['approved_by']}. "
                f"Original finding: {f.detail}",
            ))
        else:
            result.append(f)

    for key, entry in EXEMPTIONS.items():
        if key not in matched_keys:
            filepath, table, stmt_kind = key
            result.append(Finding(
                filepath, 0, table, stmt_kind, "exempt-stale", "FAIL",
                f"STALE EXEMPTION: this registered EXEMPTIONS entry for "
                f"(file={filepath!r}, table={table!r}, stmt_kind={stmt_kind!r}) "
                f"matched NO finding in this run (reason on file: "
                f"{entry['reason']!r}, approved by {entry['approved_by']}). "
                f"Per Law 3, a stale exemption is a loud failure, not a silent "
                f"pass -- either the finding it excused is genuinely gone "
                f"(remove this registry entry) or something else changed "
                f"(investigate before removing).",
            ))
    return result


def run_audit(root_paths=None, only_table=None):
    if root_paths is None:
        root_paths = [REPO_ROOT]
    extracted, errors = extract_statements_from_tree(root_paths)
    if only_table:
        extracted = [e for e in extracted if e.table == only_table]
    findings = audit_extracted(extracted)
    findings = _apply_exemptions(findings)
    return {
        "extracted": extracted,
        "errors": errors,
        "findings": findings,
    }


def print_report(result):
    findings = result["findings"]
    fails = [f for f in findings if f.severity == "FAIL"]
    passes = [f for f in findings if f.severity == "PASS"]
    nas = [f for f in findings if f.severity == "N/A"]
    exempt = [f for f in findings if f.severity == "EXEMPT"]

    print("=" * 78)
    print("verify_county_scoping.py — MC-2 real audit")
    print("=" * 78)
    print(f"Statements extracted: {sum(1 for _ in result['extracted'])}")
    print(f"Findings: {len(findings)}  ({len(fails)} FAIL, {len(passes)} PASS, "
          f"{len(nas)} N/A, {len(exempt)} EXEMPT)")
    if result["errors"]:
        print(f"\nParse errors ({len(result['errors'])}):")
        for f, e in result["errors"]:
            print(f"  {f}: {e}")

    if exempt:
        print(f"\n{'─' * 78}\nEXEMPT ({len(exempt)}) — registered, documented, not a failure:\n{'─' * 78}")
        for f in sorted(exempt, key=lambda x: (x.table, x.filepath, x.lineno)):
            print(f"  [{f.rule}] {f.table} :: {f.filepath}:{f.lineno} ({f.stmt_kind})")
            print(f"        {f.detail}")

    if fails:
        print(f"\n{'─' * 78}\nFAILURES ({len(fails)}):\n{'─' * 78}")
        for f in sorted(fails, key=lambda x: (x.table, x.filepath, x.lineno)):
            print(f"  [{f.rule}] {f.table} :: {f.filepath}:{f.lineno} ({f.stmt_kind})")
            print(f"        {f.detail}")
    else:
        print("\nNo failures.")
    print("=" * 78)
    return len(fails) == 0


# ═══════════════════════════════════════════════════════════════════════════
# PX-20260824-06 Task 4 — hardcoded county-literal comparison scanner
#
# Placement justification (per the brief's explicit "whichever fits the
# pattern's shape" instruction): neither existing scanner is a natural
# home. verify_template_county_scoping.py operates on raw Jinja/HTML text
# in templates/*.html via regex, with no Python AST at all -- the two real
# PX-20260824-06 sites (app.py's api_search_filter()) are Python
# conditionals, not template markup, so that scanner's whole extraction
# layer doesn't apply. THIS file's own extraction stage IS a Python-AST
# walker over the whole repo already (see module docstring's "AST-based
# extraction, NOT naive regex/grep" section) -- the closest-shaped
# infrastructure available, even though its Stage 2/3 checks are about SQL
# statement text, not comparison expressions. Reusing this file's file-
# walking/exclusion conventions (EXCLUDED_NAME_PREFIXES, EXCLUDED_DIRS,
# REPO_ROOT) for a NEW, independent check function is a better fit than
# bolting a Python-AST walker onto the template scanner, or standing up a
# third, entirely separate script for one check.
#
# No second hardcoded county list: the "which literals count as a county
# literal" registry is not hand-typed here -- it's parsed directly out of
# app.py's own COUNTY_SLUGS / COUNTY_PROFILES dict literals via AST (same
# technique this file already uses to resolve SQL string constants), so a
# future Harris/etc. registration is picked up automatically the next time
# this check runs, with nothing to keep in sync by hand.
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiteralFinding:
    filepath: str
    lineno: int
    literal: str
    code_snippet: str
    severity: str = "FAIL"
    detail: str = ""


def _load_county_registry_from_app_py(app_py_path=None):
    """Parses app.py's own AST (does not import/execute app.py -- same
    reason every other tool in this codebase avoids that: app.py has real
    Flask/psycopg2/Sentry imports and route registration side effects at
    module load time) to extract the REAL, current COUNTY_SLUGS and
    COUNTY_PROFILES dict literals. Returns (slugs: {slug: code},
    profiles: {code: county_name_or_None})."""
    path = app_py_path or os.path.join(REPO_ROOT, "app.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    slugs, profiles = {}, {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if name == "COUNTY_SLUGS" and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Constant) and isinstance(v.value, str):
                    slugs[k.value] = v.value
        elif name == "COUNTY_PROFILES" and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Dict)):
                    continue
                county_name = None
                for pk, pv in zip(v.keys, v.values):
                    if (isinstance(pk, ast.Constant) and pk.value == "county_name"
                            and isinstance(pv, ast.Constant) and isinstance(pv.value, str)):
                        county_name = pv.value
                profiles[k.value] = county_name
    return slugs, profiles


def _build_watched_county_literals(slugs, profiles):
    """Every literal-string SHAPE a hardcoded county comparison could
    plausibly use in this codebase, derived entirely from the registry
    dicts above -- e.g. slugs={'travis-tx': 'TRAVIS', ...} and
    profiles={'TRAVIS': 'Travis County', ...} yields {'travis-tx',
    'travis', 'TRAVIS', 'Travis County', 'TRAVIS COUNTY', ...}. The walker
    below compares case-insensitively, so only one case per shape is
    needed here."""
    watched = set()
    for slug, code in slugs.items():
        watched.add(slug)                      # "travis-tx"
        watched.add(slug.split("-")[0])        # "travis" -- the query-string-param shape api_search_filter() used
        watched.add(code)                       # "TRAVIS"
    for code, county_name in profiles.items():
        if county_name:
            watched.add(county_name)            # "Travis County"
            watched.add(county_name.upper())    # "TRAVIS COUNTY" -- categorize_entity()'s old shape
    return {w for w in watched if w}


# PX-20260824-06: registered, documented exemptions for hardcoded county-
# literal COMPARISONS that are legitimate, not stale-assumption gates.
# Same Law-3 discipline as EXEMPTIONS above (a comment in the source is
# invisible to this scanner and the next engineer running it; a registry
# entry here is not) -- keyed by (filepath, literal), file+literal grain
# rather than per-line since a line number can drift across edits while
# the literal itself is stable.
LITERAL_EXEMPTIONS = {}


def find_hardcoded_county_comparisons(root_paths=None, watched_literals=None):
    """Walks every ast.Compare node (==, !=) across the scanned .py files
    and flags any where one operand is a string Constant matching (case-
    insensitively) a real, registry-derived county-literal shape -- the
    exact `if county != "travis"` / `if county_code == "TRAVIS"` style
    gates this brief exists to catch, independent of variable name, so the
    third instance of this bug class fails loud here instead of waiting
    for a copy pass to trip over it (PX-20260824-01's own history with
    this exact class).

    Deliberately narrow to Eq/NotEq against a bare string Constant --
    `slug in COUNTY_SLUGS` / `COUNTY_SLUGS.get(slug)` (membership/lookup
    against the real registry collection) is the CORRECT pattern this
    check must not flag, and neither call shape produces an ast.Compare
    node with a string-Constant operand at all, so no special-casing is
    needed to exclude it."""
    if watched_literals is None:
        slugs, profiles = _load_county_registry_from_app_py()
        watched_literals = _build_watched_county_literals(slugs, profiles)
    watched_lower = {w.lower() for w in watched_literals}

    if root_paths is None:
        root_paths = [REPO_ROOT]
    files = []
    for p in root_paths:
        full = p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                for fn in filenames:
                    if fn.endswith(".py") and not fn.startswith(EXCLUDED_NAME_PREFIXES):
                        files.append(os.path.join(dirpath, fn))
        elif os.path.isfile(full):
            fn = os.path.basename(full)
            if fn.endswith(".py") and not fn.startswith(EXCLUDED_NAME_PREFIXES):
                files.append(full)

    findings = []
    for f in sorted(set(files)):
        relpath = os.path.relpath(f, REPO_ROOT)
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=f)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
                continue
            sides = [node.left, node.comparators[0]]
            for side in sides:
                if (isinstance(side, ast.Constant) and isinstance(side.value, str)
                        and side.value.lower() in watched_lower):
                    lineno = getattr(node, "lineno", -1)
                    key = (relpath, side.value)
                    if key in LITERAL_EXEMPTIONS:
                        entry = LITERAL_EXEMPTIONS[key]
                        findings.append(LiteralFinding(
                            relpath, lineno, side.value, _unparse(node), "EXEMPT",
                            f"EXEMPT ({entry['reason']}) -- approved by {entry['approved_by']}.",
                        ))
                    else:
                        findings.append(LiteralFinding(
                            relpath, lineno, side.value, _unparse(node), "FAIL",
                            f"Hardcoded county-literal comparison against {side.value!r} -- "
                            f"per MC-2 / PX-20260824-06, compare against a registry lookup "
                            f"(COUNTY_SLUGS/COUNTY_PROFILES membership, or the request's own "
                            f"g.county_slug/g.county_code) instead of a literal, or register a "
                            f"documented exemption in LITERAL_EXEMPTIONS if this one is "
                            f"deliberate and correct.",
                        ))
                    break
    # Law 3 parity: an EXEMPTIONS entry that matched nothing this run is a
    # loud failure, not a silent pass.
    matched = {(f.filepath, f.literal) for f in findings if f.severity == "EXEMPT"}
    for key, entry in LITERAL_EXEMPTIONS.items():
        if key not in matched:
            filepath, literal = key
            findings.append(LiteralFinding(
                filepath, 0, literal, "", "FAIL",
                f"STALE EXEMPTION: LITERAL_EXEMPTIONS entry for (file={filepath!r}, "
                f"literal={literal!r}) matched NO finding in this run (reason on file: "
                f"{entry['reason']!r}, approved by {entry['approved_by']}). Remove or "
                f"investigate, per Law 3.",
            ))
    return findings


def print_literal_report(findings):
    fails = [f for f in findings if f.severity == "FAIL"]
    exempt = [f for f in findings if f.severity == "EXEMPT"]
    print("=" * 78)
    print("verify_county_scoping.py — hardcoded county-literal comparison scan (Task 4)")
    print("=" * 78)
    print(f"Findings: {len(findings)}  ({len(fails)} FAIL, {len(exempt)} EXEMPT)")
    if exempt:
        print(f"\n{'─' * 78}\nEXEMPT ({len(exempt)}):\n{'─' * 78}")
        for f in sorted(exempt, key=lambda x: (x.filepath, x.lineno)):
            print(f"  {f.filepath}:{f.lineno} -- {f.code_snippet}")
            print(f"        {f.detail}")
    if fails:
        print(f"\n{'─' * 78}\nFAILURES ({len(fails)}):\n{'─' * 78}")
        for f in sorted(fails, key=lambda x: (x.filepath, x.lineno)):
            print(f"  {f.filepath}:{f.lineno} -- {f.code_snippet}")
            print(f"        {f.detail}")
    else:
        print("\nNo failures.")
    print("=" * 78)
    return len(fails) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="Restrict the scan to one file (repo-root-relative or absolute).")
    ap.add_argument("--table", help="Restrict findings to one registered table.")
    ap.add_argument("--literals-only", action="store_true",
                     help="PX-20260824-06 Task 4: run only the hardcoded county-literal "
                          "comparison scan (skip the SQL-writer MC-2 checks).")
    ap.add_argument("--skip-literals", action="store_true",
                     help="Run only the original SQL-writer MC-2 checks (skip Task 4's scan).")
    args = ap.parse_args()

    root_paths = [args.only] if args.only else None

    sql_ok = True
    if not args.literals_only:
        result = run_audit(root_paths=root_paths, only_table=args.table)
        sql_ok = print_report(result)

    literal_ok = True
    if not args.skip_literals:
        print()
        literal_findings = find_hardcoded_county_comparisons(root_paths=root_paths)
        literal_ok = print_literal_report(literal_findings)

    sys.exit(0 if (sql_ok and literal_ok) else 1)


if __name__ == "__main__":
    main()
