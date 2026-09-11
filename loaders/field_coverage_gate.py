#!/usr/bin/env python3
"""
loaders/field_coverage_gate.py -- Mission 4, Deliverables D2/D4: the County
Field Coverage measurement layer.

Implements the three measurement behaviors ruled in the Mission 4 brief §4:

  attribute         -- literal all-parcels denominator.
  class_conditional -- an explicit, reproducible population predicate (per
                        capability_registry.FieldDefinition.population_predicate).
  sparse_by_nature  -- structural gate (source column exists + mapped) first;
                        a sanity floor against an external reference ONLY
                        where one is registered; never a fabricated one.

This module is DB-connected (unlike capability_registry.py/capability_state.py,
which are pure Python) -- it follows this repo's existing loader conventions
(loaders/db.py's get_conn()/assert_production_db(), the --county CLI flag
pattern in loaders/backfill_prop_unit_tax_year_geoid.py, the fake-psycopg2
test double convention in loaders/test_reload_county_scope.py).

IMPORTANT -- this mission has no live database access (Mission 4 preamble
item 2, restated from every prior mission this thread). This script is
PREPARED, code-reviewed-ready, and covered by fixture tests using a fake
connection (see loaders/test_field_coverage_gate.py) -- it has never been
run against Travis or Dallas's real data by this agent. Diego runs it for
real; see PX_M4_LIVE_MEASUREMENT_COMMANDS.md for the exact invocations.

schema.sql's confirmed staleness (parcelytics.md §3) means this script
NEVER trusts schema.sql for column existence -- every measurement first
checks information_schema.columns directly (source_column_present), exactly
like verify_index_coverage.py's own live-catalog convention.
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from capability_registry import (
    FIELD_REGISTRY,
    ATTRIBUTE,
    CLASS_CONDITIONAL,
    SPARSE_BY_NATURE,
    get_field_definition,
)
from capability_state import (
    MEASURED,
    NOT_MEASURABLE,
    UNKNOWN_STATUS,
    FAILED_VALIDATION,
    SANITY_FLOOR_PASS,
    SANITY_FLOOR_FAIL,
    SANITY_FLOOR_NOT_APPLICABLE,
    COVERAGE_THRESHOLD,
)

# loaders.db (psycopg2-backed) is imported lazily, inside main(), rather
# than at module level -- every measure_*()/make_record() function above
# is pure-SQL-string-building-plus-a-cursor-argument and has ZERO import-
# time dependency on psycopg2 actually being installed, which is what lets
# loaders/test_field_coverage_gate.py exercise the real measurement logic
# with a fake cursor in an environment that has no psycopg2 (this sandbox,
# same disclosure as every other no-live-DB test this project has built).
# Production (where this script's --live path actually runs) always has
# psycopg2 installed -- this is a test-environment accommodation, not a
# production behavior change.

DEFAULT_COUNTY = "TRAVIS"

# ── D4's sanity-floor registry -----------------------------------------------
# Deliberately EMPTY in shipped code. Per the brief's own instruction ("...
# otherwise report the structural measurement without fabricating an
# external validation threshold"), no field here is given a manufactured
# reference number. A real entry gets added only once a real, external,
# county-published reference value exists and a PM ruling registers it --
# see PX_CAPABILITY_CONTRACT_M4_REPORT.md's [PM ruling needed] section.
# Shape, once populated: {field_name: {"reference_value": float,
# "reference_source": str}}.
SANITY_FLOOR_REFERENCES = {}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def make_record(
    county_code,
    field,
    field_class,
    population_definition,
    population_count,
    populated_count,
    measurement_method,
    measurement_status,
    source_column_present=None,
    source_column_mapped=None,
    sanity_floor=False,
    sanity_floor_status=SANITY_FLOOR_NOT_APPLICABLE,
    reference_value=None,
    reference_source=None,
):
    """Builds the D2-specified coverage record shape (a plain dict --
    matches this codebase's convention elsewhere of not introducing an ORM
    or ad hoc row class for something this simple)."""
    coverage_fraction = None
    if measurement_status == MEASURED and population_count:
        coverage_fraction = populated_count / population_count
    return {
        "county": county_code,
        "field": field,
        "field_class": field_class,
        "population_definition": population_definition,
        "population_count": population_count,
        "populated_count": populated_count,
        "coverage_fraction": coverage_fraction,
        "coverage_percent": None if coverage_fraction is None else round(coverage_fraction * 100, 2),
        "measurement_method": measurement_method,
        "measurement_timestamp": _now(),
        "measurement_status": measurement_status,
        "source_column_present": source_column_present,
        "source_column_mapped": source_column_mapped,
        "sanity_floor": sanity_floor,
        "sanity_floor_status": sanity_floor_status,
        "reference_value": reference_value,
        "reference_source": reference_source,
    }


def check_source_column(cur, table, column):
    """Never trusts schema.sql (confirmed stale, parcelytics.md §3) --
    queries information_schema.columns directly, exactly like
    verify_index_coverage.py's live-catalog convention."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def measure_attribute_field(cur, county_code, field_def):
    """D4: attribute fields use the literal all-parcels denominator --
    populated parcels / all eligible county parcels. No population
    predicate beyond county scoping."""
    table = field_def.table
    column = field_def.canonical_field
    if not check_source_column(cur, table, column):
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition="all county parcels", population_count=None,
            populated_count=None, measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=False,
            source_column_mapped=False,
        )
    cur.execute(
        f"SELECT COUNT(*), "
        f"COUNT(*) FILTER (WHERE NULLIF(TRIM({column}::text), '') IS NOT NULL) "
        f"FROM {table} WHERE county_code = %s",
        (county_code,),
    )
    population_count, populated_count = cur.fetchone()
    if not population_count:
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition="all county parcels", population_count=0,
            populated_count=0, measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=True,
            source_column_mapped=True,
        )
    return make_record(
        county_code, field_def.canonical_field, field_def.field_class,
        population_definition="all county parcels", population_count=population_count,
        populated_count=populated_count, measurement_method=field_def.measurement_method,
        measurement_status=MEASURED, source_column_present=True, source_column_mapped=True,
    )


def measure_class_conditional_field(cur, county_code, field_def):
    """D4: class-conditional fields define the measurable population
    predicate FIRST, then compute populated-within-population /
    population -- the predicate must be explicit and reproducible (it is:
    field_def.population_predicate, a literal SQL fragment stated once in
    capability_registry.py, never re-derived ad hoc per call site)."""
    table = field_def.table
    column = field_def.canonical_field
    predicate = field_def.population_predicate
    if not check_source_column(cur, table, column):
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition=predicate, population_count=None,
            populated_count=None, measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=False,
            source_column_mapped=False,
        )
    is_numeric_sqft = field_def.canonical_field.endswith("_sqft")
    nonempty_expr = f"{column} > 0" if is_numeric_sqft else \
        f"NULLIF(TRIM({column}::text), '') IS NOT NULL"
    cur.execute(
        f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {nonempty_expr}) "
        f"FROM {table} WHERE county_code = %s AND {predicate}",
        (county_code,),
    )
    population_count, populated_count = cur.fetchone()
    if not population_count:
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition=predicate, population_count=0, populated_count=0,
            measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=True,
            source_column_mapped=True,
        )
    return make_record(
        county_code, field_def.canonical_field, field_def.field_class,
        population_definition=predicate, population_count=population_count,
        populated_count=populated_count, measurement_method=field_def.measurement_method,
        measurement_status=MEASURED, source_column_present=True, source_column_mapped=True,
    )


def measure_sparse_by_nature_field(cur, county_code, field_def, tax_year=None,
                                    sanity_floor_references=None):
    """D4: for sparse-by-nature fields, do NOT treat a low all-parcel
    percentage as evidence of an incomplete source. Sequence: (1) confirm
    the source column exists; (2) confirm it was mapped (has at least one
    non-null value ANYWHERE, not just for this county -- distinguishes
    "column exists but this loader never writes to it" from "column exists
    and other counties/years populate it fine"); (3) measure the resulting
    population; (4) where a sanity floor is registered, apply it; otherwise
    report the structural measurement honestly with no invented threshold."""
    sanity_floor_references = sanity_floor_references if sanity_floor_references is not None else SANITY_FLOOR_REFERENCES
    table = field_def.table
    column = field_def.canonical_field

    if not check_source_column(cur, table, column):
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition=field_def.population_predicate,
            population_count=None, populated_count=None,
            measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=False,
            source_column_mapped=False,
        )

    # tax_delinquent is its own table -- a WHOLE-TABLE PRESENCE check, not a
    # per-row nonempty check. FIX (PM review, post-M4): this used to compute
    # coverage_fraction = delinquent_rows / all_parcels, which made the
    # DELINQUENCY RATE drive capability_state() -- backwards. A real, low
    # delinquency rate (a good thing) would fall below COVERAGE_THRESHOLD
    # and read Unavailable, telling users "we don't have delinquency data"
    # when we do. The capability signal for a sparse-by-nature field is
    # whether the DATASET EXISTS for this county, not what fraction of
    # parcels happen to match a rare real-world condition -- exactly the
    # same principle already applied to exemption_codes/hs_cap_loss (a low
    # exemption rate isn't itself evidence of a loading gap, per those
    # fields' own docstrings), just not carried through to this table's
    # different (whole-table, not per-row) shape until now.
    #
    # Fix: report a BOOLEAN-SHAPED measurement -- population_count=1,
    # populated_count=1 if the county has ANY row in tax_delinquent (the
    # dataset was acquired/loaded), else 0. This reuses capability_state()'s
    # existing threshold logic with no special-casing: any nonzero
    # delinquent_rows count, no matter how small relative to the county's
    # total parcels, yields coverage_fraction=1.0 (>= threshold -> "as
    # measured, Available"); zero delinquent_rows yields 0.0 (< threshold
    # -> Unavailable) -- Dallas's real, confirmed "no billing/delinquency
    # data loaded at all" case (parcelytics.md §16).
    if table == "tax_delinquent":
        cur.execute("SELECT COUNT(*) FROM tax_delinquent WHERE county_code = %s", (county_code,))
        (delinquent_rows,) = cur.fetchone()
        dataset_present = delinquent_rows > 0
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition="dataset presence for this county "
                                   "(1 = >=1 delinquent record loaded, 0 = none)",
            population_count=1, populated_count=1 if dataset_present else 0,
            measurement_method=field_def.measurement_method,
            measurement_status=MEASURED, source_column_present=True,
            source_column_mapped=dataset_present,
        )

    # exemption_codes / hs_cap_loss: parcel_tax_year-scoped, per (county, tax_year)
    cur.execute(
        f"SELECT 1 FROM {table} WHERE NULLIF(TRIM({column}::text), '') IS NOT NULL LIMIT 1"
    )
    source_column_mapped = cur.fetchone() is not None

    year_clause = "AND tax_year = %s" if tax_year is not None else ""
    params = (county_code, tax_year) if tax_year is not None else (county_code,)
    cur.execute(
        f"SELECT COUNT(*), "
        f"COUNT(*) FILTER (WHERE NULLIF(TRIM({column}::text), '') IS NOT NULL) "
        f"FROM {table} WHERE county_code = %s {year_clause}",
        params,
    )
    population_count, populated_count = cur.fetchone()

    if not population_count:
        return make_record(
            county_code, field_def.canonical_field, field_def.field_class,
            population_definition=field_def.population_predicate,
            population_count=0, populated_count=0,
            measurement_method=field_def.measurement_method,
            measurement_status=NOT_MEASURABLE, source_column_present=True,
            source_column_mapped=source_column_mapped,
        )

    record = make_record(
        county_code, field_def.canonical_field, field_def.field_class,
        population_definition=field_def.population_predicate,
        population_count=population_count, populated_count=populated_count,
        measurement_method=field_def.measurement_method,
        measurement_status=MEASURED, source_column_present=True,
        source_column_mapped=source_column_mapped,
    )

    ref = sanity_floor_references.get(field_def.canonical_field)
    if ref is not None:
        record["sanity_floor"] = True
        record["reference_value"] = ref["reference_value"]
        record["reference_source"] = ref["reference_source"]
        record["sanity_floor_status"] = (
            SANITY_FLOOR_PASS if record["coverage_fraction"] >= ref["reference_value"]
            else SANITY_FLOOR_FAIL
        )
    else:
        record["sanity_floor"] = False
        record["sanity_floor_status"] = SANITY_FLOOR_NOT_APPLICABLE

    return record


def measure_field(cur, county_code, field_name, tax_year=None, sanity_floor_references=None):
    """Dispatches to the correct measurement behavior by field_class.
    Returns a NOT_MEASURABLE record (never a false zero, per D2) for an
    unregistered field or a field whose class is None (a [PM ruling
    needed] field per capability_registry.py's own convention)."""
    field_def = get_field_definition(field_name)
    if field_def is None or field_def.field_class is None:
        return make_record(
            county_code, field_name, None, population_definition=None,
            population_count=None, populated_count=None,
            measurement_method="unregistered or field_class=None "
                                "(pm_ruling_needed) -- not measurable",
            measurement_status=NOT_MEASURABLE,
        )
    if field_def.field_class == ATTRIBUTE:
        return measure_attribute_field(cur, county_code, field_def)
    if field_def.field_class == CLASS_CONDITIONAL:
        return measure_class_conditional_field(cur, county_code, field_def)
    if field_def.field_class == SPARSE_BY_NATURE:
        return measure_sparse_by_nature_field(
            cur, county_code, field_def, tax_year=tax_year,
            sanity_floor_references=sanity_floor_references,
        )
    # Unreachable given capability_registry.VALID_FIELD_CLASSES, but fail
    # loudly rather than silently rather than guess a zero (D9's "tests
    # must fail loudly" applies to production code paths too).
    raise ValueError(f"field {field_name!r} has unrecognized field_class {field_def.field_class!r}")


def measure_county(cur, county_code, tax_year=None, fields=None, sanity_floor_references=None):
    """Measures every field in `fields` (default: the whole registry) for
    one county. Returns a list of records, one per field -- never raises on
    an individual field's measurement failure; a per-field try/except
    downgrades to FAILED_VALIDATION so one bad field can't blank out an
    entire county's run (mirrors ingest_gate.py's per-bucket-not-all-or-
    nothing philosophy)."""
    fields = fields if fields is not None else sorted(FIELD_REGISTRY.keys())
    records = []
    for field_name in fields:
        try:
            records.append(measure_field(cur, county_code, field_name, tax_year, sanity_floor_references))
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: one
            # field's SQL failing must not abort the whole county's gate run.
            records.append(make_record(
                county_code, field_name,
                (get_field_definition(field_name) or type("_", (), {"field_class": None})()).field_class,
                population_definition=None, population_count=None, populated_count=None,
                measurement_method=f"measurement raised: {exc!r}",
                measurement_status=FAILED_VALIDATION,
            ))
    return records


COVERAGE_UPSERT_SQL = """
INSERT INTO county_field_coverage
    (county_code, field, tax_year, numerator, denominator, fraction,
     measured_at, loader)
VALUES (%(county)s, %(field)s, %(tax_year)s, %(numerator)s, %(denominator)s,
        %(fraction)s, %(measured_at)s, %(loader)s)
ON CONFLICT (county_code, field, tax_year) DO UPDATE SET
    numerator = EXCLUDED.numerator,
    denominator = EXCLUDED.denominator,
    fraction = EXCLUDED.fraction,
    measured_at = EXCLUDED.measured_at,
    loader = EXCLUDED.loader
"""


def write_coverage_records(conn, records, tax_year=None, loader="field_coverage_gate"):
    """Persists MEASURED records to county_field_coverage (schema.sql
    addition -- see D2's schema section). Records with measurement_status
    != MEASURED are NOT written (a NOT_MEASURABLE/UNKNOWN/FAILED_VALIDATION
    result must never silently become a 0.0 row in the table -- that would
    be exactly the "false zero" D2 explicitly forbids); callers still see
    those records in the return value of measure_county(), they are simply
    not persisted as coverage facts."""
    rows = [
        {
            "county": r["county"], "field": r["field"],
            "tax_year": tax_year if tax_year is not None else 0,
            "numerator": r["populated_count"], "denominator": r["population_count"],
            "fraction": r["coverage_fraction"], "measured_at": r["measurement_timestamp"],
            "loader": loader,
        }
        for r in records if r["measurement_status"] == MEASURED
    ]
    written = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(COVERAGE_UPSERT_SQL, row)
            written += 1
    return written


def main():
    from loaders.db import get_conn, assert_production_db  # see note above

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--county", default=DEFAULT_COUNTY,
                         help=f"County code to measure (default: {DEFAULT_COUNTY}).")
    parser.add_argument("--tax-year", type=int, default=None,
                         help="Tax year to scope parcel_tax_year-level fields to "
                              "(required for meaningful exemption_codes/hs_cap_loss "
                              "results; omit for parcel-level-only fields).")
    parser.add_argument("--dry-run", action="store_true", default=True,
                         help="Measure and print only -- do not write to "
                              "county_field_coverage (default).")
    parser.add_argument("--live", action="store_true",
                         help="Actually write measured records to "
                              "county_field_coverage. Requires "
                              "assert_production_db() to pass first.")
    args = parser.parse_args()
    live = args.live

    conn = get_conn()
    if live:
        assert_production_db(conn)  # raises WrongDatabaseError and refuses
                                     # to write if this isn't really production
    with conn.cursor() as cur:
        records = measure_county(cur, args.county, tax_year=args.tax_year)

    print(f"── Field Coverage Measurement: {args.county} "
          f"(tax_year={args.tax_year}) ──")
    for r in records:
        pct = "n/a" if r["coverage_percent"] is None else f"{r['coverage_percent']:.2f}%"
        print(f"  {r['field']:28s} status={r['measurement_status']:18s} "
              f"coverage={pct:>8s}  sanity_floor={r['sanity_floor_status']}")

    if live:
        written = write_coverage_records(conn, records, tax_year=args.tax_year)
        conn.commit()
        print(f"\nWrote {written} measured record(s) to county_field_coverage.")
    else:
        print("\n[dry-run] Nothing written. Re-run with --live to persist "
              "(after assert_production_db() confirms this is really "
              "production).")
    conn.close()


if __name__ == "__main__":
    main()
