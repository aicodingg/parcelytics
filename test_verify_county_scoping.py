#!/usr/bin/env python3
"""
test_verify_county_scoping.py — MC2-BUILD-1's own fixture suite.

Tested-alarm principle (THE_FABLE_METHOD.md §5): a checker is only trusted
once it's proven to fire on a real, historical broken shape and stay quiet
on the real, current fixed shape. Every fixture below runs
verify_county_scoping.py's OWN extraction + audit engine
(extract_statements_from_source + audit_extracted) against real source
text — not a reimplementation, not a re-derivation of what the tool
"should" say.

REAL STATUS OF THIS SUITE, disclosed honestly: MC2-BUILD-1 (build
verify_county_scoping.py itself) originally called for FOUR fixtures built
from four real historical incidents (DALLAS-GATE-4 / d983e0b,
PIR-XLSX-HOTFIX-1 / decd438, load_delinquent() / no historical commit —
still live, per Diego's go-ahead a hypothetical-fix fixture, BILLING-GATE-
HOTFIX-1 / 443af9f). That work (task #674) was interrupted mid-session when
this tool's own real run against the repo surfaced the parcel_rollup.py /
loaders/ears_format.py gap, which became its own urgent brief,
PARCEL-ROLLUP-HOTFIX-1. Per that brief's own explicit instruction ("add
these two as the module's own 5th and 6th real fixtures"), THIS FILE
currently contains ONLY fixtures 5 and 6 (parcel_rollup.py, ears_format.py).
Fixtures 1-4 remain tracked separately under MC2-BUILD-1 task #674 and are
NOT yet in this file — adding a fake placeholder for them here would
misrepresent coverage that doesn't exist yet. Fixtures 1-4 should be
appended to this same file (numbering preserved) when #674 resumes.

Run: python3 test_verify_county_scoping.py
"""
import subprocess

import verify_county_scoping as vcs

all_ok = True


def check(label, cond):
    global all_ok
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    all_ok = all_ok and cond
    return cond


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def git_show(ref, path):
    """Real pre-fix content, read straight from git history -- not typed
    out by hand and not a synthetic reconstruction."""
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=True, cwd=".",
    ).stdout


def findings_for(source_text, filepath, table, rule):
    extracted = vcs.extract_statements_from_source(source_text, filepath)
    extracted = [e for e in extracted if e.table == table]
    findings = vcs.audit_extracted(extracted)
    return [f for f in findings if f.rule == rule]


# ─────────────────────────────────────────────────────────────────────────
# Fixture 5: parcel_rollup.py (PARCEL-ROLLUP-HOTFIX-1)
# ─────────────────────────────────────────────────────────────────────────
section("Fixture 5: parcel_rollup.py -- real pre-fix (git HEAD) vs real post-fix (working tree)")

pre_rollup = git_show("HEAD", "parcel_rollup.py")
post_rollup = open("parcel_rollup.py").read()

# Pre-fix: HEAD's real ROLLUP_SQL has no county_code in its INSERT column
# list and ON CONFLICT (geo_id, tax_year) with no county_code -- the tool
# must fire on both 3b and 3c for parcel_tax_year.
pre_3b = findings_for(pre_rollup, "parcel_rollup.py", "parcel_tax_year", "3b")
pre_3c = findings_for(pre_rollup, "parcel_rollup.py", "parcel_tax_year", "3c")
check("pre-fix (real HEAD): at least one 3b (INSERT column list) finding for parcel_tax_year",
      len(pre_3b) >= 1)
check("pre-fix (real HEAD): that 3b finding is FAIL",
      any(f.severity == "FAIL" for f in pre_3b))
check("pre-fix (real HEAD): at least one 3c (ON CONFLICT target) finding for parcel_tax_year",
      len(pre_3c) >= 1)
check("pre-fix (real HEAD): that 3c finding is FAIL",
      any(f.severity == "FAIL" for f in pre_3c))

# Post-fix: current working-tree parcel_rollup.py must show zero FAILs on
# both rules for parcel_tax_year.
post_3b = findings_for(post_rollup, "parcel_rollup.py", "parcel_tax_year", "3b")
post_3c = findings_for(post_rollup, "parcel_rollup.py", "parcel_tax_year", "3c")
check("post-fix (real working tree): 3b has no FAIL for parcel_tax_year",
      len(post_3b) >= 1 and not any(f.severity == "FAIL" for f in post_3b))
check("post-fix (real working tree): 3c has no FAIL for parcel_tax_year",
      len(post_3c) >= 1 and not any(f.severity == "FAIL" for f in post_3c))


# ─────────────────────────────────────────────────────────────────────────
# Fixture 6: loaders/ears_format.py (PARCEL-ROLLUP-HOTFIX-1)
# ─────────────────────────────────────────────────────────────────────────
section("Fixture 6: loaders/ears_format.py -- real pre-fix (git HEAD) vs real post-fix (working tree)")

pre_ears = git_show("HEAD", "loaders/ears_format.py")
post_ears = open("loaders/ears_format.py").read()

# Pre-fix: HEAD's real PROP_UNIT_UPSERT_SQL / PROP_UNIT_TAX_YEAR_UPSERT_SQL
# have no county_code in their INSERT column lists and
# ON CONFLICT (prop_id) / (prop_id, tax_year) with no county_code.
for table in ("prop_unit", "prop_unit_tax_year"):
    pre_3b = findings_for(pre_ears, "loaders/ears_format.py", table, "3b")
    pre_3c = findings_for(pre_ears, "loaders/ears_format.py", table, "3c")
    check(f"pre-fix (real HEAD): at least one 3b finding for {table}",
          len(pre_3b) >= 1)
    check(f"pre-fix (real HEAD): that 3b finding is FAIL ({table})",
          any(f.severity == "FAIL" for f in pre_3b))
    check(f"pre-fix (real HEAD): at least one 3c finding for {table}",
          len(pre_3c) >= 1)
    check(f"pre-fix (real HEAD): that 3c finding is FAIL ({table})",
          any(f.severity == "FAIL" for f in pre_3c))

    post_3b = findings_for(post_ears, "loaders/ears_format.py", table, "3b")
    post_3c = findings_for(post_ears, "loaders/ears_format.py", table, "3c")
    check(f"post-fix (real working tree): 3b has no FAIL for {table}",
          len(post_3b) >= 1 and not any(f.severity == "FAIL" for f in post_3b))
    check(f"post-fix (real working tree): 3c has no FAIL for {table}",
          len(post_3c) >= 1 and not any(f.severity == "FAIL" for f in post_3c))


# ─────────────────────────────────────────────────────────────────────────
# Cross-check against the tool's own real, full-repo run: both files must
# not appear in the failures list at all anymore.
# ─────────────────────────────────────────────────────────────────────────
section("Cross-check: real full-repo audit run, both files fully clean")

result = vcs.run_audit()
findings_in_scope = [
    f for f in result["findings"]
    if f.filepath in ("parcel_rollup.py", "loaders/ears_format.py")
]
fails_in_scope = [f for f in findings_in_scope if f.severity == "FAIL"]
check("real full-repo audit: parcel_rollup.py has zero FAIL findings",
      not any(f.filepath == "parcel_rollup.py" for f in fails_in_scope))
check("real full-repo audit: loaders/ears_format.py has zero FAIL findings",
      not any(f.filepath == "loaders/ears_format.py" for f in fails_in_scope))
check("real full-repo audit: both files produced at least some PASS findings (proves they were scanned, not silently skipped)",
      any(f.severity == "PASS" for f in findings_in_scope))


print(f"\n{'=' * 78}")
print("ALL PASS" if all_ok else "SOME FAILED")
print(f"{'=' * 78}")
raise SystemExit(0 if all_ok else 1)
