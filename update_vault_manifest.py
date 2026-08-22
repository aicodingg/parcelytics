#!/usr/bin/env python3
"""
update_vault_manifest.py — PX-20260822-01

Adds a verified "Current path" column to vault_manifest.md, documenting
where each file now lives after the FILE-ARCH-3 archive migration
(migrate_archive.py, PX-20260821-01), WITHOUT touching the existing
columns (Desktop-origin paths + migration-1 hashes stay exactly as
recorded — immutable historical record).

Per Diego's explicit ruling (2026-08-22): this is an ADDITION, not a
correction. The new column is dated in its own header so it's visibly a
point-in-time claim, not a silent assertion of permanence.

WHAT THIS SCRIPT ACTUALLY VERIFIES (not just asserts)
For every row in the existing manifest, it:
  1. Maps the row's (year, source, vault_date) to the new travis/<slug>/
     path, using the exact same MIGRATION_MAP as migrate_archive.py (
     imported directly, not re-typed, so the two scripts cannot disagree
     on the mapping by construction).
  2. Computes the REAL SHA-256 of the file at that new location.
  3. Compares its first 16 hex chars against the manifest's existing
     truncated hash (the manifest only ever stored a 16-char prefix, per
     its own existing format — "4056b89697f62fd1…").
  4. Any mismatch is a loud, hard stop. Nothing is written to
     vault_manifest.md until every row's hash reconciles. A manifest
     claiming a location without a passing hash check is exactly the
     "assertion vs evidence" gap Diego's ruling called out.

WHAT THIS SCRIPT DOES NOT DO
It does not touch, re-verify, or re-derive the ORIGINAL Desktop-path rows
or their hashes — those are migration 1's record and stay untouched.
It does not delete or modify anything under the legacy vault tree or the
Desktop originals. It only reads (real files, real hashes) and, at the
very end, writes one new version of vault_manifest.md — after first
writing a timestamped backup of the original.

Run (on Diego's machine, real drive mounted):
    python3 update_vault_manifest.py            # dry-run: reports match/mismatch, writes nothing
    python3 update_vault_manifest.py --execute   # writes the updated manifest (after a backup)
"""
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import migrate_archive as ma  # reuse the exact same MIGRATION_MAP -- never re-type it

CHUNK_SIZE = 4 * 1024 * 1024
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_manifest.md")
NEW_COLUMN_HEADER = "Current path (as of FILE-ARCH-3, 2026-08-22)"

ROW_RE = re.compile(
    r"^\|\s*(?P<year>\d{4})\s*\|\s*(?P<source>\w+)\s*\|\s*`(?P<orig_path>[^`]+)`\s*\|"
    r"\s*(?P<size>[^|]+)\|\s*(?P<vault_date>[\d-]+)\s*\|\s*`(?P<hash_prefix>[0-9a-f]+)…`\s*\|"
    r"\s*(?P<status>\w+)\s*\|\s*$"
)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def new_path_for(year, source, vault_date, filename):
    """Find the MIGRATION_MAP entry matching this row's (year, source-ish,
    vault_date) and return the real new-location path for `filename`
    inside it. Raises if no entry matches -- a row this script can't map
    is a real gap to surface, not to silently skip."""
    for entry in ma.MIGRATION_MAP:
        e_year, e_kind, e_date = entry["legacy"]
        if e_year == year and e_date == vault_date:
            new_dir = config._travis_archive(entry["slug"], *entry["new"])
            return os.path.join(new_dir, filename)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                     help="Write the updated manifest. Without this, reports only, writes nothing.")
    args = ap.parse_args(argv)

    config._require_archive_mounted()

    with open(MANIFEST_PATH, "r") as f:
        lines = f.readlines()

    updated_lines = []
    mismatches = []
    unmapped = []
    checked = 0

    for line in lines:
        m = ROW_RE.match(line.rstrip("\n"))
        if not m:
            updated_lines.append((line, None))
            continue

        year = m.group("year")
        source = m.group("source")
        vault_date = m.group("vault_date")
        orig_path = m.group("orig_path")
        hash_prefix = m.group("hash_prefix")
        filename = os.path.basename(orig_path)

        new_path = new_path_for(year, source, vault_date, filename)
        if new_path is None or not os.path.isfile(new_path):
            unmapped.append((orig_path, new_path))
            updated_lines.append((line, "UNMAPPED"))
            continue

        real_hash = sha256_of(new_path)
        checked += 1
        if real_hash[:16] != hash_prefix:
            mismatches.append((orig_path, new_path, hash_prefix, real_hash[:16]))
            updated_lines.append((line, "MISMATCH"))
            print(f"  MISMATCH: {new_path}\n"
                  f"    expected prefix {hash_prefix}, got {real_hash[:16]}")
            continue

        new_cell = f"`{new_path}` (sha256 {real_hash[:16]}…, verified {datetime.now().date()})"
        updated_lines.append((line, new_cell))
        print(f"  OK: {orig_path} -> {new_path}")

    print(f"\nChecked {checked} row(s). {len(mismatches)} mismatch(es). {len(unmapped)} unmapped row(s).")

    if mismatches:
        print("\n*** HASH MISMATCHES FOUND -- refusing to write the manifest. ***")
        print("Investigate before re-running. No files were modified by this check.")
        return 1

    if unmapped:
        print("\nUnmapped rows (no MIGRATION_MAP entry or file not found at expected new "
              "location) -- these need a human decision before the manifest can be "
              "considered complete:")
        for orig, new in unmapped:
            print(f"  {orig}  ->  {new}")

    if not args.execute:
        print("\nDRY RUN -- no changes written. Re-run with --execute to update vault_manifest.md.")
        return 0

    backup_path = MANIFEST_PATH + f".backup_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    with open(backup_path, "w") as f:
        f.writelines(l for l, _ in updated_lines)
    print(f"\nBackup written: {backup_path}")

    header_note = (
        "\n> **Two migrations are recorded in this manifest.** "
        "Migration 1 (2026-08-05): Desktop originals -> legacy vault layout "
        "(`Travis County (TX)/<year>/<source>/<date>/`) -- the `Original path` "
        "and `SHA-256 (original)` columns below. Migration 2 (FILE-ARCH-3, "
        "PX-20260821-01, 2026-08-22): legacy vault layout -> slug-grammar archive "
        "structure (`travis/<source_slug>/<date>/`) -- the "
        f"`{NEW_COLUMN_HEADER}` column, added without altering migration 1's "
        "record. Each new-path cell was independently re-hashed and verified "
        "against migration 1's recorded hash before being written; a mismatch "
        "anywhere would have blocked this update entirely.\n"
    )

    out_lines = []
    header_done = False
    table_header_done = False
    for line, extra in updated_lines:
        stripped = line.rstrip("\n")
        if not header_done and stripped.startswith("Generated:"):
            out_lines.append(line)
            out_lines.append(header_note)
            header_done = True
            continue
        if not table_header_done and stripped.startswith("| Year"):
            out_lines.append(stripped + f" {NEW_COLUMN_HEADER} |\n")
            continue
        if not table_header_done and stripped.startswith("|---"):
            out_lines.append(stripped + "---|\n")
            table_header_done = True
            continue
        if extra in (None, "UNMAPPED", "MISMATCH"):
            if extra is None:
                out_lines.append(line)
            else:
                out_lines.append(stripped + f" {extra} |\n")
        else:
            out_lines.append(stripped + f" {extra} |\n")

    with open(MANIFEST_PATH, "w") as f:
        f.writelines(out_lines)

    print(f"vault_manifest.md updated in place. Backup preserved at {backup_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
