#!/usr/bin/env python3
"""
loaders/check_taxcur_year_distribution.py

Read-only diagnostic (July 2026, incident response — Diego found a 2024
tax_billing row tagged data_source='taxcur_current', which should never
happen: load_tax_current.py's own docstring says it loads "TaxCurOpenData
(1).csv" — historically assumed to be 2025-only (see schema.sql's comment:
"Current-year tax office billing (TaxCurOpenData — 2025 only in supplied
data)"). This script tests that assumption directly against the real file
instead of trusting the old comment. Never opens a DB connection, never
writes anything — reads config.TAX_CUR_CSV and tallies every distinct
TAXYEAR value found, plus a few sample PARCEL values for any non-2025 year
so you can spot-check specific accounts (e.g. whether 0100030804 is one of
them).

Usage:
    python3 loaders/check_taxcur_year_distribution.py
"""
import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config


def main():
    path = config.TAX_CUR_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found -- nothing to check")
        return

    print(f"  Scanning {path} ({os.path.getsize(path)/1e6:.0f} MB, read-only, no DB)…")

    year_counts = Counter()
    year_samples = defaultdict(list)  # year -> up to 5 sample (geo_id, billing_num)
    n_blank_year = 0
    n_blank_parcel = 0

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, 1):
            raw_parcel = (row.get("PARCEL", "") or "").strip().strip('"')
            geo_id = raw_parcel[:10].strip() if raw_parcel else None
            year_raw = (row.get("TAXYEAR", "") or "").strip()

            if not geo_id:
                n_blank_parcel += 1
                continue
            if not year_raw:
                n_blank_year += 1
                continue

            try:
                year = int(year_raw)
            except ValueError:
                year = f"UNPARSEABLE:{year_raw!r}"

            year_counts[year] += 1
            if len(year_samples[year]) < 5:
                year_samples[year].append((geo_id, row.get("BILLING", "")))

            if lineno % 200_000 == 0:
                print(f"    … scanned {lineno:,} rows", flush=True)

    print("\n  TAXYEAR distribution in the source file:")
    for year, count in sorted(year_counts.items(), key=lambda kv: (isinstance(kv[0], str), kv[0])):
        flag = "  <-- NOT 2025" if year != 2025 else ""
        print(f"    {year}: {count:,} rows{flag}")
        if year != 2025:
            for geo_id, billing_num in year_samples[year]:
                print(f"        sample: geo_id={geo_id}  billing_num={billing_num}")

    print(f"\n  {n_blank_parcel:,} rows skipped (blank/malformed PARCEL)")
    print(f"  {n_blank_year:,} rows skipped (blank TAXYEAR)")

    non_2025_total = sum(c for y, c in year_counts.items() if y != 2025)
    if non_2025_total:
        print(f"\n  *** {non_2025_total:,} source rows carry a TAXYEAR other than 2025. ***")
        print("  Every one of these would be written to tax_billing/tax_billing_entity "
              "for that OTHER year by any live (non-dry-run) run of load_tax_current.py "
              "before the TAXYEAR-reject fix -- confirming the cross-year contamination "
              "mechanism is real and present in the source file, not just theoretical.")
    else:
        print("\n  No non-2025 TAXYEAR rows found -- the file really is 2025-only. "
              "(If Diego's live evidence still shows a contaminated 2024 row, the "
              "contamination came from a DIFFERENT run against a DIFFERENT/older "
              "copy of this file, not this one -- worth checking file history.)")


if __name__ == "__main__":
    main()
