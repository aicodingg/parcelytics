#!/usr/bin/env python3
"""
verify_index_coverage.py — POST-PARTITION-INCIDENT-1-AUDIT.

Real, general-purpose query-vs-index coverage auditor. Built in direct
response to tonight's real production incident: migrate_county_partitioning.py's
already-run migration made county_code the LEADING column of several tables'
primary keys (parcel, parcel_tax_year, parcel_metrics, tax_delinquent,
prop_unit, prop_unit_tax_year, county_tax_rate, group_stats, and the three
snapshot_* summary tables). Any real query filtering on a table's OLD
single-column identifier alone (geo_id, prop_id, entity_code) lost its fast
lookup path, because a composite index/PK led by county_code cannot
efficiently serve a query that never mentions county_code at all.
property_detail() timed out in production on exactly this pattern; 4
reactive indexes were added directly to production as an urgent hotfix
(see schema.sql's own "Real, urgent hotfix (Aug 8, 2026)" comment, commit
2b33283/3ccfc44). This script exists to answer, systematically and for
EVERY real query in the codebase (not just the 4 tables already patched):
does a real, live index actually cover this access pattern, or not.

Deliberately general, per Fable's explicit instruction (see the brief) --
NOT a geo_id-specific checker. The exact same structural risk applies to
ANY composite-keyed table: prop_id alone against prop_unit/
prop_unit_tax_year (now county_code-leading), entity_code alone against
county_tax_rate (same), and in principle any future composite-keyed table
this codebase adds. The checker has no hardcoded knowledge of "geo_id" or
"county_code" as special column names anywhere in its matching logic --
it is a pure column-list-vs-index-list leading-prefix checker. It happens
to be most interesting on the county-partitioned tables today because
that's where the real, live incident occurred, but it audits every table
any extracted query touches.

── Three real stages ────────────────────────────────────────────────────────

1. EXTRACTION (extract_calls_from_source): pulls every real SQL statement
   passed to query()/query_no_nestloop()/*.execute() out of real .py source,
   via Python's own `ast` module (stdlib -- confirmed no network access in
   this sandbox to install sqlparse or any other real SQL-parsing library;
   `pip install sqlparse --break-system-packages` was attempted directly
   before writing this and failed with a 403 from the sandbox's own proxy).
   Handles the real shapes this codebase's call sites actually use,
   confirmed via grep before writing this, not assumed:
     - plain literal:      query("SELECT * FROM parcel WHERE geo_id = %s", ...)
     - triple-quoted:      query(a triple-quoted multi-line SQL literal)
     - f-strings:          query(a triple-quoted f-string with {fragments})
       -- the static parts are kept verbatim; each interpolated {expr} is
       kept as a literal {expr} marker in the reconstructed text (via
       ast.unparse on the FormattedValue's own expression), NOT silently
       dropped or guessed at -- this is what lets stage 2 below correctly
       flag "this WHERE clause is dynamically built, not statically
       resolvable" instead of either crashing or silently mis-parsing it.
     - string concatenation (plain "a" + "b" or adjacent literals -- Python's
       own ast already merges adjacent plain-string literals at parse time).
     - a same-file variable holding the SQL text (sql = <a triple-quoted
       f-string>; then cur.execute(sql, ...)) -- resolved via a first-pass, best-effort
       (NOT full data-flow analysis) collection of simple `name = <string
       expr>` assignments, last-assignment-wins. Genuinely unresolvable
       arguments (a function call, an f-string built from another
       function's return value, etc.) are reported as UNRESOLVED, not
       silently skipped.

2. PARSING (parse_sql_shape): real, hand-rolled, paren-depth-aware SQL
   structure parsing -- NOT naive `"geo_id" in sql_text` substring matching.
   Extracts the FROM/JOIN table+alias map, then walks the WHERE clause and
   every JOIN...ON clause, splitting each on TOP-LEVEL (paren-depth-aware)
   AND boundaries, and pulling the real left-hand column reference out of
   each simple predicate (col = %s, col IN (...), col IS NULL, a.col =
   b.col, etc.), resolving `alias.column` back to the real table name via
   the alias map. This is the "real SQL parsing" the brief calls for, built
   against the actual subset of SQL this codebase's query-calling
   conventions produce (SELECT/UPDATE/DELETE, standard JOIN...ON,
   %s/%(name)s placeholders) -- it does not claim to be a general-purpose
   SQL grammar, and says so via UNRESOLVED/DYNAMIC statuses rather than
   guessing on shapes it doesn't recognize.

3. CROSS-REFERENCE (audit_source_tree + best_index_match): for each
   (table, filter_columns) pair found, checks every real index on that
   table (either from a live `pg_indexes` query against production, or --
   OFFLINE/TESTING ONLY, see the loud warning in load_table_indexes_from_
   schema_sql()'s own docstring -- parsed from schema.sql's CREATE INDEX/
   PRIMARY KEY text) for the LONGEST leading prefix of that index's own
   column list that is fully contained in the query's filter-column set.
   A result of 0 across every real index on that table is the real,
   concrete failure mode this incident was: the query cannot use any index
   as a fast lookup path at all, and Postgres falls back to a sequential
   (or, worse, a composite-index leading-column-mismatched) scan.

── Sandbox-vs-live disclosure ───────────────────────────────────────────────
This sandbox has no live Postgres connection (same disclosure as every
other tool built this project). Stages 1 and 2 (extraction + parsing) need
no database at all and run for real, directly against this repo's real
source files -- their output in this task's final report is real, not a
fixture. Stage 3 (the actual cross-reference against LIVE pg_indexes) is
proven correct here only via fixture tests (see test_verify_index_
coverage.py) and via a schema.sql-based offline approximation for
demonstration -- and that approximation is KNOWN, CONFIRMED STALE for
exactly the tables this audit cares most about (see the warning below).
Diego needs to run `python3 verify_index_coverage.py --index-source live`
against production himself for the real, authoritative answer to "did
tonight's 4 indexes cover everything" -- see this task's final report for
the exact command and how to read its output.

── CRITICAL: schema.sql's CREATE TABLE PRIMARY KEY text is STALE ──────────
Confirmed by direct inspection while building this script: schema.sql's
CREATE TABLE bodies for parcel/parcel_tax_year/parcel_metrics/
tax_delinquent/prop_unit/prop_unit_tax_year/county_tax_rate/group_stats/
the three snapshot_* summary tables still show their PRE-migration,
single/no-county_code-leading PRIMARY KEY shape (e.g. parcel_tax_year:
"PRIMARY KEY (geo_id, tax_year)", not the real, live "PRIMARY KEY
(county_code, geo_id, tax_year)"). migrate_county_partitioning.py's real
migration was applied directly to production; schema.sql's CREATE TABLE
statements were never updated to match (only the 4 reactive CREATE INDEX
statements at the bottom were appended -- confirmed, this is the same
"schema.sql has drifted from live reality" gap this project's other tools
have already had to work around, e.g. migrate_county_partitioning.py's own
choice to introspect information_schema live rather than trust schema.sql).
This means `--index-source schema-sql` UNDERSTATES real index coverage for
every one of those tables' real composite primary keys -- it is offered
here ONLY so this script's OWN logic can be exercised end-to-end without a
live connection (fixture tests, `--index-source schema-sql` demo runs), and
it prints a loud warning identifying exactly which tables it knows its own
PK data is stale for (cross-checked against migrate_county_partitioning.
py's own real TABLE_SPECS, not a hardcoded duplicate list) every time it's
used. Never treat a schema-sql-mode "no gap found" as a real answer for
those tables.

Usage:
    cd ~/Desktop/Claude\\ Files/parcel_app
    # Real, authoritative run (needs Diego's own live DB access):
    python3 verify_index_coverage.py --index-source live

    # Offline demonstration / re-running this script's own logic without a
    # live connection (see the CRITICAL warning above -- NOT a substitute
    # for the live run on the tables that matter most):
    python3 verify_index_coverage.py --index-source schema-sql

    # Narrow to one file while iterating:
    python3 verify_index_coverage.py --index-source schema-sql --only app.py

── Stage 4 — MISSING_TENANT_SCOPE (added after the real PX-20260828-16 ────
   follow-up incident; a SEPARATE, additive check -- Stages 1-3 above are
   UNCHANGED) ─────────────────────────────────────────────────────────────
Stages 1-3 above answer one question, and answer it correctly: "can
Postgres serve this query efficiently?" Stage 4 answers a completely
different question that Stages 1-3 were never designed to ask: "does this
query's own filter set actually restrict results to one county's data at
all?" A query can be a real, honest COVERED verdict under Stage 3 and
STILL be a live, real tenant-isolation bug -- those are not in tension,
they are answers to two different questions.

The real reason this needed its own stage, not a tweak to Stage 3: six
real production bugs (PX-20260828-12 Category 7) sat invisible to this
very tool for a real, concrete reason -- NOT "the scanner didn't scan
app.py" (it did), but because schema.sql's own Aug 8 2026 hotfix had
already added idx_parcel_geo_id_only ON parcel (geo_id) as a legitimate,
deliberate transitional secondary index (see that CREATE INDEX's own
schema.sql comment). That index is real and correct for what it was built
for. But its mere existence meant Stage 3's leading-prefix match against
it returned k=1 -- a real, honest "this query CAN use an index" -- for
queries that filtered on geo_id alone, with no county_code anywhere,
direct or via JOIN. A well-intentioned, correctly-built PERFORMANCE index
made a CORRECTNESS bug performance-invisible: the query got fast AND
wrong at the same time, and Stage 3 only ever had the vocabulary to notice
"fast." Stage 4 exists because "COVERED" and "correctly tenant-scoped" are
two different claims, and this codebase already had one real incident
where treating them as the same claim cost six live bugs a place to hide.

Scope: reads only. verify_county_scoping.py's own rule 3(d) already
audits every real UPDATE/DELETE writer's WHERE-clause county scoping,
via its own broader (not call-site-scoped) AST extraction across the
whole repo. Stage 4 deliberately excludes UPDATE/DELETE/INSERT statements
(see _is_write_statement) so it doesn't silently re-derive a narrower
duplicate of that already-existing, more thorough check -- and so its
"reads vs writes" framing (see the exemption-sharing note below) stays
honest rather than becoming two overlapping half-answers to the same
question.

Exemptions are SHARED with verify_county_scoping.EXEMPTIONS (one registry,
not two that can drift -- see that module's own comment on the
"applies_to" field). An entry is only ever honored by Stage 4 if it is
explicitly tagged {"read"} in applies_to. As of this build, a full,
explicit audit of all 7 real entries in that registry (done before this
stage was written, not assumed) found every one of them write-only --
each justifies skipping an INSERT's ON CONFLICT target or an UPDATE/
DELETE's WHERE-clause scoping for reasons specific to that write's own
transactional mechanics (a same-transaction whole-table rebuild, a NOT
NULL constraint failing loud at write time, a one-time incident-remediation
script) -- none of which establishes that a SELECT reading the same table
is safe to leave unscoped. So Stage 4 currently treats the shared registry
as contributing zero exemptions in practice; this is confirmed, not a gap
this stage papers over.

── Known false-positive source: subquery predicates (inherited from Stage 2, ──
   NOT something Stage 4 introduces) ─────────────────────────────────────────
Confirmed via a real run against this repo while building this stage:
parse_sql_shape() (Stage 2, UNCHANGED) walks the outer statement's top-level
WHERE/JOIN...ON clauses, but a table referenced only inside a parenthesized
subquery (e.g. `... AND ctr.entity_code IN (SELECT entity_code FROM
tax_billing_entity WHERE geo_id = %s AND county_code = %s)`) gets
REGISTERED into tables_touched (Stage 1/2's table-name regex scans the
whole raw text) without its own subquery-local WHERE predicates ever being
walked -- so that table can show up here with an empty filter_columns set
even when its real, inner WHERE clause genuinely does filter by
county_code. This is a real, disclosed accuracy limit, not a Stage-4-only
bug: Stage 3 above has carried the exact same blind spot the whole time
(a subquery-scoped table can equally show up as NO_INDEX_DATA/NO_COVERAGE
there for the same structural reason). A MISSING_TENANT_SCOPE finding on a
table that's ONLY ever referenced inside a subquery in that call site
should be hand-verified against the real SQL text before treating it as a
confirmed gap -- same "flag for human review, not full SQL grammar"
posture this whole file already discloses for Stage 2/3.
"""
import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════
# Stage 1 — Extraction
# ═══════════════════════════════════════════════════════════════════════════

SQL_CALL_FUNC_NAMES = {"query", "query_no_nestloop"}
SQL_EXECUTE_METHOD_NAMES = {"execute"}


@dataclass
class ExtractedSQL:
    filepath: str
    lineno: int
    call_name: str          # "query", "cur.execute", ...
    sql_text: str            # reconstructed text; "{expr}" markers where dynamic
    resolution: str          # "literal" | "fstring_static" | "fstring_dynamic" |
                              # "concat" | "variable" | "unresolved"


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "<?>"


def _resolve_string_node(node, assignments):
    """Best-effort resolution of an AST expression node to SQL text.
    Returns (text, resolution_kind). See module docstring, Stage 1, for the
    real shapes this handles."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "literal"

    if isinstance(node, ast.JoinedStr):  # f-string
        parts = []
        any_dynamic = False
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                any_dynamic = True
                parts.append("{" + _unparse(v.value) + "}")
            else:
                any_dynamic = True
                parts.append("{?}")
        return "".join(parts), ("fstring_dynamic" if any_dynamic else "fstring_static")

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

    return "{<unresolved-expr>}", "unresolved"


def _collect_simple_string_assignments(tree):
    """First pass: collect `name = <string-resolvable-expr>` assignments
    anywhere in the file, keyed by variable name, LAST ASSIGNMENT WINS.
    Deliberately NOT real data-flow analysis (no scope/branch awareness) --
    just enough to resolve the real `sql = f\"\"\"...\"\"\"` /
    `cur.execute(sql, ...)` pattern this codebase's loaders actually use in
    places, confirmed via grep before writing this. Good enough for an
    audit tool whose job is to flag things for human review, not to be a
    silent, fully-automated ground truth."""
    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            text, kind = _resolve_string_node(node.value, assignments)
            if kind not in ("unresolved", "variable_unresolved"):
                assignments[node.targets[0].id] = text
    return assignments


def extract_calls_from_source(source_text, filepath):
    """Real AST walk of one file's source -- returns every ExtractedSQL call
    site found (query()/query_no_nestloop()/*.execute()). Raises SyntaxError
    up to the caller if the file doesn't parse (caller decides how to
    report that -- an audit tool should never silently swallow a real
    parse failure)."""
    tree = ast.parse(source_text, filename=filepath)
    assignments = _collect_simple_string_assignments(tree)
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = None
        if isinstance(node.func, ast.Name) and node.func.id in SQL_CALL_FUNC_NAMES:
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute) and node.func.attr in SQL_EXECUTE_METHOD_NAMES:
            call_name = _unparse(node.func)
        if call_name is None:
            continue
        if not node.args:
            continue
        sql_text, kind = _resolve_string_node(node.args[0], assignments)
        results.append(ExtractedSQL(filepath, node.lineno, call_name, sql_text, kind))
    return results


def extract_calls_from_tree(root_paths):
    """root_paths: list of files/directories (repo-root-relative or
    absolute). Directories are walked for *.py files (non-recursive-import,
    real filesystem walk). Returns (list[ExtractedSQL], list[(file, error)])
    -- parse errors are collected, not raised, so one bad file doesn't kill
    the whole audit; they're reported explicitly in the final output."""
    files = []
    for p in root_paths:
        full = p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)
        if os.path.isdir(full):
            for dirpath, _dirnames, filenames in os.walk(full):
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(os.path.join(dirpath, fn))
        elif os.path.isfile(full):
            files.append(full)

    extracted = []
    errors = []
    for f in sorted(set(files)):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            extracted.extend(extract_calls_from_source(source, os.path.relpath(f, REPO_ROOT)))
        except SyntaxError as e:
            errors.append((os.path.relpath(f, REPO_ROOT), str(e)))
    return extracted, errors


# ═══════════════════════════════════════════════════════════════════════════
# Stage 2 — Real SQL-shape parsing (hand-rolled, paren-depth-aware)
# ═══════════════════════════════════════════════════════════════════════════

_CLAUSE_BOUNDARY = re.compile(
    r'\b(WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION|RETURNING|'
    r'LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|FULL\s+JOIN|JOIN)\b',
    re.IGNORECASE,
)
_AND_SPLIT = re.compile(r'\bAND\b', re.IGNORECASE)
_PREDICATE_RE = re.compile(
    # BUG FIX (found live, during the POST-PARTITION-INCIDENT-1-AUDIT build):
    # a trailing \b applied to the WHOLE operator group is wrong for the
    # symbol operators (=, !=, <>, <=, >=, <, >) -- \b requires a word-char/
    # non-word-char transition, and a symbol char followed by whitespace,
    # '%', a quote, or another symbol is a non-word/non-word transition, so
    # \b silently FAILED TO MATCH immediately after nearly every real "col =
    # %s" / "col = value" predicate in this codebase (confirmed directly:
    # the old pattern matched "col=5" but not "col = %s", "col = 5", or
    # "col=%s" -- i.e. it failed on every parameterized query this codebase
    # actually writes). That made _extract_predicate_refs() return [] for
    # nearly all '='-based predicates -- including the incident's OWN exact
    # shape (WHERE geo_id = %s) -- so those queries silently vanished from
    # the findings list instead of being checked, rather than being
    # reported as gaps. \b is only meaningful (and only applied here) to
    # the KEYWORD operators (IS, IN, LIKE, NOT ...), where it prevents a
    # partial-word match (e.g. "IN" inside "INSTANCE"); symbol operators
    # get no \b since one isn't needed or well-defined for them.
    r'^\(*\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*\.\s*([A-Za-z_][A-Za-z0-9_]*))?\s*'
    r'(!=|<>|<=|>=|=|<|>|IS\s+NOT\b|NOT\s+IN\b|NOT\s+LIKE\b|IN\b|IS\b|LIKE\b)',
    re.IGNORECASE,
)
_FROM_RE = re.compile(r'\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?', re.IGNORECASE)
_JOIN_RE = re.compile(
    r'\b(?:LEFT|RIGHT|INNER|FULL)?\s*JOIN\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
    r'(?:(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?\s+ON\s+',
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(r'\bUPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\b', re.IGNORECASE)
_DELETE_RE = re.compile(r'\bDELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)\b', re.IGNORECASE)
_SQL_KEYWORDS_AS_ALIAS_BLOCKLIST = {
    "where", "on", "set", "values", "select", "and", "or", "as", "join",
    "left", "right", "inner", "full", "group", "order", "by", "having",
    "limit", "offset", "returning", "union", "not", "in", "is", "like",
    "null", "true", "false",
}


def _find_matching_paren_end(text, open_idx):
    """text[open_idx] must be '(' -- returns index of its matching ')'."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _clause_end(text, start):
    """Find the end of a clause starting at `start`: the next occurrence of
    a boundary keyword at paren-depth 0 relative to `start`, or end of
    string. Real paren-depth tracking, not a naive regex search -- a
    boundary keyword appearing inside a subquery must not end the OUTER
    clause early."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            if depth == 0:
                return i
            depth -= 1
            i += 1
            continue
        if depth == 0:
            m = _CLAUSE_BOUNDARY.match(text, i)
            if m:
                return i
        i += 1
    return n


def _split_top_level_and(text):
    """Split `text` on top-level (paren-depth 0) AND boundaries."""
    parts = []
    depth = 0
    last = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0:
            m = _AND_SPLIT.match(text, i)
            if m:
                parts.append(text[last:i])
                i = m.end()
                last = i
                continue
        i += 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


_POINT_LOOKUP_OPERATORS = {"=", "IN"}  # see parse_sql_shape's docstring note on this choice


def _extract_predicate_refs(predicate_text):
    """Returns a list of (alias_or_none, column, is_point_lookup) refs found
    in a single top-level predicate -- usually one (the LHS), but for
    `a.col = b.col2` style JOIN...ON predicates, returns BOTH sides (both
    are real filter/join columns on their respective tables).
    is_point_lookup is True only for bare '=' and 'IN' (see
    _POINT_LOOKUP_OPERATORS) -- range/inequality/LIKE/IS-NULL-style
    predicates are still recorded (informational_columns) but are NOT
    point-equality lookups, so they are not the same class of risk this
    incident was about and are excluded from the leading-prefix coverage
    check itself (see parse_sql_shape)."""
    refs = []
    m = _PREDICATE_RE.match(predicate_text)
    if not m:
        return refs
    ident1, ident2, op = m.group(1), m.group(2), re.sub(r'\s+', ' ', m.group(3).upper())
    is_point = op in _POINT_LOOKUP_OPERATORS
    if ident2:
        refs.append((ident1.lower(), ident2.lower(), is_point))
    else:
        refs.append((None, ident1.lower(), is_point))
    # RHS: only interesting if it's itself an alias.column reference (a
    # JOIN...ON condition comparing two columns, e.g. "pty.geo_id = p.geo_id").
    rhs = predicate_text[m.end():].strip()
    rhs_m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b', rhs)
    if rhs_m:
        refs.append((rhs_m.group(1).lower(), rhs_m.group(2).lower(), is_point))
    return refs


@dataclass
class ParsedShape:
    tables_touched: set = field(default_factory=set)         # real table names
    filter_columns: dict = field(default_factory=dict)        # table -> set(cols), '='/'IN' only
    informational_columns: dict = field(default_factory=dict)  # table -> set(cols), range/other ops
    unresolved_clauses: list = field(default_factory=list)    # human-readable notes
    alias_map: dict = field(default_factory=dict)


def parse_sql_shape(sql_text):
    """Real, hand-rolled parse of one SQL statement's text (as reconstructed
    by Stage 1 -- may contain literal `{expr}` markers for dynamically-
    interpolated fragments). See module docstring, Stage 2."""
    shape = ParsedShape()
    alias_map = {}  # alias/bare-table-name -> real table name

    def register(table, alias):
        real = table.lower()
        alias_map[real] = real
        if alias and alias.lower() not in _SQL_KEYWORDS_AS_ALIAS_BLOCKLIST:
            alias_map[alias.lower()] = real
        shape.tables_touched.add(real)

    # UPDATE / DELETE FROM target table (no alias in this codebase's real
    # call sites -- confirmed via grep of actual UPDATE/DELETE statements).
    m = _UPDATE_RE.search(sql_text)
    if m:
        register(m.group(1), None)
    m = _DELETE_RE.search(sql_text)
    if m:
        register(m.group(1), None)

    # FROM <table> [alias]
    for m in _FROM_RE.finditer(sql_text):
        register(m.group(1), m.group(2))

    # JOIN <table> [alias] ON <condition> -- condition span found via real
    # paren-depth-aware boundary search, not just "next comma".
    for m in _JOIN_RE.finditer(sql_text):
        register(m.group(1), m.group(2))
        on_start = m.end()
        on_end = _clause_end(sql_text, on_start)
        on_text = sql_text[on_start:on_end]
        if "{" in on_text:
            shape.unresolved_clauses.append(f"JOIN...ON for {m.group(1)} contains a dynamic fragment: {on_text.strip()[:120]}")
        for pred in _split_top_level_and(on_text):
            for alias, col, is_point in _extract_predicate_refs(pred):
                table = alias_map.get(alias) if alias else None
                if table is None and alias is None:
                    # Unaliased bare column in an ON clause -- ambiguous in a
                    # multi-table query; real, honest limitation, not guessed.
                    if len(shape.tables_touched) == 1:
                        table = next(iter(shape.tables_touched))
                    else:
                        shape.unresolved_clauses.append(
                            f"ambiguous unaliased column '{col}' in JOIN...ON (multiple tables in scope)")
                        continue
                if table is None:
                    continue
                target = shape.filter_columns if is_point else shape.informational_columns
                target.setdefault(table, set()).add(col)

    # WHERE clause
    where_m = re.search(r'\bWHERE\b', sql_text, re.IGNORECASE)
    if where_m:
        where_start = where_m.end()
        where_end = _clause_end(sql_text, where_start)
        where_text = sql_text[where_start:where_end]
        if re.fullmatch(r'\s*\{[^{}]*\}\s*', where_text):
            shape.unresolved_clauses.append(f"WHERE clause is entirely dynamic, not statically parseable: {where_text.strip()}")
        else:
            if "{" in where_text:
                shape.unresolved_clauses.append(f"WHERE clause contains a dynamic fragment: {where_text.strip()[:160]}")
            for pred in _split_top_level_and(where_text):
                if pred.strip().startswith("{") and pred.strip().endswith("}"):
                    continue  # whole predicate is an opaque dynamic fragment, already noted above
                for alias, col, is_point in _extract_predicate_refs(pred):
                    table = alias_map.get(alias) if alias else None
                    if table is None and alias is None:
                        if len(shape.tables_touched) == 1:
                            table = next(iter(shape.tables_touched))
                        else:
                            shape.unresolved_clauses.append(
                                f"ambiguous unaliased column '{col}' in WHERE (multiple tables in scope)")
                            continue
                    if table is None:
                        continue
                    target = shape.filter_columns if is_point else shape.informational_columns
                    target.setdefault(table, set()).add(col)

    shape.alias_map = alias_map
    return shape


# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 — Real index definitions + leading-prefix cross-reference
# ═══════════════════════════════════════════════════════════════════════════

_CREATE_INDEX_RE = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+'
    r'ON\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:USING\s+\w+\s*)?\(([^)]*)\)',
    re.IGNORECASE,
)
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', re.IGNORECASE,
)
_INLINE_PK_COLUMN_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s+[A-Za-z0-9_(),\s]*?\bPRIMARY\s+KEY\b', re.IGNORECASE | re.MULTILINE,
)
_TABLE_PK_CLAUSE_RE = re.compile(r'PRIMARY\s+KEY\s*\(([^)]*)\)', re.IGNORECASE)


def _split_cols(raw):
    cols = []
    for c in raw.split(","):
        c = c.strip()
        c = re.sub(r'\s+(ASC|DESC|NULLS\s+FIRST|NULLS\s+LAST)\s*$', '', c, flags=re.IGNORECASE)
        if c:
            cols.append(c.lower())
    return cols


def parse_index_defs(sql_text):
    """Parses real `CREATE INDEX ... ON table (cols)` statements out of
    arbitrary SQL text -- shared by both schema.sql offline mode AND live
    pg_indexes.indexdef parsing (Postgres emits indexdef in exactly this
    shape), so both modes exercise the SAME parsing code, not two
    independently-maintained copies."""
    result = {}
    for m in _CREATE_INDEX_RE.finditer(sql_text):
        index_name, table, cols_raw = m.group(1), m.group(2).lower(), m.group(3)
        result.setdefault(table, []).append((index_name, _split_cols(cols_raw)))
    return result


def parse_create_table_primary_keys(sql_text):
    """Parses real `PRIMARY KEY (...)` (table-level) and inline single-
    column `col TYPE ... PRIMARY KEY` declarations out of CREATE TABLE
    bodies in schema.sql-style DDL text. Returns dict table -> [(pseudo_
    index_name, [cols])] (a PK is a real, valid index for leading-prefix
    purposes even with no explicit CREATE INDEX statement)."""
    result = {}
    for m in _CREATE_TABLE_RE.finditer(sql_text):
        table = m.group(1).lower()
        body_start = m.end() - 1  # position of the opening '('
        body_end = _find_matching_paren_end(sql_text, body_start)
        body = sql_text[body_start + 1:body_end]

        pk_m = _TABLE_PK_CLAUSE_RE.search(body)
        if pk_m:
            cols = _split_cols(pk_m.group(1))
            result.setdefault(table, []).append((f"{table}_pkey", cols))
            continue
        inline_m = _INLINE_PK_COLUMN_RE.search(body)
        if inline_m:
            result.setdefault(table, []).append((f"{table}_pkey", [inline_m.group(1).lower()]))
    return result


# Tables whose migrate_county_partitioning.py TABLE_SPECS entry means their
# REAL, LIVE primary key changed shape -- used only to print an honest,
# targeted staleness warning when schema.sql is used as the index source
# (see module docstring's CRITICAL warning). Loaded from the real module,
# not hand-duplicated, so this list can't silently drift from the real
# migration script's own authoritative TABLE_SPECS.
def _load_composite_pk_migrated_tables():
    try:
        sys.path.insert(0, REPO_ROOT)
        import migrate_county_partitioning as mcp
        return sorted(s["name"] for s in mcp.TABLE_SPECS)
    except Exception:
        return []


def load_table_indexes_from_schema_sql(schema_path):
    """OFFLINE / TESTING-ONLY index source. See module docstring's CRITICAL
    warning -- schema.sql's CREATE TABLE PRIMARY KEY text is CONFIRMED STALE
    for every composite_pk-mode table migrate_county_partitioning.py has
    already migrated live. This function still parses schema.sql faithfully
    (real text in, real parse out) -- it does not silently correct or
    second-guess the stale data -- but prints an explicit warning naming
    exactly which tables' results are therefore unreliable, so this mode is
    never mistaken for the real, live answer."""
    with open(schema_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    indexes = parse_index_defs(text)
    pks = parse_create_table_primary_keys(text)
    for table, pk_list in pks.items():
        indexes.setdefault(table, []).extend(pk_list)

    stale_tables = sorted(set(_load_composite_pk_migrated_tables()) & set(indexes.keys()))
    if stale_tables:
        print("=" * 78, file=sys.stderr)
        print("WARNING: --index-source schema-sql is being used. schema.sql's own", file=sys.stderr)
        print("CREATE TABLE PRIMARY KEY text is CONFIRMED STALE for these tables", file=sys.stderr)
        print("(their real, live primary key was changed by migrate_county_", file=sys.stderr)
        print("partitioning.py's already-run migration; schema.sql's CREATE TABLE", file=sys.stderr)
        print("body was never updated to match). Results below UNDERSTATE real,", file=sys.stderr)
        print("live index coverage for these tables -- this is NOT a substitute", file=sys.stderr)
        print("for --index-source live:", file=sys.stderr)
        for t in stale_tables:
            print(f"    - {t}", file=sys.stderr)
        print("=" * 78, file=sys.stderr)
    return indexes


def load_table_indexes_from_live_db(conn):
    """The real, authoritative index source. Read-only: SELECT ... FROM
    pg_indexes WHERE schemaname = 'public' -- no writes, safe against
    production. Reuses parse_index_defs() on the concatenated indexdef
    text (Postgres's own indexdef output is itself valid `CREATE INDEX ...`
    DDL text), so live and offline modes share one parsing code path."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        rows = cur.fetchall()
    combined_ddl = ";\n".join(r[2] for r in rows) + ";"
    return parse_index_defs(combined_ddl)


def leading_prefix_length(filter_cols, index_cols):
    """Longest k such that index_cols[0..k-1] are ALL present in
    filter_cols. This is the real criterion: a composite index/PK can only
    be used as an efficient lookup path starting from its FIRST column --
    if the first column isn't filtered at all (k=0), the index provides
    zero benefit for this query, regardless of whether a LATER column in
    the index happens to be filtered (exactly tonight's real incident:
    filtering geo_id alone against a (county_code, geo_id, tax_year)
    index gives k=0, not partial credit)."""
    k = 0
    for col in index_cols:
        if col in filter_cols:
            k += 1
        else:
            break
    return k


def best_index_match(filter_cols, indexes):
    """indexes: list of (index_name, [cols]). Returns (best_k, name, cols)
    -- (0, None, None) if no index gives any leading-column benefit at all
    (or the table has no indexes in the given index source)."""
    best = (0, None, None)
    for name, cols in indexes:
        k = leading_prefix_length(filter_cols, cols)
        if k > best[0]:
            best = (k, name, cols)
    return best


# ═══════════════════════════════════════════════════════════════════════════
# Stage 4 — MISSING_TENANT_SCOPE (separate correctness check; see module
# docstring's "Stage 4" section for why this exists and why it is kept
# independent of Stages 1-3's COVERED/NO_COVERAGE performance verdicts).
# ═══════════════════════════════════════════════════════════════════════════

_TENANT_SCOPE_COLUMN = "county_code"

# Test/fixture files are walked by Stage 1's extraction (verify_index_
# coverage.py's own extract_calls_from_tree does NOT exclude them -- that's
# an existing Stage 1 characteristic this ticket does not touch), so a
# fixture's own synthetic cur.execute("SELECT ... FROM parcel ...") calls
# show up in `extracted` alongside real production call sites. Confirmed
# live during this stage's build: without this filter, Stage 4 flagged
# loaders/test_pir_loaders.py's own fixture SQL as MISSING_TENANT_SCOPE --
# real code that never runs against a real database. Matches verify_
# county_scoping.py's own EXCLUDED_NAME_PREFIXES convention for
# distinguishing production writers from test fixtures.
_TEST_FILE_PREFIXES = ("test_", "validate_", "verify_")


def _is_test_file(filepath):
    return os.path.basename(filepath).startswith(_TEST_FILE_PREFIXES)

# A statement is excluded from Stage 4 if it's a real write (INSERT/UPDATE/
# DELETE) -- see module docstring's "Scope: reads only" note. Reuses the
# SAME _UPDATE_RE/_DELETE_RE Stage 2 already defines (one definition, not a
# second copy that could drift from Stage 2's own UPDATE/DELETE handling).
_INSERT_INTO_RE = re.compile(r'\bINSERT\s+INTO\b', re.IGNORECASE)


def _is_write_statement(sql_text):
    """True for a real INSERT/UPDATE/DELETE. Deliberately a plain keyword
    search here (not tied to parse_sql_shape's own table-registration
    logic) -- Stage 4 needs to make this call BEFORE deciding whether it's
    even worth calling parse_sql_shape on a given item, and a statement
    that's ambiguous or doesn't match any of the three write keywords is
    treated as a read by default (see the docstring on audit_tenant_scope
    for why: this check exists for tenant-isolation safety, and silently
    excluding an unrecognized statement shape from Stage 4 entirely would
    be the wrong direction to err in)."""
    return bool(_UPDATE_RE.search(sql_text) or _DELETE_RE.search(sql_text) or _INSERT_INTO_RE.search(sql_text))


def _load_shared_tenant_exemptions():
    """The exemptions registry lives in ONE place -- verify_county_scoping.
    EXEMPTIONS -- not duplicated here (see module docstring's "Exemptions
    are SHARED" note). Loaded fresh on each call, not cached at import
    time, matching this file's own existing _load_composite_pk_migrated_
    tables() convention -- so a test that monkeypatches verify_county_
    scoping.EXEMPTIONS doesn't also have to know about a second, cached
    copy living here. Defensive: returns {} (zero exemptions honored, the
    SAFE default for a tenant-isolation check) if verify_county_scoping.py
    can't be imported for any reason, rather than letting an import error
    here silently take down Stage 4 entirely."""
    try:
        sys.path.insert(0, REPO_ROOT)
        import verify_county_scoping as vcs
        return vcs.EXEMPTIONS
    except Exception:
        return {}


@dataclass
class TenantScopeFinding:
    filepath: str
    lineno: int
    call_name: str
    table: str
    status: str          # "MISSING_TENANT_SCOPE" | "EXEMPT"
    filter_columns: list
    detail: str = ""


def audit_tenant_scope(extracted, required_tables=None, shared_exemptions=None):
    """Stage 4. For every real, non-write statement that touches a table
    migrate_county_partitioning.py's TABLE_SPECS says is composite_pk-
    migrated (county_code-leading), checks whether county_code appears in
    that table's own point-equality/IN filter set -- directly (a WHERE
    predicate) or transitively (a JOIN...ON alias.county_code =
    other_alias.county_code predicate). No separate transitive-join logic
    is needed here: parse_sql_shape's own _extract_predicate_refs already
    records BOTH sides of an `alias.col = alias.col` JOIN...ON predicate
    into their respective tables' filter_columns (see Stage 2) -- so a
    join that DOES carry a county_code equality condition already leaves
    county_code sitting directly in filter_columns for both tables
    involved, with nothing extra for this stage to compute. A join that's
    MISSING that condition (tonight's real incident shape) simply never
    puts county_code in the joined table's filter_columns at all, which
    is exactly the gap this check looks for.

    Returns a list[TenantScopeFinding], entirely separate from Stage 3's
    findings -- callers must not merge the two lists (see print_report).
    """
    if required_tables is None:
        required_tables = set(_load_composite_pk_migrated_tables())
    if shared_exemptions is None:
        shared_exemptions = _load_shared_tenant_exemptions()

    findings = []
    matched_exemption_keys = set()
    for item in extracted:
        if _is_test_file(item.filepath):
            continue
        if _is_write_statement(item.sql_text):
            continue
        try:
            shape = parse_sql_shape(item.sql_text)
        except Exception:
            continue  # a real parse failure here was already reported by Stage 2/3's own pass; not duplicated

        for table in sorted(shape.tables_touched):
            if table not in required_tables:
                continue
            cols = shape.filter_columns.get(table, set())
            if _TENANT_SCOPE_COLUMN in cols:
                continue  # directly or transitively scoped -- see docstring above

            info_cols = shape.informational_columns.get(table, set())
            note = ""
            if _TENANT_SCOPE_COLUMN in info_cols:
                note = (" (county_code DOES appear against this table, but only via a "
                        "non-equality operator -- verify by hand whether it actually "
                        "restricts to one tenant; this check only counts a bare "
                        "equality/IN predicate as real scoping)")

            key = (item.filepath, table, "SELECT")
            entry = shared_exemptions.get(key)
            if entry and "read" in entry.get("applies_to", {"write"}):
                matched_exemption_keys.add(key)
                findings.append(TenantScopeFinding(
                    item.filepath, item.lineno, item.call_name, table, "EXEMPT",
                    sorted(cols),
                    f"EXEMPT ({entry['reason']}) -- approved by {entry['approved_by']}.",
                ))
            else:
                findings.append(TenantScopeFinding(
                    item.filepath, item.lineno, item.call_name, table, "MISSING_TENANT_SCOPE",
                    sorted(cols),
                    f"{table} is a composite_pk-migrated, county_code-leading table (per "
                    f"migrate_county_partitioning.py's real TABLE_SPECS), but this query's "
                    f"filter/JOIN set for {table} is {sorted(cols)!r} -- no county_code, "
                    f"direct or via JOIN.{note} A real, live index may still make this query "
                    f"FAST (see this same call site's own Stage 3 verdict, if any, above) -- "
                    f"that is a completely separate question from whether it is CORRECT. Add "
                    f"a county_code predicate, or register a documented, read-path-tagged "
                    f"({{'read'}} in applies_to) exemption in verify_county_scoping.EXEMPTIONS.",
                ))

    # Law 3 mirror (see verify_county_scoping.py's own _apply_exemptions()):
    # an exemption entry tagged {"read"} that matches ZERO Stage 4 findings
    # on this run is itself a loud failure, not a silent pass -- ONLY this
    # stage can ever match a {"read"}-tagged entry (verify_county_scoping.py's
    # own run structurally can't, since its findings are always INSERT/
    # UPDATE/DELETE -- see that file's matching "write" in applies_to gate),
    # so this stage owns the staleness duty for the read side of the shared
    # registry.
    for key, entry in shared_exemptions.items():
        if "read" not in entry.get("applies_to", {"write"}):
            continue
        if key in matched_exemption_keys:
            continue
        filepath, table, stmt_kind = key
        findings.append(TenantScopeFinding(
            filepath, 0, "exempt-stale", table, "STALE_EXEMPTION", [],
            f"STALE EXEMPTION: this registered, read-tagged EXEMPTIONS entry for "
            f"(file={filepath!r}, table={table!r}, stmt_kind={stmt_kind!r}) matched "
            f"NO Stage 4 finding in this run (reason on file: {entry['reason']!r}, "
            f"approved by {entry['approved_by']}). Either the finding it excused is "
            f"genuinely gone (remove this registry entry) or something else changed "
            f"(investigate before removing).",
        ))
    return findings


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Finding:
    filepath: str
    lineno: int
    call_name: str
    table: str
    filter_columns: list
    status: str                # NO_COVERAGE | COVERED | NO_INDEX_DATA | NO_INDEXES_ON_TABLE
    best_index_name: str = None
    best_index_cols: list = None
    best_prefix_len: int = 0
    all_table_indexes: list = field(default_factory=list)  # for NO_COVERAGE display context
    informational_columns: list = field(default_factory=list)  # range/other-op cols, same table


def audit_extracted(extracted, table_indexes):
    """Stage 2 + 3 combined: parses every ExtractedSQL and cross-references
    against table_indexes. Returns (findings: list[Finding], dynamic_notes:
    list[(file, line, note)]) -- findings only cover tables that were
    actually resolved to a real filter-column set; fully-dynamic/unresolved
    clauses are reported separately in dynamic_notes rather than silently
    treated as "no filter columns" (which would look identical to a
    genuinely unfiltered full-table scan and understate real risk).

    Distinguishes three real "we found no coverage" shapes, since they mean
    very different things to a human reading the report:
      - NO_INDEX_DATA: this table has no entry at all in the given index
        source (a real name-resolution gap in the audit itself, or a table
        the index source genuinely doesn't know about -- report and
        investigate, don't conflate with "no indexes exist").
      - NO_INDEXES_ON_TABLE: the index source DOES know this table, and
        confirms it has ZERO real indexes at all.
      - NO_COVERAGE: the table has one or more REAL indexes, but the
        query's point-lookup filter columns don't include the first
        column of ANY of them -- this is tonight's real incident's exact
        failure shape, and the only one of the three that means "there IS
        an index here, it's just the wrong shape for this query."
    """
    findings = []
    dynamic_notes = []
    for item in extracted:
        try:
            shape = parse_sql_shape(item.sql_text)
        except Exception as e:  # a real parse failure on a real statement -- report, don't hide
            dynamic_notes.append((item.filepath, item.lineno, f"PARSE ERROR: {e}"))
            continue

        for note in shape.unresolved_clauses:
            dynamic_notes.append((item.filepath, item.lineno, note))

        for table, cols in shape.filter_columns.items():
            info_cols = sorted(shape.informational_columns.get(table, set()))
            if table not in table_indexes:
                findings.append(Finding(item.filepath, item.lineno, item.call_name, table,
                                         sorted(cols), "NO_INDEX_DATA", informational_columns=info_cols))
                continue
            table_idx_list = table_indexes[table]
            if not table_idx_list:
                findings.append(Finding(item.filepath, item.lineno, item.call_name, table,
                                         sorted(cols), "NO_INDEXES_ON_TABLE", informational_columns=info_cols))
                continue
            k, name, idx_cols = best_index_match(cols, table_idx_list)
            status = "COVERED" if k >= 1 else "NO_COVERAGE"
            findings.append(Finding(item.filepath, item.lineno, item.call_name, table,
                                     sorted(cols), status, name, idx_cols, k,
                                     all_table_indexes=table_idx_list, informational_columns=info_cols))
    return findings, dynamic_notes


DEFAULT_SOURCE_PATHS = ["app.py", "loaders", "tax_logic"]


def run_audit(source_paths=None, index_source="schema-sql", conn=None, only_file=None):
    source_paths = source_paths or DEFAULT_SOURCE_PATHS
    extracted, parse_errors = extract_calls_from_tree(source_paths)
    if only_file:
        extracted = [e for e in extracted if only_file in e.filepath]

    if index_source == "live":
        if conn is None:
            raise ValueError("index_source='live' requires a real conn (see loaders/db.py's get_conn())")
        table_indexes = load_table_indexes_from_live_db(conn)
    else:
        table_indexes = load_table_indexes_from_schema_sql(os.path.join(REPO_ROOT, "schema.sql"))

    findings, dynamic_notes = audit_extracted(extracted, table_indexes)
    # Stage 4 -- independent of index_source/conn entirely (it's a
    # correctness check, not a performance one); computed from the same
    # `extracted` list (respects --only) but never merged into `findings`
    # above (see module docstring's Stage 4 section).
    tenant_scope_findings = audit_tenant_scope(extracted)
    return {
        "extracted_count": len(extracted),
        "parse_errors": parse_errors,
        "findings": findings,
        "dynamic_notes": dynamic_notes,
        "table_indexes": table_indexes,
        "tenant_scope_findings": tenant_scope_findings,
    }


def _split_covered_by_pk_staleness(covered_findings, index_source):
    """A COVERED verdict is only as trustworthy as the index it matched. If
    the matching index is the table's own inline-schema.sql PRIMARY KEY
    (name == "<table>_pkey", the synthetic entry parse_create_table_
    primary_keys() adds -- see load_table_indexes_from_schema_sql) AND that
    table is one of the composite_pk-migrated, PK-stale tables, then this
    "COVERED" is not real -- it's schema.sql's STALE, pre-migration PK text
    matching by coincidence (e.g. prop_unit's stale PK is bare (prop_id),
    matching a WHERE prop_id = %s query; the REAL, live PK is now
    (county_code, prop_id), which that same query would NOT satisfy).
    Real, standalone secondary indexes (idx_foo, not "<table>_pkey") are
    NOT affected by the PK migration and remain trustworthy even in
    schema-sql mode. Only applies when index_source == 'schema-sql' --
    --index-source live has no such ambiguity (pg_indexes IS the live
    truth, PK or not)."""
    if index_source != "schema-sql":
        return covered_findings, []
    stale_tables = set(_load_composite_pk_migrated_tables())
    trustworthy, unconfirmed = [], []
    for f in covered_findings:
        if f.table in stale_tables and f.best_index_name == f"{f.table}_pkey":
            unconfirmed.append(f)
        else:
            trustworthy.append(f)
    return trustworthy, unconfirmed


def print_report(result, index_source="schema-sql"):
    gaps = [f for f in result["findings"] if f.status == "NO_COVERAGE"]
    no_data = [f for f in result["findings"] if f.status == "NO_INDEX_DATA"]
    no_indexes = [f for f in result["findings"] if f.status == "NO_INDEXES_ON_TABLE"]
    covered_all = [f for f in result["findings"] if f.status == "COVERED"]
    covered, unconfirmed_pk = _split_covered_by_pk_staleness(covered_all, index_source)

    print(f"Extracted {result['extracted_count']} real SQL call sites.")
    if result["parse_errors"]:
        print(f"\n{len(result['parse_errors'])} file(s) failed to parse (Python syntax error -- reported, not silently skipped):")
        for f, e in result["parse_errors"]:
            print(f"  {f}: {e}")

    print(f"\n{'='*78}\nREAL GAPS FOUND: {len(gaps)} (table HAS real indexes, but none start with "
          f"a column this query filters/joins by equality -- tonight's exact incident shape)\n{'='*78}")
    for f in gaps:
        print(f"  {f.filepath}:{f.lineno}  [{f.call_name}]  table={f.table}  filter_columns(=/IN)={f.filter_columns}")
        if f.informational_columns:
            print(f"      also filtered (range/other operator, not counted toward coverage): {f.informational_columns}")
        print(f"      real indexes that exist on {f.table}, none of which start with these columns:")
        for name, cols in f.all_table_indexes:
            print(f"        - {name}: {cols}")

    if unconfirmed_pk:
        print(f"\n{'='*78}\nUNCONFIRMED (relies on schema.sql's STALE primary key -- NOT a real "
              f"secondary index; this is exactly the incident's failure shape and needs\n"
              f"--index-source live to resolve, one way or the other): {len(unconfirmed_pk)}\n{'='*78}")
        for f in unconfirmed_pk:
            print(f"  {f.filepath}:{f.lineno}  [{f.call_name}]  table={f.table}  filter_columns(=/IN)={f.filter_columns}")
            print(f"      only matched via schema.sql's stale inline PK '{f.best_index_name}' {f.best_index_cols} -- "
                  f"the real, live PK now leads with county_code (see migrate_county_partitioning.py's "
                  f"TABLE_SPECS); no OTHER real index on {f.table} covers this filter set in schema.sql.")

    if no_indexes:
        print(f"\n{len(no_indexes)} finding(s) reference a table CONFIRMED to have ZERO real indexes at all:")
        for f in no_indexes:
            print(f"  {f.filepath}:{f.lineno}  table={f.table}  filter_columns={f.filter_columns}")

    if no_data:
        print(f"\n{len(no_data)} finding(s) reference a table with NO entry in the given index source "
              f"(this audit tool doesn't know about this table's indexes at all -- investigate, don't assume covered):")
        for f in no_data:
            print(f"  {f.filepath}:{f.lineno}  table={f.table}  filter_columns={f.filter_columns}")

    if result["dynamic_notes"]:
        print(f"\n{len(result['dynamic_notes'])} dynamically-built clause(s) could not be statically parsed -- MANUAL REVIEW NEEDED:")
        for fp, ln, note in result["dynamic_notes"]:
            print(f"  {fp}:{ln}  {note}")

    print(f"\n{len(covered)} query/table pair(s) confirmed covered by a real leading-prefix index match.")

    # Stage 4 -- printed in its OWN section, never merged with the
    # NO_COVERAGE/UNCONFIRMED/COVERED verdicts above. A query can appear in
    # BOTH the "confirmed covered" count above AND the MISSING_TENANT_SCOPE
    # list below at the same time -- that is not a contradiction, it's the
    # entire reason this stage exists (see module docstring).
    tenant_findings = result.get("tenant_scope_findings", [])
    tenant_gaps = [f for f in tenant_findings if f.status == "MISSING_TENANT_SCOPE"]
    tenant_exempt = [f for f in tenant_findings if f.status == "EXEMPT"]
    print(f"\n{'='*78}\nMISSING_TENANT_SCOPE — Stage 4, a SEPARATE correctness check (a query "
          f"CAN be COVERED above and MISSING_TENANT_SCOPE here at the same time -- see module "
          f"docstring for why idx_parcel_geo_id_only made this real bug class performance-"
          f"invisible): {len(tenant_gaps)}\n{'='*78}")
    for f in tenant_gaps:
        print(f"  {f.filepath}:{f.lineno}  [{f.call_name}]  table={f.table}  filter_columns={f.filter_columns}")
        print(f"      {f.detail}")
    if not tenant_gaps:
        print("  No missing-tenant-scope findings.")
    if tenant_exempt:
        print(f"\n{len(tenant_exempt)} MISSING_TENANT_SCOPE finding(s) covered by a registered, "
              f"read-path-tagged ({{'read'}} in applies_to) exemption in verify_county_scoping."
              f"EXEMPTIONS:")
        for f in tenant_exempt:
            print(f"  {f.filepath}:{f.lineno}  table={f.table}  -- {f.detail}")
    tenant_stale = [f for f in tenant_findings if f.status == "STALE_EXEMPTION"]
    if tenant_stale:
        print(f"\n{'='*78}\nSTALE READ-SIDE EXEMPTION(S) — Law 3 mirror check: {len(tenant_stale)}\n"
              f"{'='*78}")
        print("  These {'read'}-tagged verify_county_scoping.EXEMPTIONS entries matched "
              "ZERO Stage 4 findings on this run. Per Law 3 (same discipline "
              "verify_county_scoping.py applies to its own write-side entries), an "
              "exemption that no longer matches anything is itself a finding -- either "
              "the underlying code changed (subquery got rewritten, filter added/removed) "
              "and the exemption is now dead weight, or it never should have matched this "
              "file/table in the first place. Investigate and remove or correct the entry.")
        for f in tenant_stale:
            print(f"  {f.filepath}  table={f.table}  -- {f.detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-source", choices=["schema-sql", "live"], default="schema-sql",
                     help="'live' (real, authoritative -- needs a real DB connection) or "
                          "'schema-sql' (OFFLINE/TESTING ONLY -- see module docstring's CRITICAL warning)")
    ap.add_argument("--only", default=None, help="substring filter on extracted file paths, for iterating on one file")
    args = ap.parse_args()

    conn = None
    if args.index_source == "live":
        sys.path.insert(0, REPO_ROOT)
        from loaders.db import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT inet_server_addr()")
            addr = cur.fetchone()[0]
        print(f"Target DB: {addr}  — confirm this is the environment you intend.\n")

    result = run_audit(index_source=args.index_source, conn=conn, only_file=args.only)
    print_report(result, index_source=args.index_source)

    if conn:
        conn.close()

    gaps = [f for f in result["findings"] if f.status == "NO_COVERAGE"]
    covered_all = [f for f in result["findings"] if f.status == "COVERED"]
    _, unconfirmed_pk = _split_covered_by_pk_staleness(covered_all, args.index_source)
    tenant_gaps = [f for f in result.get("tenant_scope_findings", []) if f.status == "MISSING_TENANT_SCOPE"]
    tenant_stale = [f for f in result.get("tenant_scope_findings", []) if f.status == "STALE_EXEMPTION"]
    # Exit nonzero on a confirmed gap, an unconfirmed stale-PK reliance, a
    # MISSING_TENANT_SCOPE finding, OR a stale read-side exemption. Unlike
    # unconfirmed_pk (only meaningful in schema-sql mode -- --index-source
    # live has no such ambiguity), tenant_gaps and tenant_stale hard-fail
    # UNCONDITIONALLY, in both modes: they're correctness/hygiene questions
    # that have nothing to do with which index source was used to answer
    # the performance question. tenant_stale is included per Law 3: a
    # dangling exemption that no longer matches anything is exactly the
    # kind of silently-rotting suppression that turned into a real
    # incident once already (see module docstring) -- it must fail loud,
    # not sit unnoticed.
    sys.exit(1 if (gaps or unconfirmed_pk or tenant_gaps or tenant_stale) else 0)


if __name__ == "__main__":
    main()
