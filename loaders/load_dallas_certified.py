"""
loaders/load_dallas_certified.py — DCAD Certified Roll loader
(PX-20260826-03 design, PX-20260826-03-rev1 revision, PX-20260826-04
"the bodies" -- full implementation).

Usage:
    cd ~/Parcelytics/code
    python3 loaders/load_dallas_certified.py --year 2026 --dry-run
    python3 loaders/load_dallas_certified.py --year 2026            # live load (see scope note below)

Orchestration shape mirrors load_certified_historical.py (the closest
existing precedent: a --county-flagged, --dry-run-capable, single-source
historical-year loader) and load_ajr.py's own upsert-and-gate pattern.

══════════════════════════════════════════════════════════════════════════
UPDATE (Aug 2026, URGENT dcad_certified live-correctness fix): the SCOPE
NOTE below describing this file as never executed against production is
STALE. Diego confirmed Dallas went live that night -- all 5 years
(2022-2026) loaded and verified via live psql row counts (703,446 units
for 2025 alone). A database load leaves no commit in this repo, which is
why every static signal available at the time (this docstring, git log,
the runbook's own prospective framing, app.py's county-registry comment)
still read as "not yet loaded" after the fact -- that gap is exactly what
let CERTIFIED_TIER_DATA_SOURCES ship without "dcad_certified" in it and
stay unnoticed until Diego caught it live. Do not infer production load
status from this repo's static state alone going forward -- confirm with
Diego or a live query.

PX-20260826-04 SCOPE NOTE (historical, describes this file's state as of
that brief -- superseded by the update above): this file is fully
implemented -- real CSV parsing, real prop_id/geo_id/value derivation,
real prop_unit/prop_unit_tax_year writes, real parcel_rollup call, real
ingestion gate -- but had NOT been executed against any database in any
session as of that brief. --dry-run mode (full parse + counts + gate scan,
zero DB connection) IS exercised and green (see test_dcad_format.py and
that brief's own report). A live, --dry-run-free run against production
was explicitly OUT of that brief's scope ("the deliverable ends at a
working --dry-run and green fixtures; the runbook is the next brief") --
that runbook (pre-flight checks, rollback plan, canary-slice-first
sequencing, same shape as PX-20260824-05's Travis-side precedent) was the
next brief at the time, not that one.

REV1 mapping this file implements (see PX-20260826-04 FINDING #2 note
below for the prop_id rule's real, corrected shape):
    prop_unit.geo_id  (VARCHAR(20), fits 17-char ACCOUNT_NUM, ZERO schema
                        change) <- ACCOUNT_INFO.ACCOUNT_NUM, verbatim (text).
    prop_unit.prop_id (BIGINT PRIMARY KEY, an internal surrogate) <-
                        two-tier: int(ACCOUNT_NUM) for all-digit accounts,
                        a disjoint truncated-SHA-256 derivation for
                        alphanumeric ones. See
                        dcad_format.derive_prop_id_geo_id().

PX-20260826-04 FINDING #2 (real dry-run, PM re-ruling) — the rev1 "all-
digit or fail loud" prop_id rule above was WRONG: 205,049 of 806,563 real
ACCOUNT_NUMs are alphanumeric (letters are structural block/unit
designators), including 190,375 loadable RES/COM accounts — failing loud
on all of them would have silently discarded 23.6% of the real, loadable
population. prop_id is now two-tier (see dcad_format.derive_prop_id_geo_id()
and dcad_format._hashed_prop_id() for the full derivation and bit-layout
reasoning); geo_id is unchanged. Two required guards accompany this
change, since prop_id is no longer a provably-1:1 function of a validated
numeric string:
    1. In-run duplicate-prop_id detection across a single build --
       dcad_format.check_in_run_prop_id_collisions(), called at the end
       of build_unit_rows() below.
    2. A persistent, write-time guard rejecting a write whose prop_id
       already exists in prop_unit under a DIFFERENT geo_id --
       _fetch_existing_prop_id_geo_id() + dcad_format.
       find_prop_id_geo_id_conflicts(), called from
       write_prop_unit_and_tax_year() below, live-write path only (this
       guard needs a DB read; --dry-run has no DB connection to check
       against, and is disclosed as such at its own call site).

PRE-COMMIT FIX (real, disclosed correction): tax_year no longer falls back
to --year on a missing/wrong value. The field this design originally
guessed was named "TAX_YR" does not exist on the real CSV -- it was
UNCONFIRMED and always came back None, which is why 100% of rows were
silently taking the --year fallback path. The real column is
APPRAISAL_YR (now CONFIRMED, hard-validated via EXPECTED_HEADERS). Every
account's APPRAISAL_YR is now asserted, per row, to equal the run's own
--year -- fail-loud (dcad_format.AppraisalYearMismatchError) on any
mismatch, including an account with no ACCOUNT_APPRL_YEAR match at all
(treated identically to a real value mismatch, not a special soft case).
The --year fallback is DELETED, not just bypassed. See
dcad_format.validate_appraisal_year() and build_unit_rows()'s own call
site below.

TAXABLE_OBJECT is CONFIRMED to be a building-component link table, not a
finer unit grain -- DCAD's own unit grain is the ACCOUNT (unit_count=1
everywhere, no collision mechanism). It plays no role below; see
dcad_format.TABLE_LOAD_POLICY["TAXABLE_OBJECT"].

PX-20260826-04 value-mapping (PM-verified against real 2026 rows, baked
in via dcad_format.derive_value_mapping() -- see that function for the
full rulings): market_value=TOT_VAL, land_value=LAND_VAL,
imprv_value=IMPR_VAL, assessed_value=HMSTD_CAP_VAL if >0 else TOT_VAL,
hs_cap_loss=TOT_VAL-HMSTD_CAP_VAL where a cap is present else NULL,
taxable_value=COUNTY_TAXABLE_VAL.

Classification: ACCOUNT_APPRL_YEAR.SPTD_CODE (CONFIRMED, column 47) is
carried through to prop_unit.prop_type_cd AS THE RAW CODE (mirroring
Travis's own convention of storing the raw source code on prop_unit,
e.g. PROP_TYPE_CD from PROP.TXT -- the benchmark LABEL is a query/display-
time concern layered on top via classify_dallas_sptb_code(), not a stored
column, same division of labor Travis's own tax_logic/classify.py already
uses against its own stored raw codes). This run's own classification
distribution (via dcad_format.classify_account_sptd()) is also computed
and printed for verification -- see this brief's own report for the one
open SPTB/SPTD field-naming reconciliation item.

county_code = 'DALLAS' on every row, all writes into the EXISTING,
already-migrated prop_unit / prop_unit_tax_year / parcel / parcel_tax_year
tables (per MC-1 rule 4 -- these tables are already county_code-led in
production; PROP_UNIT_UPSERT_SQL/PROP_UNIT_TAX_YEAR_UPSERT_SQL from
ears_format.py are reused UNCHANGED, no new SQL needed).

Ingestion gate: this file builds and runs its OWN Dallas-specific gate
(run_dallas_ingest_gate(), below) rather than calling
ingest_gate.gather_and_run() directly -- that function is hardwired to
Travis/EARS's fixed-width, two-file (PROP.TXT/PROP_ENT.TXT) shape
(prop_path=/prop_ent_path= params, scan_prop_ledger()/scan_prop_ent_ledger()),
incompatible with DCAD's relational, multi-CSV-table shape. This gate
reuses ingest_gate._write_audit() (the same shared ingest_audit writer
billing_gate.py already imports cross-module) so both gates land rows in
the identical real table/schema, just gathered differently -- the
redefined per-table G1 identities the PX-20260826-03 report proposed,
made real: one G1 check per loaded table (conservation identity:
sum(buckets) == total_lines, with the real named bucket breakdown --
accepted / no_account_num / bpp_excluded -- visible in the detail string)
plus a G2-analog cross-table orphan check (find_orphan_accounts()).

PX-20260826-05 Task 2 (PM BLOCKER, real finding from the PX-20260826-05
dry-run runbook): this loader now ALSO writes `parcel`, via write_parcel()
below -- it never did before. That gap was real and load-bearing, not
stylistic: app.py's _county_has_data() (and every route gated on it)
queries `parcel` directly, so a fully successful, gate-PASS Dallas load
into prop_unit/prop_unit_tax_year/parcel_tax_year alone would still
present the site as "Dallas hasn't been loaded yet" everywhere. See
write_parcel()'s own docstring for the write shape (mirrors
load_certified_2025.py's own PROP.TXT -> parcel account-layer write) and
dcad_format.derive_parcel_class_fields() for the SPTD-derived state_cd1/
prop_type_cd fields it needs. compute_metrics.py's own
compute_county_benchmarks() bug (its INSERT...SELECT has no WHERE
county_code filter at all, so --county DALLAS would mislabel Travis's
aggregate stats as Dallas's) is a real, separate, pre-existing issue this
brief does NOT fix -- see the PX-20260826-05 runbook's own §5 for why
compute_metrics.py must stay deferred for Dallas regardless of this fix.
══════════════════════════════════════════════════════════════════════════
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config  # noqa: E402
from loaders import dcad_format  # noqa: E402
from loaders import ears_format  # noqa: E402
from loaders.ingest_gate import _write_audit  # noqa: E402
# psycopg2 and parcel_rollup (which itself pulls in psycopg2 via
# loaders/scrape_billing_history.py -> loaders/db.py) are deliberately
# NOT imported at module level here, unlike every other Travis loader's
# own convention (e.g. load_certified_historical.py imports psycopg2
# unconditionally at the top). This loader's own --dry-run mode is
# documented (both here and in load_tax_current.py's own precedent) as
# "zero DB connection required" -- making the import itself lazy (deferred
# into write_prop_unit_and_tax_year() and main()'s live-write branch,
# below) means that promise holds even in an environment with no psycopg2
# installed at all (confirmed: this sandbox has none, no network access
# to install one), not just "no connection is opened." A real, disclosed
# improvement on the existing convention, not a functional change to the
# live-write path itself -- psycopg2 is still required, and still used
# identically, once a live load actually runs.

DATA_SOURCE = "dcad_certified"   # the literal data_source column value this loader writes
COUNTY_CODE = "DALLAS"
BATCH_SIZE = 5000  # matches load_certified_historical.py's own batching cadence


# ══════════════════════════════════════════════════════════════════════
# Archive path resolution — PX-20260826-04: resolves through config.py's
# new Dallas archive grammar (lazy PEP 562, mirroring the existing Travis
# CERT_DIR family). See config.py's own _DALLAS_CERT_ARCHIVE_INFO comment
# for the real, vault_manifest.md-sourced per-year cert-date/folder pairs,
# and DALLAS_EXTRACTED_YEARS for which years actually have an extracted
# folder to point at (only 2026, as of this writing).
# ══════════════════════════════════════════════════════════════════════
def _cert_dir_for_year(year):
    return {
        2022: config.DALLAS_CERT_DIR_2022,
        2023: config.DALLAS_CERT_DIR_2023,
        2024: config.DALLAS_CERT_DIR_2024,
        2025: config.DALLAS_CERT_DIR_2025,
        2026: config.DALLAS_CERT_DIR_2026,
    }[year]


# ══════════════════════════════════════════════════════════════════════
# Step 1 — scan every loaded table's conservation ledger (pure, no DB
# access). This IS the redefined multi-table G1 (see report's ingest-gate
# section): a per-table ledger for each loaded table, not one file's line
# count.
# ══════════════════════════════════════════════════════════════════════
def scan_all_tables(table_dir):
    """
    table_dir: the extracted DCAD2026_CERTIFIED_07232026/ folder (or a
    canary-slice folder with the same per-table filenames).

    Returns {table_name: ledger_dict} for ACCOUNT_INFO, ACCOUNT_APPRL_YEAR,
    APPLIED_STD_EXEMPT -- the only three tables this design's value path
    reads (TAXABLE_OBJECT and the other 10 are deliberately unloaded, see
    dcad_format.TABLE_LOAD_POLICY).
    """
    ledgers = {}
    table_iterators = {
        "ACCOUNT_INFO": dcad_format.iter_account_info_records,
        "ACCOUNT_APPRL_YEAR": dcad_format.iter_account_apprl_year_records,
        "APPLIED_STD_EXEMPT": dcad_format.iter_applied_std_exempt_records,
    }
    for table_name, iter_fn in table_iterators.items():
        path = os.path.join(table_dir, dcad_format.TABLE_FILENAMES[table_name])
        ledgers[table_name] = dcad_format.scan_table_ledger(table_name, iter_fn, path=path)
    return ledgers


# ══════════════════════════════════════════════════════════════════════
# Step 2 — cross-table conservation: any secondary table's ACCOUNT_NUM
# should trace back to a real ACCOUNT_INFO row. A miss here is this
# design's ORPHAN class -- reported by name and count, NOT silently
# folded into a blanket FAIL (mirrors BG4's LEGACY-ONLY classification,
# PX-20260826-01).
# ══════════════════════════════════════════════════════════════════════
def find_orphan_accounts(account_info_ledger, other_table_ledger):
    """
    Pure set-difference, no DB access. Returns the set of account_nums
    present in `other_table_ledger` (e.g. APPLIED_STD_EXEMPT) but absent
    from `account_info_ledger` (ACCOUNT_INFO).
    """
    return other_table_ledger["account_nums"] - account_info_ledger["account_nums"]


def filter_bpp_accounts(account_info_records):
    """
    PX-20260826-04: BPP exclusion is now a NAMED G1 skip-bucket
    ("bpp_excluded") set directly by dcad_format.iter_account_info_records()
    -- see that function's own docstring for the full reasoning (a real,
    explicit override of the pre-rev1 design, per this brief's own
    instruction). This function just partitions on skip_reason: a row
    with skip_reason == "bpp_excluded" is counted as excluded; any other
    non-None skip_reason (e.g. "no_account_num") is dropped without being
    counted as BPP-excluded; a clean (skip_reason is None) row is kept.

    Returns (kept: {account_num: dict}, excluded_count: int).
    """
    kept = {}
    excluded = 0
    for rec in account_info_records:
        if rec["skip_reason"] == "bpp_excluded":
            excluded += 1
            continue
        if rec["skip_reason"] is not None:
            continue
        kept[rec["account_num"]] = rec
    return kept, excluded


# ══════════════════════════════════════════════════════════════════════
# Step 3 — build the prop_unit / prop_unit_tax_year row shape (pure
# transform, no DB access).
# ══════════════════════════════════════════════════════════════════════
def build_unit_rows(account_info_rows, appraisal_year_rows, exempt_rows_by_account, year):
    """
    account_info_rows: {account_num: dict} from iter_account_info_records,
        BPP-excluded already (see filter_bpp_accounts()).
    appraisal_year_rows: {account_num: dict} from iter_account_apprl_year_records
        -- carries tot_val/land_val/impr_val/hmstd_cap_val/county_taxable_val/
        sptd_code/appraisal_yr.
    exempt_rows_by_account: {account_num: [dict, ...]} from
        iter_applied_std_exempt_records, consumed via
        exemption_codes_for_account().
    year: this run's own --year argument (int). PRE-COMMIT FIX: every
        account's real APPRAISAL_YR is now asserted to equal this value --
        see dcad_format.validate_appraisal_year(). No fallback: an account
        with no matching appraisal_year_rows entry (appraisal_yr is None)
        fails loud exactly the same as a real mismatch.

    prop_id/geo_id derive directly from ACCOUNT_NUM via
    dcad_format.derive_prop_id_geo_id() -- PX-20260826-04 finding #2:
    two-tier (int(ACCOUNT_NUM) for all-digit accounts, a disjoint
    truncated-SHA-256 derivation for alphanumeric ones; only a None/blank
    ACCOUNT_NUM still fails loud here). This function additionally runs
    dcad_format.check_in_run_prop_id_collisions() over the whole built
    list before returning -- the required in-run duplicate-prop_id guard.

    Value columns derive via dcad_format.derive_value_mapping() (the one
    place the PM's PX-20260826-04 rulings live).

    Classification: sptd_code is carried through as prop_type_cd (the raw
    code, mirroring Travis's own raw-code-on-prop_unit convention) AND
    separately classified via dcad_format.classify_account_sptd() into
    benchmark_label (report-only -- not a prop_unit_tax_year column; no
    such column exists, same as Travis's own classi_cd living outside
    that table).

    Returns a list of dicts shaped for write_prop_unit_and_tax_year() below.
    """
    rows = []
    for account_num, info in account_info_rows.items():
        appr = appraisal_year_rows.get(account_num, {})
        # PRE-COMMIT FIX: fail-loud APPRAISAL_YR cross-check, no fallback.
        dcad_format.validate_appraisal_year(appr.get("appraisal_yr"), year, account_num)
        prop_id, geo_id = dcad_format.derive_prop_id_geo_id(account_num)
        sptd_code = appr.get("sptd_code")
        benchmark_label = dcad_format.classify_account_sptd(sptd_code)
        # PX-20260826-05 Task 2 (PM BLOCKER): the SPTD-derived class fields
        # write_parcel() below needs for `parcel` -- see
        # dcad_format.derive_parcel_class_fields()'s own docstring for the
        # full reasoning (state_cd1 is Dallas's real analog of Travis's own
        # AJR-sourced parcel.state_cd1, via DCAD's own confirmed
        # SPTD-code-to-PTAD-class cross-reference).
        parcel_class_fields = dcad_format.derive_parcel_class_fields(sptd_code)
        values = dcad_format.derive_value_mapping(
            tot_val=appr.get("tot_val"),
            land_val=appr.get("land_val"),
            impr_val=appr.get("impr_val"),
            hmstd_cap_val=appr.get("hmstd_cap_val"),
            county_taxable_val=appr.get("county_taxable_val"),
        )
        rows.append({
            "county_code": COUNTY_CODE,
            "prop_id": prop_id,
            "geo_id": geo_id,
            "tax_year": year,
            "prop_type_cd": sptd_code,
            "market_value": values["market_value"],
            "land_value": values["land_value"],
            "imprv_value": values["imprv_value"],
            "assessed_value": values["assessed_value"],
            "hs_cap_loss": values["hs_cap_loss"],
            "taxable_value": values["taxable_value"],
            "exemption_codes": dcad_format.exemption_codes_for_account(exempt_rows_by_account.get(account_num, [])),
            "data_source": DATA_SOURCE,
            "owner_name": info.get("owner_name"),
            "owner_suppressed": info.get("owner_suppressed", False),
            "situs_address": info.get("situs_address"),
            "zip_code": info.get("zip_code"),
            "sptd_code": sptd_code,
            "benchmark_label": benchmark_label,
            "state_cd1": parcel_class_fields["state_cd1"],
        })
    # PX-20260826-04 finding #2, required guard 1 of 2: fail-loud, in-run
    # duplicate-prop_id detection across this whole build -- now that
    # prop_id is no longer a 1:1 deterministic function of a validated-
    # numeric string (alphanumeric accounts hash into a disjoint range,
    # see dcad_format._hashed_prop_id()), a same-run collision is a real,
    # if rare, possibility this design must not silently absorb. See
    # dcad_format.check_in_run_prop_id_collisions() for the guard logic
    # and DuplicatePropIdError for what it raises.
    dcad_format.check_in_run_prop_id_collisions(rows)
    return rows


# ══════════════════════════════════════════════════════════════════════
# PX-20260826-04 finding #2, required guard 2 of 2 — persistent, write-
# time identity guard. Thin DB-querying wrapper around dcad_format.
# find_prop_id_geo_id_conflicts()'s pure comparison logic (kept there so
# that function stays unit-testable without a DB); this wrapper is the
# only piece of the guard that actually touches psycopg2, which is why it
# lives here (a lazy-psycopg2 module) rather than in dcad_format.py.
# ══════════════════════════════════════════════════════════════════════
def _fetch_existing_prop_id_geo_id(conn, county_code, prop_ids):
    """
    Returns {prop_id: geo_id} for every (county_code, prop_id) already on
    file in prop_unit among the given prop_ids. Empty dict (and zero
    queries) if prop_ids is empty. Live-write path only -- requires conn.
    """
    if not prop_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT prop_id, geo_id FROM prop_unit WHERE county_code = %s AND prop_id = ANY(%s)",
            (county_code, list(prop_ids)),
        )
        return {prop_id: geo_id for prop_id, geo_id in cur.fetchall()}


# ══════════════════════════════════════════════════════════════════════
# Step 4 — write prop_unit / prop_unit_tax_year. Reuses
# ears_format.PROP_UNIT_UPSERT_SQL / PROP_UNIT_TAX_YEAR_UPSERT_SQL
# UNCHANGED -- both already accept county_code as their first bound
# parameter and already use ON CONFLICT (county_code, prop_id[, tax_year]),
# exactly Dallas's own real, live constraint shape (already migrated by
# migrate_county_partitioning.py). No new SQL needed.
# ══════════════════════════════════════════════════════════════════════
def write_prop_unit_and_tax_year(conn, unit_rows, dry_run=False):
    """
    dry_run=True: counts only, ZERO DB access (conn may be None) --
    matches load_tax_current.py's own --dry-run convention exactly. The
    write-time prop_id/geo_id conflict guard (PX-20260826-04 finding #2)
    is SKIPPED in this mode -- it needs a DB read against what's already
    written, which --dry-run has no connection to perform. This is a real,
    disclosed gap in dry-run coverage (the in-run guard,
    check_in_run_prop_id_collisions(), already ran inside build_unit_rows()
    and DOES cover dry-run) -- not a silent omission.

    Dallas has no numeric owner_id source (MULTI_OWNER is deliberately
    unloaded, see TABLE_LOAD_POLICY) -- prop_unit.owner_id is always
    written as None/NULL for this loader, same as prop_unit's own
    nullable-column contract already allows.

    first_seen_year/last_seen_year are both set to this row's own
    tax_year -- same convention load_certified_historical.py's own
    load_prop_unit() uses for a single-year load (LEAST/GREATEST in
    PROP_UNIT_UPSERT_SQL's ON CONFLICT extends the range correctly across
    repeated multi-year runs).

    Returns (n_prop_unit_rows, n_tax_year_rows).

    Raises dcad_format.DuplicatePropIdError (fail-loud, before any write)
    if any row's prop_id already exists in prop_unit under a different
    geo_id -- see _fetch_existing_prop_id_geo_id() and dcad_format.
    find_prop_id_geo_id_conflicts() above.
    """
    if dry_run:
        return len(unit_rows), len(unit_rows)

    # Guard runs BEFORE the psycopg2.extras import below, deliberately --
    # it only needs conn.cursor() (plain DB-API, no psycopg2-specific
    # helpers) and should fail loud as early as possible, before this
    # loader commits to needing execute_batch at all. A real, small
    # ordering improvement, not just a style choice.
    if unit_rows:
        county_code = unit_rows[0]["county_code"]
        existing = _fetch_existing_prop_id_geo_id(
            conn, county_code, [row["prop_id"] for row in unit_rows])
        conflicts = dcad_format.find_prop_id_geo_id_conflicts(existing, unit_rows)
        if conflicts:
            raise dcad_format.DuplicatePropIdError(
                f"Write-time prop_id/geo_id conflict: {len(conflicts)} prop_id(s) "
                f"already exist in prop_unit (county={county_code}) under a "
                f"DIFFERENT geo_id than this run is about to write -- refusing to "
                f"write ANY row from this run (ON CONFLICT DO UPDATE would "
                f"otherwise silently overwrite an unrelated, already-loaded "
                f"account's geo_id). First 10: {conflicts[:10]}"
            )

    import psycopg2.extras  # lazy -- see module-level comment on the import block above

    prop_unit_rows = []
    tax_year_rows = []
    for row in unit_rows:
        prop_unit_rows.append((
            row["county_code"], row["prop_id"], row["geo_id"], row["prop_type_cd"],
            row["situs_address"], None, row["owner_name"],
            row["tax_year"], row["tax_year"],
        ))
        tax_year_rows.append((
            row["county_code"], row["prop_id"], row["tax_year"], row["geo_id"],
            row["market_value"], row["assessed_value"], row["taxable_value"],
            row["hs_cap_loss"], row["land_value"], row["imprv_value"],
            row["exemption_codes"], row["data_source"],
        ))

    for i in range(0, len(prop_unit_rows), BATCH_SIZE):
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur, ears_format.PROP_UNIT_UPSERT_SQL, prop_unit_rows[i:i + BATCH_SIZE], page_size=2000)
            psycopg2.extras.execute_batch(
                cur, ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL, tax_year_rows[i:i + BATCH_SIZE], page_size=2000)
        conn.commit()

    return len(prop_unit_rows), len(tax_year_rows)


# ══════════════════════════════════════════════════════════════════════
# Step 4b — write `parcel`. PX-20260826-05 Task 2 (PM BLOCKER, real
# finding from the PX-20260826-05 dry-run runbook): this loader never
# wrote `parcel` before this -- only Travis's three legacy loaders
# (load_ajr.py, load_certified_2025.py, load_2026_preliminary.py) ever
# issue INSERT INTO parcel. That is a real, load-bearing gap, not a
# stylistic omission: app.py's _county_has_data() (and every route gated
# on it) queries `parcel` directly, not prop_unit/prop_unit_tax_year --
# so a fully successful, gate-PASS Dallas load would still present as
# "hasn't been loaded yet" everywhere without this write.
#
# Mirrors load_certified_2025.py's own PROP.TXT -> parcel write (the
# Travis account-layer precedent this brief asked to mirror): PROP.TXT is
# Travis's own authoritative, first-write account-layer source, exactly
# the same relationship ACCOUNT_INFO/ACCOUNT_APPRL_YEAR jointly have for
# Dallas -- so this uses that loader's unconditional EXCLUDED-overwrite
# ON CONFLICT shape, not load_ajr.py's own COALESCE-preserving "fill in
# what's missing" shape (load_ajr.py is a supplementary, fill-in-the-gaps
# source layered on TOP of an already-authoritative parcel row; this
# loader IS the authoritative source for a first Dallas load, so an
# unconditional overwrite on re-run is the correct, idempotent behavior --
# re-running this loader against the same archive should always converge
# `parcel` to the same real values, not silently preserve a stale one).
# ══════════════════════════════════════════════════════════════════════
# PX-20260827-06: zip_code added -- `parcel` has a zip_code column
# (schema.sql) but no city column, so ZIP is now populated from
# ACCOUNT_INFO's PROPERTY_ZIPCODE (via dcad_format.iter_account_info_
# records() -> build_unit_rows()'s "zip_code" key) and city is never
# attempted at all (not merely excluded from situs_address -- see
# dcad_format.PROPERTY_ZIPCODE_FIELD's own comment for the full
# reasoning). This is additive only -- every other column/behavior here
# is unchanged from PX-20260826-05.
PARCEL_SQL = """
    INSERT INTO parcel
        (county_code, geo_id, prop_id, prop_type_cd, state_cd1, owner_name, situs_address, zip_code)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (county_code, geo_id) DO UPDATE
        SET prop_id       = EXCLUDED.prop_id,
            prop_type_cd  = EXCLUDED.prop_type_cd,
            state_cd1     = EXCLUDED.state_cd1,
            owner_name    = EXCLUDED.owner_name,
            situs_address = EXCLUDED.situs_address,
            zip_code      = EXCLUDED.zip_code
"""


def write_parcel(conn, unit_rows, dry_run=False):
    """
    Upsert `parcel` from this run's own unit_rows -- one parcel row per
    account: geo_id=ACCOUNT_NUM verbatim, prop_id via the same two-tier
    derivation prop_unit already used (unit_rows already carries the
    final prop_id, not re-derived here), state_cd1/prop_type_cd from
    dcad_format.derive_parcel_class_fields() (already folded into each
    unit_rows dict by build_unit_rows()).

    dry_run=True: count only, ZERO DB access (conn may be None) -- same
    convention as write_prop_unit_and_tax_year().

    Idempotent by construction: re-running this against the same unit_rows
    always upserts to the identical final values (ON CONFLICT ... DO
    UPDATE SET <col> = EXCLUDED.<col> for every written column, no
    COALESCE-preserving branch) -- a second run is a no-op in effect, not
    merely "doesn't crash." See test_dcad_format.py's
    test_write_parcel_idempotent_on_rerun() for the fixture proof (a
    FakeConn/FakeCursor mock, since this sandbox has no live DB -- the
    proof is that PARCEL_SQL's own ON CONFLICT clause, not any
    apply-level dedup logic, is what guarantees this).

    Returns n_parcel_rows (== len(unit_rows) always -- one parcel row per
    account, since geo_id IS ACCOUNT_NUM and build_unit_rows() already
    iterates a {account_num: dict}, so there is no possibility of two
    different accounts colliding into one parcel row the way a multi-unit
    PARCEL/geo_id split can for Travis).
    """
    if dry_run:
        return len(unit_rows)

    import psycopg2.extras  # lazy -- see module-level comment on the import block above

    parcel_rows = [
        (row["county_code"], row["geo_id"], row["prop_id"], row["prop_type_cd"],
         row["state_cd1"], row["owner_name"], row["situs_address"], row.get("zip_code"))
        for row in unit_rows
    ]

    for i in range(0, len(parcel_rows), BATCH_SIZE):
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur, PARCEL_SQL, parcel_rows[i:i + BATCH_SIZE], page_size=2000)
        conn.commit()

    return len(parcel_rows)


# ══════════════════════════════════════════════════════════════════════
# Step 5 — Dallas-specific ingestion gate. See module docstring's own
# "Ingestion gate" section for why this is NOT a call into
# ingest_gate.gather_and_run().
# ══════════════════════════════════════════════════════════════════════
G3_SITUS_MIN_PCT = 99.0  # hard FAIL below -- brief's own threshold
G3_OWNER_MIN_PCT = 95.0  # lower than situs -- leaves room for legitimate EXCLUDE_OWNER suppressions


def compute_field_coverage(unit_rows):
    """
    PX-20260827-06 item 5/6: pure, DB-free computation of the two
    G3_FIELD_COVERAGE percentages over this run's own accepted unit_rows
    (BPP-excluded/no_account_num rows never reach unit_rows at all, so this
    is coverage among ACCEPTED rows only, matching the brief's own wording).

    Returns a dict: n_rows, situs_nonempty, situs_pct, owner_nonempty,
    owner_pct, owner_suppressed (count of rows where EXCLUDE_OWNER
    deliberately zeroed owner_name -- reported separately so a low
    owner_pct can be told apart from "genuinely missing data" vs
    "legitimate Sec. 25.025 suppression").

    n_rows == 0 is treated as 100%/100% (vacuously true, avoids a
    ZeroDivisionError on an empty/all-excluded run) -- an empty run has
    bigger problems than this gate, and those are caught by G1 instead.
    """
    n_rows = len(unit_rows)
    if n_rows == 0:
        return {
            "n_rows": 0, "situs_nonempty": 0, "situs_pct": 100.0,
            "owner_nonempty": 0, "owner_pct": 100.0, "owner_suppressed": 0,
        }
    situs_nonempty = sum(1 for r in unit_rows if r.get("situs_address"))
    owner_nonempty = sum(1 for r in unit_rows if r.get("owner_name"))
    owner_suppressed = sum(1 for r in unit_rows if r.get("owner_suppressed"))
    return {
        "n_rows": n_rows,
        "situs_nonempty": situs_nonempty,
        "situs_pct": situs_nonempty / n_rows * 100.0,
        "owner_nonempty": owner_nonempty,
        "owner_pct": owner_nonempty / n_rows * 100.0,
        "owner_suppressed": owner_suppressed,
    }


def run_dallas_ingest_gate(conn, ledgers, orphans, unit_rows, tax_year, county_code, dry_run=False):
    """
    Builds one G1_<TABLE> check per loaded table (conservation identity:
    sum(ledger['buckets'].values()) == ledger['total_lines'] -- true by
    scan_table_ledger()'s own construction, but the real value here is
    the VISIBLE, named bucket breakdown in the detail string, e.g.
    "accepted=780,412 bpp_excluded=1,204 no_account_num=3" -- exactly the
    kind of tested-alarm, no-silent-drops accounting
    SPEC_UNIT_MODEL_AND_INGEST_GATE.md's own G1 already enforces for
    Travis's fixed-width source, redefined here for a relational one) plus
    a G2_ORPHAN_ACCOUNTS check (find_orphan_accounts() -- reported by name
    and count per PX-20260826-01's own "must be listed, not hidden" rule;
    a non-empty orphan set does not by itself fail this check, matching
    BG4's own LEGACY-ONLY-style non-blocking classification -- a real,
    disclosed judgment call, not silently assumed) plus a NEW
    G3_FIELD_COVERAGE check (PX-20260827-06 item 5, the gate-parity item
    made concrete): non-empty situs_address must be >= G3_SITUS_MIN_PCT
    (99%) and non-empty owner_name >= G3_OWNER_MIN_PCT (95%) of unit_rows
    -- hard FAIL below either threshold. This is the concrete backstop for
    exactly the bug this brief fixes: an all-empty situs_address/owner_name
    field (the OWNER_NAME/SITUS_ADDR placeholder bug, 0% coverage) could
    previously pass G1/G2 silently forever, since neither check ever
    inspected these two columns' actual content -- only their presence.

    dry_run=True: builds and returns the same summary dict, prints nothing
    to ingest_audit, requires no DB connection (conn may be None) --
    matches this loader's own --dry-run convention. unit_rows must still
    be the real, fully-built list even in dry-run (see compute_field_
    coverage() -- pure/DB-free, so this costs nothing extra).

    Reuses ingest_gate._write_audit() (also imported cross-module by
    billing_gate.py) to land rows in the real ingest_audit table with the
    identical shape/columns Travis's own gate writes.
    """
    checks = {}
    for table_name, ledger in ledgers.items():
        bucket_sum = sum(ledger["buckets"].values())
        passed = bucket_sum == ledger["total_lines"]
        detail = (f"total_lines={ledger['total_lines']:,} "
                  f"buckets={ledger['buckets']}")
        checks[f"G1_{table_name}"] = (passed, detail)

    orphan_detail = (f"{len(orphans)} APPLIED_STD_EXEMPT account_num(s) with no "
                      f"ACCOUNT_INFO counterpart"
                      + (f" -- {sorted(orphans)[:10]}{'...' if len(orphans) > 10 else ''}"
                         if orphans else ""))
    # Non-blocking by design (see docstring) -- reported, not hidden.
    checks["G2_ORPHAN_ACCOUNTS"] = (True, orphan_detail)

    coverage = compute_field_coverage(unit_rows)
    g3_passed = (coverage["situs_pct"] >= G3_SITUS_MIN_PCT
                 and coverage["owner_pct"] >= G3_OWNER_MIN_PCT)
    g3_detail = (
        f"situs_address non-empty: {coverage['situs_pct']:.2f}% "
        f"({coverage['situs_nonempty']:,}/{coverage['n_rows']:,}, threshold "
        f">={G3_SITUS_MIN_PCT:.0f}%); owner_name non-empty: "
        f"{coverage['owner_pct']:.2f}% ({coverage['owner_nonempty']:,}/"
        f"{coverage['n_rows']:,}, threshold >={G3_OWNER_MIN_PCT:.0f}%, "
        f"{coverage['owner_suppressed']:,} of those blanks are deliberate "
        f"EXCLUDE_OWNER Sec. 25.025 suppressions, not missing data)"
    )
    checks["G3_FIELD_COVERAGE"] = (g3_passed, g3_detail)

    overall_passed = all(passed for passed, _ in checks.values())

    if not dry_run:
        _write_audit(conn, DATA_SOURCE, tax_year, checks, county_code=county_code)

    return {"checks": checks, "passed": overall_passed}


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, required=True, choices=[2022, 2023, 2024, 2025, 2026],
                     help="Tax year to load. Only 2026 has an extracted archive folder as "
                          "of this writing (config.DALLAS_EXTRACTED_YEARS) -- 2022-2025 "
                          "fail loud with a clear 'not yet extracted' message, not a "
                          "generic 'directory not found'.")
    ap.add_argument("--county", default=COUNTY_CODE,
                     help=f"county_code written to every prop_unit/prop_unit_tax_year row "
                          f"(default: {COUNTY_CODE}).")
    ap.add_argument("--table-dir", default=None,
                     help="Override the resolved archive directory -- mainly for a "
                          "canary-slice folder or a local test fixture directory with the "
                          "same per-table filenames. If omitted, resolved from config.py's "
                          "Dallas archive grammar for --year.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Full parse + classify + gate scan; ZERO DB writes, ZERO DB "
                          "connection opened at all -- matches load_tax_current.py's own "
                          "--dry-run convention exactly.")
    ap.add_argument("--skip-gate", action="store_true",
                     help="Skip the ingestion gate after a live load (matches other "
                          "loaders' --skip-gate escape-hatch naming/reasoning). Ignored "
                          "in --dry-run mode (the gate still runs there, scan-only).")
    args = ap.parse_args()

    year = args.year
    county_code = args.county

    if args.table_dir:
        table_dir = args.table_dir
    else:
        if year not in config.DALLAS_EXTRACTED_YEARS:
            print(f"ERROR: {year} Dallas certified roll has not been extracted yet. "
                  f"vault_manifest.md's own Migration 4 rows show only the still-zipped "
                  f".ZIP file for this year (DCAD's relational CSV product) -- extraction "
                  f"into per-table CSVs is a separate, later step, out of this brief's own "
                  f"scope. Pass --table-dir to point directly at an already-extracted "
                  f"folder (e.g. a canary slice) if you have one.")
            sys.exit(1)
        try:
            table_dir = _cert_dir_for_year(year)
        except KeyError:
            print(f"ERROR: no archive grammar registered for year {year}")
            sys.exit(1)
        except config.ArchiveNotMountedError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    if not os.path.isdir(table_dir):
        print(f"ERROR: table dir not found: {table_dir}")
        sys.exit(1)

    print(f"\n{'─'*65}")
    print(f"  Loading {year} DCAD Certified Roll -- county={county_code}")
    print(f"  Source dir : {table_dir}")
    print(f"  data_source: {DATA_SOURCE}")
    print(f"{'─'*65}\n")

    t_total = time.time()

    print("Scanning per-table conservation ledgers (G1, zero DB access)…")
    ledgers = scan_all_tables(table_dir)
    for name, ledger in ledgers.items():
        print(f"  {name}: {ledger['total_lines']:,} lines, buckets={ledger['buckets']}")

    orphans = find_orphan_accounts(ledgers["ACCOUNT_INFO"], ledgers["APPLIED_STD_EXEMPT"])
    if orphans:
        print(f"  ORPHAN-ACCOUNT (n={len(orphans)}): {sorted(orphans)[:10]}"
              f"{'...' if len(orphans) > 10 else ''}")
    else:
        print("  ORPHAN-ACCOUNT: none found.")

    # Second pass over each (modestly sized, single-table) CSV to build
    # the real row dicts -- the ledger scan above is a pure count/bucket
    # pass. Same accepted two-pass tradeoff load_certified_historical.py's
    # own module comment documents for a historical/occasional loader,
    # not a nightly job.
    account_info_kept, bpp_excluded = filter_bpp_accounts(
        dcad_format.iter_account_info_records(
            path=os.path.join(table_dir, dcad_format.TABLE_FILENAMES["ACCOUNT_INFO"])))

    appraisal_year_rows = {
        r["account_num"]: r
        for r in dcad_format.iter_account_apprl_year_records(
            path=os.path.join(table_dir, dcad_format.TABLE_FILENAMES["ACCOUNT_APPRL_YEAR"]))
        if r["skip_reason"] is None
    }

    exempt_rows_by_account = {}
    for r in dcad_format.iter_applied_std_exempt_records(
            path=os.path.join(table_dir, dcad_format.TABLE_FILENAMES["APPLIED_STD_EXEMPT"])):
        if r["skip_reason"] is not None:
            continue
        exempt_rows_by_account.setdefault(r["account_num"], []).append(r)

    print(f"  BPP-excluded accounts: {bpp_excluded:,} (DIVISION_CD == BPP -- named G1 "
          f"skip-bucket 'bpp_excluded', see dcad_format.TABLE_LOAD_POLICY)")

    # PRE-COMMIT FIX (real correction): tax_year no longer falls back to
    # --year on a missing/wrong value -- it is now the run's own --year,
    # ONLY after build_unit_rows() has asserted every account's real
    # APPRAISAL_YR (the actual column; the earlier "TAX_YR" guess doesn't
    # exist and always returned None, which is why 100% of rows silently
    # fell back before this fix) equals it, fail-loud on any mismatch
    # (dcad_format.AppraisalYearMismatchError, uncaught here by design --
    # a real mismatch means either the wrong archive folder is loaded for
    # this --year or a genuine per-account anomaly, either way worth a
    # hard stop, not a silent coercion).
    unit_rows = build_unit_rows(account_info_kept, appraisal_year_rows, exempt_rows_by_account, year)

    # Real, live classification distribution -- Task 1's own explicit ask
    # to verify (not just assume) classify_dallas_sptb_code()'s behavior
    # against this run's real SPTD_CODE values.
    benchmark_counts = {}
    for row in unit_rows:
        benchmark_counts[row["benchmark_label"]] = benchmark_counts.get(row["benchmark_label"], 0) + 1
    print(f"  Classification distribution (classify_dallas_sptb_code, live): {benchmark_counts}")

    print(f"  {len(unit_rows):,} unit rows built (prop_id/geo_id derived per-account; "
          f"APPRAISAL_YR cross-checked against --year={year}, fail-loud on mismatch)")

    if args.dry_run:
        gate_summary = run_dallas_ingest_gate(
            conn=None, ledgers=ledgers, orphans=orphans, unit_rows=unit_rows,
            tax_year=year, county_code=county_code, dry_run=True)
        for code, (passed, detail) in gate_summary["checks"].items():
            print(f"    {code}: {'PASS' if passed else 'FAIL'} — {detail}")
        print(f"  GATE OVERALL (dry-run, not written to ingest_audit): "
              f"{'PASS' if gate_summary['passed'] else 'FAIL'}")

        # PX-20260826-05 Task 2 (PM BLOCKER): report the would-be `parcel`
        # count too, now that this loader actually writes that table --
        # always == len(unit_rows) (one parcel row per account; see
        # write_parcel()'s own docstring for why no collision is possible
        # here), but reported explicitly rather than left implicit, so a
        # dry-run reviewer sees every table this run will touch, not just
        # prop_unit/prop_unit_tax_year.
        n_parcel_would_write = write_parcel(conn=None, unit_rows=unit_rows, dry_run=True)
        print(f"  {n_parcel_would_write:,} parcel rows would be upserted "
              f"(county={county_code}; 1 per account)")

        print(f"\n  *** --dry-run: no DB connection opened, zero writes ***")
        print(f"  Total elapsed: {time.time()-t_total:.1f}s")
        print(f"\nDone (dry-run). {len(unit_rows):,} rows would be written to "
              f"prop_unit/prop_unit_tax_year/parcel for tax_year={year}, "
              f"county={county_code}. Live load is out of scope for this brief -- "
              f"the runbook (pre-flight checks, rollback plan, canary-first "
              f"sequencing) is the next brief.")
        return

    import psycopg2  # lazy -- see module-level comment on the import block above
    import parcel_rollup  # lazy, same reason (pulls in psycopg2 transitively)

    conn = psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASS,
    )

    n_prop_unit, n_tax_year = write_prop_unit_and_tax_year(conn, unit_rows, dry_run=False)
    print(f"  → {n_prop_unit:,} prop_unit rows upserted, {n_tax_year:,} prop_unit_tax_year rows upserted")

    # PX-20260826-05 Task 2 (PM BLOCKER): write `parcel` too -- see
    # write_parcel()'s own module-level comment block for why this was
    # missing before and why it matters (_county_has_data() and every
    # route gated on it query `parcel` directly).
    n_parcel = write_parcel(conn, unit_rows, dry_run=False)
    print(f"  → {n_parcel:,} parcel rows upserted")

    print(f"  Rolling up prop_unit_tax_year → parcel_tax_year for {year} (county={county_code})…")
    result = parcel_rollup.run(conn, tax_year=year, county_code=county_code)
    print(f"    → prop_id repaired: {result['prop_id_repaired']:,}, "
          f"parcel_tax_year rows: {result['parcel_tax_year_rows']:,}")

    if args.skip_gate:
        print(f"\n  ⚠ Gate SKIPPED (--skip-gate passed) — no checks ran, no ingest_audit "
              f"row written for this run.")
    else:
        print(f"\n  Running Dallas ingestion gate for {DATA_SOURCE}…")
        gate_summary = run_dallas_ingest_gate(
            conn, ledgers, orphans, unit_rows, tax_year=year, county_code=county_code, dry_run=False)
        for code, (passed, detail) in gate_summary["checks"].items():
            print(f"    {code}: {'PASS' if passed else 'FAIL'} — {detail}")
        print(f"  GATE OVERALL: {'PASS' if gate_summary['passed'] else 'FAIL'}")
        if not gate_summary["passed"]:
            print(f"  ⚠ Gate reported a FAILURE for {DATA_SOURCE}. Data has already been "
                  f"written and rolled up -- a loud post-hoc signal, not a pre-write "
                  f"block (matching run_all.py's own gate-after-load ordering).")

    print(f"\n  Total elapsed: {time.time()-t_total:.1f}s")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
