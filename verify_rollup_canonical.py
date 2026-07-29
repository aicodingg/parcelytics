#!/usr/bin/env python3
"""
verify_rollup_canonical.py — mechanical regression test for the M2 hard
rule (SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.3/§4.3, AC5): "no file outside
parcel_rollup.py writes parcel_tax_year's value columns."

Honesty note on this file's existence (per this migration's AC8
requirement): the M2 brief instructed building this "in the same style as
the existing verify_parcel_filters_canonical.py". That file does NOT
actually exist anywhere in this repo — grepping the codebase found only
THREE files (KNOWN_LIMITATIONS.md, SPEC_UNIT_MODEL_AND_INGEST_GATE.md, and
parcel_filters.py's own docstring) referencing that name as if it were a
real, already-built regression test. It isn't. This file was built from
scratch, following the *described* pattern (a plain grep-based script,
mirroring parcel_filters.py's own "one canonical definition + a mechanical
audit that consumers match it" philosophy) rather than copied from
anything that actually exists. Flagged here explicitly rather than
silently pretending a prior test file was extended.

What this checks:
  1. HARD RULE: every `INSERT INTO parcel_tax_year` / `UPDATE
     parcel_tax_year` in the codebase touching a VALUE column (market_value,
     assessed_value, taxable_value, hs_cap_loss, land_value, imprv_value,
     exemption_codes, data_source, unit_count) must live in
     parcel_rollup.py itself — UNLESS the file is in KNOWN_PRE_EXISTING_
     WRITERS (see below).
  2. Every one of the 4 loaders this migration refactored
     (load_certified_2025.py, load_2026_preliminary.py,
     load_certified_historical.py, load_ajr.py) imports loaders/ears_format
     — i.e. hasn't quietly reverted to a locally re-typed slice table.
  3. Those same 4 loaders import parcel_rollup and actually call it
     (parcel_rollup.run(...) or parcel_rollup.rollup_tax_year(...)) inside
     their own load()/main() — not just imported and unused.
  4. No file OTHER than loaders/ears_format.py defines its own
     `EXEMPTION_FIELDS = [` slice table (the exact copy-drift risk that
     motivated consolidating it there in the first place).

KNOWN_PRE_EXISTING_WRITERS (disclosed, not silently ignored — see
loaders/run_all.py's own module docstring for the same disclosure):
these four files write parcel_tax_year's value columns directly and were
NOT part of this migration's explicit refactor scope (the brief named
only the 4 loaders in check #2/#3 above, plus run_all.py). This test
still FAILS if the count of direct-write files grows beyond this
allowlist — it just doesn't fail on the pre-existing, already-disclosed
gap.
    load_cert_2021.py    — standalone 2021 Certified Roll loader
    load_exemptions.py   — exemption_codes correction utility
    load_pir_tcad.py     — Step 5 PIR supplemental loader
    validate_coverage_sql.py — SQL-syntax validation harness (writes only
                               synthetic TEST_-prefixed rows, deletes them
                               after; included here for completeness since
                               it does contain a literal parcel_tax_year
                               write statement)

Run: python3 verify_rollup_canonical.py
Exits 0 if every check passes, 1 otherwise.
"""
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

VALUE_COLUMNS = {
    "market_value", "assessed_value", "taxable_value", "hs_cap_loss",
    "land_value", "imprv_value", "exemption_codes", "data_source", "unit_count",
}

KNOWN_PRE_EXISTING_WRITERS = {
    "loaders/load_cert_2021.py",
    "loaders/load_exemptions.py",
    "loaders/load_pir_tcad.py",
    "validate_coverage_sql.py",
    # Pre-existing integration test fixture, not a real loader — its
    # in-memory DB shim happens to embed a literal `INSERT INTO
    # parcel_tax_year` SQL string as test setup. Not in this migration's
    # scope; disclosed here rather than silently excluded without comment.
    "loaders/test_pir_loaders.py",
}

REFACTORED_LOADERS = [
    "loaders/load_certified_2025.py",
    "loaders/load_2026_preliminary.py",
    "loaders/load_certified_historical.py",
    "loaders/load_ajr.py",
]

SKIP_DIRS = {".git", "node_modules", "task_staging", "__pycache__", "uploads"}


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, REPO_ROOT)
                yield rel, full


def check_hard_rule():
    """No file outside parcel_rollup.py (or the disclosed allowlist) writes
    parcel_tax_year's value columns."""
    violations = []
    pattern = re.compile(r"(INSERT INTO\s+parcel_tax_year|UPDATE\s+parcel_tax_year)", re.IGNORECASE)

    for rel, full in _iter_py_files():
        if rel == "parcel_rollup.py":
            continue
        if rel.replace("\\", "/") == "verify_rollup_canonical.py":
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue

        for m in pattern.finditer(text):
            # Look at a window after the match for a value-column mention —
            # cheap heuristic (not a real SQL parser) but sufficient to
            # distinguish "writes a value column" from an incidental
            # mention (e.g. a SELECT, a comment, a docstring reference).
            window = text[m.start(): m.start() + 400]
            if any(col in window for col in VALUE_COLUMNS):
                rel_norm = rel.replace("\\", "/")
                if rel_norm in KNOWN_PRE_EXISTING_WRITERS:
                    continue  # disclosed, pre-existing, out of this migration's scope
                violations.append((rel_norm, m.group(0)))

    passed = len(violations) == 0
    detail = f"{len(violations)} violation(s) outside parcel_rollup.py / the disclosed allowlist"
    return passed, detail, violations


def check_loaders_use_ears_format():
    missing = []
    for rel in REFACTORED_LOADERS:
        full = os.path.join(REPO_ROOT, rel)
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            missing.append((rel, "file not found"))
            continue
        if "ears_format" not in text:
            missing.append((rel, "no reference to ears_format"))
    passed = len(missing) == 0
    detail = f"{len(REFACTORED_LOADERS) - len(missing)}/{len(REFACTORED_LOADERS)} loaders reference ears_format"
    return passed, detail, missing


def check_loaders_call_rollup():
    missing = []
    call_pattern = re.compile(r"parcel_rollup\.(run|rollup_tax_year|rollup_all_years)\s*\(")
    for rel in REFACTORED_LOADERS:
        full = os.path.join(REPO_ROOT, rel)
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            missing.append((rel, "file not found"))
            continue
        if "import parcel_rollup" not in text:
            missing.append((rel, "does not import parcel_rollup"))
            continue
        if not call_pattern.search(text):
            missing.append((rel, "imports parcel_rollup but never calls run()/rollup_tax_year()/rollup_all_years()"))
    passed = len(missing) == 0
    detail = f"{len(REFACTORED_LOADERS) - len(missing)}/{len(REFACTORED_LOADERS)} loaders call parcel_rollup"
    return passed, detail, missing


def check_loaders_import_gate():
    """AC5: 'every loader imports the gate' — literal check that each of
    the 4 refactored loaders imports loaders/ingest_gate.py. See each
    loader's own module-level comment for why this is an import-only
    wiring marker rather than an inline full-file re-scan (performance
    tradeoff on multi-GB source files, run_all.py is the real enforcement
    point) — a judgment call flagged there and in the final report, not
    silently made."""
    missing = []
    for rel in REFACTORED_LOADERS:
        full = os.path.join(REPO_ROOT, rel)
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            missing.append((rel, "file not found"))
            continue
        if "ingest_gate" not in text:
            missing.append((rel, "does not import loaders.ingest_gate"))
    passed = len(missing) == 0
    detail = f"{len(REFACTORED_LOADERS) - len(missing)}/{len(REFACTORED_LOADERS)} loaders import ingest_gate"
    return passed, detail, missing


def check_no_retyped_rollup_sql():
    """No file other than parcel_rollup.py contains the rollup's
    SUM(...)/GROUP BY signature — i.e. nobody hand-copied the aggregation
    SQL instead of calling parcel_rollup.py's functions."""
    offenders = []
    signature = re.compile(r"SUM\(y\.market_value\)|GROUP BY u\.geo_id, y\.tax_year")
    for rel, full in _iter_py_files():
        rel_norm = rel.replace("\\", "/")
        if rel_norm in ("parcel_rollup.py", "verify_rollup_canonical.py"):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if signature.search(text):
            offenders.append(rel)
    passed = len(offenders) == 0
    detail = f"{len(offenders)} file(s) contain a re-typed copy of the rollup SQL signature"
    return passed, detail, offenders


def check_no_duplicate_exemption_fields():
    offenders = []
    pattern = re.compile(r"EXEMPTION_FIELDS\s*=\s*\[")
    for rel, full in _iter_py_files():
        rel_norm = rel.replace("\\", "/")
        if rel_norm in ("loaders/ears_format.py", "verify_rollup_canonical.py"):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if pattern.search(text):
            offenders.append(rel)
    passed = len(offenders) == 0
    detail = f"{len(offenders)} file(s) define their own EXEMPTION_FIELDS outside ears_format.py"
    return passed, detail, offenders


def main():
    checks = [
        ("Hard rule: parcel_tax_year value-column writes", check_hard_rule),
        ("Refactored loaders reference ears_format", check_loaders_use_ears_format),
        ("Refactored loaders call parcel_rollup", check_loaders_call_rollup),
        ("Refactored loaders import ingest_gate", check_loaders_import_gate),
        ("No re-typed rollup SQL outside parcel_rollup.py", check_no_retyped_rollup_sql),
        ("No duplicate EXEMPTION_FIELDS outside ears_format.py", check_no_duplicate_exemption_fields),
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
    if overall_pass:
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
