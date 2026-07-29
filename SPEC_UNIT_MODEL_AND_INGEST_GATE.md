# SPEC — Unit-Level Property Model & Ingestion Conservation Gate

**Author:** Fable (full architectural authority per brief)
**Date:** July 28, 2026
**Status:** Ready for Cowork implementation
**Supersedes:** Cowork's "parcel/unit-count coverage check" proposal (absorbed and extended in Part 4)

---

## 0. Verdict, in four sentences

The `parcel`-keyed-by-`geo_id` schema encodes a false invariant — that one TCAD account
maps to one appraised property — and every loader inherits that false invariant in a
different way, so the fix must live in the schema, not in any loader. The correct
architecture is a two-layer model: a new **unit layer** (`prop_unit`,
`prop_unit_tax_year`, keyed by `prop_id`, holding TCAD's actual truth) plus the existing
**account layer** (`parcel`, `parcel_tax_year`, keyed by `geo_id`, now *derived* from the
unit layer by one canonical rollup). This keeps every existing page, URL, join, and
query working unchanged while making the aggregate values correct, and it is what makes
an *exact* (zero-tolerance) ingestion check possible — you can only reconcile
count-for-count and dollar-for-dollar against the source file if you store what the
source file actually contains. The property-detail question is resolved below
(Part 2): yes, the pages are wrong today, in five distinct ways, all confirmed by code
reading.

---

## 1. Root cause, restated precisely — there are THREE mechanisms, not one

The brief documents one mechanism. Code reading found two more. This matters because a
fix aimed at "the lookup" would fix only one of the three and silently leave the others.
The disease is the schema's 1:1 assumption; the three loader mechanisms are just its
symptoms:

**Mechanism A — lookup-miss drop** (`load_certified_2025.py:190-214`,
`load_2026_preliminary.py:160-183`, `load_ajr.py` 2021 path). The
`prop_id → geo_id` dict is built from `SELECT prop_id, geo_id FROM parcel`. Because
`parcel` holds one row per geo_id, the dict contains only the *winning* prop_id
(last-in-PROP.TXT-file-order, via `ON CONFLICT (geo_id) DO UPDATE`). Every other
prop_id sharing that geo_id misses the lookup in `flush()` and its values are silently
discarded. This is the brief's confirmed mechanism: 3,625 colliding geo_ids, 23,708
orphaned prop_ids, $26.07B never written, for 2026.

**Mechanism B — overwrite drop** (`load_certified_historical.py:85-132`, used for
2022–2024). This loader does the *right* thing the brief might have prescribed as a
fix — it builds `pid_to_geo` from PROP.TXT directly, so **every** prop_id resolves. And
it still loses the same data: each unit's flush hits
`ON CONFLICT (geo_id, tax_year) DO UPDATE`, so units overwrite each other and
last-flushed-in-PROP_ENT-order wins. This is the proof that fixing the lookup alone is
insufficient — the loss just moves from the Python dict to the database constraint.

**Mechanism C — first-wins dedup drop** (`load_ajr.py:122-124`). The AJR loader dedups
on `if not geo_id or geo_id in seen: continue` — so for 2022–2024 AJR data the *first*
unit in file order wins and later units are skipped.

Two corollaries worth internalizing:

1. **The winner is nondeterministic across years and sources.** Certified years keep the
   last unit in PROP_ENT order; the 2026 preliminary keeps the last unit in PROP.TXT
   order; AJR years kept the first unit in AJR order (later replaced by certified
   reloads for 2022–2024). Nothing guarantees the same physical unit won in consecutive
   years, so a colliding geo_id's stored "history" can splice different condo units
   together. This cannot be quantified from the live DB today because `parcel_tax_year`
   stores no `prop_id` — itself evidence of the provenance gap.
2. **`parcel`'s scalar unit columns are winner-contaminated too.** `owner_name`,
   `owner_id`, `situs_address`, and `prop_id` on a colliding geo_id describe one
   arbitrary unit, presented as if they describe the account.

Independent corroboration (from the brief, kept here for the record): raw-file scan of
the 2025 certified PROP_ENT found 486,859 distinct legitimate prop_ids vs 479,181
`parcel_tax_year` 2025 rows — a 7,678-prop_id gap consistent with these mechanisms.

---

## 2. Property-detail pages: the open question, resolved — YES, they are wrong today

Confirmed by code reading (no live DB needed; every claim is a code path):

1. **Headline values are single-unit values labeled as the parcel's values.**
   `property_detail()` (`app.py:2326-2363`) renders `parcel_tax_year` rows for the
   geo_id verbatim. For the 3,625 colliding geo_ids, each year's market/assessed/
   taxable/land/imprv is one arbitrary unit's figure — e.g. geo_id `0100060237` (24
   units) shows one condo unit's value as the account's value. Not a combined value,
   and understated by roughly the sum of the other units.
2. **Owner and situs are one arbitrary unit's.** The page's owner display comes from
   `parcel.owner_name` — one of the 24 owners, chosen by file order.
3. **Year-over-year displays and derived metrics can splice different units.** Because
   winner selection differs by source/order (corollary 1 above), the value-history
   table, `yoy_market_value_pct`, `risk_large_value_jump`, `cap_step_up_exposure`, and
   `cap_expiry_signal` for colliding geo_ids may compare unit A's 2024 against unit
   B's 2025. Wrong-unit splicing is structural; per-parcel incidence is unknowable
   pre-migration (no prop_id stored) and moot post-migration.
4. **23,708 real prop_ids are unfindable.** Search resolution falls back to
   `SELECT * FROM parcel WHERE prop_id = %s` (`app.py:2209-2211`, again at `:3469-3474`).
   Loser prop_ids exist nowhere in the DB, so searching a valid TCAD prop_id for any
   non-winning condo unit 404s. KNOWN_LIMITATIONS.md's claim that every parcel is
   "reachable by TCAD account number (geo_id) or prop_id" is false today.
5. **Benchmarks ingest the understated rows.** `county_benchmark` medians and every
   snapshot/benchmark aggregate treat a 24-unit building's single-unit value as one
   parcel's value. (Directionally small for medians; real for sums — the $26B.)

One adjacent risk surfaced during this review, **not** confirmed and needing a
measurement (see Migration step M0): `tax_billing` keys on
`geo_id = PARCEL[:10]` (`load_tax_current.py:18-19, 222-224`) with
`ON CONFLICT (geo_id, tax_year) DO UPDATE`. If the tax office issues *separate 14-digit
accounts per condo unit* sharing a 10-char prefix, billing rows are last-write-wins
lossy by the same class of mechanism. The measurement is a pure file scan of
`TaxCurOpenData (1).csv` — deterministic, no DB.

---

## 3. The architectural fix — unit layer + derived account layer

### 3.1 Design principles

- **geo_id remains the public identity.** URLs (`/parcel/<geo_id>`), tax_billing joins,
  parcel_metrics, county_benchmark, and ~40+ query sites all key on geo_id. Re-keying
  the platform on prop_id was considered and rejected (see 3.8).
- **prop_id becomes the storage truth.** TCAD's export is prop_id-grained; we store it
  at that grain, permanently ending "which unit wins" as a concept.
- **The account layer is derived, never hand-written.** Exactly one canonical rollup
  (new module `parcel_rollup.py`, sibling to `parcel_filters.py` and following its
  one-source-of-truth pattern) computes `parcel_tax_year` from the unit layer. Loaders
  stop writing `parcel_tax_year` value columns directly — enforced by a grep test, the
  same way Rule 1 enforces the canonical filter.
- **Single-unit parcels (≈99%) are numerically unchanged.** Sum over one unit is that
  unit. The migration's blast radius is precisely the colliding accounts.

### 3.2 Schema (additive; follows schema.sql's idempotent conventions)

```sql
-- Unit layer: TCAD's actual grain. One row per prop_id.
CREATE TABLE IF NOT EXISTS prop_unit (
    prop_id         BIGINT       PRIMARY KEY,
    geo_id          VARCHAR(20)  NOT NULL,      -- latest-known account membership
    prop_type_cd    VARCHAR(5),
    owner_id        BIGINT,
    owner_name      TEXT,
    situs_address   TEXT,
    first_seen_year SMALLINT,
    last_seen_year  SMALLINT
);
CREATE INDEX IF NOT EXISTS idx_prop_unit_geo ON prop_unit(geo_id);

CREATE TABLE IF NOT EXISTS prop_unit_tax_year (
    prop_id         BIGINT       NOT NULL,
    tax_year        SMALLINT     NOT NULL,
    geo_id          VARCHAR(20)  NOT NULL,      -- membership AS OF that year (replat-safe)
    market_value    BIGINT,
    assessed_value  BIGINT,
    taxable_value   BIGINT,
    hs_cap_loss     BIGINT,
    land_value      BIGINT,
    imprv_value     BIGINT,
    exemption_codes TEXT,
    data_source     VARCHAR(20),
    PRIMARY KEY (prop_id, tax_year)
);
CREATE INDEX IF NOT EXISTS idx_puty_geo_year ON prop_unit_tax_year(geo_id, tax_year);
CREATE INDEX IF NOT EXISTS idx_puty_year     ON prop_unit_tax_year(tax_year);

-- Account layer gains provenance: how many units contributed to this row.
-- unit_count IS NULL  → legacy row the rollup has not rebuilt (pre-migration data)
-- unit_count = 1      → simple parcel, value identical to its single unit
-- unit_count > 1      → multi-unit account, values are sums across units
ALTER TABLE parcel_tax_year ADD COLUMN IF NOT EXISTS unit_count SMALLINT;
```

Deliberate choices: `geo_id` is denormalized onto `prop_unit_tax_year` because a
prop_id's account membership can change across years (replats); the year row records
membership as-of-that-year and the rollup groups on the *year row's* geo_id, while
`prop_unit.geo_id` tracks latest-known. No FK from `prop_unit` to `parcel(geo_id)`:
`load_certified_historical.py` deliberately doesn't touch `parcel`, and a historical
unit whose account has since closed must still be storable; referential integrity is
instead asserted by the gate (G4). No FK from `prop_unit_tax_year` to `prop_unit`
either, for the same loader-ordering reason — gate-checked instead.

### 3.3 `parcel_rollup.py` — the one canonical aggregation (new, repo root)

Mirrors `parcel_filters.py`: one module, imported everywhere, never re-typed. Core SQL
(parameterized by year; `--all-years` iterates):

```sql
WITH vals AS (
    SELECT geo_id, tax_year,
           SUM(market_value)   AS market_value,
           SUM(assessed_value) AS assessed_value,
           SUM(taxable_value)  AS taxable_value,
           SUM(hs_cap_loss)    AS hs_cap_loss,
           SUM(land_value)     AS land_value,
           SUM(imprv_value)    AS imprv_value,
           COUNT(*)            AS unit_count,
           MIN(data_source)    AS data_source
    FROM prop_unit_tax_year
    WHERE tax_year = %(yr)s
    GROUP BY geo_id, tax_year
),
codes AS (
    SELECT geo_id, tax_year,
           string_agg(DISTINCT code, ',' ORDER BY code) AS exemption_codes
    FROM prop_unit_tax_year,
         LATERAL unnest(string_to_array(exemption_codes, ',')) AS code
    WHERE tax_year = %(yr)s AND exemption_codes IS NOT NULL
    GROUP BY geo_id, tax_year
)
INSERT INTO parcel_tax_year
    (geo_id, tax_year, market_value, assessed_value, taxable_value, hs_cap_loss,
     land_value, imprv_value, exemption_codes, data_source, unit_count)
SELECT v.geo_id, v.tax_year, v.market_value, v.assessed_value, v.taxable_value,
       v.hs_cap_loss, v.land_value, v.imprv_value, c.exemption_codes,
       v.data_source, v.unit_count
FROM vals v LEFT JOIN codes c USING (geo_id, tax_year)
ON CONFLICT (geo_id, tax_year) DO UPDATE
    SET market_value    = EXCLUDED.market_value,
        assessed_value  = EXCLUDED.assessed_value,
        taxable_value   = EXCLUDED.taxable_value,
        hs_cap_loss     = EXCLUDED.hs_cap_loss,
        land_value      = EXCLUDED.land_value,
        imprv_value     = EXCLUDED.imprv_value,
        exemption_codes = EXCLUDED.exemption_codes,
        data_source     = EXCLUDED.data_source,
        unit_count      = EXCLUDED.unit_count;
```

Semantics, stated so nobody has to re-derive them: SQL `SUM` ignores NULLs and returns
NULL only when *every* unit is NULL — which preserves the platform's "NULL means not
available, never zero" standard for single-unit parcels exactly, and for multi-unit
accounts means a unit with no value contributes nothing (accepted limitation; TCAD
market is rarely NULL for sup-0 real property, and `unit_count` records the full
membership either way). Exemption codes union across units. `data_source` is uniform
within a (geo_id, year) since one loader owns each year — `MIN` is a tiebreak, and the
gate asserts uniformity. The rollup only touches (geo_id, year) pairs present in the
unit layer, so any legacy year that can't be rebuilt from source keeps its existing
rows untouched (`unit_count` stays NULL, honestly marking them legacy).

The rollup module also deterministically repairs `parcel`'s winner-contaminated
representative columns:

```sql
UPDATE parcel p
SET    prop_id = r.rep_pid
FROM  (SELECT geo_id, MIN(prop_id) AS rep_pid FROM prop_unit GROUP BY geo_id) r
WHERE  p.geo_id = r.geo_id AND p.prop_id IS DISTINCT FROM r.rep_pid;
```

`MIN(prop_id)` replaces file-order nondeterminism with a stable, documented choice.
`parcel.owner_name` stays loader-written (it is a display convenience), but the UI
stops presenting it as authoritative for multi-unit accounts (3.5).

### 3.4 Loader changes

**Shared parsing module first** (`loaders/ears_format.py`, new): the PROP.TXT /
PROP_ENT.TXT slice tables and the streaming per-prop_id accumulation logic are
currently retyped in at least three loaders — the same copy-drift disease
`parcel_filters.py` was created to cure, in the subsystem where it just caused a $26B
bug. Extract: `iter_prop_records(path)` (yields per-line dicts: prop_id, geo_id,
prop_type_cd, owner_id, owner_name, sup_num), `iter_prop_ent_aggregates(path)` (yields
one accumulated dict per prop_id: year, market/assessed/taxable via the existing
TCO-preference logic, exemption set), plus the slice constants. Pure functions over
file paths/lines — unit-testable in the sandbox with fixture strings, no DB. The gate
(Part 4) imports the *same* functions, so scanner and loader cannot disagree on
parsing by construction.

Then, per loader:

- **`load_certified_2025.py` and `load_2026_preliminary.py`:** delete the
  `SELECT prop_id, geo_id FROM parcel` lookup entirely — it was always unnecessary,
  because PROP.TXT carries prop_id *and* geo_id on the same line. Step 1 builds an
  in-memory `pid → (geo_id, owner…)` map from PROP.TXT while upserting `parcel` and
  `prop_unit`. Step 2 streams PROP_ENT via `iter_prop_ent_aggregates` and writes
  **`prop_unit_tax_year`** (key: prop_id, tax_year — no collisions possible). Land
  (LAND_DET is already per-prop_id) updates unit rows: `land_value` per unit,
  `imprv_value = max(0, unit_market − unit_land)`. SB12 flags exemptions on unit rows
  by prop_id directly (the pid_to_geo lookup there dies too). After all unit-level
  writes: call `parcel_rollup` for the year. Also fix the drifted docstring claiming
  "ON CONFLICT DO NOTHING" while the code does DO UPDATE (`load_2026_preliminary.py:22`
  vs `:152`).
- **`load_certified_historical.py`:** already builds pid→geo from PROP.TXT (mechanism B);
  redirect its writes to `prop_unit_tax_year` (+ upsert `prop_unit` with
  first/last_seen), then rollup. Its "don't touch `parcel`" policy stands.
- **`load_ajr.py`:** delete the `seen` geo_id dedup (mechanism C). AJR aggregate-entity
  rows are per-prop; write them per-prop to `prop_unit_tax_year`. The 2021 path's
  pid→geo lookup now reads `prop_unit` (contains *all* prop_ids), which will also
  shrink the synthetic `AJR`-prefixed geo_id population for 2021. Then rollup.
- **`loaders/run_all.py`:** same loader order as today; single `parcel_rollup --all-years`
  after loads; then the gate (Part 4); `compute_metrics` runs only if the gate passes.

**Hard rule (add to BUILD_WORKFLOW.md as part of Rule 1's family):** no loader writes
`parcel_tax_year` value columns directly. Only `parcel_rollup.py` does. A regression
grep-test (pattern of the planned `verify_parcel_filters_canonical.py`) asserts no
other file contains an `INSERT INTO parcel_tax_year` targeting value columns.

### 3.5 Application-layer changes (deliberately minimal)

- **`property_detail()` (`app.py:2326`):** one added query —
  `SELECT u.prop_id, u.owner_name, u.situs_address, y.market_value, y.assessed_value,
  y.taxable_value FROM prop_unit u LEFT JOIN prop_unit_tax_year y ON y.prop_id =
  u.prop_id AND y.tax_year = <latest> WHERE u.geo_id = %s ORDER BY u.prop_id`. When it
  returns >1 row: render a "Multi-unit account — N units" panel listing units, change
  the owner line to "Multiple owners (N units)", and label headline values "account
  total (N units)". When 1 row: render nothing new. History-table rows where
  `unit_count > 1` get the same account-total labeling.
- **prop_id resolution:** extend the fallback at `app.py:2209-2211` (and its twin at
  `:3469-3474`; centralize into one helper) — if `parcel.prop_id` misses, look up
  `prop_unit.prop_id → geo_id` and serve that account's page. This makes all 23,708
  orphaned prop_ids findable.
- **`compute_metrics.py`:** inputs unchanged (it reads `parcel_tax_year`), with one
  semantic gate: the homestead-specific signals (`cap_step_up_exposure`,
  `cap_expiry_signal`, and the legacy `risk_homestead_cap_expiry`) are only
  computed where `unit_count = 1` (or IS NULL, for legacy rows). Account-level
  exemption codes are a *union* across units post-rollup; "this building has one HS
  unit somewhere" must not fire homeowner-cap logic on a 24-unit account. Affects at
  most the 3,625 colliding accounts; documented in the metrics docstring.
- **Everything else** — snapshot, benchmark, search_filter, peer sets, PDF export —
  is untouched by design: same tables, same keys, corrected values.

### 3.6 What "a parcel" now means (documentation change, KNOWN_LIMITATIONS.md + About)

A `parcel` row is a **TCAD account** (geo_id). Most accounts contain one appraised
property; condo/multi-unit accounts contain many, and their displayed values are
account totals across units. The unit layer is the appraisal-grain truth. This is also
TCAD's own semantics — we are stopping the platform from pretending otherwise.

### 3.7 tax_billing (measured decision, not assumed)

Migration step M0 scans `TaxCurOpenData (1).csv`: count distinct 14-digit `PARCEL`
values vs distinct 10-char prefixes, and list prefix groups with >1 account plus their
tax dollar sums. Decision rule: if zero collision groups → billing is genuinely
account-grained, nothing to do, record the result in KNOWN_LIMITATIONS.md. If nonzero →
`tax_billing` has last-write-wins loss of the same class; that becomes its own brief
(likely: key billing rows by the full 14-digit account with a geo_id column, and sum to
account level for display) — do **not** bundle it into this implementation, but the
measurement itself is mandatory here because the effective-tax-rate metric divides
account-level tax by (now-corrected, larger) account-level value, and we need to know
whether the numerator is complete.

### 3.8 Alternatives rejected, with reasons

- **Re-key the platform on prop_id.** Breaks every URL, tax_billing's 10-char join,
  parcel_metrics, county_benchmark, and 40+ query sites; destroys bookmarked/indexed
  pages. All cost, and the account is the entity users search anyway.
- **Aggregate inside loaders in Python, no unit tables.** Loses unit provenance (can't
  render the unit panel, can't resolve orphan prop_ids, can't verify sums against
  source per-unit), and each loader reimplements aggregation — the exact copy-drift
  disease that produced mechanisms A/B/C being reintroduced at the fix site. Also
  makes the gate's exact reconciliation impossible.
- **Change `parcel`'s PK to (geo_id, prop_id).** Every existing join assumes geo_id
  uniqueness; this is the maximal-blast-radius variant of the chosen design with no
  additional benefit over a separate unit table.
- **Store a combined value by UPDATE-adding in loaders (`DO UPDATE SET market_value =
  parcel_tax_year.market_value + EXCLUDED...`).** Non-idempotent: re-running a loader
  double-counts. Rollup-from-truth is idempotent by construction.

Costs of the chosen design, stated honestly: ~487K unit rows × 6 years ≈ 3M rows of new
storage (trivial at this scale); a full reload of all years from retained source
exports (Part 5 — all sources are still on disk per `config.py`); public numbers
change for 3,625 accounts and every county aggregate (that is the point, but it needs
a Rule 3 verified-stats refresh and a correction note, given the LinkedIn origin of
this whole investigation); and one new invariant to maintain (loaders never write
account-layer values), which is grep-enforced.

---

## 4. The Ingestion Conservation Gate (replaces/extends Cowork's proposal)

### 4.1 Why extend rather than adopt as-is

Cowork's parcel/unit-count coverage check (source distinct prop_ids vs DB rows) is
correct and becomes check G2 — but counts alone can pass while dollars drift (a value
field offset bug loads the right number of wrong values), and dollar totals alone can
pass while counts drift (offsetting errors). The generalized failure class this
platform keeps hitting — NULL-propagation filter drops, sparse-column reads, and now
collision drops — is *silent shrinkage*: records leaving the pipeline with no named
reason. The gate's principle is therefore **conservation**: every record observed in a
source file must be accounted for — loaded, or skipped for a named, counted reason —
and identity counts *and* dollar sums must reconcile exactly. Exactness is what the
unit layer buys: source and DB are now the same grain, so internal tolerance bands
(which would have hidden this bug: 7,678 of 486,859 is 1.6%, comfortably inside ±5-8%)
are neither needed nor allowed internally. Bands remain correct for the *external*
TCAD-published-totals comparison (Rule 2), where scope differences (BPP policy,
protest-cycle drift) are real. Two layers, two standards: internal = exact, external =
banded. Neither alone catches everything — the gate shares parsing code with the
loaders, so a parsing bug cancels out internally; the external banded check is what
catches parse-level value corruption, and the internal exact check is what catches
silent shrinkage the band would swallow.

### 4.2 `loaders/ingest_gate.py` (new; absorbs `investigate_geo_id_prop_id_collision.py`)

Runs automatically at the end of every loader `main()` and from `run_all.py`; also
runnable standalone (`--year`, `--source-dir`). Uses `ears_format.py` for all file
parsing. Checks, per ingestion:

- **G1 — Source scan** (pure file, no DB): distinct sup-0 prop_ids carrying a
  resolvable geo_id and value; distinct geo_ids; `SUM(market_value)` accumulated the
  same way the loader accumulates it; skip ledger — every line classified as exactly
  one of {loaded-candidate, supplement-skip, short-line-skip, no-geo-skip,
  no-value-skip}, with counts, and counts must sum to total lines (the conservation
  identity).
- **G2 — Identity coverage** (exact, zero tolerance): G1's distinct prop_id count ==
  `COUNT(*) FROM prop_unit_tax_year WHERE tax_year = Y`. This single check, existing on
  day one, catches this bug's entire class immediately (486,859 ≠ 479,181 fails loudly).
- **G3 — Dollar conservation** (exact): G1's source `SUM(market_value)` ==
  `SUM(market_value) FROM prop_unit_tax_year WHERE tax_year = Y` ==
  `SUM(market_value) FROM parcel_tax_year WHERE tax_year = Y AND unit_count IS NOT NULL`.
- **G4 — Rollup integrity** (exact, SQL only): zero rows where a `parcel_tax_year`
  value differs from the sum of its units for that year; zero unit rows whose geo_id
  has no `parcel_tax_year` row; `data_source` uniform per (geo_id, year); zero
  `prop_unit_tax_year.prop_id` values absent from `prop_unit`.
- **G5 — Account coverage** (exact): G1's distinct geo_id count == distinct geo_ids in
  `parcel_tax_year` for Y with `unit_count IS NOT NULL`.
- **G6 — External reconciliation** (banded — the existing Rule 2, now automated where a
  reference exists): platform aggregate vs TCAD-published total for the same scope,
  ±5-8% band, using a small `reference_totals` config (year → published figure +
  documented scope notes, e.g. the BPP exclusion policy). Skipped with a loud notice
  when no reference figure has been entered for the year.

Results: printed report; one row appended to a new `ingest_audit` table
(`run_at TIMESTAMPTZ, tax_year, source_path TEXT, metrics JSONB, passed BOOLEAN`);
**exit code 1 on any failure**, and `run_all.py` halts before `compute_metrics` on
failure. BUILD_WORKFLOW.md gains the corresponding rule (call it **Rule 4 —
Conservation gate on every ingestion**: no load is "done", and no Rule 3 verified-stats
refresh may occur, unless the year's latest `ingest_audit` row has `passed = TRUE`).

### 4.3 The alarm must itself be tested

A safeguard that has never fired is a hope, not a safeguard. Sandbox-runnable unit
tests (fixtures through `ears_format` + gate logic with a stubbed DB layer, or a local
throwaway Postgres if available): (a) a fixture with a 3-unit collision where the
"DB" holds only 1 row → G2 fails; (b) a fixture where one unit's value was dropped →
G3 fails; (c) the clean fixture → all pass. These tests are the acceptance proof that
the gate would have caught this incident.

---

## 5. Migration & backfill plan (Travis 2021–2026)

All source files are retained per `config.py` (AJR 2021–2024 CSVs, certified exports
2022–2025, 2026 preliminary, TaxCur/TaxDelq CSVs). Order matters; each step names its
runner. Steps marked **[human/live]** are Diego-run against the real DB per the
established workflow; everything else is Cowork-implementable and sandbox-testable.

- **M0 [deterministic file scan]** — TaxCur 14-digit collision measurement (3.7).
  Record the result either way.
- **M1 [human/live]** — Fresh `pg_dump` backup, and capture the pre-migration reference
  snapshot (SQL provided in the implementation): per-year row counts, per-year
  `SUM(market_value)`, and the 3,625 colliding geo_ids' current rows (from the
  investigation script's collision list) for before/after diffing.
- **M2** — Land schema + code: apply schema additions; build `ears_format.py`,
  `parcel_rollup.py`, `ingest_gate.py`; refactor the four loaders; app.py changes;
  metrics gating; tests. (This is the Cowork build unit.)
- **M3 [human/live]** — Reload, in `run_all.py`'s existing order, now unit-writing:
  2025 certified → AJR 2021–2024 → certified historical 2022–2024 → 2026 preliminary
  (each loader's own land/SB12 steps included). Loaders are idempotent upserts at unit
  grain, so this is a re-run, not a wipe: every (geo_id, year) with unit data gets
  rolled up and corrected in place; nothing else is touched. If any year's source
  were missing, that year would simply keep legacy rows with `unit_count IS NULL` —
  honest and visible — but per config all sources exist.
- **M4 [human/live]** — `parcel_rollup.py --all-years`, then `ingest_gate.py` per
  year/source. Expected outcomes, checked against M1's snapshot: 2026
  `SUM(market_value)` rises by exactly the source-scan delta (the measured
  $26,074,757,873 figure re-derived by G1, not assumed); 2025 unit rows == 486,859
  exactly; only colliding geo_ids' `parcel_tax_year` values change (diff count ≈ the
  per-year collision counts); all gate checks green.
- **M5 [human/live]** — `compute_metrics.py` full refresh (parcel_metrics +
  county_benchmark, all years). Colliding accounts' YoY metrics now compare
  like-for-like sums.
- **M6 [human/live]** — Rule 2/G6 external reconciliation against TCAD's published
  totals; then regenerate the Rule 3 verified-stats reference. Given the LinkedIn
  origin: the county totals will move publicly — recommend a short correction note,
  and a version bump (MINOR: additive schema, corrected data, no breaking interface;
  final call stays with the existing versioning step).
- **M7** — Documentation: KNOWN_LIMITATIONS.md (parcel-vs-unit semantics, the M0
  result, removal of the false "reachable by prop_id" claim until verified, then its
  reinstatement); BUILD_WORKFLOW.md Rule 4; CHANGELOG.

Rollback: M1's dump. The migration never deletes source-grained data (unit tables are
new; account tables are upserted), so partial failure leaves the site no worse than
today — legacy rows still render, and `unit_count` marks exactly how far the rebuild got.

---

## 6. Acceptance criteria

Sandbox-verifiable by Cowork (deterministic, no live DB):

- **AC1** — `ears_format.py` fixture tests: PROP.TXT and PROP_ENT.TXT fixture lines
  (including a 24-unit collision modeled on geo_id `0100060237`, supplement rows,
  short lines, TCO-preference cases) parse to the exact expected dicts; the
  accumulation logic reproduces the current loaders' output for non-colliding fixtures
  byte-for-byte.
- **AC2** — Loader logic tests: for a colliding fixture, N unit rows are produced
  (zero drops, all three mechanisms' trigger conditions covered: lookup path, upsert
  path, dedup path); re-running is idempotent.
- **AC3** — Rollup tests: summed values, NULL semantics (all-NULL → NULL; mixed →
  partial sum), exemption union, `unit_count`, `MIN(prop_id)` representative — each
  asserted on fixtures; rollup is idempotent.
- **AC4** — Gate alarm tests from 4.3 pass, including both deliberate-corruption
  cases *failing*.
- **AC5** — Grep/regression tests: no `INSERT INTO parcel_tax_year` value-writes
  outside `parcel_rollup.py`; no slice-table copies outside `ears_format.py`; every
  loader imports the gate; no re-typed rollup SQL.
- **AC6** — M0 scan implemented and runnable against the staged/real TaxCur CSV;
  output states collision-group count and dollar exposure explicitly, even when zero.
- **AC7** — app.py: unit-panel rendering (multi-unit fixture → panel + "Multiple
  owners (N units)"; single-unit → unchanged output), prop_id fallback resolution
  helper unit-tested, homestead-signal gating covered by a `unit_count > 1` test.
- **AC8** — Honest-disclosure standard: anything Cowork could not execute in-sandbox
  (live queries, real file scans it lacks) is listed explicitly in its report, mapped
  to the AC-L items below — not reported as done.

Live-DB / human-run (Diego, with exact commands supplied by Cowork in its report):

- **AC-L1** — Post-M3/M4: `COUNT(*) FROM prop_unit_tax_year WHERE tax_year=2025` =
  486,859; gate green for every year/source; 2026 G3 delta equals the G1-measured
  source sum.
- **AC-L2** — `/parcel/0100060237` shows the 24-unit panel, account-total labeling,
  and a headline market value equal to the sum of its units (cross-check one unit
  against TCAD's site).
- **AC-L3** — Searching a previously-orphaned prop_id (pick three from the
  investigation script's loser list) resolves to the correct account page.
- **AC-L4** — Only colliding geo_ids' `parcel_tax_year` values changed vs M1's
  snapshot (diff query provided); non-colliding spot-checks (the QA known-parcel
  trio) are byte-identical.
- **AC-L5** — External reconciliation (G6) within band against TCAD published totals
  under the documented BPP scope policy; verified-stats file regenerated after — and
  only after — that pass.

---

## 7. Explicitly out of scope (each needs its own brief)

- **tax_billing re-keying**, if and only if M0 finds collisions (decision rule in 3.7).
- **Dallas / multi-county onboarding.** This design is county-agnostic in logic
  (parameterized paths, shared parsing, gate per source) but the schema is still
  single-county: geo_ids from two CADs are not guaranteed globally unique. Before any
  Dallas ingestion, a `county_code` key migration across `parcel`, `prop_unit`, and
  the year tables is required — flagging it now so it's a planned migration, not a
  rediscovered collision class. The unit-layer pattern itself transfers directly:
  Dallas's export is also prop-grained.
- **BPP certified-year placeholder values** (`parcel_filters.py` documents this
  workaround; unchanged here).
