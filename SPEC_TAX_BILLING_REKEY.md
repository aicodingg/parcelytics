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

---

## 7. REAL CORRECTION (2026-08-17, Fable's architectural review + a resolved
live portal check, TAX-BILLING-REKEY-2)

Same pattern as `SPEC_COUNTY_PARTITIONING.md` §9/§10: sections 0-6 above are
left unedited — this section records what Fable's review found and what
changed as a result, not a silent rewrite of the original design. Fable's
review covered five real questions (Q1-Q5); several are corrections to the
original design, not confirmations, and are recorded as such below. One
gating question — whether the county portal exposes real sub-account numbers
— was resolved live, this session, against production-reachable
infrastructure, **not from this sandbox** (this sandbox has no live network
access to the county portal; this is the same disclosed limitation §1.1/§5
already carried). The result is reported here as a given, real fact per
Diego's own explicit instruction, not independently re-derived or
re-verified by this document.

### 7.1 Q1 — key sizing/scoping correction

**Real, confirmed correction, not a refinement:** `account_id` becomes
`VARCHAR(20)` (not `VARCHAR(14)`) — Dallas's own real tax accounts run 17
characters (confirmed via direct DCAD investigation, per Fable's review),
so a Travis-derived 14-character assumption baked into the column's own type
would either truncate or reject real Dallas data the moment it loads. Widened
to match `geo_id`'s own existing `VARCHAR(20)` convention rather than picking
a new, narrower bound that would just need widening again at the next county.

**`county_code` joins both new tables' primary keys from birth, not added
later:**

```sql
CREATE TABLE IF NOT EXISTS tax_billing_account (
    county_code     VARCHAR(20)  NOT NULL DEFAULT 'TRAVIS',
    account_id      VARCHAR(20)  NOT NULL,      -- full source-native account number
    tax_year        SMALLINT     NOT NULL,
    geo_id          VARCHAR(20)  NOT NULL,       -- derived, see below -- never stored as an assumption
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
    county_code  VARCHAR(20) NOT NULL DEFAULT 'TRAVIS',
    account_id   VARCHAR(20) NOT NULL,
    tax_year     SMALLINT    NOT NULL,
    geo_id       VARCHAR(20) NOT NULL,
    entity_code  VARCHAR(10) NOT NULL,
    amount_due   NUMERIC(14,2),
    amount_paid  NUMERIC(14,2),
    PRIMARY KEY (county_code, account_id, tax_year, entity_code)
);
CREATE INDEX IF NOT EXISTS idx_tbae_geo_year ON tax_billing_account_entity(county_code, geo_id, tax_year);
```

(Supersedes §1.2's original `CREATE TABLE` blocks — those used `VARCHAR(14)`
and did not lead with `county_code`; both corrected here per Fable's review,
not left as a second, conflicting schema for a future reader to reconcile.)

**The 10-char-prefix→`geo_id` relationship becomes derived logic in the
rollup, never a stored assumption on the unit rows.** §1.2's original schema
comment described `geo_id` as "`account_id[:10]`, denormalized per-year" —
correct for Travis, where the tax office's own convention genuinely is a
10-character account prefix plus a 4-digit sub-account suffix, but **not
proven as a cross-county rule**. Per Fable's own finding, prefix semantics
are Travis-specific until proven otherwise per county — Dallas's 17-character
accounts are not confirmed to follow the same "first N characters = parent
`geo_id`" structure at all. Real, corrected design: each loader computes
`geo_id` using **its own county's real, confirmed mapping rule** (Travis:
`account_id[:10]`; Dallas/Harris: whatever their own real account-numbering
convention turns out to be, once real source data exists to confirm it) and
writes the *result* into the `geo_id` column — the column stores a fact
established at write time by county-specific logic, not a hardcoded
`SUBSTRING` assumption baked into the rollup's own SQL. `tax_billing_rollup.py`
groups by the stored `geo_id` column; it does not itself compute prefixes.
**Not decided here:** Dallas's/Harris's own real account→`geo_id` mapping
rule — that needs real source data to confirm, the same way Travis's own
14-digit/10-char relationship needed `load_pir_billing_2021_full.py`'s
full-file scan to confirm rather than assume (§1.1).

**Correction to the document's own framing, not just the schema:** §1.2
originally described the lack of a synthetic ID as a "deliberate departure
from the `prop_unit` precedent." Per Fable's review, this was imprecise
framing, not a real design disagreement — `prop_unit` also uses a natural
key (`prop_id`), not an invented one; TCAD assigns `prop_id`, it doesn't
originate in this codebase. The real, shared principle both designs follow
is **"store the source's true grain under the source's own native key,"**
which `prop_id` and `account_id` both satisfy identically. The original
document's "departure" language is retired in favor of this framing;
`account_id` is not an exception to the `prop_unit` precedent, it's a second,
consistent application of the exact same principle.

### 7.2 Q2 — partitioning correction: born partitioned, real overrule of §2's own verdict

**Real overrule, not a refinement of §2's reasoning.** §2 recommended
deferring native partitioning for `tax_billing_entity`, revisiting "at Harris
onboarding." Fable's review overrules this **specifically for the two new
unit-grain tables** (`tax_billing_account`, `tax_billing_account_entity`),
on real, concrete grounds §2 didn't weigh: partitioning an *empty* table
costs nothing — no data to migrate, no lock contention, no downtime — while
re-partitioning a table that has since grown to real, populated multi-million-
row scale is the exact second-rebuild cost `SPEC_TAX_BILLING_COLLISION_AND_PARTITION.md`'s
own "one migration, not three" rule exists to prevent. Deferring
partitioning on a table born *during* this same migration would recreate the
identical problem this whole promoted brief was written to close — just one
layer down, on the new tables instead of the old ones.

**Real, corrected design:** `tax_billing_account` and
`tax_billing_account_entity` are born **natively partitioned by
`county_code`** (`PARTITION BY LIST (county_code)`, with a `TRAVIS` partition
created at migration time and a documented procedure for adding a `DALLAS`/
`HARRIS` partition at each county's own onboarding). **Fable's own stated
minimum acceptable fallback, recorded here in case native partitioning proves
impractical at actual implementation time for a reason this design-only
document can't anticipate:** at minimum, `county_code` leading the PK (as
§7.1 already establishes) plus a **written one-way partitioning plan** — a
concrete, dated commitment for exactly when and how these two tables convert
to native partitioning, not an open-ended "someday." This is intentionally a
stricter bar than §4.1's schema-wide lightweight-by-default recommendation;
it does not overrule §4.1 for the rest of the schema, only for these two
tables, born at the moment this exact lesson was learned.

**The legacy derived tables — `tax_billing`, `tax_billing_entity` — stay
unpartitioned, unchanged from §2/§3's own reasoning.** They're rebuildable by
construction (the whole point of the derived-rollup design, §1.5): if a
future decision partitions them too, the rollup can simply be re-run against
a repartitioned target, at negligible marginal cost over the unit-layer
migration itself. Nothing about §7.1/§7.2's corrections changes that.

**§2's own row-count uncertainty is reframed, not resolved by fiat:** the
real corrected `tax_billing_account_entity` population still isn't measured
by this document (§5's own preserved uncertainty stands) — what changes is
that this document no longer treats that unmeasured number as a **precondition
for a partitioning verdict**. The verdict (native partitioning, born now) is
decided on the empty-table-costs-nothing/re-migration-costs-everything
reasoning above, which holds regardless of the exact row count. The real
pre-migration count remains valuable and still-needed — not as an input this
decision depends on, but as **the number the billing conservation gate's own
G3-equivalent check (§7.5) will be calibrated against**, a concrete, load-
bearing use for that measurement this document didn't have before.

### 7.3 Q3 — the portal check, resolved; design (a) chosen; the real, complete writer closed set

**The live check, reported here as a given fact per Diego's explicit
instruction — not reproducible from this sandbox (no live network access to
the county portal, same disclosed limitation as §1.1/§5):**

```python
from loaders.scrape_billing_history import fetch_html
html, status = fetch_html('0259410216', timeout=15)
# real status: 0 (HTTP_OK), real length: 23727
# only real 14-digit number found anywhere on the page: '02594102160000'
# — exactly the synthetic geo_id+"0000" account used to REQUEST the page
```

Run against `0259410216` — the real, largest known collision group (1,210
sub-accounts) — deliberately the highest-leverage real test case available:
if any parcel's portal page were going to reveal distinct sub-account
numbers, the one with 1,210 of them is where it would show. It didn't.
**Real, definitive result: the portal does not expose distinct, real
sub-account numbers anywhere in the page it returns for a given `geo_id`.**
This resolves §1.1's own flagged open question and §5's corresponding bullet:
the portal-scrape write path (`scrape_billing_history.py`) has no
account-number field available in its own source **at all**, confirmed live,
not just absent from what this sandbox could inspect statically.

**Per Fable's own stated logic, this resolves Q3 in favor of design (a):
`scrape_billing_history.py`'s write path gets its own real, separate table**
— not tag-based cohabitation inside `tax_billing` alongside the rollup's own
writes, which §1.4's original design left as an open ordering question
between the rollup and a portal-scrape sentinel row. That question is now
retired, not answered — it doesn't arise under design (a), because the two
write paths no longer share a table at all.

```sql
-- Portal-scrape receipts: geo_id-native grain (the portal has no finer
-- identity to offer -- confirmed live, not assumed). Kept structurally
-- separate from tax_billing_account's real account-grain data so the two
-- sources' own real, different grains are never silently conflated in one
-- shared table again.
CREATE TABLE IF NOT EXISTS tax_billing_portal_scrape (
    county_code   VARCHAR(20)  NOT NULL DEFAULT 'TRAVIS',
    geo_id        VARCHAR(20)  NOT NULL,
    tax_year      SMALLINT     NOT NULL,
    total_paid    NUMERIC(14,2),
    data_source   VARCHAR(32)  NOT NULL DEFAULT 'portal_scrape',
    confidence_level VARCHAR(16),
    scraped_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (county_code, geo_id, tax_year)
);
```

Table name is Cowork's own call per the brief — `tax_billing_portal_scrape`
chosen to name the real, distinct source explicitly, matching this
codebase's existing convention of naming tables after what they structurally
are (`tax_billing_quarantine`, `parcel_2026_preliminary_snapshot`), not a
generic name that would invite a future reader to wonder whether it's
interchangeable with `tax_billing_account`.

**Real, explicit read-layer requirement this design didn't need before:**
`tax_billing_rollup.py` must now also read `tax_billing_portal_scrape` and
explicitly prefer/union it against `tax_billing_account`-derived rows by
source, rather than the rollup only ever reading one unit-grain source. Not
designed in full here (the actual preference logic — e.g., does a real
`taxcur_current` account-grain row always outrank a `portal_scrape` row for
the same `(geo_id, tax_year)`, or does `data_source`'s existing
verified/derived/portal_scrape hierarchy already answer this) is real,
concrete work for the implementation brief, not resolved by this amendment —
flagged rather than guessed at.

**Real, mandatory addition per Fable's own instruction: the complete, closed
set of `tax_billing`-family writers, enumerated directly from source, not
from memory of what §1.1/§1.4 already named.** A fresh, repo-wide grep for
every real `INSERT`/`UPDATE`/`DELETE` against `tax_billing`/
`tax_billing_entity` (excluding test files and one-off validation scripts —
`test_dallas_gate_4_county_code.py`, `test_migrate_county_partitioning.py`,
`test_upsert_billing_rows_commit.py`, `loaders/test_pir_loaders.py`,
`validate_coverage_sql.py`, all of which touch only synthetic `TEST_`/fake-
year rows, never real data) finds **eight** real production writers, not the
four §1.1 named:

| # | File | Table(s) | `county_code`-aware? | Notes |
|---|---|---|---|---|
| 1 | `loaders/load_tax_current.py` | `tax_billing`, `tax_billing_entity` | Yes (DALLAS-GATE-4) | 2025 current-year, primary writer |
| 2 | `loaders/load_pir_billing.py` | `tax_billing`, `tax_billing_entity` | Yes (DALLAS-GATE-4) | Same CSV shape, other vintages |
| 3 | `loaders/load_pir_billing_2021_full.py` | `tax_billing`, `tax_billing_entity` | Yes (DALLAS-GATE-4) | Own `BILLING_SQL`/`ENTITY_SQL`, not shared with #5 |
| 4 | `loaders/scrape_billing_history.py` (`upsert_billing_rows()`) | `tax_billing` | Yes (DALLAS-GATE-2) | Called by its own CLI **and** live by `api_billing()` (app.py) — same function, two callers |
| 5 | `app.py` `api_billing()` — direct sentinel INSERT | `tax_billing` | Yes | Separate from #4's call to the same function; both live in the same route |
| 6 | `loaders/backfill_tax_billing_2025_confidence.py` | `tax_billing` | N/A (`UPDATE` only) | Tags `data_source`/`confidence_level` on existing rows; no `INSERT`, no grain/collision exposure |
| 7 | `loaders/quarantine_contamination.py` | `tax_billing` | Yes | `DELETE` (quarantine) + `INSERT` (restore), both real writers of the table |
| 8 | **`loaders/pir_xlsx_common.py`** (`BILLING_SQL`/`ENTITY_SQL`, via `run_cli()`) | `tax_billing`, `tax_billing_entity` | **No — real, live, unpatched bug** | See below |

**Real, urgent finding this enumeration surfaced, separate from but directly
relevant to this design:** `loaders/pir_xlsx_common.py` — the shared module
backing `load_pir_billing_2022.py`/`_2023.py`/`_2024.py` (confirmed via
import grep: all three call `pir_xlsx_common.run_cli()`) — still writes with
the **pre-migration** `ON CONFLICT (geo_id, tax_year)` /
`(geo_id, tax_year, entity_code)` targets and **no `county_code` column in
either INSERT at all**. This is the identical failure mode DALLAS-GATE-2/3/4
already found and fixed in `scrape_billing_history.py`, `load_tax_current.py`,
and `load_pir_billing.py` — "there is no unique or exclusion constraint
matching the ON CONFLICT specification" against the real, live, already-
migrated schema — but `pir_xlsx_common.py` was never in scope for those
briefs and was missed. **Real, honest disclosure: this means
`load_pir_billing_2022.py`/`_2023.py`/`_2024.py` are live-broken against
production today**, the same way the other three loaders were before their
own DALLAS-GATE fixes, for as long as this has gone unaudited. **Out of
scope for this amendment to fix** — this is a design document, not a hotfix
brief — but too urgent and too directly on-point (it's the exact class of
bug Q3's own investigation was already looking for) to leave undisclosed
until the actual re-key implementation brief. **Real, recommended next
step, separate from TAX-BILLING-REKEY's own sequence:** a short, standalone
hotfix brief mirroring DALLAS-GATE-3/4's own pattern, for
`pir_xlsx_common.py` specifically, before any 2022/2023/2024 PIR reload is
attempted.

**AC5-style grep-test definition, real and concrete, to replace §1.4's own
looser "mirrors `verify_parcel_filters_canonical.py`'s pattern" description:**
a regression test asserting that the file set matching
`grep -rl "INSERT INTO tax_billing\b\|UPDATE tax_billing\b\|DELETE FROM tax_billing\b\|INSERT INTO tax_billing_entity\b\|UPDATE tax_billing_entity\b"`
across the whole repo (app.py + `loaders/*.py`, explicitly excluding
`test_*.py` and `validate_*.py`) equals exactly the 8-file closed set above
— named explicitly in the test, not inferred — so a future new writer
(the `pir_xlsx_common.py` class of gap) cannot be added without the test
failing until it's deliberately reviewed and added to the named set.

### 7.4 Q4 — sequencing correction: this migration is now on the Dallas critical path

**Real correction, not a restatement of §4's own conclusion.** §4 argued
this re-key "should still land before Dallas rows exist," as a
recommendation. Per §7.2's own partitioning fold-in, that recommendation
is upgraded to a **formal prerequisite**: because the new unit tables are
now born natively partitioned by `county_code` (§7.2), and because Dallas
onboarding is precisely the event that would create that table's first
non-`TRAVIS` partition, this migration cannot be deferred past Dallas
onboarding without either (a) Dallas data landing in a not-yet-repartitioned
`tax_billing_account`/`_entity`, recreating the exact "migrate the same
growing table twice" cost this whole promoted brief exists to prevent, or
(b) Dallas onboarding itself blocking on this re-key anyway, just later and
under more time pressure. **This document now states directly: TAX-BILLING-
REKEY is a Dallas onboarding prerequisite**, alongside `DATA_LIFECYCLE.md`'s
own named Source Registry/County Profile/Classification Map prerequisites
and the separate Dallas hard gate (`SPEC_COUNTY_PARTITIONING.md` §10.4) —
not merely "recommended to land first."

**Real, additional reason, per Fable's own review, not previously in this
document:** this re-key changes the real baseline numbers the Dallas gate's
own conservation checks (and the new billing conservation gate, §7.5) will
be calibrated against — the $5,794,968.90/$170,061,400.28 recovered-dollar
figures, the corrected `tax_billing_account_entity` row count (§7.2), the
account-grain population itself. Landing this re-key **after** Dallas data
already loaded would mean re-baselining every one of those checks twice —
once against Travis-only data, once again after Dallas's own rows change
the totals — a real, avoidable duplicate-work cost on top of the storage/
migration duplication §7.2 already named. Both reasons point the same
direction independently; recorded together because either alone would be
sufficient grounds, but Fable's review surfaced the calibration one this
document didn't have.

**§4's own resolver-seam finding is unchanged by this correction** — the
seam still doesn't touch these tables, still isn't wired into app.py, and
this migration still doesn't depend on it. What changed is *only* the
strength of the Dallas-timing conclusion (recommendation → prerequisite),
not the resolver-seam reasoning that sits alongside it.

### 7.5 Q5 — four real, mandatory additions

**1. A billing conservation gate, mirroring `loaders/ingest_gate.py`'s
G1-G6 pattern, ships inside this same migration — not optional, not
deferred to a later brief.** Real, proposed check set, named `BG1`-`BG4` to
avoid colliding with the existing appraisal-side `G1`-`G6` numbering:

- **BG1 — source ledger conservation** (mirrors G1): every line of the
  source billing file (`TaxCurOpenData`, PIR exports) is classified into
  exactly one bucket (accepted / skipped-no-account / skipped-wrong-year /
  ...); bucket counts must sum to the file's real total line count.
- **BG2 — account coverage** (mirrors G2/G5): the count of distinct real
  `account_id`s the source file scan says should exist for a given
  `(county_code, tax_year)` must exactly equal the count that landed a
  `tax_billing_account` row for that same scope.
- **BG3 — dollar conservation** (mirrors G3, the real conservation check
  Diego's brief specifically calls for): `SUM(TOTAL_TAX)` computed directly
  from the source file must exactly equal `SUM(total_tax)` in
  `tax_billing_account`, which must exactly equal `SUM(total_tax)` in the
  rolled-up `tax_billing` — zero tolerance, same standard G3 already holds
  the appraisal side to.
- **BG4 — rollup integrity** (mirrors G4): independently re-derives
  `tax_billing`'s `SUM()`/`COUNT()` from `tax_billing_account` rows itself,
  rather than trusting `tax_billing_rollup.py`'s own output — the same
  "the gate doesn't trust the module it's checking" discipline G4 already
  established.
- **Skip ledger, `ingest_audit` rows, exit-1 on failure**: same shape as
  the existing gate — every skipped/rejected source row gets a real,
  attributed reason (not silently dropped), every gate run writes a real
  `ingest_audit` row, and `compute_metrics`-equivalent downstream steps
  (here: nothing currently reads `tax_billing`/`tax_billing_entity` as a
  *gate*, but the same "don't proceed past a failed gate" discipline
  applies to whatever loader orchestration calls the rollup) refuse to run
  past a failed gate.
- **Deliberate-corruption fixture tests**, mirroring
  `loaders/test_ingest_gate.py`'s own two-deliberate-corruption-case
  pattern: a fixture that reproduces the exact last-write-wins loss
  mechanism M0/M0-EXTENSION-1 measured (two accounts sharing a `geo_id`
  prefix, second one overwriting the first under the *old* `(geo_id,
  tax_year)`-keyed write path) must make BG2/BG3 fail loudly under the old
  behavior and pass cleanly under the new unit-layer write path — proving
  the gate would have caught the real, already-measured $170M/$5.8M loss,
  not just asserting it does in prose.

**Real, pre-committed, falsifiable expectation, per Diego's own instruction
— written into the design now, not left to be discovered at implementation
time:** once the unit layer is backfilled from the same retained source
files M0/M0-EXTENSION-1 already scanned, the gate's own re-derived
recovered-dollar figures (the real difference between what the old
last-write-wins pipeline would have kept vs. what the new unit-layer
pipeline actually captures) **must equal $5,794,968.90 (`tax_billing`) and
$170,061,400.28 (`tax_billing_entity`) exactly** — the same real,
already-measured M0/M0-EXTENSION-1 figures this entire document has treated
as fixed inputs throughout. This is a real, measured post-condition the
implementation brief must report against, not an assumption this design
gets to claim credit for in advance.

**2. Rule 3 machinery engages — a real, named PM/Marketing Director
follow-through item.** Per `BUILD_WORKFLOW.md`'s Rule 3 ("public numbers
come from a verified reference, not a live page") and `DATA_LIFECYCLE.md`
§Stage 5's Published Metrics Log (the only county-level figures marketing,
press, or Diego may cite externally), any public, published billing-derived
figure built on the pre-fix pipeline — the effective-tax-rate metric
specifically named in `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §3.7's own
original rationale for measuring this in the first place, plus any Case
File/Quick Fact number that cites a billing total — is now, by construction,
computed from data this migration will have materially changed. **Real,
concrete follow-through, named now rather than rediscovered post-migration:**
after this re-key lands and the billing conservation gate (above) passes, a
Rule 3 verified-stats refresh must run before any previously-published
billing-derived figure is cited again, and the Published Metrics Log gains
a superseding entry per `DATA_LIFECYCLE.md` §4's own "a sealed vintage is
never edited — it is superseded" rule, with a dated changelog note if any
externally-cited figure moves beyond rounding. This is a real PM/Marketing
Director coordination item, not an engineering one — named here so it
doesn't get rediscovered in a panic after the migration ships.

**3. Hosting math must include this migration.** The real, new unit
tables' projected footprint — **~11M rows across `tax_billing_account` and
`tax_billing_account_entity` combined, plus their indexes**, per Diego's own
given figure for this fold-in — needs to be added to the same
`pg_total_relation_size` capacity snapshot already requested as part of the
Harris scale review, so the Dallas tier-checkpoint decision is made against
the real, post-re-key baseline, not today's pre-re-key numbers. **Real,
honest flag, not silently smoothed over:** this document's own §7.2
preserves an unresolved question about whether `tax_billing_account_entity`'s
true post-correction population significantly exceeds today's collision-
lossy `tax_billing_entity` count (§2's "1.5-2x" range would put it at
16-21M rows on its own, above the ~11M-rows-combined figure given for this
fold-in). Both numbers are recorded here rather than one silently
overriding the other: **~11M is the figure to use for the immediate
capacity-snapshot fold-in as instructed**, but whoever runs that snapshot
should treat it as provisional pending BG2/BG3's own real, measured
post-backfill row count (above), the same way §2 already flags the broader
row-count question as real and open. No existing document in this repo
names a "`pg_total_relation_size` capacity snapshot" artifact yet — this is
the first place that request is written down in the codebase itself, not a
citation to a pre-existing section.

**4. This section itself is the record that the portal account-number
question (§1.1's own flagged uncertainty) is resolved.** Design (a) —
`scrape_billing_history.py`'s write path gets its own real, separate table
(§7.3) — is the chosen, real path forward. §1.1's original "not decided
here" language on this specific question is superseded by §7.3; every other
"not decided here" item in §1.1/§5 stands unchanged.

### 7.6 What this amendment does NOT do

- Does not create any table, run any migration, or touch any live database —
  same boundary as §6.
- Does not independently verify the live portal check (§7.3) — reported as
  a given fact per explicit instruction, not reproduced from this sandbox.
- Does not fix `loaders/pir_xlsx_common.py`'s real, live `county_code` bug
  (§7.3) — flagged for a separate, urgent hotfix brief, not fixed here.
- Does not design the `tax_billing_rollup.py` read-layer source-preference
  logic between `tax_billing_account` and `tax_billing_portal_scrape` in
  full (§7.3) — flagged as real, concrete implementation-brief work.
- Does not determine Dallas's or Harris's own real account→`geo_id` mapping
  rule (§7.1) — needs real source data, not assumed from Travis's own
  convention.
- Does not resolve which of the ~11M-vs-16-21M row-count figures (§7.5
  item 3) is closer to correct — both recorded, neither silently preferred.

**Real, required next step, unchanged and now doubly true:** per Fable's own
review, only after this amendment is complete does the actual implementation
brief get written — informed by this fully-corrected design, not the
original §0-§6 alone.
