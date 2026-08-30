"""Shared database connection helper."""
import datetime
import psycopg2
import psycopg2.extras
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

# Generalized tax_year sanity guard (July 2026, "Homestead-Cap Data Integrity"
# Cowork brief, Issue 4). The July 14 incident happened because ONE loader
# (load_tax_current.py) had a hard, hand-rolled EXPECTED_TAX_YEAR==2025 reject,
# but nothing enforced any bound at all in the other loaders that also write
# tax_billing -- a bad TAXYEAR field in a future source file (blank, corrupted,
# a literal sentinel like 9999, or a stray pre-2021 row) could sail straight
# into the table from any of them. This is a single shared, generic guard
# (NOT the same as load_tax_current.py's own stricter EXPECTED_TAX_YEAR==2025
# check, or load_pir_billing.py's own stricter VALID_YEARS={2021..2024} set --
# those loaders should KEEP their own narrower, loader-specific gates; this is
# a broad backstop underneath all of them so a class of bug like this can't
# recur even in a loader nobody's added a narrow gate to yet).
#
# 1990: Travis County's earliest CAD digital records; nothing genuine
# predates this. current year + 1: allows loading a year's PRELIMINARY roll
# before it's technically "this tax year" on the calendar, mirroring the
# existing 2026-preliminary-in-2026 pattern, without opening the door to
# far-future sentinel/garbage values.
MIN_VALID_TAX_YEAR = 1990


def is_valid_tax_year(tax_year):
    """
    Return True if tax_year is a plausible real tax year for this dataset --
    False for None, non-ints, or anything outside [1990, current_year + 1].
    Every loader that writes tax_billing (or parcel_tax_year) should call this
    on every row before it reaches an INSERT, in addition to any narrower,
    loader-specific year gate it already has.
    """
    if not isinstance(tax_year, int):
        return False
    max_valid = datetime.date.today().year + 1
    return MIN_VALID_TAX_YEAR <= tax_year <= max_valid


def _warn_if_county_code_missing(conn):
    """Real, preventative fix (PIR-XLSX-HOTFIX-1 follow-up, Aug 17 2026): a
    real, live incident this session -- Diego ran a loader expecting a
    meaningful test, silently hit a stale local database via the
    DATABASE_URL-unset fallback, and got a confusing, out-of-context
    "column county_code does not exist" error 80 seconds into an unrelated
    437K-row parse, with nothing pointing at "wrong database" as the real
    cause. county_code became a real, NOT NULL, leading-PK column on
    tax_billing/tax_billing_entity/parcel/... once
    migrate_county_partitioning.py ran against production (DALLAS-GATE-1) --
    a database that predates that migration will fail the same confusing way
    the moment ANY of this codebase's county_code-aware SQL runs, which is
    effectively every real tax_billing-family read/write as of DALLAS-GATE-4/
    PIR-XLSX-HOTFIX-1. One cheap information_schema lookup per connection;
    WARNS rather than raises -- must not block execute_schema() from
    bootstrapping a genuinely fresh, pre-migration database where
    tax_billing may not exist at all yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'tax_billing'"
        )
        table_exists = cur.fetchone() is not None
        if not table_exists:
            return  # fresh/empty DB -- not this check's concern, execute_schema()'s job
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tax_billing' AND column_name = 'county_code'"
        )
        has_county_code = cur.fetchone() is not None
    if not has_county_code:
        print(
            "  [db] *** WARNING: this database's tax_billing table exists but has no "
            "county_code column -- it predates migrate_county_partitioning.py's real, "
            "already-run-in-production migration. Any tax_billing-family loader will "
            "fail with 'column county_code does not exist' the moment it touches this "
            "table. Run migrate_county_partitioning.py against this database first, or "
            "connect to a database that's already been migrated (check DATABASE_URL). ***"
        )


def get_conn():
    # PIR-XLSX-HOTFIX-1 follow-up: unmissable connection-identity banner --
    # see config.py's own DB_SOURCE comment for the real incident this
    # responds to. Printed BEFORE connecting so it's visible even if the
    # connection itself fails.
    print(
        f"  [db] connecting to {config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}"
        f"/{config.DB_NAME}  (source: {config.DB_SOURCE})"
    )
    if config.DB_SOURCE == "local-fallback-defaults":
        print(
            "  [db] *** DATABASE_URL is not set in this shell -- this is the LOCAL "
            "fallback database, not production. If you meant to test against "
            "production, set DATABASE_URL first. ***"
        )
    conn = psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASS,
    )
    _warn_if_county_code_missing(conn)
    return conn


def execute_schema(conn):
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Schema applied.")


def column_exists(conn, table, column):
    """Real bug this closes (Diego, live finding, PX-20260829-07 follow-up):
    execute_schema() above prints 'Schema applied.' unconditionally, but
    schema.sql's own CREATE TABLE IF NOT EXISTS is a documented no-op
    against an ALREADY-EXISTING production table -- it will never add a
    column schema.sql introduced after that table was first created (see
    schema.sql's own comment above the county_tax_rate mo_rate/is_rate
    columns for exactly this case: those two columns need a one-time,
    separately-run ALTER TABLE against production, NOT part of
    execute_schema()'s own execution path). 'Schema applied.' printing
    unconditional success made this look like it had worked when it had
    silently applied nothing for those two columns. Callers that depend on
    a specific new column existing should verify it with this function
    right after execute_schema() and fail loud with the exact ALTER TABLE
    needed, rather than let a later INSERT crash with a confusing 'column
    does not exist' partway through a write."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


# Standing project rule (PX-20260830-01 Task 4): the ONE real production
# Postgres address, per Diego's own explicit instruction. Earlier scripts
# in this project (refresh_group_stats.py, migrate_county_partitioning.py,
# g6_reconciliation.py, verify_index_coverage.py, backfill_prop_unit_tax_
# year_geoid.py) already print inet_server_addr() for a HUMAN to eyeball
# before trusting a run -- a real, useful defense, but one that depends on
# someone actually reading the printed address correctly under time
# pressure before a destructive write. EXPECTED_PRODUCTION_HOST +
# assert_production_db() below is the first loader-level HARD version of
# that same check, for a caller that wants a write refused outright rather
# than merely flagged for review.
EXPECTED_PRODUCTION_HOST = "10.30.105.217"


class WrongDatabaseError(RuntimeError):
    """Raised by assert_production_db() -- see that function's own
    docstring."""


def assert_production_db(conn, expected_addr=EXPECTED_PRODUCTION_HOST):
    """Verify this connection's own inet_server_addr() matches
    expected_addr EXACTLY before a caller performs any write that isn't
    trivially undoable (a DELETE, a --full-reload, a batch upsert against
    a shared table). Raises WrongDatabaseError -- never returns False;
    absence of an exception IS the pass signal, same convention as
    dallas_rates_format.check_entity_code_collisions() -- if it doesn't
    match, so a call site can simply call this and let the exception
    propagate (or catch it for a cleaner CLI message) rather than having
    to remember to check a boolean return value itself.

    Real footgun this closes: DATABASE_URL silently pointed at a local or
    staging database (or, just as dangerous the other way, a real
    production run intended for staging) -- get_conn()'s own
    "local-fallback-defaults" banner already flags ONE version of this
    (DATABASE_URL unset entirely); this closes the broader case where
    DATABASE_URL IS set, but to the wrong place, which that banner cannot
    detect at all.

    Note: inet_server_addr() returns NULL for a connection made over a
    Unix-domain socket (a real, common local-Postgres shape) rather than
    TCP/IP -- that correctly compares unequal to expected_addr here and
    raises, exactly as it should (a Unix-socket connection is never this
    project's real production database, which is only ever reached over
    TCP).

    Deliberately a small, standalone function -- NOT folded into
    get_conn() itself -- so it stays opt-in per call site rather than
    hard-failing every existing script that connects for a read-only or
    non-destructive purpose. See load_dallas_tax_rates.py's own call site
    (called immediately after get_conn(), before execute_schema() or any
    DELETE/INSERT) for the intended usage pattern; future loaders should
    call this at their own real write boundary the same way."""
    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        row = cur.fetchone()
        addr = str(row[0]) if row and row[0] is not None else None
    if addr != expected_addr:
        raise WrongDatabaseError(
            f"refusing to write -- expected production host "
            f"{expected_addr!r}, but this connection's own "
            f"inet_server_addr() reports {addr!r}. This is almost always "
            f"DATABASE_URL pointed at a local/staging database instead of "
            f"production (or vice versa) -- verify DATABASE_URL before "
            f"retrying. No write has happened yet."
        )
    return addr


def batch_upsert(conn, sql, rows, batch=2000):
    """Execute an INSERT … ON CONFLICT upsert in batches."""
    total = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            psycopg2.extras.execute_batch(cur, sql, chunk, page_size=batch)
            total += len(chunk)
    conn.commit()
    return total
