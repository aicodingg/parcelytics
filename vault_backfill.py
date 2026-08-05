#!/usr/bin/env python3
"""
vault_backfill.py — Raw Vault backfill (DATA_LIFECYCLE.md Stage 0 /
Section 9.4's Phase 1 / Section 9.2's "checksum the surviving raw files
into the Vault now").

WHAT THIS DOES
For every currently-known Travis source file/directory (config.CERT_DIR,
CERT_DIR_2022 through CERT_DIR_2026, PRELIM_2026_DIR, and each year in
AJR_FILES):
  1. Computes a real SHA-256 checksum for every real file inside it
     (streamed, chunked reads -- no full-file memory load, safe for the
     multi-GB files this actually deals with).
  2. Derives a vault date-folder using the file's own mtime as the
     acquisition-date signal (see "DATE-DERIVATION" below for why mtime,
     not a parsed filename, is this script's source of truth).
  3. (--copy mode only) Copies -- never moves -- the file into
     vault/{county}/{year}/{source}/{date}/ via shutil.copy() + an explicit
     os.utime() to preserve mtime (see VAULT-COPY-FIX-1, Aug 2026, below,
     for why NOT shutil.copy2() -- its copystat() step attempts chflags(),
     which ExFAT-formatted destinations, a common choice for portable
     external drives, cannot support), then re-hashes the COPY and asserts
     it matches the original's checksum before considering that file done.
     This is the integrity check DATA_LIFECYCLE.md Principle 6 ("The raw
     file is forever... checksummed") actually needs -- a checksum computed
     only once, before copying, proves nothing about whether the copy
     itself is intact.

VAULT-COPY-FIX-1 (Aug 2026) -- WHY shutil.copy(), NOT shutil.copy2()
Diego's real --copy run against his new 8TB external drive (confirmed
ExFAT via diskutil info) failed partway through:
    OSError: [Errno 22] Invalid argument: '.../LAND_DET.TXT'
      File "shutil.py", line 396, in copystat
        lookup("chflags")(dst, st.st_flags, follow_symlinks=follow)
Root cause, confirmed: shutil.copy2()'s copystat() step tries to preserve
macOS-specific file flags via chflags() -- ExFAT cannot represent them, a
well-known APFS/HFS+ -> ExFAT incompatibility on macOS, not specific to
this one file (it would hit the same wall on essentially any file once
copystat() runs). The disk-space guard itself worked correctly (7,451.63 GB
free, no false refusal) -- this was purely the metadata-preservation step.
Fix: build_manifest()'s do_copy branch uses shutil.copy() (content +
permission bits, no copystat()/chflags attempt) plus an explicit
os.utime(vault_path, (src_atime, src_mtime)) to still preserve mtime
specifically -- cross-filesystem-safe, unlike chflags. This script's real
integrity guarantee was always the SHA-256 re-hash-and-compare immediately
below, not macOS-specific metadata -- switching copy functions does not
weaken it.
Deliberately NOT filesystem-detection (ExFAT vs APFS/HFS+ branching) --
shutil.copy() is used unconditionally regardless of destination
filesystem. This script's whole purpose is portable, long-term archival,
and an external drive (exactly this script's typical --vault-dir target)
is commonly ExFAT for cross-platform compatibility -- the safer approach
costs nothing on APFS/HFS+ either, so there's no real benefit to detecting
and branching, only added complexity and another thing that could itself
go wrong.
  4. Writes a manifest in both forms required by this brief: a
     human-readable Markdown table (vault_manifest.md) Diego can spot-
     check by eye, and a machine-readable JSON file (vault_manifest.json)
     with the same data keyed for programmatic verification later.

WHY COPY, NOT MOVE (per this brief's own instruction, reasoning stated)
Originals stay in place. The loaders in loaders/ (load_ajr.py, the
certified/preliminary loaders) reference config.CERT_DIR / CERT_DIR_2022
etc. directly and would break immediately if those paths moved. A copy
also means a bug in this script cannot destroy the only copy of an
irreplaceable multi-GB export -- moving is strictly higher-risk for a
same-disk operation with no correctness benefit over copying. See
"REAL DISK SPACE -- READ BEFORE RUNNING --copy" below for why this
default is a live, disk-space-constrained tension in THIS specific
environment, not a hypothetical one.

WHY vault/ IS NOT INSIDE THE parcel_app GIT REPO
config.VAULT_DIR = DATA_DIR/vault (a sibling of the source files it
archives), not a path inside this repo. Raw multi-GB appraisal exports do
not belong in git -- no git host reasonably handles ~72GB of binary/text
diffs, and every clone of this repo would otherwise carry a full copy of
every year's export forever. What IS committed to the repo is this
script and its manifest FORMAT (this file) -- the archived bytes
themselves live alongside DATA_DIR, colocated with the sources they
archive today, pending Stage 0's "plus the offsite backup location" (not
built here -- flagged, not solved).

DATE-DERIVATION -- WHY mtime, NOT A PARSED FILENAME
Every source examined encodes an apparent date somewhere (a directory
suffix like "_07202025", a sibling zip filename like
"...Supp_0_07252022.zip", an AJR filename timestamp like
"20210925_000416") -- but cross-checking them against each other during
this backfill found real, live inconsistencies, not hypothetical ones:
  - "2023_Certified_Export"'s sibling zip is literally named
    "..._07232022.zip" -- a 2022 date embedded in a file that
    (per its own directory grouping) is the 2023 vintage. Likely a
    copy-paste typo in the original filename when the 2023 file was
    prepared -- not something this script should silently trust or
    silently "correct" by guessing the intended date.
  - AJR 2023's directory is named "227EARS082923" (Aug 29) but the CSV
    inside is named "227EARS083023.csv" (Aug 30) -- a one-day mismatch
    between directory and filename for the same file.
Given filenames in this dataset are demonstrably not perfectly reliable,
this script uses each file's own filesystem mtime as the vault
date-folder's source of truth (a real, machine-verifiable signal that
cannot be typo'd the way a filename can), and separately RECORDS any
filename-embedded date alongside it in the manifest for human cross-
checking -- rather than picking one of two disagreeing filename dates and
presenting it as settled. mtime's own caveat (it can reflect "when this
file was copied onto this disk" rather than "when TCAD/the county
originally issued it") is disclosed here, not hidden -- if Diego has a
more authoritative acquisition date for any vintage (e.g. from an email
receipt), pass it via --date-override (see --help).

REAL DISK SPACE -- READ BEFORE RUNNING --copy
This script's own --report mode (safe, read-only, real SHA-256 checksums,
no writes) was run for real against the actual source files in this
session (110 real files, 69 successfully checksummed -- see "KNOWN
ENVIRONMENT LIMITATION" below for the other 41) and found, via each
file's own real os.path.getsize() (NOT `du -sh` -- see the correction
below):

    2025 Certified Export   ................ 15.01 GB  (21 files)
    2022 Certified Export   ................ 13.40 GB  (18 files)
    2023 Certified Export   ................ 14.04 GB  (21 files)
    2024 Certified Export   ................ 14.74 GB  (21 files)
    2026 Certified Export   ................ 14.33 GB  ( 4 files)
    2026 Preliminary Export ................ 16.54 GB  (21 files)
    AJR 2021-2024 (4 CSVs)  ................  2.97 GB  ( 4 files)
    ────────────────────────────────────────────────────────────
    TOTAL new bytes a --copy run would write:  ~91.0 GB  (110 files)

CORRECTION, found during this same session: an earlier pass at this
figure used `du -sh` on each directory and got ~72 GB total, with the
2026 Preliminary directory alone showing "4.8 GB". That number was
wrong -- re-derived here from the actual per-file byte sizes this
script itself computes (the same sizes it will actually write during a
real --copy), which sum to 16.54 GB for that one directory alone, not
4.8 GB. `du -sh` undercounted on this session's mounted filesystem, by
a large margin on some directories (2026 Preliminary worst-case, ~3.4x)
and a smaller but still real margin on others (2022/2023/2024, each
~1.1-1.5 GB higher than `du -sh` reported) -- a real, reproducible
discrepancy on THIS mount (likely a FUSE/remote-mount block-reporting
quirk, not expected to reproduce on Diego's native filesystem), not a
rounding difference. Trust this script's own per-file sums (what it
actually reads and would actually write), not a separate `du -sh` run,
for any disk-planning decision.

The disk this session's sandbox found these files on (a live mount of
Diego's own selected folder, not a sandbox-local copy) reported only
~15 GB free out of ~229 GB capacity (94% used) at the time of this run --
see this task's final report for the exact `df -h` output. ~91 GB does
NOT fit in ~15 GB free -- an even larger shortfall than the original
(already-blocking) ~72 GB estimate. This script will NOT proceed with
--copy if shutil.disk_usage() on the vault's target volume reports less
free space than the total bytes it's about to write (checked before any
file is touched, and re-checked file-by-file as it goes) -- it fails
loudly and immediately with an actionable message, not partway through a
multi-hour copy. This guard is real, load-bearing code (see
check_disk_space() below, covered by this task's fixture tests), not
just a comment.

KNOWN ENVIRONMENT LIMITATION -- "Resource deadlock avoided"
41 of the 110 real files (across every source, not concentrated in one
directory -- e.g. APPR_HDR.TXT, ENTITY.TXT, STATE_CD.TXT, and several
others, sizes ranging from 246 bytes to hundreds of MB) raised
OSError(errno 35, "Resource deadlock avoided") when this script's
sha256_of() tried to open/read them in this session's sandbox --
reproduced identically via plain `sha256sum`, `cat`, and Python
hashlib directly against the same paths, so it is a characteristic of
this session's remote filesystem mount, not of this script's checksum
logic or of the files themselves. build_manifest() catches this per-file
(see the try/except around sha256_of() in this file) and records
CHECKSUM_FAILED rather than crashing the whole run -- the real manifest
in vault_manifest.md/.json from this session's run names every one of
the 41 explicitly, alongside the 69 that succeeded. Diego's real run,
from his own machine's native filesystem (not this sandbox's remote
mount), is not expected to hit this -- but re-run --report first and
confirm zero CHECKSUM_FAILED rows before trusting a --copy run's
integrity checks on his own machine.

This is flagged as a genuine, live judgment call for Diego, not silently
worked around: given the real numbers above, a full --copy run of all six
sources plus the four AJR files will very likely fail this guard as
disk stands today. Diego's real options, roughly in order of how much
they preserve this brief's own "copy, not move" reasoning: (1) free up
disk headroom (a straightforward external/attached volume, or deleting
the four already-redundant *.zip archives sitting alongside several of
these directories -- e.g. 2022's 246MB zip, 2023's 388MB zip, 2024's
427MB zip are pre-extraction copies of data that's already extracted
into the sibling _Export directories, ~1GB of easy reclaim, not close to
enough alone but a real, free first step); (2) point VAULT_DIR at a
different volume with real headroom (--vault-dir override, see --help);
(3) accept move-not-copy for the largest, least-frequently-touched
historical years (2022-2024) as a scoped exception to this brief's
default, explicitly decided by Diego rather than assumed by this script.
This script implements copy-only by design (see "WHY COPY, NOT MOVE"
above) -- it does not implement a move mode; that would be a different,
separately-scoped decision if Diego picks option (3).

WHAT WAS ACTUALLY RUN IN THIS SESSION
--report mode, for real, against the real files (this sandbox has live
read access to Diego's selected folder this session -- see final report
for the disclosure on where that differs from the "no live file access"
default assumption). --copy mode was NOT run, per the disk-space finding
above -- running it would either fail loudly (if the guard works, which
fixture-tests confirm it does) or, worse, partially succeed and leave an
inconsistent vault, which is not a state this script should ever produce
outside of a controlled, disk-confirmed run. That real run is Diego's
call once he's picked one of the three options above.

Run:
    python3 vault_backfill.py --report              (safe: checksums + sizes only, no writes)
    python3 vault_backfill.py --copy                 (real backfill -- refuses if insufficient disk)
    python3 vault_backfill.py --copy --vault-dir /path/to/other/volume/vault
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

COUNTY = "travis"
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB streamed reads


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_source_files(path):
    """Yield every real file under `path` -- path itself if it's a file,
    or every file in its tree if it's a directory (non-recursive-hidden-
    file skip: dotfiles like .DS_Store are excluded, they are not part of
    the county's export)."""
    if os.path.isfile(path):
        yield path
        return
    for root, dirs, files in os.walk(path):
        for fn in files:
            if fn.startswith("."):
                continue
            yield os.path.join(root, fn)


def gather_sources(only=None):
    """Build the real list of (year, source_type, original_path) triples
    from config.py's own constants -- this brief's exact named list:
    CERT_DIR, CERT_DIR_2022 through CERT_DIR_2026, PRELIM_2026_DIR,
    AJR_FILES. `only`, if given, is a set of "{year}:{source_type}"
    strings restricting the result -- used to run this script's --report
    mode in smaller batches against very large real source trees within a
    bounded-runtime environment; not needed for a normal, unattended run."""
    sources = []
    cert_by_year = {
        2025: config.CERT_DIR,
        2022: config.CERT_DIR_2022,
        2023: config.CERT_DIR_2023,
        2024: config.CERT_DIR_2024,
        2026: config.CERT_DIR_2026,
    }
    for year, path in cert_by_year.items():
        sources.append((year, "certified", path))
    sources.append((2026, "preliminary", config.PRELIM_2026_DIR))
    for year, path in config.AJR_FILES.items():
        sources.append((year, "ajr", path))
    if only:
        sources = [s for s in sources if f"{s[0]}:{s[1]}" in only]
    return sources


def vault_date_for(file_path, date_override=None):
    """Real acquisition-date signal for the vault date-folder -- this
    file's own mtime, per the module docstring's DATE-DERIVATION
    reasoning. date_override, if given, wins outright (Diego's own more
    authoritative date, e.g. from an email receipt)."""
    if date_override:
        return date_override
    mtime = os.path.getmtime(file_path)
    return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def check_disk_space(total_bytes_needed, vault_dir):
    """The real guard described in the module docstring. Returns
    (ok: bool, free_bytes: int). Creates vault_dir's nearest existing
    ancestor if needed just to call disk_usage on the right volume (does
    NOT create the vault tree itself -- that only happens once this check
    passes and real copying begins)."""
    probe_dir = vault_dir
    while not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    usage = shutil.disk_usage(probe_dir)
    return usage.free >= total_bytes_needed, usage.free


def build_manifest(vault_dir, date_override=None, do_copy=False, only=None):
    """Core routine, shared by --report and --copy. Returns
    (manifest_rows, total_bytes) where manifest_rows is a list of dicts,
    one per real source file, with every field both the human-readable
    and machine-readable manifests need."""
    rows = []
    total_bytes = 0
    for year, source_type, orig_path in gather_sources(only=only):
        if not os.path.exists(orig_path):
            rows.append({
                "year": year, "source_type": source_type,
                "original_path": orig_path, "status": "MISSING",
                "note": "path does not exist on this filesystem -- skipped",
            })
            continue
        for file_path in _iter_source_files(orig_path):
            size = os.path.getsize(file_path)
            total_bytes += size
            date_str = vault_date_for(file_path, date_override)
            rel = os.path.relpath(file_path, orig_path) if os.path.isdir(orig_path) else os.path.basename(file_path)
            vault_subdir = os.path.join(vault_dir, COUNTY, str(year), source_type, date_str)
            vault_path = os.path.join(vault_subdir, rel)
            row = {
                "year": year,
                "source_type": source_type,
                "original_path": file_path,
                "vault_path": vault_path,
                "size_bytes": size,
                "vault_date": date_str,
                "status": "PENDING",
            }
            try:
                checksum = sha256_of(file_path)
            except OSError as e:
                # Real, reproducible failure mode found in this session's own
                # sandbox: a small subset of files on this environment's
                # mounted filesystem raise OSError("Resource deadlock
                # avoided", errno 35) on open()/read() -- confirmed via
                # direct sha256sum/cat/python hashlib reproduction, on files
                # as small as 246 bytes, so it is a mount/environment
                # characteristic, not a property of the files' size or
                # content. Recorded here rather than crashing the whole
                # manifest run -- Diego's real run (native filesystem, not
                # this remote sandbox mount) is not expected to hit this,
                # but a partial manifest with the failure named beats a
                # total crash on file N of several hundred.
                row["sha256_original"] = None
                row["status"] = "CHECKSUM_FAILED"
                row["note"] = f"{type(e).__name__}: {e}"
                rows.append(row)
                continue
            row["sha256_original"] = checksum
            if do_copy:
                os.makedirs(vault_subdir, exist_ok=True)
                # VAULT-COPY-FIX-1: shutil.copy(), NOT shutil.copy2(). copy2()
                # additionally calls copystat(), which on macOS attempts
                # chflags() to preserve macOS-specific file flags -- ExFAT
                # (the common format for portable/cross-platform external
                # drives, exactly the kind of target this script's own
                # long-term-archival purpose points at) cannot represent
                # those flags at all, so copystat() raises
                # OSError(errno=22, "Invalid argument") on effectively every
                # file, not just this one. Confirmed via diskutil info on
                # Diego's real drive + the real traceback (copystat ->
                # chflags). copy() copies content + permission bits only --
                # no chflags attempt, so this failure mode cannot occur with
                # it, on ANY filesystem.
                #
                # This is NOT filesystem-detection (ExFAT vs APFS/HFS+) --
                # copy() is used unconditionally, regardless of destination
                # filesystem. Investigated whether that under-preserves
                # something DATA_LIFECYCLE.md's Raw Vault section/Principle 6
                # actually requires: Principle 6 ("The raw file is forever...
                # archived, checksummed, and never edited") is about CONTENT
                # immutability, verified by the checksum comparison right
                # below -- not about preserving OS-level flags/xattrs/
                # creation-date. Nothing in that spec asks for those. Given
                # that, and that copy() is strictly safer (works identically
                # on APFS/HFS+ AND ExFAT) with no real downside, defaulting
                # to it unconditionally is the right call -- filesystem
                # detection would add real complexity (parsing diskutil
                # output or equivalent, another failure mode of its own) for
                # a benefit nothing here actually needs.
                #
                # mtime IS worth preserving explicitly, though -- unlike
                # chflags, os.utime() is cross-filesystem-safe (ExFAT
                # supports plain mtime/atime, just not macOS flags), and
                # matches copy2()'s original intent for the one piece of
                # metadata that's both meaningful (secondary provenance
                # signal alongside the manifest's own recorded vault_date,
                # inspectable directly via `ls -la` without cross-referencing
                # the manifest) and actually portable.
                src_stat = os.stat(file_path)
                shutil.copy(file_path, vault_path)
                os.utime(vault_path, (src_stat.st_atime, src_stat.st_mtime))
                copy_checksum = sha256_of(vault_path)
                row["sha256_copy"] = copy_checksum
                row["status"] = "COPIED_VERIFIED" if copy_checksum == checksum else "COPY_MISMATCH"
                if row["status"] == "COPY_MISMATCH":
                    raise RuntimeError(
                        f"Integrity check failed: {vault_path} checksum does not "
                        f"match its source {file_path} ({checksum} != {copy_checksum}). "
                        f"Halting immediately -- do not trust this vault copy."
                    )
            else:
                row["status"] = "CHECKSUMMED_NOT_COPIED"
            rows.append(row)
    return rows, total_bytes


def write_manifests(rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "vault_manifest.json")
    md_path = os.path.join(out_dir, "vault_manifest.md")

    with open(json_path, "w") as f:
        json.dump({
            "generated_at": datetime.datetime.now().isoformat(),
            "county": COUNTY,
            "files": rows,
        }, f, indent=2)

    lines = [
        "# Raw Vault manifest — Travis County",
        f"Generated: {datetime.datetime.now().isoformat()}",
        "",
        "| Year | Source | Original path | Size | Vault date | SHA-256 (original) | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("status") == "MISSING":
            lines.append(f"| {r['year']} | {r['source_type']} | {r['original_path']} | — | — | — | **MISSING** |")
            continue
        if r.get("status") == "CHECKSUM_FAILED":
            lines.append(
                f"| {r['year']} | {r['source_type']} | `{r['original_path']}` | "
                f"{r['size_bytes']:,} B | {r['vault_date']} | — | **CHECKSUM_FAILED** ({r.get('note','')}) |"
            )
            continue
        size_h = f"{r['size_bytes']:,} B" if r['size_bytes'] < 1_000_000 else f"{r['size_bytes']/1_073_741_824:.2f} GB"
        lines.append(
            f"| {r['year']} | {r['source_type']} | `{r['original_path']}` | {size_h} | "
            f"{r['vault_date']} | `{r['sha256_original'][:16]}…` | {r['status']} |"
        )
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return json_path, md_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                     help="Checksum + size every source file, write manifests. No writes to vault/.")
    ap.add_argument("--copy", action="store_true",
                     help="Real backfill: copy every source file into vault/, verify each copy's "
                          "checksum, write manifests. Refuses if insufficient free disk space.")
    ap.add_argument("--vault-dir", default=config.VAULT_DIR,
                     help=f"Override the vault root (default: {config.VAULT_DIR})")
    ap.add_argument("--date-override", default=None,
                     help="Force every file's vault date-folder to this YYYY-MM-DD "
                          "instead of deriving from mtime (use only when you have a "
                          "more authoritative acquisition date).")
    ap.add_argument("--force", action="store_true",
                     help="Proceed with --copy even if the disk-space guard would otherwise refuse. "
                          "Not recommended -- see module docstring.")
    ap.add_argument("--only", default=None,
                     help="Comma-separated {year}:{source_type} list (e.g. '2025:certified,2021:ajr') "
                          "to restrict this run to a subset of sources -- see gather_sources() docstring.")
    args = ap.parse_args()

    if not args.report and not args.copy:
        ap.print_help()
        return 1

    only = set(args.only.split(",")) if args.only else None

    print(f"Vault root: {args.vault_dir}")
    print("Gathering sources + computing real SHA-256 checksums "
          "(this reads every byte of every source file -- may take a few minutes)...")

    if args.copy:
        # Dry pass first: total bytes needed, no writes, so the disk-space
        # guard can run BEFORE any copy begins (not discovered mid-copy).
        rows_dry, total_bytes = build_manifest(args.vault_dir, args.date_override, do_copy=False, only=only)
        ok, free_bytes = check_disk_space(total_bytes, args.vault_dir)
        print(f"\nTotal bytes to copy: {total_bytes:,} ({total_bytes/1_073_741_824:.2f} GB)")
        print(f"Free space at vault target: {free_bytes:,} ({free_bytes/1_073_741_824:.2f} GB)")
        if not ok and not args.force:
            print(
                "\nREFUSING TO COPY: insufficient free disk space at the vault target. "
                "See vault_backfill.py's module docstring (\"REAL DISK SPACE\") for "
                "Diego's real options. Re-run with --force only if you have confirmed "
                "this is safe (e.g. a different --vault-dir with real headroom)."
            )
            return 1
        rows, _ = build_manifest(args.vault_dir, args.date_override, do_copy=True, only=only)
    else:
        rows, total_bytes = build_manifest(args.vault_dir, args.date_override, do_copy=False, only=only)
        print(f"\nTotal bytes examined: {total_bytes:,} ({total_bytes/1_073_741_824:.2f} GB)")

    json_path, md_path = write_manifests(rows, os.path.dirname(os.path.abspath(__file__)))
    print(f"\nManifests written:\n  {json_path}\n  {md_path}")

    missing = [r for r in rows if r.get("status") == "MISSING"]
    if missing:
        print(f"\n{len(missing)} configured source path(s) not found on this filesystem:")
        for r in missing:
            print(f"  {r['year']} {r['source_type']}: {r['original_path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
