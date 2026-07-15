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
import psycopg2.extras

COMPUTATION_VERSION = "2.0"

# ── Row-count sanity floor ───────────────────────────────────────────────────────
# Absolute backstop for the "silently far fewer rows than expected" failure mode.
# Primary check is relative (new vs. previous row count, see _assert_row_count_sane);
# these are only the floor for a first-ever run or if the previous count was itself 0.
# parcel_metrics is currently ~2,796,316 rows (508K parcels x ~5.5 years); 1,000,000
# is comfortably below normal (survives losing a year or two of coverage) while still
# catching a near-empty or empty table.
PARCEL_METRICS_ROW_FLOOR = 1_000_000
# county_benchmark is 5 TYPE_GROUPS x ~5 years each = ~25 rows; 15 survives losing a
# year of coverage on a couple of categories without masking a real failure.
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

# ── Risk threshold ──────────────────────────────────────────────────────────────
# Set at 75% based on actual distribution across 1,401,316 parcel-year pairs:
#   p50=7.1%  p75=32.3%  p90=59.4%  p95=72.0%  p99=292.9%
#   > 50%: 15.9% of pairs  > 75%: 4.5%  > 100%: 2.6%
#
# 75% sits just above the p90 — flags genuinely unusual moves without drowning
# investors in noise. 50% (p90) flagged 1 in 6 parcel-years, too broad to be
# actionable. 100% was considered but is only 2.6% and misses real 75-99% jumps.
LARGE_JUMP_THRESHOLD_PCT = 75.0

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
# KEPT in benchmarks (all confirmed real property in Travis CAD):
#   M* (10,699) — manufactured homes (treated as real property under TX law)
#   O* (19,986) — "Other" use-type parcels (real property with valid MV)
#   L* (42,504) — commercial real estate (already in Commercial TYPE_GROUP)
#   J*  (1,524) — industrial / utility real property
#   S*    (751) — state-assessed utility real property
#   G*      (6) — government-assessed parcels (de minimis)
#
# NULL state_cd1 (17,175) parcels are naturally excluded because NULL does not
# match any LIKE pattern in the TYPE_GROUPS WHERE clause.
BENCHMARK_EXCLUDE_PREFIXES = ["X", "N"]


def _exclude_clause():
    """SQL fragment excluding non-real-property state_cd1 prefixes from benchmark queries."""
    parts = " AND ".join(
        f"p.state_cd1 NOT LIKE '{px}%%'" for px in BENCHMARK_EXCLUDE_PREFIXES
    )
    return f"AND ({parts})"


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _like_clause(prefixes):
    """Build a LIKE ANY(...) clause for state_cd1 prefix matching.
    Uses %% so the literal % survives psycopg2 parameter substitution
    when the result is interpolated into an f-string SQL."""
    patterns = ", ".join(f"'{p}%%'" for p in prefixes)
    return f"p.state_cd1 LIKE ANY(ARRAY[{patterns}])"


# ── Step 4: Threshold distribution analysis ─────────────────────────────────────
def analyze_threshold(conn):
    print("\n" + "=" * 60)
    print("  Step 4 — Risk Threshold Distribution Analysis")
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
                  ON b.geo_id = a.geo_id AND b.tax_year = a.tax_year - 1
                WHERE a.market_value > 0 AND b.market_value > 0
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
        """)
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
    thresholds = [10, 20, 30, 40, 50, 75, 100]
    counts = [r[1], r[2], r[3], r[4], r[5], r[6], r[7]]
    for t, c in zip(thresholds, counts):
        pct_flagged = 100.0 * c / total if total else 0
        marker = "  ← current LARGE_JUMP_THRESHOLD_PCT" if t == LARGE_JUMP_THRESHOLD_PCT else ""
        print(f"    > {t:3d}%:  {c:>8,}  ({pct_flagged:.1f}%){marker}")

    print(f"\n  Elapsed: {time.time()-t0:.1f}s")
    print("=" * 60)


# ── Step 2: Compute parcel_metrics ──────────────────────────────────────────────
def compute_parcel_metrics(conn):
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
        cur.execute("SELECT COUNT(*) FROM parcel_metrics")
        prev_count = cur.fetchone()[0]

    with conn.cursor() as cur:
        cur.execute("DELETE FROM parcel_metrics")

    # Main insert: YoY metrics via SQL window functions
    # yoy_tax_amount_pct is NULL for all years — no historical billing exists yet
    # effective_tax_rate populated for 2025 only (real billing available)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO parcel_metrics (
                geo_id, tax_year,
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
                -- because TOTAL_TAX in the TaxCurOpenData source is 0.00 for ~93% of all 2025
                -- rows (confirmed by direct inspection of the raw CSV — not narrowly scoped to
                -- "some property types"; it's the majority of rows regardless of type), even
                -- when entity-level DUE amounts are correct. See KNOWN_LIMITATIONS.md.
                -- Cap at 1.0 (100%) — values above that are bad data.
                CASE
                    WHEN pty.tax_year = 2025
                     AND pty.market_value > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id    = pty.geo_id
                           AND  tbe.tax_year  = 2025
                     ) > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id    = pty.geo_id
                           AND  tbe.tax_year  = 2025
                     )::NUMERIC / pty.market_value <= 1
                    THEN ROUND(
                        (
                            SELECT SUM(tbe.amount_due)
                            FROM   tax_billing_entity tbe
                            WHERE  tbe.geo_id    = pty.geo_id
                              AND  tbe.tax_year  = 2025
                        )::NUMERIC / pty.market_value,
                        6
                    )
                END,

                -- effective_tax_rate_derived (Effective Tax Rate KPI masking-bug fix,
                -- July 2026, per Diego): the CASE above always derives effective_tax_rate
                -- from SUM(tax_billing_entity.amount_due) -- it never uses tb.total_tax as
                -- the numerator, because TOTAL_TAX is blank for ~93% of 2025 rows (see
                -- comment above). This flag is the general, per-row signal of whether a
                -- real tax_billing.total_tax figure was even available to cross-check
                -- against, mirroring total_tax_derived's provenance concept at the display
                -- layer (app.py). It is NOT hardcoded TRUE: it reads tb.total_tax directly
                -- (already LEFT JOINed below), so it will correctly flip to FALSE for any
                -- parcel whose total_tax field is genuinely populated, now or after a
                -- future reload. Confirmed via live query (July 2026): of 411,043 rows
                -- with a populated effective_tax_rate, only 11,501 (~2.8%) currently have
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
                         WHERE  tbe.geo_id    = pty.geo_id
                           AND  tbe.tax_year  = 2025
                     ) > 0
                     AND (
                         SELECT SUM(tbe.amount_due)
                         FROM   tax_billing_entity tbe
                         WHERE  tbe.geo_id    = pty.geo_id
                           AND  tbe.tax_year  = 2025
                     )::NUMERIC / pty.market_value <= 1
                    THEN (tb.total_tax IS NULL OR tb.total_tax <= 0)
                END,

                -- Delinquency flag
                COALESCE(td.total_due > 0, FALSE),

                -- Data incomplete: market_value = 0 or NULL (known AJR anomaly)
                COALESCE(pty.market_value, 0) = 0,

                '{COMPUTATION_VERSION}'

            FROM parcel_tax_year pty
            JOIN parcel p ON p.geo_id = pty.geo_id
            LEFT JOIN tax_billing tb
              ON tb.geo_id = pty.geo_id AND tb.tax_year = pty.tax_year
            LEFT JOIN tax_delinquent td
              ON td.geo_id = pty.geo_id
            WINDOW w AS (PARTITION BY pty.geo_id ORDER BY pty.tax_year)
        """)
        n = cur.rowcount
    print(f"    Inserted {n:,} rows  ({time.time()-t0:.1f}s)")

    # Row-count sanity floor (silent-failure fix): a JOIN/WHERE bug that
    # silently matched far fewer rows than it should used to "succeed" here
    # with no error. This raises instead — and because nothing has been
    # committed yet, the table is left untouched (see top-of-function note).
    _assert_row_count_sane("parcel_metrics", n, prev_count, hard_floor=PARCEL_METRICS_ROW_FLOOR)

    # Pass 2: large value jump flag
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE parcel_metrics
            SET risk_large_value_jump     = TRUE,
                risk_large_value_jump_pct = ABS(yoy_market_value_pct)
            WHERE ABS(yoy_market_value_pct) > {LARGE_JUMP_THRESHOLD_PCT}
        """)
        n_jump = cur.rowcount
    print(f"    risk_large_value_jump: {n_jump:,} rows flagged (>{LARGE_JUMP_THRESHOLD_PCT}%)  ({time.time()-t1:.1f}s)")

    # Pass 3: homestead cap expiry risk — residential only, consistent with Phase 1 guard.
    # Condition: the 2025 row specifically has hs_cap_loss > 0 AND assessed < market.
    # Restricting to the 2025 row (Certified data) avoids AJR noise where hs_cap_loss
    # is present on almost every residential parcel. The 2025 Certified value is the
    # authoritative source; if the gap is real it will show there.
    t1 = time.time()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE parcel_metrics pm
            SET risk_homestead_cap_expiry = TRUE
            FROM (
                SELECT DISTINCT pty.geo_id
                FROM parcel_tax_year pty
                JOIN parcel p ON p.geo_id = pty.geo_id
                WHERE p.state_cd1 LIKE 'A%'
                  AND pty.tax_year = 2025
                  AND pty.hs_cap_loss > 0
                  AND pty.market_value > 0
                  AND pty.assessed_value < pty.market_value
            ) hs
            WHERE pm.geo_id = hs.geo_id
        """)
        n_cap = cur.rowcount
    print(f"    risk_homestead_cap_expiry: {n_cap:,} rows flagged  ({time.time()-t1:.1f}s)")

    # Pass 4: cumulative value growth (on 2025 row, from each parcel's earliest valid year)
    t1 = time.time()
    with conn.cursor() as cur:
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
                JOIN (
                    -- Find earliest year with valid (non-zero, non-null) market_value per parcel
                    SELECT geo_id, MIN(tax_year) AS earliest_year
                    FROM parcel_tax_year
                    WHERE market_value > 0
                    GROUP BY geo_id
                ) mn ON mn.geo_id = cur.geo_id
                JOIN parcel_tax_year earliest
                  ON earliest.geo_id = mn.geo_id
                 AND earliest.tax_year = mn.earliest_year
                WHERE cur.tax_year = 2025
                  AND cur.market_value > 0
                  AND earliest.market_value > 0
                  AND cur.tax_year != mn.earliest_year   -- need at least 2 data points
            ) sub
            WHERE pm.geo_id = sub.geo_id AND pm.tax_year = 2025
        """)
        n_cum = cur.rowcount
    print(f"    cumulative_value_growth_pct: {n_cum:,} rows updated  ({time.time()-t1:.1f}s)")

    # Single commit for the whole DELETE + rebuild (see top-of-function note).
    conn.commit()
    print(f"  → parcel_metrics done in {time.time()-t0:.1f}s total")


# ── Step 2b: Compute county_benchmark ──────────────────────────────────────────
def compute_county_benchmarks(conn):
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
        cur.execute("DELETE FROM county_benchmark")

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
                    'TRAVIS',
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
                JOIN parcel p ON p.geo_id = pty.geo_id
                LEFT JOIN parcel_metrics pm
                  ON pm.geo_id = pty.geo_id AND pm.tax_year = pty.tax_year
                WHERE ({label_expr}) = %s
                  {excl}
                  AND p.geo_id NOT LIKE 'AJR%%'
                  AND pty.market_value > 0
                  AND (pty.data_source IS NULL OR pty.data_source != 'preliminary')
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
            """, (label, prefix_key, label))
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
def print_sample(conn):
    sanity_parcels = [
        ("0100030105", "Commercial F1 — 1201 S Lamar"),
        ("0100030109", "Multi-family B — 1219 S Lamar"),
        ("0284460113", "Residential A — Abbeyglen Castle Dr"),
    ]
    print("\n=== Sample: sanity-check parcels ===")
    # First show tax_billing state so we can diagnose eff_rate issues
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tax_billing")
        tb_total = cur.fetchone()[0]
        cur.execute("SELECT tax_year, COUNT(*) FROM tax_billing GROUP BY tax_year ORDER BY tax_year")
        tb_by_year = cur.fetchall()
    print(f"\n  tax_billing rows: {tb_total:,}")
    for yr, cnt in tb_by_year:
        print(f"    tax_year={yr}: {cnt:,} rows")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for geo_id, label in sanity_parcels:
            print(f"\n  {label} ({geo_id})")
            cur.execute(
                "SELECT tax_year, total_tax FROM tax_billing WHERE geo_id = %s ORDER BY tax_year",
                (geo_id,)
            )
            billing_rows = cur.fetchall()
            if billing_rows:
                for b in billing_rows:
                    print(f"    billing tax_year={b['tax_year']} total_tax={b['total_tax']}")
            else:
                print("    billing: (no rows in tax_billing)")
            cur.execute(
                "SELECT tax_year, SUM(amount_due) as entity_total FROM tax_billing_entity WHERE geo_id = %s GROUP BY tax_year ORDER BY tax_year",
                (geo_id,)
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
                       risk_homestead_cap_expiry,
                       risk_delinquent,
                       risk_data_incomplete
                FROM parcel_metrics WHERE geo_id = %s ORDER BY tax_year
            """, (geo_id,))
            for r in cur.fetchall():
                d = dict(r)
                print(f"    {d['tax_year']} [{d['coverage_level']}]"
                      f"  yoy_mkt={d['yoy_market_value_pct']}"
                      f"  ratio={d['assessment_ratio']}"
                      f"  eff_rate={d['effective_tax_rate']}"
                      f"  eff_rate_derived={d['effective_tax_rate_derived']}"
                      f"  cum={d['cumulative_value_growth_pct']}"
                      f"  jump={d['risk_large_value_jump']}"
                      f"  cap={d['risk_homestead_cap_expiry']}")

        print("\n  County benchmark — Residential 2025:")
        cur.execute("""
            SELECT parcel_count, median_market_value, p25_market_value, p75_market_value,
                   median_assessment_ratio, median_yoy_value_change_pct
            FROM county_benchmark
            WHERE property_type_label = 'Residential' AND tax_year = 2025
        """)
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
    parser = argparse.ArgumentParser(description="Phase 2 metric computation")
    parser.add_argument("--analyze", action="store_true",
                        help="Print threshold distribution analysis only; skip compute")
    parser.add_argument("--benchmarks-only", action="store_true",
                        help="Rebuild county_benchmark only (skip the parcel_metrics "
                             "recompute). Use after a classification-only change.")
    args = parser.parse_args()

    conn = get_conn()
    try:
        if args.analyze:
            analyze_threshold(conn)
            return

        # Apply any new schema additions (parcel_metrics, county_benchmark, rate_trend view)
        print("Applying schema…")
        execute_schema(conn)

        if args.benchmarks_only:
            # Task 1: classification-only change touches county_benchmark bucketing,
            # not the per-parcel YoY rows — rebuild just the benchmark.
            try:
                compute_county_benchmarks(conn)
            except Exception:
                conn.rollback()
                print("\n*** county_benchmark rebuild FAILED and was rolled back — "
                      "the table is unchanged from before this run. ***")
                raise
            print_sample(conn)
            print("\nDone (benchmarks only).")
            return

        # Threshold analysis runs first so you can see what the current setting flags
        analyze_threshold(conn)

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
            compute_parcel_metrics(conn)
            compute_county_benchmarks(conn)
        except Exception:
            conn.rollback()
            print("\n*** compute_metrics FAILED and was rolled back. Check which "
                  "step's '→ ... done' line printed above (if any) — that table "
                  "was already committed and is current; whichever step didn't "
                  "finish is unchanged from before this run. ***")
            raise
        print_sample(conn)

        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
