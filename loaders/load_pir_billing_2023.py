#!/usr/bin/env python3
"""
loaders/load_pir_billing_2023.py — Thin per-year entry point for the 2023
PIR billing export ("DiegoPIR2023.xlsx"). See load_pir_billing_2022.py and
loaders/pir_xlsx_common.py for the shared design notes.

2023 uses the INLINE-STRING format, confirmed by direct decoding: a real
but empty sharedStrings.xml (`<sst count="0" uniqueCount="0"/>`) and
t="inlineStr" cells for every text field (including TXACCNUM, entity
codes). Numeric fields use t="n" with a normal <v>. TXACCYER comes through
as a float-string ("2023.0") -- handled by pir_xlsx_common._year_matches(),
not by this file.

0100030105's 2023 sanity figure ($76,601.36) was independently confirmed in
the investigation by summing TXBASTAX1-5 from the real row and matching it
exactly against Diego's own known-good total. 0100030109 has no
independently pre-confirmed figure for 2023 -- included as a data point,
not a pass/fail check (see pir_xlsx_common.verify_sanity_parcels' handling
of a None expected value).

Usage: identical to load_pir_billing_2021_full.py -- see that file or
load_pir_billing_2022.py for the full --inspect/--dry-run/load workflow.

Do NOT run this against the live database without reviewing the review-log
CSV from a --dry-run pass first. Diego runs the real load himself.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.pir_xlsx_common import run_cli

TAX_YEAR = 2023
DATA_SOURCE = "pir_billing_2023_full"
CONFIDENCE_LEVEL = "verified"
FILEPATH = os.path.join(config.DATA_DIR, "DiegoPIR2023.xlsx")
SANITY_EXPECTED = {
    "0100030105": 76601.36,   # independently confirmed pre-build (investigation)
    "0100030109": None,       # no independent figure for this parcel/year -- report only
}
REVIEW_LOG_DEFAULT = os.path.join(os.path.dirname(__file__), ".pir_2023_review.csv")

if __name__ == "__main__":
    run_cli(TAX_YEAR, DATA_SOURCE, CONFIDENCE_LEVEL, FILEPATH,
             SANITY_EXPECTED, REVIEW_LOG_DEFAULT)
