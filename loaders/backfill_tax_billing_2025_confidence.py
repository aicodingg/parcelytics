#!/usr/bin/env python3
"""
loaders/backfill_tax_billing_2025_confidence.py

One-time backfill for the 426,491 (or however many exist at run time) 2025
tax_billing rows that were written by loaders/load_tax_current.py BEFORE its
July 2026 write-time confidence fix (see that file's own comments, and
KNOWN_LIMITATIONS.md's "tax_billing.total_tax (2025): 0.00 for ~93% of rows"
section). Those existing rows have data_source/confidence_level = NULL, and
~93% of them have total_tax = 0.00 (the TaxCurOpenData source-CSV quirk),
even though tax_billing_entity already holds the real, correct per-entity
amounts for the same (geo_id, tax_year).

This is a pure DB-side UPDATE, not a re-fetch/re-parse of the source CSV:
- tax_billing_entity.amount_due is already correct today (confirmed in
  ENTITY_CODE_AUDIT.md: "tax_billing_entity.amount_due — correct for all
  entities including PIDs"), so summing it per (geo_id, tax_year) reproduces
  exactly what a full rerun of the fixed load_tax_current.py would compute
  for total_tax/confidence_level on these same rows -- no new information is
  needed from Travis County to do this correctly.
- Only rows with data_source IS NULL are touched (i.e. rows this specific
  loader wrote before the fix). A row with any other data_source is left
  alone -- this backfill only fixes load_tax_current.py's own legacy rows,
  it does not re-adjudicate provenance for rows another loader already
  tagged (portal_scrape, pir_billing_2021_full, etc.).

Three passes, in order:
  1. Rows with a genuinely populated (nonzero) source total_tax -> 'verified'.
  2. Rows with total_tax NULL/0 but a positive entity-DUE sum on file ->
     total_tax is corrected to that sum, tagged 'derived'.
  3. Anything left (total_tax NULL/0 AND no entity data at all) -> tagged
     with data_source only; confidence_level stays NULL ("no usable total"),
     same semantics load_tax_current.py's write-time fix now uses for that
     case going forward.

Usage:
    python3 loaders/backfill_tax_billing_2025_confidence.py --dry-run
        # Report how many rows each pass WOULD affect, write nothing.
    python3 loaders/backfill_tax_billing_2025_confidence.py
        # Apply all three passes for real, then print before/after counts.

Diego runs this against the live DB -- no live DB in this sandbox to run it
against directly.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders.db import get_conn
from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols

DATA_SOURCE = "taxcur_current"

COUNT_SQL = {
    "verified": """
        SELECT COUNT(*) FROM tax_billing
        WHERE tax_year = 2025 AND data_source IS NULL
          AND total_tax IS NOT NULL AND total_tax != 0
    """,
    "derived": """
        SELECT COUNT(*) FROM tax_billing tb
        WHERE tb.tax_year = 2025 AND tb.data_source IS NULL
          AND (tb.total_tax IS NULL OR tb.total_tax = 0)
          AND EXISTS (
              SELECT 1 FROM tax_billing_entity tbe
              WHERE tbe.geo_id = tb.geo_id AND tbe.tax_year = 2025
              GROUP BY tbe.geo_id, tbe.tax_year
              HAVING SUM(tbe.amount_due) > 0
          )
    """,
    "no_usable_total": """
        SELECT COUNT(*) FROM tax_billing tb
        WHERE tb.tax_year = 2025 AND tb.data_source IS NULL
          AND (tb.total_tax IS NULL OR tb.total_tax = 0)
          AND NOT EXISTS (
              SELECT 1 FROM tax_billing_entity tbe
              WHERE tbe.geo_id = tb.geo_id AND tbe.tax_year = 2025
              GROUP BY tbe.geo_id, tbe.tax_year
              HAVING SUM(tbe.amount_due) > 0
          )
    """,
}

UPDATE_VERIFIED_SQL = """
    UPDATE tax_billing
    SET data_source = %s, confidence_level = 'verified'
    WHERE tax_year = 2025 AND data_source IS NULL
      AND total_tax IS NOT NULL AND total_tax != 0
"""

UPDATE_DERIVED_SQL = """
    WITH entity_sums AS (
        SELECT geo_id, tax_year, SUM(amount_due) AS total_due_sum
        FROM tax_billing_entity
        WHERE tax_year = 2025
        GROUP BY geo_id, tax_year
        HAVING SUM(amount_due) > 0
    )
    UPDATE tax_billing tb
    SET total_tax = es.total_due_sum,
        total_paid = es.total_due_sum,
        data_source = %s,
        confidence_level = 'derived'
    FROM entity_sums es
    WHERE tb.geo_id = es.geo_id AND tb.tax_year = es.tax_year
      AND tb.tax_year = 2025 AND tb.data_source IS NULL
      AND (tb.total_tax IS NULL OR tb.total_tax = 0)
"""

UPDATE_NO_USABLE_TOTAL_SQL = """
    UPDATE tax_billing tb
    SET data_source = %s
    WHERE tb.tax_year = 2025 AND tb.data_source IS NULL
      AND (tb.total_tax IS NULL OR tb.total_tax = 0)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Report counts only, write nothing")
    args = parser.parse_args()

    conn = get_conn()
    try:
        ensure_billing_cols(conn)

        with conn.cursor() as cur:
            counts = {}
            for label, sql in COUNT_SQL.items():
                cur.execute(sql)
                counts[label] = cur.fetchone()[0]

        print("  Rows this backfill would affect (data_source IS NULL, tax_year=2025):")
        for label, n in counts.items():
            print(f"    {label}: {n:,}")
        print(f"    TOTAL: {sum(counts.values()):,}")

        if args.dry_run:
            print("\n  --dry-run set -- nothing written.")
            return

        with conn.cursor() as cur:
            cur.execute(UPDATE_VERIFIED_SQL, (DATA_SOURCE,))
            n_verified = cur.rowcount
            cur.execute(UPDATE_DERIVED_SQL, (DATA_SOURCE,))
            n_derived = cur.rowcount
            # Run last: only touches rows the two passes above didn't already claim.
            cur.execute(UPDATE_NO_USABLE_TOTAL_SQL, (DATA_SOURCE,))
            n_no_total = cur.rowcount
        conn.commit()

        print("\n  Applied:")
        print(f"    verified:         {n_verified:,}")
        print(f"    derived:          {n_derived:,}")
        print(f"    no_usable_total:  {n_no_total:,}")

        # Sanity check against the two known parcels used elsewhere this session.
        with conn.cursor() as cur:
            for geo_id in ("0100030105", "0100030109"):
                cur.execute("""
                    SELECT total_tax, data_source, confidence_level
                    FROM tax_billing WHERE geo_id = %s AND tax_year = 2025
                """, (geo_id,))
                row = cur.fetchone()
                print(f"    {geo_id}: {row}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
