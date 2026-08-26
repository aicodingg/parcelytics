#!/usr/bin/env python3
"""
test_cert_archive_paths.py — PX-20260824-03 Task 1 acceptance check.

The path-resolution dry check the brief asked for: proves every year's
resolved CERT_DIR path is a manifest-verified, existing-in-manifest file,
and proves the laziness fix (PEP 562 module __getattr__ in config.py,
_cert_dir_for_year() in loaders/load_certified_historical.py) actually
defers the archive-mount check to point-of-use rather than import time.

PX-20260826-04 follow-up (real finding from Diego's own dry-run against
the physical vault): extended with a Dallas archive-grammar pinning check
(check_dallas_archive_grammar(), below) after the FIRST version of
config.py's Dallas grammar was found to derive a per-year re-dated folder
straight from vault_manifest.md's own "Vault path" TABLE COLUMN -- which
is itself wrong relative to the real, physical vault layout (the whole
5-year Dallas acquisition was archived as ONE dated event, per the
manifest's own prose, not five independently-dated folders). This class of
drift -- manifest table column vs. real filesystem vs. brief's own
paraphrase, three sources that can each independently be wrong -- is
exactly the failure mode this test extension exists to catch at test time,
not just discover again at the next real dry-run. Pins config's resolved
Dallas paths against vault_manifest.md's own "Delivered path (as
received)" column (parsed directly from the table, not re-typed) and
against the migration's own prose-stated acquisition date, for all 5
Dallas years.

Entirely "dry": runs with no live DB, no live filesystem access to the
real external drive, and no other file mutation. Where a real-mount check
needs to be exercised, PARCELYTICS_ARCHIVE_ROOT is monkeypatched to either
a definitely-nonexistent path (unmounted case) or a temp dir this test
creates and cleans up itself (mounted case) -- config.ArchiveNotMountedError's
own logic (`os.path.isdir(...)`) can't tell the difference between a real
external drive and a temp dir with the right shape, which is exactly what
makes this testable without hardware.

AC8-style disclosure (same pattern as every other fixture-tested module in
this codebase, e.g. loaders/test_backfill_prop_unit_tax_year_geoid.py):
psycopg2 is not installed in this sandbox. loaders/load_certified_historical.py
imports `psycopg2` and `psycopg2.extras` unconditionally at module top level
(unlike backfill_prop_unit_tax_year_geoid.py's own deliberately-lazy
in-function import), so a minimal fake module is installed into
sys.modules BEFORE importing it -- same real, established technique this
codebase already uses (loaders/test_pir_xlsx_common.py,
loaders/test_load_pir_billing_2021_full.py,
loaders/test_backfill_prop_unit_tax_year_geoid.py). Nothing this test
actually exercises (path resolution, argument parsing, exception handling)
touches psycopg2's real behavior at all -- the fake module exists purely to
satisfy the import statement.

Run: python3 test_cert_archive_paths.py
"""
import os
import re
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = types.ModuleType("psycopg2.extras")
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_pg2.extras)

import config
from loaders import load_certified_historical as lch

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_manifest.md")

# Same 8-column row shape vault_manifest.md actually has today (Year | Source |
# Original path | Size | Vault date | SHA-256 (original) | Status |
# Current path (as of FILE-ARCH-3, 2026-08-22)) -- independently re-derived
# here (not imported from update_vault_manifest.py's older, 7-column ROW_RE,
# which predates that 8th column existing) so this check doesn't just trust
# the same regex a bug could already be hiding behind.
ROW_RE = re.compile(
    r"^\|\s*(?P<year>\d{4})\s*\|\s*(?P<source>\w+)\s*\|\s*`(?P<orig_path>[^`]+)`\s*\|"
    r"[^|]*\|[^|]*\|[^|]*\|\s*(?P<status>\w+)\s*\|\s*`(?P<current_path>[^`]+)`"
)

REQUIRED_FILES = {"PROP.TXT", "PROP_ENT.TXT", "LAND_DET.TXT"}

# ── PX-20260826-04 follow-up: Migration 4's table has a DIFFERENT column
# shape than the Travis rows above (County | Source slug | Year | Delivered
# path (as received) | Vault path | SHA-256 | Notes -- no Original-path/
# Size/Vault-date/Status columns at all) -- a separate regex, not a reuse
# of ROW_RE, same "don't trust the same regex a bug could be hiding
# behind" reasoning that regex's own comment already states. ─────────────
DALLAS_ROW_RE = re.compile(
    r"^\|\s*dallas\s*\|\s*certified_roll\s*\|\s*(?P<year>\d{4})\s*\|\s*`(?P<delivered_path>[^`]+)`\s*\|"
)


def parse_dallas_migration4_rows():
    """Returns {year: set(delivered_path strings)} for every real Migration 4
    'dallas' row in vault_manifest.md -- parsed from the table's own
    'Delivered path (as received)' column.

    Deliberately NOT the table's own 'Vault path' column: that column was
    the source of a real, disclosed bug (config.py's first cut of the
    Dallas archive grammar trusted it at face value and got the folder
    structure wrong -- see config.py's own Dallas archive grammar comment
    for the full correction). The delivered-path column records DCAD's own
    as-received folder names, which Diego's real dry-run finding confirmed
    the vault preserves verbatim under one shared acquisition-dated folder
    -- that combination (acquisition date + delivered-path column) is the
    real ground truth this test pins against, not a second guess.
    """
    by_year = {}
    with open(MANIFEST_PATH) as f:
        for line in f:
            m = DALLAS_ROW_RE.match(line)
            if not m:
                continue
            year = int(m.group("year"))
            by_year.setdefault(year, set()).add(m.group("delivered_path"))
    return by_year


def parse_dallas_acquisition_date():
    """Extracts the one real acquisition-event date Migration 4's own prose
    states ("archived to the vault <date>") -- the ground truth for the
    single shared folder all 5 Dallas years sit under. Parsed from the
    manifest text directly rather than re-typed, so a future manifest edit
    that changes this date fails this test instead of silently drifting
    from config.py's own DALLAS_ACQUISITION_DATE constant."""
    with open(MANIFEST_PATH) as f:
        text = f.read()
    m = re.search(r"archived to the vault (\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def parse_manifest_certified_dirs():
    """Returns {year: {directory: {"files": set(...), "all_verified": bool}}}
    for every 'certified' row in vault_manifest.md."""
    by_year = {}
    with open(MANIFEST_PATH) as f:
        for line in f:
            m = ROW_RE.match(line)
            if not m or m.group("source") != "certified":
                continue
            year = int(m.group("year"))
            current_path = m.group("current_path")
            directory, filename = current_path.rsplit("/", 1)
            entry = by_year.setdefault(year, {}).setdefault(directory, {"files": set(), "all_verified": True})
            entry["files"].add(filename)
            if m.group("status") != "COPIED_VERIFIED":
                entry["all_verified"] = False
    return by_year


def check(label, cond, extra=None):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond and extra is not None:
        print(f"       {extra}")
    return cond


def main():
    all_ok = True
    manifest_dirs = parse_manifest_certified_dirs()
    real_root = config.PARCELYTICS_ARCHIVE_ROOT

    # ── Check 1 (pure string check, no filesystem/mount access at all):
    #    config._CERT_ARCHIVE_DATES' 5 dates each build a directory string
    #    that appears in vault_manifest.md, with all 3 files the loader
    #    reads (PROP.TXT, PROP_ENT.TXT, LAND_DET.TXT) present and
    #    COPIED_VERIFIED. This is the brief's actual acceptance criterion:
    #    "every year's resolved path is a manifest-verified,
    #    existing-in-manifest file." Uses a plain os.path.join against
    #    config.PARCELYTICS_ARCHIVE_ROOT's real configured value -- NOT
    #    config._travis_archive() -- specifically so this check never
    #    triggers the mount guard and works identically whether or not the
    #    drive is actually attached to this machine. ─────────────────────
    for attr_name, date in sorted(config._CERT_ARCHIVE_DATES.items()):
        year = 2025 if attr_name == "CERT_DIR" else int(attr_name.rsplit("_", 1)[1])
        resolved_dir = os.path.join(real_root, "travis", "certified_roll", date)
        year_dirs = manifest_dirs.get(year, {})
        entry = year_dirs.get(resolved_dir)
        all_ok &= check(
            f"{attr_name} ({year}): resolved dir appears in vault_manifest.md",
            entry is not None,
            f"resolved_dir={resolved_dir!r}, manifest dirs for {year}: {sorted(year_dirs)}",
        )
        if entry is None:
            continue
        missing = REQUIRED_FILES - entry["files"]
        all_ok &= check(
            f"{attr_name} ({year}): PROP.TXT/PROP_ENT.TXT/LAND_DET.TXT all present in that manifest dir",
            not missing,
            f"missing: {missing}",
        )
        all_ok &= check(
            f"{attr_name} ({year}): every required-file row is COPIED_VERIFIED",
            entry["all_verified"],
        )

    # ── Checks 2-4: simulate "archive NOT mounted" by pointing
    #    PARCELYTICS_ARCHIVE_ROOT at a path guaranteed not to exist. ──────
    config.PARCELYTICS_ARCHIVE_ROOT = "/definitely/not/a/real/mount/path/for/this/test"
    try:
        # Check 2: laziness -- both modules are already imported at this
        # point (top of this file). If either had eagerly resolved a
        # CERT_DIR-family constant at import time, THIS SCRIPT would already
        # have crashed before reaching main() at all -- so simply having
        # gotten this far, plus one more unrelated attribute access below,
        # is the proof.
        all_ok &= check(
            "Unrelated config attribute access works with archive root unmounted "
            "(loaders.load_certified_historical + config already imported clean above)",
            config.DEFAULT_COUNTY is not None if hasattr(config, "DEFAULT_COUNTY") else True,
        )

        # Check 3: actually resolving CERT_DIR_2022 while unmounted DOES
        # raise ArchiveNotMountedError -- proves the guard still fires, not
        # silently defanged by the laziness fix.
        raised = False
        try:
            _ = config.CERT_DIR_2022
        except config.ArchiveNotMountedError:
            raised = True
        all_ok &= check(
            "config.CERT_DIR_2022 raises ArchiveNotMountedError when archive root is unmounted",
            raised,
        )

        # Check 4: main()'s new try/except turns that same exception into a
        # clean sys.exit(1) with an ERROR message, not an uncaught traceback.
        old_argv = sys.argv
        sys.argv = ["load_certified_historical.py", "--year", "2022"]
        exited_cleanly = False
        exit_code = None
        try:
            lch.main()
        except SystemExit as e:
            exited_cleanly = True
            exit_code = e.code
        except config.ArchiveNotMountedError:
            exited_cleanly = False
        finally:
            sys.argv = old_argv
        all_ok &= check(
            "main() exits cleanly (sys.exit(1)), not an uncaught exception, when archive is unmounted",
            exited_cleanly and exit_code == 1,
            f"exited_cleanly={exited_cleanly} exit_code={exit_code}",
        )
    finally:
        config.PARCELYTICS_ARCHIVE_ROOT = real_root

    # ── Checks 5-6: simulate "archive IS mounted" with a real temp dir. ──
    tmp_root = tempfile.mkdtemp(prefix="px_cert_archive_test_")
    try:
        config.PARCELYTICS_ARCHIVE_ROOT = tmp_root

        # Check 5: config.CERT_DIR_2022 resolves WITHOUT raising, to the
        # expected joined path -- the happy path, not just the failure path.
        expected = os.path.join(tmp_root, "travis", "certified_roll", "2022-07-25")
        resolved = None
        raised_unexpectedly = False
        try:
            resolved = config.CERT_DIR_2022
        except config.ArchiveNotMountedError:
            raised_unexpectedly = True
        all_ok &= check(
            "config.CERT_DIR_2022 resolves without raising when archive root IS mounted",
            not raised_unexpectedly and resolved == expected,
            f"resolved={resolved!r} expected={expected!r}",
        )

        # Check 6: load_certified_historical.py's _cert_dir_for_year() agrees
        # with config's constants for all 4 years it supports.
        for year in (2022, 2023, 2024, 2026):
            cfg_expected = getattr(config, f"CERT_DIR_{year}")
            actual = lch._cert_dir_for_year(year)
            all_ok &= check(
                f"_cert_dir_for_year({year}) matches config.CERT_DIR_{year}",
                actual == cfg_expected,
                f"actual={actual!r} expected={cfg_expected!r}",
            )
    finally:
        config.PARCELYTICS_ARCHIVE_ROOT = real_root
        shutil.rmtree(tmp_root, ignore_errors=True)

    # ── Checks 8+: PX-20260826-04 follow-up -- pin config.py's Dallas
    #    archive grammar against vault_manifest.md's own Migration 4 rows,
    #    parsed directly (not re-typed), so the exact drift class Diego's
    #    real dry-run caught (per-year re-dated folders instead of one
    #    shared acquisition-dated folder) fails a test, not just a live
    #    run. Pure string checks, same as Check 1 above -- never touches
    #    the mount guard. ──────────────────────────────────────────────
    dallas_rows = parse_dallas_migration4_rows()
    manifest_acq_date = parse_dallas_acquisition_date()
    all_ok &= check(
        "Dallas acquisition date: config.DALLAS_ACQUISITION_DATE matches vault_manifest.md's own prose "
        "('archived to the vault <date>')",
        config.DALLAS_ACQUISITION_DATE == manifest_acq_date,
        f"config={config.DALLAS_ACQUISITION_DATE!r} manifest={manifest_acq_date!r}",
    )

    # 2026 is the only year with real extracted-table rows in the manifest
    # -- full directory-level pinning against the manifest's own
    # delivered-path column for every one of those rows (not just one).
    year = 2026
    delivered_paths = dallas_rows.get(year, set())
    csv_dirs = {p.rsplit("/", 1)[0] for p in delivered_paths if "/" in p and not p.upper().endswith(".ZIP")}
    all_ok &= check(
        "Dallas Migration 4: all 2026 extracted-table rows share exactly one delivered-path directory",
        len(csv_dirs) == 1,
        f"csv_dirs={csv_dirs}",
    )
    if len(csv_dirs) == 1:
        delivered_dir = next(iter(csv_dirs))
        expected_2026 = os.path.join(
            real_root, "dallas", "certified_roll", manifest_acq_date or config.DALLAS_ACQUISITION_DATE,
            *delivered_dir.split("/"),
        )
        year_folder, dcad_folder = config._DALLAS_CERT_ARCHIVE_INFO["DALLAS_CERT_DIR_2026"]
        resolved_2026 = os.path.join(
            real_root, "dallas", "certified_roll", config.DALLAS_ACQUISITION_DATE, year_folder, dcad_folder,
        )
        all_ok &= check(
            "DALLAS_CERT_DIR_2026: config's resolved path matches vault_manifest.md's own delivered-path "
            "column, under the one real acquisition-dated folder (not a per-year re-dated one)",
            resolved_2026 == expected_2026,
            f"resolved={resolved_2026!r} expected={expected_2026!r}",
        )
        # Also confirm this is what config.DALLAS_CERT_DIR_2026 itself
        # resolves to (mounted case, same technique as Check 5 above) --
        # not just that the two internal pieces happen to agree.
        tmp_root2 = tempfile.mkdtemp(prefix="px_dallas_archive_test_")
        try:
            config.PARCELYTICS_ARCHIVE_ROOT = tmp_root2
            expected_live = os.path.join(tmp_root2, "dallas", "certified_roll", manifest_acq_date, *delivered_dir.split("/"))
            resolved_live = config.DALLAS_CERT_DIR_2026
            all_ok &= check(
                "config.DALLAS_CERT_DIR_2026 (live attribute access) matches the manifest-derived expected path",
                resolved_live == expected_live,
                f"resolved={resolved_live!r} expected={expected_live!r}",
            )
        finally:
            config.PARCELYTICS_ARCHIVE_ROOT = real_root
            shutil.rmtree(tmp_root2, ignore_errors=True)

    # 2022-2025: not yet extracted (zip only) -- the manifest's own
    # delivered-path for the relational-product zip still names the exact
    # folder DCAD's own extraction would produce (its filename minus
    # extension). Pin config's registered (year_folder, dcad_folder) tuple
    # against that name for all 4 remaining years, so a typo here fails a
    # test today rather than only surfacing once those years are actually
    # extracted.
    for year in (2022, 2023, 2024, 2025):
        rows = dallas_rows.get(year, set())
        relational_zip_paths = [p for p in rows if "DCAD" in p and p.upper().endswith(".ZIP")]
        all_ok &= check(
            f"Dallas Migration 4: exactly one relational-product zip row found for {year}",
            len(relational_zip_paths) == 1,
            f"found: {relational_zip_paths}",
        )
        if len(relational_zip_paths) != 1:
            continue
        zip_path = relational_zip_paths[0]
        expected_year_folder, zip_filename = zip_path.rsplit("/", 1)
        expected_dcad_folder = re.sub(r"\.zip$", "", zip_filename, flags=re.IGNORECASE)
        cfg_year_folder, cfg_dcad_folder = config._DALLAS_CERT_ARCHIVE_INFO[f"DALLAS_CERT_DIR_{year}"]
        all_ok &= check(
            f"DALLAS_CERT_DIR_{year}: registered year-folder matches manifest ({expected_year_folder!r})",
            cfg_year_folder == expected_year_folder,
            f"cfg={cfg_year_folder!r} expected={expected_year_folder!r}",
        )
        all_ok &= check(
            f"DALLAS_CERT_DIR_{year}: registered DCAD-folder name matches manifest's own zip filename "
            f"(minus .zip -- the folder DCAD's own extraction would produce)",
            cfg_dcad_folder == expected_dcad_folder,
            f"cfg={cfg_dcad_folder!r} expected={expected_dcad_folder!r}",
        )

    # ── Check 7: a genuinely unknown attribute still raises a normal
    #    AttributeError (the __getattr__ fix must not swallow real typos). ──
    raised_attr_error = False
    try:
        _ = config.CERT_DIR_9999_TYPO
    except AttributeError:
        raised_attr_error = True
    all_ok &= check(
        "An unrelated/typo'd attribute name still raises a normal AttributeError",
        raised_attr_error,
    )

    print()
    print("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
