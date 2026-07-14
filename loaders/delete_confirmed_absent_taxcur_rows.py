#!/usr/bin/env python3
"""
loaders/delete_confirmed_absent_taxcur_rows.py

Deletes tax_billing + tax_billing_entity rows for an EXACT, explicit list of
(geo_id, tax_year) pairs -- built for the 62 rows left over after the
2021-2024 PIR restoration, where check_geo_ids_in_pir_source.py confirmed
the account is genuinely absent from the real PIR source file for that year
(NOT_FOUND), so there is no correct value to restore and the contaminated
data_source='taxcur_current' row/entity rows should simply be removed.

SAFETY DESIGN (per Diego's explicit brief -- read before changing any of
this):

1. Scoped to an EXACT list of (geo_id, tax_year) pairs via a SQL
   `WHERE (geo_id, tax_year) IN ((%s,%s), (%s,%s), ...)` clause -- never a
   broader `WHERE data_source = 'taxcur_current' AND tax_year BETWEEN 2021
   AND 2024`, per Diego's explicit instruction: a future legitimate
   'taxcur_current'-tagged row in that year range must never be caught by
   this script.

2. Re-verifies data_source = 'taxcur_current' for every pair immediately
   before deleting (a live re-check, not trusting the input list blindly).
   Any pair whose live data_source has since changed (e.g. someone else
   already fixed it, or the list is stale) is SKIPPED with a warning, never
   force-deleted.

3. Deletes tax_billing_entity rows for a pair in the SAME transaction as
   its tax_billing row, so the two tables can never end up out of sync
   (an orphaned entity row with no parent billing row, or vice versa) --
   same discipline as load_tax_current.py's --new-only design this session.

4. --dry-run (default behavior unless --execute is passed) reports exactly
   what would be deleted -- current data_source, total_tax, and entity row
   count per pair -- without deleting anything. Requires --execute to
   actually write.

5. Hard cap of --max-pairs (default 200, well above the real 62) on how
   many pairs a single run will touch without --force -- a sanity backstop
   in case the input file is ever accidentally a much broader list than
   intended.

Usage:
    # Step 1: confirm the list first, always
    python3 loaders/delete_confirmed_absent_taxcur_rows.py \\
        --pairs-file confirmed_not_found_62.csv --dry-run

    # Step 2: only after reviewing the dry-run output
    python3 loaders/delete_confirmed_absent_taxcur_rows.py \\
        --pairs-file confirmed_not_found_62.csv --execute

    confirmed_not_found_62.csv: header "geo_id,tax_year" -- e.g. the
    --output of check_geo_ids_in_pir_source.py, filtered to rows where
    result == NOT_FOUND. This script does NOT read the "result" column
    itself and does NOT trust it -- filter to NOT_FOUND yourself before
    passing the file in, and this script's own live re-check (safety #2
    above) is the real guard at delete time.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn

EXPECTED_DATA_SOURCE = "taxcur_current"


def load_pairs(path):
    """Same tolerant parsing as check_geo_ids_in_pir_source.py's
    load_pairs() -- header "geo_id,tax_year" or a plain two-column CSV."""
    ordered = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [fn.strip().lower() for fn in (reader.fieldnames or [])]
        if "geo_id" in fieldnames and "tax_year" in fieldnames:
            for row in reader:
                gid = (row.get("geo_id") or "").strip()
                yr_raw = (row.get("tax_year") or "").strip()
                if not gid or not yr_raw:
                    continue
                ordered.append((gid, int(float(yr_raw))))
            return ordered

        f.seek(0)
        raw_reader = csv.reader(f)
        for row in raw_reader:
            if len(row) < 2:
                continue
            gid, yr = row[0].strip(), row[1].strip()
            if gid.lower() == "geo_id":
                continue
            try:
                yr_i = int(float(yr))
            except ValueError:
                continue
            ordered.append((gid, yr_i))
    return ordered


def fetch_current_state(conn, pairs):
    """Live re-check (safety #2): for every requested pair, fetch its
    CURRENT data_source/total_tax and entity row count. Returns
    {(geo_id, tax_year): {"data_source":..., "total_tax":..., "n_entity":...}}
    -- pairs with no matching tax_billing row at all are simply absent from
    the returned dict (already gone / never existed -- reported separately,
    never treated as a delete target)."""
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geo_id, tax_year, data_source, total_tax "
            "FROM tax_billing WHERE (geo_id, tax_year) IN %s",
            (tuple(pairs),),
        )
        billing = {(r[0], r[1]): {"data_source": r[2], "total_tax": r[3]} for r in cur.fetchall()}

        cur.execute(
            "SELECT geo_id, tax_year, COUNT(*) FROM tax_billing_entity "
            "WHERE (geo_id, tax_year) IN %s GROUP BY geo_id, tax_year",
            (tuple(pairs),),
        )
        entity_counts = {(r[0], r[1]): r[2] for r in cur.fetchall()}

    result = {}
    for key, info in billing.items():
        info["n_entity"] = entity_counts.get(key, 0)
        result[key] = info
    return result


def delete_pairs(conn, pairs):
    """Deletes tax_billing_entity rows, then tax_billing rows, for exactly
    `pairs`, in one transaction. Caller is responsible for having already
    filtered `pairs` down to only the ones that passed the live
    data_source re-check (safety #2) -- this function does not re-check,
    it deletes whatever it's given, tightly scoped by the IN clause."""
    if not pairs:
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tax_billing_entity WHERE (geo_id, tax_year) IN %s",
            (tuple(pairs),),
        )
        n_entity = cur.rowcount
        cur.execute(
            "DELETE FROM tax_billing WHERE (geo_id, tax_year) IN %s",
            (tuple(pairs),),
        )
        n_billing = cur.rowcount
    conn.commit()
    return n_billing, n_entity


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs-file", required=True,
                         help="CSV with geo_id,tax_year columns")
    parser.add_argument("--execute", action="store_true",
                         help="Actually delete. Without this flag, always runs as --dry-run.")
    parser.add_argument("--max-pairs", type=int, default=200,
                         help="Refuse to run (even dry-run) if the input file has more than "
                              "this many pairs, unless --force is also passed. Default 200 -- "
                              "well above the real 62, a backstop against an accidentally "
                              "broader input file.")
    parser.add_argument("--force", action="store_true",
                         help="Override the --max-pairs safety cap.")
    args = parser.parse_args()

    pairs = load_pairs(args.pairs_file)
    print(f"  Loaded {len(pairs):,} (geo_id, tax_year) pair(s) from {args.pairs_file}")

    if len(pairs) > args.max_pairs and not args.force:
        print(f"\n  *** REFUSING TO RUN: {len(pairs):,} pairs exceeds --max-pairs "
              f"{args.max_pairs:,}. This script is meant for a small, exact, hand-confirmed "
              f"list -- if you really intend to touch this many rows, re-run with --force. ***")
        return

    if not pairs:
        print("  Nothing to do.")
        return

    conn = get_conn()
    try:
        current = fetch_current_state(conn, pairs)

        to_delete = []
        skipped_gone = []
        skipped_changed = []
        for gid, year in pairs:
            info = current.get((gid, year))
            if info is None:
                skipped_gone.append((gid, year))
                continue
            if info["data_source"] != EXPECTED_DATA_SOURCE:
                skipped_changed.append((gid, year, info["data_source"]))
                continue
            to_delete.append((gid, year, info))

        print(f"\n  Live re-check against tax_billing (safety #2 -- never trusting the "
              f"input list blindly):")
        print(f"    {len(to_delete):,} pair(s) confirmed data_source='{EXPECTED_DATA_SOURCE}' "
              f"-- eligible for deletion")
        if skipped_gone:
            print(f"    {len(skipped_gone):,} pair(s) SKIPPED -- no tax_billing row found at all "
                  f"(already gone, or never existed): {skipped_gone}")
        if skipped_changed:
            print(f"    {len(skipped_changed):,} pair(s) SKIPPED -- data_source has changed "
                  f"since your list was pulled, no longer '{EXPECTED_DATA_SOURCE}':")
            for gid, year, actual_source in skipped_changed:
                print(f"        ({gid}, {year}): now data_source={actual_source!r}")

        print(f"\n  Detail for the {len(to_delete):,} pair(s) that would be deleted:")
        total_entity_rows = 0
        for gid, year, info in to_delete:
            total_entity_rows += info["n_entity"]
            print(f"    ({gid}, {year}): total_tax={info['total_tax']}, "
                  f"{info['n_entity']} tax_billing_entity row(s)")

        if not args.execute:
            print(f"\n  DRY RUN -- would delete {len(to_delete):,} tax_billing row(s) and "
                  f"{total_entity_rows:,} tax_billing_entity row(s). Re-run with --execute "
                  f"to actually delete.")
            return

        keys = [(gid, year) for gid, year, _ in to_delete]
        n_billing, n_entity = delete_pairs(conn, keys)
        print(f"\n  DELETED {n_billing:,} tax_billing row(s), {n_entity:,} "
              f"tax_billing_entity row(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
