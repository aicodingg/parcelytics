#!/usr/bin/env python3
"""
update_vault_manifest_migration3.py — PX-20260822-05

Appends a Migration 3 section to vault_manifest.md documenting the 63
files archive_source_collateral.py (PX-20260822-03, commits c6712a3 +
261facd) copied into the vault: sole-copy source/collateral files that
had no twin already migrated, found by verify_claude_files_twins.py.

WHY A SIBLING SCRIPT, NOT AN EXTENSION OF update_vault_manifest.py
update_vault_manifest.py's whole design is "add a verified column to
EXISTING migration-1 rows" (one new column, keyed by matching a row via
regex against MIGRATION_MAP). Migration 3's files were never part of
migration 1's original record -- they have no Original-path/hash row to
attach a column to; this is an APPEND of new rows in their own section,
not a decoration of old ones. Reusing update_vault_manifest.py's
ROW_RE/column-append machinery for that would be a bigger, less honest
hack than a short sibling script that mirrors its safety discipline
(dry-run default, hash-verify, timestamped backup, loud failure,
immutable prior rows) without pretending to be the same kind of update.

WHAT THIS SCRIPT DOES
Reuses archive_source_collateral.py's build_plan() (imported, never
retyped) to get the exact file list + destinations that script produced.

PX-20260822-05-rev1 UPDATE: the three stale-code findings this script
originally reported and worked around (see below) are now fixed AT THE
SOURCE, in archive_source_collateral.py itself:
  (a) the two 2021 Certified Appraisal Roll PDFs now correctly land at
      travis/certified_roll/2022-01-25/, with build_plan()'s own flag
      text recording the relocation history;
  (b) the 2025-09-04 vintage files now carry a CONFIRMED-from-MIF flag,
      not the old INFERRED-DATE text;
  (c) the rates file now uses the registered "rates" slug
      (config._travis_archive("rates")), not the unregistered
      "adopted_tax_rates".
With the source fixed, this script no longer needs to OVERRIDE
build_plan()'s output -- it uses dest_dir and flag exactly as build_plan()
returns them. What it keeps, deliberately, is a set of REGRESSION GUARDS
(assert_no_regression() below) that check build_plan()'s output still
matches the three ruled end-states, and raise loudly if it doesn't. This
was a genuine judgment call (the brief explicitly left it to this
script's discretion): a blind pass-through would silently start writing
wrong manifest rows again if archive_source_collateral.py ever regressed
(e.g. a future edit reverting one of the three fixes); keeping the old
override logic would silently mask that same regression by re-applying
the "correction" over whatever build_plan() says, which is worse --
it would hide a real bug instead of catching it. An explicit assertion
that fails loudly is the only one of the three options that surfaces a
regression instead of either propagating or hiding it.

STALE-CODE / NAMING FINDINGS -- STATUS AS OF PX-20260822-05-rev1
  (a) 2021-PDF destination: FIXED in archive_source_collateral.py.
  (b) INFERRED_2025_FLAG: FIXED (replaced with a CONFIRMED-from-MIF note).
  (c) adopted_tax_rates slug: FIXED (now uses the registered "rates"
      slug, per PM ruling -- config.py:229 TAX_RATES_XL, config.py:186).
      Diego relocates the physical file separately; this script's row
      reflects the ruled/soon-to-be-real "rates" destination.

SOURCE PATH DOCUMENTATION
Per this brief, source paths are recorded under their current, retired
location. RETIRED_CLAUDE (below, env-overridable for testing) points at
~/Desktop/Claude Files_RETIRED_20260821 by default -- the Desktop rename
Diego completed after archive_source_collateral.py's real run. It is
substituted for archive_source_collateral.CLAUDE before calling that
module's build_plan(), so the exact same enumeration logic walks the
real (renamed) location rather than the pre-rename path that no longer
exists.

WHAT THIS SCRIPT VERIFIES (not just asserts)
For each of the 63 files: confirms the file really exists at its vault
destination (a missing file is a real gap, reported, not silently
skipped), and computes its REAL SHA-256 from that live vault file --
never copied from archive_source_collateral.py's old stdout. Where the
source file is still reachable at its retired Desktop location, this
script ALSO re-hashes the source and compares source-vs-destination, as
an extra integrity check beyond what this brief strictly asked for
(belt-and-suspenders, consistent with this repo's "never trust a claim
alone" convention) -- any mismatch is a loud, hard stop; if the source
is no longer reachable that's noted, not treated as a failure.

WHAT THIS SCRIPT DOES NOT DO
Does not touch, re-verify, or rewrite Migration 1 or Migration 2's
existing rows or the columns they occupy -- read-only against those; the
only edit made to existing content is the header note's migration count
and one added sentence, both required by this brief. Does not touch the
vault except reading. Writes exactly one thing: vault_manifest.md, after
a timestamped backup, and only after every row's hash check passes.
Refuses to run --execute a second time against an already-updated
manifest (the header-sentence match fails on purpose) rather than risk
appending a duplicate section.

SANDBOX DISCLOSURE
No /Volumes access in this sandbox (same limitation as every prior
archive-touching script in this repo). All logic below is fixture-
tested against synthetic stand-ins for the retired Desktop folder and
the vault; the real 63-file run must happen on Diego's own machine.

Run (on Diego's machine, real drive + renamed Desktop folder present):
    python3 update_vault_manifest_migration3.py            # dry-run
    python3 update_vault_manifest_migration3.py --execute   # writes
"""
import argparse
import hashlib
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import archive_source_collateral as asc  # reuse build_plan() -- never retype

CHUNK_SIZE = 4 * 1024 * 1024
MANIFEST_PATH = os.environ.get(
    "PARCELYTICS_VAULT_MANIFEST_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_manifest.md"),
)
RETIRED_CLAUDE = os.environ.get(
    "PARCELYTICS_RETIRED_DESKTOP_ROOT",
    os.path.expanduser("~/Desktop/Claude Files_RETIRED_20260821"),
)

# --- Regression guards (see module docstring) ---
# These no longer OVERRIDE build_plan()'s output (the source fixes in
# archive_source_collateral.py make that unnecessary) -- they ASSERT it
# still matches the ruled end-state, and raise loudly if a future edit to
# archive_source_collateral.py regresses any of the three.
STALE_2021_PDF_NAMES = {
    "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_Alpha.pdf",
    "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf",
}
EXPECTED_2021_PDF_DEST_SUFFIX = os.path.join("certified_roll", "2022-01-25")
STALE_2021_PDF_DEST_SUFFIX = os.path.join("certified_roll", "2021-09-25")

CONFIRMED_2025_SRC_HINT = "227EARS090425"  # substring match against src path
STALE_INFERRED_MARKER = "INFERRED-DATE"  # regression signal if this reappears in the flag

STALE_RATES_SLUG = "adopted_tax_rates"
EXPECTED_RATES_SLUG = "rates"
RATES_FILENAME = "2025RatesHistory1990-2025.xlsx"


class ArchiveSourceCollateralRegressionError(RuntimeError):
    """Raised when archive_source_collateral.py's build_plan() no longer
    matches the PX-20260822-05-rev1 ruled end-state for one of the three
    findings this script used to work around -- a real regression, not a
    cosmetic mismatch. Loud on purpose: see module docstring."""


def assert_no_regression(src, dest_dir, flag):
    """Checked per-PLAN-entry inside build_migration3_rows(). Raises
    ArchiveSourceCollateralRegressionError on the first sign any of the
    three PX-20260822-05-rev1 source fixes has regressed."""
    fn = os.path.basename(src)

    if fn in STALE_2021_PDF_NAMES:
        if STALE_2021_PDF_DEST_SUFFIX in dest_dir:
            raise ArchiveSourceCollateralRegressionError(
                f"REGRESSION: {fn} is back under the pre-relocation "
                f"2021-09-25 destination ({dest_dir}) -- "
                f"archive_source_collateral.py's fix (a) appears reverted."
            )
        if EXPECTED_2021_PDF_DEST_SUFFIX not in dest_dir:
            raise ArchiveSourceCollateralRegressionError(
                f"REGRESSION: {fn} destination ({dest_dir}) is neither the "
                f"expected 2022-01-25 path nor the old stale 2021-09-25 path "
                f"-- something else has changed. Investigate before trusting "
                f"this row."
            )

    if CONFIRMED_2025_SRC_HINT in src and flag and STALE_INFERRED_MARKER in flag:
        raise ArchiveSourceCollateralRegressionError(
            f"REGRESSION: {src} is carrying the stale INFERRED-DATE flag "
            f"again ({flag!r}) -- archive_source_collateral.py's fix (b) "
            f"appears reverted."
        )

    if fn == RATES_FILENAME:
        if STALE_RATES_SLUG in dest_dir:
            raise ArchiveSourceCollateralRegressionError(
                f"REGRESSION: {fn} destination ({dest_dir}) is back on the "
                f"unregistered adopted_tax_rates slug -- archive_source_"
                f"collateral.py's fix (c) appears reverted."
            )
        if EXPECTED_RATES_SLUG not in dest_dir.split(os.sep):
            raise ArchiveSourceCollateralRegressionError(
                f"REGRESSION: {fn} destination ({dest_dir}) does not use "
                f"the registered 'rates' slug -- investigate before "
                f"trusting this row."
            )

HEADER_ANCHOR = "Two migrations are recorded in this manifest."
HEADER_ANCHOR_REPLACEMENT = "Three migrations are recorded in this manifest."
MIGRATION2_TAIL_ANCHOR = "a mismatch anywhere would have blocked this update entirely.\n"
MIGRATION3_HEADER_ADDITION = (
    "a mismatch anywhere would have blocked this update entirely. "
    "Migration 3 (PX-20260822-03, commits `c6712a3`/`261facd`, recorded via "
    "PX-20260822-05, {today}): 63 sole-copy source/collateral files with no "
    "prior vault twin, archived by `archive_source_collateral.py` -- see the "
    "Migration 3 section below for the full per-file record.\n"
)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


# Matches a Current-path cell's path portion: `<path>` (sha256 ...) -- this
# exact "backtick-path immediately followed by '(sha256 '" shape is unique
# to migration 2's Current-path column (the Original-path and hash-prefix
# columns are backtick-quoted too, but neither is followed by "(sha256 ").
EXISTING_CURRENT_PATH_RE = re.compile(r"`([^`]+)`\s*\(sha256\s")


def existing_manifest_destinations():
    """Read-only. Returns the set of every destination path already
    recorded in vault_manifest.md's Current-path column (migration 2's
    record of where each migration-1 file now lives). Used by
    build_migration3_rows() to exclude any archive_source_collateral.py
    plan entry that duplicates an already-migrated destination.

    PX-20260822-05-rev2: this is a STRUCTURAL exclusion (parse the real
    manifest, exclude whatever's really already there), not a hardcoded
    filename check -- the real overlap this round caught was
    20210925_000416_PTD.csv (archive_source_collateral.py's "2021EARS092521
    2" duplicate-suffixed folder plans to copy the same file, to the same
    travis/certified_roll/2021-09-25/ destination, that migration 1 already
    put there), but the same structural situation could recur for any
    future file, so the check is general: parse-and-exclude, not a name
    list."""
    if not os.path.isfile(MANIFEST_PATH):
        return set()
    with open(MANIFEST_PATH, "r") as f:
        content = f.read()
    return set(EXISTING_CURRENT_PATH_RE.findall(content))


def build_migration3_rows():
    """Read-only. Returns a list of row-dicts (one per real, NEW vault
    file this migration produced) with all the fields the manifest
    section needs.

    Two kinds of exclusion happen before a plan entry becomes a row (order
    matters -- prior-migration exclusion is checked first, so a file that
    both duplicates a sibling PLAN entry AND an existing manifest row is
    still only ever reported once, as PRIOR-MIGRATION SKIP):
      1. Excluded if its destination is already recorded in
         vault_manifest.md's existing Current-path column (see
         existing_manifest_destinations() -- PX-20260822-05-rev2's
         MUST-FIX, structural: parse-and-exclude, not a hardcoded
         filename). The real case this catches: archive_source_
         collateral.py's own '2021EARS092521 2' entry for
         20210925_000416_PTD.csv plans a destination migration 1 already
         put a file at -- that plan entry has no sibling duplicate WITHIN
         the plan itself (the non-suffixed '2021EARS092521' folder was
         never fed into the plan at all, since it already had a twin in
         the vault), so nothing catches it except this explicit
         cross-reference against the manifest's own prior record.
      2. Deduplicated by real destination path against OTHER plan
         entries (the ' 2'-suffixed sibling-folder case archive_source_
         collateral.py's own dedup logic collapses at copy time --
         collapsed here the same way, so this script never has to
         actually run a real copy to know the row count).
    Per Diego's real, already-executed archive_source_collateral.py run
    (commit c6712a3): 76 planned, 63 copied, 13 deduped. This script's
    own count over THIS retired-Desktop/vault pairing may differ slightly
    from that historical run (e.g. if any file changed since) -- treat
    the number this script reports, not a number hardcoded here, as the
    one to check against expectation before --execute."""
    config._require_archive_mounted()

    existing_dests = existing_manifest_destinations()

    orig_claude = asc.CLAUDE
    asc.CLAUDE = RETIRED_CLAUDE
    try:
        plan = asc.build_plan()
    finally:
        asc.CLAUDE = orig_claude

    seen_dest = {}
    for src, dest_dir, flag in plan:
        assert_no_regression(src, dest_dir, flag)  # raises loudly on any of the 3 findings regressing
        fn = os.path.basename(src)
        note = flag or ""
        dest_path = os.path.join(dest_dir, fn)
        if dest_path in existing_dests:
            print(f"  PRIOR-MIGRATION SKIP (already recorded in vault_manifest.md's "
                  f"Current-path column): {dest_path}  (planned source: {src})")
            continue
        if dest_path in seen_dest:
            continue  # exact-duplicate destination from a " 2" sibling folder -- already counted
        seen_dest[dest_path] = {"src": src, "dest": dest_path, "note": note}
    return list(seen_dest.values())


def verify_and_hash(rows):
    """Real verification pass -- read-only. Returns (verified_rows,
    missing, mismatches). Never trusts a prior hash; always recomputes
    from the live vault file."""
    verified = []
    missing = []
    mismatches = []
    for row in rows:
        if not os.path.isfile(row["dest"]):
            missing.append(row)
            continue
        dest_hash = sha256_of(row["dest"])
        if os.path.isfile(row["src"]):
            src_hash = sha256_of(row["src"])
            if src_hash != dest_hash:
                mismatches.append((row, src_hash, dest_hash))
                continue
            cross_check = "source re-hashed, matches vault"
        else:
            cross_check = "source no longer reachable at retired path -- vault hash only"
        verified.append({**row, "sha256": dest_hash, "cross_check": cross_check})
    return verified, missing, mismatches


def print_precommit_summary(verified, missing, mismatches):
    print("=" * 78)
    print("PRE-COMMIT SUMMARY -- Migration 3 -- review before running --execute")
    print("=" * 78)
    print(f"  {len(verified)} file(s) verified and ready to record")
    for row in verified:
        print(f"  {row['dest']}")
    if missing:
        print(f"\n  {len(missing)} MISSING from vault (in the plan, not found on disk):")
        for row in missing:
            print(f"    {row['dest']}  (expected from {row['src']})")
    if mismatches:
        print(f"\n  {len(mismatches)} HASH MISMATCH(ES) between retired source and vault file:")
        for row, s, d in mismatches:
            print(f"    {row['dest']}: source={s} vault={d}")
    print("=" * 78)


def render_section(verified):
    lines = []
    lines.append("\n## Migration 3 (PX-20260822-03/-05, 2026-08-22): sole-copy source/collateral archived\n")
    lines.append(
        "Files found by `verify_claude_files_twins.py` with no twin already in the "
        "vault -- copied in by `archive_source_collateral.py` (commits `c6712a3`, "
        "`261facd`, PX-20260822-05-rev1 fixes). Source paths below are recorded "
        "at their current, retired Desktop location. Every row's SHA-256 was "
        "recomputed directly from the live vault file by this script, never "
        "copied from a prior script's stdout. Three stale-code findings this "
        "process originally surfaced (2021-PDF destination, 2025-vintage "
        "INFERRED-DATE flag, unregistered adopted_tax_rates slug) were fixed at "
        "the source per PM ruling; this script now only asserts build_plan()'s "
        "output still matches that ruled end-state (see "
        "`update_vault_manifest_migration3.py`'s module docstring).\n\n"
    )
    lines.append("| Source path (retired) | Destination (vault) path | SHA-256 | Verified date | Notes |\n")
    lines.append("|---|---|---|---|---|\n")
    for row in verified:
        note = f"{row['note']}; {row['cross_check']}" if row["note"] else row["cross_check"]
        lines.append(
            f"| `{row['src']}` | `{row['dest']}` | `{row['sha256'][:16]}…` | "
            f"{datetime.now().date()} | {note} |\n"
        )
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                     help="Write the Migration 3 section. Without this, reports only, writes nothing.")
    args = ap.parse_args(argv)

    rows = build_migration3_rows()
    verified, missing, mismatches = verify_and_hash(rows)
    print_precommit_summary(verified, missing, mismatches)

    if mismatches:
        print("\n*** HASH MISMATCHES -- refusing to write. Investigate before re-running. ***")
        return 1

    if missing:
        print(f"\n{len(missing)} file(s) from the plan are missing on the real vault -- "
              f"confirm this matches what you expect before --execute (e.g. a partial "
              f"archive_source_collateral.py run). This script will still record the "
              f"{len(verified)} it DID verify if you proceed.")

    if not args.execute:
        print("\nDRY RUN -- no changes written. Re-run with --execute to update vault_manifest.md.")
        return 0

    if not os.path.isfile(MANIFEST_PATH):
        print(f"*** {MANIFEST_PATH} not found -- refusing to write. ***")
        return 1

    with open(MANIFEST_PATH, "r") as f:
        content = f.read()

    if HEADER_ANCHOR not in content:
        print("*** Expected header sentence not found (manifest may already document "
              "3 migrations, or may have changed shape since this script was "
              "written) -- refusing to guess. If Migration 3 is already recorded, "
              "this is the correct, safe refusal, not a bug. Investigate before "
              "re-running. ***")
        return 1

    # MUST-FIX (PX-20260822-05-rev1): checked explicitly, and BEFORE the
    # backup write, not left to the generic updated_header == content
    # guard below. That guard can't catch this specific failure mode: once
    # the HEADER_ANCHOR replace above succeeds, updated_header already
    # differs from content, so a silently-failed (no-op) tail-anchor
    # replace would sail straight through it -- the Migration-3 sentence
    # would be silently dropped from the header while the section still
    # gets appended and the run reports success. Caught here instead.
    if MIGRATION2_TAIL_ANCHOR not in content:
        print("*** Expected migration-2 tail sentence not found verbatim -- "
              "refusing to write a manifest whose Migration-3 header sentence "
              "would be silently dropped. The header's 'Two'->'Three' text "
              "may have changed shape since this script was written; "
              "investigate before re-running. ***")
        return 1

    backup_path = MANIFEST_PATH + f".backup_{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    with open(backup_path, "w") as f:
        f.write(content)
    print(f"\nBackup written: {backup_path}")

    updated_header = content.replace(HEADER_ANCHOR, HEADER_ANCHOR_REPLACEMENT, 1)
    addition = MIGRATION3_HEADER_ADDITION.format(today=datetime.now().date())
    updated_header = updated_header.replace(MIGRATION2_TAIL_ANCHOR, addition, 1)

    if addition not in updated_header:
        print("*** Post-replacement sanity check failed: the Migration-3 header "
              "addition is not actually present in the updated header despite "
              "both anchor checks passing -- refusing to write. Investigate "
              "before re-running. ***")
        return 1

    if updated_header == content:
        print("*** Header replacement made no change -- expected text not found "
              "verbatim. Refusing to write a manifest whose header wasn't "
              "actually updated. Investigate before re-running. ***")
        return 1

    new_content = updated_header + "".join(render_section(verified))

    with open(MANIFEST_PATH, "w") as f:
        f.write(new_content)

    print(f"\nvault_manifest.md updated: header now documents 3 migrations, "
          f"{len(verified)} Migration 3 row(s) appended. Backup preserved at "
          f"{backup_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
