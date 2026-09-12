#!/usr/bin/env python3
"""
loaders/test_field_coverage_gate.py -- Mission 4, Deliverable D9: fixture
tests for the measurement layer (D2/D4).

Uses a fake cursor (same convention as loaders/test_reload_county_scope.py:
a configurable `behavior(sql, params)` callable standing in for a real
Postgres connection, since this sandbox has no live database access, same
disclosure as every other test this project has built). check(name,
condition, detail) / PASS/FAIL printing / FAILURES-list exit code also
matches that file's established convention.

Run: python3 loaders/test_field_coverage_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.field_coverage_gate import (
    measure_attribute_field,
    measure_class_conditional_field,
    measure_sparse_by_nature_field,
    measure_field,
    measure_county,
    check_source_column,
)
from capability_registry import FIELD_REGISTRY, get_field_definition
from capability_state import (
    MEASURED, NOT_MEASURABLE, UNKNOWN_STATUS, FAILED_VALIDATION,
    SANITY_FLOOR_PASS, SANITY_FLOOR_FAIL, SANITY_FLOOR_NOT_APPLICABLE,
    capability_state, should_render, county_shows_field, county_has_field,
    AVAILABLE, PARTIAL, UNAVAILABLE, UNKNOWN, COVERAGE_THRESHOLD,
)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  --  {detail}")
        FAILURES.append(name)


class _FakeCursor:
    """`behavior` is a callable (normalized_sql: str, params: tuple) ->
    a fetchone()-shaped tuple, or a list (meaning: information_schema
    existence check, so bool(list) drives fetchone()'s None/row shape),
    or an Exception instance (raised)."""

    def __init__(self, behavior):
        self.behavior = behavior

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        result = self.behavior(normalized, params or ())
        if isinstance(result, Exception):
            raise result
        self._result = result

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_cursor(behavior):
    return _FakeCursor(behavior)


# ── 1. Attribute field: literal all-parcels denominator ────────────────────
def test_measure_attribute_field_basic():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)  # column exists
        if "COUNT(*)" in sql and "FILTER" in sql:
            return (1000, 950)  # population, populated
        raise AssertionError(f"unexpected SQL: {sql}")

    field_def = get_field_definition("situs_address")
    record = measure_attribute_field(make_cursor(behavior), "TRAVIS", field_def)
    check("attribute field measured", record["measurement_status"] == MEASURED, record)
    check("attribute field coverage_fraction = 0.95",
          abs(record["coverage_fraction"] - 0.95) < 1e-9, record)
    check("attribute field population_definition is all-parcels (no predicate)",
          record["population_definition"] == "all county parcels", record)


# ── 2. Class-conditional field: explicit population predicate ──────────────
def test_measure_class_conditional_field_basic():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "prop_type_cd = 'R'" in sql:
            return (500, 400)
        raise AssertionError(f"unexpected SQL: {sql}")

    field_def = get_field_definition("classi_cd")
    record = measure_class_conditional_field(make_cursor(behavior), "TRAVIS", field_def)
    check("class_conditional field measured", record["measurement_status"] == MEASURED, record)
    check("class_conditional coverage_fraction = 0.8",
          abs(record["coverage_fraction"] - 0.8) < 1e-9, record)
    check("class_conditional population_definition states the real predicate",
          record["population_definition"] == "prop_type_cd = 'R'", record)


# ── 3 / 11. Sparse-by-nature: structural gate catches a missing source column ──
def test_sparse_field_missing_source_column():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            return None  # column does NOT exist
        raise AssertionError("should not query further once column check fails")

    field_def = get_field_definition("exemption_codes")
    record = measure_sparse_by_nature_field(make_cursor(behavior), "DALLAS", field_def, tax_year=2026)
    check("missing source column -> NOT_MEASURABLE, not a false zero",
          record["measurement_status"] == NOT_MEASURABLE, record)
    check("missing source column -> source_column_present is False",
          record["source_column_present"] is False, record)
    check("missing source column -> coverage_fraction is None (never 0.0)",
          record["coverage_fraction"] is None, record)


# ── 4/5. Sanity floor pass / fail (D4's optional external-reference check) ──
def test_sanity_floor_pass_and_fail():
    def behavior_high_coverage(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "LIMIT 1" in sql:
            return (1,)  # source_column_mapped = True
        if "COUNT(*)" in sql:
            return (1000, 600)  # 60% coverage
        raise AssertionError(sql)

    field_def = get_field_definition("hs_cap_loss")
    # Synthetic, TEST-ONLY reference (0.50) -- proves the mechanism only;
    # no real external CAD-published reference is claimed here or in
    # production code (SANITY_FLOOR_REFERENCES ships empty -- see that
    # module's own docstring).
    refs = {"hs_cap_loss": {"reference_value": 0.50, "reference_source": "TEST-ONLY synthetic reference"}}
    record = measure_sparse_by_nature_field(
        make_cursor(behavior_high_coverage), "TRAVIS", field_def, tax_year=2023,
        sanity_floor_references=refs,
    )
    check("sanity floor PASS when measured fraction >= reference",
          record["sanity_floor_status"] == SANITY_FLOOR_PASS, record)

    def behavior_low_coverage(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "LIMIT 1" in sql:
            return (1,)
        if "COUNT(*)" in sql:
            return (1000, 100)  # 10% coverage, below the 0.50 reference
        raise AssertionError(sql)

    record2 = measure_sparse_by_nature_field(
        make_cursor(behavior_low_coverage), "TRAVIS", field_def, tax_year=2025,
        sanity_floor_references=refs,
    )
    check("sanity floor FAIL when measured fraction < reference",
          record2["sanity_floor_status"] == SANITY_FLOOR_FAIL, record2)
    check("sanity floor fail still reports the structural fraction honestly (not suppressed)",
          record2["coverage_fraction"] == 0.1, record2)


# ── PM review fix: tax_delinquent presence, not delinquency RATE, drives
#    capability. A low-but-nonzero delinquency count is the exact regression
#    this fix prevents -- before the fix, this would have computed
#    coverage_fraction = 12/50000 = 0.00024, well below COVERAGE_THRESHOLD,
#    and read Unavailable even though the dataset is fully present.
def test_tax_delinquent_low_but_nonzero_rate_still_available():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            # Live-validation regression (PM review): the real tax_delinquent
            # table has NO column literally named "tax_delinquent" (its real
            # columns are delinquent_total, current_year_total, total_due,
            # etc, confirmed via a live `\d tax_delinquent`). If this branch
            # is ever hit, measure_sparse_by_nature_field()'s tax_delinquent
            # case has regressed back to routing through the generic
            # single-column check_source_column() gate, which always fails
            # for this table -- fail loud rather than silently answering
            # "yes" the way this fixture used to (that's exactly what made
            # the original bug invisible to fixture testing).
            raise AssertionError(
                "tax_delinquent measurement must not query "
                "information_schema.columns -- it has no single named "
                "column to look up; see test_tax_delinquent_measures_via_"
                "table_presence_not_column_lookup for the dedicated proof"
            )
        if "COUNT(*) FROM tax_delinquent" in sql:
            return (12,)  # only 12 delinquent parcels -- a low, GOOD rate
        raise AssertionError(f"unexpected SQL for tax_delinquent presence check: {sql}")

    field_def = get_field_definition("tax_delinquent")
    record = measure_sparse_by_nature_field(make_cursor(behavior), "TRAVIS", field_def)
    check("tax_delinquent: dataset present (12 rows) -> MEASURED, not a rate",
          record["measurement_status"] == MEASURED, record)
    check("tax_delinquent: coverage_fraction is 1.0 (presence), not 12/all_parcels",
          record["coverage_fraction"] == 1.0, record)
    state = capability_state(record["measurement_status"], record["coverage_fraction"])
    check("tax_delinquent: a low-but-nonzero delinquency count still reads Available "
          "-- a real, good, low delinquency rate must never be mistaken for missing data",
          state == AVAILABLE, (record, state))


def test_tax_delinquent_zero_rows_reads_unavailable():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            # See the matching comment in
            # test_tax_delinquent_low_but_nonzero_rate_still_available --
            # this must never be queried for tax_delinquent.
            raise AssertionError(
                "tax_delinquent measurement must not query "
                "information_schema.columns"
            )
        if "COUNT(*) FROM tax_delinquent" in sql:
            return (0,)  # Dallas today: no billing/delinquency data loaded at all
        raise AssertionError(sql)

    field_def = get_field_definition("tax_delinquent")
    record = measure_sparse_by_nature_field(make_cursor(behavior), "DALLAS", field_def)
    check("tax_delinquent: zero rows for the county -> coverage_fraction 0.0",
          record["coverage_fraction"] == 0.0, record)
    state = capability_state(record["measurement_status"], record["coverage_fraction"])
    check("tax_delinquent: zero rows -> Unavailable (dataset genuinely not loaded, Dallas today)",
          state == UNAVAILABLE, (record, state))


# ── Live-validation fix: tax_delinquent must measure via whole-table
#    presence, NEVER via check_source_column()'s single-column
#    information_schema.columns lookup. Production's real tax_delinquent
#    table has no column literally named "tax_delinquent" -- confirmed via
#    a live `\d tax_delinquent` during Diego's live-validation pass (real
#    columns: delinquent_total, current_year_total, total_due, etc). Before
#    this fix, check_source_column(table="tax_delinquent",
#    column="tax_delinquent") ALWAYS returned False in production, so the
#    structural gate ALWAYS short-circuited to NOT_MEASURABLE and the
#    presence-check code (added in the earlier post-M4-review fix) was
#    dead, unreachable code -- invisible to the fixture tests above only
#    because their fixtures always answered "yes, the column exists" to
#    ANY information_schema.columns query, which is not what real
#    production's catalog says for this table. This test's fixture instead
#    fails loudly if that query is ever issued for tax_delinquent, so a
#    regression back to routing through check_source_column() is caught
#    immediately rather than silently passing.
def test_tax_delinquent_measures_via_table_presence_not_column_lookup():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            raise AssertionError(
                "tax_delinquent has no column named 'tax_delinquent' in "
                "real production (its real columns are delinquent_total, "
                "current_year_total, total_due, etc) -- "
                "measure_sparse_by_nature_field()'s tax_delinquent branch "
                "must run its own table-presence check BEFORE and INSTEAD "
                "OF check_source_column(), never query "
                "information_schema.columns for it at all"
            )
        if "COUNT(*) FROM tax_delinquent" in sql:
            return (37,)  # any nonzero count -- dataset is present
        raise AssertionError(f"unexpected SQL: {sql}")

    field_def = get_field_definition("tax_delinquent")
    record = measure_sparse_by_nature_field(make_cursor(behavior), "TRAVIS", field_def)
    check("tax_delinquent: measures MEASURED via table-presence query alone, "
          "no column-existence lookup needed or performed",
          record["measurement_status"] == MEASURED, record)
    check("tax_delinquent: presence-based coverage_fraction is 1.0",
          record["coverage_fraction"] == 1.0, record)
    state = capability_state(record["measurement_status"], record["coverage_fraction"])
    check("tax_delinquent: reads Available once the structural gate no "
          "longer depends on a nonexistent column name",
          state == AVAILABLE, (record, state))


# ── 10. Zero-population edge case ───────────────────────────────────────────
def test_zero_population_no_crash():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "COUNT(*)" in sql and "FILTER" in sql:
            return (0, 0)  # zero population -- must not divide by zero
        raise AssertionError(sql)

    field_def = get_field_definition("owner_name")
    record = measure_attribute_field(make_cursor(behavior), "HARRIS", field_def)
    check("zero population -> NOT_MEASURABLE, no ZeroDivisionError",
          record["measurement_status"] == NOT_MEASURABLE, record)
    check("zero population -> coverage_fraction is None, never 0.0",
          record["coverage_fraction"] is None, record)


# ── measure_field() dispatch + unregistered-field honesty ──────────────────
def test_measure_field_unregistered_is_not_measurable():
    record = measure_field(make_cursor(lambda *a: (0,)), "TRAVIS", "no_such_field")
    check("unregistered field -> NOT_MEASURABLE, never a false zero",
          record["measurement_status"] == NOT_MEASURABLE, record)


def test_measure_county_isolates_per_field_failures():
    def behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        raise RuntimeError("boom -- simulated per-field measurement failure")

    records = measure_county(make_cursor(behavior), "TRAVIS", tax_year=2026,
                              fields=["situs_address", "owner_name"])
    check("one field's exception downgrades to FAILED_VALIDATION, doesn't abort the county run",
          len(records) == 2 and all(r["measurement_status"] == FAILED_VALIDATION for r in records),
          records)


# ── 6/7/8/9. Capability-state decision (D3) -- the four states ─────────────
def test_capability_state_available():
    state = capability_state(MEASURED, 0.95)
    check("coverage well above threshold -> Available", state == AVAILABLE, state)


def test_capability_state_unavailable():
    state = capability_state(MEASURED, 0.10)  # below 0.30 COVERAGE_THRESHOLD
    check("coverage below threshold -> Unavailable", state == UNAVAILABLE, state)


def test_capability_state_unknown():
    state = capability_state(NOT_MEASURABLE, None)
    check("not_measurable -> Unknown", state == UNKNOWN, state)
    state2 = capability_state(UNKNOWN_STATUS, 0.99)
    check("unknown measurement_status -> Unknown even with a coverage number present",
          state2 == UNKNOWN, state2)


def test_capability_state_partial():
    state = capability_state(MEASURED, 0.60, sanity_floor_status=SANITY_FLOOR_FAIL)
    check("coverage above threshold BUT sanity floor failed -> Partial", state == PARTIAL, state)


def test_should_render_and_threshold_is_030_not_050():
    check("COVERAGE_THRESHOLD is 0.30 (parcelytics.md §7), not 0.50",
          COVERAGE_THRESHOLD == 0.30, COVERAGE_THRESHOLD)
    check("Available renders", should_render(AVAILABLE) is True)
    check("Partial renders", should_render(PARTIAL) is True)
    check("Unavailable does not render", should_render(UNAVAILABLE) is False)
    check("Unknown does not render", should_render(UNKNOWN) is False)


# ── county_shows_field()/county_has_field(): snapshot + legacy fallback ─────
def test_county_shows_field_uses_snapshot_when_present():
    snapshot = {
        ("DALLAS", "classi_cd"): {
            "measurement_status": MEASURED, "coverage_fraction": 0.0,
            "sanity_floor_status": SANITY_FLOOR_NOT_APPLICABLE,
        },
    }
    result = county_shows_field("DALLAS", "classi_cd", snapshot=snapshot,
                                 legacy_field_coverage={"classi_cd": True})
    check("measured 0% coverage overrides a stale legacy True -- capability, not mere existence",
          result is False, result)


def test_county_shows_field_falls_back_to_legacy_when_unmeasured():
    result = county_shows_field("DALLAS", "exemption_codes", snapshot={},
                                 legacy_field_coverage={"exemption_codes": False})
    check("no measurement yet -> falls back to the legacy hand-declared boolean",
          result is False, result)
    result2 = county_shows_field("TRAVIS", "exemption_codes", snapshot={},
                                  legacy_field_coverage={"exemption_codes": True})
    check("legacy fallback also correctly returns True when the old boolean was True",
          result2 is True, result2)


def test_county_has_field_wrapper_matches_county_shows_field():
    snapshot = {("TRAVIS", "classi_cd"): {
        "measurement_status": MEASURED, "coverage_fraction": 0.95,
        "sanity_floor_status": SANITY_FLOOR_NOT_APPLICABLE,
    }}
    a = county_has_field("TRAVIS", "classi_cd", snapshot=snapshot)
    b = county_shows_field("TRAVIS", "classi_cd", snapshot=snapshot)
    check("county_has_field() is a pure wrapper -- identical result to county_shows_field()",
          a == b == True, (a, b))


# ── 12. Travis/Dallas regression: the system must not only work because
#        Travis happens to have a particular schema/coverage profile ───────
#
# Mission 5 (PX-20260911, Dallas Stage B Task 0) REVISION #1: this test
# originally asserted Dallas classi_cd measures Unavailable via a 0-of-
# 769,536 result from evaluating Travis's own "prop_type_cd = 'R'" literal
# against Dallas data -- confirmed (ISS-0910-06, live) to be a bug: a Travis
# literal silently, wrongly evaluated against a county it was never
# validated for. That revision changed the expectation to NOT_MEASURABLE ->
# Unknown, pending live validation of a real Dallas predicate.
#
# REVISION #2 (PX-20260911-01): the PM ruling is now in, live-validated
# 2026-09-11: Dallas's certified class-conditional predicate is
# `state_cd1 = 'A'` (617,446 rows). `capability_registry.py` now carries a
# "DALLAS" key for classi_cd (and the other three class-conditional
# fields). This test's expectation changes AGAIN, in the direction the
# mission always intended: Dallas classi_cd must now resolve a REAL
# predicate and produce a real MEASURED result -- Unknown was only ever the
# honest placeholder for "not yet certified," never the target end state.
# The "must never issue SQL for Dallas" assertion from REVISION #1 is
# retired for this field specifically (a certified predicate means SQL
# SHOULD now run) -- see test_dallas_year_built_stays_unavailable_after_
# predicate_certification() below for the field that remains gated by a
# DIFFERENT mechanism (a missing source mapping, not an uncertified
# predicate) even after this change.
def test_travis_dallas_classi_cd_regression():
    def travis_behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "prop_type_cd = 'R'" in sql:
            return (500000, 495000)  # Travis: classi_cd well populated
        raise AssertionError(sql)

    def dallas_behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "state_cd1 = 'A'" in sql:
            # PM-supplied, live-validated 2026-09-11: 617,446 rows in the
            # certified Dallas population. classi_cd itself remains
            # genuinely unmapped for Dallas (Workstream C finding) --
            # 0 populated, a real, honest 0% result now, not a blocked
            # measurement.
            return (617446, 0)
        raise AssertionError(
            "unexpected SQL for Dallas classi_cd post-certification -- "
            "the certified predicate is state_cd1 = 'A'; any other "
            "predicate string reaching this point is a regression: " + sql
        )

    field_def = get_field_definition("classi_cd")
    travis_record = measure_class_conditional_field(make_cursor(travis_behavior), "TRAVIS", field_def)
    dallas_record = measure_class_conditional_field(make_cursor(dallas_behavior), "DALLAS", field_def)

    travis_state = capability_state(travis_record["measurement_status"], travis_record["coverage_fraction"])
    dallas_state = capability_state(dallas_record["measurement_status"], dallas_record["coverage_fraction"])

    check("Travis classi_cd measures Available (99% coverage)", travis_state == AVAILABLE, travis_record)
    check("Dallas classi_cd now resolves the certified state_cd1 = 'A' predicate "
          "(population_definition reflects it, not a 'NOT CERTIFIED' placeholder)",
          dallas_record["population_definition"] == "state_cd1 = 'A'", dallas_record)
    check("Dallas classi_cd measures MEASURED (a real query ran against the "
          "certified population), not NOT_MEASURABLE",
          dallas_record["measurement_status"] == "measured", dallas_record)
    check("Dallas classi_cd measures Unavailable (0% coverage within the "
          "certified population) -- classi_cd itself is still genuinely "
          "unmapped for Dallas (Workstream C), a separate, real gap from "
          "the predicate question this mission certified",
          dallas_state == UNAVAILABLE, dallas_record)
    check("Travis and Dallas now both resolve real, county-specific "
          "predicates and produce genuinely different coverage results from "
          "genuinely different data -- not from a county-name conditional "
          "in the measurement code",
          travis_state != dallas_state, (travis_state, dallas_state))


# ── 13. year_built must NOT appear silently "fixed" by certifying the
#         predicate alone -- PX-20260911-01 Task 1's explicit instruction.
#         A certified predicate only gets measure_class_conditional_field()
#         past the predicate gate; check_source_column() then still finds
#         the `year_built` COLUMN exists on `parcel` (Travis has real data
#         in it), so the query runs -- but every Dallas row's `year_built`
#         is NULL (the field is separately unmapped, per Workstream C), so
#         the real, honest result is MEASURED / 0% / Unavailable, not
#         Available and not NOT_MEASURABLE. ───────────────────────────────
def test_dallas_year_built_stays_unavailable_after_predicate_certification():
    def dallas_behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)  # the year_built COLUMN exists on parcel (schema-level)
        if "state_cd1 = 'A'" in sql:
            # Real column, zero non-null values for Dallas -- genuinely
            # unmapped, not a blocked measurement.
            return (617446, 0)
        raise AssertionError(sql)

    field_def = get_field_definition("year_built")
    record = measure_class_conditional_field(make_cursor(dallas_behavior), "DALLAS", field_def)
    state = capability_state(record["measurement_status"], record["coverage_fraction"])

    check("Dallas year_built resolves the certified predicate (proves the "
          "predicate fix alone does not block measurement)",
          record["population_definition"] == "state_cd1 = 'A'", record)
    check("Dallas year_built measures MEASURED, not NOT_MEASURABLE -- the "
          "predicate is certified and the column exists; it is not blocked",
          record["measurement_status"] == "measured", record)
    check("Dallas year_built capability_state is Unavailable, NOT Available -- "
          "a certified predicate must never be mistaken for a mapped field; "
          "this field is still 0% populated for Dallas regardless of "
          "predicate certification (a separate, real, unresolved gap)",
          state == UNAVAILABLE, record)


# ── 14. living_area_sqft / gross_building_area_sqft also received the
#         same certified DALLAS predicate in this round -- one shared
#         assertion confirming neither silently reverted to the
#         "not certified" placeholder. ─────────────────────────────────
def test_dallas_sqft_fields_also_certified():
    def dallas_behavior(sql, params):
        if "information_schema.columns" in sql:
            return (1,)
        if "state_cd1 = 'A'" in sql:
            return (617446, 0)
        raise AssertionError(sql)

    for name in ("living_area_sqft", "gross_building_area_sqft"):
        field_def = get_field_definition(name)
        record = measure_class_conditional_field(make_cursor(dallas_behavior), "DALLAS", field_def)
        check(f"Dallas {name} resolves the certified state_cd1 = 'A' predicate",
              record["population_definition"] == "state_cd1 = 'A'", record)
        check(f"Dallas {name} measures MEASURED (not blocked by an "
              f"uncertified predicate)",
              record["measurement_status"] == "measured", record)


ALL_TESTS = [
    test_measure_attribute_field_basic,
    test_measure_class_conditional_field_basic,
    test_sparse_field_missing_source_column,
    test_sanity_floor_pass_and_fail,
    test_tax_delinquent_low_but_nonzero_rate_still_available,
    test_tax_delinquent_zero_rows_reads_unavailable,
    test_tax_delinquent_measures_via_table_presence_not_column_lookup,
    test_zero_population_no_crash,
    test_measure_field_unregistered_is_not_measurable,
    test_measure_county_isolates_per_field_failures,
    test_capability_state_available,
    test_capability_state_unavailable,
    test_capability_state_unknown,
    test_capability_state_partial,
    test_should_render_and_threshold_is_030_not_050,
    test_county_shows_field_uses_snapshot_when_present,
    test_county_shows_field_falls_back_to_legacy_when_unmeasured,
    test_county_has_field_wrapper_matches_county_shows_field,
    test_travis_dallas_classi_cd_regression,
    test_dallas_year_built_stays_unavailable_after_predicate_certification,
    test_dallas_sqft_fields_also_certified,
]


def main():
    print("── field_coverage_gate.py / capability_state.py fixture tests (D9) ──")
    for t in ALL_TESTS:
        t()
    print(f"\nTotals: {len(ALL_TESTS)} test functions, {len(FAILURES)} FAIL")
    if FAILURES:
        print("FAILED:", ", ".join(FAILURES))
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
