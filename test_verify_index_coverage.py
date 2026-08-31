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
# Stage 4 — MISSING_TENANT_SCOPE (added after PX-20260828-16's follow-up
# investigation surfaced the need; see verify_index_coverage.py's own
# module docstring "Stage 4" section for the full design rationale).
# ═══════════════════════════════════════════════════════════════════════════

def _tenant_extracted(sql, filepath="app.py", call_name="query", lineno=1):
    """One-item extracted list, matching ExtractedSQL's real shape, for
    feeding directly into vic.audit_tenant_scope()."""
    return [vic.ExtractedSQL(filepath, lineno, call_name, sql, "literal")]


def test_stage4_reproduces_px_20260828_12_category_7_shape():
    """The real incident shape (PX-20260828-12 Category 7): a query joins
    parcel_metrics by geo_id alone, no county_code anywhere -- exactly the
    bug class idx_parcel_geo_id_only made performance-invisible (see this
    stage's own module docstring). Must be flagged MISSING_TENANT_SCOPE
    for parcel_metrics, regardless of whether `parcel` itself is scoped."""
    sql = (
        "SELECT p.geo_id, pm.risk_large_value_jump "
        "FROM parcel p "
        "JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = %s "
        "WHERE p.county_code = %s"
    )
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql), required_tables={"parcel", "parcel_metrics"}, shared_exemptions={},
    )
    gaps = {f.table: f for f in findings if f.status == "MISSING_TENANT_SCOPE"}
    assert "parcel_metrics" in gaps, "the exact incident shape must be caught"
    assert "parcel" not in gaps, "parcel IS scoped here (p.county_code = %s) and must not be flagged"
    assert gaps["parcel_metrics"].filter_columns == ["geo_id", "tax_year"]


def test_stage4_direct_where_scoping_is_not_flagged():
    """The straightforward, correctly-scoped case: a single table filtered
    directly by county_code. No finding at all."""
    sql = "SELECT * FROM parcel_metrics WHERE geo_id = %s AND tax_year = %s AND county_code = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql), required_tables={"parcel_metrics"}, shared_exemptions={},
    )
    assert findings == []


def test_stage4_transitive_join_scoping_needs_no_special_logic():
    """Per this stage's own design note: a JOIN...ON condition that itself
    carries an alias.county_code = alias.county_code equality predicate
    already lands county_code directly in BOTH tables' filter_columns,
    via parse_sql_shape's existing _extract_predicate_refs (Stage 2,
    UNCHANGED) -- no separate transitive-join detection was written for
    Stage 4, and this test proves that claim rather than assuming it."""
    sql = (
        "SELECT p.geo_id, pm.risk_large_value_jump "
        "FROM parcel p "
        "JOIN parcel_metrics pm ON pm.geo_id = p.geo_id AND pm.tax_year = %s "
        "                       AND pm.county_code = p.county_code "
        "WHERE p.county_code = %s"
    )
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql), required_tables={"parcel", "parcel_metrics"}, shared_exemptions={},
    )
    assert findings == [], f"both tables are scoped (directly or via the JOIN condition); got {findings}"


def test_stage4_ignores_writes_updates_deletes_inserts():
    """Scope is reads only (see module docstring's 'Scope: reads only'
    note) -- verify_county_scoping.py's rule 3(d) already owns UPDATE/
    DELETE WHERE-clause scoping via its own, broader extraction. An
    unscoped UPDATE/DELETE/INSERT against a composite_pk-migrated table
    must NOT produce a Stage 4 finding, even though it would be a real
    rule-3(d)/3(b) finding in the sibling tool."""
    for sql in [
        "UPDATE parcel_metrics SET risk_large_value_jump = TRUE WHERE geo_id = %s",
        "DELETE FROM parcel_metrics WHERE geo_id = %s",
        "INSERT INTO parcel_metrics (geo_id, tax_year) VALUES (%s, %s)",
    ]:
        findings = vic.audit_tenant_scope(
            _tenant_extracted(sql), required_tables={"parcel_metrics"}, shared_exemptions={},
        )
        assert findings == [], f"write statement must be excluded from Stage 4: {sql!r} -> {findings}"


def test_stage4_excludes_test_fixture_files():
    """Confirmed via a real run against this repo while building this
    stage: without this filter, Stage 4 flagged loaders/test_pir_loaders.py's
    own fixture SQL (Stage 1's extraction walks test files too -- an
    existing, untouched Stage 1 characteristic). A file whose basename
    starts with test_/validate_/verify_ is not a real production call
    site and must never produce a Stage 4 finding."""
    sql = "SELECT * FROM parcel_metrics WHERE geo_id = %s"
    for fp in ["loaders/test_pir_loaders.py", "test_something.py", "verify_other_thing.py"]:
        findings = vic.audit_tenant_scope(
            _tenant_extracted(sql, filepath=fp), required_tables={"parcel_metrics"}, shared_exemptions={},
        )
        assert findings == [], f"test/verify file must be excluded: {fp} -> {findings}"
    # Sanity: the SAME sql from a real production file IS flagged.
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"), required_tables={"parcel_metrics"}, shared_exemptions={},
    )
    assert len(findings) == 1 and findings[0].status == "MISSING_TENANT_SCOPE"


def test_stage4_write_only_exemption_does_not_suppress_a_read_finding():
    """The core collision-safety claim from this stage's exemption-sharing
    design: an exemption entry registered for a WRITE finding (stmt_kind
    DELETE, applies_to={'write'} only -- exactly the real shape of all 7
    entries currently in verify_county_scoping.EXEMPTIONS, confirmed via
    the 'one check first' audit) must NOT suppress a Stage 4 SELECT
    finding on the same (file, table) pair. Proves the {'read'} tag gate
    actually gates, rather than any matching key silently passing."""
    write_only_exemptions = {
        ("loaders/compute_metrics.py", "parcel_metrics", "DELETE"): {
            "reason": "whole-table rebuild, write-side only",
            "approved_by": "PX-20260823-02",
            "applies_to": {"write"},
        },
    }
    sql = "SELECT * FROM parcel_metrics WHERE geo_id = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="loaders/compute_metrics.py"),
        required_tables={"parcel_metrics"},
        shared_exemptions=write_only_exemptions,
    )
    assert len(findings) == 1
    assert findings[0].status == "MISSING_TENANT_SCOPE", (
        "a write-only-tagged exemption must not silently suppress a read-path finding"
    )


def test_stage4_read_tagged_exemption_does_suppress():
    """The other half of the same claim: an exemption explicitly keyed
    (file, table, 'SELECT') AND tagged {'read'} in applies_to DOES
    suppress the finding, reported as EXEMPT rather than dropped
    silently (Law-3-style transparency, matching verify_county_scoping.py's
    own EXEMPT reporting convention)."""
    read_exemptions = {
        ("app.py", "parcel_metrics", "SELECT"): {
            "reason": "hypothetical, reviewed read-path exemption for this test only",
            "approved_by": "TEST-FIXTURE",
            "applies_to": {"read"},
        },
    }
    sql = "SELECT * FROM parcel_metrics WHERE geo_id = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel_metrics"},
        shared_exemptions=read_exemptions,
    )
    assert len(findings) == 1
    assert findings[0].status == "EXEMPT"
    assert "TEST-FIXTURE" in findings[0].detail


def test_stage4_non_equality_county_code_is_still_flagged_with_note():
    """county_code appearing only via a non-equality operator (e.g. !=)
    lands in informational_columns, not filter_columns (Stage 2's own
    is_point_lookup distinction, unchanged) -- this does NOT count as
    real tenant scoping (a '!=' predicate is the wrong direction for a
    security boundary) and must still be flagged, with an explanatory
    note attached rather than silently passing."""
    sql = "SELECT * FROM parcel_metrics WHERE geo_id = %s AND county_code != %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql), required_tables={"parcel_metrics"}, shared_exemptions={},
    )
    assert len(findings) == 1
    assert findings[0].status == "MISSING_TENANT_SCOPE"
    assert "non-equality operator" in findings[0].detail


def test_stage4_ignores_tables_not_in_required_set():
    """A table not in the composite_pk-migrated required set (e.g. a
    plain, never-migrated table) produces no finding even with zero
    filter columns at all -- Stage 4 only ever looks at the tables
    _load_composite_pk_migrated_tables() names, per Diego's explicit
    instruction to source the required-tables list from that function."""
    sql = "SELECT * FROM some_unrelated_table WHERE id = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql), required_tables={"parcel_metrics"}, shared_exemptions={},
    )
    assert findings == []


def test_stage4_is_write_statement_classification():
    """Direct unit coverage of the write/read classifier itself, since
    every other Stage 4 test exercises it only indirectly."""
    assert vic._is_write_statement("UPDATE parcel SET x = 1 WHERE geo_id = %s")
    assert vic._is_write_statement("DELETE FROM parcel WHERE geo_id = %s")
    assert vic._is_write_statement("INSERT INTO parcel (geo_id) VALUES (%s)")
    assert not vic._is_write_statement("SELECT * FROM parcel WHERE geo_id = %s")


def test_stage4_never_merged_with_stage3_findings():
    """Diego's explicit instruction: Stage 4's findings must be a
    SEPARATE list, never merged into Stage 1-3's NO_COVERAGE/UNCONFIRMED/
    COVERED verdicts. run_audit() must expose them under their own key."""
    result = vic.run_audit(
        source_paths=[],  # no real files scanned; just checking the result shape
        index_source="schema-sql",
    )
    assert "tenant_scope_findings" in result
    assert isinstance(result["tenant_scope_findings"], list)
    # And no Stage 3 Finding should ever end up with a MISSING_TENANT_SCOPE
    # status -- the two Finding types are entirely distinct dataclasses.
    for f in result["findings"]:
        assert not hasattr(f, "status") or f.status != "MISSING_TENANT_SCOPE"


def test_stage4_stale_read_exemption_flagged_when_no_finding_matches():
    """Law-3 mirror: a {'read'}-tagged EXEMPTIONS entry that matches ZERO
    Stage 4 findings on this run must itself surface as a loud
    STALE_EXEMPTION finding, not silently persist forever. This is the
    read-side mirror of verify_county_scoping.py's own write-side Law-3
    check -- each tool owns staleness only for the direction it can
    structurally ever match (see both files' applies_to gating)."""
    stale_read_exemptions = {
        ("app.py", "parcel_metrics", "SELECT"): {
            "reason": "hypothetical exemption that no longer matches anything",
            "approved_by": "TEST-FIXTURE",
            "applies_to": {"read"},
        },
    }
    # A query that does NOT touch parcel_metrics at all, and is properly
    # scoped where it does touch a required table -- so this run produces
    # zero MISSING_TENANT_SCOPE findings AND zero EXEMPT findings for the
    # registered key above. The exemption should surface as stale.
    sql = "SELECT * FROM parcel WHERE county_code = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel"},
        shared_exemptions=stale_read_exemptions,
    )
    stale = [f for f in findings if f.status == "STALE_EXEMPTION"]
    assert len(stale) == 1, f"expected exactly 1 STALE_EXEMPTION finding, got: {findings}"
    assert stale[0].filepath == "app.py"
    assert stale[0].table == "parcel_metrics"
    assert "TEST-FIXTURE" in stale[0].detail


def test_stage4_write_tagged_only_exemption_is_never_stale_checked_here():
    """A registry entry tagged {'write'} only (no 'read') must NEVER be
    flagged STALE_EXEMPTION by Stage 4, even if it matches nothing here --
    that entry belongs to verify_county_scoping.py's own write-side Law 3
    check, not this one (the applies_to gate must exclude it, not just
    happen to not find it)."""
    write_only_exemptions = {
        ("loaders/quarantine_contamination.py", "tax_billing", "DELETE"): {
            "reason": "write-only exemption, irrelevant to Stage 4",
            "approved_by": "TEST-FIXTURE",
            "applies_to": {"write"},
        },
    }
    sql = "SELECT * FROM parcel WHERE county_code = %s"
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel"},
        shared_exemptions=write_only_exemptions,
    )
    assert all(f.status != "STALE_EXEMPTION" for f in findings), (
        f"a write-only exemption must never be Stage-4-stale-checked, got: {findings}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# PX-20260830-05 Task 1 (Bucket A): nested-paren-body tenant-scope walk
# ═══════════════════════════════════════════════════════════════════════════

def test_stage4_cte_scoped_query_passes():
    """A real, live query shape (app.py's Tier-2 peer-set query): TWO
    independent `WITH ... AS MATERIALIZED (...)` CTEs, each with its own
    county_code-scoped WHERE clause, joined together at the outer level with
    no top-level WHERE at all. Stage 2's own single-WHERE-clause walk can
    only ever see ONE of the two CTE bodies' WHERE text (whichever "WHERE"
    keyword appears FIRST in the raw statement) -- confirmed against the
    real app.py query this reproduces, this shape used to flag the SECOND
    CTE's own table as MISSING_TENANT_SCOPE despite that CTE's own body
    genuinely filtering by county_code. Must now pass with zero findings."""
    sql = """
        WITH candidates AS MATERIALIZED (
            SELECT p.geo_id FROM parcel p
            WHERE p.classi_cd = %(cc)s AND p.county_code = %(county_code)s
        ),
        mv_band AS MATERIALIZED (
            SELECT pty.geo_id, pty.market_value FROM parcel_tax_year pty
            WHERE pty.tax_year = 2025 AND pty.county_code = %(county_code)s
        )
        SELECT c.geo_id, m.market_value
        FROM candidates c JOIN mv_band m ON m.geo_id = c.geo_id
    """
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel", "parcel_tax_year"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE"]
    assert not gaps, f"expected zero findings (both CTEs are genuinely county_code-scoped), got: {gaps}"


def test_stage4_exists_scoped_query_passes():
    """A real, live query shape (app.py's already_fetched cache-check): a
    bare `SELECT (EXISTS (...)) OR (EXISTS (...)) AS already_fetched` with
    no top-level FROM/WHERE at all -- both real tables only ever appear
    inside their own EXISTS(...) subquery, each with its own county_code
    predicate. Stage 2's single-WHERE walk only ever resolves the FIRST
    EXISTS's own WHERE text, and (since both tables are already registered
    into tables_touched by the flat FROM-regex scan before the WHERE walk
    runs) the ambiguous-unaliased-column branch fires and drops county_code
    for BOTH tables. Must now pass with zero findings for the composite_pk
    table under test (tax_billing)."""
    sql = (
        "SELECT "
        "(EXISTS (SELECT 1 FROM tax_billing_portal_scrape WHERE geo_id = %s AND county_code = %s)) "
        "OR "
        "(EXISTS (SELECT 1 FROM tax_billing WHERE geo_id = %s AND county_code = %s AND tax_year BETWEEN 2021 AND 2024)) "
        "AS already_fetched"
    )
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"tax_billing"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE"]
    assert not gaps, f"expected zero findings for tax_billing (its own EXISTS body is county_code-scoped), got: {gaps}"


def test_stage4_derived_table_with_nested_in_subquery_still_resolves():
    """The trickiest real shape found while building this walk: a
    parenthesized derived table whose OWN WHERE clause has bare (unaliased)
    county_code AND a further-nested `geo_id IN (SELECT geo_id FROM
    candidates)` sub-subquery. Without _blank_inner_parens(), that inner
    `FROM candidates` leaks into the derived table's own tables_touched,
    flips the bare county_code predicate into 'ambiguous, 2 tables in
    scope', and county_code is silently never attributed to
    tax_billing_entity at all -- reproduced here as its own regression case
    (this exact shape was found live in app.py's real peer-set fallback
    query, and required a fix beyond the basic CTE/EXISTS walk)."""
    sql = """
        WITH candidates AS (
            SELECT p.geo_id FROM parcel p WHERE p.county_code = %(county_code)s
        )
        SELECT c.geo_id, tbe.entity_tax_sum
        FROM candidates c
        LEFT JOIN (
            SELECT geo_id, SUM(amount_due) AS entity_tax_sum
            FROM tax_billing_entity
            WHERE tax_year = 2025
              AND county_code = %(county_code)s
              AND geo_id IN (SELECT geo_id FROM candidates)
            GROUP BY geo_id
        ) tbe ON tbe.geo_id = c.geo_id
    """
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"tax_billing_entity"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE"]
    assert not gaps, f"expected zero findings (derived table's own WHERE scopes county_code), got: {gaps}"


def test_stage4_nested_body_without_county_code_still_fails():
    """The negative control the brief explicitly requires: a CTE-shaped
    query where the predicate is GENUINELY absent (not just hard for Stage 2
    to see) must still be flagged. Proves the nested-body walk is additive
    scoping detection, not a blanket 'any CTE passes' loophole."""
    sql = """
        WITH candidates AS MATERIALIZED (
            SELECT p.geo_id FROM parcel p WHERE p.classi_cd = %(cc)s
        )
        SELECT c.geo_id FROM candidates c
    """
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE" and f.table == "parcel"]
    assert len(gaps) == 1, f"expected a real MISSING_TENANT_SCOPE finding for parcel (no county_code anywhere), got: {findings}"


def test_stage4_real_app_py_tax_delinquent_now_scoped():
    """Source-level regression proof for the real app.py fix (PX-20260830-05
    Bucket A): property_detail()'s tax_delinquent lookup used to be `WHERE
    geo_id = %s` alone. Reads the real, shipping app.py source directly (not
    a reimplementation) and asserts the fixed line is present, and that no
    bare, single-column tax_delinquent lookup survives anywhere in the file."""
    src = open("app.py", encoding="utf-8").read()
    assert 'FROM tax_delinquent WHERE geo_id = %s AND county_code = %s' in src, (
        "expected the fixed, county_code-scoped tax_delinquent query to be present in app.py"
    )
    assert 'FROM tax_delinquent WHERE geo_id = %s"' not in src, (
        "a stale, unscoped bare-geo_id tax_delinquent query still exists in app.py"
    )


def test_stage4_real_app_py_and_registry_end_to_end_clean():
    """End-to-end proof against the REAL, current app.py source tree (not a
    fixture) that the four CTE/EXISTS false positives this walk was built to
    resolve, plus the tax_delinquent fix, are both reflected in the live
    Stage 4 run, and that the four tax_billing_entity findings PX-20260828-16
    used to carry as {'read'} exemptions now resolve on their own -- i.e.
    that registry entry's removal (PX-20260830-05 Task 1) was safe.

    Narrowed to app.py specifically (PX-20260830-05 Task 3/4): this
    assertion originally checked "zero EXEMPT tax_billing_entity findings"
    repo-wide, back when the shared registry carried zero {'read'}-tagged
    entries anywhere (see verify_index_coverage.py's own module docstring,
    updated same session) -- at that point "zero in app.py" and "zero
    anywhere" were the same claim by coincidence, not by this test's actual
    design. Tasks 3-4 registered real, unrelated {'read'} exemptions for
    loaders/refresh_group_stats.py and loaders/delete_confirmed_
    absent_taxcur_rows.py (both correct-by-design, not app.py regressions --
    see verify_county_scoping.EXEMPTIONS' own PX-20260830-05 entries), which
    made the repo-wide version of this assertion fail for a reason that has
    nothing to do with what this test actually verifies. Scoped to
    filepath == 'app.py' so it keeps testing its own real claim -- the four
    app.py call sites resolve on their own -- without being coupled to
    exemptions this brief registers elsewhere in the same run."""
    import verify_county_scoping as vcs
    result = vic.run_audit(index_source="schema-sql")
    tenant = result["tenant_scope_findings"]
    exempt = [f for f in tenant if f.status == "EXEMPT" and f.table == "tax_billing_entity"
              and f.filepath == "app.py"]
    assert not exempt, f"expected zero EXEMPT tax_billing_entity findings in app.py (registry entry was retired), got: {exempt}"
    stale = [f for f in tenant if f.status == "STALE_EXEMPTION"]
    assert not stale, f"expected zero stale exemptions after retiring the tax_billing_entity entry, got: {stale}"
    key = ("app.py", "tax_billing_entity", "SELECT")
    assert key not in vcs.EXEMPTIONS, "the retired tax_billing_entity exemption entry must not reappear in the registry"


# ═══════════════════════════════════════════════════════════════════════════
# PX-20260830-05 Task 2 correction: FILTER(WHERE ...) parser bug (found live
# against load_2026_preliminary.py's run_qa() null-rate loop during this
# brief's acceptance sweep, after all 27 remaining Bucket B rows were
# predicated)
# ═══════════════════════════════════════════════════════════════════════════

def test_stage4_filter_where_before_real_where_does_not_mask_scoping():
    """Real, live shape (load_2026_preliminary.py:320's null-rate loop): a
    `COUNT(*) FILTER (WHERE ...)` aggregate clause sits in the SELECT list,
    BEFORE the statement's real, top-level WHERE clause. The old WHERE-
    clause finder used `re.search(r'\\bWHERE\\b', sql_text)` -- the FIRST
    occurrence anywhere in the text -- so it locked onto the FILTER's own
    nested WHERE, and _clause_end() stopped right at the FILTER's closing
    paren, meaning the real outer WHERE (carrying the actual county_code
    predicate) was NEVER examined at all -- a false MISSING_TENANT_SCOPE on
    a query that IS correctly scoped. Must now produce zero findings."""
    sql = (
        "SELECT COUNT(*) FILTER (WHERE market_value IS NULL OR market_value = 0) AS nulls, "
        "COUNT(*) AS total "
        "FROM parcel_tax_year WHERE tax_year = 2026 AND county_code = %s"
    )
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel_tax_year"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE"]
    assert not gaps, f"expected zero findings (the real outer WHERE scopes county_code), got: {gaps}"


def test_stage4_filter_where_shape_still_fails_when_county_code_genuinely_absent():
    """Negative control for the fixture above: same FILTER(WHERE ...)-before-
    real-WHERE shape, but county_code is genuinely absent from the real
    outer WHERE this time. Must still be flagged -- proves the fix finds the
    real top-level WHERE and evaluates ITS content correctly, not that a
    FILTER clause in the SELECT list now makes everything pass."""
    sql = (
        "SELECT COUNT(*) FILTER (WHERE market_value IS NULL OR market_value = 0) AS nulls, "
        "COUNT(*) AS total "
        "FROM parcel_tax_year WHERE tax_year = 2026"
    )
    findings = vic.audit_tenant_scope(
        _tenant_extracted(sql, filepath="app.py"),
        required_tables={"parcel_tax_year"},
        shared_exemptions={},
    )
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE" and f.table == "parcel_tax_year"]
    assert len(gaps) == 1, f"expected a real MISSING_TENANT_SCOPE finding (county_code genuinely absent), got: {findings}"


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
