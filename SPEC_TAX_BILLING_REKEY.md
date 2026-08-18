# tax_billing / tax_billing_entity — Re-Key + Partitioning + Transitional-Index Design (TAX-BILLING-REKEY-1)

**Status:** DESIGN-ONLY, NOT IMPLEMENTED. No schema change, migration script, or
live database action results from this document. Answers the three real,
interlocking questions `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md` §2 raised
but explicitly left unresolved, using the real, measured M0/M0-EXTENSION-1
numbers already recorded in `KNOWN_LIMITATIONS.md` ($5,794,968.90 `tax_billing`
loss, $170,061,400.28 `tax_billing_entity` loss, both 2025-only) as fixed
inputs, not re-derived here. Per this brief's own explicit next step: this
document goes to Fable for architectural review before any implementation —
the same process every prior major schema decision in this codebase has
followed (`SPEC_UNIT_MODEL_AND_INGEST_GATE.md`, `SPEC_COUNTY_PARTITIONING.md`).
Nothing here executes against production.

---

## 0. Verdict, in five sentences

Mirror the `prop_unit`/`prop_unit_tax_year` precedent exactly: two new
unit-grain tables (`tax_billing_account`, `tax_billing_account_entity`) keyed
on the real, full 14-digit tax-office account number — already present in
every source file this codebase reads (`PARCEL` in `TaxCurOpenData`,
`TXACCNUM` in the 2021 PIR export) and currently discarded after truncation to
`geo_id` — with `tax_billing`/`tax_billing_entity` becoming pure, derived
rollups written by one new canonical module (`tax_billing_rollup.py`,
mirroring `parcel_rollup.py`), so none of app.py's 9 real read call sites need
to change. Native partitioning is **not** recommended for `tax_billing_entity`
even at the corrected ~27-28M-row Dallas-onboarding projection — but that
projection itself needs to be re-measured once the unit layer exists, because
today's 10,770,184-row count is an *undercount* relative to real entity-grain
population (it's the collision-lossy figure the M0-EXTENSION-1 measurement
found losing 106,297 real overwrite events), not a stable baseline to project
forward from. The two existing transitional indexes (`idx_billing_geo`,
`idx_tbe_entity_code`) are **not touched by this migration at all** — they
sit on the derived rollup tables, whose key shape doesn't change — which
corrects a premise in `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md` §2c's own
framing, flagged explicitly in §3 below. This re-key does not need to wait for
the resolver-seam (`resolve_parcel()`) to be fully wired into app.py — that
seam only touches the `parcel` table and isn't wired into any call site yet
regardless — but it should still land before Dallas rows exist, per the
promoted brief's own "one migration, not three" reasoning, independent of the
resolver seam's own unrelated timeline.

---

## 1. Part 1 — The re-key design

### 1.1 What's actually being thrown away today (confirmed, not assumed)

Every current billing writer has the real 14-digit account number in hand and
discards it after truncating to `geo_id`:

- `loaders/load_tax_current.py` (2025 current-year, the primary writer):
  `raw_parcel = row.get("PARCEL", ...)`, then `geo_id = raw_parcel[:10]` —
  `raw_parcel` itself is never stored anywhere.
- `loaders/load_pir_billing.py` (same `TaxCurOpenData`-shaped CSV format,
  different vintage): identical `raw_parcel[:10]` truncation, identical loss.
- `loaders/load_pir_billing_2021_full.py` (2021 PIR bulk export,
  `TXACCNUM`-keyed): **already independently discovered and partially solved
  half of this exact problem** — its own finding 2(a) documents "SAME 10-char
  geo_id prefix, DIFFERENT 14-char TXACCNUM suffix... genuinely separate
  billable unit(s)... must be SUMMED per (geo_id, entity_code)" for 1,696
  real multi-sub-account geo_ids, and implements that summation before
  writing. This is real, working prior art for the "sum sub-accounts to the
  account total" half of this design — but it still writes only the *summed*
  result into `tax_billing`/`tax_billing_entity`, discarding `TXACCNUM` itself
  after aggregating, so it doesn't preserve unit-level provenance either, and
  it isn't consistent with `load_tax_current.py`'s own last-write-wins
  behavior on the exact same class of collision for 2025 data. **This
  inconsistency between loaders — one sums, one overwrites — for the
  structurally identical problem is itself a real, concrete argument for one
  canonical rollup module**, the same copy-drift risk `parcel_filters.py` and
  `parcel_rollup.py` were built to close on the appraisal side.
- `loaders/scrape_billing_history.py` (`upsert_billing_rows()`, called both
  by its own CLI batch mode and live, on-demand, by `api_billing()` in
  app.py): architecturally different — driven by a per-`geo_id` scrape of the
  county tax portal's own receipt page, not a bulk `PARCEL`-keyed file.
  `parse_receipts()` returns `{tax_year, payment_amount}` with no account
  identifier in the source data at all. **Real, unresolved, flagged rather
  than assumed:** whether the portal's own per-`geo_id` receipt page already
  reflects combined receipts across all of that `geo_id`'s sub-accounts, or
  only one, is not something this sandbox can verify (no live network
  access to the county portal). If it's the latter, this write path carries
  an analogous — but separately-caused — undercount risk that this design
  does not resolve, because there is no equivalent full-account identifier in
  its own source data to re-key against. **Not decided here** — a real,
  live check of the portal's actual per-`geo_id` receipt content for a known
  multi-sub-account parcel (e.g. `0259410216`, M0's own largest collision
  group) is the concrete next step, separate from this design.

### 1.2 Schema — new unit layer, additive, mirrors §3.2 of `SPEC_UNIT_MODEL_AND_INGEST_GATE.md`

```sql
-- Unit layer: one row per real 14-digit tax-office account.
-- Mirrors prop_unit's role exactly -- geo_id here is "membership as of
-- the row's own tax_year," same replat-safety reasoning prop_unit_tax_year
-- uses (a sub-account's parent geo_id assignment is not assumed permanent,
-- though no evidence of it changing has been found -- flagged as an open
-- question in §5, not assumed either way).
CREATE TABLE IF NOT EXISTS tax_billing_account (
    account_id      VARCHAR(14)  NOT NULL,      -- full PARCEL / TXACCNUM, zero-padded
    county_code     VARCHAR(20)  NOT NULL DEFAULT 'TRAVIS',
    tax_year        SMALLINT     NOT NULL,
    geo_id          VARCHAR(20)  NOT NULL,       -- account_id[:10], denormalized per-year
    billing_num     VARCHAR(30),
    owner_name      TEXT,
    total_tax       NUMERIC(14,2),
    total_paid      NUMERIC(14,2),
    total_due       NUMERIC(14,2),
    is_delinquent   BOOLEAN      DEFAULT FALSE,
    cause_number    VARCHAR(50),
    exemption_codes VARCHAR(50),
    data_source     VARCHAR(32),
    confidence_level VARCHAR(16),
    PRIMARY KEY (county_code, account_id, tax_year)
);
CREATE INDEX IF NOT EXISTS idx_tba_geo_year ON tax_billing_account(county_code, geo_id, tax_year);

CREATE TABLE IF NOT EXISTS tax_billing_account_entity (
    account_id   VARCHAR(14) NOT NULL,
    county_code  VARCHAR(20) NOT NULL DEFAULT 'TRAVIS',
    tax_year     SMALLINT    NOT NULL,
    geo_id       VARCHAR(20) NOT NULL,
    entity_code  VARCHAR(10) NOT NULL,
    amount_due   NUMERIC(14,2),
    amount_paid  NUMERIC(14,2),
    PRIMARY KEY (county_code, account_id, tax_year, entity_code)
);
CREATE INDEX IF NOT EXISTS idx_tbae_geo_year ON tax_billing_account_entity(county_code, geo_id, tax_year);

-- Existing tables gain provenance, same pattern as parcel_tax_year.unit_count:
-- NULL = legacy row, not yet rolled up from the unit layer
-- 1    = single sub-account, values identical to that one account
-- >1   = true multi-sub-account geo_id, values are SUM() across accounts
ALTER TABLE tax_billing        ADD COLUMN IF NOT EXISTS account_count SMALLINT;
ALTER TABLE tax_billing_entity ADD COLUMN IF NOT EXISTS account_count SMALLINT;
```

Deliberate departure from the `prop_unit` precedent, stated so it isn't
mistaken for an oversight: `prop_unit` needed a *separate* surrogate key
(`prop_id`, TCAD-assigned) because TCAD's own appraisal export has no single
column that is both stable and finer-grained than `geo_id`. Billing doesn't
have that problem — the 14-digit account number **is** already a real,
stable, source-provided finer-grained identifier (confirmed exactly 14
digits, all-numeric, no exceptions, by `load_pir_billing_2021_full.py`'s own
full-file scan). So `tax_billing_account.account_id` needs no synthetic ID;
it's the real account number itself, stored as `VARCHAR(14)` to preserve
leading zeros, matching `geo_id`'s own `VARCHAR` convention rather than
`prop_id`'s `BIGINT`.

### 1.3 `tax_billing_rollup.py` — the one canonical aggregation (new, repo root)

Mirrors `parcel_rollup.py` directly:

```sql
INSERT INTO tax_billing (county_code, geo_id, tax_year, billing_num, owner_name,
    total_tax, total_paid, total_due, is_delinquent, exemption_codes,
    data_source, confidence_level, account_count)
SELECT county_code, geo_id, tax_year,
       -- billing_num/owner_name/cause_number: MIN() tiebreak, same
       -- rationale as parcel_rollup's data_source MIN() -- these are
       -- display convenience fields, not summed quantities.
       MIN(billing_num), MIN(owner_name),
       SUM(total_tax), SUM(total_paid), SUM(total_due),
       BOOL_OR(is_delinquent),
       string_agg(DISTINCT exemption_codes, ',' ORDER BY exemption_codes),
       MIN(data_source), MIN(confidence_level),
       COUNT(*)
FROM tax_billing_account
WHERE tax_year = %(yr)s
GROUP BY county_code, geo_id, tax_year
ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE
    SET total_tax = EXCLUDED.total_tax, total_paid = EXCLUDED.total_paid,
        total_due = EXCLUDED.total_due, is_delinquent = EXCLUDED.is_delinquent,
        exemption_codes = EXCLUDED.exemption_codes,
        data_source = EXCLUDED.data_source, confidence_level = EXCLUDED.confidence_level,
        account_count = EXCLUDED.account_count;
```

...with the equivalent `SUM(amount_due)`/`SUM(amount_paid)` grouped rollup
into `tax_billing_entity` from `tax_billing_account_entity`. Same NULL
semantics as `parcel_rollup.py`'s own docstring establishes (SQL `SUM`
ignores NULLs, a fully-NULL group rolls up to NULL, never a silent 0) — this
matters here specifically because `tax_billing.total_tax` being NULL vs. 0 is
already a load-bearing distinction elsewhere in this codebase (the
`data_source`/`confidence_level`/"derived from entity sum" logic
`load_tax_current.py` already implements at write time — see its own module
docstring). **Real, honest note on the derived-value fallback:** that
existing 0.00-vs-derived-from-entity-sum logic currently lives in the loader,
computed once per `(geo_id, tax_year)` before writing. Once the unit layer
exists, the natural home for that same fallback is inside
`tax_billing_rollup.py` itself, computed once per rollup rather than
independently in every loader — a real simplification this re-key enables,
not required by it. Not designed in full here; flagged as a concrete
follow-on cleanup once the base re-key lands.

`account_count` mirrors `unit_count`'s semantics exactly, giving `property.html`
a real, honest future hook — a "this account reflects N sub-accounts" note —
without requiring one; `app.py`'s existing read sites ignore the new column
by default (`SELECT *` sites pick it up automatically and harmlessly; explicit
column-list sites are simply unaffected).

### 1.4 Loader changes

Each of the four PARCEL/TXACCNUM-keyed writers (§1.1) stops writing
`tax_billing`/`tax_billing_entity` directly and instead writes
`tax_billing_account`/`tax_billing_account_entity`, keyed by the real
`account_id` it already parses and currently discards — a one-line change to
what each loader's own buffered-row tuple contains, not a rewrite of parsing
logic:

- **`load_tax_current.py`**: `raw_parcel` (currently discarded after
  `geo_id = raw_parcel[:10]`) becomes the `account_id` value written to
  `tax_billing_account`/`_entity`. No `ON CONFLICT` collision is possible
  at this new grain — every real account gets its own row, by construction —
  which is the entire point; this is what actually stops the
  $170M/$5.8M-a-year real loss, not a bigger buffer or a smarter tiebreak.
  After the batched writes: call `tax_billing_rollup.rollup_tax_year(2025)`.
- **`load_pir_billing.py`**: same change, same reasoning.
- **`load_pir_billing_2021_full.py`**: **its own existing sub-account
  summation logic (finding 2(a)) is retired, not extended** — that logic
  exists only because there was previously nowhere to put per-sub-account
  detail; once `tax_billing_account` exists, each of the 1,696 real
  multi-sub-account geo_ids' individual `TXACCNUM` rows get written directly,
  and the rollup module does the summing instead, uniformly with every other
  loader. This loader's **other** piece of collision handling — the
  exact-duplicate-`TXACCNUM` resolution (finding 2(b), the majority-vote
  clustering logic for 3,008 accounts whose *same* account number appears
  more than once in the export) — is **unrelated and stays exactly as-is**:
  that's resolving which of several repeated extracts of the *same* real
  account is correct, a data-quality problem this re-key doesn't touch, and
  it still needs to run before a `tax_billing_account` row is written for
  that account, same as today.
- **`scrape_billing_history.py`**: **unchanged** — per §1.1, its source data
  has no account-number field to re-key against at all. It continues writing
  directly to `tax_billing` at `(county_code, geo_id, tax_year)` grain, same
  as today. This does mean `tax_billing` gains two real write paths after
  this migration — the rollup (for PARCEL/TXACCNUM-sourced rows) and this
  loader (for portal-scrape-sourced rows) — which is not a new problem this
  design introduces: `tax_billing.data_source` already distinguishes
  `'taxcur_current'`/`'pir_billing'`/`'portal_scrape'`-tagged rows today for
  exactly this reason, and the rollup's own `ON CONFLICT ... DO UPDATE`
  target is the same row a portal-scrape sentinel might already occupy. Real
  ordering question, not decided here: whether a portal-scrape row should be
  protected from being overwritten by a later rollup run the way
  `load_tax_current.py`'s `--new-only` mode protects already-tagged rows —
  flagged for the actual implementation brief, not resolved in this design.
- **`loaders/run_all.py`**: same shape as `parcel_rollup`'s integration —
  loaders write the unit layer, then `tax_billing_rollup.rollup_all_years()`
  runs once, before `compute_metrics.py`.

**Hard rule, same family as the existing one:** no loader writes
`tax_billing`/`tax_billing_entity` value columns directly except
`scrape_billing_history.py` (the one architecturally-justified exception,
named explicitly above) and `tax_billing_rollup.py` itself. A grep-test
mirroring `verify_parcel_filters_canonical.py`'s pattern enforces this.

### 1.5 Application-layer changes (deliberately none, mirroring §3.5's own restraint)

All 9 real app.py call sites found (`build_bill_waterfall`, `property_detail`,
`export_due_diligence_pdf`, `api_parcel_entities`, `api_estimate_acq`,
`api_peer_benchmark_local`, `api_peer_set`, `api_billing`, `compare_parcels`)
read `tax_billing`/`tax_billing_entity` directly — **none of them write to
these tables** (confirmed via grep: the only `INSERT`/`UPDATE` against
`tax_billing` in app.py is `api_billing()`'s own sentinel-row insert, which
is `scrape_billing_history.py`'s call path, already addressed in §1.4).
Because the derived tables keep their exact current shape, PK, and contents
(same rows, same columns, now written by the rollup instead of loaders
directly, plus one new nullable `account_count` column every existing query
either ignores or picks up harmlessly via `SELECT *`), **zero of these 9 call
sites need to change** for this migration to be complete and correct. This is
the direct, intended benefit of the derived-rollup pattern over re-keying
`tax_billing`/`tax_billing_entity` themselves (rejected below) — the entire
blast radius is loaders + one new module + two new tables, not nine read
sites plus every future one.

A real, optional future addition — not required by this brief, flagged the
same way `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §3.5 flagged the multi-unit
panel: `property_detail()` could query `tax_billing_account` the same way it
already queries `prop_unit` today, and render a "Billing account reflects N
sub-accounts" panel for the real 1,696+ affected parcels. Not designed here.

### 1.6 Alternatives rejected, with reasons (same discipline as §3.8)

- **Re-key `tax_billing`/`tax_billing_entity` themselves to full-account
  grain**, making the account layer primary instead of derived. Rejected:
  breaks all 9 real app.py call sites' current assumption that these tables
  are `geo_id`-grained (every one of them would need a `GROUP BY geo_id`
  added, at query time, on every page load) — strictly worse than computing
  it once, at write time, in one rollup module. Also breaks
  `tax_billing_quarantine`'s existing `(county_code, geo_id, tax_year)` shape
  and `quarantine_contamination.py`'s move/restore logic, which would need a
  parallel re-key of its own for no real benefit.
- **Aggregate in Python inside each loader, no unit tables** — the exact
  alternative §3.8 already rejected for the appraisal side, for the identical
  reasons: loses account-level provenance (can't build the optional
  multi-sub-account panel, can't verify the rollup's sums against source),
  and is the precise copy-drift pattern already caught happening in
  `load_pir_billing_2021_full.py` vs. `load_tax_current.py` (§1.1) — one
  sums, one doesn't, because each loader reimplements this independently
  today. A second, avoidable instance of the same mistake.
- **`ON CONFLICT ... DO UPDATE SET total_tax = tax_billing.total_tax +
  EXCLUDED.total_tax`-style additive upsert directly against `tax_billing`,
  no unit layer at all.** Rejected for the same non-idempotency reason §3.8
  gives: re-running a loader (a real, existing operational need — `--dry-run`
  and `--new-only` both exist specifically because reruns happen) would
  double-count every account, silently, forever.

---

## 2. Part 2 — Native-partitioning decision for `tax_billing_entity`, specifically

**Recommendation: still lightweight — no native `PARTITION BY`. But the
20-30M-row trigger projection this recommendation rests on needs to be
re-measured post-re-key, not carried forward from today's number.**

The real, current baseline (`SPEC_COUNTY_PARTITIONING.md` §1.3, DALLAS-GATE-5
measurement): 10,770,184 rows, 1494 MB, projecting to ~27-28M rows / ~3.9-4GB
at Dallas onboarding — inside §4.1's 20-30M-row revisit band, which is why
this table's own decision needed individual reasoning rather than inheriting
the schema-wide lightweight default by omission (exactly what this brief
asked for).

**The real complication this design surfaces, not previously visible:**
10,770,184 is the row count of a table §1's own measurement (M0-EXTENSION-1)
confirms is losing real rows to collision — 106,297 real overwrite events
in 2025 alone, each one a `tax_billing_entity` row that existed transiently
during a load and was overwritten before ever being counted in the live
10,770,184 figure. That number is not a stable population to project 3-4x
forward for Dallas/Harris; it's an undercount of the *current* real Travis
population, before Dallas exists at all. Once `tax_billing_account_entity`
exists and is populated (§1), the honest row count for entity-grain billing
data becomes **`COUNT(*) FROM tax_billing_account_entity`**, a number this
design does not have and does not invent — the real M0-EXTENSION-1 numbers
(14,989 real collision pairs, 3,148 of 3,384 prefix groups affected) describe
*lost events*, not total population, so they cannot be arithmetically
converted into a corrected row count without a real measurement. **Not
decided here, flagged as the concrete first step of the actual
implementation work**: run a real distinct-`account_id`-per-entity count
against the retained source files (the same files M0/M0-EXTENSION-1 already
scanned) before finalizing either the migration's storage-sizing estimate or
re-confirming this section's own lightweight-vs-native verdict against the
corrected number.

**Reasoning for the lightweight recommendation holding regardless of the
exact corrected count:** even a real, honest 1.5-2x undercount correction
(a plausible range given `tax_billing`'s own $5.79M-vs-$170M 29x dollar-loss
ratio reflects compounding across shared entities per account, not a
1:1 row-count multiplier) keeps `tax_billing_account_entity` in the tens of
millions of rows at Dallas onboarding, not the hundreds of millions —
`SPEC_COUNTY_PARTITIONING.md` §4.1's own reasoning (index range scans matching
partition pruning's real benefit only once a single partition's data stops
fitting comfortably in cache, and once there's a real recurring need to
bulk-drop a whole partition) still applies at that scale. No new county is
being retired from this platform, so condition (b) doesn't apply either.
**Recommend the same explicit numeric trigger discipline §4.1 already
established, applied fresh**: revisit this specific table's own
native-partitioning decision again at Harris onboarding, using Harris's own
real measured data once it exists rather than the ~39.5M-row floor-not-ceiling
extrapolation `HARRIS-ONBOARD-1` already flagged as uncertain — not before.

---

## 3. Part 3 — Transitional-index retirement/redesign: real correction to the brief's own premise

`SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md` §2c framed this as "what happens
to `idx_billing_geo`/`idx_tbe_entity_code` as part of this same migration" —
implying the re-key migration touches them directly. **Direct inspection of
both indexes (`schema.sql`) shows this isn't the case, and this document
corrects it rather than silently complying with a premise the code doesn't
support:**

- `idx_billing_geo ON tax_billing(geo_id)` and
  `idx_tbe_entity_code ON tax_billing_entity(entity_code)` are both indexes
  **on the derived rollup tables**, `tax_billing`/`tax_billing_entity`
  themselves — not on any table this re-key creates or restructures. Per §1,
  `tax_billing`/`tax_billing_entity`'s own key shape
  (`(county_code, geo_id, tax_year)` / `(county_code, geo_id, tax_year,
  entity_code)`) is **completely unchanged** by this design — they stay
  exactly as `migrate_county_partitioning.py` already left them, just written
  by a rollup instead of loaders directly.
- Both indexes' retirement is therefore governed entirely by the **existing**
  rule already written down for them (`SPEC_COUNTY_PARTITIONING.md` §10.3):
  drop only once `verify_index_coverage.py --index-source live` reports zero
  real call sites still relying on the bare `geo_id`-only /
  `entity_code`-only access path. This condition is **orthogonal to this
  re-key migration** — it depends on real call-site coverage across all of
  `app.py`/`loaders`, most of which this re-key doesn't touch, not on
  whether a unit layer exists underneath `tax_billing`/`tax_billing_entity`.
  **This document does not change either index's retirement condition or
  timeline.**

What §1's new tables *do* need is their own, fresh indexing — not
"transitional" in §10.3's sense at all, since there's no prior single-column
key shape to be backward-compatible with (these tables don't exist yet):
`idx_tba_geo_year`/`idx_tbae_geo_year` (§1.2), built `county_code`-leading
from the start, matching every other table this schema has added since the
partitioning incident (`group_stats`, `snapshot_*`). No transitional/final
split is needed for a table born after the lesson that incident taught.

---

## 4. Sequencing vs. the resolver seam — investigated, not assumed

**Real, current state of both pieces of work, checked directly:**

- `parcel_resolver.py`'s `resolve_parcel()` — the actual resolver-seam
  function — queries **only** `parcel`: `SELECT * FROM parcel WHERE
  county_code = %s AND geo_id = %s`. It does not touch `tax_billing` or
  `tax_billing_entity` at all, by design (its own docstring scopes it to the
  `parcel` lookup specifically).
- `resolve_parcel()` is **not called anywhere in app.py today** (confirmed
  via grep — the only real match is a comment referencing it). Per its own
  docstring, this is deliberate: "Nothing in app.py calls this function yet
  ... PARTITION-2-IMPLEMENT's brief is explicit: build `resolve_parcel()` for
  real, tested, ready to be adopted later — do NOT wire up app.py's 218 real
  call sites in this brief." That wiring is still deferred to Diego's
  separate, still-undecided §7 routing/UI question.
- `tax_billing`/`tax_billing_entity`'s **own** 9 real app.py call sites
  already carry their own, separate, already-completed `county_code`
  scoping — `DALLAS-GATE-3`/`DALLAS-GATE-4` inline-fixed these directly
  (confirmed via reading `load_tax_current.py`'s `billing_sql`/`entity_sql`
  and `app.py`'s own read queries, all already targeting the real
  `(county_code, geo_id, tax_year[, entity_code])` composite key). This work
  is **complete and independent of `resolve_parcel()`** — it was never
  routed through the resolver seam in the first place.

**Real conclusion: this re-key does not need to wait for the resolver seam.**
The seam's own scope doesn't reach these two tables' call sites at all, and
those call sites' `county_code`-scoping is already done, separately, by
already-completed work. Treating "resolver seam fully wired" as a
prerequisite for this re-key would be blocking on a piece of work that,
investigated directly, turns out not to gate it.

**What actually *does* matter for timing, correctly identified:** the
separate, already-formal **Dallas hard gate**
(`SPEC_COUNTY_PARTITIONING.md` §10.4) — `verify_index_coverage.py
--index-source live` must report zero CONFIRMED and zero UNCONFIRMED
coverage gaps before *any* real Dallas parcel data loads, full stop, covering
every county-keyed table, not just these two. As of that section's own
last-written status ("This gate is not yet passed as of this amendment"),
it was open; this design-only brief did not re-run that live audit (no
production access from this sandbox) and does not claim to know its current
pass/fail state — that's a real, separate, live check Diego or a future
brief needs to re-run, not assumed here either way. **The practical
sequencing recommendation stands regardless of that gate's exact current
status**, because `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md` §2's own
"one migration, not three" framing already establishes the right relative
order on independent grounds: land this re-key **before** Dallas rows exist
in `tax_billing`/`tax_billing_entity`, so Dallas data is written directly
into the new unit-grain shape from day one rather than needing its own
retrofit pass later. That recommendation doesn't depend on, and isn't
weakened by, the resolver seam's separate, unrelated timeline.

---

## 5. Real, honest uncertainty this document preserves, not resolves

- **The corrected `tax_billing_account_entity` row-count baseline (§2)** —
  not measured here; a real pre-migration count against retained source
  files is the concrete next step before this design's own native-partitioning
  verdict can be treated as final rather than provisional.
- **Whether `scrape_billing_history.py`'s portal-scrape write path carries an
  analogous, separately-caused undercount risk (§1.1)** — genuinely unknown
  from source-code inspection alone; needs a real, live check of the county
  portal's own per-`geo_id` receipt content for a known multi-sub-account
  parcel.
- **Whether a sub-account's parent `geo_id` assignment can change across tax
  years** (the replat-safety question `prop_unit_tax_year` already carries a
  real answer for on the appraisal side) — no evidence found either way for
  billing accounts specifically; `tax_billing_account`'s schema (§1.2)
  defensively denormalizes `geo_id` per-year the same way `prop_unit_tax_year`
  does, but this hasn't been confirmed as a real, observed phenomenon for
  billing data the way it has for appraisal parcels.
- **Whether the rollup should absorb `load_tax_current.py`'s existing
  0.00-vs-derived-from-entity-sum fallback logic (§1.3)** — a real
  simplification opportunity, not designed in full here.
- **The Dallas-onboarding (~27-28M rows) and Harris-alone (~39.5M-row floor)
  projections** — both remain ratio extrapolations from Travis's own real
  measured density, not real measurements of either county's actual data,
  per `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md` §3's own preserved
  uncertainty, unchanged by this document.
- **Attorney-fee and penalty/interest columns** (`TXATTFEE`, `TXPENINT` in
  the 2021 PIR export) — `load_pir_billing_2021_full.py`'s own investigation
  already flagged these as present in source but with no home in
  `tax_billing_entity`'s current schema. `tax_billing_account_entity` (§1.2)
  doesn't add columns for them either — a related but genuinely separate
  schema-completeness question, not decided here.

---

## 6. What this document does NOT do

- Does not create any table, run any migration, or touch any live database.
- Does not finalize a row-count or storage-sizing estimate for the new unit
  tables — flags the real measurement needed instead of guessing.
- Does not resolve the `scrape_billing_history.py` portal-scrape undercount
  question (§1.1, §5) — flags it as a real, separate, live-verification need.
- Does not re-run or claim to know the current pass/fail state of the Dallas
  hard gate (`SPEC_COUNTY_PARTITIONING.md` §10.4).
- Does not decide the `load_tax_current.py`/`tax_billing_rollup.py` write-
  ordering question against `scrape_billing_history.py`'s sentinel/portal
  rows (§1.4) — flagged as a real open question for the implementation brief.
- Does not set a date or sequence this ahead of or behind other open Dallas
  prerequisites — Diego's call, same boundary every prior design-only brief
  in this codebase has stated.

**Real, required next step, per this brief's own instruction:** this
document goes to Fable for architectural review before any implementation
brief is written or any code is touched.
