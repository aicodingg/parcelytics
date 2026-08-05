# SPEC — County Partitioning: Investigation & Design (PARTITION-1)

**Author:** Claude (Cowork session)
**Date:** August 5, 2026
**Status:** Investigation and design only — NOT implemented, NOT reviewed yet. No schema change has been made against any real table. For review by Diego and Fable before any follow-up implementation brief is written.
**Prompted by:** `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §7's own flagged prerequisite, and tonight's AGGPRECOMP-2 measurement (snapshot-refresh at 51-52% of its declared budget on Travis-only data).

---

## 0. Verdict, in four sentences

`county_code` prior art already exists in this codebase — `county_benchmark.county_code VARCHAR(20) NOT NULL DEFAULT 'TRAVIS'` — and that convention, not FIPS and not a new scheme, is what every other table should adopt. `geo_id` and `prop_id` are TCAD-internal account numbers with no evidence of cross-CAD coordination, so treating them as globally unique across counties is an unverified assumption this design does not make — `county_code` must join the primary key on every table keyed by them, not just ride along as a filter column. At Travis+Dallas+Harris scale (low millions of rows, not tens of millions), native Postgres declarative partitioning buys real query-plan benefits Postgres already gets for free from a well-chosen composite index — the recommendation is the lighter-weight approach (`county_code` as a leading PK/index column, no `PARTITION BY`), with a concrete, numeric trigger condition for revisiting that later. Every part of this is investigation and design for review, not a migration that has been run.

---

## 1. Real table inventory

### 1.1 Every table found, systematic search

Searched `schema.sql` for every `CREATE TABLE` (20 statements, including `_shadow` twins), plus a repo-wide grep for `CREATE TABLE` outside `schema.sql` — found one more, `tax_billing_quarantine`, defined inline in `loaders/quarantine_contamination.py` rather than in `schema.sql`. Full real list:

**Core, populated, county-scoped tables (need `county_code`):**

| Table | Current PK | Current indexes (non-PK) | Real FK |
|---|---|---|---|
| `parcel` | `geo_id` (single-column) | `idx_parcel_prop_id(prop_id)`, `idx_parcel_owner(owner_name)`, `idx_parcel_neighborhood_cd`, `idx_parcel_classi_cd`, `idx_parcel_year_built`, `idx_parcel_use_code_exact` (expr), `idx_parcel_classi_state_expr` (expr) | — |
| `parcel_tax_year` | `(geo_id, tax_year)` | `idx_pty_year(tax_year)`, `idx_pty_year_market_value(tax_year, market_value)` | — |
| `tax_billing` | `(geo_id, tax_year)` | `idx_billing_geo(geo_id)` | — |
| `tax_billing_entity` | `(geo_id, tax_year, entity_code)` | (see `idx_metrics_year_etr` — actually on `parcel_metrics`; no dedicated index on this table beyond its PK, confirmed via grep) | — |
| `tax_delinquent` | `geo_id` (single-column) | none beyond PK | — |
| `prop_unit` | `prop_id` (single-column) | `idx_prop_unit_geo_id(geo_id)` | — |
| `prop_unit_tax_year` | `(prop_id, tax_year)` | `idx_put_year(tax_year)`, `idx_put_geoid_year(geo_id, tax_year)` | — |
| `parcel_metrics` | `(geo_id, tax_year)` (confirmed via schema read) | `idx_metrics_year`, `idx_metrics_risk_jump` (partial), `idx_metrics_cap_expiry` (partial), `idx_metrics_delinquent` (partial), `idx_metrics_cap_step_up` (partial), `idx_metrics_cap_expiry_signal` (partial), `idx_metrics_parcel_metrics_year_risk_covering`, `idx_metrics_year_etr` | **`geo_id REFERENCES parcel(geo_id)`** — a real FK constraint, confirmed at schema.sql:129 |
| `county_tax_rate` | `(entity_code, tax_year)` | `idx_rate_year(tax_year)`, `idx_rate_entity(entity_code)` | — |

**Already has `county_code` (prior art — see §2):**

| Table | Current PK |
|---|---|
| `county_benchmark` | `(county_code, tax_year, property_type_label)` |

**Tier 1/3 precomputed tables, built tonight (AGGPRECOMP-1/2), county-scoped by construction even though nothing points at a second county yet:**

| Table | Current PK | Grain columns that are themselves county-specific taxonomy |
|---|---|---|
| `group_stats` | `(neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year)` | `neighborhood_cd_key` and `classi_cd_key` are Travis's own coding schemes |
| `snapshot_breakdown` | `(view, ptype)` | `ptype`/`sort_key` derive from `snapshot_taxonomy.py`'s Travis-specific `classi_cd`/`state_cd1` mappings |
| `snapshot_totals` | `view` (single-column) | same |
| `snapshot_neighborhood_movers` | `(view, neighborhood_cd)` | `neighborhood_cd` is Travis's own coding scheme |

**Operational/audit tables — county-relevant but a different shape of change:**

| Table | Current PK | Note |
|---|---|---|
| `ingest_audit` | `id` (BIGSERIAL) | `source_tag` (e.g. `'certified_2025'`) would become ambiguous across counties — `'certified_2025'` could mean Travis's or Dallas's load. Needs `county_code` as a real column for filtering/audit-trail clarity, not necessarily the PK (this table is append-only, `id` already disambiguates rows; the risk is a *human* reading two counties' `'certified_2025'` rows and not being able to tell them apart, not a data-integrity risk). |
| `load_batch` | `batch_id` (BIGSERIAL) | Same shape of concern as `ingest_audit` — not a collision risk (surrogate key), but worth a `county_code` column so a batch's scope is self-describing rather than inferred from which script ran it. |
| `tax_billing_quarantine` | `(geo_id, tax_year)` | Defined in `loaders/quarantine_contamination.py`, not `schema.sql` — same geo_id-collision exposure as `tax_billing`. Small table (12 hardcoded geo_ids as of this session), low priority but should not be forgotten in a real migration. |
| `parcel_2026_preliminary_snapshot` | `geo_id` (single-column) | Explicitly a one-time, narrow-scoped, NOT-kept-in-sync table per its own docstring (schema.sql:597-618) — recommend simply retiring/archiving this rather than migrating it; it was never designed to be a durable multi-year, let alone multi-county, structure. |

### 1.2 Real row counts and disk sizes — honest gap, not fabricated numbers

This sandbox has no live Postgres connection (same constraint disclosed all session). I will not invent row counts or `pg_total_relation_size()` figures. What I have from this session's own real, documented work:

- `parcel`: **517,614 total rows** (all `state_cd1` prefixes, confirmed live, June 2026 — `KNOWN_LIMITATIONS.md` §"state_cd1 prefix population"). Residential subset alone: 317,461. Commercial (post-AJR-exclusion): 13,527.
- `tax_billing`: **426,491 rows** for 2025 alone (confirmed live, per this session's earlier billing-verification work) — real total across all years not separately confirmed this session.
- `snapshot_breakdown` / `snapshot_totals` / `snapshot_neighborhood_movers`: **190 / 11 / 4,626 rows** respectively — the real row counts from tonight's AGGPRECOMP-2-FIX-2 dry-run and confirmed by Diego's real refresh.
- `group_stats`: not directly measured this session; grain is `(neighborhood_cd × state_cd1_class × classi_cd × tax_year)` — with roughly a few hundred real neighborhood codes, 5 `state_cd1_class` values, on the order of 100 `classi_cd` values, and 6 tax years (2021-2026), a **rough estimate** is tens of thousands of rows, but this is an estimate, not a measurement, and should not be treated as one.
- `parcel_tax_year`, `tax_billing_entity`, `tax_delinquent`, `county_tax_rate`, `parcel_metrics`, `prop_unit`, `prop_unit_tax_year`, `ingest_audit`, `load_batch`, `parcel_2026_preliminary_snapshot`, `tax_billing_quarantine`: **not measured this session.** No real number is stated for any of these rather than guessing.

Disk sizes (`pg_total_relation_size()`) for any table: **not measured** — this genuinely requires a live connection; there is no code-reading substitute for it.

**Diego, run this once and the inventory above can be filled in completely** (safe, read-only):

```sql
SELECT relname AS table_name,
       n_live_tup AS approx_row_count,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       pg_size_pretty(pg_relation_size(relid)) AS table_size,
       pg_size_pretty(pg_total_relation_size(relid) - pg_relation_size(relid)) AS index_size
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
```

---

## 2. `county_code` convention — real prior art found, not invented

Investigated both places the brief pointed at before proposing anything:

**Already exists, live, in `schema.sql`:**
```sql
CREATE TABLE IF NOT EXISTS county_benchmark (
    county_code   VARCHAR(20)  NOT NULL DEFAULT 'TRAVIS',
    tax_year      SMALLINT     NOT NULL,
    ...
    PRIMARY KEY (county_code, tax_year, property_type_label)
);
```
This table was forward-scaffolded for multi-county use — but `loaders/compute_metrics.py:713` hardcodes the literal string `'TRAVIS'` in its `INSERT` (not parameterized), so despite the schema being ready, the actual write path is not multi-county-aware today. Real, honest finding: **the column existing is not the same as the loader being ready.**

**FIPS codes** (`48453`, `48113`, `48201`, `36061`, `06037`, `17031`) also exist in this codebase — `templates/index.html`'s `MARKETS` array and `templates/search.html`'s `ROADMAP` object — but investigated their actual purpose before assuming they're the right DB key: both are D3/TopoJSON county-boundary map renderers, and FIPS is what the US Census county topology data is keyed by (`d.id` in the GeoJSON feature). That's an external-system compatibility requirement for map rendering, not a data-model design choice. `index.html`'s own `MARKETS` array additionally carries a `slug` field (`"travis-tx"`, `"dallas-tx"`, `"harris-tx"`) for its own HTML `data-market` attributes — a third convention, used for CSS/JS hooks only.

**Recommendation:** `county_code VARCHAR(20) NOT NULL DEFAULT 'TRAVIS'`, exactly matching `county_benchmark`'s existing shape and default — `'TRAVIS'` / `'DALLAS'` / `'HARRIS'`, uppercase county name, no FIPS, no new lowercase scheme. This is the one convention that's both already live in production and semantically named (a human reading a row instantly knows what county it is, unlike a bare FIPS integer). FIPS codes stay exactly where they are, doing exactly what they're for (map rendering) — no change needed there, and no reason to make the DB schema's key match the map layer's key.

---

## 3. `geo_id` / `prop_id` cross-county uniqueness — investigated, not assumed

Traced `geo_id`'s real origin: `loaders/load_pir_tcad.py:33` — `F_GEO_ID = 6  # 10-char TCAD long account (2022+); prop_id in 2021`. `geo_id` is **Travis Central Appraisal District's own internal account-numbering scheme** (a 10-character string, e.g. `0100030105`), sourced directly from TCAD's `PROP.TXT` export. `prop_id` (`prop_unit.prop_id BIGINT PRIMARY KEY`) is TCAD's own internal short integer ID for the same records.

There is no evidence anywhere in this codebase, in `DATA_LIFECYCLE.md`, or in `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` that Texas CADs coordinate account-numbering schemes with each other — each appraisal district (TCAD, DCAD for Dallas, HCAD for Harris) assigns account numbers independently within its own jurisdiction; there is no statewide registry that would prevent two different counties from independently assigning the same 10-character string to two different, unrelated parcels. Given that, and given hundreds of thousands of accounts per county, **the risk of a real value collision between Travis's and Dallas's `geo_id` values is real and plausible, not hypothetical** — exactly what `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §7 already flagged. This design does not assume uniqueness holds; it assumes the opposite, and designs the key accordingly (§4.3).

Found a related-but-distinct piece of prior art while checking: `investigate_geo_id_prop_id_collision.py` (repo root) documents that even *within* Travis alone, `geo_id` is not always 1:1 with `prop_id` (a single geo_id can carry multiple prop_ids — a condo/multi-unit account; 3,625 colliding geo_ids measured in the 2026 Preliminary export, 23,708 orphaned prop_ids). That is a real, already-identified, already-being-addressed issue (the unit-layer model in `SPEC_UNIT_MODEL_AND_INGEST_GATE.md`) — a different collision mechanism than the cross-county question this brief asks, and out of scope here. Named so it isn't confused with the cross-county risk this document is about.

**Conclusion:** `county_code` must be part of the primary key (or a real uniqueness-scoping mechanism) on every table keyed by `geo_id` or `prop_id` — not merely an informational filter column. Composite key `(county_code, geo_id, tax_year)` etc., not `(geo_id, tax_year)` with `county_code` bolted on separately.

---

## 4. Design recommendation

### 4.1 Native partitioning vs. lightweight column+index — real reasoning, not default-to-"correct"

**Recommendation: lightweight — `county_code` as a real column, part of the primary key, with `county_code`-leading composite indexes. NOT native `PARTITION BY LIST (county_code)`.**

Reasoning, against real numbers:

- Travis today: 517,614 `parcel` rows (confirmed). At the brief's own cited 3-5x scale for Dallas+Harris combined, full three-county `parcel` sits somewhere around **1.5-2.5 million rows** — the largest single table in this schema is likely `parcel_tax_year` (`parcel` rows × ~6 tax years), putting it in the **low tens of millions at the very outside**, and most of the other tables well under that.
- Postgres native declarative partitioning earns its real operational cost (see below) at table sizes where (a) a single partition's data no longer fits comfortably in shared_buffers/OS cache, so partition pruning meaningfully reduces I/O, not just planning overhead, or (b) there's a real, recurring operational need to bulk-drop or bulk-detach an entire partition's data (e.g., archiving old years, or genuinely removing a county's data set). Low millions of rows per table, with all three counties' current query patterns wanting to read across recent years continuously (not drop whole years), is not that regime. A well-chosen B-tree index with `county_code` as the leading column gives Postgres the same effective query-plan benefit — an index range scan restricted to one county's rows — via ordinary index-range-scan planning, at none of partitioning's structural cost.
- Real costs native partitioning would add, concretely, on THIS schema: every unique constraint (including every existing PK) on a partitioned table must include the partition key — that's already true of the lightweight design too, so no difference there — but additionally, foreign keys referencing a partitioned table have real, version-dependent restrictions in Postgres (the live `parcel_metrics.geo_id REFERENCES parcel(geo_id)` FK would need re-examination under partitioning that it does not need under the lightweight design, where `parcel`'s shape changes but it stays a single physical table). Every `INSERT` needs to route to the correct partition (either Postgres does this automatically via the partition key being present in the row, which does work, but planning/constraint-exclusion behavior on `ON CONFLICT ... DO UPDATE` upserts — which every loader in this codebase uses — has more edge cases and version-sensitivity under partitioning than a plain table). None of this is impossible, all of it is manageable, but it is real, additional operational surface for a benefit that doesn't clearly exist yet at this data volume.
- **Concrete future trigger, not "someday":** revisit native partitioning if/when (a) any single table's row count is confirmed (via the query in §1.2) to exceed roughly 20-30 million rows, where partition pruning's I/O benefit becomes measurable rather than theoretical, OR (b) a real operational need emerges to bulk-remove one county's entire data set at once (e.g., a county contract ending) — `DETACH PARTITION` is genuinely valuable for that in a way a `DELETE WHERE county_code = ...` on a lightweight design is not (a multi-hour, WAL-heavy bulk delete vs. a near-instant detach). Neither condition holds today.

### 4.2 What `county_code` should be

Per §2: `VARCHAR(20) NOT NULL`, values `'TRAVIS'` / `'DALLAS'` / `'HARRIS'` (and so on for future roadmap counties), matching `county_benchmark`'s already-live convention exactly. No default value on the NEW columns being added to existing tables during migration is safe to leave silently applied forever — see §5's migration plan for why the backfill step explicitly sets `'TRAVIS'` on every existing row rather than relying on a column default to paper over it (a `DEFAULT 'TRAVIS'` is fine for schema-level convenience on new inserts, matching `county_benchmark`'s own pattern, but the existing-data backfill in a real migration should be an explicit, verified `UPDATE`, not something quietly implied by a default).

### 4.3 Primary key changes — county_code joins the key, everywhere geo_id/prop_id appears

Per §3's real finding (cross-county collision risk, not assumed away), every table currently keyed by `geo_id` or `prop_id` needs `county_code` as a **leading** column of its primary key (leading, not trailing, so that `WHERE county_code = 'TRAVIS' AND geo_id = ...` — the shape essentially every real query will take — gets a genuine index range scan, not a full-index scan filtered afterward):

| Table | Current PK | Recommended PK |
|---|---|---|
| `parcel` | `geo_id` | `(county_code, geo_id)` |
| `parcel_tax_year` | `(geo_id, tax_year)` | `(county_code, geo_id, tax_year)` |
| `tax_billing` | `(geo_id, tax_year)` | `(county_code, geo_id, tax_year)` |
| `tax_billing_entity` | `(geo_id, tax_year, entity_code)` | `(county_code, geo_id, tax_year, entity_code)` |
| `tax_delinquent` | `geo_id` | `(county_code, geo_id)` |
| `parcel_metrics` | `(geo_id, tax_year)` | `(county_code, geo_id, tax_year)` — and its FK to `parcel` becomes a composite FK `(county_code, geo_id) REFERENCES parcel(county_code, geo_id)` |
| `prop_unit` | `prop_id` | `(county_code, prop_id)` |
| `prop_unit_tax_year` | `(prop_id, tax_year)` | `(county_code, prop_id, tax_year)` |
| `tax_billing_quarantine` | `(geo_id, tax_year)` | `(county_code, geo_id, tax_year)` |
| `group_stats` | `(neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year)` | `(county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year)` — `neighborhood_cd`/`classi_cd` are also county-internal codes, same reasoning as geo_id |
| `snapshot_breakdown` | `(view, ptype)` | `(county_code, view, ptype)` |
| `snapshot_totals` | `view` | `(county_code, view)` |
| `snapshot_neighborhood_movers` | `(view, neighborhood_cd)` | `(county_code, view, neighborhood_cd)` |

`county_tax_rate` is keyed by `(entity_code, tax_year)` — taxing-entity codes (school districts, cities, etc.) are Texas Comptroller-assigned, a different, statewide-coordinated namespace than CAD account numbers; **not assumed to need the same treatment without checking** — worth a real, separate confirmation of whether entity codes can collide across counties (a school district that spans two counties might legitimately need the SAME entity_code in both, which would make adding `county_code` to this table's key actively wrong). Flagged as an open question, not decided here.

`ingest_audit` and `load_batch`: add `county_code` as a plain (non-PK) column — these are append-only audit tables with surrogate keys already; no collision risk, just a real usability/clarity gap today.

### 4.4 Indexes

Every composite index currently leading with `geo_id`/`tax_year` (e.g. `idx_billing_geo`, `idx_put_geoid_year`, `idx_pty_year_market_value`) should get `county_code` prepended as the new leading column, for the same reason as the PKs — Postgres can only use a leading-column-first B-tree efficiently. Indexes that don't touch `geo_id`/`prop_id` at all (`idx_parcel_owner`, `idx_metrics_risk_jump` and the other partial indexes on boolean risk flags, `idx_rate_year`) don't need to change — county-scoping a search-by-owner-name index, for instance, isn't wrong, but there's no evidence it's needed until real cross-county query patterns exist to measure against.

---

## 5. Migration plan — design only, not executed, not scripted yet

Postgres cannot `ALTER TABLE ... PARTITION BY` a populated table, and this design doesn't recommend partitioning anyway — but changing a PK from `(geo_id, tax_year)` to `(county_code, geo_id, tax_year)` on a live, populated table is itself a real, higher-stakes operation (dropping and recreating a PRIMARY KEY on a table with real FKs pointing at it, e.g. `parcel_metrics → parcel`, requires care about lock duration and constraint ordering). This is a bigger, higher-stakes version of the shadow-table pattern already proven twice this session (`group_stats`, `snapshot_*`) — same shape, real data instead of empty new tables, so real verification matters more, not less.

**Per-table migration shape** (illustrated for `parcel`; the same shape repeats for each table in §4.3's list, in dependency order — tables with FKs pointing at them migrate together with their referents, or the FK is temporarily dropped and re-added):

1. Create `parcel_new` with the target shape: same columns, `county_code VARCHAR(20) NOT NULL DEFAULT 'TRAVIS'` added, `PRIMARY KEY (county_code, geo_id)`.
2. Backfill: `INSERT INTO parcel_new SELECT 'TRAVIS', <every existing column> FROM parcel;` — a single, explicit, auditable statement, not a default silently doing the work. For a 517,614-row table this is a bulk INSERT, not a per-row operation — should complete in seconds to low minutes, not hours, based on this session's own measured throughput for comparably-sized real operations tonight (though this needs a real `EXPLAIN ANALYZE`/timed dry run against production before being treated as a plan rather than an estimate — flagged, not assumed).
3. Reconciliation, BEFORE any swap or drop — matching this week's own established standard (the cross-table consistency assertion built earlier tonight for `snapshot_breakdown`/`snapshot_totals`, generalized to old-table-vs-new-table instead of old-table-vs-old-table):
   - `SELECT COUNT(*) FROM parcel` vs. `SELECT COUNT(*) FROM parcel_new` — must match exactly.
   - For every numeric/dollar column in the table (e.g. on `parcel_tax_year`: `SUM(market_value)`, `SUM(assessed_value)`, `SUM(taxable_value)`), compute the sum on both old and new and assert exact equality — the same "real reconciliation... proving row counts and dollar totals match exactly" standard this session has used for every prior real-data migration (AGGPRECOMP-1's `group_stats`, the Raw Vault backfill's checksum-and-compare).
   - A real spot-check sample (e.g. 20 random `geo_id`s) compared row-by-row, all columns, old vs. new — cheap, and catches a column-mapping mistake a pure count/sum reconciliation could miss.
4. Only after reconciliation passes: swap. `ALTER TABLE parcel RENAME TO parcel_old_pre_partition; ALTER TABLE parcel_new RENAME TO parcel;` inside one transaction — the exact same atomic-rename pattern `swap_shadow_in()` already uses for `group_stats`/`snapshot_*`, generalized. FK-dependent tables (`parcel_metrics`) need their FK constraint dropped before the rename and re-added after, pointing at the new composite key.
5. Keep `parcel_old_pre_partition` in place for a real, deliberate retention window (this session's own established pattern: nothing gets dropped same-day after a real-data migration) — Diego confirms the new table is serving correctly in production before the old one is ever actually `DROP`ped.
6. Repeat per table, respecting FK/dependency order (`parcel` before `parcel_metrics`; `prop_unit` before `prop_unit_tax_year`).

**Zero data loss / minimal downtime, real shape:** steps 1-3 (build + backfill + reconcile) touch only the new table — the live `parcel` table is untouched and fully readable/writable by the running app the entire time, exactly like `build_shadow()`'s ~472s window tonight held zero locks on live tables. Step 4 (the actual swap) is the only moment real downtime risk exists, and it should be as short as `swap_shadow_in()`'s own measured 0.632s this session — a few `ALTER TABLE ... RENAME` statements, not a data-copying operation. The real unresolved question for the migration script (a follow-up brief, not this one) is exact **application quiesce** — whether the live Flask app needs a brief maintenance window during step 4 to avoid an in-flight request reading a table mid-rename, or whether Postgres's DDL locking makes that safe without one; this needs a real test against a non-production copy before being asserted either way.

**Rollback plan:** if reconciliation ever fails at step 3, the migration simply doesn't proceed to step 4 — the live table was never touched, so "rollback" is really "don't swap," the same safety property the shadow-swap pattern already provides. If a problem is discovered AFTER the swap, `parcel_old_pre_partition` (retained per step 5) is the real rollback path — rename back.

**Explicitly not built here, per the brief:** the actual migration script. This section describes the real, reviewed shape; the script itself is a follow-up brief once this design is approved.

---

## 6. Code impact — real inventory, not just schema

### 6.1 `app.py` — scope, measured

218 occurrences of `geo_id` across `app.py`, 28 route decorators (`@app.route`), all of them today implicitly single-county — none filter or reference any county concept because there has only ever been one. Representative, real examples of the pattern that recurs across essentially every route:

```python
parcel = query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)
```

Once `county_code` joins `parcel`'s primary key, this becomes ambiguous the moment two counties' `geo_id`s could collide (§3) — every one of these needs either an additional `county_code` parameter (requiring the route to know which county it's asking about — a routing/UI question, see §7) or the app needs some other mechanism to disambiguate before this query runs. This is the real blast radius: not a small, contained set of call sites, but a pattern repeated across the majority of `app.py`'s real query surface (`/parcel/<geo_id>`, `/snapshot`, `/snapshot/neighborhood/<code>`, `/api/peer_benchmark_local/<geo_id>`, `/api/peer_set/<geo_id>`, `/api/billing/<geo_id>`, `/api/estimate_acq/<geo_id>`, `/parcels`, `/compare`, and more — every route that takes a `geo_id` in its URL or does a parcel-level lookup).

`parcel_filters.py` (`CANONICAL_PARCEL_EXCL`, `exclude_non_real_property_gap_sql()`, `peer_state_cd1_match_sql()`) are pure SQL-fragment builders that operate on `state_cd1`/`classi_cd` — county-agnostic logic, no change needed to the fragments themselves. Every one of their 12 real consumer call sites (per tonight's `verify_parcel_filters_coverage.py` check) would need `county_code` added to the surrounding `WHERE` clause alongside the existing exclusion fragment, but the fragment logic itself is unaffected.

### 6.2 Loaders — `config.py`'s single-county source-path assumption

`config.py`'s source-directory constants are flat, single-county module-level variables:
```python
CERT_DIR      = os.path.join(DATA_DIR, "2025 Certified Appraisal Export Supp 0_07202025")
CERT_DIR_2022 = os.path.join(DATA_DIR, "2022_Certified_Export")
CERT_DIR_2023 = os.path.join(DATA_DIR, "2023_Certified_Export")
CERT_DIR_2024 = os.path.join(DATA_DIR, "2024_Certified_Export")
CERT_DIR_2026 = os.path.join(DATA_DIR, "2026_Certified_Export")
PRELIM_2026_DIR = os.path.join(DATA_DIR, "2026 Preliminary Appraisal Export Supp 0_06092026 (1)")
AJR_FILES = { ... }
```
9 loader files import these directly (`config.CERT_DIR`, `config.AJR_FILES`, `config.PRELIM_2026_DIR`). Adding Dallas means either a parallel set of per-county constants or restructuring into a per-county dict (e.g. `CERT_DIR_BY_COUNTY = {"TRAVIS": {...}, "DALLAS": {...}}`) — a real, non-trivial refactor across every loader entry point (`load_certified_historical.py`, `load_2026_preliminary.py`, `load_ajr.py`, `load_pir_tcad.py`, `vault_backfill.py`'s own `gather_sources()`, and `run_all.py`'s call chain), not just a config-file edit. Every loader also needs to STAMP `county_code` on every row it writes (the write-time equivalent of `compute_metrics.py`'s currently-hardcoded `'TRAVIS'` literal, generalized and made real everywhere instead of just scaffolded in one table).

`search_logic.py` / `resolve_exact_parcel()` / the typeahead search path (`static/parcel-typeahead.js`, `/api/address_search`) currently assume a single global `geo_id` namespace — searching for a geo_id or address with no county qualifier is a real, direct consequence of single-county scope, and becomes ambiguous the moment two counties' data coexist. This is the loudest example of an implicit single-county assumption with no current filter at all (not "the wrong filter" — no filter, because none was ever needed).

`ingest_gate.py`'s G1-G6 checks and `parcel_rollup.py`'s `ROLLUP_SQL` operate per-source-file today (one certified/preliminary/AJR file = one gate run); they'd need `county_code` threaded through as a parameter alongside the existing `tax_year`/`source_tag` parameters, but the check LOGIC (conservation math, reconciliation) is itself county-agnostic — same shape of change as `parcel_filters.py`, mechanical propagation, not a logic redesign.

### 6.3 Not touched by this design

`tax_logic/classify.py`, `tax_logic/texas.py` (tax-code-specific business logic — Texas Tax Code applies statewide, not per-county, no change needed). `snapshot_taxonomy.py`'s classification CASE expressions are built from Travis's own `classi_cd`/`USE_CODE_LOOKUP` values — genuinely might need per-county taxonomy tables once real Dallas data exists and its use-code conventions are known, but this is **explicitly not assumed to transfer as-is** — flagged per the brief's own instruction not to assume Dallas's data looks structurally like Travis's. This needs its own investigation once real Dallas source files exist; guessing at DCAD's use-code scheme now would be exactly the kind of assumption this project's standing rules warn against.

---

## 7. Explicit boundary — application-level multi-county routing/UI is NOT decided here

No URL or route in this application currently has any concept of "which county" — `/parcel/<geo_id>`, `/snapshot?view=X`, `/search`, all of it implicitly assumes Travis. Whether the URL gains a county segment (`/travis/parcel/<geo_id>`), whether the app infers county from the `geo_id`'s own looked-up `county_code` once it's resolvable, or whether there's a county switcher in the UI, is a real product design decision. This document does not propose an answer, does not lean toward one, and does not bake an assumption about it into the schema design above (the schema design in §4 works under any of the three application-level approaches — it establishes that `county_code` exists and is real, not how a request decides which value to use). **This is the next real conversation to have with Diego, separately from this document.**

---

## 8. Summary of what's real vs. what needs Diego/Fable review before proceeding

**Real, confirmed via code-reading this session, not assumed:**
- `county_code` convention already exists (`county_benchmark`), unused elsewhere.
- `geo_id`/`prop_id` are county-internal identifiers with a real, unverified-but-plausible cross-county collision risk.
- Full real table/PK/index inventory (§1.1).
- `config.py`'s single-county source-path structure and its real loader blast radius (§6.2).
- The shadow-swap migration pattern this design generalizes has been run twice already this session, successfully, on new (not populated) tables.

**Real gaps, honestly flagged, not filled with guesses:**
- Row counts and disk sizes for 11 of 20 real tables (§1.2) — needs Diego to run the provided query live.
- Whether `county_tax_rate.entity_code` can legitimately collide across counties (§4.3) — a real open question, not decided here.
- Whether application quiesce is needed during the swap step (§5) — needs a real test against a non-production copy.
- Whether Dallas's real source data structurally resembles Travis's at all (`snapshot_taxonomy.py`'s classification scheme, `PROP.TXT` field layout, etc.) — cannot be answered without real Dallas source files.

**Decisions this document makes, for review, not yet acted on:**
- Lightweight column+PK design over native partitioning (§4.1), with a concrete numeric trigger for revisiting.
- `county_code VARCHAR(20)`, uppercase county name, matching `county_benchmark` (§4.2).
- `county_code` joins the primary key on every `geo_id`/`prop_id`-keyed table (§4.3).
- Migration shape: generalized shadow-table pattern, reconciliation-gated swap (§5).

Nothing above has been executed. No `ALTER TABLE`, no `CREATE TABLE ... PARTITION BY`, no write of any kind against `parcel`, `parcel_tax_year`, `tax_billing`, `tax_billing_entity`, `prop_unit`, `prop_unit_tax_year`, or any of the Tier 1/3 precomputed tables. This document is the artifact for review.
