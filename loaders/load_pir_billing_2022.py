#!/usr/bin/env python3
"""
loaders/load_pir_billing_2022.py — Thin per-year entry point for the 2022
PIR billing export ("DiegoPIR2022.xlsx"). All parsing/aggregation logic
lives in loaders/pir_xlsx_common.py (shared with 2023/2024, and in spirit
with load_pir_billing_2021_full.py, which predates this refactor) -- this
file supplies only the constants that differ per year.

2022 uses the SHARED-STRING format (like 2021), confirmed by direct
decoding: a real, large sharedStrings.xml (144MB uncompressed, 3,930,954
entries) and t="s" cells. Its real column headers were decoded in full
(all 264, A-JD) during the investigation and matched the assumed
2021/2023/2024 layout exactly for the billing block (A-BS), including the
slot-3 TXBASTAX3/TXENTCOD3 order swap (columns R/S) and the slot-5
irregularity -- neither affects this loader since columns are looked up by
real header name, not position.

Sanity-check figures below are the investigation's own confirmed, fully
re-verified totals for parcel 0100030105 (all 10 slots decoded, cross-
checked against a full-file scan proving no hidden duplicate sub-account)
and 0100030109 (found, not independently pre-confirmed, but included as
the investigation's second real data point).

Usage: identical to load_pir_billing_2021_full.py --
    python3 loaders/load_pir_billing_2022.py --inspect
    python3 loaders/load_pir_billing_2022.py --dry-run
    python3 loaders/load_pir_billing_2022.py
    python3 loaders/load_pir_billing_2022.py --skip-metrics

Do NOT run this against the live database without reviewing the review-log
CSV from a --dry-run pass first -- same process as every prior real data
load this session. Diego runs the real load himself.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.pir_xlsx_common import run_cli

TAX_YEAR = 2022
DATA_SOURCE = "pir_billing_2022_full"
CONFIDENCE_LEVEL = "verified"
FILEPATH = os.path.join(config.DATA_DIR, "DiegoPIR2022.xlsx")
SANITY_EXPECTED = {
    "0100030105": 58432.29,     # investigation: fully re-verified, all 10 slots
    "0100030109": 1303196.40,   # investigation: found (second data point, no
                                 # independent pre-confirmed figure for this
                                 # parcel/year)
}
REVIEW_LOG_DEFAULT = os.path.join(os.path.dirname(__file__), ".pir_2022_review.csv")

if __name__ == "__main__":
    run_cli(TAX_YEAR, DATA_SOURCE, CONFIDENCE_LEVEL, FILEPATH,
             SANITY_EXPECTED, REVIEW_LOG_DEFAULT)
