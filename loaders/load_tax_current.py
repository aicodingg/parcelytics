"""
Load TaxCurOpenData (1).csv into tax_billing and tax_billing_entity.

Columns (confirmed from inspection):
  PARCEL          — 14-char tax office account (long account + 0000)
  BILLING         — billing number
  TAXYEAR
  OWNID           — owner ID
  NAMELF          — owner name
  EXEMPTION       — exemption codes (H=homestead, W=OV65, etc.)
  CAUSE           — cause/lawsuit number
  TOTAL_TAX       — total levy
  TOTAL_PI        — penalty & interest
  TOTAL_DUE       — total amount due
  PAYDTP / PAYDTE — payment dates (if paid)
  ENTITY1…ENTITY20, DUE1…DUE20, PAID1…PAID20  — per-entity amounts

The PARCEL field is the 14-digit tax office account. To join to TCAD geo_id:
  geo_id = PARCEL[:10].strip()  (the 14-digit = 10-char geo_id + '0000')
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
# Reused, not reimplemented: scrape_billing_history.py already added
# data_source/confidence_level to tax_billing and knows how to ensure those
# columns exist (same migration load_pir_billing_2021_full.py also reuses).
from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols

import psycopg2.extras

# Confidence tagging (July 2026, per Diego's "fix at the source" brief).
# TaxCurOpenData's own TOTAL_TAX field is genuinely populated for only ~3.5%
# of 2025 rows -- KNOWN_LIMITATIONS.md confirms, by direct inspection of the
# raw CSV, that ~93.3% of rows carry a literal "0.00" in TOTAL_TAX (and
# TOTAL_DUE) in the source file itself, while the same row's ENTITY*/DUE*
# columns carry the real nonzero per-entity amounts. This mirrors, at write
# time, the exact fallback app.py's property_detail() previously performed at
# READ time on every page load (total_tax_derived): if the source total is
# missing/zero but entity data exists, the real total is the entity-DUE sum,
# tagged 'derived' rather than 'verified' so that provenance distinction is
# stored once, here, instead of being silently re-computed by every caller.
DATA_SOURCE = "taxcur_current"

# Hard, unconditional year guard (July 2026, incident response). Confirmed
# live: a real 2024 tax_billing row (0100030804) ended up tagged
# data_source='taxcur_current' after a --new-only run -- root cause is that
# this loader has NEVER filtered on TAXYEAR (it writes whatever value each
# source row's own TAXYEAR field carries), and --new-only's "already tagged"
# protection query only checked tax_year = 2025, so any non-2025 row in the
# source file sailed straight through both the unguarded upsert AND
# --new-only's protection untouched. This is NOT a --new-only-specific bug --
# the plain unconditional-overwrite mode has had the exact same
# TAXYEAR-agnostic behavior since before this session's fix (confirmed by
# diffing against the pre-fix version of this file: the original INSERT/
# upsert never referenced TAXYEAR either). This loader's entire stated
# purpose is CURRENT-YEAR billing -- it must never be able to write any
# other year, in ANY mode, regardless of which flags are passed. Enforced
# once, at the top of the per-row loop, before any other processing.
EXPECTED_TAX_YEAR = 2025


def _f(v):
    """Parse a numeric string, returning None if blank/non-numeric."""
    try:
        return float(v.strip().replace(",", "")) if v and v.strip() else None
    except ValueError:
        return None


def _i(v):
    try:
        return int(v.strip()) if v and v.strip() else None
    except ValueError:
        return None


def load(conn, dry_run=False, new_only=False):
    """
    dry_run=True (July 2026, per Diego's "is a live rerun safe" question):
    parses and classifies every row exactly as a real load would, but never
    calls execute_batch and never commits -- zero writes, zero risk to the
    400K+ rows the backfill already tagged. Requires no DB connection at all
    UNLESS combined with new_only=True (which needs one read-only SELECT --
    see below); the __main__ block below only opens a connection for
    dry_run when new_only is also set.

    new_only=True (July 2026, per Diego's "load just the new/changed rows,
    not an unconditional overwrite" question, after the dry-run surfaced
    ~67K more classifiable rows in the current source file than currently
    exist in tax_billing): before parsing, fetches the full set of
    (geo_id, tax_year) pairs that ALREADY have a non-NULL data_source (i.e.
    already correctly tagged by this fix or the backfill), and skips those
    rows entirely during the CSV pass -- not just guarding the tax_billing
    UPDATE, but skipping tax_billing_entity/owner writes for that key too,
    so an already-tagged row's billing confidence and its entity-level
    detail can never end up out of sync with each other. This is a
    conscious alternative to a bare `ON CONFLICT ... DO UPDATE ... WHERE
    data_source IS NULL` guard (the pattern scrape_billing_history.py
    already uses): a SQL-only guard would still let entity_sql/owner_update_sql
    rewrite an already-tagged row's entity detail even though its billing
    row was skipped, which could leave total_tax describing entity amounts
    that no longer match. Pre-filtering by key avoids that inconsistency
    entirely, at the cost of one upfront SELECT and holding that key set in
    memory (a few hundred thousand small tuples -- negligible).

    default (dry_run=False, new_only=False): the loader's original,
    unconditional-overwrite behavior, UNCHANGED from before this fix --
    every row in the source file is written, ON CONFLICT DO UPDATE with no
    guard. Deliberately left as the default rather than silently making
    new_only the new normal -- that's Diego's call, not this loader's to
    decide unilaterally.
    """
    path = config.TAX_CUR_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found, skipping")
        return 0

    already_tagged_keys = None
    if new_only:
        if conn is None:
            raise ValueError(
                "new_only=True requires a DB connection (to fetch already-tagged "
                "keys) even in dry-run mode -- pass a real conn, or drop new_only."
            )
        print("  --new-only: fetching existing tagged (geo_id, tax_year) keys "
              "(read-only SELECT, no writes)…")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT geo_id, tax_year FROM tax_billing "
                "WHERE tax_year = 2025 AND data_source IS NOT NULL"
            )
            already_tagged_keys = {(r[0], r[1]) for r in cur.fetchall()}
        print(f"    {len(already_tagged_keys):,} already-tagged rows will be skipped entirely")

    print(f"  Loading TaxCurOpenData ({os.path.getsize(path)/1e6:.0f} MB)…")
    t0 = time.time()

    billing_sql = """
        INSERT INTO tax_billing
            (geo_id, tax_year, billing_num, owner_name,
             total_tax, total_paid, total_due,
             is_delinquent, exemption_codes,
             data_source, confidence_level)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year) DO UPDATE
            SET billing_num   = EXCLUDED.billing_num,
                owner_name    = EXCLUDED.owner_name,
                total_tax     = EXCLUDED.total_tax,
                total_paid    = EXCLUDED.total_paid,
                total_due     = EXCLUDED.total_due,
                is_delinquent = EXCLUDED.is_delinquent,
                exemption_codes  = EXCLUDED.exemption_codes,
                data_source      = EXCLUDED.data_source,
                confidence_level = EXCLUDED.confidence_level
    """

    entity_sql = """
        INSERT INTO tax_billing_entity (geo_id, tax_year, entity_code, amount_due, amount_paid)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (geo_id, tax_year, entity_code) DO UPDATE
            SET amount_due  = EXCLUDED.amount_due,
                amount_paid = EXCLUDED.amount_paid
    """

    owner_update_sql = """
        UPDATE parcel SET owner_name = %s, owner_id = %s
        WHERE geo_id = %s AND owner_name IS NULL
    """

    billing_rows = []
    entity_rows  = []
    owner_rows   = []
    total_billing = 0
    # Diagnostic counter (July 2026, per Diego's "missing-row gap" brief):
    # this loader previously had NO visibility into how many source rows it
    # silently skipped for a blank/malformed PARCEL field -- unlike
    # load_pir_billing_2021_full.py's n_bad_accnum, there was nothing here to
    # tell "genuinely not in the source file" apart from "dropped during
    # parsing." Counting and printing it doesn't fix the missing-row gap by
    # itself, but rules this specific cause in or out with a real number
    # instead of a guess.
    n_skipped_no_parcel = 0
    n_skipped_wrong_year = 0
    n_skipped_already_tagged = 0
    # Confidence-bucket counters -- RAW, per source row processed. Can
    # overcount vs. what actually lands in the table if the source file has
    # duplicate (geo_id, tax_year) rows (this loader, unlike
    # load_pir_billing_2021_full.py, does not dedupe -- Postgres's own
    # ON CONFLICT DO UPDATE silently collapses duplicates to whichever
    # occurrence is applied last). See final_by_key below for the
    # DISTINCT/final counts that actually correspond to table rows.
    n_verified = 0
    n_derived = 0
    n_no_usable_total = 0
    # Tracks the FINAL (last-occurrence-wins, matching Postgres's own
    # ON CONFLICT DO UPDATE semantics under this loader's file-order batched
    # execute_batch calls) confidence_level per distinct (geo_id, tax_year)
    # -- lets the report separate "raw rows in the source file" from "rows
    # that would actually exist in tax_billing after a real load," so a
    # duplicate-row-inflated raw count can't be mistaken for genuinely new
    # data (July 2026, per Diego's "confirm the ~67K is really new data"
    # question).
    final_by_key = {}

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # Discover entity columns dynamically (ENTITY1..ENTITY30)
        max_entity = 0
        for h in headers:
            if h.startswith("ENTITY") and h[6:].isdigit():
                max_entity = max(max_entity, int(h[6:]))

        for lineno, row in enumerate(reader, 1):
            # Derive geo_id: first 10 chars of the 14-digit PARCEL field
            raw_parcel = row.get("PARCEL", "").strip().strip('"')
            geo_id = raw_parcel[:10].strip() if raw_parcel else None
            if not geo_id:
                n_skipped_no_parcel += 1
                continue

            tax_year = _i(row.get("TAXYEAR", ""))

            # Hard year guard -- see EXPECTED_TAX_YEAR's module-level comment.
            # Unconditional: runs in every mode (default, --dry-run,
            # --new-only alike), before any other processing, before
            # --new-only's own key check. This loader must never be able to
            # write to any year but 2025, full stop.
            if tax_year != EXPECTED_TAX_YEAR:
                n_skipped_wrong_year += 1
                continue

            if already_tagged_keys is not None and (geo_id, tax_year) in already_tagged_keys:
                n_skipped_already_tagged += 1
                continue

            billing_num = row.get("BILLING", "").strip().strip('"')
            owner_name = row.get("NAMELF", "").strip().strip('"') or None
            owner_id   = _i(row.get("OWNID", ""))
            exemptions = row.get("EXEMPTION", "").strip().strip('"') or None
            cause      = row.get("CAUSE", "").strip() or None
            total_tax  = _f(row.get("TOTAL_TAX", ""))
            total_pi   = _f(row.get("TOTAL_PI", ""))
            total_due  = _f(row.get("TOTAL_DUE", ""))

            # Is delinquent if total_due > 0 and not fully paid
            is_delinquent = bool(total_due and total_due > 0.01)

            # Parse entity columns first -- needed below to compute a
            # fallback total when the source TOTAL_TAX is the literal "0.00"
            # quirk (see DATA_SOURCE comment above). Same field (DUE{i}) and
            # same "skip falsy" semantics app.py's read-time fallback used.
            row_entities = []
            for i in range(1, max_entity + 1):
                ent_code = row.get(f"ENTITY{i}", "").strip().strip('"')
                due      = _f(row.get(f"DUE{i}", ""))
                paid     = _f(row.get(f"PAID{i}", ""))
                if ent_code and (due or paid):
                    row_entities.append((ent_code, due, paid))
                    entity_rows.append((geo_id, tax_year, ent_code, due, paid))

            # Confidence tagging + total_tax correction, computed once here
            # instead of re-derived by every reader (app.py's property_detail(),
            # export_due_diligence_pdf(), etc.):
            #   'verified' -- source TOTAL_TAX is genuinely populated (nonzero)
            #   'derived'  -- source TOTAL_TAX is missing/0.00; real total is
            #                 the entity-DUE sum instead
            #   NULL       -- neither a real TOTAL_TAX nor any entity DUE data
            #                 exists for this row; no usable total at all
            entity_sum = sum(d for _, d, _ in row_entities if d) or None
            if total_tax:
                confidence_level = "verified"
                n_verified += 1
            elif entity_sum:
                total_tax = entity_sum
                confidence_level = "derived"
                n_derived += 1
            else:
                confidence_level = None
                n_no_usable_total += 1

            # Last-occurrence-in-file-order wins, same as Postgres's own
            # ON CONFLICT DO UPDATE across this loader's sequential,
            # file-order batches -- see final_by_key's declaration above.
            final_by_key[(geo_id, tax_year)] = confidence_level

            billing_rows.append((
                geo_id, tax_year, billing_num, owner_name,
                total_tax, total_tax,  # total_paid ≈ total_tax if not delinquent
                total_due, is_delinquent, exemptions,
                DATA_SOURCE, confidence_level
            ))

            # Backfill owner name on parcel table
            if owner_name:
                owner_rows.append((owner_name, owner_id, geo_id))

            if not dry_run and len(billing_rows) >= 3000:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
                    if entity_rows:
                        psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
                    if owner_rows:
                        psycopg2.extras.execute_batch(cur, owner_update_sql, owner_rows, page_size=2000)
                conn.commit()
                total_billing += len(billing_rows)
                billing_rows = []
                entity_rows  = []
                owner_rows   = []
            elif dry_run and len(billing_rows) >= 3000:
                # Same batching cadence as a real run (so memory use is
                # comparable and progress prints land at the same points),
                # just discarded instead of written.
                total_billing += len(billing_rows)
                billing_rows = []
                entity_rows  = []
                owner_rows   = []

            if lineno % 100_000 == 0:
                print(f"    … {lineno:,} rows, {total_billing:,} {'parsed' if dry_run else 'committed'}")

    # Final flush
    if billing_rows:
        if not dry_run:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, billing_sql, billing_rows, page_size=2000)
                if entity_rows:
                    psycopg2.extras.execute_batch(cur, entity_sql, entity_rows, page_size=2000)
                if owner_rows:
                    psycopg2.extras.execute_batch(cur, owner_update_sql, owner_rows, page_size=2000)
            conn.commit()
        total_billing += len(billing_rows)

    elapsed = time.time() - t0
    mode = "DRY RUN -- nothing written" if dry_run else "written"
    n_distinct_keys = len(final_by_key)
    n_duplicate_key_rows = total_billing - n_distinct_keys
    final_verified = sum(1 for v in final_by_key.values() if v == "verified")
    final_derived  = sum(1 for v in final_by_key.values() if v == "derived")
    final_none     = sum(1 for v in final_by_key.values() if v is None)

    print(f"    → {total_billing:,} raw source rows processed [{mode}] in {elapsed:.1f}s")
    print(f"    {n_skipped_no_parcel:,} source rows skipped (blank/malformed PARCEL field)")
    print(f"    {n_skipped_wrong_year:,} source rows skipped (TAXYEAR != {EXPECTED_TAX_YEAR} -- "
          f"this loader only ever writes {EXPECTED_TAX_YEAR})")
    if already_tagged_keys is not None:
        print(f"    {n_skipped_already_tagged:,} source rows skipped (--new-only: key already tagged)")
    print(f"    RAW confidence tally (per source row -- inflated by any duplicate "
          f"(geo_id, tax_year) rows in the source file):")
    print(f"      verified={n_verified:,}  derived={n_derived:,}  no_usable_total={n_no_usable_total:,}")
    print(f"    DISTINCT (geo_id, tax_year) keys -- what would ACTUALLY exist in "
          f"tax_billing after a real load, this is the number to compare against "
          f"the live table's row count:")
    print(f"      {n_distinct_keys:,} distinct keys "
          f"({n_duplicate_key_rows:,} raw rows were duplicates of an already-seen key)")
    print(f"      verified={final_verified:,}  derived={final_derived:,}  no_usable_total={final_none:,}")
    return total_billing


def load_delinquent(conn):
    """Load TaxDelqOpenData.csv into tax_delinquent."""
    path = config.TAX_DELQ_CSV
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found, skipping delinquent load")
        return 0

    print(f"  Loading TaxDelqOpenData ({os.path.getsize(path)/1e6:.1f} MB)…")

    sql = """
        INSERT INTO tax_delinquent
            (geo_id, tax_year, delinquent_total, current_year_total, total_due,
             first_delinquent_yr, cause_number)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (geo_id) DO UPDATE
            SET delinquent_total    = EXCLUDED.delinquent_total,
                current_year_total  = EXCLUDED.current_year_total,
                total_due           = EXCLUDED.total_due,
                first_delinquent_yr = EXCLUDED.first_delinquent_yr,
                cause_number        = EXCLUDED.cause_number
    """

    rows = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row.get("Account #", "").strip()
            geo_id = raw[:10].strip() if raw else None
            if not geo_id:
                continue
            rows.append((
                geo_id,
                _i(row.get("Last Tax Roll Year", "")),
                _f(row.get("Delinquent Total", "")),
                _f(row.get("Current Year Total", "")),
                _f(row.get("Total Due", "")),
                _i(row.get("1st Year Delinquent", "")),
                row.get("Cause #", "").strip() or None,
            ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=2000)
    conn.commit()
    print(f"    → {len(rows):,} delinquent rows")
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse + classify only, write nothing")
    parser.add_argument("--new-only", action="store_true",
                         help="Skip any (geo_id, tax_year) already tagged with a data_source "
                              "-- only add genuinely new rows, never touch already-tagged ones. "
                              "Combine with --dry-run to preview what would be added first.")
    args = parser.parse_args()

    if args.dry_run and args.new_only:
        print("  *** --dry-run --new-only: opening a READ-ONLY connection (one SELECT, to "
              "fetch already-tagged keys) -- no writes, no commit, no schema/column migration ***")
        conn = get_conn()
        load(conn, dry_run=True, new_only=True)
        conn.close()
    elif args.dry_run:
        print("  *** --dry-run: no DB connection will be opened, nothing will be written ***")
        load(conn=None, dry_run=True)
    else:
        conn = get_conn()
        execute_schema(conn)
        ensure_billing_cols(conn)  # adds data_source/confidence_level if not already present
        load(conn, new_only=args.new_only)
        load_delinquent(conn)
        conn.close()
