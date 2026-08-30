"""
loaders/load_dallas_tax_rates.py -- Dallas County Tax Office rate loader
(PX-20260829-07 Task 5).

Usage:
    cd ~/Parcelytics/code
    python3 loaders/load_dallas_tax_rates.py --dry-run
    python3 loaders/load_dallas_tax_rates.py            # live load

Reads the two real Dallas rate pages registered in config.py
(DALLAS_RATES_HTML_CURRENT, DALLAS_RATES_HTML_HISTORY -- see that file's
own comment for the exact source URLs). This session cannot physically
place either file at those paths (no vault/filesystem access beyond this
repo) -- Diego must save each page's real HTML there (View Source or
Save Page As, "Webpage, HTML only") before this loader can run for real,
live OR --dry-run. Fails loud with that exact instruction if either file
is missing, rather than a generic FileNotFoundError.

Parsing/identity logic lives in loaders/dallas_rates_format.py (approved
PX-20260829-07 Task 3 design) -- this file is orchestration + DB I/O only,
mirroring load_dallas_certified.py's own division of labor (format module
pure/testable, loader module handles CLI/DB/printing).

County scoping (Task 5's explicit requirement): every row this loader
produces carries county_code='DALLAS' (COUNTY_CODE below, not a CLI flag --
unlike load_tax_rates.py's --county, which defaults to Travis, this
loader has exactly one county it's for, so there's no accidental-Travis-
write surface to close). The upsert's ON CONFLICT target is
(county_code, entity_code, tax_year) -- Travis's own rows all carry
county_code='TRAVIS' from load_tax_rates.py, so a Dallas-only run can
never touch, overwrite, or ON-CONFLICT-collide with a Travis row, at the
SQL level, independent of any application-level care. See
test_load_dallas_tax_rates.py's test_upsert_sql_is_county_scoped() for a
fixture proof of this (string/regex assertion against the shipping SQL,
same convention as test_dallas_gate_4_county_code.py).

Skip-bucket ledger (Task 3's approved design, mirroring dcad_format.py /
ingest_gate.py's g1_conservation_check() convention): every parsed row
carries a skip_reason (None = accepted). This loader tallies buckets by
name and prints the ledger before any write -- a row is never silently
dropped; an unparseable/unpublished row is a counted, named skip.
"""
import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # noqa: E402
from loaders import dallas_rates_format as fmt  # noqa: E402

COUNTY_CODE = "DALLAS"
DATA_SOURCE = "dallas_tax_rates"

# Task 5: county_code is the FIRST column and the FIRST element of the
# ON CONFLICT target -- matching load_tax_rates.py's own PARCEL-ROLLUP-
# HOTFIX-1 convention for this exact table, extended here with the new
# mo_rate/is_rate columns (Task 2's approved schema.sql addition). Every
# VALUES tuple this loader builds has county_code='DALLAS' as its first
# element (see build_rows() below) -- there is no code path that can
# produce a row with any other county_code.
UPSERT_SQL = """
    INSERT INTO county_tax_rate (county_code, entity_code, entity_name, tax_year, rate, mo_rate, is_rate)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (county_code, entity_code, tax_year) DO UPDATE
        SET entity_name = EXCLUDED.entity_name,
            rate        = EXCLUDED.rate,
            mo_rate     = EXCLUDED.mo_rate,
            is_rate     = EXCLUDED.is_rate
"""


def _require_html(path, label):
    if not os.path.isfile(path):
        print(
            f"ERROR: {label} not found at {path}\n"
            f"  This sandbox cannot fetch or save Dallas's live rate pages -- "
            f"Diego needs to save the real page there first (browser 'Save Page "
            f"As... Webpage, HTML only', or View Source, saved to that exact "
            f"path). See config.py's own comment above "
            f"DALLAS_RATES_HTML_CURRENT/DALLAS_RATES_HTML_HISTORY for the real "
            f"source URLs (dallascounty.org/departments/tax/tax-rates.php and "
            f".../past-tax-rates.php)."
        )
        sys.exit(1)


def _load_soup(path):
    from bs4 import BeautifulSoup

    with open(path, encoding="utf-8", errors="replace") as f:
        return BeautifulSoup(f.read(), "html.parser")


def parse_history_file(path):
    """past-tax-rates.php -- the multi-year Bootstrap-accordion page.
    Returns a flat list of per-entity-year dicts (including skipped ones,
    each still carrying its own skip_reason -- filtering happens in
    build_rows() below, not here, so the full ledger is always visible)
    via dallas_rates_format.find_year_tables()/parse_year_table()."""
    soup = _load_soup(path)
    rows = []
    for tax_year, table_soup in fmt.find_year_tables(soup):
        rows.extend(fmt.parse_year_table(table_soup, tax_year))
    return rows


def parse_current_file(path):
    """tax-rates.php -- the single-year, NON-accordion page (a second real
    structural difference from history, found the same way: Diego's own
    --dry-run against the real saved file -- see
    dallas_rates_format.find_current_year_table()'s own CORRECTION
    docstring). Uses find_current_year_table() (h2-heading-derived year,
    no toggle/label near the table at all) instead of find_year_tables()
    -- these two source pages do NOT share a parsing path, deliberately,
    rather than forcing one function to handle two different real
    structures."""
    soup = _load_soup(path)
    tax_year, table_soup = fmt.find_current_year_table(soup)
    return fmt.parse_year_table(table_soup, tax_year)


def build_rows(all_parsed_rows):
    """Splits parsed rows into (accepted, skip_counter). accepted is a
    list of (county_code, entity_code, entity_name, tax_year, rate,
    mo_rate, is_rate) tuples ready for UPSERT_SQL. skip_counter tallies
    every non-None skip_reason by name -- printed as the loud ledger
    before any write, per Task 3's approved design. No row is ever
    dropped without being counted here."""
    accepted = []
    skip_counter = Counter()
    skipped_names_by_reason = {}

    for row in all_parsed_rows:
        if row["skip_reason"] is not None:
            skip_counter[row["skip_reason"]] += 1
            skipped_names_by_reason.setdefault(row["skip_reason"], []).append(
                f"{row['entity_name']} ({row['tax_year']})"
            )
            continue
        accepted.append((
            COUNTY_CODE,
            row["entity_code"],
            row["entity_name"],
            row["tax_year"],
            row["rate"],
            row["mo_rate"],
            row["is_rate"],
        ))

    return accepted, skip_counter, skipped_names_by_reason


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                     help="Parse + build rows + print the skip ledger; ZERO DB "
                          "writes, ZERO DB connection opened -- matches "
                          "load_dallas_certified.py's own --dry-run convention.")
    args = ap.parse_args()

    print(f"\n{'-'*65}")
    print(f"  Loading Dallas County Tax Office rates -- county={COUNTY_CODE}")
    print(f"  Current-year source : {config.DALLAS_RATES_HTML_CURRENT}")
    print(f"  History source      : {config.DALLAS_RATES_HTML_HISTORY}")
    print(f"{'-'*65}\n")

    _require_html(config.DALLAS_RATES_HTML_HISTORY, "DALLAS_RATES_HTML_HISTORY")
    _require_html(config.DALLAS_RATES_HTML_CURRENT, "DALLAS_RATES_HTML_CURRENT")

    t0 = time.time()

    print("Parsing past-tax-rates.php (accordion, 2015-2024, per live verification)...")
    history_rows = parse_history_file(config.DALLAS_RATES_HTML_HISTORY)
    years_seen = sorted(set(r["tax_year"] for r in history_rows))
    print(f"  {len(history_rows):,} raw rows across years: {years_seen}")

    print("Parsing tax-rates.php (current year, NON-accordion, h2-derived year)...")
    current_rows = parse_current_file(config.DALLAS_RATES_HTML_CURRENT)
    print(f"  {len(current_rows):,} raw rows, year(s): "
          f"{sorted(set(r['tax_year'] for r in current_rows))}")

    all_rows = history_rows + current_rows

    # Duplicate-year guard: if the current-year page's year is ALSO present
    # in the history page (e.g. Dallas eventually folds it into the
    # archive), the current-year page's row should win (it is the more
    # authoritative, single-year-focused source) -- but this must be a
    # visible decision, not a silent last-write-wins accident of dict
    # ordering. Detected and reported here; UPSERT_SQL's own ON CONFLICT
    # DO UPDATE makes the actual DB write idempotent either way, so this
    # is a reporting/awareness check, not a correctness-blocking one.
    history_years = set(r["tax_year"] for r in history_rows)
    current_years = set(r["tax_year"] for r in current_rows)
    overlap = history_years & current_years
    if overlap:
        print(f"  NOTE: year(s) {sorted(overlap)} appear on BOTH pages -- "
              f"current-year rows are appended after history rows, so "
              f"batch_upsert's last-write-wins ordering favors the "
              f"current-year page's values for those years.")

    # Loud, pre-write collision guard (Task 3's own design, wired in after
    # test_dallas_rates_format.py caught a REAL truncation collision during
    # this brief -- see dallas_rates_format.dallas_entity_code()'s own
    # docstring). Runs BEFORE build_rows()/any write, in both --dry-run and
    # live paths, over ALL parsed rows (accepted and skipped alike, since a
    # collision is a name-resolution problem independent of whether a given
    # row's rate happened to be published that year).
    fmt.check_entity_code_collisions(all_rows)

    accepted, skip_counter, skipped_names = build_rows(all_rows)

    print(f"\nSkip ledger (Task 3 loud-skip convention -- no row silently dropped):")
    if skip_counter:
        for reason, count in skip_counter.most_common():
            names = skipped_names[reason]
            preview = names[:10]
            more = f" ...+{len(names)-10} more" if len(names) > 10 else ""
            print(f"  {reason}: {count:,}  e.g. {preview}{more}")
    else:
        print("  (none -- every parsed row had a usable rate)")

    entity_codes = set(r[1] for r in accepted)
    print(f"\n  Accepted: {len(accepted):,} rows / {len(entity_codes):,} distinct "
          f"entity_code values / years {sorted(set(r[3] for r in accepted))}")

    # Alias-crosswalk visibility: report how many distinct RAW names
    # collapsed into fewer entity_codes, so a reviewer can sanity-check
    # the crosswalk actually did something (rather than everything
    # silently getting its own code because canonicalize_name() alone
    # didn't close a real drift case still sitting in the raw names).
    raw_names = set(r["entity_name"] for r in all_rows if r["skip_reason"] is None)
    print(f"  ({len(raw_names):,} distinct raw entity_name strings collapsed to "
          f"{len(entity_codes):,} entity_code values via canonicalize_name() + "
          f"DALLAS_ENTITY_ALIASES)")

    if args.dry_run:
        print(f"\n  *** --dry-run: no DB connection opened, zero writes ***")
        print(f"  Total elapsed: {time.time()-t0:.1f}s")
        print(f"\nDone (dry-run). {len(accepted):,} rows would be upserted into "
              f"county_tax_rate for county={COUNTY_CODE}. Travis's rows "
              f"(county_code='TRAVIS') are never read or touched by this "
              f"loader -- see UPSERT_SQL's own ON CONFLICT target.")
        return

    from loaders.db import get_conn, execute_schema, batch_upsert

    conn = get_conn()
    execute_schema(conn)
    n = batch_upsert(conn, UPSERT_SQL, accepted)
    print(f"\n  county_tax_rate: {n:,} rows upserted (county={COUNTY_CODE})")
    print(f"  Total elapsed: {time.time()-t0:.1f}s")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
