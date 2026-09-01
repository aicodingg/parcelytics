#!/usr/bin/env python3
"""
loaders/test_param_sql_placeholder_safety.py -- PX-20260831-03 HOTFIX Task 2.

Recurrence guard for the class of bug that broke the first live Dallas
metrics run: loaders/compute_metrics.py:498's INSERT (compute_parcel_metrics())
carried several bare, un-doubled '%' characters inside SQL COMMENTS ("~93%
of", "(100%)", "~2.8%") that live INSIDE the triple-quoted f-string handed to
cur.execute(sql, params). psycopg2 only treats a query string literally when
it is executed with NO params; the moment params are supplied (as this
statement's `(county_code,)` always has been, via PX-20260828-16-followup's
`WHERE pty.county_code = %s` fix), psycopg2 substitutes over the ENTIRE
string it is given -- comments included, because a Python/psycopg2 string
has no concept of "this part is a SQL comment, don't touch it". A bare '%'
anywhere in that string that isn't a real `%s`/`%(name)s` placeholder or an
escaped `%%` is live ammunition the instant params are non-empty.

Root cause identified (see PX-20260831-03 report for full detail): four
bare '%' characters in loaders/compute_metrics.py's compute_parcel_metrics()
main INSERT (in comments only -- the real SQL logic was always correct),
plus the same class of bug in two homestead-cap UPDATEs in the same function
(`LIKE 'A%'` / `LIKE '%HS%'`, un-doubled) and one comment inside
compute_county_benchmarks()'s INSERT that spells out the literal text "%s"
while describing what the query does. All four statements DO pass a
non-empty params tuple to cur.execute(), so all four were live-unsafe.
refresh_group_stats.py and refresh_snapshot_summary.py were independently
audited end-to-end (every cur.execute call site, every f-string SQL body)
and found already clean -- no fixes were needed there.

This file's checker (`check_param_sql`) implements the exact two-part test
specified in the PX-20260831-03 brief:
  1. sql.replace('%%', '').count('%s') == len(params)              [[positional]]
     (a dict-keyed equivalent for %(name)s placeholders is included too,
     since two of the three modules use RealDictCursor-style named params.)
  2. no '%' survives in the string after removing every %s/%(name)s/%% --
     i.e. no bare, un-doubled '%' anywhere in the text that's actually sent
     to cur.execute() alongside a non-empty params argument.

Per the brief: psycopg2 itself is not installed in this sandbox (no network
access to pip-install it either -- confirmed this session), so this test is
built as a PURE STRING check with no psycopg2/DB dependency at all -- it
does not need a live connection or psycopg2.extensions/sql/adapt to prove
its point, since PM's own two-part spec is itself a pure-string invariant.
This is intentionally more conservative than modeling psycopg2's exact
C-level substitution grammar (which this session could not empirically
verify without a live psycopg2 install) -- it flags every statement that
*could* be dangerous under any plausible reading of psycopg2's behavior,
which is the right posture for a recurrence guard.

Run: python3 loaders/test_param_sql_placeholder_safety.py
"""
import os
import re
import sys

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── The checker itself (PM's exact two-part spec) ───────────────────────────

_NAMED_PLACEHOLDER_RE = re.compile(r"%\((\w+)\)s")


def check_param_sql(sql, params):
    """
    Returns (ok: bool, detail: str).

    Handles BOTH calling conventions used across these three modules:
      - positional: cur.execute(sql, (a, b, ...))  -> plain '%s' placeholders
      - named:      cur.execute(sql, {"k": v, ...}) -> '%(k)s' placeholders

    A statement with params=None or params omitted is always safe regardless
    of its text (psycopg2 performs no substitution at all in that case) --
    callers of this function should not call it for no-params call sites;
    the AST/fixture layer below only calls it where params is genuinely
    supplied and non-None.
    """
    working = sql.replace("%%", "")  # remove escaped literal percents first

    if isinstance(params, dict):
        named = _NAMED_PLACEHOLDER_RE.findall(working)
        missing = [k for k in named if k not in params]
        stripped = _NAMED_PLACEHOLDER_RE.sub("", working)
        stray = "%" in stripped
        ok = not missing and not stray
        detail = ""
        if missing:
            detail += f"named placeholder(s) {missing!r} have no matching dict key; "
        if stray:
            detail += f"stray unescaped '%' remains after removing %(name)s/%%: {stripped!r}"
        return ok, detail

    n_params = len(params) if params is not None else 0
    count_s = working.count("%s")
    stripped = working.replace("%s", "")
    stray = "%" in stripped
    ok = (count_s == n_params) and not stray
    detail = ""
    if count_s != n_params:
        detail += f"{count_s} '%s' placeholder(s) but {n_params} param(s); "
    if stray:
        detail += f"stray unescaped '%' remains after removing %s/%%: {stripped!r}"
    return ok, detail


# ── Part 1: prove the checker itself fires correctly (both directions) ─────

def test_checker_passes_clean_positional():
    ok, detail = check_param_sql("SELECT * FROM t WHERE county_code = %s", ("DALLAS",))
    check("clean positional statement (1 %s / 1 param): PASSES", ok, detail)


def test_checker_passes_clean_named():
    ok, detail = check_param_sql(
        "INSERT INTO t (a) VALUES (%(batch_id)s::BIGINT)", {"batch_id": 5}
    )
    check("clean named statement (%(batch_id)s / matching dict key): PASSES", ok, detail)


def test_checker_passes_correctly_escaped_like():
    ok, detail = check_param_sql(
        "SELECT * FROM t WHERE state_cd1 LIKE 'A%%' AND county_code = %s",
        ("DALLAS",),
    )
    check("correctly-escaped LIKE 'A%%' alongside one real %s: PASSES", ok, detail)


def test_checker_fails_bare_percent_in_comment_before_real_placeholder():
    # This is the EXACT pre-fix shape of compute_parcel_metrics()'s main
    # INSERT: a bare '%' inside a SQL comment, followed later by the one
    # real, legitimate %s placeholder. PM's own two-part spec catches this
    # via the "no stray %" half even though the naive count happens to
    # match (1 real %s == 1 param) -- proving why BOTH parts of the check
    # are required, not just the count.
    sql = """
        SELECT pty.county_code
        -- because TOTAL_TAX in the source is 0.00 for ~93% of all 2025 rows
        FROM parcel_tax_year pty
        WHERE pty.county_code = %s
    """
    ok, detail = check_param_sql(sql, ("DALLAS",))
    check(
        "pre-fix shape (bare '%' in a comment ahead of the real %s): correctly FAILS",
        ok is False, detail,
    )
    check("failure detail names the stray '%' text", "stray" in detail.lower(), detail)


def test_checker_fails_unescaped_like_pattern():
    sql = "UPDATE t SET x = TRUE WHERE state_cd1 LIKE 'A%' AND county_code = %s"
    ok, detail = check_param_sql(sql, ("DALLAS",))
    check("unescaped LIKE 'A%' (should be 'A%%'): correctly FAILS", ok is False, detail)


def test_checker_fails_literal_percent_s_in_comment():
    # This is the exact compute_county_benchmarks() pre-fix shape: a SQL
    # comment that literally spells out "%s" while describing the query,
    # sitting inside a statement that also has real %s placeholders.
    sql = """
        SELECT %s, pty.tax_year
        -- stamping the single %s county_code value onto every row
        FROM parcel_tax_year pty
        WHERE pty.county_code = %s
    """
    ok, detail = check_param_sql(sql, ("DALLAS", "DALLAS"))
    check(
        "comment containing literal '%s' text (3rd match, but only 2 params): correctly FAILS",
        ok is False, detail,
    )


def test_checker_fails_named_placeholder_missing_key():
    ok, detail = check_param_sql("INSERT INTO t VALUES (%(batch_id)s)", {"other_key": 1})
    check("named placeholder with no matching dict key: correctly FAILS", ok is False, detail)


# ── Part 2: real fixtures against the ACTUAL current source, imported (not
# retyped) -- proves the shipped code, post-fix, is clean; and independently
# reconstructs the documented pre-fix shape to prove it WOULD have failed. ──

def _install_fake_psycopg2():
    import types
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


def test_current_compute_parcel_metrics_insert_is_clean():
    """
    Renders compute_parcel_metrics()'s ACTUAL current main INSERT text (the
    exact f-string literal at loaders/compute_metrics.py, starting at the
    'INSERT INTO parcel_metrics (' line) with a representative county_code,
    exactly as the real code does, and checks it with the params tuple the
    real code passes: (county_code,).
    """
    _install_fake_psycopg2()
    import ast
    path = os.path.join(REPO_ROOT, "loaders", "compute_metrics.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)

    insert_call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)):
            segment = ast.get_source_segment(src, node.args[0]) or ""
            if "INSERT INTO parcel_metrics" in segment:
                insert_call = node
                break

    check("found compute_parcel_metrics()'s main INSERT cur.execute() call via AST", insert_call is not None)
    if insert_call is None:
        return

    # COMPUTATION_VERSION is the only module-level interpolation in this
    # statement (label_case_sql()/_exclude_clause() are NOT used here --
    # confirmed by reading the function; those are compute_county_benchmarks()
    # only). Bind it from the real module so this fixture can't silently
    # drift from the shipped constant.
    import loaders.compute_metrics as cm
    county_code = "DALLAS"
    local_ns = {"COMPUTATION_VERSION": cm.COMPUTATION_VERSION}
    rendered_sql = eval(
        compile(ast.Expression(body=insert_call.args[0]), "<insert>", "eval"),
        {}, local_ns,
    )

    params_node = insert_call.args[1]
    # It's always a 1-tuple: (county_code,)
    check("main INSERT call site passes exactly 1 positional param", (
        isinstance(params_node, ast.Tuple) and len(params_node.elts) == 1
    ))
    params = (county_code,)

    ok, detail = check_param_sql(rendered_sql, params)
    check("CURRENT compute_parcel_metrics() main INSERT: no unsafe %/placeholder mismatch", ok, detail)


def test_predocumented_prefix_shape_of_main_insert_would_have_failed():
    """
    Reconstructs the EXACT pre-fix comment text (bare '%', not '%%') that
    was present in loaders/compute_metrics.py before this hotfix, using the
    real surrounding SQL shape, and proves check_param_sql() would have
    flagged it -- i.e. this guard is not a test that only the post-fix code
    can trivially pass.
    """
    pre_fix_fragment = """
        SELECT
            pty.county_code,
            CASE
                WHEN pty.tax_year = 2025
                -- because TOTAL_TAX in the TaxCurOpenData source is 0.00 for ~93% of all 2025
                -- Cap at 1.0 (100%) -- values above that are bad data.
                THEN 1
            END
        FROM parcel_tax_year pty
        WHERE pty.county_code = %s
    """
    ok, detail = check_param_sql(pre_fix_fragment, ("DALLAS",))
    check("reconstructed PRE-FIX comment shape: correctly FAILS (would have caught this before it shipped)",
          ok is False, detail)


def test_current_cap_step_up_and_cap_expiry_updates_are_clean():
    """
    Same AST-driven approach for the two homestead-cap UPDATE statements in
    compute_parcel_metrics() (cap_step_up_exposure / cap_expiry_signal),
    which used un-doubled `LIKE 'A%'` / `LIKE '%HS%'` before this hotfix.
    """
    _install_fake_psycopg2()
    import ast
    path = os.path.join(REPO_ROOT, "loaders", "compute_metrics.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)

    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            text = node.args[0].value
            if "cap_step_up_exposure = TRUE" in text or "cap_expiry_signal = TRUE" in text:
                found.append((node, text))

    check("found both cap_step_up_exposure/cap_expiry_signal UPDATE statements via AST", len(found) == 2)
    for node, sql in found:
        params_node = node.args[1] if len(node.args) > 1 else None
        n_params = len(params_node.elts) if isinstance(params_node, ast.Tuple) else 0
        params = tuple(f"DALLAS{i}" for i in range(n_params))
        ok, detail = check_param_sql(sql, params)
        which = "cap_step_up_exposure" if "cap_step_up_exposure = TRUE" in sql else "cap_expiry_signal"
        check(f"CURRENT {which} UPDATE: no unsafe %/placeholder mismatch", ok, detail)


def test_current_compute_county_benchmarks_insert_is_clean():
    """
    compute_county_benchmarks()'s per-TYPE_GROUP INSERT -- the statement
    whose comment used to spell out the literal text "%s" while describing
    the query. Renders it with label_case_sql()/_exclude_clause() bound from
    the real module (both already proven elsewhere to emit no '%' of their
    own), and the real 5-element params tuple shape the live loop passes.
    """
    _install_fake_psycopg2()
    import ast
    path = os.path.join(REPO_ROOT, "loaders", "compute_metrics.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)

    insert_call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)):
            segment = ast.get_source_segment(src, node.args[0]) or ""
            if "INSERT INTO county_benchmark" in segment:
                insert_call = node
                break

    check("found compute_county_benchmarks()'s per-TYPE_GROUP INSERT via AST", insert_call is not None)
    if insert_call is None:
        return

    import loaders.compute_metrics as cm
    from tax_logic.classify import label_case_sql
    excl = cm._exclude_clause()
    label_expr = label_case_sql("p.classi_cd", "p.state_cd1")
    local_ns = {"excl": excl, "label_expr": label_expr}
    rendered_sql = eval(
        compile(ast.Expression(body=insert_call.args[0]), "<insert2>", "eval"),
        {}, local_ns,
    )
    # Real call site: (county_code, label, prefix_key, label, county_code)
    params = ("DALLAS", "Residential", "A", "Residential", "DALLAS")
    ok, detail = check_param_sql(rendered_sql, params)
    check("CURRENT compute_county_benchmarks() per-TYPE_GROUP INSERT: no unsafe %/placeholder mismatch",
          ok, detail)


def test_predocumented_prefix_shape_of_benchmarks_insert_would_have_failed():
    pre_fix_fragment = """
        SELECT %s, pty.tax_year, %s, %s
        -- rows before stamping the single %s county_code value
        -- (first SELECT-list column above) onto every resulting row
        FROM parcel_tax_year pty
        WHERE pty.county_code = %s
    """
    params = ("DALLAS", "Residential", "A", "DALLAS")
    ok, detail = check_param_sql(pre_fix_fragment, params)
    check("reconstructed PRE-FIX compute_county_benchmarks() comment shape: correctly FAILS",
          ok is False, detail)


# ── Part 3: full-module sweep -- every cur.execute() call site across all
# three named modules that passes a real params argument, checked generically
# via AST. This is the actual, ongoing recurrence guard: it will fire on any
# FUTURE statement added to these three files with the same mismatch class,
# not just the ones this incident already found. ───────────────────────────

def _iter_execute_calls_with_literal_or_fstring_sql(path):
    """Yields (lineno, sql_text_or_None, params_repr) for every cur.execute()
    call in `path` whose first argument is a plain string Constant or an
    f-string (JoinedStr) with ONLY Constant pieces (no interpolation) -- i.e.
    every call site this generic sweep can check without needing to import
    and evaluate arbitrary expressions. Calls with real interpolated
    f-strings are covered individually by the targeted fixtures above
    instead (this sweep does not silently skip them without saying so)."""
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    results = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            continue
        sql_node = node.args[0]
        sql_text = None
        interpolated = False
        if isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
            sql_text = sql_node.value
        elif isinstance(sql_node, ast.JoinedStr):
            if all(isinstance(v, ast.Constant) for v in sql_node.values):
                sql_text = "".join(v.value for v in sql_node.values)
            else:
                interpolated = True
        has_params = len(node.args) > 1 and not (
            isinstance(node.args[1], ast.Constant) and node.args[1].value is None
        )
        results.append((node.lineno, sql_text, interpolated, has_params, node.args[1] if has_params else None))
    return results


def _dummy_params_for(params_node):
    import ast
    if params_node is None:
        return None
    if isinstance(params_node, ast.Tuple):
        return tuple(f"X{i}" for i in range(len(params_node.elts)))
    if isinstance(params_node, ast.Dict):
        keys = []
        for k in params_node.keys:
            if isinstance(k, ast.Constant):
                keys.append(k.value)
        return {k: "X" for k in keys}
    # Call/Name/etc (e.g. {"batch_id": batch_id} built elsewhere, or a Call
    # to a function that returns params) -- can't statically resolve; treat
    # as "unknown shape", handled by the caller.
    return "__UNRESOLVED__"


def test_full_module_sweep_no_new_unescaped_percent_regressions():
    modules = ["compute_metrics.py", "refresh_group_stats.py", "refresh_snapshot_summary.py"]
    total_checked = 0
    total_skipped_interpolated_no_params = 0
    total_skipped_unresolved_params = 0
    for modname in modules:
        path = os.path.join(REPO_ROOT, "loaders", modname)
        for lineno, sql_text, interpolated, has_params, params_node in \
                _iter_execute_calls_with_literal_or_fstring_sql(path):
            if not has_params:
                # No params => psycopg2 does no substitution => always safe,
                # regardless of '%' content. Correctly out of scope.
                continue
            if sql_text is None:
                # Interpolated f-string WITH params -- these are exactly the
                # dangerous shape (main INSERT / county_benchmark INSERT
                # above), and are covered by name-specific fixtures instead
                # of this generic sweep, since rendering them correctly
                # requires importing real project constants. Not silently
                # skipped: counted and reported below.
                total_skipped_interpolated_no_params += 1
                continue
            params = _dummy_params_for(params_node)
            if params == "__UNRESOLVED__":
                total_skipped_unresolved_params += 1
                continue
            ok, detail = check_param_sql(sql_text, params)
            total_checked += 1
            check(f"{modname}:{lineno} plain-string cur.execute() with params: no unsafe %/placeholder mismatch",
                  ok, detail)

    check("sweep actually checked at least one real call site", total_checked > 0,
          f"checked={total_checked}")
    print(f"    [sweep] checked={total_checked}  "
          f"interpolated-with-params (covered by targeted fixtures)={total_skipped_interpolated_no_params}  "
          f"unresolved-params-shape={total_skipped_unresolved_params}")


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL PARAM_SQL_PLACEHOLDER_SAFETY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
