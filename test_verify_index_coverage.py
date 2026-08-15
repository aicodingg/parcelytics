"""
test_verify_index_coverage.py — fixture tests for POST-PARTITION-INCIDENT-1-AUDIT's
verify_index_coverage.py.

Per the brief's explicit verification requirement: "Real fixture tests for
the SQL-extraction and column-matching logic -- prove it correctly
identifies a real, known-uncovered pattern (e.g. reproduce tonight's actual
incident as a fixture case) and correctly does NOT flag a real, known-covered
pattern."

Covers, in order:
  1. The incident itself, reproduced exactly: parcel_tax_year filtered by
     geo_id alone, BEFORE vs AFTER tonight's reactive hotfix index existed.
  2. A regression test for a real bug found and fixed WHILE building this
     tool tonight: the predicate regex's trailing \\b silently failed to
     match "col = %s" (any equality followed by whitespace) -- meaning the
     tool's own first real run silently missed almost every '='-based
     predicate in the codebase. This must never regress silently again.
  3. Extraction-stage unit tests: literal / f-string-with-dynamic-fragment /
     concatenation / same-file-variable resolution.
  4. Parsing-stage unit tests: alias resolution, JOIN...ON both-sides
     extraction, top-level AND splitting, point-lookup vs range-operator
     classification, unaliased-single-table fallback.
  5. Leading-prefix / best_index_match semantics (k=0 exact-incident-shape
     vs partial vs full).
  6. The stale-PK "UNCONFIRMED" classification added after discovering that
     schema.sql's stale inline PK text produces false "COVERED" verdicts for
     prop_unit/tax_billing/etc. in offline mode.

Run: python3 -m pytest test_verify_index_coverage.py -v
     (or plain python3 test_verify_index_coverage.py -- falls back to a
     manual runner if pytest isn't available; confirmed via this file's
     own __main__ block, since this sandbox has no confirmed pip access).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_index_coverage as vic


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE INCIDENT ITSELF, REPRODUCED
# ═══════════════════════════════════════════════════════════════════════════

def test_incident_reproduction_no_coverage_pre_hotfix():
    """Tonight's real incident, reproduced exactly: property_detail() ran
    `SELECT * FROM parcel_tax_year WHERE geo_id = %s AND tax_year = %s`
    (app.py's real, actual shape -- geo_id alone is the point-lookup key
    the app cares about; tax_year is a second equality filter). Before the
    migration, parcel_tax_year's PK was (geo_id, tax_year) -- fine. AFTER
    migrate_county_partitioning.py's real migration, the PK became
    (county_code, geo_id, tax_year) -- county_code leading. A query that
    never filters on county_code gets ZERO benefit from that PK (k=0),
    which is exactly what caused the real production timeout. This fixture
    supplies ONLY the post-migration PK as the table's index list (no
    reactive hotfix index yet) and asserts the tool correctly reports
    NO_COVERAGE."""
    sql = "SELECT * FROM parcel_tax_year WHERE geo_id = %s AND tax_year = %s"
    shape = vic.parse_sql_shape(sql)
    assert shape.filter_columns == {"parcel_tax_year": {"geo_id", "tax_year"}}

    table_indexes = {
        "parcel_tax_year": [
            ("parcel_tax_year_pkey", ["county_code", "geo_id", "tax_year"]),
        ]
    }
    k, name, cols = vic.best_index_match(shape.filter_columns["parcel_tax_year"], table_indexes["parcel_tax_year"])
    assert k == 0, f"expected k=0 (the incident's exact failure shape), got k={k} via {name}"


def test_incident_reproduction_covered_post_hotfix():
    """Same query, same table -- but now with tonight's REAL reactive hotfix
    index present (idx_pty_geo_id ON parcel_tax_year(geo_id, tax_year), the
    actual shape of the 4 indexes added directly to production per commit
    3ccfc44). Must now report COVERED (k>=1)."""
    sql = "SELECT * FROM parcel_tax_year WHERE geo_id = %s AND tax_year = %s"
    shape = vic.parse_sql_shape(sql)

    table_indexes = {
        "parcel_tax_year": [
            ("parcel_tax_year_pkey", ["county_code", "geo_id", "tax_year"]),
            ("idx_pty_geo_id", ["geo_id", "tax_year"]),
        ]
    }
    k, name, cols = vic.best_index_match(shape.filter_columns["parcel_tax_year"], table_indexes["parcel_tax_year"])
    assert k == 2, f"expected k=2 (full match against the real hotfix index), got k={k}"
    assert name == "idx_pty_geo_id"


def test_incident_reproduction_end_to_end_via_audit_extracted():
    """Same fixture, but driven through the real audit_extracted() orchestration
    (not just the two helper functions in isolation), using a fake ExtractedSQL
    so this also exercises Finding/status assembly end to end."""
    item = vic.ExtractedSQL(
        filepath="app.py", lineno=9999, call_name="query",
        sql_text="SELECT * FROM parcel_tax_year WHERE geo_id = %s AND tax_year = %s",
        resolution="literal",
    )
    pre_hotfix = {"parcel_tax_year": [("parcel_tax_year_pkey", ["county_code", "geo_id", "tax_year"])]}
    findings, notes = vic.audit_extracted([item], pre_hotfix)
    assert len(findings) == 1
    assert findings[0].status == "NO_COVERAGE"

    post_hotfix = {"parcel_tax_year": [
        ("parcel_tax_year_pkey", ["county_code", "geo_id", "tax_year"]),
        ("idx_pty_geo_id", ["geo_id", "tax_year"]),
    ]}
    findings, notes = vic.audit_extracted([item], post_hotfix)
    assert len(findings) == 1
    assert findings[0].status == "COVERED"


# ═══════════════════════════════════════════════════════════════════════════
# 2. REGRESSION TEST for the real predicate-regex bug found tonight
# ═══════════════════════════════════════════════════════════════════════════

def test_predicate_regex_matches_equality_with_surrounding_whitespace():
    """Real bug, found and fixed while building this tool: the original
    _PREDICATE_RE had a trailing \\b applied to the WHOLE operator
    alternation group. \\b requires a word-char/non-word-char transition;
    for a symbol operator like '=' followed by whitespace (or '%', or a
    quote), BOTH sides of that boundary are non-word characters, so \\b
    never matched -- meaning _extract_predicate_refs("geo_id = %s") silently
    returned [] instead of the real (None, "geo_id", True) ref. This broke
    nearly every '='-based predicate in the ENTIRE codebase, including the
    incident's own exact shape (WHERE geo_id = %s). Must never regress."""
    cases_that_must_match = [
        ("prop_id = %s", "prop_id", None, "="),
        ("geo_id = %s", "geo_id", None, "="),
        ("geo_id=%s", "geo_id", None, "="),
        ("tax_year = 2025", "tax_year", None, "="),
        ("market_value > 0", "market_value", None, ">"),
        ("pty.geo_id = p.geo_id", "pty", "geo_id", "="),
        ("col != 5", "col", None, "!="),
        ("col <> 5", "col", None, "<>"),
        ("col <= 5", "col", None, "<="),
        ("entity_code IN (1,2)", "entity_code", None, "IN"),
    ]
    for text, ident1, ident2, op in cases_that_must_match:
        m = vic._PREDICATE_RE.match(text)
        assert m is not None, f"_PREDICATE_RE failed to match {text!r} (this is the exact bug)"
        assert m.group(1).lower() == ident1
        assert (m.group(2) or None) == ident2
        assert m.group(3).upper().replace("  ", " ") == op or op in m.group(3).upper()


def test_extract_predicate_refs_returns_nonempty_for_equality():
    """Direct regression test at the level the original bug report was
    discovered: this line's real filter_columns went from {} to
    {'prop_unit': {'prop_id'}} once the regex was fixed."""
    refs = vic._extract_predicate_refs("prop_id = %s")
    assert refs == [(None, "prop_id", True)], f"got {refs}"

    refs = vic._extract_predicate_refs("market_value > 0")
    assert refs == [(None, "market_value", False)], f"got {refs}"

    refs = vic._extract_predicate_refs("pty.geo_id = p.geo_id")
    assert ("pty", "geo_id", True) in refs
    assert ("p", "geo_id", True) in refs


def test_single_table_unaliased_where_resolves_to_that_table():
    """app.py:1630's real shape: SELECT geo_id FROM prop_unit WHERE prop_id
    = %s -- no alias anywhere. Confirms the single-table fallback correctly
    attributes the bare column to the only table in scope."""
    shape = vic.parse_sql_shape("SELECT geo_id FROM prop_unit WHERE prop_id = %s")
    assert shape.filter_columns == {"prop_unit": {"prop_id"}}


# ═══════════════════════════════════════════════════════════════════════════
# 3. EXTRACTION STAGE
# ═══════════════════════════════════════════════════════════════════════════

def _extract(src):
    return vic.extract_calls_from_source(src, "fixture.py")


def test_extraction_plain_literal():
    src = 'row = query("SELECT * FROM parcel WHERE geo_id = %s", (g,))'
    calls = _extract(src)
    assert len(calls) == 1
    assert calls[0].resolution == "literal"
    assert "FROM parcel WHERE geo_id = %s" in calls[0].sql_text


def test_extraction_fstring_keeps_dynamic_fragment_marker():
    src = 'rows = query(f"SELECT * FROM parcel WHERE {where_sql}")'
    calls = _extract(src)
    assert len(calls) == 1
    assert "{where_sql}" in calls[0].sql_text


def test_extraction_concatenation():
    src = 'rows = query("SELECT * FROM parcel " + "WHERE geo_id = %s", (g,))'
    calls = _extract(src)
    assert len(calls) == 1
    assert "FROM parcel " in calls[0].sql_text and "WHERE geo_id = %s" in calls[0].sql_text


def test_extraction_same_file_variable_resolution():
    src = (
        'SQL = "SELECT * FROM parcel WHERE geo_id = %s"\n'
        'def f():\n'
        '    return query(SQL, (g,))\n'
    )
    calls = _extract(src)
    assert len(calls) == 1
    assert calls[0].resolution == "variable"
    assert "FROM parcel WHERE geo_id = %s" in calls[0].sql_text


def test_extraction_cur_execute_method_call():
    src = 'cur.execute("UPDATE prop_unit_tax_year SET x=1 WHERE prop_id = %s AND tax_year = %s", (p, y))'
    calls = _extract(src)
    assert len(calls) == 1
    assert calls[0].call_name == "cur.execute"


# ═══════════════════════════════════════════════════════════════════════════
# 4. PARSING STAGE
# ═══════════════════════════════════════════════════════════════════════════

def test_join_on_extracts_both_sides():
    sql = """
        SELECT * FROM parcel p
        JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = %(tax_year)s
        WHERE p.county_code = %(county)s
    """
    shape = vic.parse_sql_shape(sql)
    assert "geo_id" in shape.filter_columns.get("parcel_metrics", set())
    assert "geo_id" in shape.filter_columns.get("parcel", set())
    assert "tax_year" in shape.filter_columns.get("parcel_metrics", set())
    assert "county_code" in shape.filter_columns.get("parcel", set())


def test_range_predicate_goes_to_informational_not_filter():
    sql = "SELECT * FROM parcel_tax_year WHERE market_value > 0 AND geo_id = %s"
    shape = vic.parse_sql_shape(sql)
    assert shape.filter_columns["parcel_tax_year"] == {"geo_id"}
    assert shape.informational_columns["parcel_tax_year"] == {"market_value"}


def test_top_level_and_split_respects_parens():
    sql = "SELECT * FROM t WHERE (a = 1 AND b = 2) AND c = %s"
    where_text = " (a = 1 AND b = 2) AND c = %s"
    parts = vic._split_top_level_and(where_text)
    # top-level split must NOT break inside the parens
    assert len(parts) == 2
    assert parts[0].strip().startswith("(a = 1 AND b = 2")
    assert parts[1].strip() == "c = %s"


def test_entirely_dynamic_where_clause_flagged_not_silently_dropped():
    sql = "SELECT * FROM parcel WHERE {where_sql}"
    shape = vic.parse_sql_shape(sql)
    assert shape.filter_columns == {}
    assert any("entirely dynamic" in note for note in shape.unresolved_clauses)


def test_update_and_delete_statements_resolve_table_without_alias():
    shape = vic.parse_sql_shape("UPDATE prop_unit_tax_year SET x=1 WHERE prop_id = %s AND tax_year = %s")
    assert shape.filter_columns == {"prop_unit_tax_year": {"prop_id", "tax_year"}}

    shape = vic.parse_sql_shape("DELETE FROM tax_billing_quarantine WHERE geo_id = %s")
    assert shape.filter_columns == {"tax_billing_quarantine": {"geo_id"}}


# ═══════════════════════════════════════════════════════════════════════════
# 5. LEADING-PREFIX / best_index_match SEMANTICS
# ═══════════════════════════════════════════════════════════════════════════

def test_leading_prefix_length_zero_when_first_column_unfiltered():
    # exact incident shape: filtering geo_id alone against a
    # (county_code, geo_id, tax_year) index -- county_code (the FIRST
    # column) is not filtered, so k must be 0, not "partial credit" for
    # matching geo_id/tax_year later in the index.
    k = vic.leading_prefix_length({"geo_id", "tax_year"}, ["county_code", "geo_id", "tax_year"])
    assert k == 0


def test_leading_prefix_length_full_match():
    k = vic.leading_prefix_length({"geo_id", "tax_year"}, ["geo_id", "tax_year"])
    assert k == 2


def test_best_index_match_picks_longest_real_prefix():
    indexes = [
        ("idx_a", ["tax_year"]),
        ("idx_b", ["geo_id", "tax_year"]),
        ("pkey", ["county_code", "geo_id", "tax_year"]),
    ]
    k, name, cols = vic.best_index_match({"geo_id", "tax_year"}, indexes)
    assert k == 2 and name == "idx_b"


# ═══════════════════════════════════════════════════════════════════════════
# 6. STALE-PK "UNCONFIRMED" CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def test_stale_pk_reliance_is_flagged_unconfirmed_not_silently_covered():
    """The real, live bug this catches: schema.sql's CREATE TABLE PRIMARY
    KEY text for prop_unit is still the PRE-migration `PRIMARY KEY
    (prop_id)` (confirmed by direct inspection -- schema.sql was never
    updated after migrate_county_partitioning.py ran live). In offline
    schema-sql mode, a query filtering prop_unit by prop_id alone
    "matches" that stale PK and would be reported COVERED -- which may be
    FALSE in the real, live database (whose real PK is now
    (county_code, prop_id)). This must be downgraded to UNCONFIRMED, not
    reported as a clean COVERED, when index_source == 'schema-sql'."""
    item = vic.ExtractedSQL(
        filepath="app.py", lineno=1630, call_name="query",
        sql_text="SELECT geo_id FROM prop_unit WHERE prop_id = %s",
        resolution="literal",
    )
    # prop_unit is a real TABLE_SPECS entry (composite_pk mode) -- confirm
    # via the tool's own loader rather than hardcoding the assumption here.
    stale_tables = set(vic._load_composite_pk_migrated_tables())
    assert "prop_unit" in stale_tables, "fixture assumption invalid -- prop_unit is no longer composite_pk-migrated?"

    table_indexes = {"prop_unit": [("prop_unit_pkey", ["prop_id"])]}
    findings, notes = vic.audit_extracted([item], table_indexes)
    assert findings[0].status == "COVERED"  # true at the raw Finding level (stale PK genuinely matches)
    assert findings[0].best_index_name == "prop_unit_pkey"

    trustworthy, unconfirmed = vic._split_covered_by_pk_staleness(findings, "schema-sql")
    assert len(unconfirmed) == 1 and len(trustworthy) == 0
    assert unconfirmed[0].filepath == "app.py" and unconfirmed[0].lineno == 1630


def test_real_secondary_index_not_downgraded_even_on_stale_pk_table():
    """Contrast case: county_tax_rate IS a stale-PK table, but a query
    matched via idx_rate_entity (a real, standalone, non-PK index
    unaffected by the PK migration) must NOT be downgraded -- only matches
    against the literal "<table>_pkey" synthetic entry are suspect."""
    item = vic.ExtractedSQL(
        filepath="app.py", lineno=1924, call_name="query",
        sql_text="SELECT rate FROM county_tax_rate WHERE entity_code = %s",
        resolution="literal",
    )
    table_indexes = {"county_tax_rate": [
        ("idx_rate_entity", ["entity_code"]),
        ("county_tax_rate_pkey", ["entity_code", "tax_year"]),
    ]}
    findings, notes = vic.audit_extracted([item], table_indexes)
    trustworthy, unconfirmed = vic._split_covered_by_pk_staleness(findings, "schema-sql")
    assert len(trustworthy) == 1 and len(unconfirmed) == 0
    assert trustworthy[0].best_index_name == "idx_rate_entity"


def test_live_index_source_never_downgrades():
    """--index-source live has no PK-staleness ambiguity at all (pg_indexes
    IS the live truth) -- _split_covered_by_pk_staleness must be a no-op
    when index_source == 'live'."""
    item = vic.ExtractedSQL(
        filepath="app.py", lineno=1630, call_name="query",
        sql_text="SELECT geo_id FROM prop_unit WHERE prop_id = %s",
        resolution="literal",
    )
    table_indexes = {"prop_unit": [("prop_unit_pkey", ["prop_id"])]}
    findings, notes = vic.audit_extracted([item], table_indexes)
    trustworthy, unconfirmed = vic._split_covered_by_pk_staleness(findings, "live")
    assert len(trustworthy) == 1 and len(unconfirmed) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. prop_id / entity_code GENERAL-PURPOSE SCOPE (explicit brief requirement)
# ═══════════════════════════════════════════════════════════════════════════

def test_general_purpose_prop_id_leading_risk_prop_unit_tax_year():
    """Explicit brief requirement: don't build a geo_id-only checker.
    prop_unit_tax_year's real, live PK is (county_code, prop_id, tax_year)
    per migrate_county_partitioning.py's TABLE_SPECS. A query joining/
    filtering by prop_id + tax_year alone (no county_code) is the exact
    same risk class as the incident, just a different column."""
    sql = "UPDATE prop_unit_tax_year SET market_value=%s WHERE prop_id = %s AND tax_year = %s"
    shape = vic.parse_sql_shape(sql)
    assert shape.filter_columns == {"prop_unit_tax_year": {"prop_id", "tax_year"}}

    k, name, cols = vic.best_index_match(
        shape.filter_columns["prop_unit_tax_year"],
        [("prop_unit_tax_year_pkey", ["county_code", "prop_id", "tax_year"])],
    )
    assert k == 0  # zero benefit, same as the incident


def test_general_purpose_entity_code_leading_risk_county_tax_rate():
    """Same risk class, county_tax_rate/entity_code: real, live PK is
    (county_code, entity_code, tax_year). A query filtering by entity_code
    alone against ONLY that PK (no standalone secondary index) gets k=0."""
    sql = "SELECT rate FROM county_tax_rate WHERE entity_code = %s"
    shape = vic.parse_sql_shape(sql)
    assert shape.filter_columns == {"county_tax_rate": {"entity_code"}}

    k, name, cols = vic.best_index_match(
        shape.filter_columns["county_tax_rate"],
        [("county_tax_rate_pkey", ["county_code", "entity_code", "tax_year"])],
    )
    assert k == 0


# ═══════════════════════════════════════════════════════════════════════════
# Manual runner (no confirmed pytest/pip access in this sandbox)
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [(name, fn) for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        print(f"\n{len(failed)} FAILURE(S):")
        for name, msg in failed:
            print(f"  {name}: {msg}")
        sys.exit(1)
    sys.exit(0)
