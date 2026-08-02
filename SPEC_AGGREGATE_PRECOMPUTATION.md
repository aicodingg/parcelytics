# SPEC: Aggregate Precomputation Architecture ("Compute-at-Write, Serve-at-Read")

**Status:** Approved direction (Fable review, 2026-08-02). This spec turns that
review into a concrete, buildable plan.
**Origin:** Four separate production `QueryCanceled` timeouts (Sentry PYTHON-FLASK-5,
6, 7, and a live recurrence of a previously-mitigated query within `county_snapshot`)
across 2026-07-30 to 08-02, three fixed individually with real live evidence, before
recognizing the shared root cause: expensive, county-wide aggregate queries computed
live on every request, against tables that only ever grow.
**Core principle:** the platform already applies this exact idea to data confidence
— established once at ingest, not re-derived at display time. This spec applies the
same idea to aggregates.

## Problem statement

Parcelytics data changes on a real-world, government calendar — certified exports,
preliminary exports, billing loads — roughly ten meaningful write events a year,
already funneled through the existing gated ETL pipeline (Ingestion Conservation
Gate, `ingest_gate.py`). Between two loads, every county-wide aggregate (the Market
Snapshot's per-type breakdowns, medians, percentile bands, top-moving neighborhoods,
risk-flag counts) is mathematically constant. Computing one live, on a page view,
recomputes that constant potentially thousands of times between loads, against
tables that grow with every new county. The 8-second `statement_timeout` isn't the
problem — it's the symptom that surfaced the real one.

**This will get meaningfully worse on purpose**: Dallas County and Harris County
(Houston) are the next planned expansions, multiplying relevant row counts several
times over. Continuing to fix individual queries reactively, one Sentry crash at a
time, does not scale with that roadmap.

## Design: three read tiers

### Tier 1 — County-wide aggregates → precomputed summary tables

Everything the Market Snapshot page serves (per-property-type breakdowns via
`GROUPING SETS`, county medians/percentiles, top-moving neighborhoods, new
construction and risk-flag counts) becomes rows in small, purpose-built summary
tables — expected size: hundreds to low thousands of rows each, not millions.

**Written as the final stage of the load pipeline**, inside the existing
`ingest_gate.py`/`parcel_rollup.py` flow — not on a separate schedule (a cron job
would be a second clock that can drift from the real one; tying refresh to the
actual load event means the summary is exactly as fresh as the data, by
construction).

**Three non-negotiable implementation properties:**

1. **Provenance-stamped.** Every summary table row carries
   `(source_import_batch_id, refreshed_at)`. This extends the platform's existing
   data-freshness ledger concept to aggregates for free — the property page's
   "Data as of" language, or an equivalent for county-wide stats, can read this
   directly rather than being hardcoded.
2. **Staleness must be loudly detectable, never silently trusted.** A standing
   assertion (harness check, or a startup/health check) verifies every summary
   table's `source_import_batch_id` matches the latest completed load's batch id.
   Modeled on the existing year-bounds invariant in `loaders/db.py`
   (`is_valid_tax_year`) that made a whole class of contamination impossible by
   construction — this does the same for staleness.
3. **No live fallback, ever.** If a summary table is missing or confirmed stale, the
   page must show an honest "data temporarily unavailable" state — it must NOT fall
   back to computing the aggregate live. A silent fallback both hides real pipeline
   failures and resurrects the exact timeout class this spec exists to retire.

**Refresh mechanism:** build each summary into a shadow table, then swap
transactionally (e.g. `ALTER TABLE ... RENAME`, or an equivalent atomic swap) so
readers never see a half-written summary mid-refresh. Expected cost: minutes of
pipeline time per load, roughly ten times a year — negligible.

### Tier 2 — Per-parcel reads → stay live, unchanged

Indexed single-parcel lookups (the property detail page's own data) are exactly
what Postgres does well already. No change in scope for this spec.

### Tier 3 — Parameterized group queries → live queries against a precomputed stats layer

Peer-set and benchmark endpoints (`api_peer_benchmark_local`, `api_peer_set`, and
similar) can't be fully precomputed since users' filter choices vary — but their
real expense is the inner aggregation, not the final filtering step.

**Precompute one `group_stats` table**, granular by
`(neighborhood_cd/market-segment, state_cd1 class, classi_cd/use_code, tax_year)`,
holding: `count, min, p25, median, p75, max` for the core metrics (market_value,
assessed_value, total_tax or the entity-tax-sum equivalent). Expected size: tens of
thousands of rows, refreshed on the same load-triggered schedule as Tier 1.

Peer/benchmark endpoints then join this small table instead of aggregating millions
of raw rows per request — this is the durable, shared form of what tonight's
`api_peer_benchmark_local` fix did as a one-off. This also directly retires the
currently-live-broken `county_snapshot` "breakdown" query (`GROUPING SETS` +
percentile math) — once built, that query becomes a `SELECT` from a small,
pre-aggregated table.

## Explicitly declined, with reasoning

**A separate caching layer (Redis, HTTP-level cache) — declined for now.** A summary
table refreshed by its own sole writer already IS a cache with provably-correct
invalidation (it's invalidated exactly when, and only when, the data it summarizes
changes). A separate cache adds real infrastructure, a second copy of truth, and the
exact invalidation-correctness problem this spec is designed to avoid — to speed up
reads that will already be single-digit milliseconds once Tier 1/3 land. Narrow
exception to revisit later, not now: dumb HTTP/CDN caching on fully-static aggregate
endpoints, only if public traffic volume itself ever becomes a load problem
independent of query cost.

**`query_no_nestloop()` — plan retirement, do not expand.** Its own docstring
already warns against broad reuse without the same on/off verification used to
justify each existing use. Tonight proved even its already-verified uses expire as
data grows. Each query migrated into Tier 1 or Tier 3 should have its
`query_no_nestloop()` call site removed as part of that migration. Anything still
using it once this migration completes joins the query registry (below) with a real
duration budget, so its eventual expiry fails a gate instead of surfacing as a user
error.

## New standing mechanism: query registry + load-time budget gate

Merges "proactive monitoring" and "scheduled review" into one load-triggered
mechanism, since the real trigger for degradation is data growth, and data growth
happens at known, discrete moments (loads, county launches) — not calendar time.

- A registry of named, genuinely-expensive queries that remain live post-migration
  (Tier 3 survivors, anything still on `query_no_nestloop()`), each with a real
  duration budget — proposed: under 50% of `statement_timeout` (currently 8s, so
  under 4s), leaving real margin, not a razor's edge.
- Run with live `EXPLAIN ANALYZE` (or equivalent real timing) as part of **every
  load's own verification step** and **every new county's launch** — a query that's
  degraded fails the load/launch gate before real users exist on the new data. This
  changes "found via a Sentry crash in production" into "blocked a load before it
  shipped" — the correct severity for this class of problem.
- Two cheap complements: enable `pg_stat_statements` on the production Postgres
  instance (available on Render), and add a "top-N queries by mean time" glance to
  the existing weekly platform-risk checklist. Sentry remains the real-world
  backstop underneath all of this, not replaced by it.

## Second-order dependency: partition before Dallas

Not part of the original four-option list, but load-bearing for the roadmap:
**partition `parcel_tax_year` and `tax_billing_entity` (the largest, fastest-growing
tables) by county, before Dallas data is loaded.** Tier 1's summary tables make
county-wide aggregates immune to multi-county growth by construction, but Tier 2's
per-parcel scans and Tier 3's live group-filtered queries still benefit from
partition pruning — a Travis County query should touch only Travis-sized data
forever, regardless of how large Harris County's data eventually becomes. This
should be the **first** task of Dallas-launch prep, not something bolted on
afterward.

## Migration order

1. **`group_stats`** (Tier 3) first. One table, one refresh function, one staleness
   assertion. This alone gives the currently-live-broken `county_snapshot` breakdown
   query a real fix path via migration, not another live patch — though if
   production pressure demands an interim fix before this lands, that's acceptable
   as a stopgap; the migration still lands regardless, and the stopgap becomes this
   query's documented history the same way `query_no_nestloop()`'s own history is
   documented today.
2. **Snapshot summary tables** (Tier 1) — Parts 1-4 of `_compute_snapshot_data()`
   all served from summary tables; the Market Snapshot page's request path performs
   zero live aggregation once this lands.
3. **Query registry + load-time budget gate** wired into the existing load/verify
   pipeline; `pg_stat_statements` enabled; weekly risk-checklist glance added.
4. **Peer/benchmark endpoints rebased onto `group_stats`**; `query_no_nestloop()`
   call sites removed as each query migrates off it.
5. **Partition decision executed** as the literal first task of Dallas County prep,
   not deferred to "if it becomes a problem."

## Acceptance criteria (measurable, verified live — not by reasoning alone)

- Market Snapshot page: p95 response time under ~300ms.
- Zero `GROUPING SETS` or `percentile_cont` reachable from any live request handler
  (should be grep-able as a real, checkable invariant, not just an intention).
- Every summary/stats table row carries a real `source_import_batch_id`; the
  staleness assertion passes (matches the latest completed load) at all times in
  production.
- Every query registry entry passes its real duration budget, measured on real
  production-scale data, at every load and every county launch — not assumed.
- `query_no_nestloop()` call-site count is monotonically decreasing across this
  migration, trending toward zero (or a small, registry-tracked, budget-verified
  remainder).

**Per this week's own hard-won lesson** (two confirmed-wrong conclusions from
code-level reasoning alone, both corrected only by live measurement): every one of
these criteria must be verified with real `EXPLAIN ANALYZE`/live timing evidence
before being considered met, never accepted on reasoning alone.

## Tradeoffs, stated plainly (per Fable's own honest framing)

**Real cost:** a new pipeline stage per summary table (schema surface grows
deliberately — each aggregate becomes a table + a refresh function + a staleness
assertion); minutes added to every load; and a real, ongoing discipline requirement
that aggregation logic lives *only* inside refresh functions — a page quietly
re-deriving a stat live anywhere would fork the truth between the summary and the
live computation, defeating the whole point.

**Real benefit:** the timeout class is retired rather than continually outrun one
query at a time; page loads get *faster* as more counties are added, not slower;
aggregate data gets the same provenance/freshness guarantees the platform already
gives per-property data; Dallas and Harris become "a row count," not "a fresh
performance crisis."

**What this explicitly does NOT fix:** a genuinely novel expensive query someone
writes next year, outside this framework. That's precisely what the query registry
and load-time gate exist for — the monitoring mechanism ships as part of this same
migration, not as an afterthought once the migration is "done."

**Why this isn't overengineering for a platform this size:** every piece of this
rides on infrastructure that already exists — the ETL gate pipeline, the batch/
provenance concept, the existing verification harness discipline. This is one more
gated stage in a pipeline that already knows how to gate, not a new category of
infrastructure being introduced from scratch.
