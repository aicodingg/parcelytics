-- PEER_SET_DISTRIBUTION_CHECK.sql
--
-- Diagnostic for the Item 3 filter tightening (July 2026, per Diego):
-- api_peer_set() now requires an EXACT classi_cd match (was: broad
-- classify.py category match). This sandbox has no live DB, so the actual
-- distribution of "how many parcels would land in each fallback tier" could
-- not be measured here — run this against the real database to see the real
-- numbers the code only reasons about structurally.
--
-- ============================================================================
-- REWRITE NOTICE (July 2026) — the original version of this file hung for
-- 50+ minutes with no result and had to be cancelled. Root cause, fix, and
-- how the fix was verified are below. Same "investigate before fixing"
-- discipline as the Market Snapshot slowdown.
--
-- ROOT CAUSE (found by reading the query, not by running EXPLAIN myself —
-- this sandbox has no DB connection and no way to install Postgres locally
-- to run one, either; see "What I could not verify" at the bottom):
--
-- The original tier1_counts CTE counted each parcel's peers with a
-- CORRELATED SUBQUERY against the `subj` CTE:
--
--     tier1_counts AS (
--       SELECT s.geo_id,
--              (SELECT COUNT(*) FROM subj o WHERE ... ) AS tier1_peer_count
--       FROM subj s
--     )
--
-- `subj` is referenced TWICE here (once as `s`, once as `o` inside the
-- subquery). Postgres's planner only inlines a CTE when it's referenced
-- exactly once; referenced twice, it defaults to MATERIALIZING it — turning
-- `subj` into a plain, unindexed, ~400-425K-row temporary row set (matching
-- the "406,355 distinct geo_ids" / "425,927 matched row blocks" figures from
-- the 2022-2024 PIR loader work earlier this session — this table really is
-- in that size range). The correlated subquery then re-scans that entire
-- materialized set FROM SCRATCH for every single outer row: ~400K outer rows
-- × ~400K-row scan each ≈ 160 BILLION row comparisons. That is the
-- "completely normal, fast" subj CTE Diego's own isolated EXPLAIN already
-- confirmed is NOT the bottleneck, followed immediately by a step that is
-- architecturally an O(N²) nested loop over an index-less intermediate
-- result — not a missing index on the real `parcel`/`parcel_tax_year`
-- tables (an index on those can't even be consulted here, because the
-- correlated subquery isn't reading the base tables at that point, it's
-- reading the already-materialized `subj`). Query #2 below (zero_tier1 /
-- rescue_counts) has the SAME pattern, twice over — a NOT EXISTS correlated
-- subquery to build zero_tier1, then two MORE correlated subqueries against
-- `subj` per row of zero_tier1 for tier2/tier3 — so it would have been even
-- slower had query #1 ever finished.
--
-- FIX: rewrite every correlated subquery against `subj` as a real self-JOIN
-- (LEFT JOIN, so parcels with zero peers still appear with count 0 instead
-- of silently vanishing — this matters a lot here since "0 tier1 peers" is
-- the single most important bucket this diagnostic exists to measure) +
-- GROUP BY, and the NOT EXISTS as a LEFT JOIN ... WHERE o.geo_id IS NULL
-- anti-join. This lets Postgres build ONE hash table over `subj` and do ONE
-- hash join / hash anti-join per CTE — O(N) instead of O(N²) — rather than
-- re-scanning `subj` from scratch per row.
--
-- SUPPORTING INDEX (recommended, not required for correctness — the rewrite
-- above is the real fix): `UPPER(TRIM(classi_cd))` appears in every tier's
-- join/filter condition. Wrapping a column in functions means a plain btree
-- index on classi_cd can't be used for it — only a matching expression index
-- can. Worth adding regardless of this one-off diagnostic, because the
-- shipped api_peer_set() endpoint (app.py) filters on this exact expression
-- on every property-page load, not just here:
--
--     CREATE INDEX CONCURRENTLY idx_parcel_use_code_exact
--       ON parcel (UPPER(TRIM(classi_cd)), neighborhood_cd);
--
-- (CONCURRENTLY so it doesn't lock the table while building; drop it if you
-- don't want an extra permanent index just for a one-time diagnostic — the
-- JOIN rewrite alone should already turn this from "50+ minutes, cancelled"
-- into a query that completes.)
--
-- WHAT I ACTUALLY VERIFIED vs. WHAT STILL NEEDS YOUR LIVE DB:
--   - Verified: built a synthetic ~3,000-row dataset (SQLite, same column
--     shapes and same query logic, including deliberately-isolated rows with
--     zero same-use-code/same-neighborhood peers) and confirmed the rewritten
--     LEFT JOIN + GROUP BY / anti-join versions return IDENTICAL per-row
--     results to the original correlated-subquery versions — this proves the
--     rewrite is logically correct, not just faster.
--   - NOT verified (cannot be, from this sandbox): actual query TIMING or a
--     real EXPLAIN plan. No DB connection here, and no way to install
--     Postgres locally to build one (no root/sudo in this sandbox — checked
--     and confirmed before writing this). The EXPLAIN commands below are
--     for you to run; they were not run by me.
-- ============================================================================

-- Reads real 2025 parcel_tax_year data. Read-only — no writes.

-- ── Diagnostic EXPLAIN commands (run these yourself before the full query) ──
--
-- (A) SAFE — plan-only, does NOT execute the query, returns in well under a
-- second even though the original correlated-subquery version would take
-- 50+ minutes to actually run. This is what answers your point 1 (a real
-- EXPLAIN specifically on tier1_counts) without needing to wait:
--
--   EXPLAIN
--   WITH subj AS (
--     SELECT p.geo_id, p.classi_cd, p.neighborhood_cd, p.state_cd1, pty.market_value
--     FROM parcel p
--     JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
--     WHERE p.geo_id NOT LIKE 'AJR%'
--       AND p.classi_cd IS NOT NULL AND TRIM(p.classi_cd) <> ''
--   ),
--   tier1_counts AS (
--     SELECT s.geo_id,
--            (SELECT COUNT(*) FROM subj o
--              WHERE o.geo_id <> s.geo_id
--                AND UPPER(TRIM(o.classi_cd)) = UPPER(TRIM(s.classi_cd))
--                AND o.neighborhood_cd = s.neighborhood_cd
--                AND o.market_value BETWEEN s.market_value * 0.75 AND s.market_value * 1.25
--            ) AS tier1_peer_count
--     FROM subj s
--   )
--   SELECT * FROM tier1_counts;
--
-- What to look for in the output: a "CTE subj" scan appearing under a
-- "Materialize" node, and that Materialize being the INNER side of a
-- "Nested Loop" (not a "Hash Join") — that combination is the O(N²) pattern
-- described above. If you see that, this diagnosis is confirmed.
--
-- (B) Real timing on the REWRITTEN version below — safe to run with ANALYZE
-- since it should actually complete:
--
--   EXPLAIN (ANALYZE, BUFFERS)
--   <tier1_counts query from section 1 below>
--
-- ──────────────────────────────────────────────────────────────────────────

-- 1. For every parcel, how many OTHER parcels share its exact classi_cd AND
--    neighborhood_cd, within the existing ±25% 2025 market-value band? This
--    is Tier 1 (the tightest, most-relevant match) — the count here directly
--    answers "how often does the tightened filter have enough exact-use-code
--    neighbors to work without falling back."
--    REWRITTEN (July 2026): LEFT JOIN + GROUP BY instead of a correlated
--    subquery — see notice above. LEFT JOIN (not INNER) so a parcel with
--    ZERO peers still produces one row with tier1_peer_count = 0, instead of
--    disappearing from the result entirely.
WITH subj AS (
  SELECT p.geo_id, UPPER(TRIM(p.classi_cd)) AS cc, p.neighborhood_cd, p.state_cd1,
         pty.market_value
  FROM parcel p
  JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
  WHERE p.geo_id NOT LIKE 'AJR%'
    AND p.classi_cd IS NOT NULL AND TRIM(p.classi_cd) <> ''
),
tier1_counts AS (
  SELECT s.geo_id, COUNT(o.geo_id) AS tier1_peer_count
  FROM subj s
  LEFT JOIN subj o
    ON o.geo_id <> s.geo_id
   AND o.cc = s.cc
   AND o.neighborhood_cd = s.neighborhood_cd
   AND o.market_value BETWEEN s.market_value * 0.75 AND s.market_value * 1.25
  GROUP BY s.geo_id
)
SELECT
  CASE
    WHEN tier1_peer_count = 0 THEN '0 (needs Tier 2/3 fallback)'
    WHEN tier1_peer_count BETWEEN 1 AND 2 THEN '1-2 (thin, "limited" flag would show)'
    WHEN tier1_peer_count BETWEEN 3 AND 4 THEN '3-4 (below full 5, still Tier 1)'
    ELSE '5+ (full Tier 1 result)'
  END AS tier1_bucket,
  COUNT(*) AS parcel_count,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_all_parcels
FROM tier1_counts
GROUP BY 1
ORDER BY 1;

-- 2. Of the parcels with ZERO Tier-1 peers above, how many pick up peers once
--    relaxed to Tier 2 (same exact use code, same state_cd1 prefix, any
--    neighborhood) or Tier 3 (exact use code, county-wide, ±40% band)? This
--    tells you how much Tier 2/3 actually rescue vs. how often you'd still
--    fall all the way to Tier 4 (broad-category fallback).
--    REWRITTEN (July 2026): the NOT EXISTS correlated subquery (zero_tier1)
--    is now a LEFT JOIN ... WHERE o.geo_id IS NULL anti-join, and the two
--    correlated subqueries in rescue_counts (tier2/tier3) are now their own
--    LEFT JOIN + GROUP BY CTEs, same reasoning as query 1 above. This query
--    had THREE separate O(N²) correlated-subquery operations chained
--    together in the original version — worse than query 1, not better.
WITH subj AS (
  SELECT p.geo_id, UPPER(TRIM(p.classi_cd)) AS cc, p.neighborhood_cd, p.state_cd1,
         pty.market_value
  FROM parcel p
  JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.tax_year = 2025
  WHERE p.geo_id NOT LIKE 'AJR%'
    AND p.classi_cd IS NOT NULL AND TRIM(p.classi_cd) <> ''
),
zero_tier1 AS (
  SELECT s.*
  FROM subj s
  LEFT JOIN subj o
    ON o.geo_id <> s.geo_id
   AND o.cc = s.cc
   AND o.neighborhood_cd = s.neighborhood_cd
   AND o.market_value BETWEEN s.market_value * 0.75 AND s.market_value * 1.25
  WHERE o.geo_id IS NULL
),
tier2 AS (
  SELECT z.geo_id, COUNT(o.geo_id) AS tier2_peer_count
  FROM zero_tier1 z
  LEFT JOIN subj o
    ON o.geo_id <> z.geo_id
   AND o.cc = z.cc
   AND LEFT(UPPER(o.state_cd1), 1) = LEFT(UPPER(z.state_cd1), 1)
   AND o.market_value BETWEEN z.market_value * 0.75 AND z.market_value * 1.25
  GROUP BY z.geo_id
),
tier3 AS (
  SELECT z.geo_id, COUNT(o.geo_id) AS tier3_peer_count
  FROM zero_tier1 z
  LEFT JOIN subj o
    ON o.geo_id <> z.geo_id
   AND o.cc = z.cc
   AND o.market_value BETWEEN z.market_value * 0.60 AND z.market_value * 1.40
  GROUP BY z.geo_id
)
SELECT
  COUNT(*) AS zero_tier1_parcels,
  COUNT(*) FILTER (WHERE tier2.tier2_peer_count > 0) AS rescued_by_tier2,
  COUNT(*) FILTER (WHERE tier2.tier2_peer_count = 0 AND tier3.tier3_peer_count > 0) AS rescued_by_tier3_only,
  COUNT(*) FILTER (WHERE tier2.tier2_peer_count = 0 AND tier3.tier3_peer_count = 0) AS falls_to_broad_category_fallback
FROM zero_tier1 z
JOIN tier2 ON tier2.geo_id = z.geo_id
JOIN tier3 ON tier3.geo_id = z.geo_id;

-- 3. How many parcels have NO classi_cd on file at all (can't exact-match,
--    forced straight to the Tier 4 broad-category fallback regardless of how
--    common or rare their true use would be)? Unchanged — this was always a
--    simple aggregate, never part of the slowdown.
SELECT COUNT(*) AS parcels_with_no_classi_cd
FROM parcel
WHERE geo_id NOT LIKE 'AJR%'
  AND (classi_cd IS NULL OR TRIM(classi_cd) = '');
