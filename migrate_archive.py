#!/usr/bin/env python3
"""
migrate_archive.py — PX-20260821-01, Archive Migration: legacy vault layout
-> slug-grammar archive structure.

WHAT THIS DOES
Copies (never moves) Travis's 11 real, known archived source-data vintages
from their legacy location on the external vault drive:

    <PARCELYTICS_ARCHIVE_ROOT>/Travis County (TX)/<year>/<certified|preliminary|ajr>/<date>/

into the new, slug-grammar structure config.py's FILE-ARCH-3 work
(_travis_archive(), commit 6f30b97) already establishes:

    <PARCELYTICS_ARCHIVE_ROOT>/travis/<source_slug>/<date>/

The legacy tree is referenced history (Fable's ruling) and is NEVER
written to, renamed, or deleted by this script -- read-only access only.

WHY shutil.copy(), NOT shutil.copy2() (this brief said "or equivalent")
This repo already has a real, hard-won finding for exactly this drive:
VAULT-COPY-FIX-1 (see vault_backfill.py's own module docstring) found
shutil.copy2()'s copystat() step calls chflags(), which fails outright on
ExFAT-formatted drives -- confirmed via diskutil against this exact
external drive. PARCELYTICS_ARCHIVE_ROOT (config.py) points at that same
physical drive, so this script would hit the identical failure if it used
copy2() literally. Uses shutil.copy() + an explicit os.utime() instead --
content + permission bits + mtime preserved, no chflags attempt, safe on
any filesystem. The real integrity guarantee (a SHA-256 re-hash-and-
compare after every copy) is unaffected by this substitution.

THE AJR -> certified_roll MAPPING (verified, not assumed, before writing
this file)
config.py's own AJR_FILES comment states plainly: "no separate Registry
row exists for AJR specifically... so the 2021-2024 AJR/EARS files...
nest under certified_roll, the same registry row, rather than inventing an
unregistered 'ajr' slug." Cross-checked independently against a live fetch
of Travis's real Source Registry (Notion): exactly 4 rows -- CAD certified
appraisal export, CAD preliminary appraisal export, Tax office billing
data, Adopted tax rates -- no AJR row exists there either. `grep -n
"\"ajr\"" config.py` finds only that one comment explaining why no such
slug exists, never a slug definition. MIGRATION_MAP below reflects this:
every legacy .../ajr/... directory maps to travis/certified_roll/<date>/,
differentiated from the same year's real certified export only by its own
distinct date subfolder (the two never collide since AJR and certified
exports for the same year always carry different real acquisition dates).

THE 2026-07-30 "superseded/" EXCEPTION
Independently confirmed (this session, prior brief) byte-identical to
2026-07-19 -- same underlying TCAD 07-19 export, re-touched/re-copied on
07-30. Per Fable's FILE-ARCH-3 ruling, kept anyway under an explicit
superseded/ subfolder rather than silently dropped: provenance means
keeping the file you didn't use, not just the one you did.

WHAT THIS SCRIPT DOES NOT TOUCH
current/, any canary/ directory (both derive from PARCELYTICS_DATA_ROOT,
a completely separate root this script never imports or references), and
the legacy Travis County (TX)/... tree (read-only: os.walk + open("rb")
only, never written to). Filesystem-only -- no DB import, no network call.

SANDBOX DISCLOSURE (read before trusting anything below)
This sandbox has no /Volumes access at all (confirmed directly this
session: `ls /Volumes` -> "No such file or directory", and a `find`
against the real vault path returns nothing reachable). Every test in
test_migrate_archive.py runs against synthetic fixture directory trees
standing in for the real vault, with PARCELYTICS_ARCHIVE_ROOT pointed at a
tempdir via the env var config.py already reads. That proves this
script's LOGIC (path mapping, dry-run/execute split, hash verification,
idempotent skip, totals reconciliation) is correct against a structurally
faithful stand-in. It does NOT prove anything about the real drive, the
real 11 directories, their real file counts/sizes, or real ExFAT behavior
on Diego's actual machine -- only Diego's own real --dry-run / --execute
runs, on his own machine, against the real mounted drive, can confirm
that.

Run (on Diego's own machine, real drive mounted):
    python3 migrate_archive.py                 (dry-run: prints plan + totals, writes nothing)
    python3 migrate_archive.py --execute        (real copy, hash-verified, skip-and-report safe to re-run)
"""
import argparse
import hashlib
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB streamed reads -- safe for multi-GB files

LEGACY_SUBDIR = "Travis County (TX)"

# One entry per real legacy vintage directory this brief names. "legacy" is
# joined under legacy_root() (Travis County (TX)/<year>/<type>/<date>/);
# "slug"/"new" are passed straight to config._travis_archive(slug, *new) --
# the same real helper FILE-ARCH-3 built, so this script never hand-builds
# an archive-side path any other way. See module docstring for why AJR
# entries carry slug="certified_roll", and for the superseded/ exception.
MIGRATION_MAP = [
    {"legacy": ("2021", "ajr", "2021-09-25"),         "slug": "certified_roll",   "new": ("2021-09-25",)},
    {"legacy": ("2022", "certified", "2022-07-25"),   "slug": "certified_roll",   "new": ("2022-07-25",)},
    {"legacy": ("2022", "ajr", "2022-09-28"),         "slug": "certified_roll",   "new": ("2022-09-28",)},
    {"legacy": ("2023", "certified", "2023-07-22"),   "slug": "certified_roll",   "new": ("2023-07-22",)},
    {"legacy": ("2023", "ajr", "2023-08-30"),         "slug": "certified_roll",   "new": ("2023-08-30",)},
    {"legacy": ("2024", "certified", "2024-08-21"),   "slug": "certified_roll",   "new": ("2024-08-21",)},
    {"legacy": ("2024", "ajr", "2024-08-28"),         "slug": "certified_roll",   "new": ("2024-08-28",)},
    {"legacy": ("2025", "certified", "2025-07-20"),   "slug": "certified_roll",   "new": ("2025-07-20",)},
    {"legacy": ("2026", "preliminary", "2026-06-12"), "slug": "preliminary_roll", "new": ("2026-06-12",)},
    {"legacy": ("2026", "certified", "2026-07-19"),   "slug": "certified_roll",   "new": ("2026-07-19",)},
    {"legacy": ("2026", "certified", "2026-07-30"),   "slug": "certified_roll",   "new": ("superseded", "2026-07-30")},
]


class ArchiveMigrationMismatchError(RuntimeError):
    """Raised whenever this script finds a real integrity problem: a fresh
    copy whose hash doesn't match its source, or a destination file that
    already existed (the skip-and-report path) whose SIZE doesn't match
    its source. Either way, a loud failure, not a note -- see module
    docstring's "any mismatch is a loud failure" requirement."""


def legacy_root():
    return os.path.join(config.PARCELYTICS_ARCHIVE_ROOT, LEGACY_SUBDIR)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root):
    """Yield every real file under `root` as (abs_path, rel_path) pairs,
    rel_path relative to `root` itself -- so any internal subdirectory
    structure inside a legacy vintage folder is preserved unchanged at the
    new destination. Dotfiles (.DS_Store etc.) skipped -- not part of the
    county's real export."""
    for r, _dirs, files in os.walk(root):
        for fn in files:
            if fn.startswith("."):
                continue
            abs_path = os.path.join(r, fn)
            yield abs_path, os.path.relpath(abs_path, root)


def build_plan():
    """Read-only. Returns a list of dicts, one per MIGRATION_MAP entry:
    {legacy_dir, new_dir, slug, missing, files: [(abs_src, rel, size)], total_bytes}.
    Does not create anything -- new_dir is a path config._travis_archive()
    computes, whether or not it exists yet on disk."""
    root = legacy_root()
    plan = []
    for entry in MIGRATION_MAP:
        legacy_dir = os.path.join(root, *entry["legacy"])
        new_dir = config._travis_archive(entry["slug"], *entry["new"])
        item = {
            "legacy_dir": legacy_dir,
            "new_dir": new_dir,
            "slug": entry["slug"],
            "files": [],
            "total_bytes": 0,
            "missing": not os.path.isdir(legacy_dir),
        }
        if not item["missing"]:
            for abs_src, rel in _iter_files(legacy_dir):
                size = os.path.getsize(abs_src)
                item["files"].append((abs_src, rel, size))
                item["total_bytes"] += size
        plan.append(item)
    return plan


def print_precommit_summary(plan):
    """Printed BEFORE any copy runs, per THE_FABLE_METHOD.md §5 -- the
    number a human checks against expectation before committing to the
    run. Returns (grand_total_files, grand_total_bytes)."""
    print("=" * 78)
    print("PRE-COMMIT SUMMARY -- review before running --execute")
    print("=" * 78)
    grand_files = 0
    grand_bytes = 0
    for item in plan:
        n = len(item["files"])
        b = item["total_bytes"]
        grand_files += n
        grand_bytes += b
        status = "MISSING (legacy dir not found)" if item["missing"] else "ok"
        print(f"  {item['legacy_dir']}")
        print(f"    -> {item['new_dir']}")
        print(f"    {n:,} file(s), {b:,} bytes ({b / 1_073_741_824:.3f} GB)  [{status}]")
    print("-" * 78)
    print(f"  GRAND TOTAL: {grand_files:,} file(s), {grand_bytes:,} bytes "
          f"({grand_bytes / 1_073_741_824:.3f} GB)")
    print("=" * 78)
    return grand_files, grand_bytes


def run_copy(plan):
    """Real copy pass. Returns (copied_files, copied_bytes, skipped_files,
    skipped_bytes). Raises ArchiveMigrationMismatchError immediately on any
    integrity problem -- never trusts a copy call's return status alone."""
    copied_files = copied_bytes = 0
    skipped_files = skipped_bytes = 0
    for item in plan:
        if item["missing"]:
            continue
        for abs_src, rel, size in item["files"]:
            dst = os.path.join(item["new_dir"], rel)
            if os.path.exists(dst):
                dst_hash = sha256_of(dst)
                src_hash = sha256_of(abs_src)
                if dst_hash != src_hash:
                    raise ArchiveMigrationMismatchError(
                        f"destination already exists but its content does not match the "
                        f"source (hash mismatch): {dst} vs {abs_src} -- refusing to treat "
                        f"this as an already-migrated file. Investigate before re-running."
                    )
                print(f"  SKIP (already present, hash matches): {dst}")
                skipped_files += 1
                skipped_bytes += size
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            src_checksum = sha256_of(abs_src)
            # shutil.copy(), not copy2() -- see module docstring
            # (VAULT-COPY-FIX-1 precedent, this exact drive).
            src_stat = os.stat(abs_src)
            shutil.copy(abs_src, dst)
            os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
            dst_checksum = sha256_of(dst)
            if dst_checksum != src_checksum:
                raise ArchiveMigrationMismatchError(
                    f"hash mismatch after copy: {dst} does not match its source "
                    f"{abs_src} ({src_checksum} != {dst_checksum}). Halting "
                    f"immediately -- do not trust this destination file."
                )
            print(f"  COPIED+VERIFIED: {abs_src} -> {dst}")
            copied_files += 1
            copied_bytes += size
    return copied_files, copied_bytes, skipped_files, skipped_bytes


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--execute", action="store_true",
        help="Actually copy files. Without this flag, runs as --dry-run: "
             "prints the full plan and pre-commit totals, writes nothing.")
    args = ap.parse_args(argv)

    # Named mount guard, at the point of use -- config.py's own
    # ArchiveNotMountedError / _require_archive_mounted() (FILE-ARCH-3).
    # Both the legacy read side and the new write side live on the same
    # physical drive, so this one check covers both.
    config._require_archive_mounted()

    plan = build_plan()
    expected_files, expected_bytes = print_precommit_summary(plan)

    missing_count = sum(1 for item in plan if item["missing"])
    if missing_count:
        print(f"\n{missing_count} of {len(plan)} legacy directories were not found "
              f"under {legacy_root()} -- see [MISSING] markers above. Confirm this "
              f"matches what you expect on the real drive before --execute.")

    if not args.execute:
        print("\nDRY RUN -- no files copied. Re-run with --execute to perform the real copy.")
        return 0

    print("\n--execute given -- starting real copy...\n")
    copied_files, copied_bytes, skipped_files, skipped_bytes = run_copy(plan)

    actual_files = copied_files + skipped_files
    actual_bytes = copied_bytes + skipped_bytes

    print("\n" + "=" * 78)
    print("RUN COMPLETE")
    print("=" * 78)
    print(f"  Copied:         {copied_files:,} file(s), {copied_bytes:,} bytes")
    print(f"  Skipped:        {skipped_files:,} file(s) (already present), {skipped_bytes:,} bytes")
    print(f"  Actual total:   {actual_files:,} file(s), {actual_bytes:,} bytes")
    print(f"  Expected total: {expected_files:,} file(s), {expected_bytes:,} bytes")

    if actual_files != expected_files or actual_bytes != expected_bytes:
        print("\n*** MISMATCH: actual totals do not match the pre-commit expectation "
              "printed above. Something changed between the plan and the copy, or "
              "there is a real bug here -- do NOT trust this run's output. ***")
        return 1

    print("\nAll totals reconcile. Legacy tree untouched; new archive structure "
          "populated per the plan above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
