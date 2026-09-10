# PX — Mission 3 Completion Report: DB $0 Cleanup + Live Validation

**Executed:** 2026-09-10 · **Prepared by:** PM (live, with Diego executing every command against production) · **Governed by:** `parcelytics.md`, Mission 2 (`PX_POST_M1_ARCHITECTURE_RECOMMENDATION.md`), the Mission 3 brief.

---

## Executive Summary

**What was cleaned:** 9 tables and 10 indexes — every object in production suffixed `_old_pre_partition` or `_pre_partition_backup`, the complete set of artifacts retained by `migrate_county_partitioning.py` as a deliberate rollback safety net. **Why it was safe:** zero foreign keys, zero other database-level dependents, and zero live code references on all 19 objects, independently confirmed twice (Cowork's repo grep; Diego's own wider-net grep against production's `git status`-clean working tree) — plus, going beyond the brief's own bar, an actual row-count check on the two largest tables to rule out stale-statistics artifacts before treating them as inert. **The supervised DROP occurred**, with explicit approval given immediately before execution, per the mission's stop condition. **Overall result:** clean success — all 19 objects gone, zero errors, two live-table row counts confirmed unchanged across the operation.

**One correction to Mission 2's own estimate, made during this mission's live validation rather than assumed:** Mission 2 projected the win at up to ~5.8 GB, reasoning from `tax_billing_entity`'s last-known 1036 MB `_old_pre_partition` twin. Live querying (§ Pre-Cleanup Evidence) found **no such twin exists** — `tax_billing_entity` has no `_old_pre_partition` table at all. The real, exhaustively-measured candidate set was **~822 MB** (~630 MB tables + ~192 MB indexes), not ~5.8 GB. This is disclosed here rather than left for someone else to discover; Mission 2's broader ~5.8 GB "not serving reads" figure evidently includes other categories (the re-keyed billing partitions, zero-scan indexes) beyond this specific cleanup's scope, per Report 1's original breakdown.

## Pre-Cleanup Evidence

Measured live, 2026-09-10, in sequence:

1. **Exhaustive object inventory** (`pg_class` matched against both `%pre_partition%` and `%_old%`, not just the expected suffix, to rule out variant naming): 19 objects total.

   | Object | Kind | Size |
   |---|---|---|
   | parcel_metrics_old_pre_partition | table | 546 MB |
   | idx_parcel_metrics_year_risk_covering_pre_partition_backup | index | 84 MB |
   | idx_metrics_year_etr_pre_partition_backup | index | 81 MB |
   | parcel_2026_preliminary_snapshot_old_pre_partition | table | 62 MB |
   | idx_metrics_year_pre_partition_backup | index | 19 MB |
   | group_stats_old_pre_partition | table | 13 MB |
   | tax_billing_quarantine_old_pre_partition | table | 7416 kB |
   | idx_metrics_risk_jump_pre_partition_backup | index | 1120 kB |
   | tax_delinquent_old_pre_partition | table | 1064 kB |
   | snapshot_neighborhood_movers_old_pre_partition | table | 552 kB |
   | county_tax_rate_old_pre_partition | table | 416 kB |
   | idx_metrics_delinquent_pre_partition_backup | index | 408 kB |
   | idx_metrics_cap_expiry_signal_pre_partition_backup | index | 104 kB |
   | idx_metrics_cap_step_up_pre_partition_backup | index | 96 kB |
   | snapshot_breakdown_old_pre_partition | table | 88 kB |
   | idx_rate_entity_pre_partition_backup | index | 48 kB |
   | idx_rate_year_pre_partition_backup | index | 40 kB |
   | snapshot_totals_old_pre_partition | table | 24 kB |
   | idx_metrics_cap_expiry_pre_partition_backup | index | 8192 bytes |

   `[measured 2026-09-10, live psql]`

2. **`tax_billing_entity` check:** confirmed only `tax_billing_entity` itself exists in production — no `_old_pre_partition` twin. This directly corrects Mission 2's storage-win estimate `[measured 2026-09-10, live psql]`.

3. **Dependency check, all 19 objects:** `pg_constraint` (foreign keys referencing each object) and `pg_depend` (other non-internal dependents) both returned **0 for every object, no exceptions** `[measured 2026-09-10, live psql]`.

4. **Statistics/activity check, all 9 tables:** `last_vacuum`, `last_autovacuum`, `last_analyze` all NULL; `n_live_tup` = 0 for every table `[measured 2026-09-10, live psql]`. Per the brief's own instruction ("do not infer unused solely because grep returned nothing"), this was **not** taken as proof of emptiness — `n_live_tup=0` with real on-disk bytes indicated stale statistics, not empty tables, so a direct count was run.

5. **Direct row count, the two largest tables:** `parcel_metrics_old_pre_partition` = **2,796,316** rows; `parcel_2026_preliminary_snapshot_old_pre_partition` = **433,050** rows `[measured 2026-09-10, live psql]`. Confirms these are populated, orphaned artifacts — not empty shells — and that the zero-dependency/zero-reference findings above are the reason they're safe to drop, not an accident of them being empty.

6. **Code-reference check (repo-side):** Cowork's grep (session-prior) plus Diego's independent, wider-pattern re-grep against the live working tree — both returned zero references to any of the 19 object names outside `migrate_county_partitioning.py` and its own test file.

7. **Database size baseline:** `pg_database_size` = 13 GB before the operation `[measured 2026-09-10]`.

## Approved Object(s)

All 9 tables and 10 indexes listed in the inventory table above. Rationale, common to all 19: each is an artifact `migrate_county_partitioning.py` deliberately retained as a rollback safety net at the time of the county-partitioning migration; the migrated, renamed tables they preceded have served production without incident since (Travis fully live throughout, Dallas since 2026-08-28); zero database-level dependents; zero live code references; the retention window Mission 2 identified as the original justification for keeping them has been satisfied.

## Approval Record

Full evidence chain (inventory → dependency check → statistics/row-count verification → dump verification) was presented in chat. The exact `BEGIN...COMMIT` SQL block was shown in full before execution. Diego gave explicit, separate approval ("approved") only after all evidence was reviewed and the dumps were confirmed written to two locations — approval was requested and given immediately before execution, not in advance of the evidence.

## SQL Executed

```sql
BEGIN;

DROP INDEX IF EXISTS idx_metrics_cap_expiry_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_cap_expiry_signal_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_cap_step_up_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_delinquent_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_risk_jump_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_year_etr_pre_partition_backup;
DROP INDEX IF EXISTS idx_metrics_year_pre_partition_backup;
DROP INDEX IF EXISTS idx_parcel_metrics_year_risk_covering_pre_partition_backup;
DROP INDEX IF EXISTS idx_rate_entity_pre_partition_backup;
DROP INDEX IF EXISTS idx_rate_year_pre_partition_backup;

DROP TABLE IF EXISTS county_tax_rate_old_pre_partition;
DROP TABLE IF EXISTS group_stats_old_pre_partition;
DROP TABLE IF EXISTS parcel_2026_preliminary_snapshot_old_pre_partition;
DROP TABLE IF EXISTS parcel_metrics_old_pre_partition;
DROP TABLE IF EXISTS snapshot_breakdown_old_pre_partition;
DROP TABLE IF EXISTS snapshot_neighborhood_movers_old_pre_partition;
DROP TABLE IF EXISTS snapshot_totals_old_pre_partition;
DROP TABLE IF EXISTS tax_billing_quarantine_old_pre_partition;
DROP TABLE IF EXISTS tax_delinquent_old_pre_partition;

COMMIT;
```

Executed exactly as shown, in one transaction, no modification. Output: 10× `DROP INDEX`, 9× `DROP TABLE`, `COMMIT` — no errors, no warnings `[measured 2026-09-10, psql output]`.

**Rollback path preserved:** all 9 tables individually dumped via `pg_dump -F c` immediately before the drop, verified present with proportionate non-trivial file sizes (18.5 KB–37.7 MB depending on table), and copied to two locations (`/tmp/pre_drop_dumps/` and `~/Parcelytics/pre_drop_dumps_20260910/`, the latter surviving a machine restart) before execution proceeded `[measured 2026-09-10]`.

## Post-Cleanup Validation

- **Object existence check:** `SELECT relname FROM pg_class WHERE relname LIKE '%_old_pre_partition' OR relname LIKE '%_pre_partition_backup'` → **0 rows**. All 19 objects confirmed gone `[measured 2026-09-10]`.
- **Live-table integrity:** `parcel_metrics` count = 6,372,950 (vs. 6,370,606 at the last full count, 2026-09-02 — the ~2,344-row difference is ordinary production write activity in the intervening 8 days, not attributable to this operation); `county_tax_rate` count = 4,349, matching the pre-operation baseline exactly (TRAVIS 3,256 + DALLAS 1,093) `[measured 2026-09-10]`.
- **`parcel_metrics` table size, directly:** 2,909 MB — identical to its 2026-09-02 measurement, confirming the live table itself was untouched by dropping its `_old_pre_partition` namesake `[measured 2026-09-10]`.
- **No errors encountered** at any step of the transaction or the post-checks.

## Before / After Measurements

| Metric | Before | After |
|---|---|---|
| `_old_pre_partition` / `_pre_partition_backup` objects | 19 (9 tables + 10 indexes) | **0** |
| Combined size of those 19 objects | ~822 MB (630 MB tables + 192 MB indexes) | 0 (reclaimed at catalog level) |
| `pg_database_size(current_database())` | 13 GB | 13 GB (see Performance Impact — not yet reflected) |
| `parcel_metrics` table size | 2909 MB (unchanged) | 2909 MB |
| `parcel_metrics` row count | 6,370,606 (2026-09-02 baseline) | 6,372,950 (ordinary growth) |
| `county_tax_rate` row count | 4,349 | 4,349 (unchanged) |

## Performance Impact

**No performance improvement is claimed, because none was measured.** Per the mission's own instruction ("do not claim performance improvement unless it is actually measured" / "a storage reduction may be measurable even if query performance does not materially change"), this mission measured storage reclamation only — it did not re-run `EXPLAIN ANALYZE` on any query, did not re-check the heap cache-hit ratio, and did not assess whether removing ~822 MB of cold objects from a 1 GB-RAM instance changed buffer-cache behavior in practice. That measurement is explicitly deferred to a future database/performance mission per the brief's own scope discipline (§7).

**Storage reduction itself is honestly qualified, not overclaimed:** all 19 objects are confirmed gone at the catalog level (§ Post-Cleanup Validation), but `pg_database_size()` still reports 13 GB immediately after the drop. This is expected Postgres/Render behavior — freed pages are not always reflected in reported database size until a later storage recalculation — and is stated here as an open, honest gap rather than a claimed win: the ~822 MB reduction is real at the object level and not yet independently confirmed at the whole-database-size level.

## Cost

**Incremental cost: $0.** No Render plan change, no new service, no paid tooling. Only existing infrastructure (production Postgres, local `pg_dump`) was used.

## Remaining Database Findings (deferred, not acted on)

Per the mission's explicit scope discipline (§7), the following remain open for a future database/performance mission, unchanged by this cleanup:
- Heap cache-hit ratio (measured 22% as of 2026-09-02; not re-measured here).
- Large sequential-scan activity on `parcel_tax_year` and others.
- The 10 `verify_index_coverage.py --index-source live` findings — **still not triaged**; this mission's scope was the pre-partition objects only, not this separate index question.
- The two canonical-writer scanner false-positive candidates (`verify_rollup_canonical.py`, `verify_tax_billing_rollup_canonical.py`) — still not triaged.
- Autovacuum/`ANALYZE` configuration and statistics freshness on the largest live tables — notably, this mission's own evidence (§ Pre-Cleanup Evidence, item 4) found the *dropped* tables had never been analyzed; whether the *live* tables' autovacuum is keeping up was flagged by Mission 2 as a candidate check but was not run here (out of scope; would have required an `ANALYZE`, which this mission was not authorized to run without separate approval).
- Connection pooling, statement timeouts, query-plan issues, partition architecture, database resizing, derived-table architecture — all untouched, as instructed.
- The `tax_billing_entity` native-partitioning trigger question (raised in Mission 2) is unaffected by this cleanup and remains open.

## Unknowns

- Whether `pg_database_size()`'s continued 13 GB reading will drop after Render's next storage recalculation, or whether some other factor (e.g. WAL, TOAST, or other objects not examined here) is keeping total size steady despite the drop — `[unknown]`, would need a follow-up size check in a day or more.
- Whether the ~822 MB reclaimed materially affects cache-hit ratio or query performance — `[unknown]`, requires the deferred performance re-measurement noted above.
- Why `tax_billing_entity` specifically has no `_old_pre_partition` twin when 9 other tables in the same `TABLE_SPECS` migration batch do — `[unknown]`; possible explanations (different migration path for that table, prior manual cleanup, a naming difference this mission's exhaustive search still wouldn't catch) were not investigated further, as it was outside this mission's scope once the absence was confirmed safe to proceed around.

## Recommendations

**P0 — required before further production DB work**
- None. This mission's scope is closed; nothing here blocks any other mission.

**P1 — important next improvements**
- Re-check `pg_database_size()` in a few days to confirm the ~822 MB reduction is eventually reflected at the whole-database level; if it never is, investigate why (this itself would be a small, cheap diagnostic, not a new cleanup).
- Run the deferred live-mode validations from Mission 2's Step B that this mission's scope explicitly excluded: the 10 index findings, the two canonical-writer scanner failures, and a fresh cache-hit/sequential-scan measurement — now on a database ~822 MB smaller, which is itself useful context for interpreting those results.
- Investigate why `tax_billing_entity` has no `_old_pre_partition` twin, since it directly affects the accuracy of any future storage-win estimate involving that table.

**P2 — later optimization**
- Everything in Remaining Database Findings above that requires broader database work: autovacuum tuning, connection pooling, the native-partitioning trigger evaluation, and any subsequent cleanup category Mission 2 identified but this mission did not touch (re-keyed billing partitions, zero-scan indexes).

---

Per the mission's stop condition: Mission 2's rulings were verified against the live DB (and one, the storage estimate, was corrected rather than assumed); the exact cleanup objects were independently validated as obsolete/unused through dependency checks, code-reference checks, and a direct row-count sanity check beyond what the brief required; a pre-cleanup baseline was established; the DROP SQL was reviewed in full before execution; explicit human approval was obtained immediately before running it; only the approved DROP was executed; post-cleanup validation is complete; before/after measurements are recorded, including the honest caveat on database-size reporting; no unrelated production changes were made. **Stopping here**, per instruction — no Stage A implementation, no broader database optimization performed.
