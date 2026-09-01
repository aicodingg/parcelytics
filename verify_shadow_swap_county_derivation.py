#!/usr/bin/env python3
"""
verify_shadow_swap_county_derivation.py — PX-20260831-02 Task 2: shadow-swap
lint (recurrence guard).

This is the THIRD real instance of the exact same bug class in this
codebase: a per-county aggregate gets computed with the county_code
EXTERNALLY STAMPED onto every row from a caller-supplied parameter, instead
of DERIVED from each row's own source data.

  1. compute_county_benchmarks() -- upsert-based variant of the same class,
     fixed earlier (see refresh_group_stats.py's own module docstring for
     the cross-reference; that fix is a different write architecture --
     per-row upsert, not a shadow-swap -- so it is out of THIS tool's
     structural detection scope, which is specifically the shadow-swap
     shape named below).
  2. refresh_group_stats.py's build_shadow() -- PX-20260828-13 (commits
     8f9ebdc + 5bfe005) -- the shadow-swap variant, fixed by deriving
     county_code per-row from `parcel.county_code` and carrying it through
     REFRESH_GROUP_STATS_SQL's GROUP BY.
  3. refresh_snapshot_summary.py's build_shadow() -- PX-20260831-02 Task 1,
     this same brief -- fixed identically, five separate query builders
     each deriving county_code per-row and carrying it through their own
     GROUP BY / GROUPING SETS clause.

Three strikes is a pattern, not a coincidence -- every shadow-swap-shaped
loader in this codebase is structurally tempted toward the same shortcut
(stamp one external value on every row of a full-table-replace, since the
old single-county world made that indistinguishable from "derive it
correctly"). This tool exists so a FOURTH instance is caught by CI-time
static analysis before it ever reaches a live run, rather than found one at
a time after the fact -- the same motivating discipline as MC-2's
verify_county_scoping.py (see that file's own module docstring for the
sibling incident list on the upsert/plain-INSERT side of this same
problem).

── Structural detection, not a filename list ───────────────────────────────
Per this brief's explicit instruction: a module is classified as
"performing a shadow-swap" by looking for the actual DROP/RENAME SQL shape
in its own source text, not by asking "is this refresh_group_stats.py or
refresh_snapshot_summary.py" -- so a THIRD or FOURTH future file that
reinvents this same pattern (rather than being a plugin registered here) is
still caught. Concretely, a module is shadow_swap=True iff its resolved SQL
string constants contain BOTH:
  (a) a `DROP TABLE IF EXISTS <ident>_shadow` statement, AND
  (b) an `ALTER TABLE <ident>_shadow RENAME TO <ident>` (or equivalently,
      any `RENAME TO` statement whose surrounding text also mentions
      `_shadow`) -- the atomic-swap half of the pattern.
Both refresh_group_stats.py and refresh_snapshot_summary.py build these
statements as f-strings parameterized by a `for tbl in (...)` loop variable
(`f"DROP TABLE IF EXISTS {tbl}_shadow"`) rather than a literal table name --
_resolve_string_node (reused unchanged from verify_county_scoping.py)
renders an unresolved Name reference as the literal placeholder text
`{tbl}_shadow`, which the regexes below match on directly (they do not
require a real, resolved identifier -- only the literal `_shadow` suffix
and the DROP/RENAME keyword shape), so this detection works identically
whether the table name is a hardcoded literal or a loop/parameter variable.

── The two checks (PM's Task 2 (a) and (b), verbatim) ──────────────────────
For every module classified as shadow_swap:
  (a) NO function definition that (i) has a parameter literally named
      `county_code` on its signature AND (ii) whose own body contains an
      `INSERT INTO <ident>_shadow` statement. This is exactly the retired
      PARTITION-2-FIX-1 shape: build_shadow(conn, batch_id, county_code=...)
      writing that one external value onto every row. A function that
      merely READS a county_code parameter without ever touching a
      `_shadow` table (assert_group_stats_fresh(conn, county_code=...),
      assert_snapshot_summary_fresh(conn, county_code=...) -- both
      genuinely kept, per-county STALENESS checks, not write-path) does not
      trip this check, because "does this function's body reference a
      _shadow table" is the actual write-path signal, not merely "does this
      module happen to have some shadow tables somewhere."
  (b) EVERY SELECT statement in the module that has its own GROUP BY /
      GROUPING SETS clause must carry `county_code` INSIDE that specific
      clause (not merely anywhere in the SQL string -- see
      `_group_by_clauses()` below for the exact substring extraction). This
      is the derivation proof: a query that aggregates parcels without
      grouping by county_code is, structurally, exactly the shape that
      forces "blend every county together, then stamp one label on the
      blend" -- the root defect all three real incidents share.

── What this does NOT prove ────────────────────────────────────────────────
This is a static, source-text-and-AST check. It does not run any SQL, and
it cannot prove a GROUP BY clause that DOES mention county_code is wired
correctly end-to-end (that's test_refresh_snapshot_summary.py's /
test_refresh_group_stats.py's own behavioral FakeConn fixtures, plus
Diego's live EXPLAIN proof -- see loaders/
explain_snapshot_summary_county_derivation.py). It also only classifies a
module as shadow_swap via the specific DROP-then-RENAME textual shape
above; a hypothetical FOURTH write architecture that replaces a whole table
some other way (e.g. TRUNCATE + COPY) would not be caught by this tool and
would need its own structural detector, named explicitly rather than
silently assumed covered.

Usage:
    python3 verify_shadow_swap_county_derivation.py
    python3 verify_shadow_swap_county_derivation.py --only loaders/refresh_snapshot_summary.py
"""
import argparse
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_county_scoping import (
    REPO_ROOT, EXCLUDED_DIRS, EXCLUDED_NAME_PREFIXES,
    _resolve_string_node, _collect_simple_string_assignments,
)

# ── Registered exemptions (same convention as verify_county_scoping.py's
# own EXEMPTIONS registry) -- kept empty today. A future legitimate case
# would be documented here with a reason, never silently skipped. ──────────
EXEMPTIONS = {
    # (relative_file_path, function_name): "reason"
}

DROP_SHADOW_RE = re.compile(r'\bDROP\s+TABLE\s+IF\s+EXISTS\s+\S*_shadow\b', re.IGNORECASE)
RENAME_TO_RE = re.compile(r'\bRENAME\s+TO\s+\S+', re.IGNORECASE)
INSERT_SHADOW_RE = re.compile(r'\bINSERT\s+INTO\s+(\S*_shadow)\b', re.IGNORECASE)
# The compound-alternative ("GROUP BY GROUPING SETS") is listed FIRST so
# re.finditer's leftmost-alternative-wins scan consumes this codebase's real
# `GROUP BY GROUPING SETS (...)` shape (breakdown_sql()/single_year_mv_sql())
# as ONE clause boundary, not two adjacent ones -- the naive
# `GROUP BY` | `GROUPING SETS` alternation used to split that single real
# clause into a spurious empty "GROUP BY " fragment immediately followed by
# the real "GROUPING SETS (...)" content, which made the empty fragment
# fail check (b) even though the actual clause correctly carries
# county_code -- caught by running this tool against the real, already-fixed
# refresh_snapshot_summary.py before trusting it as a recurrence guard.
GROUP_KEYWORD_RE = re.compile(
    r'\bGROUP\s+BY\s+GROUPING\s+SETS\b|\bGROUP\s+BY\b|\bGROUPING\s+SETS\b|\bHAVING\b|\bORDER\s+BY\b',
    re.IGNORECASE,
)
SELECT_RE = re.compile(r'\bSELECT\b', re.IGNORECASE)


def _iter_py_files(root_paths):
    """Same walk/exclusion convention as verify_county_scoping.py's own
    extract_statements_from_tree() -- test_*/validate_*/verify_* files and
    EXCLUDED_DIRS skipped, matching this codebase's established split
    between production writers and test/audit-tool fixtures."""
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
    return sorted(set(files))


def _all_resolved_strings(tree, assignments):
    """Every string this file could plausibly execute as SQL: every literal
    string constant, every f-string (resolved via _resolve_string_node,
    which substitutes in known module/function-level constant assignments
    for bare Name references and renders anything else, including loop
    variables, as a literal `{name}` placeholder text -- see this module's
    own docstring for why that's sufficient for the DROP/RENAME/INSERT
    regexes below, which only care about the literal `_shadow` suffix, not
    a fully-resolved identifier)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            text, _kind = _resolve_string_node(node, assignments)
            out.append(text)
    return out


def _is_shadow_swap_module(all_strings):
    """Structural classification -- see module docstring's detection
    section. Both a DROP...{x}_shadow and a RENAME TO statement whose
    surrounding text also mentions _shadow must be present somewhere in the
    file for it to count."""
    has_drop_shadow = any(DROP_SHADOW_RE.search(s) for s in all_strings)
    has_shadow_rename = any(
        RENAME_TO_RE.search(s) and "_shadow" in s.lower() for s in all_strings
    )
    return has_drop_shadow and has_shadow_rename


def _group_by_clauses(text):
    """Yields each GROUP BY / GROUPING SETS clause substring in `text`,
    from its own keyword to the next GROUP BY/GROUPING SETS/HAVING/ORDER BY
    keyword (or end of string). A single SQL string can legitimately
    contain more than one real grouping clause (e.g.
    refresh_group_stats.py's REFRESH_GROUP_STATS_SQL: one inside the
    tbe_sum CTE, one on the outer SELECT) -- each is checked independently,
    since a bug in EITHER one is a real bug, not just the outer one."""
    matches = list(GROUP_KEYWORD_RE.finditer(text))
    clauses = []
    for i, m in enumerate(matches):
        if not m.group(0).upper().lstrip().startswith(("GROUP", "GROUPING")):
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        clauses.append(text[start:end])
    return clauses


def _select_strings_with_grouping(all_strings):
    """Filters to just the strings that are real SELECT statements with at
    least one GROUP BY / GROUPING SETS clause -- Check (b)'s actual
    population. A SELECT with no aggregation at all is not this check's
    target (see module docstring's "What this does NOT prove")."""
    out = []
    for s in all_strings:
        if not SELECT_RE.search(s):
            continue
        clauses = _group_by_clauses(s)
        if clauses:
            out.append((s, clauses))
    return out


def _function_defs(tree):
    """All FunctionDef/AsyncFunctionDef nodes anywhere in the module
    (top-level and nested) -- nested closures like build_shadow()'s own
    `_log()` helper are harmless to include (they never have a county_code
    parameter AND write to a _shadow table simultaneously in this
    codebase's real functions), and including them costs nothing."""
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _param_names(func_node):
    args = func_node.args
    names = set()
    for a in (args.posonlyargs + args.args + args.kwonlyargs):
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def analyze_module(tree, label):
    """Runs both checks against one already-parsed module AST. `label` is
    used only for reporting (a real file path, or a synthetic fixture
    name). Returns a dict:
        is_shadow_swap: bool
        county_param_violations: [{"function": name, "lineno": int}]
        group_by_violations: [{"sql_snippet": str, "clause": str}]
    Exposed as a standalone function (rather than folded into a
    file-reading main loop) specifically so test_verify_shadow_swap_county_
    derivation.py can feed it synthetic ast.parse()'d source text directly
    -- no temp files needed for the fixture tests."""
    assignments = _collect_simple_string_assignments(tree)
    all_strings = _all_resolved_strings(tree, assignments)

    is_shadow_swap = _is_shadow_swap_module(all_strings)
    result = {"is_shadow_swap": is_shadow_swap, "county_param_violations": [], "group_by_violations": []}
    if not is_shadow_swap:
        return result

    # Check (a)
    for func in _function_defs(tree):
        if EXEMPTIONS.get((label, func.name)):
            continue
        if "county_code" not in _param_names(func):
            continue
        func_assignments = assignments  # module-wide constants remain visible inside the function
        func_strings = _all_resolved_strings(func, func_assignments)
        writes_to_shadow = any(INSERT_SHADOW_RE.search(s) for s in func_strings)
        if writes_to_shadow:
            result["county_param_violations"].append({"function": func.name, "lineno": func.lineno})

    # Check (b)
    for sql_text, clauses in _select_strings_with_grouping(all_strings):
        for clause in clauses:
            if "county_code" not in clause.lower():
                result["group_by_violations"].append({
                    "sql_snippet": sql_text[:160].replace("\n", " ") + ("..." if len(sql_text) > 160 else ""),
                    "clause": clause[:160].replace("\n", " ") + ("..." if len(clause) > 160 else ""),
                })

    return result


def run_audit(root_paths=None):
    if root_paths is None:
        root_paths = [REPO_ROOT]
    files = _iter_py_files(root_paths)

    findings = {}
    errors = []
    for f in files:
        rel = os.path.relpath(f, REPO_ROOT)
        try:
            with open(f, "r", encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=f)
        except SyntaxError as e:
            errors.append((rel, str(e)))
            continue
        result = analyze_module(tree, rel)
        if result["is_shadow_swap"]:
            findings[rel] = result
    return findings, errors


def print_report(findings, errors):
    ok = True
    print("── verify_shadow_swap_county_derivation.py ──────────────────────")
    if errors:
        ok = False
        print(f"{len(errors)} file(s) failed to parse (treated as failures):")
        for rel, msg in errors:
            print(f"  {rel}: {msg}")
        print()

    if not findings:
        print("No shadow-swap-shaped modules found (0 modules matched the DROP+RENAME structural pattern).")
        return ok

    for rel, result in sorted(findings.items()):
        violations = result["county_param_violations"] or result["group_by_violations"]
        status = "FAIL" if violations else "PASS"
        print(f"[{status}] {rel}  (shadow-swap module)")
        for v in result["county_param_violations"]:
            ok = False
            print(f"    FAIL (a): {v['function']}() at line {v['lineno']} accepts a `county_code` "
                  f"parameter AND writes to a _shadow table -- externally-stamped county, not derived.")
        for v in result["group_by_violations"]:
            ok = False
            print(f"    FAIL (b): a SELECT's grouping clause does not carry county_code:")
            print(f"        clause:  {v['clause']}")
            print(f"        in SQL:  {v['sql_snippet']}")
        if not violations:
            print(f"    OK: no function stamps an external county_code onto a _shadow write, "
                  f"and every grouping clause carries county_code.")
    print()
    print("ALL SHADOW-SWAP MODULES PASS" if ok else "SHADOW-SWAP COUNTY-DERIVATION LINT FAILED")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="Restrict the scan to one file (repo-root-relative or absolute).")
    args = ap.parse_args()
    root_paths = [args.only] if args.only else None
    findings, errors = run_audit(root_paths=root_paths)
    ok = print_report(findings, errors)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
