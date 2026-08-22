#!/usr/bin/env python3
"""
test_migrate_archive.py — fixture tests for migrate_archive.py.

Uses synthetic directory trees standing in for the real external vault
drive (config.PARCELYTICS_ARCHIVE_ROOT pointed at a tempdir via the same
env var config.py already reads) -- never touches the real drive. See
migrate_archive.py's own "SANDBOX DISCLOSURE" for exactly what this suite
does and does not prove.

Run: python3 test_migrate_archive.py
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


def _make_fixture_vault(root, entries):
    """Build a fake legacy vault under `root`/Travis County (TX)/... per
    `entries`, a list of (year, type, date, {filename: content_bytes}).
    Returns nothing -- writes fixture files to disk."""
    for year, kind, date, files in entries:
        d = os.path.join(root, "Travis County (TX)", year, kind, date)
        os.makedirs(d, exist_ok=True)
        for fn, content in files.items():
            with open(os.path.join(d, fn), "wb") as f:
                f.write(content)


def _reload_config_with_root(tmp_root):
    """config.py reads PARCELYTICS_ARCHIVE_ROOT via os.environ.get() at
    IMPORT time, so a real re-import (not just setting the env var) is
    needed for a fresh value to take effect -- mirrors how every other
    test file in this repo that needs a different config value handles it
    (fresh sys.modules entry, not a partial reload)."""
    os.environ["PARCELYTICS_ARCHIVE_ROOT"] = tmp_root
    for mod in ("config", "migrate_archive"):
        sys.modules.pop(mod, None)
    import config  # noqa: F401
    import migrate_archive as ma
    return ma


def test_path_mapping_certified_and_ajr_and_superseded():
    """Confirms build_plan() maps every MIGRATION_MAP entry correctly,
    including the two rules this brief called out explicitly by name: AJR
    nests under certified_roll (not a phantom ajr slug), and 2026-07-30
    lands under an explicit superseded/ subfolder."""
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        _make_fixture_vault(vault, [
            ("2022", "ajr", "2022-09-28", {"227EARS092822.csv": b"ajr 2022 data"}),
            ("2026", "certified", "2026-07-19", {"PROP.TXT": b"same export content"}),
            ("2026", "certified", "2026-07-30", {"PROP.TXT": b"same export content"}),
        ])
        ma = _reload_config_with_root(vault)
        plan = ma.build_plan()

        by_legacy = {tuple(e["legacy"]): item for e, item in zip(ma.MIGRATION_MAP, plan)}

        ajr_item = by_legacy[("2022", "ajr", "2022-09-28")]
        ok = check(
            "AJR 2022 legacy dir maps to travis/certified_roll/2022-09-28 (no ajr slug)",
            ajr_item["new_dir"].endswith(os.path.join("travis", "certified_roll", "2022-09-28")),
            ajr_item["new_dir"],
        )
        ok = check("AJR mapping does not contain the string 'ajr' in its new_dir",
                    "ajr" not in ajr_item["new_dir"].split(os.sep)[-3:], ajr_item["new_dir"]) and ok

        superseded_item = by_legacy[("2026", "certified", "2026-07-30")]
        ok = check(
            "2026-07-30 maps under an explicit superseded/ subfolder",
            superseded_item["new_dir"].endswith(os.path.join("certified_roll", "superseded", "2026-07-30")),
            superseded_item["new_dir"],
        ) and ok

        primary_item = by_legacy[("2026", "certified", "2026-07-19")]
        ok = check(
            "2026-07-19 (the real, used export) is NOT under superseded/",
            "superseded" not in primary_item["new_dir"], primary_item["new_dir"],
        ) and ok
        return ok


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        _make_fixture_vault(vault, [
            ("2025", "certified", "2025-07-20", {"PROP.TXT": b"2025 certified data"}),
        ])
        ma = _reload_config_with_root(vault)

        rc = ma.main([])  # no --execute -> dry-run

        ok = check("dry-run exits 0", rc == 0, f"rc={rc}")
        new_root = os.path.join(vault, "travis")
        ok = check("dry-run creates no travis/ destination tree at all",
                    not os.path.exists(new_root)) and ok
        return ok


def test_real_run_copies_with_matching_hashes():
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        content = b"real certified export content, not a placeholder" * 100
        _make_fixture_vault(vault, [
            ("2023", "certified", "2023-07-22", {"PROP.TXT": content, "PROP_ENT.TXT": b"entity data"}),
        ])
        ma = _reload_config_with_root(vault)

        rc = ma.main(["--execute"])
        ok = check("real run exits 0", rc == 0, f"rc={rc}")

        dst_dir = os.path.join(vault, "travis", "certified_roll", "2023-07-22")
        dst_prop = os.path.join(dst_dir, "PROP.TXT")
        ok = check("destination file exists after real run", os.path.exists(dst_prop)) and ok
        with open(dst_prop, "rb") as f:
            copied = f.read()
        ok = check("copied content byte-for-byte matches source", copied == content) and ok

        src_hash = hashlib.sha256(content).hexdigest()
        dst_hash = ma.sha256_of(dst_prop)
        ok = check("sha256 of destination matches sha256 of source", src_hash == dst_hash) and ok

        legacy_prop = os.path.join(vault, "Travis County (TX)", "2023", "certified", "2023-07-22", "PROP.TXT")
        ok = check("legacy source file is untouched (still present) after copy",
                    os.path.exists(legacy_prop)) and ok
        return ok


def test_rerun_is_idempotent_skip_and_report():
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        content = b"idempotency check content"
        _make_fixture_vault(vault, [
            ("2024", "certified", "2024-08-21", {"PROP.TXT": content}),
        ])
        ma = _reload_config_with_root(vault)

        rc1 = ma.main(["--execute"])
        ok = check("first real run exits 0", rc1 == 0, f"rc={rc1}")

        # Capture the destination file's own mtime so we can prove the
        # second run genuinely skipped it (didn't re-copy/re-touch it).
        dst = os.path.join(vault, "travis", "certified_roll", "2024-08-21", "PROP.TXT")
        first_mtime = os.path.getmtime(dst)

        rc2 = ma.main(["--execute"])
        ok = check("second (re-)run also exits 0 -- no crash on already-present files",
                    rc2 == 0, f"rc={rc2}") and ok

        plan = ma.build_plan()
        copied, copied_b, skipped, skipped_b = ma.run_copy(plan)
        ok = check("a third pass reports 0 newly-copied files (all skipped)",
                    copied == 0, f"copied={copied}") and ok
        ok = check("a third pass reports the file as skipped, not silently dropped",
                    skipped == 1, f"skipped={skipped}") and ok

        second_mtime = os.path.getmtime(dst)
        ok = check("re-run did not touch/overwrite the already-present destination file",
                    first_mtime == second_mtime) and ok
        return ok


def test_precommit_totals_match_actual_totals_happy_path():
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        _make_fixture_vault(vault, [
            ("2021", "ajr", "2021-09-25", {"20210925_000416_PTD.csv": b"ajr 2021" * 50}),
            ("2026", "preliminary", "2026-06-12", {"PROP.TXT": b"prelim data" * 30}),
        ])
        ma = _reload_config_with_root(vault)

        plan = ma.build_plan()
        expected_files, expected_bytes = ma.print_precommit_summary(plan)
        copied, copied_b, skipped, skipped_b = ma.run_copy(plan)

        ok = check("expected file count matches actual (copied+skipped) file count",
                    expected_files == copied + skipped,
                    f"expected={expected_files}, actual={copied + skipped}")
        ok = check("expected byte total matches actual (copied+skipped) byte total",
                    expected_bytes == copied_b + skipped_b,
                    f"expected={expected_bytes}, actual={copied_b + skipped_b}") and ok
        return ok


def test_corrupted_destination_different_size_fails_loudly():
    """Deliberate-corruption fixture #1: a destination file already exists
    (simulating a partial/corrupted prior run) but with a DIFFERENT size
    than its source -- the skip path's hash compare must refuse this, not
    silently accept it as 'already migrated'."""
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        _make_fixture_vault(vault, [
            ("2024", "ajr", "2024-08-28", {"227EARS082824.csv": b"real full-length content here"}),
        ])
        ma = _reload_config_with_root(vault)

        # Pre-create a WRONG (truncated) destination file before any real run.
        dst_dir = os.path.join(vault, "travis", "certified_roll", "2024-08-28")
        os.makedirs(dst_dir, exist_ok=True)
        with open(os.path.join(dst_dir, "227EARS082824.csv"), "wb") as f:
            f.write(b"truncated")  # deliberately different size from the source

        plan = ma.build_plan()
        raised = False
        detail = ""
        try:
            ma.run_copy(plan)
        except ma.ArchiveMigrationMismatchError as e:
            raised = True
            detail = str(e)
        return check("different-size destination raises ArchiveMigrationMismatchError, "
                     "loudly, not silently", raised, detail)


def test_corrupted_destination_same_size_different_content_fails_loudly():
    """PX-20260821-01-rev1: the gap the PM flagged. A destination file
    already exists, is the SAME SIZE as its source (e.g. a truncated-then-
    padded write, or ExFAT weirdness on the external drive), but has
    DIFFERENT CONTENT -- this is exactly the case a size-only skip check
    would pass silently. The skip path must now hash-compare, not just
    size-compare, so this must be caught."""
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        real_content = b"real full-length content here!"  # 31 bytes
        _make_fixture_vault(vault, [
            ("2025", "certified", "2025-07-20", {"PROP.TXT": real_content}),
        ])
        ma = _reload_config_with_root(vault)

        # Pre-create a same-size-but-wrong destination file (swap one
        # character so len() matches exactly but bytes differ).
        corrupted_content = b"real full-length CONTENT here!"  # same length, different bytes
        assert len(corrupted_content) == len(real_content)
        dst_dir = os.path.join(vault, "travis", "certified_roll", "2025-07-20")
        os.makedirs(dst_dir, exist_ok=True)
        with open(os.path.join(dst_dir, "PROP.TXT"), "wb") as f:
            f.write(corrupted_content)

        plan = ma.build_plan()
        raised = False
        detail = ""
        try:
            ma.run_copy(plan)
        except ma.ArchiveMigrationMismatchError as e:
            raised = True
            detail = str(e)
        return check("same-size, different-content destination raises "
                     "ArchiveMigrationMismatchError, loudly, not silently "
                     "(the size-only check would have missed this)",
                     raised, detail)


def test_corrupted_copy_hash_mismatch_fails_loudly():
    """Deliberate-corruption fixture #2: mirrors the gate's own 'alarm must
    itself be tested' standard for the FRESH-copy path. Monkeypatches
    sha256_of to return a different value on its second call (simulating
    the destination silently getting corrupted between copy and re-hash),
    proving the hash-mismatch alarm actually fires, not just the happy
    path."""
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        _make_fixture_vault(vault, [
            ("2022", "certified", "2022-07-25", {"PROP.TXT": b"original real content"}),
        ])
        ma = _reload_config_with_root(vault)

        orig_sha = ma.sha256_of
        call_count = {"n": 0}

        def fake_sha256(path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "aaaa_source_checksum"
            return "bbbb_different_checksum_simulating_corruption"

        ma.sha256_of = fake_sha256
        try:
            plan = ma.build_plan()
            raised = False
            detail = ""
            try:
                ma.run_copy(plan)
            except ma.ArchiveMigrationMismatchError as e:
                raised = True
                detail = str(e)
        finally:
            ma.sha256_of = orig_sha

        return check("hash mismatch on a fresh copy raises ArchiveMigrationMismatchError, "
                     "loudly, not silently", raised, detail)


def test_missing_legacy_directory_is_non_fatal():
    """A vintage this brief names that isn't actually present on the real
    drive (partial mount, drive not fully populated, etc.) must be
    reported, not crash the whole run."""
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.join(tmp, "vault")
        # No fixture files at all -- every one of the 11 legacy dirs is "missing".
        os.makedirs(vault, exist_ok=True)
        ma = _reload_config_with_root(vault)

        rc = ma.main([])
        ok = check("dry-run with zero real legacy directories present still exits 0",
                    rc == 0, f"rc={rc}")
        plan = ma.build_plan()
        ok = check("every entry is correctly marked missing",
                    all(item["missing"] for item in plan)) and ok
        return ok


def test_mount_guard_fires_when_archive_root_absent():
    """The named mount guard (config.ArchiveNotMountedError) must fire
    when PARCELYTICS_ARCHIVE_ROOT itself doesn't exist -- not a bare
    FileNotFoundError three directories deep."""
    with tempfile.TemporaryDirectory() as tmp:
        nonexistent = os.path.join(tmp, "this_path_does_not_exist_at_all")
        os.environ["PARCELYTICS_ARCHIVE_ROOT"] = nonexistent
        for mod in ("config", "migrate_archive"):
            sys.modules.pop(mod, None)
        import config
        import migrate_archive as ma

        raised = False
        try:
            ma.main([])
        except config.ArchiveNotMountedError:
            raised = True
        return check("main() raises config.ArchiveNotMountedError when the "
                     "archive root isn't mounted (not a bare FileNotFoundError)",
                     raised)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} MIGRATE_ARCHIVE FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
