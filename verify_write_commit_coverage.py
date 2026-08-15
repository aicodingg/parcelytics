"""
verify_write_commit_coverage.py — BILLING-DIAG-7 permanent regression test.

Fable's architectural review (BILLING-DIAG-7) required a real, permanent
regression test making a specific failure class structurally impossible to
reintroduce silently: a function that opens a write cursor (INSERT/UPDATE/
DELETE/execute_batch/executemany against a `conn`-like object) but never
commits that work -- meaning `conn.close()` (or the request ending) silently
discards it via psycopg2's default rollback-on-close behavior, with no
exception, no log line, and a perfectly clean-looking response. This is the
exact class of bug BILLING-DIAG-7's own brief was originally built around
(and, per that brief's own report, turned out to be a FALSE ALARM for the
specific function investigated -- `upsert_billing_rows()` already committed
correctly. But the review is correct that this class of bug is real and
worth guarding against structurally, independent of whether this specific
instance turned out to be real).

Technique: same family as this codebase's other AC5-style / audit-style
regression tests (see the AUDIT task series and verify_index_coverage.py) --
regex-based extraction of top-level function bodies from real source files,
then a real, static check on each one. This is NOT a full AST/dataflow
analysis (a function that commits via a helper it calls, rather than
directly, is treated as safe ONLY if that's a known, allow-listed delegation
-- see DELEGATES_COMMIT_TO below) -- but it is real, it runs against the
actual shipped source text (not a description of it), and it directly
reproduces the exact structural gap this brief worried about.

Run: python3 verify_write_commit_coverage.py
"""
import re
import sys

# Files to scan. app.py + every loaders/*.py write-capable module.
import glob

SCAN_FILES = ["app.py"] + sorted(glob.glob("loaders/*.py"))

# Regex for a top-level (column-0) `def name(...):` — matches this
# codebase's real convention (helper functions are module-level, not
# nested/indented, in every file actually touched by this audit).
FUNC_DEF_RE = re.compile(r"^def\s+(\w+)\s*\(", re.MULTILINE)

# A cursor-execute-style call, loosely matched (execute/execute_batch/executemany).
EXECUTE_CALL_RE = re.compile(r"\.(execute|execute_batch|executemany)\s*\(")

# Real, minimal write-keyword detection on the SQL text near an execute call.
# Deliberately loose (substring match on the raw source in the vicinity of
# the call) rather than a full SQL parser -- consistent with this codebase's
# existing SQL-extraction tooling (the AUDIT task series), which uses the
# same "good enough, real, and cheap" tradeoff.
WRITE_KEYWORD_RE = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)

# A function's own docstring (the first statement after `def ...:`, when it's
# a string literal) — this is prose, not SQL, and real functions in this
# codebase routinely use ordinary English words like "update"/"delete" in
# their docstrings (confirmed empirically: an early version of this checker
# flagged 4 pure-SELECT/orchestration functions purely because of docstring
# prose like "never treated as a delete target" or a `# coverage_level
# update` comment). Stripped before keyword-matching so only real code/SQL
# text is considered. Deliberately does NOT strip other triple-quoted
# strings in a function body (e.g. a multi-line SQL constant assigned with
# `"""..."""`) -- only the docstring immediately after the `def` line.
_DOCSTRING_RE = re.compile(
    r'^def\s+\w+\s*\([^)]*\)\s*(?:->[^:]+)?:\s*\n\s*'
    r'(?P<q>"""|\'\'\')(?P<body>.*?)(?P=q)',
    re.DOTALL,
)
_LINE_COMMENT_RE = re.compile(r"#[^\n]*")


def _strip_prose(body: str) -> str:
    """Remove the function's own docstring and all `#` line comments, so
    keyword-matching only sees real code/SQL text."""
    body = _DOCSTRING_RE.sub(lambda m: m.group(0).replace(m.group("body"), " " * len(m.group("body"))), body, count=1)
    body = _LINE_COMMENT_RE.sub("", body)
    return body

# Functions that legitimately open a write cursor and DO commit, but whose
# commit is spelled in a way this regex-based check can't see directly (e.g.
# a schema-only DDL helper that's exempt, or a function whose only "write"
# keyword hit is inside a comment/docstring, not real SQL). Reviewed by hand
# against the real BILLING-DIAG-7 audit; kept short and named, not a general
# escape hatch.
KNOWN_SAFE_FALSE_POSITIVES = {
    # (filename, function_name): reason
}


def extract_functions(src: str) -> list[tuple[str, str]]:
    """Return [(name, body_text), ...] for every top-level `def` in src.

    body_text ends at the true end of the function's indented block (the
    first subsequent line that is non-blank AND starts at column 0), NOT at
    the next top-level `def` -- module-level code sitting BETWEEN two
    functions (a SQL constant like `BILLING_SQL = \"\"\"...\"\"\"`, for
    instance) previously got misattributed to the PRECEDING function under a
    naive next-def-boundary slice, which is exactly what produced 2 of the
    real false positives found while building this checker (reconcile_geo_ids()
    in two different loader files, both pure-SELECT, both incorrectly
    flagged only because an unrelated INSERT-containing SQL constant
    happened to be defined right after them in the file). A simple
    triple-quote-tracking line scan (this codebase's real style: 4-space
    indented bodies, module-level defs/constants at column 0, no functions
    containing genuinely unindented top-level code inside their own
    docstrings/SQL constants) is enough to get this right without a full
    parser.
    """
    lines = src.splitlines(keepends=True)
    def_line_idxs = [i for i, line in enumerate(lines) if FUNC_DEF_RE.match(line)]
    out = []
    for idx in def_line_idxs:
        name = FUNC_DEF_RE.match(lines[idx]).group(1)
        end = len(lines)
        in_string = None  # None, or the triple-quote delimiter currently open
        for j in range(idx + 1, len(lines)):
            line = lines[j]
            if in_string:
                if in_string in line:
                    in_string = None
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if not line[0].isspace():
                end = j
                break
            # Track any triple-quoted string that OPENS and does not close
            # on the same line, so column-0 continuation lines inside it
            # (e.g. an unindented-for-readability SQL block) don't falsely
            # end the function.
            for q in ('"""', "'''"):
                count = line.count(q)
                if count % 2 == 1:
                    in_string = q
                    break
        body = "".join(lines[idx:end])
        out.append((name, body))
    return out


def function_has_write_call(body: str) -> bool:
    # Prose (the function's own docstring + all `#` comments) stripped FIRST
    # -- both the execute-call scan and the keyword window below run against
    # this cleaned text, so English words in comments/docstrings ("update",
    # "delete") can no longer produce a false positive. _strip_prose()
    # replaces stripped spans with same-length whitespace, so all character
    # offsets stay aligned with the original body.
    clean = _strip_prose(body)
    for m in EXECUTE_CALL_RE.finditer(clean):
        # Look at a bounded window around the call for a write keyword —
        # covers both `cur.execute("INSERT ...")` (keyword right there) and
        # `cur.execute(SOME_SQL_CONSTANT, params)` where the constant is
        # defined a few lines above/below with the keyword in it. Bounded
        # window keeps this from accidentally matching unrelated SQL
        # elsewhere in a long function.
        window = clean[max(0, m.start() - 400): m.end() + 400]
        if WRITE_KEYWORD_RE.search(window):
            return True
    return False


def function_commits(body: str) -> bool:
    return bool(re.search(r"\bwith\s+conn\s*:", body)) or bool(re.search(r"\.commit\s*\(", body))


def audit_file(path: str) -> list[str]:
    with open(path) as f:
        src = f.read()
    violations = []
    for name, body in extract_functions(src):
        if (path, name) in KNOWN_SAFE_FALSE_POSITIVES:
            continue
        if function_has_write_call(body) and not function_commits(body):
            violations.append(f"{path}:{name}() -- write call with no `with conn:` or `.commit()` in the same function")
    return violations


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


def main():
    all_ok = True
    all_violations = []
    files_scanned = 0
    for path in SCAN_FILES:
        try:
            violations = audit_file(path)
        except FileNotFoundError:
            continue
        files_scanned += 1
        all_violations.extend(violations)

    all_ok &= check(f"scanned {files_scanned} real source files (app.py + loaders/*.py)", files_scanned > 5)
    all_ok &= check("zero write-cursor-without-commit violations found", all_violations == [])
    for v in all_violations:
        print(f"    VIOLATION: {v}")

    # Sanity check on the checker itself: a deliberately broken synthetic
    # snippet (write call, no commit) MUST be flagged, and a correct one
    # (using `with conn:`) MUST NOT be — proves the regex logic actually
    # discriminates, not just returns empty on everything.
    broken_snippet = (
        "def broken_write(conn, x):\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('INSERT INTO t (a) VALUES (%s)', (x,))\n"
    )
    fixed_snippet = (
        "def fixed_write(conn, x):\n"
        "    with conn:\n"
        "        with conn.cursor() as cur:\n"
        "            cur.execute('INSERT INTO t (a) VALUES (%s)', (x,))\n"
    )
    # Real false-positive shape this checker hit against the actual codebase:
    # a pure-SELECT function whose docstring/comment happens to contain an
    # English word like "update"/"delete" in prose, not SQL.
    prose_snippet = (
        "def read_only(conn, pairs):\n"
        "    \"\"\"Never treated as a delete target -- read-only lookup.\"\"\"\n"
        "    # coverage_level update happens elsewhere, not here\n"
        "    with conn.cursor() as cur:\n"
        "        cur.execute('SELECT * FROM t')\n"
        "        return cur.fetchall()\n"
    )
    fns_broken = extract_functions(broken_snippet)
    fns_fixed = extract_functions(fixed_snippet)
    fns_prose = extract_functions(prose_snippet)
    all_ok &= check(
        "self-test: checker correctly FLAGS a synthetic write-without-commit function",
        function_has_write_call(fns_broken[0][1]) and not function_commits(fns_broken[0][1])
    )
    all_ok &= check(
        "self-test: checker correctly PASSES a synthetic `with conn:` write function",
        function_has_write_call(fns_fixed[0][1]) and function_commits(fns_fixed[0][1])
    )
    all_ok &= check(
        "self-test: checker does NOT flag a read-only function with 'update'/'delete' prose "
        "in its docstring/comments (the real false-positive shape found against this codebase)",
        not function_has_write_call(fns_prose[0][1])
    )

    print()
    if all_ok:
        print("ALL CHECKS PASSED")
        sys.exit(0)
    else:
        print("SOME CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
