"""
compute_metrics.py — Phase 2 computed insight layer.

Populates parcel_metrics and county_benchmark from Phase 1 source data.
Run after any data load that updates parcel_tax_year or tax_billing.

Usage:
    python3 loaders/compute_metrics.py            # full compute + brief analysis
    python3 loaders/compute_metrics.py --analyze  # distribution analysis only, no compute

Data Integrity Standard (Part 2 — binding for all phases):
  - NULL in a parcel_metrics field = "Not Available" — never a zero or blank
  - coverage_level = 'full'       → real, VERIFIED billing on file for that year
                                     (tax_billing.confidence_level = 'verified')
  - coverage_level = 'value_only' → market + assessed only; that year's billing
                                     is missing, derived/reconstructed, or a
                                     portal-scrape partial receipt
  - has_tax_data = FALSE          → never show tax metrics for that row in UI

  Real fix (July 2026, per Diego's brief — "Property Page Small Bugs Batch"
  item 3): coverage_level used to be a pure `tax_year = 2025` check, unaware
  of billing confidence -- unconditionally 'full' for a 2025 row even when
  that row's total_tax was a derived/reconstructed sum or a portal-scrape
  partial receipt, not a genuinely confirmed figure. This was masked at the
  template layer (templates/property.html's Growth & Assessment Metrics
  coverage badge cross-checked r.is_billing_verified for the 2025 row only,
  ahead of trusting coverage_level) rather than fixed at the source. Now that
  tax_billing.data_source/confidence_level are reliably populated at write
  time (this session's earlier fix) for EVERY year that has billing (2025's
  current-year loader, and 2021-2024's PIR loaders), coverage_level is
  computed directly from confidence_level = 'verified' below -- correct for
  any year, not special-cased to 2025 -- and the template-layer patch has
  been removed accordingly (see property.html's Growth & Assessment Metrics
  card). See update_coverage_level() in load_pir_billing.py for the matching
  fix on the "billing loaded after the fact" path -- it had the identical
  gap (flipped coverage_level to 'full' whenever ANY tax_billing row existed
  for that year, not just a verified one) and needed the same fix, or it
  would have silently re-introduced this bug the next time a PIR loader ran.
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, execute_schema
from tax_logic.classify import label_case_sql
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE
# PX-20260823-02: DEFAULT_COUNTY imported from the same real source every
# other county-aware loader uses (single source of truth), rather than a
# second, independent "TRAVIS" constant.
from loaders.scrape_billing_history import DEFAULT_COUNTY
import psycopg2.extras

COMPUTATION_VERSION = "2.0"

# ── Row-count sanity floor ───────────────────────────────────────────────────────
# Absolute backstop for the "silently far fewer rows than expected" failure mode.
# Primary check is relative (new vs. previous row count, see _assert_row_count_sane);
# these are only the floor for a first-ever run or if the previous count was itself 0.
#
# PX-20260831-02 Task 3: PARCEL_METRICS_ROW_FLOOR = 1_000_000 (a single hardcoded
# constant, retired below) was itself a Travis-tuned number -- comfortably below
# Travis's own ~2,796,316-row normal (508K parcels x ~5.5 years), but with NO
# relationship at all to any other county's real size. Confirmed dangerous the
# moment a second county exists: Dallas's real parcel_tax_year row count is
# 3,576,634 (measured live, 2026-08-31), so 1,000,000 happens to still clear for
# Dallas today -- but a THIRD, smaller county (or an early partial Dallas load)
# could easily produce a genuinely healthy row count under 1,000,000 that this
# constant would have rejected as a false failure, while simultaneously being
# FAR too permissive a floor for a truly broken join on a large county (a JOIN
# bug that silently drops 90% of a 3.5M-row county still clears "over 1,000,000"
# with room to spare). A single global constant cannot be both tight enough to
# catch real failures and loose enough to never false-positive across counties
# of very different sizes -- only a per-county-derived floor can be.
#
# Replaced with a per-county floor computed fresh inside compute_parcel_metrics()
# itself, in the SAME transaction, before the INSERT: half of that county's own
# parcel_tax_year row count (see _parcel_metrics_row_floor() below). Since this
# INSERT's SELECT is one row per (pty.geo_id, pty.tax_year) row scoped to this
# county (the LEFT JOINs to tax_billing/tax_delinquent can only leave that row
# count unchanged or produce NULLs, never multiply/reduce it), parcel_metrics'
# real row count for a healthy run is very close to 1:1 with parcel_tax_year's
# own count for that county -- 0.5x is a generous floor that only fires on a
# genuine catastrophic failure (a join/WHERE bug cutting the result in half or
# worse) while comfortably tolerating ordinary attrition (a parcel excluded for
# a data-quality reason, etc.). This is the pattern, not a Dallas-specific
# unblock -- Dallas's own 3,576,634-row parcel_tax_year count already clears the
# OLD 1,000,000 constant on its own; the point is that the NEXT county, whatever
# size it is, gets a floor sized to ITS OWN data, automatically, with no manual
# re-tuning required.
#
# county_benchmark is 5 TYPE_GROUPS x ~5 years each = ~25 rows; 15 survives losing a
# year of coverage on a couple of categories without masking a real failure. (Not
# touched by this task -- county_benchmark's own row shape doesn't scale with
# county size the way parcel_metrics does, so the per-county-derivation argument
# above doesn't apply to it the same way; out of this brief's scope.)
COUNTY_BENCHMARK_ROW_FLOOR = 15
# Relative tolerance: a rebuild producing fewer than this fraction of the previous
# row count is treated as a failure, not a quirk of the source data.
ROW_COUNT_TOLERANCE = 0.95


class MetricsIntegrityError(RuntimeError):
    """Raised when a rebuild step produces a suspiciously low row count.

    This is the fix for the "silent" failure mode flagged in
    COMPUTE_METRICS_CURRENCY_REPORT_2026-06-30.md: a bug that matches far fewer
    rows than it should previously completed with no error and a quietly-wrong
    row count. Raising here makes that loud instead — combined with the
    transaction-per-table rebuild (see compute_parcel_metrics /
    compute_county_benchmarks), the failure also leaves the table in its prior
    state rather than committing the short rebuild.
    """


def _assert_row_count_sane(label, new_count, prev_count, hard_floor, tolerance=ROW_COUNT_TOLERANCE):
    """Fail loudly if a rebuild produced a suspiciously low row count.

    Primary check: new_count vs. prev_count (self-adjusting as the county's
    parcel/year coverage naturally grows over time). Secondary check: an
    absolute hard_floor, which is what catches a first-ever run (prev_count
    == 0) or guards against prev_count having itself already been wrong.
    """
    if prev_count > 0 and new_count < prev_count * tolerance:
        drop_pct = (1 - new_count / prev_count) * 100
        raise MetricsIntegrityError(
            f"{label}: rebuild produced {new_count:,} rows, down from {prev_count:,} "
            f"({drop_pct:.1f}% drop) — exceeds the {(1 - tolerance) * 100:.0f}% "
            f"tolerance. Treating this as a failure, not a successful rebuild."
        )
    if new_count < hard_floor:
        raise MetricsIntegrityError(
            f"{label}: rebuild produced {new_count:,} rows, below the absolute "
            f"floor of {hard_floor:,}. Treating this as a failure, not a "
            f"successful rebuild."
        )
    print(f"    row-count check OK: {label} = {new_count:,} rows "
          f"(prev {prev_count:,}, floor {hard_floor:,})")


def _parcel_metrics_row_floor(conn, county_code, fraction=0.5):
    """PX-20260831-02 Task 3: per-county derived hard floor for
    _assert_row_count_sane()'s parcel_metrics check, replacing the retired
    PARCEL_METRICS_ROW_FLOOR module constant (see that name's own retirement
    comment above for the full reasoning). Computed fresh, in the SAME
    transaction as compute_parcel_metrics()'s DELETE+INSERT (called before
    the INSERT below), so it always reflects that county's CURRENT
    parcel_tax_year row count, not a number someone hardcoded once and never
    revisited as the data grew.

    fraction=0.5 is the proposed default: parcel_metrics' INSERT is one row
    per (geo_id, tax_year) pair for this county (the tax_billing/
    tax_delinquent LEFT JOINs can only preserve or NULL-pad that count, never
    multiply or shrink it), so a healthy run's real row count sits very close
    to 1:1 with parcel_tax_year's own count -- half of that is a floor that
    only fires on a genuine catastrophic failure (a join/WHERE bug cutting
    the result by 50%+) while tolerating ordinary attrition.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM parcel_tax_year WHERE county_code = %s", (county_code,))
        source_count = cur.fetchone()[0]
    return int(fraction * source_count)


# ── Risk threshold ──────────────────────────────────────────────────────────────
# PX-20260831-02 Task 4: LARGE_JUMP_THRESHOLD_PCT = 75.0 (a single hardcoded
# constant, retired below) was measured ONLY against Travis's distribution and
# applied identically to every county regardless of that county's own
# yoy_market_value_pct shape. Confirmed actively wrong for Dallas: Dallas's own
# measured distribution (analyze_threshold(county_code=DALLAS), live,
# 2026-08-31, 2,745,496 YoY parcel-year pairs) is structurally different from
# Travis's --
#
#   TRAVIS (original measurement, 1,401,316 pairs):
#     p50=7.1%   p75=32.3%  p90=59.4%  p95=72.0%  p99=292.9%
#     >50%: 15.9%   >75%: 4.5%   >100%: 2.6%
#
#   DALLAS (live, 2026-08-31, 2,745,496 pairs):
#     p50=0.00%  p75=11.91% p90=27.53% p95=41.77% p99=167.59%
#     Min -100.0%  Max 878960.0%  Avg 33.12%
#     >10%: 30.1%  >20%: 16.0%  >30%: 9.0%  >40%: 5.5%  >50%: 3.8%
#     >75%: 2.2%   >100%: 1.5%
#
# Applying Travis's 75.0 to Dallas would flag only 2.2% of Dallas's pairs --
# under-detecting by roughly half relative to Travis's own ~4.5% flag rate.
# Dallas's median of exactly 0.00% is a real, disclosed data characteristic
# (DCAD carries values forward on non-reappraised parcels — see the Dallas
# metrics runbook's R3 section), not a defect in this measurement or an
# argument for a different methodology; it does explain why Dallas's whole
# distribution sits well below Travis's at every percentile.
#
# Same methodology applied to Dallas as produced Travis's own 75.0: Travis's
# threshold sits ~3 points above Travis's own p95 (75.0 - 72.0 = 3.0). Applying
# that same ~3-point-above-p95 rule to Dallas's p95 (41.77) gives
# 41.77 + 3.23 ≈ 45.0. Checking the resulting flag rate against Dallas's own
# measured brackets: 45.0 falls between the >40% (5.5%) and >50% (3.8%) points;
# linear interpolation between them (45 is 50% of the way from 40 to 50) gives
# an estimated flag rate of 5.5% - 0.5*(5.5-3.8) ≈ 4.65% -- landing right next
# to Travis's own ~4.5% flag rate. That consistency (same TARGET flag rate
# across counties, not the same raw percentage) is the real justification for
# 45.0, not just "it happens to sit near p95" -- it reproduces the same
# investor-facing signal density Travis's 75.0 already proved out, on Dallas's
# own genuinely different value-change distribution. PM confirms before this
# value is used against production.
LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY = {
    "TRAVIS": 75.0,
    "DALLAS": 45.0,
}


def _large_jump_threshold_for_county(county_code):
    """No default, no fallback to Travis's value -- a county missing from
    LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY must fail loudly rather than silently
    borrow another county's threshold (which was the exact single-county-
    tuned-constant problem this whole task exists to retire). Per-county;
    measured at each county's first metrics run via --analyze; re-measured
    when a county's roll is refreshed.
    """
    try:
        return LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY[county_code]
    except KeyError:
        raise MetricsIntegrityError(
            f"No LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY entry for county_code={county_code!r}. "
            f"Run `--analyze --county {county_code}` to measure this county's own "
            f"yoy_market_value_pct distribution and register a threshold before running "
            f"metrics for it -- there is no default and no fallback to another county's value."
        )

# State code prefix → benchmark property type label
# Matches the display mapping used in property.html
TYPE_GROUPS = [
    (["A"],      "Residential",  "A"),
    (["B"],      "Multi-Family", "B"),
    (["C"],      "Land/Vacant",  "C"),
    (["D", "E"], "Agricultural", "D/E"),
    (["F"],      "Commercial",   "F"),
]

# State code prefixes excluded from benchmark aggregation.
# Based on query_state_cd1_prefixes.py analysis of 517,614 Travis CAD parcels:
#   X* (13,998) — tax-exempt accounts (churches, government, nonprofits; XV, XB,
#                 XU, XI, XJ, XR, XD, XG, XO, XL, XN, XA). Excluded because
#                 large preliminary-vs-certified swings on near-zero prior values
#                 produce meaningless benchmark statistics.
#   N*  (3)     — personal property accounts. Negligible count; excluded for
#                 correctness.
#
# IMPORTANT: this list is NOT the only thing that decides what lands in
# county_benchmark. The actual row filter below is `WHERE (label_expr) = %s`,
# where label_expr = label_case_sql() (tax_logic/classify.py) — the single
# canonical taxonomy shared with the rest of the app. A prefix being absent
# from BENCHMARK_EXCLUDE_PREFIXES does NOT mean it appears in the output; it
# only means it isn't excluded by *this* list. It still has to independently
# match one of label_case_sql()'s five real-estate WHEN clauses to produce a
# row at all. TYPE_GROUPS below never used its own `prefixes` list to filter
# either (that first tuple element is unused in the query — label_case_sql()
# does the actual matching) — so "prefix in TYPE_GROUPS" was never really the
# mechanism; don't read it as one when auditing this.
#
# Per query_state_cd1_prefixes.py's live count (kept vs. excluded, cross-
# checked against label_case_sql()'s actual behavior):
#   M* (10,699) — manufactured homes (treated as real property under TX law).
#                 label_case_sql() maps M -> Residential. Genuinely kept.
#   O* (19,986) — "Other" use-type parcels (real property with valid MV).
#                 NOT mapped in label_case_sql() (falls through to NULL) —
#                 never actually appears in any of the 5 TYPE_GROUPS output,
#                 despite not being on BENCHMARK_EXCLUDE_PREFIXES either.
#   J*  (1,524) — industrial / utility real property. Same as O*: NOT mapped
#                 in label_case_sql(), falls through to NULL, never appears
#                 in county_benchmark output. (A prior version of this
#                 comment called J* "kept" — that conflated "not on the
#                 exclude-list" with "appears in output"; those are not the
#                 same thing. J* was never actually producing a benchmark
#                 row.)
#   S*    (751) — state-assessed utility real property. Same as O*/J*: not
#                 mapped, never appears in output.
#   G*      (6) — government-assessed parcels (de minimis). Same as O*/J*/S*.
#   L* (~42,300 as of a July 2026 recount) — Personal Property (equipment,
#                 inventory, business personal property) per the Texas
#                 Comptroller's own state class code scheme, NOT commercial
#                 real estate. A prior version of this comment called L*
#                 "commercial real estate (already in Commercial TYPE_GROUP)"
#                 — that was wrong on the merits, not just a loader-bug
#                 symptom. As of the county_benchmark contamination
#                 investigation (see KNOWN_LIMITATIONS.md), label_case_sql()
#                 no longer maps 'L' to 'Commercial' at all — it now falls
#                 through to NULL like O*/J*/S*/G*, so L* never appears in
#                 county_benchmark output either. 99.5% of state_cd1='L' rows
#                 already carried a synthetic "AJR"-prefixed geo_id and were
#                 already excluded by the AJR-prefix leg of the canonical
#                 exclude clause (see _exclude_clause() below, now sourced
#                 from parcel_filters.py); the remaining ~0.5% (real,
#                 resolvable 10-digit geo_ids) are the ones this classify.py
#                 fix newly excludes.
#
# NULL state_cd1 (17,175 parcels, per the July 2026 recount) — CORRECTED
# (July 2026, "Fix parcel-exclusion filtering" brief): the claim this
# comment used to make here ("naturally excluded... but also can't match
# any label_case_sql() WHEN clause, so it never appears in output either")
# was WRONG, not just imprecise. label_case_sql() is classi_cd-FIRST — a
# NULL-state_cd1 parcel with a valid classi_cd (e.g. new-construction
# apartments built after the 2021-2024 AJR extract this field is sourced
# from) CAN independently match one of its five real-estate WHEN clauses.
# The actual mechanism dropping these rows was the exclude clause's own
# WHERE-level NULL propagation: `state_cd1 NOT LIKE 'X%'` evaluates to SQL
# NULL (not TRUE) when state_cd1 IS NULL, and Postgres's WHERE silently
# drops any row whose condition is NULL — before label_case_sql() ever ran.
# That's a real bug, not a benign double-exclusion, and it was live in
# every county-wide dollar/percentile total built on this exclude clause,
# not just this file's own output (the same un-centralized fragment was
# independently retyped in app.py three more times — see parcel_filters.py's
# module docstring for the full blast-radius writeup).
#
# BENCHMARK_EXCLUDE_PREFIXES / _exclude_clause() are retired as of this fix
# — this file now imports CANONICAL_PARCEL_EXCL_BARE from the repo-root
# parcel_filters.py, the single NULL-safe definition every consumer
# (this file, and every app.py route that needs the same scoping) shares,
# so this exclusion logic can't independently drift again the way it
# already had (app.py's /parcels route was found to have silently dropped
# the N% leg entirely before this fix).
def _exclude_clause():
    """SQL fragment excluding non-real-property state_cd1 prefixes AND
    AJR-prefixed business-personal-property geo_ids from benchmark queries.
    See parcel_filters.py for the canonical, NULL-safe definition this
    wraps -- kept as a function (not a bare import) so every call site
    below is unchanged."""
    return f"AND ({CANONICAL_PARCEL_EXCL_BARE})"


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _like_clause(prefixes):
    """Build a LIKE ANY(...) clause for state_cd1 prefix matching.
    Uses %% so the literal % survives psycopg2 parameter substitution
    when the result is interpolated into an f-string SQL."""
    patterns = ", ".join(f"'{p}%%'" for p in prefixes)
    return f"p.state_cd1 LIKE ANY(ARRAY[{patterns}])"


# ── Step 4: Threshold distribution analysis ─────────────────────────────────────
def analyze_threshold(conn, county_code):
    """PX-20260830-05 Task 3 (Bucket C): county_code is a REQUIRED parameter
    here, with no default -- unlike every other function in this file, a
    silent DEFAULT_COUNTY fallback would be actively wrong for this one.
    This is a per-county distribution report (the printed percentiles/flag
    counts describe ONE county's YoY value-change shape); parcel_tax_year is
    composite_pk-migrated (county_code-leading), and the self-join below
    would otherwise pool every loaded county's parcel-year pairs into one
    blended distribution, silently mislabeling it as if it were Travis's (or
    whichever single county the caller had in mind) once a second county has
    real data. Forcing every caller to pass county_code explicitly (see
    main()'s two call sites) makes that scoping decision visible at the call
    site instead of hidden behind a default.
    """
    print("\n" + "=" * 60)
    print(f"  Step 4 — Risk Threshold Distribution Analysis (county_code={county_code})")
    print("=" * 60)
    print("  Computing YoY market value changes across all parcel-years…")
    t0 = time.time()

    with conn.cursor() as cur:
        cur.execute("""
            WITH yoy AS (
                SELECT
                    a.geo_id,
                    a.tax_year,
                    CASE
                        WHEN b.market_value > 0
                        THEN ROUND(
                            100.0 * (a.market_value - b.market_value) / b.market_value, 2)
                        ELSE NULL
                    END AS yoy_pct
                FROM parcel_tax_year a
                JOIN parcel_tax_year b
                  ON b.county_code = a.county_code
                 AND b.geo_id = a.geo_id
                 AND b.tax_year = a.tax_year - 1
                WHERE a.county_code = %s AND a.market_value > 0 AND b.market_value > 0
            )
            SELECT
                COUNT(*)                                                             AS total_pairs,
                SUM(CASE WHEN ABS(yoy_pct) > 10  THEN 1 ELSE 0 END)                AS flag_10,
                SUM(CASE WHEN ABS(yoy_pct) > 20  THEN 1 ELSE 0 END)                AS flag_20,
                SUM(CASE WHEN ABS(yoy_pct) > 30  THEN 1 ELSE 0 END)                AS flag_30,
                SUM(CASE WHEN ABS(yoy_pct) > 40  THEN 1 ELSE 0 END)                AS flag_40,
                SUM(CASE WHEN ABS(yoy_pct) > 50  THEN 1 ELSE 0 END)                AS flag_50,
                SUM(CASE WHEN ABS(yoy_pct) > 75  THEN 1 ELSE 0 END)                AS flag_75,
                SUM(CASE WHEN ABS(yoy_pct) > 100 THEN 1 ELSE 0 END)                AS flag_100,
                ROUND(MIN(yoy_pct), 1)                                               AS min_yoy,
                ROUND(MAX(yoy_pct), 1)                                               AS max_yoy,
                ROUND(AVG(yoy_pct)::NUMERIC, 2)                                      AS avg_yoy,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p50,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p75,
                ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p90,
                ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p95,
                ROUND(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY yoy_pct)::NUMERIC, 2) AS p99
            FROM yoy
        """, (county_code,))
        r = cur.fetchone()

    total = r[0]
    print(f"\n  Total YoY parcel-year pairs:  {total:,}")
    print(f"\n  Distribution of YoY market value changes:")
    print(f"    Median (p50):  {r[11]}%")
    print(f"    p75:           {r[12]}%")
    print(f"    p90:           {r[13]}%")
    print(f"    p95:           {r[14]}%")
    print(f"    p99:           {r[15]}%")
    print(f"    Min:           {r[8]}%   Max: {r[9]}%   Avg: {r[10]}%")

    print(f"\n  Parcels flagged (|YoY| > threshold):")
    # PX-20260831-02 Task 4: this marker uses .get(), NOT
    # _large_jump_threshold_for_county() -- --analyze is the tool Diego runs
    # to MEASURE a county's distribution BEFORE a threshold is registered for
    # it in LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY, so an unregistered county
    # here is the expected, common case, not an error to raise on. Once a
    # value IS registered, the marker shows it against the measured
    # distribution exactly as before.
    registered_threshold = LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY.get(county_code)
    thresholds = [10, 20, 30, 40, 50, 75, 100]
    counts = [r[1], r[2], r[3], r[4], r[5], r[6], r[7]]
    for t, c in zip(thresholds, counts):
        pct_flagged = 100.0 * c / total if total else 0
        marker = "  ← current registered threshold" if t == registered_threshold else ""
        print(f"    > {t:3d}%:  {c:>8,}  ({pct_flagged:.1f}%){marker}")

    print(f"\n  Elapsed: {time.time()-t0:.1f}s")
    print("=" * 60)


# ── Step 2: Compute parcel_metrics ──────────────────────────────────────────────
def compute_parcel_metrics(conn, county_code=DEFAULT_COUNTY):
    print("\n[1] Computing parcel_metrics…")
    t0 = time.time()

    # Partial-write-window fix: DELETE and the full rebuild below run in ONE
    # transaction (no commit() until the very end of this function). If
    # anything raises before that final commit — including the row-count
    # sanity check — Postgres rolls back the DELETE along with everything
    # else, so a crash mid-run leaves parcel_metrics in its PRIOR state,
    # never empty. (Previously the DELETE committed immediately, so a crash
    # between that commit and the rebuild's commit left the table empty.)
    with conn.cursor() as cur:
        # PX-20260828-16-followup: scoped by county_code (diagnostic
        # prev_count is now per-county, matching the DELETE/rebuild below --
        # this used to silently report the GLOBAL row count, which would
        # have understated "before" once a second county's rows also lived
        # in this table).
        cur.execute("SELECT COUNT(*) FROM parcel_metrics WHERE county_code = %s", (county_code,))
        prev_count = cur.fetchone()[0]

    # PX-20260831-02 Task 3: per-county hard floor, computed fresh in this
    # SAME transaction (see _parcel_metrics_row_floor()'s own docstring for
    # the 0.5x reasoning) -- replaces the retired PARCEL_METRICS_ROW_FLOOR
    # module constant.
    row_floor = _parcel_metrics_row_floor(conn, county_code)

    with conn.cursor() as cur:
        # PX-20260828-16-followup: this DELETE and the INSERT...SELECT below
        # are now BOTH scoped to county_code, together, in the same fix --
        # exactly the "DELETE AND the INSERT's SELECT WHERE scoped TOGETHER
        # as one behavior change" the prior EXEMPT note (PX-20260823-02)
        # said this needed. Previously this ran a full-table rebuild across
        # every county on every invocation; the INSERT has no ON CONFLICT
        # (a genuine insert-only rebuild), so scoping only one side would
        # have made per-county reruns actively wrong (re-inserting every
        # OTHER county's rows on top of their own untouched data, a
        # duplicate-key failure). Diego's ruling: fix this now, as part of
        # unblocking Dallas metrics, rather than leave it as a disclosed
        # follow-up.
        cur.execute("DELETE FROM parcel_metrics WHERE county_code = %s", (county_code,))

    # Main insert: YoY metrics via SQL window functions
    # yoy_tax_amount_pct is NULL for all years — no historical billing exists yet
    # effective_tax_rate populated for 2025 only (real billing available)
    # DALLAS-GATE-4 family completion (PX-20260822-06-rev1): county_code
    # added to the column list, sourced directly from pty.county_code
    # (parcel_tax_year already carries it, written by every real writer in
    # this session's DALLAS-GATE-4/PARCEL-ROLLUP-HOTFIX-1 line of fixes).
    # Live PK for parcel_metrics is (county_code, geo_id, tax_year) with FK
    # (county_code, geo_id) -> parcel, confirmed via \d against production,
    # 2026-08-23 -- NOT reflected in this repo's schema.sql (a pre-existing
    # staleness gap, not something introduced by this fix; see PM's own
    # note that schema.sql's DEFAULT 'TRAVIS' at line 217 is likewise dead).
    # This INSERT has no ON CONFLICT clause at all -- genuinely correct by
    # design, not a gap. It's a full DELETE+INSERT...SELECT rebuild sharing
    # one transaction with the DELETE above (same pattern as
    # loaders/snapshot_2026_preliminary.py's own already-verified-correct
    # INSERT_SQL): the DELETE always runs first in the same transaction, so
    # no pre-existing row this INSERT could conflict against survives --
    # there is no unique-constraint target to have.
    # PX-20260828-16-followup: pty.county_code = %s added to the WHERE,
    # scoped TOGETHER with the DELETE above (see its comment) -- was
    # previously EXEMPT (PX-20260823-02) as a disclosed, coupled follow-up;
    # fixed now per Diego's ruling, as part of unblocking Dallas metrics.
    print("  → Pass 1 start (main INSERT)", flush=True)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO parcel_metrics (
                county_code, geo_id, tax_year,
                coverage_level, has_tax_data,
                yoy_market_value_pct,
                yoy_assessed_value_pct,
                yoy_tax_amount_pct,
                assessment_ratio,
                effective_tax_rate,
                effective_tax_rate_derived,
                risk_delinquent,
                risk_data_incomplete,
                computation_version
            )
            SELECT
                pty.county_code,
                pty.geo_id,
                pty.tax_year,

                -- coverage_level: real fix (see module docstring) -- driven by
                -- tb.confidence_level (already LEFT JOINed below), not tax_year.
                -- 'full' only when this year's billing is genuinely verified;
                -- everything else (no billing row, derived/reconstructed sum,
                -- portal-scrape partial receipt) is 'value_only', for any year.
                CASE WHEN tb.confidence_level = 'verified' THEN 'full' ELSE 'value_only' END,
                -- COALESCE required: has_tax_data is NOT NULL in schema, but
                -- `tb.confidence_level = 'verified'` evaluates to SQL NULL (not
                -- FALSE) whenever tb.confidence_level itself is NULL -- e.g. no
                -- matching tax_billing row at all via the LEFT JOIN below, or a
                -- billing row with no usable total (confidence_level IS NULL).
                -- Bare boolean would have raised a NOT NULL constraint violation
                -- on the very first such row -- caught in the isolated dry-run
                -- test before this was ever run for real (see verification notes).
                COALESCE(tb.confidence_level = 'verified', FALSE),

                -- YoY market value pct
                CASE
                    WHEN LAG(pty.market_value) OVER w > 0
                    THEN ROUND(
                        100.0 * (pty.market_value - LAG(pty.market_value) OVER w)
                        / LAG(pty.market_value) OVER w, 4)
                END,

                -- YoY assessed value pct
                CASE
                    WHEN LAG(pty.assessed_value) OVER w > 0
                    THEN ROUND(
                        100.0 * (pty.assessed_value - LAG(pty.assessed_value) OVER w)
                        / LAG(pty.assessed_value) OVER w, 4)
                END,

                -- yoy_tax_amount_pct: computed when prior-year billing exists;
                -- NULL (Not Available) when either year lacks billing data.
                -- Initially NULL for 2021-2024 (no historical billing yet);
                -- flips to real values after load_pir_billing.py runs.
                CASE
                    WHEN LAG(tb.total_tax) OVER w > 0
                    THEN ROUND(
                        100.0 * (tb.total_tax - LAG(tb.total_tax) OVER w)
                        / LAG(tb.total_tax) OVER w, 4)
                END,

                -- Assessment ratio: assessed / market
                -- NULL if market = 0 OR ratio > 100 (AJR bad-data rows where
                -- market_value is erroneously tiny produce ratios > 999 that
                -- overflow even NUMERIC(10,4); cap these as not meaningful).
                CASE
                    WHEN pty.market_value > 0
                     AND pty.assessed_value::NUMERIC / pty.market_value <= 100
                    THEN ROUND(pty.assessed_value::NUMERIC / pty.market_value, 4)
                END,

                -- Effective tax rate: real billing for 2025 only; Not Available otherwise.
                -- Uses SUM(amount_due) from tax_billing_entity rather than tax_billing.total_tax,
                -- because TOTAL_TAX in the TaxCurOpenData source is 0.00 for ~93%% of all 2025
                -- rows (confirmed by direct inspection of the raw CSV — not narrowly scoped to
                -- "some property types"; it's the majority of rows regardless of type), even
                -- when entity-level DUE amounts are correct. See KNOWN_LIMITATIONS.md.
                -- Cap at 1.0 (100%%) — values above that are bad data.
                -- PX-20260831-03 HOTFIX: percent signs above are now doubled (previously
                -- single, unescaped characters) -- this whole INSERT is executed WITH a
                -- params tuple, and psycopg2 substitutes over the ENTIRE string it is
                -- given, comments included. Any single, un-doubled percent character in
                -- this text (not just in real SQL) is unsafe once params are passed --
                -- see the PX-20260831-03 incident report for the full mechanism.
                CASE
                    WHEN pty.tax_year = 2025
                     AND pty.market_value > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id      = pty.geo_id
                           AND  tbe.tax_year    = 2025
                           AND  tbe.county_code = pty.county_code
                     ) > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id      = pty.geo_id
                           AND  tbe.tax_year    = 2025
                           AND  tbe.county_code = pty.county_code
                     )::NUMERIC / pty.market_value <= 1
                    THEN ROUND(
                        (
                            SELECT SUM(tbe.amount_due)
                            FROM   tax_billing_entity tbe
                            WHERE  tbe.geo_id      = pty.geo_id
                              AND  tbe.tax_year    = 2025
                              AND  tbe.county_code = pty.county_code
                        )::NUMERIC / pty.market_value,
                        6
                    )
                END,

                -- effective_tax_rate_derived (Effective Tax Rate KPI masking-bug fix,
                -- July 2026, per Diego): the CASE above always derives effective_tax_rate
                -- from SUM(tax_billing_entity.amount_due) -- it never uses tb.total_tax as
                -- the numerator, because TOTAL_TAX is blank for ~93%% of 2025 rows (see
                -- comment above). This flag is the general, per-row signal of whether a
                -- real tax_billing.total_tax figure was even available to cross-check
                -- against, mirroring total_tax_derived's provenance concept at the display
                -- layer (app.py). It is NOT hardcoded TRUE: it reads tb.total_tax directly
                -- (already LEFT JOINed below), so it will correctly flip to FALSE for any
                -- parcel whose total_tax field is genuinely populated, now or after a
                -- future reload. Confirmed via live query (July 2026): of 411,043 rows
                -- with a populated effective_tax_rate, only 11,501 (~2.8%%) currently have
                -- a usable tax_billing.total_tax on file.
                -- Same WHEN conditions as the effective_tax_rate CASE above, so this flag
                -- is non-NULL in exactly the same rows effective_tax_rate is -- NULL
                -- (Not Available) everywhere else.
                CASE
                    WHEN pty.tax_year = 2025
                     AND pty.market_value > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id      = pty.geo_id
                           AND  tbe.tax_year    = 2025
                           AND  tbe.county_code = pty.county_code
                     ) > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id      = pty.geo_id
                           AND  tbe.tax_year    = 2025
                           AND  tbe.county_code = pty.county_code
                     )::NUMERIC / pty.market_value <= 1
                    THEN (tb.total_tax IS NULL OR tb.total_tax <= 0)
                END,

                -- Delinquency flag
                COALESCE(td.total_due > 0, FALSE),

                -- Data incomplete: market_value = 0 or NULL (known AJR anomaly)
                COALESCE(pty.market_value, 0) = 0,

                '{COMPUTATION_VERSION}'

            FROM parcel_tax_year pty
            JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code
            LEFT JOIN tax_billing tb
              ON tb.geo_id = pty.geo_id AND tb.tax_year = pty.tax_year AND tb.county_code = pty.county_code
            LEFT JOIN tax_delinquent td
              ON td.geo_id = pty.geo_id AND td.county_code = pty.county_code
            WHERE pty.county_code = %s
            WINDOW w AS (PARTITION BY pty.geo_id ORDER BY pty.tax_year)
        """, (county_code,))
        n = cur.rowcount
    print(f"    Inserted {n:,} rows  ({time.time()-t0:.1f}s)", flush=True)
    print(f"  → Pass 1 done ({time.time()-t0:.1f}s, {n:,} rows)", flush=True)

    # Row-count sanity floor (silent-failure fix): a JOIN/WHERE bug that
    # silently matched far fewer rows than it should used to "succeed" here
    # with no error. This raises instead — and because nothing has been
    # committed yet, the table is left untouched (see top-of-function note).
    _assert_row_count_sane("parcel_metrics", n, prev_count, hard_floor=row_floor)

    # Pass 2: large value jump flag
    print("  → Pass 2 start (risk_large_value_jump)", flush=True)
    t1 = time.time()
    # PX-20260831-02 Task 4: threshold is now per-county, looked up here (once
    # per run, same transaction) via _large_jump_threshold_for_county() --
    # raises MetricsIntegrityError with no default/fallback if this county has
    # never had its own distribution measured and registered.
    jump_threshold = _large_jump_threshold_for_county(county_code)
    with conn.cursor() as cur:
        # PX-20260823-02: county_code added to the WHERE -- this UPDATE
        # previously touched every county's rows on every run.
        cur.execute(f"""
            UPDATE parcel_metrics
            SET risk_large_value_jump     = TRUE,
                risk_large_value_jump_pct = ABS(yoy_market_value_pct)
            WHERE ABS(yoy_market_value_pct) > {jump_threshold}
              AND county_code = %s
        """, (county_code,))
        n_jump = cur.rowcount
    print(f"    risk_large_value_jump: {n_jump:,} rows flagged (>{jump_threshold}%)  ({time.time()-t1:.1f}s)",
          flush=True)
    print(f"  → Pass 2 done ({time.time()-t1:.1f}s, {n_jump:,} rows)", flush=True)

    # Pass 3: homestead cap signals — residential only, consistent with Phase 1 guard.
    #
    # BUG-FIX HISTORY: the prior round (July 2026, "Fix Remaining Homestead-Cap
    # Gaps" Cowork brief, item 2) fixed risk_homestead_cap_expiry's original
    # "hs_cap_loss > 0" condition (structurally always False for 2025 rows --
    # never populated by load_certified_2025.py) by OR-ing in
    # "assessed_value < market_value". That made the flag fire correctly, but
    # created a NEW problem: assessed < market is simply "the cap is
    # currently working" -- the default state for any appreciating homestead,
    # not a genuine risk signal. Confirmed live: 404,355 rows / 68,336
    # distinct parcels flagged, each across most/all of 6 parcel_tax_year
    # rows (the UPDATE joined on geo_id only, no tax_year scoping -- a
    # SEPARATE bug from the hs_cap_loss issue, fanning the flag across every
    # year's row for a matching parcel). Not a counting bug -- confirmed
    # 100% correctly scoped to residential (state_cd1 LIKE 'A%') -- the real
    # problem was the flag's MEANING and its year-fanout.
    #
    # SPLIT (Issue 2, "Homestead-Cap Data Integrity: Full Fix Set" Cowork
    # brief, July 2026) into two honestly-named signals, each keyed to ONE
    # row per parcel (pm.tax_year = 2025 only, no fanout):
    #
    #   cap_step_up_exposure -- investor-facing, informational: a real,
    #     MATERIALLY-SIZED cap a buyer would lose at purchase. Two
    #     conditions, both required: the relative gap excludes trivial
    #     wedges, the dollar floor keeps it materially meaningful.
    #     Threshold tuned against the real percentile distribution (Diego's
    #     query: percentile_cont over (market-assessed)/market for
    #     HS-exempt, assessed<market 2025 rows). Sandbox reconstruction
    #     from the raw 2025 Certified PROP_ENT.TXT export (N=68,091,
    #     matching Diego's live 68,336 closely) found p25=4.12%, p50=10.11%,
    #     p75=22.32%, p90=40.68% -- Diego's originally-proposed 10%
    #     threshold sits almost exactly at the MEDIAN, not the top quartile
    #     he asked for, and would flag roughly half the protected
    #     population. Retuned to 22% (≈p75) so the flag lands in the top
    #     quartile as specified -- expect roughly 17,000 parcels on the
    #     relative-gap condition alone (25% of ~68,336) before the dollar
    #     floor further narrows it. $500 dollar floor: (market-assessed) ×
    #     this parcel's own effective rate (tb.total_tax / taxable_value,
    #     the real per-parcel rate already used elsewhere on this page,
    #     e.g. the Effective Tax Rate KPI) when 2025 billing is on file;
    #     falls back to a conservative 2% county-wide approximation
    #     (matching the ~1.8-2.1% effective rates seen elsewhere in this
    #     codebase's real parcel examples) when it isn't. NEEDS DIEGO'S
    #     LIVE RUN to confirm the exact post-both-conditions count --
    #     the relative-gap-only estimate above is sandbox-derived, not the
    #     live number.
    #
    #   cap_expiry_signal -- the name's real meaning, protection actually
    #     ENDING: HS active on the 2025 CERTIFIED roll (reliable: 55.1%
    #     populated) but absent from the 2026 PRELIMINARY exemption flags
    #     (only 52.8% populated -- confirmed less reliable, per
    #     data_coverage.py). Copy must be tentative (see property.html) --
    #     this can also just mean the preliminary file hasn't caught up yet,
    #     not a genuine loss, until 2026 certifies.
    # NULL-safety note (July 2026, "Fix parcel-exclusion filtering" brief):
    # `p.state_cd1 LIKE 'A%'` below is a POSITIVE match (residential
    # scoping), not an exclusion, so a NULL state_cd1 already evaluates to
    # NULL/false in this WHERE either way -- wrapping it in
    # COALESCE(p.state_cd1, '') doesn't change what matches today (COALESCE
    # to '' still fails 'A%'), but makes that NULL-handling explicit and
    # documented rather than incidental, consistent with the rest of this
    # file's exclusion logic (see _exclude_clause() / parcel_filters.py).
    # KNOWN LIMITATION, left open by this brief (out of scope -- this is a
    # positive-match site, not one of the four exclusion-fragment copies or
    # six peer-matching sites this brief was scoped to fix): a
    # NULL-state_cd1 residential parcel (new construction newer than the
    # 2021-2024 AJR extract this field is sourced from) never matches 'A%'
    # here, so it's never evaluated for homestead-cap step-up/expiry risk on
    # the property page, even if it genuinely carries an HS exemption. A
    # real fix would need a classi_cd-based residential check (matching
    # label_case_sql()'s classi_cd-first approach) as a fallback when
    # state_cd1 is NULL -- flagged for a future brief, not built here.
    print("  → Pass 3 start (cap_step_up_exposure, cap_expiry_signal)", flush=True)
    t3_start = time.time()
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE parcel_metrics pm
            SET cap_step_up_exposure = TRUE
            FROM (
                SELECT DISTINCT pty.geo_id
                FROM parcel_tax_year pty
                JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code
                LEFT JOIN tax_billing tb
                       ON tb.geo_id = pty.geo_id AND tb.tax_year = 2025
                      AND tb.county_code = pty.county_code
                WHERE COALESCE(p.state_cd1, '') LIKE 'A%%'
                  AND pty.county_code = %s
                  AND pty.tax_year = 2025
                  AND pty.exemption_codes LIKE '%%HS%%'
                  AND pty.market_value > 0
                  AND pty.assessed_value IS NOT NULL
                  AND pty.assessed_value < pty.market_value
                  AND (pty.market_value - pty.assessed_value)::float / pty.market_value >= 0.22
                  AND (pty.market_value - pty.assessed_value) * COALESCE(
                        NULLIF(tb.total_tax, 0) / NULLIF(pty.taxable_value, 0),
                        0.02
                      ) >= 500
                  -- Migration M2 gating (SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.5):
                  -- a multi-unit account's market_value/assessed_value are now
                  -- SUMS across every unit sharing this geo_id (parcel_rollup.py).
                  -- A summed gap crossing the 22%%/$500 thresholds doesn't mean any
                  -- ONE homestead has that much cap exposure -- it can just be an
                  -- artifact of adding several units' unrelated gaps together. Only
                  -- evaluate genuinely single-unit parcels (unit_count = 1) or rows
                  -- rollup hasn't touched yet (unit_count IS NULL, pre-migration
                  -- legacy state -- preserves this signal's existing behavior for
                  -- any environment that hasn't run the M2 loaders yet).
                  AND (pty.unit_count = 1 OR pty.unit_count IS NULL)
            ) cse
            WHERE pm.geo_id = cse.geo_id AND pm.tax_year = 2025
              AND pm.county_code = %s
        """, (county_code, county_code))
        n_step_up = cur.rowcount
    print(f"    cap_step_up_exposure: {n_step_up:,} rows flagged (2025 only, >=22% relative gap "
          f"AND >=$500 estimated)  ({time.time()-t1:.1f}s)", flush=True)

    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE parcel_metrics pm
            SET cap_expiry_signal = TRUE
            FROM (
                SELECT DISTINCT pty25.geo_id
                FROM parcel_tax_year pty25
                JOIN parcel p ON p.geo_id = pty25.geo_id AND p.county_code = pty25.county_code
                LEFT JOIN parcel_tax_year pty26
                       ON pty26.geo_id = pty25.geo_id AND pty26.tax_year = 2026
                      AND pty26.county_code = pty25.county_code
                WHERE COALESCE(p.state_cd1, '') LIKE 'A%%'
                  AND pty25.county_code = %s
                  AND pty25.tax_year = 2025
                  AND pty25.exemption_codes LIKE '%%HS%%'
                  AND (
                        pty26.geo_id IS NULL
                        OR pty26.exemption_codes IS NULL
                        OR pty26.exemption_codes NOT LIKE '%%HS%%'
                      )
                  -- Migration M2 gating -- same rationale as cap_step_up_exposure
                  -- above: exemption_codes on a multi-unit row is a UNION across
                  -- every unit sharing the geo_id (parcel_rollup.py), so "HS absent
                  -- in 2026" for the summed row doesn't mean the SAME unit that had
                  -- HS in 2025 actually lost it -- it could be a different unit's
                  -- exemption state changing. Only single-unit parcels (unit_count
                  -- = 1) or pre-rollup legacy rows (unit_count IS NULL) are
                  -- unambiguous enough for this signal.
                  AND (pty25.unit_count = 1 OR pty25.unit_count IS NULL)
            ) ces
            WHERE pm.geo_id = ces.geo_id AND pm.tax_year = 2025
              AND pm.county_code = %s
        """, (county_code, county_code))
        n_expiry = cur.rowcount
    print(f"    cap_expiry_signal: {n_expiry:,} rows flagged (2025 only, HS on 2025 "
          f"certified, absent from 2026 preliminary)  ({time.time()-t1:.1f}s)", flush=True)
    print("    (risk_homestead_cap_expiry column left in place for backward "
          "compatibility but no longer written by this script -- see schema.sql's "
          "migration comment)", flush=True)
    print(f"  → Pass 3 done ({time.time()-t3_start:.1f}s, {n_step_up + n_expiry:,} rows)", flush=True)

    # Pass 4: cumulative value growth (on 2025 row, from each parcel's earliest valid year)
    #
    # PX-20260901-01 HOTFIX: this pass ran 13h07m (cancelled by PM, no wait
    # event -- pure CPU/IO) on its second live run, against Dallas. Root cause
    # (confirmed against `git show 0fcddcd`, the pre-PX-20260831-02-Task-5
    # version -- not assumed): the inner subquery below (formerly aliased
    # `mn`) computed MIN(tax_year) GROUP BY geo_id[, county_code] with NO
    # county_code predicate in its own WHERE clause -- it NEVER has, in
    # either version. Pre-Task-5, that was harmless: the ENTIRE
    # parcel_tax_year table was Travis-only, so "whole table" and "this
    # county" were the same set -- the query wasn't scoped, it just never
    # needed to be. Task 5 added county_code to that subquery's
    # SELECT/GROUP BY/join condition -- a real, necessary CORRECTNESS fix,
    # preventing a geo_id collision between counties from blending two
    # counties' "earliest valid market value" together -- but did NOT add a
    # matching `WHERE county_code = %s` filter to scope the aggregation
    # itself. The moment Dallas's rows coexisted with Travis's in
    # parcel_tax_year (~6.4M rows combined per the incident report, vs.
    # ~2.8M when this ran for Travis alone on Aug 3), every run of this pass
    # -- even a single-county run like this one -- paid the cost of a
    # MIN()/GROUP BY aggregation across BOTH counties' complete history
    # before the join ever narrowed anything down to the one county actually
    # being computed. On a Render Basic-1gb instance, a HashAggregate over
    # that many distinct (geo_id, county_code) groups is memory-pressure-
    # prone and can spill heavily to local disk -- consistent with "active,
    # no wait event, pure CPU/IO for 13 hours" (no lock contention, just very
    # large local I/O).
    #
    # Fix: materialize this county's per-geo earliest-valid-year values into
    # an indexed temp table FIRST, with county_code = %s inside the
    # aggregation's own WHERE clause (before GROUP BY, not just present in
    # the SELECT list) -- so the aggregation only ever scans this county's
    # rows. This also makes the cross-county-collision guard STRONGER than a
    # same-shape scoped-subquery-in-place would: since _pass4_earliest_year
    # only ever contains this county's geo_ids (by construction, not just by
    # an added equality check), no cross-county geo_id collision is possible
    # at the join below. ON COMMIT DROP -- self-cleaning within this
    # function's single transaction (see top-of-function note).
    #
    # Diego's EXPLAIN (loaders/explain_compute_metrics_passes.py) is what
    # confirms the plan against the live DB; this diagnosis is from reading
    # the code + the git history, not a live plan.
    print("  → Pass 4 start (cumulative_value_growth_pct)", flush=True)
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE _pass4_earliest_year ON COMMIT DROP AS
            SELECT geo_id, MIN(tax_year) AS earliest_year
            FROM parcel_tax_year
            WHERE county_code = %s
              AND market_value > 0
            GROUP BY geo_id
        """, (county_code,))
        cur.execute("CREATE UNIQUE INDEX ON _pass4_earliest_year (geo_id)")
        cur.execute("""
            UPDATE parcel_metrics pm
            SET cumulative_value_growth_pct = sub.cum_pct
            FROM (
                SELECT
                    cur.geo_id,
                    ROUND(
                        100.0 * (cur.market_value - earliest.market_value)
                        / earliest.market_value, 4
                    ) AS cum_pct
                FROM parcel_tax_year cur
                JOIN _pass4_earliest_year mn ON mn.geo_id = cur.geo_id
                JOIN parcel_tax_year earliest
                  ON earliest.geo_id = mn.geo_id
                 AND earliest.tax_year = mn.earliest_year
                 AND earliest.county_code = %s
                WHERE cur.county_code = %s
                  AND cur.tax_year = 2025
                  AND cur.market_value > 0
                  AND earliest.market_value > 0
                  AND cur.tax_year != mn.earliest_year   -- need at least 2 data points
            ) sub
            WHERE pm.geo_id = sub.geo_id AND pm.tax_year = 2025
              AND pm.county_code = %s
        """, (county_code, county_code, county_code))
        n_cum = cur.rowcount
    print(f"    cumulative_value_growth_pct: {n_cum:,} rows updated  ({time.time()-t1:.1f}s)",
          flush=True)
    print(f"  → Pass 4 done ({time.time()-t1:.1f}s, {n_cum:,} rows)", flush=True)

    # Single commit for the whole DELETE + rebuild (see top-of-function note).
    conn.commit()
    print(f"  → parcel_metrics done in {time.time()-t0:.1f}s total", flush=True)


# ── Step 2b: Compute county_benchmark ──────────────────────────────────────────
def compute_county_benchmarks(conn, county_code=DEFAULT_COUNTY):
    print("\n[2] Computing county_benchmark…")
    t0 = time.time()

    # Partial-write-window fix: same approach as compute_parcel_metrics() — the
    # DELETE and the full rebuild loop below share one transaction, committed
    # once at the end. A crash or sanity-check failure anywhere in between
    # rolls back the DELETE too, leaving county_benchmark in its prior state.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM county_benchmark")
        prev_count = cur.fetchone()[0]

    with conn.cursor() as cur:
        # PX-20260823-02: scoped by county_code -- cheap and safe here
        # (unlike parcel_metrics' DELETE above) because the INSERT below is
        # a real upsert (ON CONFLICT (county_code, tax_year,
        # property_type_label) DO UPDATE), so re-inserting every county's
        # rows on every run -- which the unscoped SELECT below still does --
        # can't collide with rows this DELETE left untouched for other
        # counties.
        cur.execute("DELETE FROM county_benchmark WHERE county_code = %s", (county_code,))

    excl = _exclude_clause()
    # classi_cd-first label (Task 1): apartments carrying a multi-family
    # improvement code are bucketed as Multi-Family even when state_cd1 says 'A'.
    label_expr = label_case_sql("p.classi_cd", "p.state_cd1")
    total_n = 0
    with conn.cursor() as cur:
        for prefixes, label, prefix_key in TYPE_GROUPS:
            cur.execute(f"""
                INSERT INTO county_benchmark (
                    county_code, tax_year, property_type_label, state_cd1_prefix,
                    parcel_count,
                    median_market_value, p25_market_value, p75_market_value,
                    median_assessed_value, median_assessment_ratio,
                    median_yoy_value_change_pct
                )
                SELECT
                    %s,
                    pty.tax_year,
                    %s,
                    %s,
                    COUNT(*),
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY pty.market_value)::BIGINT,
                    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY pty.assessed_value)::BIGINT,
                    ROUND(
                        PERCENTILE_CONT(0.50) WITHIN GROUP (
                            ORDER BY CASE WHEN pty.market_value > 0
                                THEN pty.assessed_value::NUMERIC / pty.market_value
                                ELSE NULL END
                        )::NUMERIC, 4),
                    ROUND(
                        PERCENTILE_CONT(0.50) WITHIN GROUP (
                            ORDER BY pm.yoy_market_value_pct
                        )::NUMERIC, 4)
                FROM parcel_tax_year pty
                JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code
                LEFT JOIN parcel_metrics pm
                  ON pm.geo_id = pty.geo_id AND pm.tax_year = pty.tax_year
                  AND pm.county_code = pty.county_code
                WHERE ({label_expr}) = %s
                  {excl}
                  AND pty.market_value > 0
                  AND (pty.data_source IS NULL OR pty.data_source != 'preliminary')
                  -- PX-20260828-16-followup: this INSERT...SELECT previously
                  -- aggregated across EVERY county's parcel_tax_year/parcel
                  -- rows before stamping the single %%s county_code value
                  -- (first SELECT-list column above) onto every resulting
                  -- row -- confirmed the exact bug load_dallas_certified.py's
                  -- own comment already flagged. Scoped now, together with
                  -- the already-correct DELETE above, so a per-county run
                  -- only ever aggregates that county's own rows.
                  AND pty.county_code = %s
                GROUP BY pty.tax_year
                ON CONFLICT (county_code, tax_year, property_type_label) DO UPDATE
                    SET parcel_count                = EXCLUDED.parcel_count,
                        median_market_value         = EXCLUDED.median_market_value,
                        p25_market_value            = EXCLUDED.p25_market_value,
                        p75_market_value            = EXCLUDED.p75_market_value,
                        median_assessed_value       = EXCLUDED.median_assessed_value,
                        median_assessment_ratio     = EXCLUDED.median_assessment_ratio,
                        median_yoy_value_change_pct = EXCLUDED.median_yoy_value_change_pct,
                        computed_at                 = NOW()
            """, (county_code, label, prefix_key, label, county_code))
            n = cur.rowcount
            print(f"    {label}: {n} year rows")
            # Each of the five TYPE_GROUPS is known to have real parcels in
            # Travis County every year — a category producing zero rows is a
            # silent-failure signal (bad label_expr, bad exclusion clause,
            # etc.), not a legitimate empty category. Fail loudly rather than
            # let the aggregate floor below mask a single broken category.
            if n == 0:
                raise MetricsIntegrityError(
                    f"county_benchmark: category '{label}' produced 0 rows — "
                    f"every TYPE_GROUP is expected to have parcels every year. "
                    f"Treating this as a failure, not a successful rebuild."
                )
            total_n += n

    _assert_row_count_sane("county_benchmark", total_n, prev_count, hard_floor=COUNTY_BENCHMARK_ROW_FLOOR)

    # Single commit for the whole DELETE + rebuild (see top-of-function note).
    conn.commit()
    print(f"  → county_benchmark done in {time.time()-t0:.1f}s")


# ── Sample verification output ──────────────────────────────────────────────────
def print_sample(conn, county_code=DEFAULT_COUNTY):
    """PX-20260830-05 Task 3 (Bucket C): county_code param added, default
    DEFAULT_COUNTY (unlike analyze_threshold() above, a default is fine
    here -- this is a debug/sanity-check printout, not a business figure,
    and the three sanity_parcels geo_ids are Travis-specific real accounts
    anyway). Every query below is now predicated on county_code --
    tax_billing/tax_billing_entity/parcel_metrics are all composite_pk-
    migrated, and geo_id alone is not guaranteed unique across counties, so
    an unscoped lookup could silently print another county's rows under a
    Travis-labeled sample parcel once a second county's data shares a geo_id.
    """
    sanity_parcels = [
        ("0100030105", "Commercial F1 — 1201 S Lamar"),
        ("0100030109", "Multi-family B — 1219 S Lamar"),
        ("0284460113", "Residential A — Abbeyglen Castle Dr"),
    ]
    print(f"\n=== Sample: sanity-check parcels (county_code={county_code}) ===")
    # First show tax_billing state so we can diagnose eff_rate issues
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tax_billing WHERE county_code = %s", (county_code,))
        tb_total = cur.fetchone()[0]
        cur.execute(
            "SELECT tax_year, COUNT(*) FROM tax_billing WHERE county_code = %s "
            "GROUP BY tax_year ORDER BY tax_year",
            (county_code,)
        )
        tb_by_year = cur.fetchall()
    print(f"\n  tax_billing rows: {tb_total:,}")
    for yr, cnt in tb_by_year:
        print(f"    tax_year={yr}: {cnt:,} rows")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for geo_id, label in sanity_parcels:
            print(f"\n  {label} ({geo_id})")
            cur.execute(
                "SELECT tax_year, total_tax FROM tax_billing "
                "WHERE geo_id = %s AND county_code = %s ORDER BY tax_year",
                (geo_id, county_code)
            )
            billing_rows = cur.fetchall()
            if billing_rows:
                for b in billing_rows:
                    print(f"    billing tax_year={b['tax_year']} total_tax={b['total_tax']}")
            else:
                print("    billing: (no rows in tax_billing)")
            cur.execute(
                "SELECT tax_year, SUM(amount_due) as entity_total FROM tax_billing_entity "
                "WHERE geo_id = %s AND county_code = %s GROUP BY tax_year ORDER BY tax_year",
                (geo_id, county_code)
            )
            entity_rows = cur.fetchall()
            if entity_rows:
                for e in entity_rows:
                    print(f"    entity_total tax_year={e['tax_year']} sum(amount_due)={e['entity_total']}")
            else:
                print("    entity: (no rows in tax_billing_entity)")
            cur.execute("""
                SELECT tax_year, coverage_level,
                       yoy_market_value_pct,
                       assessment_ratio,
                       effective_tax_rate,
                       effective_tax_rate_derived,
                       cumulative_value_growth_pct,
                       risk_large_value_jump,
                       risk_large_value_jump_pct,
                       cap_step_up_exposure,
                       cap_expiry_signal,
                       risk_delinquent,
                       risk_data_incomplete
                FROM parcel_metrics WHERE geo_id = %s AND county_code = %s ORDER BY tax_year
            """, (geo_id, county_code))
            for r in cur.fetchall():
                d = dict(r)
                print(f"    {d['tax_year']} [{d['coverage_level']}]"
                      f"  yoy_mkt={d['yoy_market_value_pct']}"
                      f"  ratio={d['assessment_ratio']}"
                      f"  eff_rate={d['effective_tax_rate']}"
                      f"  eff_rate_derived={d['effective_tax_rate_derived']}"
                      f"  cum={d['cumulative_value_growth_pct']}"
                      f"  jump={d['risk_large_value_jump']}"
                      f"  cap_step_up={d['cap_step_up_exposure']}"
                      f"  cap_expiry={d['cap_expiry_signal']}")

        print("\n  County benchmark — Residential 2025:")
        cur.execute("""
            SELECT parcel_count, median_market_value, p25_market_value, p75_market_value,
                   median_assessment_ratio, median_yoy_value_change_pct
            FROM county_benchmark
            WHERE property_type_label = 'Residential' AND tax_year = 2025 AND county_code = %s
        """, (county_code,))
        r = cur.fetchone()
        if r:
            d = dict(r)
            print(f"    n={d['parcel_count']:,}  "
                  f"median=${d['median_market_value']:,}  "
                  f"p25=${d['p25_market_value']:,}  "
                  f"p75=${d['p75_market_value']:,}  "
                  f"ratio={d['median_assessment_ratio']}  "
                  f"yoy={d['median_yoy_value_change_pct']}%")


# ── Entrypoint ──────────────────────────────────────────────────────────────────
def main():
    # PX-20260901-01 HOTFIX Task 3: Pass 4's 13-hour runaway printed NOTHING
    # to the terminal/log the whole time it ran, even though the code between
    # passes does call print() -- Python block-buffers stdout by default
    # whenever it isn't a TTY (e.g. piped through `tee`, as the runbook's run
    # commands do), so those print()s were sitting in an internal buffer, not
    # actually reaching the log, until either the buffer filled or the
    # process exited. This line -- plus flush=True on every print() inside
    # compute_parcel_metrics()'s passes below -- means PM will see each
    # pass's start/done line the moment it happens, not after the fact (or
    # never, if the process is killed mid-run). The runbook's run commands
    # also gain `-u` (Python's own unbuffered-stdio flag) as a second,
    # redundant guard against the same class of silence.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Phase 2 metric computation")
    parser.add_argument("--analyze", action="store_true",
                        help="Print threshold distribution analysis only; skip compute")
    parser.add_argument("--benchmarks-only", action="store_true",
                        help="Rebuild county_benchmark only (skip the parcel_metrics "
                             "recompute). Use after a classification-only change.")
    parser.add_argument(
        "--county", default=DEFAULT_COUNTY,
        help=f"county_code used to scope EVERY write and source SELECT this script "
             f"issues, including compute_parcel_metrics()'s and "
             f"compute_county_benchmarks()'s own aggregation queries (default: "
             f"{DEFAULT_COUNTY}). PX-20260823-02 scoped the UPDATE passes and "
             f"county_benchmark's DELETE; PX-20260828-16-followup closed the "
             f"remaining gap -- both functions' main DELETE+INSERT...SELECT rebuild "
             f"is now scoped to this one county per run, not a global rebuild across "
             f"every county's rows.",
    )
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.analyze:
            analyze_threshold(conn, county_code=args.county)
            return

        # Apply any new schema additions (parcel_metrics, county_benchmark, rate_trend view)
        print("Applying schema…")
        execute_schema(conn)

        if args.benchmarks_only:
            # Task 1: classification-only change touches county_benchmark bucketing,
            # not the per-parcel YoY rows — rebuild just the benchmark.
            try:
                compute_county_benchmarks(conn, county_code=args.county)
            except Exception:
                conn.rollback()
                print("\n*** county_benchmark rebuild FAILED and was rolled back — "
                      "the table is unchanged from before this run. ***")
                raise
            print_sample(conn, county_code=args.county)
            print("\nDone (benchmarks only).")
            return

        # Threshold analysis runs first so you can see what the current setting flags
        analyze_threshold(conn, county_code=args.county)

        # Compute. Each function commits its own table's DELETE+rebuild as one
        # transaction; if either raises (including the row-count sanity checks
        # inside them), roll back explicitly here too — belt and suspenders in
        # case the connection isn't closed cleanly — and re-raise so the
        # failure is loud (non-zero exit, visible traceback), never silent.
        # Note: if compute_parcel_metrics() already printed "→ parcel_metrics
        # done" before compute_county_benchmarks() fails, parcel_metrics was
        # already committed and IS updated — only the table whose "done" line
        # never printed was rolled back to its prior state.
        try:
            compute_parcel_metrics(conn, county_code=args.county)
            compute_county_benchmarks(conn, county_code=args.county)
        except Exception:
            conn.rollback()
            print("\n*** compute_metrics FAILED and was rolled back. Check which "
                  "step's '→ ... done' line printed above (if any) — that table "
                  "was already committed and is current; whichever step didn't "
                  "finish is unchanged from before this run. ***")
            raise
        print_sample(conn, county_code=args.county)

        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
