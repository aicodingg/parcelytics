#!/usr/bin/env python3
"""
loaders/check_geo_ids_in_taxcur_source.py

Read-only diagnostic (July 2026, per Diego's "is this a matching bug or a
real absence" question on the 54,115-row 2025 billing gap). Does NOT open a
database connection and does NOT write anything anywhere -- it only reads
the local TaxCurOpenData CSV and a plain-text list of geo_ids you supply,
then reports, for each geo_id, one of three outcomes:

  EXACT_MATCH   -- found via the loader's own PARCEL[:10] logic. (If a
                   geo_id you're checking shows this, it should NOT actually
                   be missing from tax_billing -- worth double-checking your
                   input list against a fresh live query.)
  FUZZY_MATCH   -- not found via PARCEL[:10], but the geo_id appears
                   SOMEWHERE in some row's raw PARCEL field (different
                   position/padding). This is the interesting case: it means
                   the account IS in the county's file, just keyed
                   differently than load_tax_current.py expects -- a real,
                   fixable matching bug, not a data-absence question.
  NOT_FOUND     -- the geo_id does not appear anywhere in the PARCEL column
                   of this file, in any form. This points toward "genuinely
                   not in this extract" -- a question for the county, not a
                   loader bug.

Usage:
    python3 loaders/check_geo_ids_in_taxcur_source.py --geo-ids-file missing_A_geoids.txt

    missing_A_geoids.txt: one geo_id per line, e.g. the output of
        SELECT p.geo_id FROM parcel p
        WHERE p.state_cd1 = 'A'
          AND NOT EXISTS (SELECT 1 FROM tax_billing tb
                           WHERE tb.geo_id = p.geo_id AND tb.tax_year = 2025);
    run live and saved to a file -- this script never queries the DB itself.

    --limit N   only check the first N geo_ids from the file (useful for a
                quick spot-check). The full set runs in one pass regardless
                of size -- see scan_source()'s docstring for why this stays
                fast even at 10,000+ geo_ids.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config


def load_geo_id_list(path, limit=None):
    ids = []
    with open(path) as f:
        for line in f:
            gid = line.strip().strip('"').strip(",")
            if gid:
                ids.append(gid)
            if limit and len(ids) >= limit:
                break
    return ids


def scan_source(path, target_ids):
    """One pass over the CSV. Returns dict: geo_id -> 'EXACT_MATCH' | 'FUZZY_MATCH' | 'NOT_FOUND'.

    Performance note: naively checking "is target_gid a substring of
    raw_parcel" for every target against every row is O(len(targets) x
    n_rows) -- for the full 10,715-geo_id "A" set against ~426K+ source
    rows, that's ~4.5 BILLION string-search calls, hours of runtime. Instead,
    each row's raw PARCEL string (14 chars per the loader's own docstring,
    so at most 5 length-10 substrings) is decomposed into every length-10
    substring it contains, and each of those is checked against the target
    set via O(1) set membership -- O(n_rows x ~5) instead, seconds not
    hours regardless of how many geo_ids you're checking.
    """
    targets = set(target_ids)
    exact_found = set()
    fuzzy_found = set()

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, 1):
            raw_parcel = (row.get("PARCEL", "") or "").strip().strip('"')
            if not raw_parcel:
                continue
            exact_geo_id = raw_parcel[:10].strip()
            if exact_geo_id in targets:
                exact_found.add(exact_geo_id)

            L = len(raw_parcel)
            if L > 10:
                for start in range(0, L - 9):
                    sub = raw_parcel[start:start + 10]
                    if sub != exact_geo_id and sub in targets:
                        fuzzy_found.add(sub)

            if lineno % 200_000 == 0:
                print(f"    … scanned {lineno:,} source rows", flush=True)

    result = {}
    for gid in target_ids:
        if gid in exact_found:
            result[gid] = "EXACT_MATCH"
        elif gid in fuzzy_found:
            result[gid] = "FUZZY_MATCH"
        else:
            result[gid] = "NOT_FOUND"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--geo-ids-file", required=True,
                         help="Plain text file, one geo_id per line")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only check the first N geo_ids from the file")
    parser.add_argument("--output", default=None,
                         help="Write full per-geo_id results to this CSV (default: print summary only)")
    args = parser.parse_args()

    path = config.TAX_CUR_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found -- nothing to check against")
        return

    target_ids = load_geo_id_list(args.geo_ids_file, limit=args.limit)
    print(f"  Checking {len(target_ids):,} geo_ids against {path} "
          f"({os.path.getsize(path)/1e6:.0f} MB, one read-only pass)…")

    results = scan_source(path, target_ids)

    counts = {"EXACT_MATCH": 0, "FUZZY_MATCH": 0, "NOT_FOUND": 0}
    for v in results.values():
        counts[v] += 1

    print("\n  Results:")
    print(f"    EXACT_MATCH  (should not have been in your missing-row list): {counts['EXACT_MATCH']:,}")
    print(f"    FUZZY_MATCH  (in the file, keyed differently -- real matching bug): {counts['FUZZY_MATCH']:,}")
    print(f"    NOT_FOUND    (genuinely absent from this extract): {counts['NOT_FOUND']:,}")

    if args.output:
        with open(args.output, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["geo_id", "result"])
            for gid, res in results.items():
                w.writerow([gid, res])
        print(f"\n  Full results written to {args.output}")


if __name__ == "__main__":
    main()
