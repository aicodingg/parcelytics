#!/usr/bin/env python3
"""
archive_source_collateral.py — PX-20260822-03 (PM decision, layout per that brief)

Archives the ~70 NO_TWIN files found by verify_claude_files_twins.py:
sole-copy source data and collateral still sitting in ~/Desktop/Claude
Files/, never copied into the vault by any prior migration.

LAYOUT (per PX-20260822-03, PM decision, 2026-08-22; corrected per
PX-20260822-05-rev1, 2026-08-23 -- code below now matches the ruled
end-state, not just the docstring):
- Collateral lives FLAT inside its matching vintage folder alongside the
  already-archived extracted data (no separate collateral/ subdir).
- Original delivery zips are archived alongside their extracted contents.
- Vintage dates for 2021-2024 EARS collateral are read directly from
  migrate_archive.py's own MIGRATION_MAP (imported, never retyped) --
  these are HIGH CONFIDENCE, already independently verified by that
  migration.
- The 2025 EARS delivery (227EARS090425[, " 2"]) has no MIGRATION_MAP
  entry -- it's a new vintage. Date 2025-09-04 is CONFIRMED (not
  inferred) by reading the archived signed MIF (Form 50-792,
  227_EARS_MIF_090425_Signed.pdf): "Date Prepared 09/04/2025", signed by
  Chief Appraiser Leana H Mann. Note the same form lists a separate
  "Certification Date 07/18/2025" -- the appraisal roll's certification,
  not this submission's preparation. Vault vintages are keyed on
  preparation/acquisition date (consistent with the 2022-09-28 /
  2023-08-30 / 2024-08-28 AJR vintages, all EARS prep dates), so
  2025-09-04 is correct. (PX-20260822-05-rev1: build_plan() previously
  still applied a stale INFERRED-DATE flag in code despite this
  confirmation being documented here -- fixed below.)
- The two 2021 Certified Appraisal Roll PDFs: their only internal date
  is a True Automation report-generation timestamp, 01/25/2022 22:09PM
  -- these are separately generated reports of the 2021 certified roll
  "as of Supplement 0", NOT part of the September 2021 EARS delivery.
  Destination is travis/certified_roll/2022-01-25/, keeping the
  archive's one dating rule: folders are keyed by when the artifact was
  acquired/produced, never by tax year. The "2021" stays unambiguous
  from the filenames. (PX-20260822-05-rev1: build_plan() previously
  still hardcoded the pre-relocation 2021-09-25 destination despite the
  real files having been physically relocated to 2022-01-25 -- fixed
  below.)
- 2025RatesHistory1990-2025.xlsx is NOT collateral and is NOT a vintage
  export -- it's the Source Registry's "Adopted tax rates" row, a
  cumulative 1990-2025 history workbook. Destination is
  travis/rates/2025RatesHistory1990-2025.xlsx: the REGISTERED slug
  (config.py:229, TAX_RATES_XL = _travis("rates", ...); config.py:186
  ties this slug to the Registry's "Adopted tax rates" row), at the
  slug root with no vintage subfolder (there is no single vintage date
  for a cumulative history file). (PX-20260822-05-rev1 ruling:
  travis/adopted_tax_rates/ was an unregistered invention -- fixed
  below. Diego relocates the physical file separately; this fix makes
  the code match that ruled end-state.)

NOTE ON ._ FILES: the vault is ExFAT, which can't store macOS resource
forks natively, so macOS writes AppleDouble sidecars (._<name>) beside
copied files. These are metadata, not data; every script here skips
dotfiles, which is why file counts stay clean. Deleting them is
pointless -- macOS recreates them on the next copy. Any future tooling
walking the vault must skip dotfiles or it will double-count.

DEDUPLICATION: the " 2"-suffixed EARS folders are duplicates by name of
their non-suffixed siblings. Each file is hashed; if a same-named file
already exists in the planned destination (from the non-suffixed
folder), content is compared -- identical means skip (dedup, not an
error), different means a loud stop (real, unexplained divergence).

Same integrity discipline as migrate_archive.py: full SHA-256 compare on
both fresh copies and existing-destination skips, never size-only.

Run:
    python3 archive_source_collateral.py            # dry-run, writes nothing
    python3 archive_source_collateral.py --execute   # real copy, hash-verified
"""
import argparse
import hashlib
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import migrate_archive as ma  # reuse MIGRATION_MAP -- never retype vintage dates

CLAUDE = os.path.expanduser("~/Desktop/Claude Files")
CHUNK_SIZE = 4 * 1024 * 1024

# year -> vintage date, read directly from the already-verified MIGRATION_MAP
_YEAR_TO_DATE = {
    e["legacy"][0]: e["new"][0]
    for e in ma.MIGRATION_MAP
    if e["slug"] == "certified_roll" and e["legacy"][1] in ("ajr", "certified")
}

PLAN = []  # list of (src_path, dest_dir, flag_or_None)

def _add_dir_by_year(dirname, year, flag=None):
    """High-confidence path: date comes from MIGRATION_MAP via `year`."""
    date = _YEAR_TO_DATE.get(year)
    if date is None:
        print(f"  SKIP (no known vintage date for year {year}): {dirname}")
        return
    _add_dir_with_date(dirname, date, flag)

def _add_dir_with_date(dirname, date, flag=None):
    """Explicit-date path: used when there's no MIGRATION_MAP entry to
    read from (e.g. a genuinely new vintage). `date` is asserted by the
    caller, not looked up -- callers of this path must pass a `flag`
    explaining the assertion's basis, since it's not independently
    verified the way MIGRATION_MAP-derived dates are."""
    src_dir = os.path.join(CLAUDE, dirname)
    if not os.path.isdir(src_dir):
        print(f"  MISSING_SRC: {dirname}")
        return
    dest_dir = config._travis_archive("certified_roll", date)
    n = 0
    for r, _d, files in os.walk(src_dir):
        for fn in files:
            if fn.startswith("."):
                continue
            PLAN.append((os.path.join(r, fn), dest_dir, flag))
            n += 1
    if n == 0:
        print(f"  WARNING: {dirname} exists but contributed 0 files")

def build_plan():
    config._require_archive_mounted()
    PLAN.clear()

    # High-confidence: dates from MIGRATION_MAP directly.
    _add_dir_by_year("2021EARS092521 2", "2021")
    _add_dir_by_year("227EARS092822 (2) 2", "2022")
    _add_dir_by_year("227EARS082923 (2) 2", "2023")
    _add_dir_by_year("227EARS082824 (2) 2", "2024")

    # 2025-09-04 vintage date CONFIRMED (not inferred) from the signed MIF --
    # see module docstring. Both duplicate-suffixed folders feed the SAME
    # destination so the dedup logic in main() catches any overlap.
    CONFIRMED_2025_NOTE = (
        "CONFIRMED 2025-09-04 from the archived signed MIF (Form 50-792, "
        "\"Date Prepared 09/04/2025\", Chief Appraiser Leana H Mann) -- not "
        "inferred from the folder name."
    )
    for d in ("227EARS090425", "227EARS090425 2"):
        _add_dir_with_date(d, "2025-09-04", flag=CONFIRMED_2025_NOTE)

    # 2021 Certified Appraisal Roll PDFs -- destination is the CONFIRMED
    # 2022-01-25 vintage (their own True Automation report-generation
    # timestamp), not the September 2021 EARS delivery date. See module
    # docstring for the relocation history.
    for fn in ("2021 CERTIFIED APPRAISAL ROLL as of Supp 0_Alpha.pdf",
               "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf"):
        p = os.path.join(CLAUDE, fn)
        if os.path.isfile(p):
            PLAN.append((p, config._travis_archive("certified_roll", "2022-01-25"),
                         "RELOCATED: staged under 2021-09-25 originally (ASSUMED-VINTAGE), "
                         "then relocated (hash-verified both sides) to 2022-01-25, CONFIRMED "
                         "via the PDF's own True Automation report-generation timestamp, "
                         "01/25/2022 22:09"))

    # Registered slug (PX-20260822-05-rev1 ruling): "rates", not the
    # unregistered "adopted_tax_rates" -- see module docstring. No vintage
    # subfolder: this is a cumulative history workbook, not a dated export.
    p = os.path.join(CLAUDE, "2025RatesHistory1990-2025.xlsx")
    if os.path.isfile(p):
        PLAN.append((p, config._travis_archive("rates"), None))

    return PLAN


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)

    plan = build_plan()
    total_bytes = sum(os.path.getsize(s) for s, _, _ in plan)
    flagged = [p for p in plan if p[2]]

    print("=" * 78)
    print("PRE-COMMIT SUMMARY")
    print("=" * 78)
    print(f"  {len(plan)} file(s), {total_bytes:,} bytes ({total_bytes/1_073_741_824:.3f} GB)")
    print(f"  {len(flagged)} file(s) carry an ASSUMPTION FLAG -- review before --execute:")
    seen_flags = set()
    for src, dest, flag in flagged:
        if flag not in seen_flags:
            print(f"    - {flag}")
            seen_flags.add(flag)
    print("=" * 78)

    if not args.execute:
        for src, dest, flag in plan:
            marker = f"  [{flag}]" if flag else ""
            print(f"  PLAN: {src} -> {dest}{marker}")
        print("\nDRY RUN -- no files copied. Review flags above, then re-run with --execute.")
        return 0

    print("\n--execute given -- starting real copy...\n")
    copied = skipped = deduped = 0
    for src, dest_dir, flag in plan:
        fn = os.path.basename(src)
        dst = os.path.join(dest_dir, fn)
        os.makedirs(dest_dir, exist_ok=True)
        src_hash = sha256_of(src)
        if os.path.exists(dst):
            dst_hash = sha256_of(dst)
            if dst_hash == src_hash:
                print(f"  DEDUP (identical, skipped): {dst}")
                deduped += 1
                continue
            else:
                print(f"  *** MISMATCH: {dst} differs from {src} -- halting. Investigate. ***")
                return 1
        src_stat = os.stat(src)
        shutil.copy(src, dst)
        os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
        if sha256_of(dst) != src_hash:
            print(f"  *** HASH MISMATCH after copy: {dst} -- halting. ***")
            return 1
        print(f"  COPIED+VERIFIED: {src} -> {dst}")
        copied += 1

    print(f"\nDone. Copied: {copied}, Deduped(identical, skipped): {deduped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
