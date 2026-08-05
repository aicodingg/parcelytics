#!/usr/bin/env python3
"""
test_vault_backfill.py — fixture tests for vault_backfill.py's real,
load-bearing logic: the disk-space guard (must refuse loudly, not fail
mid-copy) and the copy+verify integrity check (must catch a real
mismatch, not just the happy path). Uses synthetic small files in a
temp directory -- does not touch the real multi-GB source exports.

Run: python3 test_vault_backfill.py
"""
import hashlib
import os
import shutil
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


def test_disk_space_guard_refuses_when_insufficient():
    from vault_backfill import check_disk_space
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "vault")
        os.makedirs(target)
        # Ask for an absurd amount (more than any real disk has) -- must refuse.
        ok, free = check_disk_space(10 ** 18, target)
        return check("disk-space guard refuses an impossibly large request", ok is False, f"ok={ok}")


def test_disk_space_guard_allows_when_sufficient():
    from vault_backfill import check_disk_space
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "vault")
        os.makedirs(target)
        ok, free = check_disk_space(1, target)  # 1 byte -- any real disk has this
        return check("disk-space guard allows a trivially small request", ok is True, f"ok={ok}, free={free}")


def test_sha256_of_matches_hashlib_directly():
    from vault_backfill import sha256_of
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "sample.txt")
        content = b"the quick brown fox jumps over the lazy dog" * 1000
        with open(p, "wb") as f:
            f.write(content)
        expected = hashlib.sha256(content).hexdigest()
        got = sha256_of(p)
        return check("sha256_of() matches hashlib computed directly", got == expected, f"{got} != {expected}")


def test_build_manifest_report_mode_no_writes():
    """--report mode (do_copy=False) must not create the vault directory at all."""
    from vault_backfill import gather_sources
    import vault_backfill as vb
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "PROP.TXT"), "wb") as f:
            f.write(b"fake prop data")
        vault_dir = os.path.join(tmp, "vault")

        orig_gather = vb.gather_sources
        vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
        try:
            rows, total = vb.build_manifest(vault_dir, do_copy=False)
        finally:
            vb.gather_sources = orig_gather

        ok = check("report mode found the fake source file", len(rows) == 1 and rows[0]["status"] == "CHECKSUMMED_NOT_COPIED")
        ok = check("report mode did NOT create the vault directory", not os.path.exists(vault_dir)) and ok
        ok = check("report mode's total_bytes matches the fake file's real size",
                    total == len(b"fake prop data"), f"got {total}") and ok
        return ok


def test_build_manifest_copy_mode_verifies_checksum():
    """--copy mode must actually copy the file, and the copy's checksum
    must match the original -- this is the real integrity check
    DATA_LIFECYCLE.md Principle 6 asks for."""
    import vault_backfill as vb
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
        os.makedirs(src_dir)
        content = b"real integrity check content, not a placeholder"
        with open(os.path.join(src_dir, "PROP.TXT"), "wb") as f:
            f.write(content)
        vault_dir = os.path.join(tmp, "vault")

        orig_gather = vb.gather_sources
        vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
        try:
            rows, total = vb.build_manifest(vault_dir, do_copy=True)
        finally:
            vb.gather_sources = orig_gather

        row = rows[0]
        ok = check("copy mode status is COPIED_VERIFIED", row["status"] == "COPIED_VERIFIED", row["status"])
        ok = check("copy mode: original and copy checksums match",
                    row["sha256_original"] == row["sha256_copy"]) and ok
        ok = check("copy mode: the vault file actually exists on disk",
                    os.path.exists(row["vault_path"])) and ok
        with open(row["vault_path"], "rb") as f:
            ok = check("copy mode: the vault file's real content matches the source",
                        f.read() == content) and ok
        return ok


def test_build_manifest_copy_mode_catches_a_real_mismatch():
    """Deliberate-corruption test: if the copy on disk is tampered with
    AFTER copy2() but its checksum is recomputed, the mismatch must be
    caught. Simulated by monkeypatching sha256_of to return a different
    value on the second (post-copy) call -- proving the comparison logic
    itself, not just the happy path, actually fires."""
    import vault_backfill as vb
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "PROP.TXT"), "wb") as f:
            f.write(b"original content")
        vault_dir = os.path.join(tmp, "vault")

        orig_gather = vb.gather_sources
        orig_sha = vb.sha256_of
        call_count = {"n": 0}

        def fake_sha256(path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "aaaa_original_checksum"
            return "bbbb_different_checksum_simulating_corruption"

        vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
        vb.sha256_of = fake_sha256
        try:
            raised = False
            try:
                vb.build_manifest(vault_dir, do_copy=True)
            except RuntimeError as e:
                raised = "Integrity check failed" in str(e)
        finally:
            vb.gather_sources = orig_gather
            vb.sha256_of = orig_sha

        return check("copy-mismatch is caught and raises RuntimeError", raised)


def test_vault_date_for_uses_mtime_not_filename():
    from vault_backfill import vault_date_for
    import time
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "some_file_named_20991231.TXT")  # misleading filename date
        with open(p, "w") as f:
            f.write("x")
        # Force a known mtime distinct from the filename's implied date.
        known_time = time.mktime((2024, 3, 15, 0, 0, 0, 0, 0, 0))
        os.utime(p, (known_time, known_time))
        got = vault_date_for(p)
        return check("vault_date_for() derives from mtime, ignoring a misleading filename date",
                     got == "2024-03-15", f"got {got}")


def test_vault_date_for_respects_override():
    from vault_backfill import vault_date_for
    got = vault_date_for("/does/not/matter.txt", date_override="2020-01-01")
    return check("vault_date_for() respects an explicit date_override", got == "2020-01-01", got)


# ── VAULT-COPY-FIX-1 (Aug 2026): shutil.copy() not copy2(), ExFAT chflags ──
#
# This sandbox's own filesystem is neither macOS nor ExFAT (Linux
# container mount) -- os.chflags doesn't even exist as an attribute on
# Linux, and shutil.copystat() silently skips the flags step when it's
# absent, so the real OSError(errno=22) from Diego's actual ExFAT drive
# cannot be organically reproduced here. Per this brief's own instruction,
# that's disclosed explicitly, not glossed over -- see the final report.
#
# What CAN be verified here, and is: (1) shutil.copy() -- what
# build_manifest() now calls -- structurally never invokes copystat() at
# all (confirmed by monkeypatching shutil.copystat to raise the EXACT real
# production error and proving a real do_copy=True run never triggers it),
# which is what actually eliminates this failure mode, on any OS/
# filesystem, not just by getting lucky on this sandbox's own filesystem;
# and (2) that the monkeypatched copystat WOULD have fired under the OLD
# copy2()-based code, reproducing the bug's real trigger condition
# one level up, proving this fixture is actually wired to the real failure
# mode and not a no-op.

def test_copy_mode_never_invokes_copystat_chflags_path():
    """The core proof: build_manifest()'s real do_copy=True path must NOT
    call shutil.copystat() (the function whose internal chflags() call is
    the actual, confirmed root cause) at all. Monkeypatches copystat to
    raise the EXACT real production error if it's ever called, then runs a
    real copy through the unmodified build_manifest() -- must succeed
    cleanly."""
    import vault_backfill as vb

    def _copystat_raises_like_exfat(*a, **kw):
        raise OSError(22, "Invalid argument")  # the exact real errno/message

    orig_copystat = shutil.copystat
    shutil.copystat = _copystat_raises_like_exfat
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "LAND_DET.TXT"), "wb") as f:
                f.write(b"real content, same filename as Diego's real failure")
            vault_dir = os.path.join(tmp, "vault")

            orig_gather = vb.gather_sources
            vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
            try:
                rows, total = vb.build_manifest(vault_dir, do_copy=True)
            finally:
                vb.gather_sources = orig_gather

        ok = check("copy mode with copystat() poisoned to fail like ExFAT: still succeeds",
                    rows[0]["status"] == "COPIED_VERIFIED", rows[0])
        return ok
    finally:
        shutil.copystat = orig_copystat


def test_old_copy2_approach_would_have_failed_this_exact_way():
    """Companion proof, one level up: confirms this fixture is actually
    wired to the real bug -- if build_manifest() still called
    shutil.copy2() (the pre-fix code), the SAME poisoned copystat() WOULD
    raise, reproducing Diego's real traceback shape. This is what makes
    the test above meaningful (proving absence of a call that would
    otherwise really fire), not a tautology."""
    def _copystat_raises_like_exfat(*a, **kw):
        raise OSError(22, "Invalid argument")

    orig_copystat = shutil.copystat
    shutil.copystat = _copystat_raises_like_exfat
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "LAND_DET.TXT")
            dst = os.path.join(tmp, "copy_of_LAND_DET.TXT")
            with open(src, "wb") as f:
                f.write(b"content")
            raised = False
            try:
                shutil.copy2(src, dst)  # the OLD approach -- must still fail here
            except OSError as e:
                raised = e.errno == 22
        return check("sanity: shutil.copy2() (old approach) really does hit the poisoned "
                      "copystat() -- proves the fixture reproduces the real failure mode",
                      raised)
    finally:
        shutil.copystat = orig_copystat


def test_copy_mode_preserves_mtime_via_explicit_os_utime():
    """VAULT-COPY-FIX-1's second half: switching away from copy2() must not
    silently drop mtime preservation -- the explicit os.utime() call
    must still leave the vault copy's mtime matching the source's,
    even though copystat() (which copy2() would have used) is never
    called."""
    import vault_backfill as vb
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
        os.makedirs(src_dir)
        src_path = os.path.join(src_dir, "PROP.TXT")
        with open(src_path, "wb") as f:
            f.write(b"content")
        # A known, distinctive mtime, far from "now" so a false pass
        # (mtime just happening to be close to copy-time) can't sneak by.
        known_time = 1_700_000_000  # 2023-11-14 -- arbitrary, just fixed+distinctive
        os.utime(src_path, (known_time, known_time))
        vault_dir = os.path.join(tmp, "vault")

        orig_gather = vb.gather_sources
        vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
        try:
            rows, total = vb.build_manifest(vault_dir, do_copy=True)
        finally:
            vb.gather_sources = orig_gather

        vault_path = rows[0]["vault_path"]
        got_mtime = int(os.path.getmtime(vault_path))
        return check("copy mode: vault copy's mtime matches the source's real mtime "
                     "(preserved via explicit os.utime(), not copystat())",
                     got_mtime == known_time, f"got {got_mtime}, expected {known_time}")


def test_checksum_verification_logic_unaffected_by_copy_function_change():
    """Explicit re-confirmation (not just 'the existing tests still pass'):
    the copy-mismatch detection -- this script's REAL integrity guarantee,
    per its own docstring -- still fires correctly after switching away
    from copy2(). Same technique as the pre-existing corruption test,
    kept as its own named assertion here so this specific requirement
    (checksum logic unaffected by the copy-function change) has a test
    that documents exactly what it's proving, not just riding along on a
    pre-existing test that was written for a different reason."""
    import vault_backfill as vb
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "src", "2099_Certified_Export")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "PROP.TXT"), "wb") as f:
            f.write(b"original content")
        vault_dir = os.path.join(tmp, "vault")

        orig_gather = vb.gather_sources
        orig_sha = vb.sha256_of
        call_count = {"n": 0}

        def fake_sha256(path):
            call_count["n"] += 1
            return "aaaa" if call_count["n"] == 1 else "bbbb_corrupted"

        vb.gather_sources = lambda only=None: [(2099, "certified", src_dir)]
        vb.sha256_of = fake_sha256
        try:
            raised = False
            try:
                vb.build_manifest(vault_dir, do_copy=True)
            except RuntimeError as e:
                raised = "Integrity check failed" in str(e)
        finally:
            vb.gather_sources = orig_gather
            vb.sha256_of = orig_sha

        return check("post-fix: checksum mismatch detection still raises RuntimeError "
                     "(the real integrity guarantee is unchanged by the copy() switch)",
                     raised)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} VAULT BACKFILL FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
