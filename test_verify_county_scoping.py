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
section("Fixture 5: parcel_rollup.py -- real pre-fix vs real post-fix (working tree)")

# PX-20260823-02: pinned to the real immutable pre-fix commit (f72532f^,
# the parent of the PARCEL-ROLLUP-HOTFIX-1 fix commit itself), NOT the
# moving "HEAD" ref this fixture originally used. HEAD was a valid
# pre-fix snapshot only in the window between when this fixture was
# authored and when the fix commit (f72532f) landed as HEAD -- once
# committed, "git show HEAD:parcel_rollup.py" silently started returning
# the POST-fix content, which would have made every "pre-fix" assertion
# below false (verified: f72532f actually IS the fix commit for this
# file; f72532f^ genuinely lacks county_code in ROLLUP_SQL's INSERT/ON
# CONFLICT, confirmed by direct git show). A commit hash is immutable;
# HEAD is not -- pin fixtures like this to the hash.
pre_rollup = git_show("f72532f^", "parcel_rollup.py")
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
section("Fixture 6: loaders/ears_format.py -- real pre-fix vs real post-fix (working tree)")

# PX-20260823-02: same fix as Fixture 5 above -- pinned to the real
# immutable pre-fix commit (f72532f^) instead of the moving "HEAD" ref.
pre_ears = git_show("f72532f^", "loaders/ears_format.py")
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


# ─────────────────────────────────────────────────────────────────────────
# Fixture 7: EXEMPTIONS registry — a registered exemption converts a
# matching FAIL to EXEMPT (PX-20260823-02 Part 1).
# ─────────────────────────────────────────────────────────────────────────
section("Fixture 7: EXEMPTIONS registry converts a matching FAIL to EXEMPT")

_real_exemptions = dict(vcs.EXEMPTIONS)  # save, restore at the end -- never leave test state behind

try:
    # A synthetic FAIL finding, built the same way audit_extracted() would
    # produce one (not hand-typed to look like a Finding, actually IS one).
    fake_fail = vcs.Finding(
        filepath="loaders/fake_test_loader.py",
        lineno=42,
        table="parcel",
        stmt_kind="DELETE",
        rule="3d",
        severity="FAIL",
        detail="No county_code reference found in this UPDATE/DELETE's WHERE clause.",
    )
    fake_pass = vcs.Finding(
        filepath="loaders/fake_test_loader.py",
        lineno=43,
        table="parcel",
        stmt_kind="DELETE",
        rule="3a",
        severity="PASS",
        detail="loaders/fake_test_loader.py is a registered writer.",
    )

    vcs.EXEMPTIONS = {
        ("loaders/fake_test_loader.py", "parcel", "DELETE"): {
            "reason": "synthetic test exemption -- proves the conversion mechanism, not a real loader.",
            "approved_by": "PX-TEST-FIXTURE-7",
        },
    }

    converted = vcs._apply_exemptions([fake_fail, fake_pass])
    exempted = [f for f in converted if f.rule == "3d"]
    untouched = [f for f in converted if f.rule == "3a"]

    check("EXEMPT conversion: exactly one finding still has rule 3d", len(exempted) == 1)
    check("EXEMPT conversion: that finding's severity is now EXEMPT (not FAIL)",
          len(exempted) == 1 and exempted[0].severity == "EXEMPT")
    check("EXEMPT conversion: detail carries the registered reason",
          len(exempted) == 1 and "synthetic test exemption" in exempted[0].detail)
    check("EXEMPT conversion: detail carries the approved_by brief ID",
          len(exempted) == 1 and "PX-TEST-FIXTURE-7" in exempted[0].detail)
    check("EXEMPT conversion: detail still preserves the original finding text",
          len(exempted) == 1 and "No county_code reference found" in exempted[0].detail)
    check("EXEMPT conversion: an unrelated PASS finding (different rule/key) is untouched",
          len(untouched) == 1 and untouched[0].severity == "PASS")
    check("EXEMPT conversion: no stale-exemption finding fires when the registry key WAS matched",
          not any(f.rule == "exempt-stale" for f in converted))
finally:
    vcs.EXEMPTIONS = _real_exemptions


# ─────────────────────────────────────────────────────────────────────────
# Fixture 8: EXEMPTIONS registry — a registered exemption matching ZERO
# findings becomes a loud FAIL ("stale exemption"), per Law 3
# (PX-20260823-02 Part 1).
# ─────────────────────────────────────────────────────────────────────────
section("Fixture 8: a stale EXEMPTIONS entry (matches nothing) fires as a loud FAIL")

_real_exemptions = dict(vcs.EXEMPTIONS)

try:
    vcs.EXEMPTIONS = {
        ("loaders/nonexistent_loader.py", "parcel", "UPDATE"): {
            "reason": "synthetic test exemption for code that doesn't exist -- proves stale-exemption detection.",
            "approved_by": "PX-TEST-FIXTURE-8",
        },
    }

    # No findings at all reference this key -- the registry entry is stale
    # from the moment it's registered, same as it would be if the real
    # finding it was written for got fixed/deleted without updating the
    # registry.
    result = vcs._apply_exemptions([])
    stale = [f for f in result if f.rule == "exempt-stale"]

    check("stale exemption: exactly one stale-exemption finding is produced", len(stale) == 1)
    check("stale exemption: its severity is FAIL (loud, not silently dropped)",
          len(stale) == 1 and stale[0].severity == "FAIL")
    check("stale exemption: detail names the unmatched file",
          len(stale) == 1 and "loaders/nonexistent_loader.py" in stale[0].detail)
    check("stale exemption: detail says STALE EXEMPTION explicitly",
          len(stale) == 1 and "STALE EXEMPTION" in stale[0].detail)
    check("stale exemption: detail carries the approved_by brief ID",
          len(stale) == 1 and "PX-TEST-FIXTURE-8" in stale[0].detail)
finally:
    vcs.EXEMPTIONS = _real_exemptions


# ─────────────────────────────────────────────────────────────────────────
# Cross-check: the REAL, currently-registered EXEMPTIONS dict, run against
# the real full-repo audit -- every registered entry must actually match a
# real finding (i.e. the production registry itself has zero stale entries
# right now). This is the "tested alarm" proof for the real registry, not
# just the mechanism.
# ─────────────────────────────────────────────────────────────────────────
section("Cross-check: real EXEMPTIONS registry has zero stale entries against the real repo")

real_result = vcs.run_audit()
real_stale = [f for f in real_result["findings"] if f.rule == "exempt-stale"]
real_exempt = [f for f in real_result["findings"] if f.severity == "EXEMPT"]
check(f"real audit: zero stale-exemption findings ({len(real_stale)} found)",
      len(real_stale) == 0)
check(f"real audit: every registered EXEMPTIONS key produced at least one EXEMPT finding "
      f"({len(real_exempt)} EXEMPT findings from {len(vcs.EXEMPTIONS)} registry entries)",
      len(real_exempt) >= len(vcs.EXEMPTIONS))
check("real audit: zero remaining FAIL findings (every 3d/3b/3c gap is either fixed or registered)",
      not any(f.severity == "FAIL" for f in real_result["findings"]))


print(f"\n{'=' * 78}")
print("ALL PASS" if all_ok else "SOME FAILED")
print(f"{'=' * 78}")
raise SystemExit(0 if all_ok else 1)
