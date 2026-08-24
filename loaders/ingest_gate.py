"""
loaders/ingest_gate.py — the Ingestion Conservation Gate (Migration M2,
SPEC_UNIT_MODEL_AND_INGEST_GATE.md §4.2). Absorbs
investigate_geo_id_prop_id_collision.py's PROP.TXT/PROP_ENT.TXT scan logic
(that script's two-pass geo_id/prop_id collision measurement is now just
one instance of this module's G1+G2 checks) plus five additional checks,
run after every loader finishes, before compute_metrics.py is allowed to
run on the new data.

Two-tier standard (per spec §4.1): checks G1-G5 are INTERNAL — the source
file and our own tables must agree EXACTLY (zero tolerance; any drift
means a loader silently dropped or duplicated something, which is exactly
the class of bug this migration exists to eliminate). G6 is EXTERNAL — a
banded comparison (see g6_external_reconciliation_check's own docstring
for how the band is interpreted) against TCAD's own published totals,
where some deviation is expected and legitimate (their published scope
and ours are not byte-identical — different as-of dates, different
included property classes, etc).

  G1 — source scan conservation identity: every line of a source file is
       classified into EXACTLY one bucket (accepted, short_line,
       supplement, no_geo_id, ...); bucket counts must sum to the file's
       total line count. This is what catches a line silently falling
       through every classification with no bucket at all — the
       "did we account for every single line" check.
  G2 — identity coverage: the count of distinct prop_ids the file scan
       says should exist for a source must exactly equal the count of
       distinct prop_ids that landed a prop_unit_tax_year row for THAT
       SAME tax_year (fixed 2026-07-29, task M3-G2-FIX: an earlier version
       of this check compared against an unscoped COUNT(*) FROM prop_unit,
       which accumulates across every year ever loaded and can never
       match a single year's file scan -- see g2_identity_coverage_check's
       own docstring for the live-DB numbers that exposed this).
  G3 — dollar conservation: SUM(market_value) computed directly from the
       source file (one value per prop_id, not per entity-line) must
       exactly equal SUM(market_value) in prop_unit_tax_year, which must
       exactly equal SUM(market_value) in the rolled-up parcel_tax_year
       for the same tax_year.
  G4 — rollup integrity: for every geo_id, parcel_tax_year's value
       columns must exactly equal SUM()/COUNT() over that geo_id's
       prop_unit_tax_year rows — i.e., parcel_rollup.py actually did what
       it claims to do, checked independently of parcel_rollup.py's own
       code (this check re-derives the aggregation itself rather than
       trusting parcel_rollup ran correctly).
  G5 — account coverage: for THIS SAME tax_year, the count of distinct
       geo_ids with real unit data (prop_unit_tax_year JOIN prop_unit)
       must exactly equal the count of rows in parcel_tax_year (fixed
       2026-07-29, task M3-G5-FIX: an earlier version of this check
       compared an unscoped COUNT(DISTINCT geo_id) FROM prop_unit -- all
       years ever loaded -- against COUNT(*) FROM parcel, the
       year-independent master reference table, not parcel_tax_year;
       confirmed live it printed the identical number for every tax_year
       passed to --check-db, proving it was blind to year entirely). A
       small residual mismatch is still expected post-fix -- see
       g5_account_coverage_check's own docstring and KNOWN_LIMITATIONS.md's
       orphaned P-type prop_unit_tax_year entry.
  G6 — external reconciliation: computed county-wide total vs a
       TCAD-published total, banded (not exact) — see docstring below.

Design mirrors parcel_rollup.py's split between production SQL/DB code
and a pure-Python decision layer: every g*_check() function below takes
already-computed numbers (counts, sums, sets) and returns a
(passed, detail) verdict with NO database access at all — these are what
loaders/test_ingest_gate.py fixture-tests directly, including the two
deliberate-corruption cases that must fail. The `gather_*` functions that
actually query Postgres to produce those numbers are thin, untested-in-
this-sandbox wrappers (see that test file's docstring for the AC8
disclosure) — same division of labor as parcel_rollup's ROLLUP_SQL vs
compute_rollup().
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config  # noqa: E402

from loaders import ears_format  # noqa: E402


# ══════════════════════════════════════════════════════════════════════
# G1 — source scan conservation identity (pure, file-scan based)
# ══════════════════════════════════════════════════════════════════════
def scan_prop_ledger(path=None, lines=None):
    """
    Run iter_prop_lines() over PROP.TXT (or a fixture `lines` iterable)
    and build the G1 ledger: bucket counts by skip_reason (None bucket
    renamed 'accepted'), plus the set of accepted geo_ids/prop_ids for
    downstream G2 use. Returns a dict:
        {"total_lines": int, "buckets": {"accepted": n, "short_line": n,
         "supplement": n, "no_geo_id": n}, "prop_ids": set(...),
         "geo_ids": set(...)}
    """
    buckets = {"accepted": 0, "short_line": 0, "supplement": 0, "no_geo_id": 0}
    prop_ids = set()
    geo_ids = set()
    total = 0
    for rec in ears_format.iter_prop_lines(path, lines):
        total += 1
        bucket = rec["skip_reason"] or "accepted"
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket == "accepted":
            if rec["prop_id"] is not None:
                prop_ids.add(rec["prop_id"])
            if rec["geo_id"] is not None:
                geo_ids.add(rec["geo_id"])
    return {"total_lines": total, "buckets": buckets, "prop_ids": prop_ids, "geo_ids": geo_ids}


def scan_prop_ent_ledger(path=None, lines=None):
    """Same idea for PROP_ENT.TXT — buckets are per LINE (one line per prop_id x entity)."""
    buckets = {"accepted": 0, "short_line": 0, "supplement": 0}
    prop_ids = set()
    total = 0
    for rec in ears_format.iter_prop_ent_lines(path, lines):
        total += 1
        bucket = rec["skip_reason"] or "accepted"
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket == "accepted" and rec["prop_id"] is not None:
            prop_ids.add(rec["prop_id"])
    return {"total_lines": total, "buckets": buckets, "prop_ids": prop_ids}


def g1_conservation_check(ledger):
    """
    ledger: output of scan_prop_ledger() or scan_prop_ent_ledger().
    PASS iff sum(buckets.values()) == total_lines exactly — every single
    line was classified into exactly one bucket, none double-counted,
    none dropped on the floor uncounted.
    """
    bucket_sum = sum(ledger["buckets"].values())
    total = ledger["total_lines"]
    passed = bucket_sum == total
    detail = f"buckets sum to {bucket_sum:,}, file has {total:,} lines"
    if not passed:
        detail += f"  MISMATCH ({bucket_sum - total:+,}) — some line(s) uncounted or double-counted"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# G2 — identity coverage (pure decision; counts supplied by caller)
# ══════════════════════════════════════════════════════════════════════
def g2_identity_coverage_check(file_prop_id_count, db_landed_count, expected_gap=0):
    """
    SCOPE: per-source. PASS iff db_landed_count - file_prop_id_count ==
    expected_gap exactly.

    file_prop_id_count: distinct prop_ids from THIS source's PROP.TXT
        accepted-row scan (scan_prop_ledger()'s "accepted" bucket).
    db_landed_count: distinct prop_ids landed in prop_unit_tax_year for
        this SAME tax_year + county_code + data_source. PX-20260824-04:
        now scoped by data_source (gather_and_run() adds `AND data_source
        = %s`) -- previously unscoped, which silently conflated this
        source's file-scan count against every data_source's rows for
        that tax_year (harmless for 2025/2026, where exactly one source
        ever writes that tax_year's rows, but structurally wrong for
        historical years where e.g. both `ajr_2022` and `cert_2022` can
        have rows for tax_year=2022 at once).
    expected_gap: PX-20260824-04. PROP_ENT.TXT (the file that actually
        drives prop_unit_tax_year's population via load_prop_ent()) and
        PROP.TXT (the file file_prop_id_count comes from) are TWO
        DIFFERENT FILES with, in general, two different prop_id
        populations -- a prop_id can carry entity/tax data in
        PROP_ENT.TXT while being excluded from PROP.TXT's "accepted"
        bucket (a supplement row, a blank geo_id field, etc). That
        prop_id's PROP_ENT.TXT row still lands a prop_unit_tax_year row
        (with geo_id=NULL) -- see load_prop_ent()'s own n_no_geo counter,
        which is the load-time face of this exact same gap -- so
        db_landed_count will legitimately exceed file_prop_id_count by
        this population difference even for a perfectly-loaded file, with
        zero rows actually lost or duplicated. gather_and_run() computes
        expected_gap directly from the two file scans already gathered
        for G1 (len(ent_ledger prop_ids - prop_ledger prop_ids)) BEFORE
        this function is ever called -- a real, re-derived-every-run
        number, not a hardcoded pass-through of a previously-observed
        figure. If the actual observed gap doesn't match this freshly
        recomputed expectation, that IS a genuine, unexplained
        discrepancy and this check still FAILS loudly (see
        test_ingest_gate.py's deliberate-incomplete-load corruption case).
        Default 0 preserves this function's pre-PX-20260824-04 exact-match
        behavior for any caller that doesn't supply it.
    """
    actual_gap = db_landed_count - file_prop_id_count
    passed = actual_gap == expected_gap
    detail = (f"file(PROP.TXT accepted, this source): {file_prop_id_count:,} distinct prop_ids  "
              f"landed(prop_unit_tax_year, this data_source): {db_landed_count:,} rows  "
              f"gap: {actual_gap:+,}")
    if expected_gap:
        detail += f"  (expected {expected_gap:+,}, from PROP_ENT.TXT prop_ids absent from PROP.TXT's accepted set)"
    if not passed:
        detail += f"  MISMATCH -- actual gap {actual_gap:+,} != expected {expected_gap:+,}"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# G3 — dollar conservation (pure decision)
# ══════════════════════════════════════════════════════════════════════
def g3_dollar_conservation_check(file_sum, unit_table_sum):
    """
    SCOPE: per-source. file_sum and unit_table_sum must match EXACTLY.

    PX-20260824-04: narrowed from a three-way file/unit/account comparison
    to this two-way file/unit comparison. This is the real per-source
    conservation claim -- "did every dollar this file's PROP_ENT.TXT
    entity data names actually land in prop_unit_tax_year for this
    specific data_source." Unlike PROP.TXT's population (which feeds G2
    and can legitimately differ from PROP_ENT.TXT's -- see
    g2_identity_coverage_check's own docstring), load_prop_ent() writes
    EVERY PROP_ENT.TXT aggregate's market_value regardless of whether that
    row's geo_id resolved (a NULL-geo_id row is still a written row with
    its real dollar value) -- so file_sum and unit_table_sum (both scoped
    to this data_source) should match exactly for a correctly-functioning
    load, historical years included, with zero structural exception.

    account_table_sum (parcel_tax_year, the rolled-up view) is
    deliberately NOT part of this check anymore -- it is inherently a
    whole-year, cross-source, geo_id-keyed view, not a per-source dollar
    total, and structurally EXCLUDES no-geo rows (parcel_rollup.py's
    ROLLUP_SQL requires geo_id to have anywhere to roll up TO). Comparing
    it against a single source's unit-level sum needs residual-aware
    semantics, not exact equality -- see g3_rollup_residual_check() below.
    file_sum/unit_table_sum may each individually be None (meaning "no
    non-null dollar values at all in that source") -- None is treated as
    a valid, comparable value (None == None passes), matching SQL SUM()'s
    own all-NULL-returns-NULL semantics.
    """
    passed = file_sum == unit_table_sum
    detail = f"file(PROP_ENT.TXT)=${_fmt(file_sum)}  unit_table(this data_source)=${_fmt(unit_table_sum)}"
    if not passed:
        detail += "  MISMATCH"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# G3_rollup — whole-year rollup residual reconciliation (pure decision;
# new PX-20260824-04, split out of the old three-way G3 check)
# ══════════════════════════════════════════════════════════════════════
def g3_rollup_residual_check(whole_year_unit_sum, account_table_sum, min_expected_residual):
    """
    SCOPE: WHOLE-YEAR, both sides -- deliberately NOT per-source on either
    side. This is the one check in this module that's genuinely meant to
    be a whole-year comparison (per the brief's own instruction to "keep
    the whole-year comparison only where it means something") -- comparing
    a per-source sum against a whole-year rollup total is NOT a
    well-defined claim once more than one data_source has rows for the
    same tax_year (a real, historical-year scenario) -- see this
    function's own git history / PX-20260824-04 report for the earlier,
    broken per-source-vs-whole-year version of this check and why it was
    replaced with this whole-year-vs-whole-year design during fixture
    testing.

    whole_year_unit_sum: SUM(market_value) FROM prop_unit_tax_year for
        this tax_year/county, UNSCOPED by data_source (every source that
        has ever written a row for this tax_year, combined) -- gathered
        by a second, deliberately unscoped query in gather_and_run(),
        alongside the data_source-scoped one G2/G3 use.
    account_table_sum: SUM(market_value) FROM parcel_tax_year for this
        tax_year/county -- inherently whole-year and cross-source (one row
        per geo_id, `data_source` on it is a MIN() representative across
        every unit summed in, per post_load_summary()'s own comment).
        parcel_rollup.py's ROLLUP_SQL structurally excludes any row with a
        NULL geo_id (nowhere to roll up TO), so whole_year_unit_sum will
        always legitimately be >= account_table_sum for a correctly
        functioning system -- never the reverse.
    min_expected_residual: gather_and_run() computes this directly from
        THIS run's own source file -- the sum of market_value for every
        PROP_ENT.TXT aggregate in THIS source whose prop_id has no
        resolved geo_id (absent from PROP.TXT's accepted set) -- BEFORE
        calling this function. This is a LOWER BOUND on the true
        whole-year residual, not a claim of exact equality: OTHER sources
        that have also written rows for this tax_year (e.g. a lingering
        `ajr_2022` alongside a fresh `cert_2022` load) may carry their own
        no-geo rows too, which this run has no file to re-derive (it only
        has ITS OWN prop_path/prop_ent_path) -- their contribution would
        add to the real residual without this check being able to name it
        specifically. A real, re-derived-every-run number either way, not
        a hardcoded pass-through of a previously-observed figure (e.g. the
        $774,939,443 2022 figure PX-20260824-04 investigated).

    PASS iff (whole_year_unit_sum - account_table_sum) >= min_expected_residual.
    Using >= rather than == is a deliberate, disclosed choice: a residual
    LARGER than this source's own known explanation is not itself a
    failure (other sources may be contributing their own, separately
    unexplained gap -- a real but different concern, not this run's to
    diagnose from its own file alone). A residual SMALLER than the known
    minimum, or negative, IS a genuine problem -- it would mean even this
    source's own well-understood no-geo dollars aren't fully reflected in
    the whole-year gap, which should never happen if parcel_rollup.py is
    working correctly -- and still fails this loudly.
    """
    whole_year_unit_sum = whole_year_unit_sum or 0
    account_table_sum = account_table_sum or 0
    actual_residual = whole_year_unit_sum - account_table_sum
    passed = actual_residual >= min_expected_residual
    detail = (f"unit_table(WHOLE-YEAR, all sources)=${_fmt(whole_year_unit_sum)}  "
              f"account_table(parcel_tax_year, WHOLE-YEAR rollup)=${_fmt(account_table_sum)}  "
              f"residual=${_fmt(actual_residual)}  min_expected(no-geo rows, THIS source only)=${_fmt(min_expected_residual)}")
    if not passed:
        detail += "  MISMATCH -- residual smaller than even this source's own known no-geo gap"
    elif actual_residual > min_expected_residual:
        detail += "  (residual exceeds this source's own known gap -- other source(s) likely also contributing unexplained no-geo dollars for this tax_year; not this run's own problem to name)"
    return passed, detail


def _fmt(v):
    return f"{v:,}" if v is not None else "NULL"


# ══════════════════════════════════════════════════════════════════════
# G4 — rollup integrity (pure decision; re-derives the aggregation itself
#      rather than trusting parcel_rollup.py's own output, so a bug in
#      parcel_rollup.py can't hide itself from its own gate check)
# ══════════════════════════════════════════════════════════════════════
def g4_rollup_integrity_check(unit_rows, parcel_tax_year_rows, tax_year):
    """
    unit_rows: prop_unit_tax_year rows joined to geo_id (same shape as
        parcel_rollup.compute_rollup()'s input).
    parcel_tax_year_rows: {geo_id: {"market_value":..., "unit_count":..., ...}}
        as currently stored in parcel_tax_year for this tax_year.

    Independently re-derives what parcel_tax_year SHOULD contain (via
    parcel_rollup.compute_rollup — the same hand-verified mirror used by
    parcel_rollup's own tests) and diffs it against what's actually
    stored. Any geo_id where they disagree is a mismatch.
    """
    import parcel_rollup

    expected_rows = {r["geo_id"]: r for r in parcel_rollup.compute_rollup(unit_rows, tax_year)}
    mismatches = []
    compare_cols = ["market_value", "assessed_value", "taxable_value", "hs_cap_loss",
                     "land_value", "imprv_value", "exemption_codes", "unit_count"]

    all_geo_ids = set(expected_rows) | set(parcel_tax_year_rows)
    for geo_id in all_geo_ids:
        expected = expected_rows.get(geo_id)
        actual = parcel_tax_year_rows.get(geo_id)
        if expected is None:
            mismatches.append((geo_id, "in parcel_tax_year but no prop_unit_tax_year rows"))
            continue
        if actual is None:
            mismatches.append((geo_id, "has prop_unit_tax_year rows but missing from parcel_tax_year"))
            continue
        for col in compare_cols:
            if expected.get(col) != actual.get(col):
                mismatches.append((geo_id, f"{col}: expected {expected.get(col)!r}, actual {actual.get(col)!r}"))

    passed = len(mismatches) == 0
    detail = f"{len(all_geo_ids):,} geo_ids checked, {len(mismatches):,} mismatches"
    return passed, detail, mismatches


# ══════════════════════════════════════════════════════════════════════
# G5 — account coverage (pure decision)
# ══════════════════════════════════════════════════════════════════════
def g5_account_coverage_check(distinct_geo_ids_in_units, geo_id_count_in_parcel):
    """
    PASS iff, for a given tax_year, the count of distinct geo_ids with
    real unit data exactly equals the count of rows in that same year's
    parcel_tax_year.

    Fixed 2026-07-29 (task M3-G5-FIX): both caller-supplied counts must
    now be scoped to the SAME tax_year (distinct_geo_ids_in_units from
    `prop_unit_tax_year JOIN prop_unit WHERE tax_year = %s`,
    geo_id_count_in_parcel from `parcel_tax_year WHERE tax_year = %s` --
    NOT the year-independent `parcel` master reference table, which is a
    different table with a different meaning). The pre-fix version was
    unscoped on both sides and confirmed live to report the identical
    number for every tax_year passed to --check-db -- it could never
    have caught a real per-year gap. This function itself never changed;
    the bug was entirely in what the caller supplied.

    Previously-documented residual even after this fix: ~3,119/year
    orphaned "P-type" prop_unit_tax_year rows with no matching prop_unit
    row (see KNOWN_LIMITATIONS.md's Unit-Model Migration section, item #2).
    STATUS (PX-20260824-04, 2026-08-24 live smoke run): this figure is
    STALE -- re-measured at 0 for all years by that run (byte-identical
    before/after snapshots; already repaired by intervening rekey/rollup
    work, not a fresh fix). See KNOWN_LIMITATIONS.md's "2026-08-24: G4/G5
    residual baselines above are STALE" entry for the full correction and
    its own sandbox-vs-live disclosure. A nonzero gap here today should be
    treated as a genuinely new finding, not assumed to be this old,
    already-resolved cause.
    """
    passed = distinct_geo_ids_in_units == geo_id_count_in_parcel
    detail = f"prop_unit_tax_year distinct geo_ids: {distinct_geo_ids_in_units:,}  parcel_tax_year rows: {geo_id_count_in_parcel:,}"
    if not passed:
        detail += f"  MISMATCH ({geo_id_count_in_parcel - distinct_geo_ids_in_units:+,})"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# G6 — external reconciliation (pure decision; the one banded check)
# ══════════════════════════════════════════════════════════════════════
def g6_external_reconciliation_check(computed_total, published_total, warn_pct=0.05, fail_pct=0.08):
    """
    Banded per spec §4.1/§4.2 ("±5-8%"). Interpreted here as two
    thresholds, not one — this is a judgment call made explicit rather
    than silently picking one number: deviation <= warn_pct (5%) is a
    clean pass; between warn_pct and fail_pct (5-8%) still PASSES (an
    external, differently-scoped total is expected to drift some) but is
    flagged 'warn' in the detail/level so a human reviewing ingest_audit
    notices it; beyond fail_pct (>8%) FAILS outright. If Diego intended a
    single fixed threshold instead, that's a one-line change to how
    `level` is computed below.

    published_total of 0 or None short-circuits to a fail (can't
    reconcile against nothing) rather than raising a ZeroDivisionError.
    """
    if not published_total:
        return False, "no published_total supplied — cannot reconcile", "fail"

    deviation = abs(computed_total - published_total) / published_total
    if deviation <= warn_pct:
        level = "ok"
    elif deviation <= fail_pct:
        level = "warn"
    else:
        level = "fail"

    passed = level in ("ok", "warn")
    detail = (f"computed=${computed_total:,.2f}  published=${published_total:,.2f}  "
              f"deviation={deviation*100:.2f}%  level={level}")
    return passed, detail, level


# ══════════════════════════════════════════════════════════════════════
# DB-facing gather/orchestration (production code path — requires a live
# conn; NOT exercised in this sandbox — see test file's AC8 disclosure)
# ══════════════════════════════════════════════════════════════════════
def gather_and_run(conn, source_tag, tax_year, prop_path=None, prop_ent_path=None,
                    published_total=None, county_code="TRAVIS", data_source=None):
    """
    Full production entry point: scans the source files for G1/G2/G3,
    queries prop_unit/prop_unit_tax_year/parcel/parcel_tax_year for the
    rest, runs all seven checks (G1_prop, G1_prop_ent, G2, G3, G3_rollup,
    G4, G5, G6 -- eight, actually, counting both G1 halves), writes one
    ingest_audit row per check, and returns an overall summary dict.
    Requires a live psycopg2 connection — this function itself is not
    fixture-tested (see AC8 disclosure); the g*_check() decision functions
    it calls ARE.

    data_source: PX-20260824-04. The literal `data_source` column value
        THIS run's rows carry in prop_unit_tax_year -- used to scope G2/G3's
        landed/unit-sum queries to just this run's own rows (see those
        queries' own comments below for why an unscoped query was a real,
        structural bug for historical years). Defaults to `source_tag` if
        not given, which is ONLY correct when source_tag IS the literal
        column value -- true for load_certified_historical.py's calls
        (source_tag=f"cert_{year}", written verbatim as data_source). It is
        NOT true for run_all.py's two calls: source_tag="certified_2025"/
        "preliminary_2026" are ingest_audit LABELS, while the real column
        values load_certified_2025.py/load_2026_preliminary.py write are
        "certified"/"preliminary" (their own DATA_SRC constants) -- a real
        landmine found while building this fix. Those callers MUST pass
        data_source explicitly; run_all.py has been updated accordingly.
    """
    results = {}

    prop_ledger = None
    ent_ledger = None
    if prop_path:
        prop_ledger = scan_prop_ledger(prop_path)
        results["G1_prop"] = g1_conservation_check(prop_ledger)
    if prop_ent_path:
        ent_ledger = scan_prop_ent_ledger(prop_ent_path)
        results["G1_prop_ent"] = g1_conservation_check(ent_ledger)

    # PX-20260824-04: the real data_source column value this run's rows
    # carry -- see this function's own docstring for the run_all.py
    # landmine that makes `source_tag` unsafe as a silent default for
    # every caller (safe only for load_certified_historical.py's calls).
    effective_data_source = data_source if data_source is not None else source_tag

    # PX-20260824-04: expected_gap/no_geo_dollars are computed PURELY from
    # the two file scans above -- zero DB access -- so they're a real,
    # re-derived-every-run cross-check, not a trusted constant. See
    # g2_identity_coverage_check()'s and g3_rollup_residual_check()'s own
    # docstrings for the full mechanism: a prop_id can carry entity/tax
    # data in PROP_ENT.TXT while being excluded from PROP.TXT's "accepted"
    # bucket (supplement row, blank geo_id, etc) -- that prop_id's row
    # still lands in prop_unit_tax_year (geo_id=NULL), so it's expected
    # to appear as a gap in G2's count and a residual in G3_rollup's
    # dollar comparison, not a real conservation failure.
    known_prop_ids = prop_ledger["prop_ids"] if prop_ledger is not None else set()
    expected_gap = (len(ent_ledger["prop_ids"] - known_prop_ids)
                     if ent_ledger is not None and prop_ledger is not None else 0)

    with conn.cursor() as cur:
        # G2 fix (task M3-G2-FIX): the old `SELECT COUNT(*) FROM prop_unit`
        # here was unscoped by year -- prop_unit accumulates one row per
        # prop_id ever seen across every year ever loaded (2025 certified +
        # 2021-2024 AJR + eventually 2022-2024 certified historical + 2026
        # preliminary), so it could never match a single year's file scan.
        # Folded the replacement COUNT(DISTINCT prop_id) into this existing
        # tax_year-scoped query (rather than firing a second one) since
        # G3 already queries prop_unit_tax_year WHERE tax_year = %s.
        #
        # Task M5-PERYEAR-GEOID: checked this query and G3's below against
        # its own docstring per the brief's explicit instruction not to
        # assume identical treatment -- NEITHER needs a change. G2 counts
        # prop_ids (COUNT(DISTINCT prop_id)) and G3 sums market_value by
        # prop_id/tax_year; neither one ever references geo_id at all, so
        # the per-year-geo_id bug (old years using a later year's account
        # number) cannot affect either check. Only G4 (which explicitly
        # groups by geo_id, re-deriving parcel_rollup's own aggregation)
        # and G5 (which derives its geo_id count from G4's gathered rows)
        # are touched below.
        # PX-20260824-03: county_code added to both queries below. [...]
        # (see git history for the full original comment -- unchanged
        # reasoning, still applies alongside the PX-20260824-04 fix
        # immediately below.)
        #
        # PX-20260824-04: `AND data_source = %s` added to the unit-table
        # query -- this is the actual fix Task 1 asked for. Before this,
        # the query was scoped by tax_year + county_code only, which is
        # equivalent to "every row for this tax_year, from EVERY source
        # that has ever written one" -- true assurance only for 2025/2026,
        # where exactly one data_source ever writes that tax_year's rows.
        # For historical years, `ajr_2022` and `cert_2022` (etc) can BOTH
        # have live rows for tax_year=2022 at once (a cert load doesn't
        # delete pre-existing AJR-sourced rows for prop_ids its own
        # PROP_ENT.TXT never mentions) -- an unscoped query would silently
        # count both sources together and compare that combined total
        # against ONE source's own file scan, which is not the claim G2/G3
        # are supposed to be making ("did THIS file land completely").
        # account_table_sum (parcel_tax_year, below) is deliberately left
        # UNSCOPED by data_source -- it's inherently a whole-year, rolled-
        # up, cross-source view (see g3_rollup_residual_check()'s own
        # docstring for why that's correct, not an oversight).
        cur.execute(
            "SELECT SUM(market_value), COUNT(DISTINCT prop_id) "
            "FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s AND data_source = %s",
            (tax_year, county_code, effective_data_source),
        )
        unit_table_sum, g2_landed_count = cur.fetchone()

        # PX-20260824-04: a SECOND, deliberately UNSCOPED query, used only
        # by G3_rollup below -- comparing a per-source unit sum against the
        # whole-year, cross-source parcel_tax_year total is not a
        # well-defined claim once more than one data_source has rows for
        # this tax_year (found and fixed during this task's own fixture
        # testing -- a multi-source scenario made a per-source comparison
        # go negative and fail nonsensically). This is literally the query
        # that was here before this fix -- revived specifically for the
        # one check where "whole-year" genuinely is the right scope on
        # BOTH sides. See g3_rollup_residual_check()'s own docstring.
        cur.execute(
            "SELECT SUM(market_value) "
            "FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",
            (tax_year, county_code),
        )
        whole_year_unit_sum = cur.fetchone()[0]

        cur.execute(
            "SELECT SUM(market_value) FROM parcel_tax_year WHERE tax_year = %s AND county_code = %s",
            (tax_year, county_code),
        )
        account_table_sum = cur.fetchone()[0]

        # G4 inputs: independently re-derive the rollup from prop_unit_tax_year
        # and diff it against what's actually stored in parcel_tax_year.
        #
        # Task M5-PERYEAR-GEOID: this used to JOIN prop_unit u ON u.prop_id =
        # y.prop_id and select ONLY u.geo_id (prop_unit's latest-known
        # assignment) -- matching the OLD parcel_rollup.py's ROLLUP_SQL,
        # which is exactly the mechanism this task fixes (old years' rollups
        # silently using a LATER year's account number). Now that
        # parcel_rollup.py itself groups by COALESCE(y.geo_id, u.geo_id)
        # (the row's own real per-year value, falling back to prop_unit only
        # when that's still NULL -- see parcel_rollup.py's ROLLUP_SQL comment
        # for the full NULL-fallback rationale), G4 must gather the SAME two
        # values and let parcel_rollup.compute_rollup() do the identical
        # COALESCE, so this independent re-derivation actually re-derives
        # what parcel_rollup.py now computes, not what it used to compute.
        # LEFT JOIN (not INNER) so a row with its own y.geo_id populated is
        # never dropped just because its prop_id has no prop_unit match.
        # PX-20260824-03: county_code added to the WHERE, AND to the JOIN
        # condition (u.county_code = y.county_code) -- matching
        # parcel_rollup.py's own ROLLUP_SQL fix under PARCEL-ROLLUP-HOTFIX-1
        # exactly. Without the join condition, a Travis prop_id that happens
        # to collide numerically with a Dallas prop_id (prop_id is only
        # unique WITHIN a county) could join against the WRONG county's
        # prop_unit row, silently corrupting this check's own independent
        # re-derivation of what parcel_tax_year should contain.
        cur.execute("""
            SELECT y.prop_id, y.geo_id AS geo_id, u.geo_id AS prop_unit_geo_id,
                   y.tax_year, y.market_value, y.assessed_value,
                   y.taxable_value, y.hs_cap_loss, y.land_value, y.imprv_value,
                   y.exemption_codes, y.data_source
            FROM prop_unit_tax_year y
            LEFT JOIN prop_unit u ON u.prop_id = y.prop_id AND u.county_code = y.county_code
            WHERE y.tax_year = %s AND y.county_code = %s
        """, (tax_year, county_code))
        g4_cols = [d[0] for d in cur.description]
        g4_unit_rows = [dict(zip(g4_cols, row)) for row in cur.fetchall()]

        cur.execute("""
            SELECT geo_id, market_value, assessed_value, taxable_value, hs_cap_loss,
                   land_value, imprv_value, exemption_codes, unit_count
            FROM parcel_tax_year
            WHERE tax_year = %s AND county_code = %s
        """, (tax_year, county_code))
        pty_cols = [d[0] for d in cur.description]
        g4_parcel_rows = {row[0]: dict(zip(pty_cols, row)) for row in cur.fetchall()}

        # G5 fix (task M3-G5-FIX): the old queries here were unscoped by
        # year (`SELECT COUNT(DISTINCT geo_id) FROM prop_unit` -- all years
        # ever loaded) AND queried the wrong table on the right side
        # (`parcel`, the year-independent master reference table, instead
        # of `parcel_tax_year`, the year-scoped rollup table G3/G4 already
        # use) -- confirmed live tonight: G5 printed the identical number
        # for every tax_year passed to --check-db, proving it was blind to
        # year entirely. Fixed by deriving both sides from the SAME
        # tax_year-scoped result sets G4 already fetched above, in Python,
        # rather than firing two more near-identical queries:
        #   - distinct_geo_ids: distinct EFFECTIVE geo_id values (this
        #     year's own prop_unit_tax_year.geo_id, falling back to
        #     prop_unit.geo_id only when that's NULL) across g4_unit_rows.
        #     Task M5-PERYEAR-GEOID: updated to use the same
        #     COALESCE(y.geo_id, u.geo_id) effective value G4/ROLLUP_SQL
        #     now use, rather than the raw prop_unit-joined geo_id alone --
        #     using the un-coalesced y.geo_id here would incorrectly count
        #     legacy NULL (not-yet-backfilled) rows as "no geo_id" even
        #     though they resolve to a real one via the fallback, and would
        #     disagree with what G4 just verified against parcel_tax_year.
        #     Rows where BOTH are None are excluded entirely, matching
        #     ROLLUP_SQL's WHERE ... IS NOT NULL filter (they never make it
        #     into parcel_tax_year, so they shouldn't count here either).
        #   - parcel_count: g4_parcel_rows is already a {geo_id: row} dict
        #     built from `parcel_tax_year WHERE tax_year = %s` -- its
        #     length is exactly `SELECT COUNT(*) FROM parcel_tax_year
        #     WHERE tax_year = %s` (one row per geo_id per year, the same
        #     one-row-per-geo_id-per-year assumption G4 already relies on).
        distinct_geo_ids = len({
            (row["geo_id"] if row["geo_id"] is not None else row["prop_unit_geo_id"])
            for row in g4_unit_rows
            if row["geo_id"] is not None or row["prop_unit_geo_id"] is not None
        })
        parcel_count = len(g4_parcel_rows)

    if prop_path:
        results["G2"] = g2_identity_coverage_check(
            len(prop_ledger["prop_ids"]), g2_landed_count, expected_gap
        )

    results["G4"] = g4_rollup_integrity_check(g4_unit_rows, g4_parcel_rows, tax_year)

    # PX-20260824-04: file_sum and no_geo_dollars are computed in ONE
    # combined pass over iter_prop_ent_aggregates(), not two separate
    # scans -- this loader's own module comment already discloses the
    # accepted "re-scan the source file a second time" tradeoff (once for
    # loading, once for the gate); a THIRD pass just to compute
    # no_geo_dollars separately would needlessly compound that cost.
    # no_geo_dollars: sum of market_value for every PROP_ENT.TXT aggregate
    # whose prop_id is absent from PROP.TXT's accepted set -- the same
    # population g2_identity_coverage_check's expected_gap counts, just
    # summed in dollars instead of counted in rows. This is the real,
    # re-derived-every-run answer to "where do the no-geo rows' dollars
    # go, and how much do they carry" -- not a hardcoded pass-through of
    # a previously-observed figure (e.g. the $774,939,443 2022 number
    # PX-20260824-04 investigated).
    file_sum = None
    no_geo_dollars = 0
    if prop_ent_path:
        running_sum = 0
        any_value = False
        for agg in ears_format.iter_prop_ent_aggregates(prop_ent_path):
            mv = agg["market_value"]
            if mv is None:
                continue
            running_sum += mv
            any_value = True
            # PX-20260824-04: only attribute a dollar to the no-geo
            # residual if we actually HAVE a PROP.TXT scan to check
            # membership against -- with prop_path absent, `known_prop_ids`
            # is an empty set (see its own definition above), which would
            # otherwise misclassify every single row as "no geo" rather
            # than honestly reporting "can't compute this residual without
            # a PROP.TXT scan." Matches expected_gap's own prop_ledger-is-None
            # guard, same limitation, same reasoning.
            if prop_ledger is not None and agg["prop_id"] not in known_prop_ids:
                no_geo_dollars += mv
        file_sum = running_sum if any_value else None
    results["G3"] = g3_dollar_conservation_check(file_sum, unit_table_sum)
    results["G3_rollup"] = g3_rollup_residual_check(whole_year_unit_sum, account_table_sum, no_geo_dollars)

    results["G5"] = g5_account_coverage_check(distinct_geo_ids, parcel_count)

    # PX-20260824-03: G6 now ALWAYS appears in results/ingest_audit, even
    # when no published_total is available -- previously it was simply
    # omitted from `results` in that case, which meant a caller reading the
    # per-check printout (or ingest_audit afterward) saw 5 checks and had
    # no way to tell "G6 wasn't run" apart from "G6 wasn't relevant" or an
    # oversight. This matters most for historical-year runs specifically:
    # TCAD's published county total is readily available for the current
    # year (2026's real load DID pass one -- see CHANGELOG's 1.5.0 entry,
    # "5.45% deviation vs. 10.61%") but nothing in this repo has an
    # on-file published total for 2022-2024 -- SKIPPED here is an honest,
    # loud statement of that gap, not a silent absence. passed=True so a
    # skip never blocks compute_metrics (matching this function's existing
    # `overall_pass = all(r[0] for r in results.values())` semantics) --
    # only a genuine reconciliation FAILURE should ever block downstream
    # steps, not "we didn't have a number to check against."
    if published_total is not None:
        results["G6"] = g6_external_reconciliation_check(account_table_sum or 0, published_total)
    else:
        results["G6"] = (
            True,
            f"SKIPPED — no published_total supplied for tax_year {tax_year}. "
            f"TCAD's published county total for this year was not available to "
            f"this run. G6 provides no assurance for this source/year until a "
            f"published total is sourced and passed explicitly via "
            f"--published-total.",
            "skipped",
        )

    _write_audit(conn, source_tag, tax_year, results, county_code=county_code)
    overall_pass = all(r[0] for r in results.values())
    return {"passed": overall_pass, "checks": results}


def _write_audit(conn, source_tag, tax_year, results, county_code="TRAVIS"):
    # BILLING-GATE-HOTFIX-1: county_code added -- ingest_audit's real,
    # live county_code column is NOT NULL (added by the county-
    # partitioning migration), and this shared function (used by both
    # ingest_gate.py's own appraisal-side gate and billing_gate.py) never
    # threaded it through, on either side. Default "TRAVIS" matches this
    # codebase's established DEFAULT_COUNTY convention for every other
    # not-yet-multi-county-aware call site.
    rows = []
    for check_code, result in results.items():
        passed, detail = result[0], result[1]
        rows.append((source_tag, tax_year, check_code, passed, detail, county_code))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest_audit (source_tag, tax_year, check_code, passed, detail, county_code) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
    conn.commit()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prop", help="Path to PROP.TXT (runs G1 on it, and G2 if --check-db)")
    ap.add_argument("--prop-ent", help="Path to PROP_ENT.TXT (runs G1 on it, and G3 if --check-db)")
    ap.add_argument("--source-tag", default="manual_scan")
    ap.add_argument("--tax-year", type=int, default=None)
    ap.add_argument("--check-db", action="store_true", help="Also run G2/G3/G5 against the live DB")
    ap.add_argument("--published-total", type=float, default=None, help="TCAD-published total for G6")
    ap.add_argument(
        "--county", default="TRAVIS",
        help="county_code every DB-facing check is scoped to (default: TRAVIS). "
             "PX-20260824-03: mirrors load_certified_historical.py's own --county "
             "convention -- was previously accepted by gather_and_run() but not "
             "exposed here, and not threaded into any of that function's own "
             "gathering queries either (see gather_and_run()'s own PX-20260824-03 "
             "comment for the fix).",
    )
    ap.add_argument(
        "--data-source", default=None,
        help="PX-20260824-04. The literal data_source column value to scope G2/G3's "
             "landed/unit-sum queries to (defaults to --source-tag if omitted -- see "
             "gather_and_run()'s own docstring for exactly when that default is UNSAFE: "
             "e.g. for the real 'certified_2025'/'preliminary_2026' run_all.py source "
             "tags, the actual column value is 'certified'/'preliminary', a different "
             "string). Get this wrong and G2/G3 will report a false, misleading gap.",
    )
    args = ap.parse_args()

    if not args.check_db:
        if args.prop:
            ledger = scan_prop_ledger(args.prop)
            passed, detail = g1_conservation_check(ledger)
            print(f"G1 (PROP.TXT): {'PASS' if passed else 'FAIL'} — {detail}")
            print(f"  buckets: {ledger['buckets']}")
        if args.prop_ent:
            ledger = scan_prop_ent_ledger(args.prop_ent)
            passed, detail = g1_conservation_check(ledger)
            print(f"G1 (PROP_ENT.TXT): {'PASS' if passed else 'FAIL'} — {detail}")
            print(f"  buckets: {ledger['buckets']}")
    else:
        from loaders.db import get_conn
        conn = get_conn()
        summary = gather_and_run(conn, args.source_tag, args.tax_year, args.prop, args.prop_ent,
                                  args.published_total, county_code=args.county,
                                  data_source=args.data_source)
        for code, result in summary["checks"].items():
            print(f"{code}: {'PASS' if result[0] else 'FAIL'} — {result[1]}")
        print(f"\nOVERALL: {'PASS' if summary['passed'] else 'FAIL'}")
        conn.close()
