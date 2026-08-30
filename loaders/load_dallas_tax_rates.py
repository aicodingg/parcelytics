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

# Real live finding (Diego, post-launch, deployed Dallas rates page): a
# DALLAS_ENTITY_ALIASES fix that reassigns an existing entity's
# entity_code (e.g. "Carrollton-Farmers Br ISD" merging into "...Branch
# ISD"'s code) does NOT clean up the rows already written under the OLD,
# now-orphaned code -- UPSERT_SQL's ON CONFLICT only ever touches rows
# matching the row's OWN (county_code, entity_code, tax_year), so the old
# code's rows just sit there forever, duplicating that district's history
# under two codes. This DELETE is deliberately scoped to county_code=
# 'DALLAS' only (never touches Travis's own county_code='TRAVIS' rows in
# the same table) and is safe to run before every full reload BECAUSE
# this table is single-source for Dallas -- every row this loader would
# ever write is reconstructed fresh from parsing the two real HTML files,
# nothing here is additive-only data a plain upsert can't fully rebuild.
# Wired into main() as an explicit --full-reload flag (not the default --
# a scoped-but-still-real DELETE shouldn't run silently on every
# incremental load) and executed in the SAME transaction as the
# subsequent upsert (see main()) so a failure before that transaction
# commits rolls the delete back too, never leaving the table empty.
FULL_RELOAD_DELETE_SQL = "DELETE FROM county_tax_rate WHERE county_code = %s"


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
                          "load_dallas_certified.py's own --dry-run convention. "
                          "Ignores --full-reload if both are given.")
    ap.add_argument("--full-reload", action="store_true",
                     help="DELETE FROM county_tax_rate WHERE county_code='DALLAS' "
                          "before reinserting, in the SAME transaction as the "
                          "upsert (a failure before that transaction commits "
                          "rolls the delete back too -- never leaves the table "
                          "empty). Safe because this table is single-source for "
                          "Dallas: every row this loader writes is rebuilt fresh "
                          "from the two real HTML files every run. NEEDED after "
                          "any DALLAS_ENTITY_ALIASES change that reassigns an "
                          "existing entity's entity_code -- otherwise the OLD "
                          "code's rows are orphaned forever, since a plain upsert "
                          "only ever touches rows matching each row's OWN code. "
                          "Not the default -- a real DELETE, even this tightly "
                          "scoped, shouldn't run silently on every incremental "
                          "load.")
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
    #
    # PX-20260830-01 Task 6 (real off-by-one, found by Diego: "139 distinct
    # raw names this run" on the audit line below vs "138 ... collapsed to"
    # here -- confirmed NOT a bug, a genuine counting-SCOPE difference that
    # was never labeled as such, so it read as a disagreement):
    #   - accepted_raw_names (this line) counts distinct raw entity_name
    #     strings among ACCEPTED rows only (skip_reason is None) -- this is
    #     "how many raw spellings did the crosswalk actually have to
    #     collapse to produce entity_codes," so a SKIPPED row's name
    #     shouldn't count here at all: it never contributed a code.
    #   - all_raw_names (the audit block below) deliberately counts EVERY
    #     parsed row's name, skipped included -- find_near_duplicate_names()
    #     needs the skipped names too (a same-district split doesn't care
    #     whether that year's rate happened to be published).
    #   These are two DIFFERENT, correctly-scoped counts of two different
    #   things, not two attempts at the same count -- both are now labeled
    #   explicitly below, and any name(s) present in one scope but not the
    #   other are named directly so this can never again look like an
    #   unexplained disagreement.
    accepted_raw_names = set(r["entity_name"] for r in all_rows if r["skip_reason"] is None)
    print(f"  ({len(accepted_raw_names):,} distinct raw entity_name strings among ACCEPTED "
          f"rows collapsed to {len(entity_codes):,} entity_code values via "
          f"canonicalize_name() + DALLAS_ENTITY_ALIASES)")

    # Near-duplicate name AUDIT (added in direct response to a real live
    # finding -- "Carrollton-Farmers Br ISD" / "Carrollton-Farmers Branch
    # ISD" rendering as two entities on the deployed page; see
    # dallas_rates_format.find_near_duplicate_names()'s own docstring for
    # the full design rationale). Runs over EVERY raw name seen this run
    # (skipped rows included -- a same-district name split doesn't care
    # whether that particular year's rate was published), sorted for a
    # human to scan. This is a candidate list, not a guard: it never
    # blocks a load and never edits DALLAS_ENTITY_ALIASES itself -- a
    # human confirms each real match, then adds the alias by hand (see
    # dallas_entity_code()'s own docstring on the real "Levee District 14"
    # vs "Levee District 4" pair -- similarly spelled, genuinely DIFFERENT
    # entities, correctly a candidate here but correctly NOT an alias).
    all_raw_names = [r["entity_name"] for r in all_rows]
    all_distinct_raw_names = set(all_raw_names)
    near_dupes = fmt.find_near_duplicate_names(all_raw_names)
    print(f"\nNear-duplicate name audit ({len(all_distinct_raw_names):,} distinct raw names "
          f"this run INCLUDING SKIPPED ROWS, sorted, similarity >= 0.90):")
    if near_dupes:
        for name_a, name_b, ratio in near_dupes:
            print(f"  [{ratio:.3f}] {name_a!r}  <->  {name_b!r}")
        print(f"  {len(near_dupes)} candidate pair(s) above -- review each by hand; "
              f"add a DALLAS_ENTITY_ALIASES entry ONLY for ones confirmed to be the "
              f"same real district (a high similarity score alone does not mean "
              f"they are -- see the module's own docstring).")
    else:
        print("  (none at this threshold)")

    # Task 6's own reconciliation line: name the exact names, if any, that
    # exist in one scope but not the other, so the two counts above are
    # never again read as an unexplained disagreement.
    skip_only_names = all_distinct_raw_names - accepted_raw_names
    if skip_only_names:
        print(f"  (note: the two counts above differ by {len(skip_only_names)} -- "
              f"{sorted(skip_only_names)} appear ONLY in a skipped row this run, "
              f"never in an accepted one, so they count in the near-duplicate "
              f"audit's broader scope above but not in the accepted-rows count "
              f"further up. This is expected, not a bug.)")

    if args.dry_run:
        print(f"\n  *** --dry-run: no DB connection opened, zero writes ***")
        print(f"  Total elapsed: {time.time()-t0:.1f}s")
        print(f"\nDone (dry-run). {len(accepted):,} rows would be upserted into "
              f"county_tax_rate for county={COUNTY_CODE}. Travis's rows "
              f"(county_code='TRAVIS') are never read or touched by this "
              f"loader -- see UPSERT_SQL's own ON CONFLICT target.")
        return

    from loaders.db import get_conn, execute_schema, batch_upsert, column_exists, assert_production_db, WrongDatabaseError

    conn = get_conn()

    # Standing project rule (Diego, PX-20260830-01 Task 4): verify this is
    # really the one production database BEFORE any write -- including
    # execute_schema()'s own CREATE TABLE IF NOT EXISTS below, which is
    # itself a write. See loaders.db.assert_production_db()'s own
    # docstring for the real footgun this closes (DATABASE_URL silently
    # pointed at the wrong environment) and why this is a hard fail rather
    # than the print-and-hope-a-human-reads-it pattern this project's
    # other scripts already use. --dry-run never reaches this line at all
    # (it returns above, before get_conn() is even called) -- unaffected.
    try:
        addr = assert_production_db(conn)
    except WrongDatabaseError as e:
        print(f"\nERROR: {e}")
        conn.close()
        sys.exit(1)
    print(f"  [db] production address confirmed: {addr}")

    execute_schema(conn)

    # Real bug this closes (Diego, live finding): execute_schema() above
    # just printed "Schema applied." unconditionally, but schema.sql's own
    # CREATE TABLE IF NOT EXISTS is a documented no-op against an
    # already-existing production county_tax_rate table missing mo_rate/
    # is_rate -- those two columns need a separately-run, one-time ALTER
    # (see schema.sql's own comment above the county_tax_rate definition).
    # Verify they actually exist NOW, right after the misleading success
    # message, and fail loud with the exact ALTER TABLE needed rather than
    # let batch_upsert() crash mid-write with a confusing "column mo_rate
    # of relation county_tax_rate does not exist".
    missing_cols = [c for c in ("mo_rate", "is_rate")
                    if not column_exists(conn, "county_tax_rate", c)]
    if missing_cols:
        alters = "\n".join(
            f"  ALTER TABLE county_tax_rate ADD COLUMN IF NOT EXISTS {c} NUMERIC(8,6);"
            for c in missing_cols
        )
        print(
            f"\nERROR: 'Schema applied.' above printed success, but "
            f"county_tax_rate is still missing: {', '.join(missing_cols)}. "
            f"execute_schema()'s CREATE TABLE IF NOT EXISTS is a no-op against "
            f"this already-existing table -- it never adds a column introduced "
            f"after the table was first created. Run this once against THIS "
            f"SAME database, then retry:\n{alters}"
        )
        conn.close()
        sys.exit(1)

    if args.full_reload:
        print(f"\n  *** --full-reload: DELETE FROM county_tax_rate WHERE "
              f"county_code='{COUNTY_CODE}' before reinserting ***")
        with conn.cursor() as cur:
            cur.execute(FULL_RELOAD_DELETE_SQL, (COUNTY_CODE,))
            deleted = cur.rowcount
        print(f"  Deleted {deleted:,} existing {COUNTY_CODE} row(s) -- uncommitted, "
              f"part of the same transaction as the upsert below, so a failure "
              f"before that commits rolls this delete back too (Travis's own "
              f"county_code='TRAVIS' rows in this table are untouched -- this "
              f"DELETE is scoped to county_code='{COUNTY_CODE}' only).")

    n = batch_upsert(conn, UPSERT_SQL, accepted)
    print(f"\n  county_tax_rate: {n:,} rows upserted (county={COUNTY_CODE})")
    print(f"  Total elapsed: {time.time()-t0:.1f}s")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
