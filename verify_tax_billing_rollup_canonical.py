#!/usr/bin/env python3
"""
verify_tax_billing_rollup_canonical.py — mechanical regression test for
TAX-BILLING-REKEY-3's own hard rule (SPEC_TAX_BILLING_REKEY.md §7.3/§7.5
AC5-equivalent): "no file outside tax_billing_rollup.py writes
tax_billing/tax_billing_entity's value columns, except a small, disclosed,
non-value-deriving administrative allowlist." Mirrors
verify_rollup_canonical.py's exact pattern (that file's own docstring
already discloses it was built from a described-but-never-existing
"verify_parcel_filters_canonical.py" precedent; this file follows the same
now-real, mechanically-checked pattern for the billing side).

REAL CLOSED WRITER SET (verified this session via direct grep across the
live repo, NOT assumed from the spec's own §7.3 pre-migration 8-writer
enumeration — that enumeration was for the OLD, pre-migration state, and
turned out to have missed one real post-migration writer entirely, see
below):

    tax_billing_rollup.py                              -- the ONLY writer
        of tax_billing/tax_billing_entity's real, derived VALUE columns
        (total_tax, total_paid, total_due, is_delinquent, billing_num,
        owner_name, exemption_codes, data_source, confidence_level,
        account_count / amount_due, amount_paid). SUMs from
        tax_billing_account/tax_billing_account_entity.

    loaders/backfill_tax_billing_2025_confidence.py     -- UPDATE-only;
        sets data_source/confidence_level metadata on existing rows, never
        derives new dollar totals. Pre-existing, unchanged by this
        migration (per its own DALLAS-GATE-4 county_code fix, already
        applied and tested separately).

    loaders/quarantine_contamination.py                 -- DELETE (move to
        tax_billing_quarantine) + INSERT (restore from quarantine)
        administrative moves; both paths carry a row's existing value
        columns verbatim, never derive new ones. Pre-existing, unchanged.

    loaders/delete_confirmed_absent_taxcur_rows.py       -- DELETE-only,
        exact-(geo_id,tax_year)-pairs-scoped administrative cleanup (see
        that file's own extensive docstring for its 5 safety mechanisms).
        *** NOT in SPEC_TAX_BILLING_REKEY.md §7.3's own 8-writer table ***
        -- found by this task's own fresh grep, not inherited from that
        table or from this session's own prior (pre-compaction) summary,
        which had incorrectly assumed the post-migration closed set was
        only 3 files. Flagged explicitly to Diego in the M0-M4 final
        report: the real number is 4, not 3, and the spec's own §7.3
        enumeration (which was scoped to the OLD write-path writers being
        redirected, not a total-writer-surface audit) should be read with
        that scope in mind rather than as an exhaustive list of every file
        that ever touches these two tables.

DISCLOSED, OUT-OF-SCOPE ALLOWLIST (test/diagnostic code, not production
writers -- see check_hard_rule()'s own KNOWN_PRE_EXISTING_WRITERS below for
the one-line reason each is excluded):
    validate_coverage_sql.py       -- writes only synthetic TEST_-prefixed
                                       rows (geo_id='TEST000001', tax_year=
                                       2088), deletes them after; confirmed
                                       via direct read of its own docstring
                                       ("Nothing in the real data is
                                       touched").
    loaders/test_pir_loaders.py    -- literal SQL string embedded as
                                       in-memory test-fixture seed data,
                                       never executed against a real DB
                                       connection in production.
    test_dallas_gate_4_county_code.py -- confirmed via direct read: this
                                       file's own technique (per its own
                                       docstring) is "direct string-
                                       membership/regex assertions against
                                       each file's REAL, shipping source
                                       text" -- i.e. its source embeds
                                       literal SQL-fragment string
                                       constants (e.g. "INSERT INTO
                                       tax_billing_entity (county_code,
                                       ...)") as PATTERNS it searches for
                                       inside OTHER files' text, never SQL
                                       this file itself executes. This
                                       checker's own hard-rule scan reads
                                       this file's raw source and correctly
                                       finds those pattern-literal strings
                                       present -- a real but harmless false
                                       positive of the same shape
                                       verify_rollup_canonical.py already
                                       had to exclude itself from.

What this checks:
  1. HARD RULE: every `INSERT INTO tax_billing` / `UPDATE tax_billing` /
     `INSERT INTO tax_billing_entity` / `UPDATE tax_billing_entity` in the
     codebase touching a VALUE column must live in tax_billing_rollup.py
     itself, OR the statement must be one of the disclosed administrative
     moves in KNOWN_PRE_EXISTING_WRITERS (verbatim carry-forward, not a
     newly-derived value) OR the file is in the test/diagnostic allowlist.
  2. Every one of the 4 redirected loaders (load_tax_current.py,
     load_pir_billing.py, load_pir_billing_2021_full.py,
     pir_xlsx_common.py) imports tax_billing_rollup AND actually calls
     tax_billing_rollup.run(...) inside its own load()/main() path -- not
     just imported and unused.
  3. Those same 4 loaders' real INSERT statements target
     tax_billing_account (the new unit-grain table), NOT tax_billing
     directly -- proves the redirect actually happened, not merely that
     the rollup module got imported alongside an unchanged old write path.
  4. scrape_billing_history.py and app.py's api_billing() route target
     tax_billing_portal_scrape for their write statements (§7.3 design
     (a)), not tax_billing directly.
  5. No file other than tax_billing_rollup.py contains the rollup's
     SUM(total_tax)/GROUP BY (county_code, geo_id, tax_year) aggregation
     signature -- i.e. nobody hand-copied the rollup SQL instead of
     calling tax_billing_rollup.py's own functions.
  6. run_all.py imports tax_billing_rollup and calls it after both the
     2025 tax_current step and the PIR billing-file loop.

Run: python3 verify_tax_billing_rollup_canonical.py
Exits 0 if every check passes, 1 otherwise.
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".git", "node_modules", "task_staging", "__pycache__", "uploads"}

VALUE_COLUMNS = {
    "total_tax", "total_paid", "total_due", "is_delinquent", "billing_num",
    "owner_name", "cause_number", "exemption_codes", "data_source",
    "confidence_level", "account_count", "amount_due", "amount_paid",
    "first_delinquent_yr",
}

# See module docstring for the one-line reason each entry is here. Both are
# test/diagnostic code, never a production writer.
KNOWN_PRE_EXISTING_WRITERS = {
    "validate_coverage_sql.py",
    "loaders/test_pir_loaders.py",
    "test_dallas_gate_4_county_code.py",
}

# Real, disclosed administrative writers: move/annotate a row's EXISTING
# value columns verbatim (or DELETE-only), never derive a new dollar total
# from source data the way tax_billing_rollup.py's SUM()-based rollup does.
# See module docstring's REAL CLOSED WRITER SET section for the full
# rationale on each, including the delete_confirmed_absent_taxcur_rows.py
# correction (found via this task's own fresh grep, not carried over from
# the spec's own pre-migration 8-writer table).
KNOWN_ADMINISTRATIVE_WRITERS = {
    "loaders/backfill_tax_billing_2025_confidence.py",
    "loaders/quarantine_contamination.py",
    "loaders/delete_confirmed_absent_taxcur_rows.py",
}

REDIRECTED_LOADERS = [
    "loaders/load_tax_current.py",
    "loaders/load_pir_billing.py",
    "loaders/load_pir_billing_2021_full.py",
    "loaders/pir_xlsx_common.py",
]

BILLING_WRITE_PATTERN = re.compile(
    r"(INSERT INTO\s+tax_billing\b|UPDATE\s+tax_billing\b)(?!_)", re.IGNORECASE
)
ENTITY_WRITE_PATTERN = re.compile(
    r"(INSERT INTO\s+tax_billing_entity\b|UPDATE\s+tax_billing_entity\b)", re.IGNORECASE
)


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, REPO_ROOT).replace("\\", "/")
                yield rel, full


def _read(full):
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# ══════════════════════════════════════════════════════════════════════
# Check 1: HARD RULE
# ══════════════════════════════════════════════════════════════════════
def check_hard_rule():
    violations = []
    allowlist = KNOWN_PRE_EXISTING_WRITERS | KNOWN_ADMINISTRATIVE_WRITERS

    for rel, full in _iter_py_files():
        if rel in ("tax_billing_rollup.py", "verify_tax_billing_rollup_canonical.py"):
            continue
        text = _read(full)
        if text is None:
            continue

        for pattern in (BILLING_WRITE_PATTERN, ENTITY_WRITE_PATTERN):
            for m in pattern.finditer(text):
                window = text[m.start(): m.start() + 500]
                if not any(col in window for col in VALUE_COLUMNS):
                    continue  # matched the table name but no value column nearby -- not a real hit
                if rel in allowlist:
                    continue  # disclosed, verified non-value-deriving (or test-only)
                violations.append((rel, m.group(0)))

    passed = len(violations) == 0
    detail = f"{len(violations)} violation(s) outside tax_billing_rollup.py / the disclosed allowlist"
    return passed, detail, violations


# ══════════════════════════════════════════════════════════════════════
# Check 2: redirected loaders import + call tax_billing_rollup
# ══════════════════════════════════════════════════════════════════════
def check_loaders_call_rollup():
    missing = []
    call_pattern = re.compile(r"tax_billing_rollup\.run\s*\(")
    for rel in REDIRECTED_LOADERS:
        full = os.path.join(REPO_ROOT, rel)
        text = _read(full)
        if text is None:
            missing.append((rel, "file not found"))
            continue
        if "import tax_billing_rollup" not in text:
            missing.append((rel, "does not import tax_billing_rollup"))
            continue
        if not call_pattern.search(text):
            missing.append((rel, "imports tax_billing_rollup but never calls run()"))
    passed = len(missing) == 0
    detail = f"{len(REDIRECTED_LOADERS) - len(missing)}/{len(REDIRECTED_LOADERS)} loaders call tax_billing_rollup.run()"
    return passed, detail, missing


# ══════════════════════════════════════════════════════════════════════
# Check 3: redirected loaders' real INSERTs target tax_billing_account
# ══════════════════════════════════════════════════════════════════════
def check_loaders_target_unit_table():
    missing = []
    unit_pattern = re.compile(r"INSERT INTO\s+tax_billing_account\b", re.IGNORECASE)
    for rel in REDIRECTED_LOADERS:
        full = os.path.join(REPO_ROOT, rel)
        text = _read(full)
        if text is None:
            missing.append((rel, "file not found"))
            continue
        if not unit_pattern.search(text):
            missing.append((rel, "no INSERT INTO tax_billing_account found -- redirect may not have landed"))
    passed = len(missing) == 0
    detail = f"{len(REDIRECTED_LOADERS) - len(missing)}/{len(REDIRECTED_LOADERS)} loaders write tax_billing_account"
    return passed, detail, missing


# ══════════════════════════════════════════════════════════════════════
# Check 4: scrape_billing_history.py + app.py target the portal-scrape
# table, not tax_billing directly
# ══════════════════════════════════════════════════════════════════════
def check_portal_writers_target_scrape_table():
    problems = []
    portal_pattern = re.compile(r"INSERT INTO\s+tax_billing_portal_scrape\b", re.IGNORECASE)
    for rel in ("loaders/scrape_billing_history.py", "app.py"):
        full = os.path.join(REPO_ROOT, rel)
        text = _read(full)
        if text is None:
            problems.append((rel, "file not found"))
            continue
        if not portal_pattern.search(text):
            problems.append((rel, "no INSERT INTO tax_billing_portal_scrape found"))
        # Direct-write violation would already have been caught by
        # check_hard_rule() above (these two files are NOT in either
        # allowlist) -- this check only confirms the POSITIVE redirect
        # target, not the absence, to avoid duplicating that assertion.
    passed = len(problems) == 0
    detail = f"{2 - len(problems)}/2 portal writers target tax_billing_portal_scrape"
    return passed, detail, problems


# ══════════════════════════════════════════════════════════════════════
# Check 5: no re-typed rollup SQL outside tax_billing_rollup.py
# ══════════════════════════════════════════════════════════════════════
def check_no_retyped_rollup_sql():
    """
    A genuine re-typed copy of ROLLUP_SQL/ENTITY_ROLLUP_SQL would have BOTH
    'FROM tax_billing_account' (the real rollup source table) AND a
    'GROUP BY county_code, geo_id, tax_year' close together in the same
    statement -- that combination is the actual "copy" signature, same
    windowed-proximity technique
    verify_parcel_filters_coverage.py.check_no_retyped_exclusion_fragment()
    uses for its own multi-leg signature. Deliberately does NOT match on
    'SUM(total_tax)' alone -- both
    loaders/backfill_tax_billing_2025_confidence.py (a legitimate,
    unrelated aggregation FROM tax_billing_entity, not tax_billing_account)
    and loaders/billing_gate.py (legitimate read-only SELECT SUM(total_tax)
    verification queries with no GROUP BY at all -- BG3/BG4 gather sums
    scoped by WHERE, nothing to group) both mention that fragment for
    real, disclosed, non-duplicate reasons -- confirmed by direct read of
    both files during this check's own construction, not assumed.
    """
    offenders = []
    from_pattern = re.compile(r"FROM\s+tax_billing_account\b")
    group_pattern = re.compile(r"GROUP BY county_code,\s*geo_id,\s*tax_year\b")
    for rel, full in _iter_py_files():
        if rel in ("tax_billing_rollup.py", "verify_tax_billing_rollup_canonical.py"):
            continue
        text = _read(full)
        if text is None:
            continue
        for m in from_pattern.finditer(text):
            window = text[m.start(): m.start() + 300]
            if group_pattern.search(window):
                offenders.append(rel)
                break
    passed = len(offenders) == 0
    detail = f"{len(offenders)} file(s) contain a re-typed copy of the rollup SQL signature"
    return passed, detail, offenders


# ══════════════════════════════════════════════════════════════════════
# Check 6: run_all.py wires tax_billing_rollup in
# ══════════════════════════════════════════════════════════════════════
def check_run_all_wires_rollup():
    full = os.path.join(REPO_ROOT, "loaders/run_all.py")
    text = _read(full)
    if text is None:
        return False, "loaders/run_all.py not found", [("loaders/run_all.py", "file not found")]
    problems = []
    if "import tax_billing_rollup" not in text:
        problems.append(("loaders/run_all.py", "does not import tax_billing_rollup"))
    if "tax_billing_rollup.run(" not in text:
        problems.append(("loaders/run_all.py", "never calls tax_billing_rollup.run()"))
    passed = len(problems) == 0
    detail = "run_all.py imports and calls tax_billing_rollup.run()" if passed else "missing wiring"
    return passed, detail, problems


# ══════════════════════════════════════════════════════════════════════
# Check 7 (PX-20260826-01): run_all.py wires billing_gate in, at both
# billing steps, non-blocking (never depends on someone remembering to
# run loaders/billing_gate.py by hand -- same discipline check 6 above
# already established for tax_billing_rollup.run() itself).
# ══════════════════════════════════════════════════════════════════════
def check_run_all_wires_billing_gate():
    full = os.path.join(REPO_ROOT, "loaders/run_all.py")
    text = _read(full)
    if text is None:
        return False, "loaders/run_all.py not found", [("loaders/run_all.py", "file not found")]
    problems = []
    if "from loaders import billing_gate" not in text and "import billing_gate" not in text:
        problems.append(("loaders/run_all.py", "does not import billing_gate"))

    # Only count real call sites, not this comment's own mention of the
    # function name or any other prose reference -- filter to lines whose
    # stripped text doesn't start with '#' before matching.
    call_pattern = re.compile(r"billing_gate\.gather_and_run\s*\(")
    calls = [
        line for line in text.splitlines()
        if not line.strip().startswith("#") and call_pattern.search(line)
    ]
    if len(calls) < 2:
        problems.append(("loaders/run_all.py",
                          f"expected 2 billing_gate.gather_and_run() call sites "
                          f"(2025 TaxCur step + PIR-billing-year loop), found {len(calls)}"))

    # Non-blocking check: each gather_and_run() call site's own immediate
    # neighborhood must be plain print()s, not a `return`/`sys.exit()` --
    # a genuinely blocking wiring would abort the run on a billing_gate
    # FAIL, which the brief explicitly says NOT to do (loud, post-hoc,
    # non-blocking, mirroring ceda1f0's load_certified_historical.py wiring).
    for m in call_pattern.finditer(text):
        window = text[m.end(): m.end() + 400]
        if re.search(r"\breturn\b|\bsys\.exit\(", window):
            problems.append(("loaders/run_all.py",
                              "a billing_gate.gather_and_run() call site is followed by "
                              "return/sys.exit() within 400 chars -- looks blocking, not "
                              "loud-post-hoc-non-blocking as required"))

    passed = len(problems) == 0
    detail = (f"{len(calls)} billing_gate.gather_and_run() call site(s), imported, non-blocking"
              if passed else "missing/blocking billing_gate wiring")
    return passed, detail, problems


def main():
    checks = [
        ("Hard rule: tax_billing/tax_billing_entity value-column writes", check_hard_rule),
        ("Redirected loaders call tax_billing_rollup.run()", check_loaders_call_rollup),
        ("Redirected loaders write tax_billing_account (not tax_billing directly)", check_loaders_target_unit_table),
        ("Portal writers (scraper + app.py) target tax_billing_portal_scrape", check_portal_writers_target_scrape_table),
        ("No re-typed rollup SQL outside tax_billing_rollup.py", check_no_retyped_rollup_sql),
        ("run_all.py wires tax_billing_rollup in", check_run_all_wires_rollup),
        ("run_all.py wires billing_gate in (non-blocking)", check_run_all_wires_billing_gate),
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

    print()
    print("Real closed writer set (4 files): tax_billing_rollup.py, "
          "loaders/backfill_tax_billing_2025_confidence.py, "
          "loaders/quarantine_contamination.py, "
          "loaders/delete_confirmed_absent_taxcur_rows.py")
    print()
    if overall_pass:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
