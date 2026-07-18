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


def get_conn():
    return psycopg2.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASS,
    )


def execute_schema(conn):
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Schema applied.")


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
