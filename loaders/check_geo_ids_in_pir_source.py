#!/usr/bin/env python3
"""
loaders/check_geo_ids_in_pir_source.py

Read-only diagnostic (July 2026, restoration follow-up). After restoring
2021-2024 tax_billing from the four PIR loaders, 62 (geo_id, tax_year) pairs
remained tagged data_source='taxcur_current' (the contaminated tag) because
the PIR reruns had nothing to overwrite them with. Before deleting anything,
this checks each pair directly against the REAL PIR source file for that
year, the same way check_geo_ids_in_taxcur_source.py checked the "A" gap
against TaxCurOpenData -- never opens a DB connection, never writes
anything, only reads the local .xlsx files and a CSV of pairs you supply.

For each (geo_id, tax_year) pair, reports one of four outcomes:

  EXACT_MATCH_CORRECT_YEAR
      A TXACCNUM with this geo_id prefix exists in the file, tagged with
      THIS target tax_year. This would be surprising -- it means the row
      SHOULD have been loaded by the PIR rerun. Worth checking why it
      wasn't (e.g. reconcile_geo_ids' parcel-table join, or a duplicate-
      resolution edge case) rather than deleting.

  WRONG_YEAR_IN_FILE
      A TXACCNUM with this geo_id prefix exists in the file, but every
      occurrence is tagged with a DIFFERENT TXACCYER than the target year.
      Explains why the PIR loader's own year filter correctly skipped it
      for this year -- the account is genuinely not billed for this year
      in the county's own PIR extract, even though it appears in the file
      under some other year tag.

  FUZZY_MATCH
      Not found via the clean TXACCNUM[:10] == geo_id check, but the
      geo_id string appears somewhere inside some row's raw TXACCNUM
      field (different position/padding) -- a potential matching-bug
      case, though PIR TXACCNUM is a clean 14-digit field per the 2021
      loader's own verified finding, so this is expected to be rare/zero.

  NOT_FOUND
      The geo_id does not appear anywhere in the file's TXACCNUM column,
      in any form, under any year. Genuinely absent from this PIR source
      -- the strongest evidence that deleting the contaminated row (rather
      than trying to re-derive a correct value) is the right call.

Usage:
    python3 loaders/check_geo_ids_in_pir_source.py --pairs-file contaminated_62.csv

    contaminated_62.csv: header row "geo_id,tax_year" then one pair per
        line, e.g. the 62 rows Diego pulled live:
        SELECT geo_id, tax_year FROM tax_billing
        WHERE data_source = 'taxcur_current' AND tax_year BETWEEN 2021 AND 2024;

    --output PATH   write full per-pair results to this CSV (default: print
                    summary only, grouped by year)
"""
import argparse
import csv
import os
import re
import sys
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.pir_xlsx_common import (
    load_shared_strings, parse_header, detect_string_format,
    _decode_cell, _year_matches, NS,
)


def filepath_for_year(tax_year):
    if tax_year == 2021:
        return config.PIR_2021_FULL_XLSX
    if tax_year in (2022, 2023, 2024):
        return os.path.join(config.DATA_DIR, f"DiegoPIR{tax_year}.xlsx")
    return None


def load_pairs(path):
    """Returns {tax_year: set(geo_id, ...)} and the original ordered list of
    (geo_id, tax_year) for final per-pair reporting."""
    by_year = defaultdict(set)
    ordered = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        # Tolerate either header casing / column order, and a headerless
        # two-column file (first row treated as data if it doesn't look
        # like "geo_id"/"tax_year").
        fieldnames = [fn.strip().lower() for fn in (reader.fieldnames or [])]
        if "geo_id" not in fieldnames or "tax_year" not in fieldnames:
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
                by_year[yr_i].add(gid)
            return by_year, ordered

        for row in reader:
            gid = (row.get("geo_id") or row.get("GEO_ID") or "").strip()
            yr_raw = (row.get("tax_year") or row.get("TAX_YEAR") or "").strip()
            if not gid or not yr_raw:
                continue
            yr_i = int(float(yr_raw))
            ordered.append((gid, yr_i))
            by_year[yr_i].add(gid)
    return by_year, ordered


def build_minimal_cell_regex(needed_letters):
    """Same dual-form (shared-string / inline-string) cell regex as
    pir_xlsx_common.build_row_cell_regex, but this file only ever asks for
    TXACCNUM/TXACCYER's two column letters -- no entity fields -- so scans
    are fast even though a full 400K+ row file is read."""
    colpat = "|".join(sorted(needed_letters, key=len, reverse=True))
    pattern = (
        rf'<c r="({colpat})\d+"(?:[^>]*?t="(\w+)")?[^>]*>'
        rf'(?:<v>([^<]*)</v>|<is><t(?:[^>]*)>([^<]*)</t></is>)'
        rf'</c>'
    )
    return re.compile(pattern.encode())


def scan_pir_file(filepath, target_geo_ids, target_year):
    """One streamed pass over filepath. Returns
    {geo_id: 'EXACT_MATCH_CORRECT_YEAR'|'WRONG_YEAR_IN_FILE'|'FUZZY_MATCH'|'NOT_FOUND'}
    for every geo_id in target_geo_ids."""
    targets = set(target_geo_ids)
    correct_year_found = set()
    wrong_year_found = set()
    fuzzy_found = set()

    z = zipfile.ZipFile(filepath)
    detect_string_format(z)  # raises on disagreement -- same discipline as the loaders
    shared = load_shared_strings(z)
    name_to_letter = parse_header(z, shared)
    for needed in ("TXACCNUM", "TXACCYER"):
        if needed not in name_to_letter:
            raise RuntimeError(
                f"{filepath}: header missing {needed} -- file layout may "
                f"have changed, stop and re-inspect."
            )

    letter_to_name = {
        name_to_letter["TXACCNUM"]: "TXACCNUM",
        name_to_letter["TXACCYER"]: "TXACCYER",
    }
    cell_re = build_minimal_cell_regex(set(letter_to_name.keys()))
    row_re = re.compile(rb'<row r="(\d+)"[^>]*>(.*?)</row>', re.DOTALL)

    CHUNK = 32 * 1024 * 1024
    tail = b""
    n_rows = 0
    with z.open("xl/worksheets/sheet1.xml") as f:
        first = True
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            matches = list(row_re.finditer(buf))
            if matches:
                tail = buf[matches[-1].end():]
                if len(tail) > 5_000_000:
                    tail = tail[-2_000_000:]
            else:
                tail = buf[-2_000_000:] if len(buf) > 2_000_000 else buf
            for m in matches:
                block = m.group(2)
                if first:
                    first = False
                    continue
                row = {}
                for cm in cell_re.finditer(block):
                    letter_bytes, ttype_b, v_val_b, is_val_b = (
                        cm.group(1), cm.group(2), cm.group(3), cm.group(4)
                    )
                    letter = letter_bytes.decode()
                    name = letter_to_name.get(letter)
                    if not name:
                        continue
                    ttype = ttype_b.decode() if ttype_b else None
                    v_val = v_val_b.decode() if v_val_b is not None else None
                    is_val = is_val_b.decode() if is_val_b is not None else None
                    row[name] = _decode_cell(ttype, v_val, is_val, shared)
                n_rows += 1

                accnum = row.get("TXACCNUM")
                if accnum:
                    if len(accnum) == 14 and accnum.isdigit():
                        geo_id = accnum[:10]
                        if geo_id in targets:
                            if _year_matches(row.get("TXACCYER"), target_year):
                                correct_year_found.add(geo_id)
                            else:
                                wrong_year_found.add(geo_id)
                    else:
                        # Malformed TXACCNUM (unexpected per the 2021 loader's
                        # verified "always 14-digit" finding, but checked
                        # defensively) -- fuzzy substring check.
                        L = len(accnum)
                        if L > 10:
                            for start in range(0, L - 9):
                                sub = accnum[start:start + 10]
                                if sub in targets:
                                    fuzzy_found.add(sub)

    result = {}
    for gid in target_geo_ids:
        if gid in correct_year_found:
            result[gid] = "EXACT_MATCH_CORRECT_YEAR"
        elif gid in wrong_year_found:
            result[gid] = "WRONG_YEAR_IN_FILE"
        elif gid in fuzzy_found:
            result[gid] = "FUZZY_MATCH"
        else:
            result[gid] = "NOT_FOUND"
    return result, n_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs-file", required=True,
                         help="CSV with geo_id,tax_year columns (header row expected)")
    parser.add_argument("--output", default=None,
                         help="Write full per-pair results to this CSV")
    args = parser.parse_args()

    by_year, ordered = load_pairs(args.pairs_file)
    print(f"  Loaded {len(ordered):,} (geo_id, tax_year) pairs across "
          f"{len(by_year)} year(s): {sorted(by_year.keys())}")

    all_results = {}  # (geo_id, tax_year) -> outcome
    for year in sorted(by_year.keys()):
        filepath = filepath_for_year(year)
        if filepath is None:
            print(f"\n  *** No known PIR file mapping for tax_year={year} -- skipping "
                  f"{len(by_year[year])} pair(s), reported as UNCHECKED. ***")
            for gid in by_year[year]:
                all_results[(gid, year)] = "UNCHECKED_NO_FILE_MAPPING"
            continue
        if not os.path.exists(filepath):
            print(f"\n  WARNING: {filepath} not found -- skipping "
                  f"{len(by_year[year])} pair(s) for {year}, reported as UNCHECKED.")
            for gid in by_year[year]:
                all_results[(gid, year)] = "UNCHECKED_FILE_NOT_FOUND"
            continue

        print(f"\n  Scanning {filepath} ({os.path.getsize(filepath)/1e6:.0f} MB) "
              f"for {len(by_year[year])} target geo_id(s), tax_year={year}…")
        result, n_rows = scan_pir_file(filepath, by_year[year], year)
        print(f"    {n_rows:,} data rows scanned")
        for gid, outcome in result.items():
            all_results[(gid, year)] = outcome

    counts = defaultdict(int)
    for outcome in all_results.values():
        counts[outcome] += 1

    print("\n  ── Summary across all pairs ──")
    for label in ("EXACT_MATCH_CORRECT_YEAR", "WRONG_YEAR_IN_FILE", "FUZZY_MATCH",
                  "NOT_FOUND", "UNCHECKED_NO_FILE_MAPPING", "UNCHECKED_FILE_NOT_FOUND"):
        if counts[label]:
            print(f"    {label}: {counts[label]}")

    print("\n  ── Per-pair detail ──")
    for gid, year in ordered:
        print(f"    ({gid}, {year}): {all_results[(gid, year)]}")

    if args.output:
        with open(args.output, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["geo_id", "tax_year", "result"])
            for gid, year in ordered:
                w.writerow([gid, year, all_results[(gid, year)]])
        print(f"\n  Full results written to {args.output}")


if __name__ == "__main__":
    main()
