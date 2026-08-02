#!/usr/bin/env python3
"""
loaders/backfill_prop_unit_tax_year_geoid.py — Task M5-PERYEAR-GEOID, Step 5.

One-time backfill: existing prop_unit_tax_year rows (loaded before this
task's schema change) have geo_id = NULL. This script re-scans each
year's REAL source file (the same file the original loader used) via
the SAME shared parser used at load time, and UPDATEs
prop_unit_tax_year.geo_id for every existing row for that year with the
value that file actually states for that prop_id — re-deriving real,
source-verified data, not inferring or guessing.

Explicitly does NOT read from prop_unit.geo_id (the latest-known
column) — that's precisely the wrong value for old years, the bug this
whole task fixes (see parcel_rollup.py's ROLLUP_SQL comment and this
task's brief).

Source-per-year mapping (six years, 2021-2026):
  2021: config.AJR_FILES[2021]  — SPECIAL CASE, see "2021 data
        limitation" section below. This year's source format genuinely
        does not carry a real per-year geo_id field the way 2022-2026's
        PROP.TXT does.
  2022: config.CERT_DIR_2022 / PROP.TXT
  2023: config.CERT_DIR_2023 / PROP.TXT
  2024: config.CERT_DIR_2024 / PROP.TXT
  2025: config.CERT_DIR / PROP.TXT
  2026: config.CERT_DIR_2026 / PROP.TXT  — NOT config.PRELIM_2026_DIR.
        See "2026: certified vs preliminary" section below.

2026: certified vs preliminary — a real decision, stated explicitly
  config.py has TWO 2026 source directories: PRELIM_2026_DIR (the
  original 2026 Preliminary Export) and CERT_DIR_2026 (the 2026
  Certified Export, loaded later by load_certified_historical.py
  --year 2026). Per that loader's own module docstring, certified data
  is written with the SAME (prop_id, tax_year) upsert key as
  preliminary, so today's LIVE prop_unit_tax_year rows for tax_year=2026
  already reflect certified values (confirmed by
  snapshot_2026_preliminary.py's own docstring: "Today's
  load_certified_historical.py --year 2026 run overwrote the live
  preliminary values in parcel_tax_year / prop_unit_tax_year with
  certified values"). This script therefore re-scans CERT_DIR_2026 — the
  file that actually produced the geo_id currently sitting in each live
  2026 row — not PRELIM_2026_DIR. Re-scanning the preliminary file
  instead would risk backfilling a stale/superseded value for any
  account TCAD corrected between the preliminary and certified exports.
  If Diego needs the ORIGINAL preliminary 2026 geo_id specifically,
  that's already preserved separately in parcel_2026_preliminary_snapshot
  (Task M4-2026-PRELIM-SNAPSHOT) — out of scope for this table, per this
  task's own "out of scope" section.

2021 data limitation — STOP AND REPORT, not silently improvised around
  Every other year's source (2022-2026) is a fixed-width PROP.TXT export
  with a real geo_id field per record — iter_prop_records() reads it
  directly, so backfilling there is a genuine re-derivation from source.
  2021 is different: the AJR CSV format for that year (confirmed in
  loaders/load_ajr.py's own module docstring and inline comments) does
  NOT carry a geo_id field at all in the source file — field[6] holds
  prop_id, not geo_id, for the 2021 format specifically. load_ajr.py
  resolves 2021's geo_id via `pid_lookup.get(prop_id) or f"AJR{prop_id}"`
  — a lookup against prop_unit (whatever the DB's latest-known mapping
  happens to be AT LOAD TIME), falling back to a synthetic placeholder
  when even that lookup misses. There is no independent, source-verified
  "real, as-of-2021" geo_id to re-derive by re-scanning the file, because
  the file itself never contained one.
  This script backfills 2021 using the IDENTICAL method load_ajr.py's
  Step 2 fix already uses going forward (build_pid_lookup() against the
  current prop_unit table + the same AJR{prop_id} fallback) — not
  because it's a correct "real per-year value" in the same sense as
  2022-2026, but because it's the same best-available method already
  accepted as correct for this exact year's ongoing load path, and
  re-deriving it via a different method here would create a NEW
  inconsistency between what backfill writes and what a fresh load
  would write for the same prop_id. Flagging this explicitly per this
  task's own standing rule: a spec instruction ("re-scan each year's
  real source file") that turns out infeasible against the real 2021
  data should be reported, not silently treated as equivalent to the
  other five years.

Idempotent / safe to re-run: only rows where geo_id IS STILL NULL are
selected for update (see backfill_year()'s query below), so re-running
this script after it (or a Step-2-fixed loader) has already populated a
row is a fast no-op for that row, not a wasted re-write.

Usage:
    cd ~/Desktop/Claude\\ Files/parcel_app
    python3 loaders/backfill_prop_unit_tax_year_geoid.py --dry-run           # parse/match only, no writes
    python3 loaders/backfill_prop_unit_tax_year_geoid.py --year 2022         # backfill one year
    python3 loaders/backfill_prop_unit_tax_year_geoid.py --all-years         # backfill all six years

IMPORTANT (sandbox-vs-live disclosure): this script is written and
fixture-tested (loaders/test_backfill_prop_unit_tax_year_geoid.py)
against small synthetic fixtures in this sandbox, which has neither a
live database connection nor access to the real multi-gigabyte source
files (confirmed: config.CERT_DIR_2022 and siblings do not exist in
this sandbox). Diego needs to run this live himself, per this week's
two-environment discipline: local first (confirm via the printed
SELECT inet_server_addr() at startup), then production, each with
explicit target confirmation before any write.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders import ears_format
# NOTE: loaders.load_ajr (reused for the 2021 special case, see
# build_pid_to_geo_for_2021() below) is deliberately NOT imported here at
# module level -- load_ajr.py imports loaders.db, which imports psycopg2
# at module level, and psycopg2 is not installed in this sandbox. Importing
# it lazily inside the one function that actually needs it means this
# module (and its fixture tests) can still be imported and exercised for
# the 2022-2026 PROP.TXT path with no psycopg2 installed and no DB
# reachable -- same lazy-import pattern already used by
# loaders/snapshot_2026_preliminary.py and loaders/ingest_gate.py for the
# identical reason (see those modules' own comments).

UPDATE_SQL = """
    UPDATE prop_unit_tax_year
    SET geo_id = %s
    WHERE prop_id = %s AND tax_year = %s
"""

SELECT_NULL_ROWS_SQL = """
    SELECT prop_id FROM prop_unit_tax_year WHERE tax_year = %s AND geo_id IS NULL
"""

# Year -> Certified Export dir containing PROP.TXT. 2021 is handled
# separately (see module docstring's "2021 data limitation" section) and
# intentionally has no entry here.
CERT_SOURCE_DIRS = {
    2022: config.CERT_DIR_2022,
    2023: config.CERT_DIR_2023,
    2024: config.CERT_DIR_2024,
    2025: config.CERT_DIR,
    2026: config.CERT_DIR_2026,  # NOT PRELIM_2026_DIR -- see module docstring
}

ALL_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def build_pid_to_geo_from_prop_txt(prop_txt_path=None, prop_txt_lines=None):
    """
    Shared re-derivation for 2022-2026: read PROP.TXT via the same
    ears_format.iter_prop_records() parser every loader uses, return
    {prop_id: geo_id} exactly as that file states it. Accepts either a
    real `prop_txt_path` (production) or `prop_txt_lines` (fixture
    tests), same path/lines duality as every ears_format.py iterator.
    """
    pid_to_geo = {}
    for rec in ears_format.iter_prop_records(path=prop_txt_path, lines=prop_txt_lines):
        if rec["prop_id"] and rec["geo_id"]:
            pid_to_geo[rec["prop_id"]] = rec["geo_id"]
    return pid_to_geo


def build_pid_to_geo_for_2021(conn):
    """
    2021 special case — see module docstring's "2021 data limitation"
    section for why this can't be a true source-file re-derivation the
    way 2022-2026 are. Reuses load_ajr.py's own build_pid_lookup() (the
    exact method that loader's Step 2 fix already uses for 2021 rows
    going forward), rather than inventing a second, possibly-diverging
    implementation of the same fallback logic. Imported lazily here (not
    at module top level) since load_ajr.py imports loaders.db, which
    imports psycopg2 -- see the top-of-file import comment.
    """
    from loaders import load_ajr
    return load_ajr.build_pid_lookup(conn)


def backfill_year(conn, year, pid_to_geo, dry_run=False, verbose=True):
    """
    pid_to_geo: {prop_id: geo_id} for this year, already built by one of
    the two builder functions above (kept as a separate parameter, not
    built inline here, so this function — the part that actually reads
    the DB and writes — is fixture-testable independent of file I/O).
    """
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(SELECT_NULL_ROWS_SQL, (year,))
        null_prop_ids = {r[0] for r in cur.fetchall()}

    matched = [(pid_to_geo[pid], pid, year) for pid in null_prop_ids if pid in pid_to_geo]
    unmatched = len(null_prop_ids) - len(matched)

    _log(f"    {len(null_prop_ids):,} prop_unit_tax_year rows for {year} still need geo_id, "
         f"{len(matched):,} resolvable from source, {unmatched:,} not found in source (left NULL)")

    if dry_run:
        return {"year": year, "matched": len(matched), "updated": 0, "unmatched": unmatched}

    import psycopg2.extras
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPDATE_SQL, matched, page_size=2000)
    conn.commit()

    _log(f"    → {len(matched):,} rows updated  [{time.time()-t0:.1f}s]")
    return {"year": year, "matched": len(matched), "updated": len(matched), "unmatched": unmatched}


def run_year(conn, year, dry_run=False, verbose=True):
    """Full per-year entry point: resolves the right source, builds
    pid_to_geo, then calls backfill_year() to do the DB work."""
    def _log(msg):
        if verbose:
            print(msg)

    t0 = time.time()
    if year == 2021:
        _log(f"  Year 2021: building prop_id→geo_id via load_ajr.build_pid_lookup() "
             f"against the live prop_unit table (2021 AJR source has no real "
             f"per-year geo_id field of its own — see module docstring's "
             f"'2021 data limitation' section)")
        pid_to_geo = build_pid_to_geo_for_2021(conn)
    else:
        cert_dir = CERT_SOURCE_DIRS[year]
        prop_txt = os.path.join(cert_dir, "PROP.TXT")
        _log(f"  Year {year}: reading {prop_txt}")
        if not os.path.isdir(cert_dir):
            _log(f"    ERROR: source dir not found: {cert_dir} — skipping")
            return {"year": year, "matched": 0, "updated": 0, "unmatched": 0, "skipped_no_source": True}
        pid_to_geo = build_pid_to_geo_from_prop_txt(prop_txt_path=prop_txt)

    _log(f"    {len(pid_to_geo):,} prop_id→geo_id mappings from source  [{time.time()-t0:.1f}s]")
    return backfill_year(conn, year, pid_to_geo, dry_run=dry_run, verbose=verbose)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, choices=ALL_YEARS, help="Backfill a single tax_year")
    ap.add_argument("--all-years", action="store_true", help="Backfill all six years (2021-2026)")
    ap.add_argument("--dry-run", action="store_true", help="Parse and match only; no DB writes")
    args = ap.parse_args()

    if not args.all_years and args.year is None:
        ap.error("pass --year YYYY or --all-years")

    from loaders.db import get_conn
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        addr = cur.fetchone()[0]
    print(f"Target DB: {addr}  — confirm this is the environment you intend BEFORE any write commits.\n")

    years = ALL_YEARS if args.all_years else [args.year]

    results = []
    for year in years:
        print(f"\n{'='*65}")
        print(f"  Backfilling prop_unit_tax_year.geo_id for {year}"
              f"{'  (DRY RUN — no writes)' if args.dry_run else ''}")
        print(f"{'='*65}")
        results.append(run_year(conn, year, dry_run=args.dry_run))

    conn.close()

    print(f"\n{'='*65}")
    print("  SUMMARY")
    print(f"{'='*65}")
    for r in results:
        note = "  (source dir missing, skipped)" if r.get("skipped_no_source") else ""
        print(f"  {r['year']}: {r.get('updated', 0):,} updated, {r.get('unmatched', 0):,} unmatched{note}")


if __name__ == "__main__":
    main()
