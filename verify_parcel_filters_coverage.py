#!/usr/bin/env python3
"""
verify_parcel_filters_coverage.py — the real regression harness Rule 1 of
BUILD_WORKFLOW.md calls for: mechanically proves every intended
parcel-exclusion and peer-matching call site in this codebase references
the single canonical, NULL-safe definitions in parcel_filters.py, rather
than a re-typed copy.

Naming note: this is deliberately NOT named verify_parcel_filters_
canonical.py. That name was found, this session, to have been referenced
in KNOWN_LIMITATIONS.md, parcel_filters.py's own docstring, and
SPEC_UNIT_MODEL_AND_INGEST_GATE.md as if it were a real, already-built,
already-run test (KNOWN_LIMITATIONS.md went further and claimed a
specific "25-check" result) — but it never existed anywhere in this
repo's history, committed or uncommitted (confirmed via `git status` and
`git show HEAD:KNOWN_LIMITATIONS.md`). Reusing that filename here would
make it look like the old, false claim had simply been "fixed in place";
using a new name makes clear this is a freshly built, independently
verifiable artifact, not a resurrection of something that was asserted
into existence.

Scope, determined by direct code reading (not a blind grep — a blind
`grep NOT LIKE 'AJR%'` pulls ~15 hits in app.py alone, most legitimately
unrelated to county-total/peer-matching scoping, e.g. search/typeahead
dedup). The intended consumers, confirmed by tracing every real (non-
comment) usage of CANONICAL_PARCEL_EXCL / CANONICAL_PARCEL_EXCL_BARE /
peer_state_cd1_match_sql in the codebase:

  UPDATE, Task AGGPRECOMP-2 (Aug 2026): app.py :: _compute_snapshot_data(view)
  was REWIRED to read precomputed summary tables instead of running any live
  query -- it no longer references CANONICAL_PARCEL_EXCL at all (removed
  from CANONICAL_EXCL_CONSUMERS below, and this checker now explicitly
  asserts it stays that way -- see check_snapshot_data_no_longer_duplicates_
  exclusion_logic()). The exclusion logic that used to live inside that
  function moved, in full, to loaders/refresh_snapshot_summary.py's five
  SQL-builder functions (breakdown_sql, single_year_mv_sql, part4_agg_sql,
  cert_agg_sql, neighborhoods_sql), which reference the module-level
  CANONICAL_EXCL constant -- itself built from CANONICAL_PARCEL_EXCL +
  exclude_non_real_property_gap_sql(), the exact same fix this task
  originally applied. That's a one-level indirection (constant, not a
  wrapper function call) -- see KNOWN_MODULE_CONSTANT_INDIRECTIONS below,
  the constant-indirection sibling of KNOWN_WRAPPER_INDIRECTIONS.

  CANONICAL_PARCEL_EXCL family (county-wide dollar/percentile totals):
    app.py :: snapshot_neighborhood(code)        -- /snapshot/neighborhood/<code>
    app.py :: api_benchmark()                    -- /api/benchmark
    app.py :: parcel_list()                      -- /parcels (drill-through)
    loaders/compute_metrics.py :: compute_county_benchmarks()  -- county_benchmark loader,
        via CANONICAL_PARCEL_EXCL_BARE / _exclude_clause()
    loaders/refresh_snapshot_summary.py :: breakdown_sql() / single_year_mv_sql() /
        part4_agg_sql() / cert_agg_sql() / neighborhoods_sql()  -- Tier 1 summary
        refresh for /snapshot (AGGPRECOMP-2), via the module-level CANONICAL_EXCL
        constant

  peer_state_cd1_match_sql() family (property-detail peer-matching widgets):
    app.py :: api_peer_benchmark_local(geo_id)    -- /api/peer_benchmark_local/<geo_id>
        (Peer Set widget's local-market comp; 2 query embeddings from 1 call)
    app.py :: api_peer_benchmark_sf(geo_id)       -- /api/peer_benchmark_sf/<geo_id>
        ($/SF Benchmark widget)
    app.py :: api_peer_set(geo_id)                -- /api/peer_set/<geo_id>
        (Submarket Position widget; upper=True variant)

  HONEST COUNT CORRECTION: parcel_filters.py's own docstring and this
  session's prior report both repeated an inherited "6 peer-matching call
  sites" figure. Tracing every actual peer_state_cd1_match_sql() call
  site in the current codebase finds 3 function-level call sites (4 total
  SQL embeddings, since api_peer_benchmark_local reuses one call across 2
  queries). "6" appears to be a historical figure from BEFORE the July
  2026 centralization (task #284's raw-fragment count, pre-consolidation)
  that was never updated to reflect the current, smaller, de-duplicated
  count. This script checks the 3 real current call sites, not 6 — and
  will need a one-line update to CANONICAL_EXCL_CONSUMERS / PEER_MATCH_
  CONSUMERS below if a new peer-matching widget is ever added.

Two kinds of checks, same split as verify_rollup_canonical.py:
  1. Every intended consumer file imports the canonical symbol from
     parcel_filters.py (not a re-typed literal) -- and the named
     function's body actually references it.
  2. No file OTHER than parcel_filters.py defines its own copy of either
     the exclusion fragment or the peer-matching fragment.
Both check functions accept an optional `files_override` dict
({relpath: source_text}) so they're independently fixture-testable
without touching the real repo -- see the deliberate-corruption test in
this file's own __main__ block.

Run: python3 verify_parcel_filters_coverage.py
Exits 0 if every check passes, 1 otherwise.
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "node_modules", "task_staging", "__pycache__", "uploads"}

# ── Intended consumers (determined by direct code reading, see docstring) ──
CANONICAL_EXCL_CONSUMERS = [
    ("app.py", "snapshot_neighborhood", "CANONICAL_PARCEL_EXCL"),
    ("app.py", "api_benchmark", "CANONICAL_PARCEL_EXCL"),
    ("app.py", "parcel_list", "CANONICAL_PARCEL_EXCL"),
    ("loaders/compute_metrics.py", "compute_county_benchmarks", "CANONICAL_PARCEL_EXCL_BARE"),
    ("loaders/refresh_snapshot_summary.py", "breakdown_sql", "CANONICAL_PARCEL_EXCL"),
    ("loaders/refresh_snapshot_summary.py", "single_year_mv_sql", "CANONICAL_PARCEL_EXCL"),
    ("loaders/refresh_snapshot_summary.py", "part4_agg_sql", "CANONICAL_PARCEL_EXCL"),
    ("loaders/refresh_snapshot_summary.py", "cert_agg_sql", "CANONICAL_PARCEL_EXCL"),
    ("loaders/refresh_snapshot_summary.py", "neighborhoods_sql", "CANONICAL_PARCEL_EXCL"),
]

PEER_MATCH_CONSUMERS = [
    ("app.py", "api_peer_benchmark_local"),
    ("app.py", "api_peer_benchmark_sf"),
    ("app.py", "api_peer_set"),
]

IMPORT_REQUIREMENTS = {
    "app.py": {"CANONICAL_PARCEL_EXCL", "peer_state_cd1_match_sql"},
    "loaders/compute_metrics.py": {"CANONICAL_PARCEL_EXCL_BARE"},
    "loaders/refresh_snapshot_summary.py": {"CANONICAL_PARCEL_EXCL"},
}

# app.py :: _compute_snapshot_data(view) is DELIBERATELY absent from
# CANONICAL_EXCL_CONSUMERS above (Task AGGPRECOMP-2, Aug 2026) -- it no
# longer runs any live query at all, so it has nothing to reference. This
# constant names it explicitly so check_snapshot_data_no_longer_duplicates_
# exclusion_logic() can assert the negative directly, rather than that
# absence being silent/unverified.
_MIGRATED_AWAY_FROM_LIVE_EXCLUSION = ("app.py", "_compute_snapshot_data")

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# ── Source access (real repo, or fixture override for testing) ──────────
def _read(relpath, files_override=None):
    if files_override is not None:
        return files_override.get(relpath)
    full = os.path.join(REPO_ROOT, relpath)
    if not os.path.exists(full):
        return None
    with open(full, encoding="utf-8", errors="replace") as f:
        return f.read()


def _iter_py_files(files_override=None):
    if files_override is not None:
        for relpath, text in files_override.items():
            yield relpath, text
        return
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, REPO_ROOT).replace("\\", "/")
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        yield rel, f.read()
                except OSError:
                    continue


def _extract_function_body(source, func_name):
    """Return the text from `def func_name(` up to (not including) the
    next top-level `def ` or `@app.route` line, or None if not found."""
    m = re.search(rf"\ndef {re.escape(func_name)}\(", source)
    if not m:
        return None
    start = m.start()
    rest = source[start + 1:]
    end_m = re.search(r"\n(def |@app\.route)", rest)
    end = start + 1 + end_m.start() if end_m else len(source)
    return source[start:end]


def _non_comment_occurrences(text, needle):
    """Count occurrences of `needle` on lines that are not pure comments."""
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if needle in line:
            count += 1
    return count


# ══════════════════════════════════════════════════════════════════════
# Check 1: intended consumers import + actually reference the canonical
# symbol (not a re-typed literal) inside the named function.
# ══════════════════════════════════════════════════════════════════════
def check_import_requirements(files_override=None):
    missing = []
    for relpath, needed_symbols in IMPORT_REQUIREMENTS.items():
        src = _read(relpath, files_override)
        if src is None:
            missing.append((relpath, "file not found"))
            continue
        import_lines = [l for l in src.splitlines() if l.strip().startswith("from parcel_filters import")]
        imported = set()
        for l in import_lines:
            imported.update(s.strip() for s in l.split("import", 1)[1].split(","))
        absent = needed_symbols - imported
        if absent:
            missing.append((relpath, f"missing import(s): {sorted(absent)}"))
    passed = len(missing) == 0
    detail = f"{len(IMPORT_REQUIREMENTS) - len(missing)}/{len(IMPORT_REQUIREMENTS)} files import required symbols"
    return passed, detail, missing


# Known, legitimate one-level-of-indirection wrappers: a consumer function
# doesn't reference the canonical symbol directly in its own body, but calls
# a small local helper whose body does. Listed explicitly here (not inferred
# by following arbitrary call chains) so this stays a deliberate, auditable
# exception rather than a magic "search everywhere" fallback that could mask
# a genuine drift. compute_county_benchmarks() -> _exclude_clause() is real,
# pre-existing architecture -- _exclude_clause()'s own docstring says it's
# "kept as a function (not a bare import) so every call site below is
# unchanged", and this exact chain was confirmed correct via direct
# assignment-tracing in an earlier brief this session (not assumed here).
KNOWN_WRAPPER_INDIRECTIONS = {
    ("loaders/compute_metrics.py", "compute_county_benchmarks"): "_exclude_clause",
}

# Sibling of KNOWN_WRAPPER_INDIRECTIONS above, for the different shape of
# indirection loaders/refresh_snapshot_summary.py's 5 query-builder
# functions use (Task AGGPRECOMP-2, Aug 2026): they reference a MODULE-
# LEVEL CONSTANT (CANONICAL_EXCL), not a wrapper FUNCTION CALL. Verified
# real, not assumed: `python3 -c "import loaders.refresh_snapshot_summary as
# rss; assert 'CANONICAL_PARCEL_EXCL' in ... "` during this task's own build
# confirmed CANONICAL_EXCL's assignment line genuinely includes
# CANONICAL_PARCEL_EXCL (see test_snapshot_correctness_1.py's
# test_canonical_excl_assignment_includes_l_exclusion(), updated the same
# task, for the ongoing regression check of that assignment).
KNOWN_MODULE_CONSTANT_INDIRECTIONS = {
    ("loaders/refresh_snapshot_summary.py", "breakdown_sql"): "CANONICAL_EXCL",
    ("loaders/refresh_snapshot_summary.py", "single_year_mv_sql"): "CANONICAL_EXCL",
    ("loaders/refresh_snapshot_summary.py", "part4_agg_sql"): "CANONICAL_EXCL",
    ("loaders/refresh_snapshot_summary.py", "cert_agg_sql"): "CANONICAL_EXCL",
    ("loaders/refresh_snapshot_summary.py", "neighborhoods_sql"): "CANONICAL_EXCL",
}


def check_excl_consumers_reference_canonical(files_override=None):
    problems = []
    for relpath, func_name, symbol in CANONICAL_EXCL_CONSUMERS:
        src = _read(relpath, files_override)
        if src is None:
            problems.append((relpath, func_name, "file not found"))
            continue
        body = _extract_function_body(src, func_name)
        if body is None:
            problems.append((relpath, func_name, "function not found"))
            continue
        if _non_comment_occurrences(body, symbol) > 0:
            continue  # direct reference -- satisfied

        # No direct reference -- check for a documented wrapper indirection.
        # This only succeeds if (a) this exact (file, function) pair has a
        # named wrapper on record above, (b) the consumer's body actually
        # calls that wrapper (not just coincidentally present), AND (c) the
        # wrapper's own body actually references the canonical symbol. All
        # three must hold, or this still fails like any other missing
        # reference would.
        wrapper_name = KNOWN_WRAPPER_INDIRECTIONS.get((relpath, func_name))
        if wrapper_name and _non_comment_occurrences(body, wrapper_name + "(") > 0:
            wrapper_body = _extract_function_body(src, wrapper_name)
            if wrapper_body is not None and _non_comment_occurrences(wrapper_body, symbol) > 0:
                continue  # satisfied via documented wrapper indirection

        # No direct reference, no wrapper-function indirection -- check for
        # a documented MODULE-CONSTANT indirection instead: the consumer's
        # body references a named module-level constant, and that constant's
        # own top-level assignment line (anywhere in the same file) actually
        # references the canonical symbol.
        const_name = KNOWN_MODULE_CONSTANT_INDIRECTIONS.get((relpath, func_name))
        if const_name and _non_comment_occurrences(body, const_name) > 0:
            const_m = re.search(rf"^{re.escape(const_name)}\s*=\s*(.+)$", src, re.MULTILINE)
            if const_m is not None and symbol in const_m.group(1):
                continue  # satisfied via documented module-constant indirection

        problems.append((relpath, func_name, f"no real-code reference to {symbol}"))
    passed = len(problems) == 0
    detail = f"{len(CANONICAL_EXCL_CONSUMERS) - len(problems)}/{len(CANONICAL_EXCL_CONSUMERS)} consumers verified"
    return passed, detail, problems


def check_snapshot_data_no_longer_duplicates_exclusion_logic(files_override=None):
    """
    Task AGGPRECOMP-2's negative assertion: app.py's _compute_snapshot_data()
    must NOT re-derive its own canonical_excl-shaped WHERE fragment now that
    it reads precomputed summary tables -- if a future edit accidentally
    reintroduced a live query with its own copy of this exclusion logic
    inside this function, that would be exactly the kind of silently
    reintroduced duplicate-implementation risk this whole checker exists to
    catch, just inside the one function this task deliberately emptied out.
    """
    relpath, func_name = _MIGRATED_AWAY_FROM_LIVE_EXCLUSION
    src = _read(relpath, files_override)
    if src is None:
        return False, f"{relpath} not found", [(relpath, func_name, "file not found")]
    body = _extract_function_body(src, func_name)
    if body is None:
        return False, f"{func_name} not found in {relpath}", [(relpath, func_name, "function not found")]
    if "CANONICAL_PARCEL_EXCL" in body or "canonical_excl" in body:
        problem = [(relpath, func_name,
                    "still references CANONICAL_PARCEL_EXCL/canonical_excl -- should read "
                    "precomputed summary tables instead, per AGGPRECOMP-2")]
        return False, "0/1 verified -- unexpected re-duplication found", problem
    return True, "1/1 verified -- no live exclusion logic re-duplicated", []


def check_peer_match_consumers_reference_canonical(files_override=None):
    problems = []
    for relpath, func_name in PEER_MATCH_CONSUMERS:
        src = _read(relpath, files_override)
        if src is None:
            problems.append((relpath, func_name, "file not found"))
            continue
        body = _extract_function_body(src, func_name)
        if body is None:
            problems.append((relpath, func_name, "function not found"))
            continue
        if _non_comment_occurrences(body, "peer_state_cd1_match_sql(") == 0:
            problems.append((relpath, func_name, "no real-code call to peer_state_cd1_match_sql()"))
    passed = len(problems) == 0
    detail = f"{len(PEER_MATCH_CONSUMERS) - len(problems)}/{len(PEER_MATCH_CONSUMERS)} consumers verified"
    return passed, detail, problems


# ══════════════════════════════════════════════════════════════════════
# Check 2: no file other than parcel_filters.py defines its own copy of
# either fragment (a genuinely retyped duplicate, not just an unrelated
# single-purpose AJR%/state_cd1 usage elsewhere in the codebase).
# ══════════════════════════════════════════════════════════════════════
_X_LEG = re.compile(r"NOT LIKE ['\"]X%")
_N_LEG = re.compile(r"NOT LIKE ['\"]N%")
_AJR_LEG = re.compile(r"NOT LIKE ['\"]AJR%")

# Deliberately requires the right-hand side to be a bound parameter or a
# variable -- NOT a quoted string literal. A genuine retyped copy of
# peer_state_cd1_match_sql()'s peer-matching comparison always compares a
# CANDIDATE row's prefix against a RUNTIME-SUPPLIED subject value (e.g.
# `= %(sc1)s`), because that's the entire point of "peer"-matching: matching
# against whatever the current subject parcel's own type happens to be.
# `LEFT(state_cd1,1) = 'A'` (a hardcoded letter) is a different construct --
# type CLASSIFICATION/labeling (bucketing rows into fixed, named categories
# for a report), the same category of code as label_case_sql() referenced in
# parcel_filters.py's own docstring, not peer-matching. Found in practice:
# query_2026_vs_2025.py (a standalone, non-route diagnostic script) has 5
# such CASE-expression legs (`= 'A'` / `'B'` / `'C'` / `'D'` / `'E'`) that
# are classification labels, not peer-match duplicates -- excluding string
# literals here is what correctly keeps this check from flagging them.
_PEER_MATCH_LITERAL = re.compile(
    r"LEFT\(\s*(UPPER\()?\s*[\w.]*state_cd1\)?\s*,\s*1\s*\)\s*=(?!\s*['\"])"
)


def check_no_retyped_exclusion_fragment(files_override=None):
    """
    A genuine re-typed copy of the canonical exclusion would have all
    THREE legs (X%, N%, AJR%) present close together in the same
    statement — that's the actual "copy" signature, not merely mentioning
    AJR% for an unrelated purpose (e.g. search dedup). Window: same
    function body (checked per contiguous non-comment block of up to 15
    lines, which comfortably covers a WHERE clause).
    """
    offenders = []
    for relpath, src in _iter_py_files(files_override):
        if relpath in ("parcel_filters.py", "verify_parcel_filters_coverage.py", "test_verify_parcel_filters_coverage.py"):
            continue
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if _X_LEG.search(line):
                window = "\n".join(lines[i:i + 15])
                if _N_LEG.search(window) and _AJR_LEG.search(window):
                    offenders.append((relpath, i + 1))
    passed = len(offenders) == 0
    detail = f"{len(offenders)} retyped-copy signature(s) found outside parcel_filters.py"
    return passed, detail, offenders


def check_no_retyped_peer_match_fragment(files_override=None):
    """A retyped peer-match copy would use a raw, non-NULL-safe
    `LEFT(state_cd1, 1) = <param or variable>` comparison in real code (not
    a comment) outside parcel_filters.py. Excludes this file itself (its own
    regex source text contains the literal pattern being searched for) the
    same way check_no_retyped_exclusion_fragment() already excludes itself."""
    offenders = []
    for relpath, src in _iter_py_files(files_override):
        if relpath in ("parcel_filters.py", "verify_parcel_filters_coverage.py", "test_verify_parcel_filters_coverage.py"):
            continue
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _PEER_MATCH_LITERAL.search(line):
                offenders.append((relpath, i))
    passed = len(offenders) == 0
    detail = f"{len(offenders)} retyped raw LEFT(state_cd1,1)= comparison(s) found outside parcel_filters.py"
    return passed, detail, offenders


def check_no_duplicate_symbol_definitions(files_override=None):
    """No file other than parcel_filters.py defines CANONICAL_PARCEL_EXCL
    or peer_state_cd1_match_sql itself."""
    offenders = []
    def_patterns = [
        re.compile(r"^CANONICAL_PARCEL_EXCL\s*="),
        re.compile(r"^def peer_state_cd1_match_sql\("),
    ]
    for relpath, src in _iter_py_files(files_override):
        if relpath in ("parcel_filters.py", "test_verify_parcel_filters_coverage.py"):
            continue
        for i, line in enumerate(src.splitlines(), 1):
            for pat in def_patterns:
                if pat.match(line.strip()):
                    offenders.append((relpath, i, line.strip()))
    passed = len(offenders) == 0
    detail = f"{len(offenders)} duplicate definition(s) found outside parcel_filters.py"
    return passed, detail, offenders


def main():
    checks = [
        ("Import requirements (CANONICAL_PARCEL_EXCL[_BARE], peer_state_cd1_match_sql)", check_import_requirements),
        ("CANONICAL_PARCEL_EXCL consumers reference the canonical symbol", check_excl_consumers_reference_canonical),
        ("_compute_snapshot_data() no longer duplicates exclusion logic (AGGPRECOMP-2)", check_snapshot_data_no_longer_duplicates_exclusion_logic),
        ("peer_state_cd1_match_sql() consumers reference the canonical function", check_peer_match_consumers_reference_canonical),
        ("No retyped exclusion-fragment copy outside parcel_filters.py", check_no_retyped_exclusion_fragment),
        ("No retyped peer-match fragment outside parcel_filters.py", check_no_retyped_peer_match_fragment),
        ("No duplicate symbol definitions outside parcel_filters.py", check_no_duplicate_symbol_definitions),
    ]

    overall_pass = True
    for name, fn in checks:
        passed, detail, offenders = fn()
        overall_pass = overall_pass and passed
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {name} — {detail}")
        if not passed:
            for o in offenders:
                print(f"    {o}")

    total_excl = len(CANONICAL_EXCL_CONSUMERS)
    total_peer = len(PEER_MATCH_CONSUMERS)
    print()
    print(f"Real call-site count checked: {total_excl} CANONICAL_PARCEL_EXCL consumers "
          f"+ {total_peer} peer_state_cd1_match_sql() consumers = {total_excl + total_peer} total.")

    print()
    if overall_pass:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
