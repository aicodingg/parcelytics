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
def g2_identity_coverage_check(file_prop_id_count, db_landed_count):
    """
    PASS iff the number of distinct prop_ids the source file scan says
    should exist for THIS tax_year exactly equals the number that actually
    landed a prop_unit_tax_year row for that same tax_year.

    Fixed 2026-07-29 (task M3-G2-FIX): db_landed_count must be a
    tax-year-scoped count (e.g. `SELECT COUNT(DISTINCT prop_id) FROM
    prop_unit_tax_year WHERE tax_year = %s`), NOT an unscoped `SELECT
    COUNT(*) FROM prop_unit` (which accumulates one row per prop_id ever
    seen across every year ever loaded, and will never match a single
    year's file scan -- confirmed against the live DB: 518,894 all-time
    prop_unit rows vs 449,290 distinct prop_ids in the 2025 file, a
    69,604 gap explained 100% by scope, not a real data problem). This
    function itself didn't change -- it was always a correct, generic
    count-equality check; the bug was entirely in what count the caller
    supplied as the second argument.
    """
    passed = file_prop_id_count == db_landed_count
    detail = f"file: {file_prop_id_count:,} distinct prop_ids, landed (this tax_year): {db_landed_count:,} rows"
    if not passed:
        detail += f"  MISMATCH ({db_landed_count - file_prop_id_count:+,})"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# G3 — dollar conservation (pure decision)
# ══════════════════════════════════════════════════════════════════════
def g3_dollar_conservation_check(file_sum, unit_table_sum, account_table_sum):
    """
    All three must match EXACTLY. file_sum/unit_table_sum/account_table_sum
    may each individually be None (meaning "no non-null dollar values at
    all in that source") — None is treated as a valid, comparable value
    (None == None passes; None vs a real number fails, same as any other
    mismatch), matching SQL SUM()'s own all-NULL-returns-NULL semantics.
    """
    passed = file_sum == unit_table_sum == account_table_sum
    detail = f"file=${_fmt(file_sum)}  unit_table=${_fmt(unit_table_sum)}  account_table=${_fmt(account_table_sum)}"
    if not passed:
        detail += "  MISMATCH"
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

    Known, already-documented residual even after this fix: ~3,119/year
    orphaned "P-type" prop_unit_tax_year rows have no matching prop_unit
    row (see KNOWN_LIMITATIONS.md), so they're never counted on the left
    side either way; any stale parcel_tax_year rows without current unit
    data would inflate the right side. A small nonzero gap post-fix is
    expected, not proof the fix didn't work -- compare its size against
    this documented cause before treating it as new.
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
                    published_total=None, county_code="TRAVIS"):
    """
    Full production entry point: scans the source files for G1/G2/G3,
    queries prop_unit/prop_unit_tax_year/parcel/parcel_tax_year for the
    rest, runs all six checks, writes one ingest_audit row per check, and
    returns an overall summary dict. Requires a live psycopg2 connection
    — this function itself is not fixture-tested (see AC8 disclosure);
    the g*_check() decision functions it calls ARE.
    """
    results = {}

    if prop_path:
        prop_ledger = scan_prop_ledger(prop_path)
        results["G1_prop"] = g1_conservation_check(prop_ledger)
    if prop_ent_path:
        ent_ledger = scan_prop_ent_ledger(prop_ent_path)
        results["G1_prop_ent"] = g1_conservation_check(ent_ledger)

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
        # PX-20260824-03: county_code added to both queries below. Before
        # this fix, gather_and_run() accepted a county_code parameter (used
        # only to LABEL the ingest_audit row -- see _write_audit() below)
        # but never actually filtered any of its own gathering queries by
        # it, despite prop_unit_tax_year/parcel_tax_year both being real,
        # already-migrated, county_code-leading-PK tables in production
        # (migrate_county_partitioning.py's own TABLE_SPECS) -- the exact
        # same bug class PARCEL-ROLLUP-HOTFIX-1 already found and fixed in
        # parcel_rollup.py's ROLLUP_SQL. Harmless today (Travis is still
        # the only county with rows in these tables, so an unscoped COUNT/
        # SUM equals the scoped version), but confirmed as a real,
        # load-bearing gap: prop_id is NOT globally unique across counties
        # (prop_unit's real PK is (county_code, prop_id)), so the day a
        # second county's data lands here, an unscoped COUNT(DISTINCT
        # prop_id) would count both counties' prop_ids together and
        # compare it against one county's file-scan count -- a false FAIL.
        cur.execute(
            "SELECT SUM(market_value), COUNT(DISTINCT prop_id) "
            "FROM prop_unit_tax_year WHERE tax_year = %s AND county_code = %s",
            (tax_year, county_code),
        )
        unit_table_sum, g2_landed_count = cur.fetchone()
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
        results["G2"] = g2_identity_coverage_check(len(prop_ledger["prop_ids"]), g2_landed_count)

    results["G4"] = g4_rollup_integrity_check(g4_unit_rows, g4_parcel_rows, tax_year)

    file_sum = None
    if prop_ent_path:
        file_sum = sum(
            agg["market_value"] for agg in ears_format.iter_prop_ent_aggregates(prop_ent_path)
            if agg["market_value"] is not None
        ) or None
    results["G3"] = g3_dollar_conservation_check(file_sum, unit_table_sum, account_table_sum)

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
                                  args.published_total, county_code=args.county)
        for code, result in summary["checks"].items():
            print(f"{code}: {'PASS' if result[0] else 'FAIL'} — {result[1]}")
        print(f"\nOVERALL: {'PASS' if summary['passed'] else 'FAIL'}")
        conn.close()
