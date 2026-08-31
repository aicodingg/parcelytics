"""
Load AJR (Annual Jurisdiction Roll) CSV files for tax years 2021–2024.

AJR format: comma-delimited, no header, one row per parcel × taxing entity.
We read only the aggregate entity rows (field[3] == '227000') to get one row
per unique parcel per year.

Confirmed field positions (0-based):
  [1]  tax_year
  [3]  entity_code      ('227000' = Travis County aggregate)
  [6]  geo_id           TCAD long account (10 chars, e.g. '0100030105')
  [7]  prop_id          TCAD short integer ID
  [9]  situs_address
  [11] legal_desc
  [16] neighborhood_cd
  [24] ptd_state_cd     property class code (A=SFR, F1=commercial, D1=ag …)
  [29] owner_id
  [30] state_cd1
  [31] state_cd2
  [32] market_value     (confirmed for 2022/2024/2025 cross-refs)
  [34] assessed_value   (market minus HS cap; anomalous in some 2021–2023 rows —
                         see NOTE in README; stored as-is for review)
  [35] hs_cap_loss
  [-- 2025 AJR only --]
  [-4] zip_code         (second-to-last meaningful fields added in 2025 format)
  [-3] latitude
  [-2] longitude

NOTE: 2021 file has two copies of data (_PTD.csv and _PTD_AJR_RECORDS.csv).
      We use the _PTD.csv (slightly larger) as the canonical source.

Migration M2 (SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.4, Mechanism C): this
loader used to keep an application-level `seen = set()` and
`if geo_id in seen: continue` — explicit first-wins dedup that silently
discarded every unit past the first sharing a geo_id, even though nothing
in the AJR data or the DB schema required that discard (the ON CONFLICT
clause already handles genuine duplicate rows safely). That dedup is
removed entirely: every accepted row now writes its own prop_unit /
prop_unit_tax_year row, keyed by prop_id, and parcel_tax_year is derived
by parcel_rollup.py's SUM() at the end of load(), same as every other
loader in this migration.

The 2021-format prop_id → geo_id lookup (needed because 2021 AJR rows
carry only prop_id, not geo_id, in field[6]) now reads from `prop_unit`
instead of `parcel`. prop_unit contains every unit ever seen from ANY
loaded year (certified 2025/2026 first, in run_all.py's load order), so
this lookup should resolve strictly more 2021 prop_ids than the old
`parcel`-based lookup did (parcel only ever held ONE winning prop_id per
geo_id) — shrinking the population of synthetic "AJR{prop_id}" geo_ids
this loader falls back to when a lookup genuinely misses.
"""
import csv
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
from loaders import ears_format
from loaders import ingest_gate  # noqa: F401 — AC5 wiring marker; see load_certified_2025.py's
                                  # module note for why this isn't also called inline here
                                  # (AJR CSVs also aren't the fixed-width EARS format G1 scans
                                  # anyway — see run_all.py's gate step for what IS covered)
from loaders.scrape_billing_history import DEFAULT_COUNTY  # DALLAS-GATE-4 / PARCEL-ROLLUP-HOTFIX-1

import psycopg2.extras


AGGREGATE_ENTITY = "227000"

# PX-20260822-06-rev1 (DALLAS-GATE-4 family completion): county_code added
# first in the column list/VALUES/ON CONFLICT target, matching the
# PARCEL-ROLLUP-HOTFIX-1 convention -- live PK for parcel is
# (county_code, geo_id), confirmed via \d against production, 2026-08-23.
# This was the third of load_ajr.py's three breakages named in the
# ADDENDUM: PARCEL_SQL had ZERO county_code awareness (not even the stale
# pre-hotfix 8-column shape ears_format.py's two shared SQL constants had
# before their own fix -- this statement is entirely local to this file).
PARCEL_SQL = """
    INSERT INTO parcel (county_code, geo_id, prop_id, situs_address, legal_desc,
                        neighborhood_cd, state_cd1, state_cd2, owner_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (county_code, geo_id) DO UPDATE
        SET situs_address  = COALESCE(parcel.situs_address,  EXCLUDED.situs_address),
            legal_desc     = COALESCE(parcel.legal_desc,     EXCLUDED.legal_desc),
            neighborhood_cd= COALESCE(parcel.neighborhood_cd,EXCLUDED.neighborhood_cd),
            state_cd1      = COALESCE(parcel.state_cd1,      EXCLUDED.state_cd1),
            state_cd2      = COALESCE(parcel.state_cd2,      EXCLUDED.state_cd2),
            owner_id       = COALESCE(parcel.owner_id,       EXCLUDED.owner_id)
"""
# NOTE: PARCEL_SQL is unchanged by this migration — it still fills IN
# missing identity fields on `parcel` (COALESCE-preserving 2025+ data),
# which is separate from the parcel_tax_year VALUE-column hard rule
# (verify_rollup_canonical.py only forbids writing parcel_tax_year's
# value columns outside parcel_rollup.py; `parcel`'s address/legal/etc
# columns are a different table and not in scope for that rule).


def _int_or_none(v):
    try:
        s = v.strip() if v else ""
        return int(float(s)) if s else None
    except (ValueError, AttributeError):
        return None


def build_pid_lookup(conn, county_code=DEFAULT_COUNTY):
    """Return {prop_id: geo_id} from prop_unit (every unit ever loaded, any year).

    PX-20260830-05 Task 2 (Bucket B), formalizing the KNOWN GAP this
    docstring used to flag-not-fix (deferred by PX-20260822-06-rev1, which
    named exactly three load_ajr.py breakages to fix together and left this
    one disclosed-but-open because the loader was Travis-only at the time):
    prop_unit is composite_pk-migrated (county_code-leading), so an
    unscoped prop_id lookup can resolve to the WRONG county's geo_id once a
    prop_id collides across counties. county_code IS available at both real
    call sites in load() below, so this is now scoped rather than left as a
    standing gap.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT prop_id, geo_id FROM prop_unit WHERE prop_id IS NOT NULL AND county_code = %s",
            (county_code,)
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def load_year(conn, year, filepath, pid_lookup, county_code=DEFAULT_COUNTY):
    t0 = time.time()
    print(f"  Loading {year} AJR: {os.path.basename(filepath)}")

    parcel_rows = []
    unit_rows   = []
    pty_rows    = []
    n_rows      = 0

    with open(filepath, encoding="latin-1", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for lineno, fields in enumerate(reader, 1):
            if len(fields) < 36:
                continue
            # Only aggregate entity rows
            if fields[3].strip() != AGGREGATE_ENTITY:
                continue

            # 2022+: field[6] = geo_id (10-char account), field[7] = prop_id
            # 2021:  field[6] = prop_id, field[7] = prop_id (no geo_id in file)
            f6 = fields[6].strip()
            f7 = _int_or_none(fields[7])

            if len(f6) == 10 and f6.isdigit():
                # 2022+ format: geo_id is directly available
                geo_id  = f6
                prop_id = f7
            else:
                # 2021 format: field[6] is prop_id, look up geo_id
                prop_id = _int_or_none(f6)
                geo_id  = pid_lookup.get(prop_id) or (f"AJR{prop_id}" if prop_id is not None else None)

            if not geo_id or not prop_id:
                continue
            # No dedup — every accepted row (one per prop_id, per AGGREGATE_ENTITY
            # filter above) writes its own prop_unit_tax_year row.

            address      = fields[9].strip()
            legal        = fields[11].strip()
            # 2021: neighborhood at [17]; 2022+: at [16]
            nbhd         = (fields[16].strip() or fields[17].strip()) if len(fields) > 17 else ""
            state_cd1    = fields[30].strip()
            state_cd2    = fields[31].strip()
            owner_id     = _int_or_none(fields[29])
            market_val   = _int_or_none(fields[32])
            assessed_val = _int_or_none(fields[34])
            hs_cap       = _int_or_none(fields[35])

            parcel_rows.append((county_code, geo_id, prop_id, address, legal,
                                nbhd, state_cd1, state_cd2, owner_id))
            # PX-20260822-06-rev1: county_code prepended -- both shared
            # ears_format.py constants gained it as their leading column in
            # PARCEL-ROLLUP-HOTFIX-1, but this file's tuples were never
            # updated to match, an 8-value tuple against PROP_UNIT_UPSERT_SQL's
            # 9 placeholders (a real arity mismatch, not just a missing value).
            unit_rows.append((county_code, prop_id, geo_id, None, address, owner_id, None, year, year))
            # Task M5-PERYEAR-GEOID: geo_id is already resolved above (either
            # directly from this row's own field[6], or via pid_lookup for
            # the 2021 no-geo_id format) -- this IS this year's real,
            # as-of-that-year account assignment, so it's the correct value
            # for prop_unit_tax_year.geo_id too, no separate lookup needed.
            # PX-20260822-06-rev1: county_code prepended, same arity-mismatch
            # fix as unit_rows above (11 values against PROP_UNIT_TAX_YEAR_
            # UPSERT_SQL's 12 placeholders).
            pty_rows.append((county_code, prop_id, year, geo_id, market_val, assessed_val, None,
                              hs_cap, None, None, None, f"ajr_{year}"))
            n_rows += 1

            if lineno % 500_000 == 0:
                print(f"    … {lineno:,} lines scanned, {n_rows:,} units")

    # Bulk insert
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, PARCEL_SQL, parcel_rows, page_size=2000)
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_UPSERT_SQL, unit_rows, page_size=2000)
        psycopg2.extras.execute_batch(cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, pty_rows, page_size=2000)
    conn.commit()

    elapsed = time.time() - t0
    print(f"    → {n_rows:,} units loaded in {elapsed:.1f}s")
    return n_rows


def load(conn, county_code=DEFAULT_COUNTY):
    import parcel_rollup

    print("  Building prop_id → geo_id lookup from prop_unit…")
    pid_lookup = build_pid_lookup(conn, county_code=county_code)
    print(f"  {len(pid_lookup):,} units in lookup")

    total = 0
    years_loaded = []
    for year, filepath in sorted(config.AJR_FILES.items()):
        if not os.path.exists(filepath):
            print(f"  WARNING: {filepath} not found, skipping {year}")
            continue
        total += load_year(conn, year, filepath, pid_lookup, county_code=county_code)
        years_loaded.append(year)
        # Refresh the lookup after each year so a later year's 2021-style
        # fallback (if ever needed) can resolve prop_ids this same run
        # just added — matches the old behavior of re-reading `parcel`
        # fresh each call, now against prop_unit instead.
        pid_lookup = build_pid_lookup(conn, county_code=county_code)

    print(f"  AJR total: {total:,} unit-year rows")

    for year in years_loaded:
        print(f"  Rolling up prop_unit_tax_year → parcel_tax_year for {year}…")
        result = parcel_rollup.run(conn, tax_year=year, county_code=county_code)
        print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
              f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    return total


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--county", default=DEFAULT_COUNTY,
        help=f"county_code written to every parcel/prop_unit/prop_unit_tax_year "
             f"row (default: {DEFAULT_COUNTY}). DALLAS-GATE-4 / "
             f"PARCEL-ROLLUP-HOTFIX-1 convention.",
    )
    args = ap.parse_args()

    conn = get_conn()
    execute_schema(conn)
    load(conn, county_code=args.county)
    conn.close()
