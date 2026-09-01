# RUNBOOK — Dallas Metrics First Run — REV 1 (supersedes REV 0, absorbed) (PX-20260831-02)

**Status: write-only deliverable. Nothing in this document has been executed. All facts below come from reading the shipping source on 2026-08-31 (post PX-20260831-02 Tasks 1–5); no live queries were run, except where a section explicitly says otherwise (R3's threshold measurement, which Diego already ran live and whose output is pre-filled below).**

**This document is now the sole, self-contained runbook for this run.** It absorbs every section the original `RUNBOOK_DALLAS_METRICS_FIRST_RUN.md` (PX-20260831-01, "REV 0") defined — nothing is deferred to that file anymore, and REV 0 has been deleted. Every section below either carries REV 0's original content forward verbatim (R1.2–R1.6, R2.1/R2.3/R2.4/R2-LOG, R5, R7, R4's homestead-signal table) or supersedes it with a rewritten version (R1.1, R2.2, R3, R4's row-floor/TYPE_GROUPS subsections, R6, R8's KNOWN_LIMITATIONS wording) — the "REV 1" callouts throughout mark exactly which sections changed and why, for anyone comparing this against REV 0's git history.

Scope: unchanged — the first-ever run of `compute_metrics.py --county DALLAS`, `refresh_group_stats.py`, and `refresh_snapshot_summary.py` against production.

Every step below is `[Diego/live]` (Diego runs it against production) or `[PM checkpoint]` (stop and get PM's ruling before continuing).

**Headline change from REV 0:** REV 0 flagged R6 (`refresh_snapshot_summary.py`'s missing county scoping) as a blocking bug that made the whole back half of this runbook unsafe to run as written. **That bug is now fixed** (PX-20260831-02 Task 1) — R6 is rewritten below as a normal, runnable step with its own live proof command, not a `[PM checkpoint — do not run as-is]`. R3 and R4 are also rewritten: the per-county large-jump threshold and the per-county row floor PM flagged as open judgment calls in REV 0 are now both implemented in code, closing two of REV 0's six `[PM ruling needed]` items without further judgment calls needed from Diego at run time.

---

## R1 — Preconditions

### R1.1 — Deploy hash check `[Diego/live]` — placeholder, not a fixed hash

REV 0 named a specific commit (`5bfe005`) to confirm against. That commit predates this brief's own fixes (Tasks 1–5: per-row county derivation in `refresh_snapshot_summary.py`, the shadow-swap lint, the per-county row floor and large-jump threshold, and the join-scoping fixes in `compute_metrics.py`) — running this runbook against `5bfe005` itself would mean running against the OLD, R6-blocked code, defeating the point of this re-issue.

```bash
/usr/bin/python3 -c "import subprocess; print(subprocess.check_output(['git','rev-parse','HEAD']).decode().strip())"
```

**Expect: `9cd17f6` (PX-20260831-02, committed 2026-08-31 — ships Tasks 1–5). A later descendant is acceptable only if the three greps below all still pass.** Before running R1.1's check for real, confirm the deployed HEAD includes:
- `refresh_snapshot_summary.py`'s per-row `county_code` derivation (grep for `_ALL_COUNTIES_BATCH_SENTINEL` — its presence confirms Task 1 shipped)
- `compute_metrics.py`'s `LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY` dict and `_parcel_metrics_row_floor()` function (confirms Tasks 3–4 shipped)
- `compute_metrics.py`'s `p.county_code = pty.county_code` equality on the main INSERT's `parcel p` join (confirms Task 5 shipped)

If any of the three greps above come back empty on the deployed commit, **STOP** — this runbook's R3/R4/R6 sections assume all three are live; running against a commit missing any of them means the corresponding original REV 0 behavior (unfixed) applies instead of what's written below.

### R1.2 — `DATABASE_URL` exported `[Diego/live]`

```bash
echo "DATABASE_URL is set: $([ -n "$DATABASE_URL" ] && echo yes || echo NO -- STOP)"
```

`loaders/db.py`'s `get_conn()` prints a banner naming `config.DB_SOURCE`; if `DATABASE_URL` is unset it falls back to `"local-fallback-defaults"` and prints an unmissable warning. If you see that warning, STOP — you are not talking to production.

### R1.3 — `inet_server_addr()` = production proof `[Diego/live]`

`loaders/db.py` already has the exact primitive for this (`EXPECTED_PRODUCTION_HOST = "10.30.105.217"`, `assert_production_db()`). Run:

```bash
/usr/bin/python3 -c "
import sys; sys.path.insert(0, 'loaders')
import db
conn = db.get_conn()
print('inet_server_addr():', db.assert_production_db(conn))
conn.close()
"
```

Expect: prints `inet_server_addr(): 10.30.105.217` and exits 0. If it raises `WrongDatabaseError`, STOP — no write has happened yet, per that function's own contract.

### R1.4 — `/usr/bin/python3` throughout

Every command in this runbook uses `/usr/bin/python3` explicitly, not a bare `python3` or `python`, per standing project convention (avoids a shadowed venv/pyenv interpreter silently pointing at the wrong site-packages or, worse, a stale local checkout).

### R1.5 — Fresh `pg_dump` of the six affected tables `[Diego/live]`

Schema + data, these six tables only (`parcel_metrics`, `group_stats`, `snapshot_breakdown`, `snapshot_totals`, `snapshot_neighborhood_movers`, `county_benchmark`) — **not** their `_shadow` siblings (those get dropped/recreated by the scripts themselves and hold no data worth preserving pre-run), and not `load_batch` (append-only, never rolled back).

```bash
/usr/bin/python3 -c "
import sys; sys.path.insert(0, 'loaders')
import db, config
print(f'{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}')
"
```

Use that connection info in:

```bash
pg_dump \
  --host=<DB_HOST from above> --port=<DB_PORT> --username=<DB_USER> --dbname=<DB_NAME> \
  --table=parcel_metrics --table=group_stats \
  --table=snapshot_breakdown --table=snapshot_totals --table=snapshot_neighborhood_movers \
  --table=county_benchmark \
  --format=custom --file=pre_dallas_metrics_run_$(date +%Y%m%d_%H%M%S).dump
```

Flags explained: `--format=custom` (compressed, and the only format `pg_restore --table=` selective-restore below can target); `--table=X` repeated once per table, schema+data both included by default (no `--schema-only` / `--data-only` flag — this is deliberate, since a restore needs both if a table's shape ever needs to be recreated from scratch). No `--no-owner`/`--no-acl` — keep the dump byte-faithful to what's live.

**Disk/time estimate:** `group_stats` is documented at ~59,469 Travis rows today (post-tbe_sum-fix, all counties in one pass — Dallas adds an unknown but bounded number of additional rows, since it's one row per `(county_code, neighborhood_cd, state_cd1_class, classi_cd, tax_year)` combination, not per-parcel). `parcel_metrics` is documented at ~2,796,316 rows (comment in `compute_metrics.py`, "508K parcels x ~5.5 years") — this dump's data volume is dominated by this one table. `snapshot_breakdown`/`snapshot_totals`/`snapshot_neighborhood_movers` are all small (one row per `view`/`(view,ptype)`/`(view,neighborhood)` — low thousands of rows total across all three). `county_benchmark` is ~25 rows per county. **I could not determine an exact byte size or wall-clock estimate from code reading alone** — `pg_dump --table=parcel_metrics` on ~2.8M rows is very unlikely to take more than a minute or exceed a few hundred MB, but this is an estimate, not a measurement. **[Diego/live]: run `\dt+ parcel_metrics` in `psql` (or check Render's dashboard) for the real on-disk size before running the dump if you want a firmer number first.**

### R1.6 — Deploy hash re-confirmation

Re-run R1.1 immediately before R4 (the first real write) if any time has passed since R1.1 — a deploy landing between precondition-check and write-step is the exact class of footgun `assert_production_db()` exists to catch on the DB side; this is the equivalent check on the code side.

---

## R2 — Before-snapshots (read-only SQL)

All queries in this section are read-only. Run and **record the output values directly in this runbook's own Section R2-LOG below** before proceeding to R3.

### R2.1 — Row counts, total and by `county_code`, per table `[Diego/live]`

```sql
SELECT 'parcel_metrics' AS tbl, county_code, COUNT(*) FROM parcel_metrics GROUP BY county_code
UNION ALL
SELECT 'group_stats', county_code, COUNT(*) FROM group_stats GROUP BY county_code
UNION ALL
SELECT 'snapshot_breakdown', county_code, COUNT(*) FROM snapshot_breakdown GROUP BY county_code
UNION ALL
SELECT 'snapshot_totals', county_code, COUNT(*) FROM snapshot_totals GROUP BY county_code
UNION ALL
SELECT 'snapshot_neighborhood_movers', county_code, COUNT(*) FROM snapshot_neighborhood_movers GROUP BY county_code
UNION ALL
SELECT 'county_benchmark', county_code, COUNT(*) FROM county_benchmark GROUP BY county_code
ORDER BY 1, 2;
```

**REV 1 — closed:** `parcel_metrics.county_code` is confirmed live against production (`\d parcel_metrics`, 2026-08-23 — per `compute_metrics.py`'s own comment: "Live PK for parcel_metrics is `(county_code, geo_id, tax_year)`... confirmed via `\d` against production, 2026-08-23"). This repo's `schema.sql` not reflecting that column is a known, pre-existing documentation staleness gap, not a live-data open question anymore — removed from this runbook's open-questions list accordingly. **Kept as belt-and-braces, not as an open question:** if this query somehow errors with `column "county_code" does not exist"` against the actual target database, STOP and report back before continuing — at that point the concern is that you're not pointed at the production database this runbook was verified against, not that the column itself is in doubt.

### R2.2 — Travis-only checksums (must be byte-identical after R4–R6) — REV 1: R2.2-CAVEAT dropped

Deterministic design note: rather than hand-listing every column per table (error-prone, and this runbook's author does not have live access to verify an exhaustive column list against production), each checksum uses `to_jsonb(row)` minus the known write-time/provenance columns (`refreshed_at`, `computed_at`, and — for the tables whose whole-table rebuild legitimately changes provenance on every run — `source_import_batch_id`), aggregated in a fixed order by primary key. This is deterministic (same input rows always produce the same string) and complete (every real data column is included automatically, with no risk of a hand-typed list silently omitting one).

```sql
-- parcel_metrics: PK (county_code, geo_id, tax_year) per compute_metrics.py's own comment
SELECT md5(string_agg((to_jsonb(t) - 'computed_at')::text, '~' ORDER BY geo_id, tax_year))
FROM parcel_metrics t WHERE county_code = 'TRAVIS';

-- group_stats: PK (county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year)
-- NOTE: refresh_group_stats.py rebuilds ALL counties together in one pass (no --county
-- flag affects a real run) -- source_import_batch_id and refreshed_at WILL legitimately
-- change for Travis's rows too on every run. Exclude both from this checksum; the DATA
-- columns are what must be byte-identical, not the provenance stamps.
SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year))
FROM group_stats t WHERE county_code = 'TRAVIS';

-- county_benchmark: PK (county_code, tax_year, property_type_label)
-- compute_county_benchmarks() DELETEs and rebuilds scoped by county_code -- Travis rows
-- should be completely untouched by a Dallas run, so this checksum is a true
-- before/after identity check (no provenance-column exclusion needed beyond the generic set).
SELECT md5(string_agg((to_jsonb(t))::text, '~' ORDER BY tax_year, property_type_label))
FROM county_benchmark t WHERE county_code = 'TRAVIS';

-- snapshot_breakdown / snapshot_totals / snapshot_neighborhood_movers: see the REV 1 note
-- below -- these three are now fully trusted, not caveated.
SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view, ptype))
FROM snapshot_breakdown t WHERE county_code = 'TRAVIS';

SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view))
FROM snapshot_totals t WHERE county_code = 'TRAVIS';

SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view, neighborhood_cd))
FROM snapshot_neighborhood_movers t WHERE county_code = 'TRAVIS';
```

**REV 1 note:** REV 0's R2.2-CAVEAT warned that the three `snapshot_*` checksums were meaningless until R6's bug was fixed, because a Dallas run would have replaced Travis's rows entirely rather than leaving them untouched. **That caveat is dropped in this revision** — `refresh_snapshot_summary.py`'s aggregation is now genuinely per-county (Task 1), so these three checksums behave exactly like the `group_stats` checksum already does: a real before/after identity check on Travis's own rows, meaningful the same way R2.2's `parcel_metrics`/`group_stats`/`county_benchmark` checksums always were. No query text changed from REV 0 — only the trust level of the three `snapshot_*` ones, which is now full.

### R2.3 — `group_stats` Travis count, verify don't assume `[Diego/live]`

```sql
SELECT COUNT(*) FROM group_stats WHERE county_code = 'TRAVIS';
```

PM's brief states this is expected to be **59,469**. **If the live count differs, STOP** — do not proceed to R5 on the assumption that a mismatch is fine; a wrong Travis count going into a full-table shadow-swap run is exactly the scenario R5's post-run verification exists to catch, and catching it *before* the run (when it's just a number) is cheaper than catching it *after* (when it means restoring from R1.5's dump).

### R2.4 — `snapshot_breakdown` DALLAS rows, expected 0 before `[Diego/live]`

```sql
SELECT COUNT(*) FROM snapshot_breakdown WHERE county_code = 'DALLAS';
```

Expect: `0`. Dallas's Market Snapshot summary has never been refreshed — this is the literal reason the live `/dallas-tx/snapshot` page currently shows the "being prepared" state (see R7).

### R2-LOG — record your R2 results here before continuing

```
R2.1 row counts:
  parcel_metrics       TRAVIS=____  DALLAS=____
  group_stats          TRAVIS=____  DALLAS=____
  snapshot_breakdown   TRAVIS=____  DALLAS=____
  snapshot_totals      TRAVIS=____  DALLAS=____
  snapshot_neighborhood_movers  TRAVIS=____  DALLAS=____
  county_benchmark     TRAVIS=____  DALLAS=____

R2.2 checksums:
  parcel_metrics (TRAVIS):      ____
  group_stats (TRAVIS):         ____
  county_benchmark (TRAVIS):    ____
  snapshot_breakdown (TRAVIS):  ____
  snapshot_totals (TRAVIS):     ____
  snapshot_neighborhood_movers (TRAVIS): ____

R2.3 group_stats TRAVIS count: ____  (expected 59,469 — match? Y/N)
R2.4 snapshot_breakdown DALLAS count: ____  (expected 0)
```

---

## R3 — Threshold measurement `[Diego/live]`

REV 0 treated this as a `[PM checkpoint]` with two unresolved options (apply Travis's 75.0 constant to Dallas unmodified, or build a per-county mechanism first). **PM ruled Option B, and it has since been built** (PX-20260831-02 Task 4) — this section no longer has an open ruling on WHICH threshold to use. **REV 1 update: Diego already ran this measurement live on 2026-08-31 — the result is pre-filled in R3-LOG below, and re-running it is now optional, not required**, since no Dallas data has loaded since that measurement was taken (a re-run would be harmless, just redundant, unless PM/Diego specifically wants a second confirmation before proceeding).

```bash
/usr/bin/python3 loaders/compute_metrics.py --county DALLAS --analyze
```

This still prints percentiles and flag-counts only, writes nowhere, and does not feed back into anything automatically.

### What happens to the measured value today (read from the code)

`compute_metrics.py`'s `LARGE_JUMP_THRESHOLD_PCT = 75.0` single module constant is retired. In its place, `LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY = {"TRAVIS": 75.0, "DALLAS": 45.0}` and `_large_jump_threshold_for_county(county_code)` — Pass 2's `risk_large_value_jump` UPDATE now looks up each county's own registered value via that function, and **raises `MetricsIntegrityError` with no default and no fallback to another county's value** if a county has no registered entry. Dallas already has a registered value (45.0) as of this brief, derived from PM's own live measurement (recorded below).

**Dallas's median of exactly 0.00% is a real, disclosed data characteristic, not a defect in this measurement: DCAD carries values forward on non-reappraised parcels, so a large share of Dallas parcel-years show zero year-over-year change by construction, not because Dallas property values are genuinely static.** This is why Dallas's whole distribution sits well below Travis's at every percentile, and why 45.0 (not 75.0) is the right threshold for Dallas — applying Travis's number to a structurally different distribution would under-flag Dallas by roughly half relative to Travis's own ~4.5% flag rate.

### R3-LOG — Diego's live measurement (2026-08-31), pre-filled

```
Dallas analyze_threshold() output (live, 2026-08-31, 2,745,496 YoY pairs):
  p50=0.00%  p75=11.91%  p90=27.53%  p95=41.77%  p99=167.59%
  Min -100.0%  Max 878960.0%  Avg 33.12%
  >10%: 30.1%  >20%: 16.0%  >30%: 9.0%  >40%: 5.5%  >50%: 3.8%  >75%: 2.2%  >100%: 1.5%

Matches the registered 45.0 threshold's own derivation? [x] Yes — this measurement IS the
                                                                basis LARGE_JUMP_THRESHOLD_PCT_BY_COUNTY["DALLAS"]
                                                                = 45.0 was registered from.
Re-run required before proceeding to R4?   [ ] No — no Dallas data has loaded since this
                                                    measurement; registered 45.0 stands as-is.
                                            [ ] Yes, I want a second confirmation — if so, re-run
                                                the command above and compare against the numbers
                                                pasted here. If the second run differs meaningfully
                                                (a materially different p95 or flag-rate shape,
                                                not normal measurement noise), STOP and flag it to
                                                PM before R4 -- `_large_jump_threshold_for_county()`'s
                                                own docstring says this value should be "re-measured
                                                when a county's roll is refreshed," and real drift
                                                here is exactly that trigger.
```

**Open question 3 (from REV 0/REV 1's earlier open-questions lists) is closed by default** — it only reopens if Diego chooses to re-run `--analyze` and the second run shows meaningful drift from the numbers above.

---

## R4 — `compute_metrics.py` run `[Diego/live]`

### Exact command — unchanged

```bash
/usr/bin/python3 loaders/compute_metrics.py --county DALLAS
```

Runs `analyze_threshold()`, then `compute_parcel_metrics()`, then `compute_county_benchmarks()`, then `print_sample()` — same sequence as REV 0, same single try/except/rollback wrapper.

### Expected row-count range in `parcel_metrics` for DALLAS — arithmetic shown

PM's brief states Dallas has 769,536 parcels across years 2022–2026 (5 years; matches `load_dallas_certified.py`'s `--year` choices `[2022, 2023, 2024, 2025, 2026]`, one year per invocation).

`compute_parcel_metrics()`'s INSERT has **no** `market_value > 0` filter at the base-row level (confirmed by reading the query: `FROM parcel_tax_year pty JOIN parcel p ... WHERE pty.county_code = %s`, no additional row-level filter before the JOIN/WHERE) — every `parcel_tax_year` row for `county_code='DALLAS'` produces exactly one `parcel_metrics` row. This means the row count is an **exact equality, not an estimate**:

```
parcel_metrics(DALLAS) row count == COUNT(*) FROM parcel_tax_year WHERE county_code = 'DALLAS'
```

Ceiling estimate by arithmetic (not the real answer, just a sanity bound): 769,536 parcels × 5 years = **3,847,680 rows maximum**, if every parcel had a loaded row for every one of the 5 years with zero attrition. Floor estimate: **769,536 rows minimum**, if only one year has been loaded so far for Dallas. The real number depends entirely on how many of the 5 `--year` loader invocations have actually been run against production — **I could not determine this from code reading alone** — it depends on load history, not code.

**[Diego/live]: run this exact query before R4's real command, and use its result as the true expected `parcel_metrics` row count (not the ceiling/floor estimate above):**

```sql
SELECT COUNT(*) FROM parcel_tax_year WHERE county_code = 'DALLAS';
```

### The `PARCEL_METRICS_ROW_FLOOR` hard-floor problem — REV 1: replaced with a per-county derived floor

REV 0 flagged a near-certain first-run failure: a single hardcoded `PARCEL_METRICS_ROW_FLOOR = 1_000_000`, tuned for Travis's ~2.8M-row table, would very likely exceed Dallas's real first-run row count and raise `MetricsIntegrityError` before the run ever committed. **This constant is retired** (PX-20260831-02 Task 3). `compute_parcel_metrics()` now computes `row_floor = _parcel_metrics_row_floor(conn, county_code)` fresh, inside the same transaction, immediately before the sanity check — the floor is `0.5 × COUNT(*) FROM parcel_tax_year WHERE county_code = county_code`, i.e. half of Dallas's OWN current source row count, not a Travis-shaped absolute number.

Concretely: since `parcel_metrics`' INSERT produces exactly one row per `parcel_tax_year` row for this county (no row-level filter, per this section's own arithmetic above), a healthy run's real row count sits at ~100% of the source count — a 50% floor only fires on a genuine catastrophic failure (a join/WHERE bug cutting the result by half or more), not on Dallas simply being a smaller county than Travis. **This closes REV 0's `[PM ruling needed]` item on the row floor — there is no longer a first-run-blocking gate to rule on; the floor now scales with whatever Dallas's real row count turns out to be.**

No pre-check query is needed for this specific risk anymore. The pre-check query above (Dallas's `parcel_tax_year` count) is still worth running for its own sake — as a sanity number to compare the final `parcel_metrics` row count against — but it is no longer gating whether the run can proceed.

### REV 1: `TYPE_GROUPS` zero-rows pre-check for `compute_county_benchmarks()` — corrected to group by TYPE_GROUP using the live code's own mapping

REV 0 flagged, but could not resolve from code alone, whether Dallas's classification/SPTB distribution guarantees at least one parcel per `TYPE_GROUP` per loaded tax year — `compute_county_benchmarks()` raises `MetricsIntegrityError` if any of the 5 `TYPE_GROUPS` (`Residential`, `Multi-Family`, `Land/Vacant`, `Agricultural`, `Commercial`) produces 0 rows for a given year.

**PX-20260831-02 delta correction, read fresh against the live `compute_county_benchmarks()` source before writing this section:** `tax_logic.classify.label_case_sql()` — the function this pre-check imports — already emits exactly one of the same five `TYPE_GROUPS` labels directly (`Residential`/`Multi-Family`/`Land/Vacant`/`Agricultural`/`Commercial`), not a finer-grained sub-taxonomy; the fine-grained Retail/Office/Industrial/Hotel labels live in a *different* function (`snapshot_taxonomy.py`'s taxonomy machinery for the `/snapshot` page's "commercial" tab), which `compute_county_benchmarks()` never calls. So label granularity was never the actual gap in the prior version of this pre-check. **The real gap, found by comparing this pre-check's query against `compute_county_benchmarks()`'s real `INSERT...SELECT` line by line:** the prior pre-check queried `parcel_tax_year`/`parcel` with only a `county_code = 'DALLAS'` filter, while the real query additionally requires `({CANONICAL_PARCEL_EXCL_BARE})`, `pty.market_value > 0`, and `(pty.data_source IS NULL OR pty.data_source != 'preliminary')` — three WHERE conditions the old pre-check never applied. A row population that looked non-zero under the old, looser pre-check could still be zero once the real query's stricter filtering ran, producing a false PASS. Fixed below by importing every one of the real query's own building blocks (`TYPE_GROUPS`, `label_case_sql`, `CANONICAL_PARCEL_EXCL_BARE`) and reproducing its exact WHERE clause, not just its label expression:

```bash
/usr/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from loaders.db import get_conn
from tax_logic.classify import label_case_sql
from parcel_filters import CANONICAL_PARCEL_EXCL_BARE
from loaders.compute_metrics import TYPE_GROUPS
conn = get_conn()
label_expr = label_case_sql('p.classi_cd', 'p.state_cd1')
with conn.cursor() as cur:
    cur.execute(\"SELECT DISTINCT tax_year FROM parcel_tax_year WHERE county_code = 'DALLAS' ORDER BY 1\")
    dallas_years = [r[0] for r in cur.fetchall()]
    print(f'Dallas years loaded so far: {dallas_years}')
    print()
    print(f'{\"TYPE_GROUP\":<14} {\"tax_year\":>8}  count')
    for prefixes, label, prefix_key in TYPE_GROUPS:
        cur.execute(f'''
            SELECT pty.tax_year, COUNT(*)
            FROM parcel_tax_year pty
            JOIN parcel p ON p.geo_id = pty.geo_id AND p.county_code = pty.county_code
            WHERE ({label_expr}) = %s
              AND ({CANONICAL_PARCEL_EXCL_BARE})
              AND pty.market_value > 0
              AND (pty.data_source IS NULL OR pty.data_source != 'preliminary')
              AND pty.county_code = 'DALLAS'
            GROUP BY pty.tax_year
            ORDER BY pty.tax_year
        ''', (label,))
        rows = cur.fetchall()
        seen_years = {r[0] for r in rows}
        for year, n in rows:
            print(f'{label:<14} {year:>8}  {n}')
        for missing_year in sorted(set(dallas_years) - seen_years):
            print(f'{label:<14} {missing_year:>8}  0   <-- ZERO ROWS, this (year, TYPE_GROUP) combination is missing')
conn.close()
"
```

This reuses `TYPE_GROUPS`, `label_case_sql`, and `CANONICAL_PARCEL_EXCL_BARE` directly from the modules `compute_county_benchmarks()` itself imports them from — not retyped — so the printed matrix is exactly the 5×N-years population the real run will try to produce a row for. **Before running R4's real command, confirm every `(TYPE_GROUP, tax_year)` combination printed above shows a non-zero count, for every year in the printed `dallas_years` list.** If any combination is missing or zero:

- **If it's a genuine, permanent Dallas data gap** (e.g. Dallas truly has zero agricultural parcels in some year) — this is the same `[PM ruling needed]` REV 0 flagged: document it in `KNOWN_LIMITATIONS.md` per R8 rather than treating it as a bug, and PM should confirm that framing before this run proceeds past the gap.
- **If it looks like a loader problem** (a classification code Dallas uses that `label_case_sql()`/`TYPE_GROUPS` doesn't map, producing an unexpectedly-empty bucket rather than a genuinely-empty one) — STOP, this is a data-quality question to resolve before running `compute_county_benchmarks()` for real, not something to route around.

### Homestead-signal expectations given Dallas has no billing data

Dallas has zero rows in `tax_billing`, `tax_billing_entity`, and `tax_delinquent` today. Reading each conditioned column in `compute_parcel_metrics()`'s INSERT:

- `effective_tax_rate` — populated only when `pty.tax_year = 2025 AND` a positive `SUM(tax_billing_entity.amount_due)` subquery result exists for that `geo_id`/2025. No `tax_billing_entity` rows exist for Dallas → this subquery returns `NULL` for every Dallas row → the `CASE` condition is never satisfied → **NULL for every Dallas row, every year.**
- `effective_tax_rate_derived` — gated by the identical condition as `effective_tax_rate` → **NULL for every Dallas row.**
- `yoy_tax_amount_pct` — depends on `tb.total_tax` via the `LEFT JOIN tax_billing tb`. No `tax_billing` rows for Dallas → **NULL for every Dallas row.**
- `risk_delinquent` — `COALESCE(td.total_due > 0, FALSE)` via `LEFT JOIN tax_delinquent td` → wrapped in `COALESCE`, so a missing join produces **FALSE, not NULL, for every Dallas row** — this is a real, deliberate default, not an absence.
- `cap_step_up_exposure` — conditioned on `pty.exemption_codes LIKE '%HS%'` and a dollar-floor calculation that falls back to "a conservative 2% county-wide approximation" when no billing exists — **this one COULD still populate for Dallas rows that carry an HS exemption code**, since it has a documented no-billing fallback path. Not a blanket NULL.
- `cap_expiry_signal` — conditioned only on `exemption_codes` across the 2025/2026 `parcel_tax_year` rows, no billing dependency at all — **behaves normally for Dallas**, exactly as it does for Travis, as long as Dallas's `parcel_tax_year.exemption_codes` field is populated (a load-time question, not a compute_metrics.py question).
- `coverage_level` — `CASE WHEN tb.confidence_level = 'verified' THEN 'full' ELSE 'value_only' END` → **`'value_only'` for every Dallas row**, unconditionally (no verified billing possible without `tax_billing` rows).
- `has_tax_data` — `COALESCE(tb.confidence_level = 'verified', FALSE)` → **FALSE for every Dallas row.**
- `assessment_ratio`, `yoy_market_value_pct`, `yoy_assessed_value_pct`, `cumulative_value_growth_pct`, `risk_large_value_jump`, `risk_data_incomplete` — none of these depend on billing data at all (pure value/assessed-value arithmetic from `parcel_tax_year`) — **behave normally for Dallas**, same as Travis.

**REV 1 update to the latent-risk paragraph below:** REV 0 flagged that the `tax_billing`/`tax_delinquent` LEFT JOINs in the main INSERT were not scoped by `county_code`, joining on `geo_id` alone. **This is now fixed** (PX-20260831-02 Task 5): both joins carry `AND tb.county_code = pty.county_code` / `AND td.county_code = pty.county_code` equality. The cross-county `geo_id`-collision risk REV 0 flagged as a future concern (relevant once Dallas billing data is eventually loaded) is closed, not merely mitigated by today's zero-Dallas-billing-rows coincidence.

### STOP conditions for R4 — plus the new TYPE_GROUPS pre-check gate

- R1.1's deploy-hash re-check (immediately before this step) fails.
- R1.3's `assert_production_db()` check was not re-verified this session.
- `MetricsIntegrityError` is raised by either `compute_parcel_metrics()` or `compute_county_benchmarks()` — the transaction has already rolled back by the time you see this; **do not retry blindly**, read the printed reason and route it to the appropriate ruling above (the `TYPE_GROUPS`-has-zero-rows case, if the pre-check above didn't already catch it).
- The `TYPE_GROUPS` pre-check above finds a missing/zero `(TYPE_GROUP, tax_year)` combination — checkable BEFORE the real run now, per the rewritten pre-check above, rather than only discoverable after `compute_county_benchmarks()` raises mid-run.

The row-count-floor STOP condition from REV 0 is removed (superseded by the per-county floor fix above).

### Expected post-run output, informational only

`print_sample(conn, county_code=args.county)` runs last and uses Travis-specific hardcoded sanity `geo_id`s (`"0100030105"`, `"0100030109"`, `"0284460113"`) — these will simply return no rows for a Dallas-scoped run. **This is expected, not a bug** — noting it here so it isn't mistaken for a failure.

---

## R5 — `refresh_group_stats.py` run `[Diego/live]`

### Shadow-swap mechanics, stated plainly

Two phases, stated exactly as the code implements them:

**Phase 1 (`build_shadow()`):** `DROP TABLE IF EXISTS group_stats_shadow`, `CREATE TABLE group_stats_shadow (LIKE group_stats INCLUDING ALL)`, then one `INSERT INTO group_stats_shadow (...) SELECT ...` that computes every county's rows in a single aggregation pass (the `REFRESH_GROUP_STATS_SQL` query, `county_code` derived per-row from `parcel.county_code` inside its `effective` CTE — this is the tbe_sum fix commit `5bfe005` proves via EXPLAIN, per PM's brief). The live `group_stats` table is **not touched** during this phase — safe to run while live traffic reads it, however long it takes.

**Phase 2 (`swap_shadow_in()`):** one transaction, three DDL statements — `ALTER TABLE group_stats RENAME TO group_stats_old`, `ALTER TABLE group_stats_shadow RENAME TO group_stats`, `DROP TABLE group_stats_old`. Metadata-only, near-instant ACCESS EXCLUSIVE lock.

### Exact command

```bash
/usr/bin/python3 loaders/refresh_group_stats.py
```

Note: **this script has no `--county` flag that affects a real run** — `refresh_group_stats()`/`build_shadow()` always rebuild every county's rows together in one pass. There is no `--county DALLAS`-scoped invocation to run separately; this single command recomputes both counties' `group_stats` rows at once, which is exactly why the Travis-invariant check below matters.

### Expected DALLAS row count reasoning

`group_stats`'s grain is `(county_code, neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year)` — one row per distinct combination that actually occurs in Dallas's loaded `parcel`/`parcel_tax_year` rows, not one row per parcel. **I could not derive a specific expected Dallas row count from arithmetic alone** — it depends on how many distinct neighborhood codes, property-class codes, and loaded tax years actually occur in Dallas's data, none of which this session queried live. **[Diego/live]: this is a case where the right check is "a real, non-zero, non-suspiciously-tiny number of Dallas rows appeared" rather than a specific predicted count** — compare against the DALLAS row count from R2.1 (which should be 0 or near-0 pre-run) to confirm growth happened at all.

### The Travis invariant — exact verification SQL

Re-run R2.2's `group_stats` checksum query verbatim:

```sql
SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY neighborhood_cd_key, state_cd1_class, classi_cd_key, tax_year))
FROM group_stats t WHERE county_code = 'TRAVIS';
```

**Must equal R2-LOG's recorded `group_stats (TRAVIS)` value exactly.** Also re-run R2.3's Travis count query — it must still read **59,469** (or whatever R2.3 actually recorded, if it differed and PM already ruled that discrepancy acceptable before proceeding).

### STOP-and-restore procedure if not identical

If either check fails (checksum differs, or the Travis row count changed):

1. **STOP immediately — do not run R6.** A `group_stats` regression here means the one-pass aggregation (this run's real, first production execution) has a bug real enough to have altered Travis's own numbers, which is the exact risk this whole runbook exists to catch before it reaches `refresh_snapshot_summary.py`.
2. Restore `group_stats` from R1.5's dump:

```bash
/usr/bin/python3 -c "
import sys; sys.path.insert(0, 'loaders')
import db
conn = db.get_conn()
print('inet_server_addr() before restore:', db.assert_production_db(conn))
conn.close()
"
pg_restore --host=<DB_HOST> --port=<DB_PORT> --username=<DB_USER> --dbname=<DB_NAME> \
  --table=group_stats --clean --if-exists \
  pre_dallas_metrics_run_<timestamp>.dump
```

`--clean --if-exists` drops the current (bad) `group_stats` before restoring the dumped version, rather than erroring on "relation already exists." This restores `group_stats` alone — it does not touch `parcel_metrics`/`county_benchmark` from R4, which are unaffected by anything in R5.

3. Re-run R2.2/R2.3 against the restored table to confirm the checksum and count match the original R2-LOG values before reporting back to PM.

---

## R6 — `refresh_snapshot_summary.py` run `[Diego/live]` — no longer a blocking checkpoint

REV 0 marked this entire section `[PM checkpoint — do not run as-is]` because `build_shadow()` stamped one externally-passed `county_code` onto every row of a full-table rebuild whose underlying aggregation blended every county's parcels together. **That bug is fixed** (PX-20260831-02 Task 1): all five query builders now derive `county_code` per-row from `parcel.county_code` (mirroring `refresh_group_stats.py`'s own established pattern), `build_shadow()`/`refresh_snapshot_summary()` no longer accept an external `county_code` parameter at all, and a real run computes every county's rows in one pass, exactly like `refresh_group_stats.py` already did safely in R5.

### Exact command

```bash
/usr/bin/python3 loaders/refresh_snapshot_summary.py
```

Same shape as R5's `refresh_group_stats.py` invocation: **no `--county` flag affects a real run** (the flag still exists, but as of this fix it only selects which county's freshness `--check-staleness` reports on — see the flag's own updated help text). This single command recomputes both counties' `snapshot_breakdown`/`snapshot_totals`/`snapshot_neighborhood_movers` rows at once.

### Live proof before trusting the write — `explain_snapshot_summary_county_derivation.py`

Before running the real refresh, run the read-only EXPLAIN proof script built alongside this fix (issues zero writes, zero row-returning execution — EXPLAIN only, inside a transaction that's always rolled back):

```bash
/usr/bin/python3 loaders/explain_snapshot_summary_county_derivation.py
```

**Expect:** `ALL 5 PLANS carry county_code as a group key for view='overall'.` This proves, against the real production planner and real production statistics/indexes — not just the SQL source text — that all five builders' aggregation genuinely groups by `county_code` per row, the structural signature that distinguishes "this query derives county_code per row" from "this query still secretly blends every county into one number." Run it a second time with `--view retail` (or another real view from `snapshot_taxonomy._SNAPSHOT_VALID_VIEWS`) if you want a second view's plan confirmed before trusting every view, not just `overall`.

If this script reports `NOT ALL PLANS carry county_code as a group key` for any view, **STOP — do not run the real refresh.** That result means either the deployed commit is missing this brief's fix (see R1.1) or a real regression has been introduced since — either way, running the real refresh at that point would reproduce REV 0's original blended-aggregate corruption risk.

### What running this does now — corrected from REV 0

Unlike REV 0's description of the old, broken behavior (which replaced Travis's rows with a Travis+Dallas blend mislabeled `DALLAS`), a real run now:

1. Computes every breakdown/totals/neighborhood aggregate **separately per county**, `county_code` derived from each row's own `parcel.county_code`.
2. Writes both Travis's and Dallas's rows into the same shadow tables, each carrying its own correct `county_code`.
3. Atomically swaps the shadow tables in — Travis's rows are recomputed (same underlying parcels, same expected values) and Dallas's rows appear for the first time, side by side in the same table, neither corrupting the other.

### The Travis invariant — same shape as R5, now meaningful for these three tables too

Re-run R2.2's three `snapshot_*` checksum queries (no longer caveated, per R2.2's REV 1 update above):

```sql
SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view, ptype))
FROM snapshot_breakdown t WHERE county_code = 'TRAVIS';

SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view))
FROM snapshot_totals t WHERE county_code = 'TRAVIS';

SELECT md5(string_agg((to_jsonb(t) - 'refreshed_at' - 'source_import_batch_id')::text, '~'
       ORDER BY view, neighborhood_cd))
FROM snapshot_neighborhood_movers t WHERE county_code = 'TRAVIS';
```

**Must equal R2-LOG's recorded pre-run values exactly.** If any differ, STOP and use R5's STOP-and-restore procedure verbatim (restore all three `snapshot_*` tables from R1.5's dump instead of just `group_stats`), same as REV 0 already instructed for this failure shape.

### The `(view, county_code)` consistency assertion — confirmed real, and now automatic

`assert_snapshot_breakdown_totals_consistent()` (or `--check-consistency`) — this was written into REV 1 by name; **re-confirmed against the live source for this delta**: `--check-consistency` is a real, currently-shipping flag on `loaders/refresh_snapshot_summary.py`'s `main()` (`ap.add_argument("--check-consistency", action="store_true", ...)`, wired to run `assert_snapshot_breakdown_totals_consistent(conn)` standalone with no refresh). No invocation correction was needed.

**One thing worth knowing that changed since REV 1 first named this flag:** as of PX-20260831-02 Task 1, `refresh_snapshot_summary()` itself (the function the real command above calls) now runs this exact same consistency check **automatically**, immediately after every real (non-dry-run) refresh swaps in, and raises `SnapshotConsistencyError` if it finds any mismatch — it is no longer gated behind a separate flag. So the command below is a **manual confirmation / standalone re-check**, not the only way this check runs; the real refresh command earlier in this section already enforces it and will fail loudly on its own if something is wrong. Running it explicitly here is still worthwhile as an independent, human-triggered confirmation step:

```bash
/usr/bin/python3 loaders/refresh_snapshot_summary.py --check-consistency
```

Expect zero mismatches for both `TRAVIS` and `DALLAS`. This check itself did not need to change (PX-20260830-05 Task 3 already had it grouping by `(view, county_code)` correctly) — REV 0 was correct that this part was never the bug; it's simply now exercised against genuinely per-county data instead of the blended aggregate REV 0 warned about.

---

## R7 — Live-site verification `[Diego/live]`

### `/dallas-tx/snapshot` renders real data

Once R6 has run correctly, `_snapshot_summary_freshness(county_code='DALLAS')` (app.py) checks that `snapshot_breakdown`, `snapshot_totals`, and `snapshot_neighborhood_movers` each have exactly one distinct `source_import_batch_id` for `county_code='DALLAS'`, that all three agree with each other, and that it matches `load_batch`'s latest `batch_id`. If all five of those checks pass, `_compute_snapshot_data()` reads real rows from the three summary tables (each query scoped `WHERE view = %s AND county_code = %s`) and the page renders populated cards, the "2026 Certified vs 2025 Certified" framing (or "preliminary"/"mixed", derived from `n_preliminary_2026`/`n_total_2026` the same three-way branch used for Travis), and real neighborhood movers.

### Travis snapshot unchanged vs. tonight's screenshots `[Diego/live]`

Take screenshots of `/travis-tx/snapshot` (all relevant views: overall + at least 2-3 sector tabs) **before** R4 begins, and again **after** R6 completes. Compare visually — the numbers, not just the layout, since R2.2's Travis checksums already prove the underlying data didn't move; the screenshots are the human-visible confirmation of the same fact, useful for a non-technical sanity check and for the record.

### The "being prepared" copy flips automatically — confirmed by reading the freshness-branch code

`app.py`'s `unavailable_copy("being_prepared", ...)` produces the current Dallas Market Snapshot text: *"Dallas County's Market Snapshot is being prepared. Parcel and appraisal data for Dallas County are live; the summary view will be available soon."* This is returned by `_snapshot_summary_freshness()` whenever any of its five freshness checks fails for `county_code='DALLAS'` — most relevantly, the very first check: `if not batch_ids_by_table[tbl]: return False, unavailable_copy(...)`, which fires today because `snapshot_breakdown`/`snapshot_totals`/`snapshot_neighborhood_movers` have **zero** Dallas rows (confirmed by R2.4's expected-0 count).

Once R6 runs correctly and Dallas rows exist with a `source_import_batch_id` matching `load_batch`'s latest entry, `_snapshot_summary_freshness()` returns `(True, None)` and `_compute_snapshot_data()` proceeds past the `data_unavailable` branch entirely — **the "being prepared" text disappears automatically, purely because the underlying freshness check now passes.** No template change, no copy change, and no manual flag flip is needed anywhere — this is a direct, mechanical consequence of the code path already in production, confirmed by reading `_snapshot_summary_freshness()` and `unavailable_copy()` in full.

---

## R8 — Post-run ledger `[Diego/live]` + `[PM checkpoint — reduced scope]`

### Published Metrics Log entry, marked not-sealed

Per `DATA_LIFECYCLE.md`'s publication rules (§5): external county-level claims quote **sealed**-vintage Metrics Log entries, not staged/promoted/verified ones. This first Dallas metrics run is not a sealed vintage — it is the first computation, not a reviewed-and-sealed one. **Any Dallas figure that might be cited publicly from this run (parcel counts, median values, YoY percentages) needs a Published Metrics Log entry explicitly marked `not-sealed`**, so nothing from this run is mistaken for a citable, sealed number until PM's own seal process (per `DATA_LIFECYCLE.md` §7 — PM "executes every checklist... performs seals") actually runs. **I could not find a committed, machine-readable Published Metrics Log format in this repo** — `DATA_LIFECYCLE.md` describes the Ledger/Metrics Log as a Notion DB with a repo mirror (§8, item 1: "Vintage Ledger schema (Notion DB + repo mirror)"), and this write-only, no-execution brief has no Notion access. **This runbook still cannot access Notion directly — PM (or Diego, handing Cowork the exact Notion database/template in a follow-up call) creates this entry directly in the Notion Ledger.** Nothing about the mechanism changed since REV 0; restated here only so it isn't lost among the sections that DID change around it.

### Task Log entry

Record this run (PX-20260831-02's execution, not this write-only runbook itself) in whatever Task Log convention Diego/PM already use for prior PX-numbered briefs — this repo's own commit history and the many `PX-YYYYMMDD-NN` task numbers referenced throughout this session imply a tracking convention exists, but **I could not find a single committed Task Log file in this repo to pattern-match against** — every reference to "Task Log" found in this repo appears inside spec/standards documents (`MULTI_COUNTY_ONBOARDING_STANDARDS.md`, `SPEC_TAX_BILLING_REKEY.md`, `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md`, `DATA_LIFECYCLE.md`) describing the *concept* of a Task Log, not an actual log file with entries to copy the format from. **[Diego/live]: use whatever your standing PX task-tracking tool actually is (Notion, per the same pattern as the Metrics Log) — record: date, the real PX task number, which of R4/R5/R6 actually ran, final row counts, and a link to this runbook.**

### `KNOWN_LIMITATIONS.md` line — wording approved, no longer a `[PM checkpoint]`

REV 0 drafted this entry but flagged it `[PM checkpoint]: confirm this exact wording before it's committed.` **PM has approved the wording verbatim** — this is no longer an open checkpoint. Add, in `KNOWN_LIMITATIONS.md`'s existing `## Data Coverage` section, matching the file's existing heading/prose style:

```markdown
### Dallas: effective_tax_rate, effective_tax_rate_derived, yoy_tax_amount_pct — Not available
- Dallas has no `tax_billing` or `tax_billing_entity` rows loaded (no billing source acquired yet — see Dallas County Profile / Source Registry for acquisition status).
- `effective_tax_rate` and `effective_tax_rate_derived` require a `tax_billing_entity.amount_due` sum for `tax_year = 2025`; with zero Dallas rows in that table, both are NULL for every Dallas parcel.
- `yoy_tax_amount_pct` requires `tax_billing.total_tax`; same gap, same result — NULL for every Dallas parcel.
- No billing data → no effective tax rate. This is a coverage gap, not a computation error — Dallas's `assessment_ratio`, YoY value/assessed-value percentages, and both homestead-cap signals (`cap_step_up_exposure`, `cap_expiry_signal`) are unaffected and populate normally, since none of those four depend on billing data.
- Resolves automatically once a Dallas billing source is acquired and loaded — no code change needed in `compute_metrics.py` itself.
```

Diego (or whoever commits this run's post-run documentation) can commit this text as-is, no further sign-off needed on the wording itself.

---

## Summary — what changed vs. the retired REV 0

| Section | REV 0 status | This document's status |
|---|---|---|
| R1.1 | Hardcoded a specific stale commit hash | Placeholder + a 3-grep confirmation that Tasks 1/3-4/5 are actually deployed |
| R1.2–R1.6 | — | Unchanged (now inlined here directly, no longer deferred to a separate file) |
| R2.1, R2.3, R2.4 | — | Unchanged (inlined). R2.1's `parcel_metrics.county_code` open question is now closed. |
| R2.2 | Three `snapshot_*` checksums caveated as meaningless until R6's bug fixed | Caveat dropped — all six checksums are now real, meaningful before/after checks |
| R3 | `[PM checkpoint]`, two unresolved options | PM ruled Option B; per-county map now built and registered; Diego's live measurement is pre-filled in R3-LOG; re-running it is optional, not required |
| R4 (row floor) | `[PM checkpoint]`, near-certain first-run failure expected | Per-county derived floor (0.5× Dallas's own source count) — closes this ruling, no longer a first-run blocker |
| R4 (TYPE_GROUPS) | Flagged as an unresolvable-from-code risk, discoverable only if it fires mid-run | Runnable pre-check added, using the query's own `label_case_sql()`/`TYPE_GROUPS`/`CANONICAL_PARCEL_EXCL_BARE` imports directly (not retyped) and reproducing the real query's full WHERE clause — same two PM-ruling branches (genuine gap vs. loader problem) if it does fire, but now checkable BEFORE the real run, with the actual row-population gap (missing WHERE filters, not label granularity) corrected from an earlier draft of this pre-check |
| R5, R7 | — | Unchanged (inlined). Nothing about `refresh_group_stats.py` or `app.py`'s read side changed in this brief. |
| R6 | `[PM checkpoint — do not run as-is]`, full section describing a confirmed blocking bug | Runnable, with a new live EXPLAIN proof step before the real write; `--check-consistency` confirmed to be a real, currently-shipping flag, and now also runs automatically after every real refresh (not just on manual invocation) |
| R8 (Metrics/Task Log) | — | Unchanged (inlined; still Notion, still no committed template in-repo) |
| R8 (KNOWN_LIMITATIONS wording) | `[PM checkpoint]`, draft pending approval | Approved verbatim, no longer an open checkpoint |

## Open questions remaining (down from REV 0's 8)

1. **R1.1:** fill in the real deployed commit hash once this brief is committed, and confirm the three greps pass before treating this runbook as applicable.
2. **R3:** only if Diego chooses to re-run `--analyze` and the live distribution shows meaningful drift from the pre-filled R3-LOG measurement — otherwise closed by default, proceed with the registered 45.0.
3. **R4:** only if the `TYPE_GROUPS` pre-check finds a genuine zero-row combination — PM rules genuine-gap-for-KNOWN_LIMITATIONS.md vs. loader-problem-to-fix-first, same two branches REV 0 already posed, just checkable earlier now, against a corrected pre-check.
4. **R8:** where the real Published Metrics Log / Task Log entries get written (Notion access this brief still does not have) — unchanged from REV 0.

**Closed since REV 0/REV 1:** R2.1's `parcel_metrics.county_code` live-column question (confirmed via `\d`, 2026-08-23); R6's county-scoping bug (fixed, PX-20260831-02 Task 1); R8's KNOWN_LIMITATIONS wording (approved verbatim).
