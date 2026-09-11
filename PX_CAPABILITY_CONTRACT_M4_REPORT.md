# PX_CAPABILITY_CONTRACT_M4_REPORT

**Mission:** 4 — Capability Contract Measurement Layer
**Repo state at start:** `02e8e39` (confirmed against `parcelytics.md`'s own read, per the Mission 4 preamble's item 1)
**Access:** no database, Render, Notion, vault, or network access — confirmed, unchanged from every prior mission. Every Travis/Dallas number in this report is either (a) already on file elsewhere in this repo, cited `[file:line]` or `[source doc]`, or (b) explicitly marked `[PENDING — live execution]`. Nothing here claims a live measurement this mission performed itself.
**Commit/push:** none. Everything below is in the working tree only.

---

## Files changed

New files:
- `capability_registry.py` — D1, the canonical field taxonomy (10 fields registered).
- `loaders/field_coverage_gate.py` — D2/D4, the measurement layer + CLI runner.
- `capability_state.py` — D3, `COVERAGE_THRESHOLD`, `capability_state()`, `county_shows_field()`, `county_has_field()`.
- `capability_contract.py` — D5, minimal `CountyIdentity`/`FieldCapability`/`SourceRegistryEntry`/`CountyCapabilityContract` dataclasses.
- `render_contract.py` — D7, `should_render_field()` / `render_action()` / `is_publicly_exposed()`.
- `loaders/test_field_coverage_gate.py` — D9, 18 fixture tests covering all 12 required cases plus two tax_delinquent regression tests added in post-review fix (see "Post-review fix" below).
- `loaders/demonstrate_capability_m4.py` — D6, the Travis/Dallas proof (fixture-backed, every number cited to a real, on-file source).
- `PX_M4_LIVE_MEASUREMENT_COMMANDS.md` — the exact commands for Diego's real measurement pass (M2/M3 Step B pattern).
- `PX_CAPABILITY_CONTRACT_M4_REPORT.md` — this file.

Modified files:
- `schema.sql` — added `county_field_coverage` (D2's persisted measurement table; schema reused verbatim from `STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md` §A5.2's prior design, not redesigned).
- `app.py` — `county_has_field()` is now a thin wrapper over `capability_state.county_has_field()` (see "county_has_field() replacement" below). One new import (`capability_state`), one new lazy-loaded module-level cache (`_FIELD_COVERAGE_SNAPSHOT` / `_load_field_coverage_snapshot()`). No other line in `app.py` changed. Confirmed `py_compile`-clean; confirmed the full offline scanner suite (`run_offline_checks.py`, 6/6 REQUIRED-tier checks) still passes, including `verify_exemption_gating.py`'s 16/16 checks against the exact call sites this change touches.
- `parcelytics.md` — §6 rewritten from "target state — not yet built" to "shipped" with the capability→confidence sequence (D8); §7's implementation-status paragraph corrected to reflect the now-shipped `COVERAGE_THRESHOLD`; §17's priority queue updated to reflect Mission 4 having shipped the measurement layer itself.

---

## Schema/model introduced

```sql
CREATE TABLE IF NOT EXISTS county_field_coverage (
    county_code   VARCHAR(10)  NOT NULL,
    field         VARCHAR(40)  NOT NULL,
    tax_year      SMALLINT     NOT NULL DEFAULT 0,
    numerator     BIGINT       NOT NULL,
    denominator   BIGINT       NOT NULL,
    fraction      NUMERIC(6,5) NOT NULL,
    measured_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    loader        VARCHAR(60)  NOT NULL,
    PRIMARY KEY (county_code, field, tax_year)
);
```

Not yet run against production — Diego runs it as Step 0 of `PX_M4_LIVE_MEASUREMENT_COMMANDS.md`.

---

## Fields measured (D1's registry)

| Field | Class | Table | Population |
|---|---|---|---|
| situs_address | attribute | parcel | all county parcels |
| owner_name | attribute | parcel | all county parcels |
| legal_desc | attribute | parcel | all county parcels |
| state_cd1 | attribute | parcel | all county parcels |
| neighborhood_cd | attribute | parcel | all county parcels |
| classi_cd | class_conditional | parcel | `prop_type_cd = 'R'` |
| living_area_sqft | class_conditional | parcel | `prop_type_cd = 'R'` |
| gross_building_area_sqft | class_conditional | parcel | `prop_type_cd = 'R'` |
| year_built | class_conditional | parcel | `prop_type_cd = 'R'` |
| exemption_codes | sparse_by_nature | parcel_tax_year | structural gate + all-rows fraction |
| hs_cap_loss | sparse_by_nature | parcel_tax_year | structural gate + all-rows fraction |
| tax_delinquent | sparse_by_nature | tax_delinquent | whole-table presence, boolean-shaped (see "Post-review fix" below — not a delinquency-rate fraction) |

This is deliberately a small, brief-anchored set (every field is either one of the brief's own §2.1 class examples, or one of the two Dallas-passing-case fields the preamble supplied) — not the full parcel/parcel_tax_year column inventory. Extending it further is natural follow-on work, not required for Mission 4's stop condition.

## Denominator used per field class

- **Attribute:** literal all-parcels fraction, no predicate.
- **Class-conditional:** `prop_type_cd = 'R'` for all four registered class-conditional fields (classi_cd, living_area_sqft, gross_building_area_sqft, year_built) — real property only, since personal/mineral accounts never carry a use-code or structure square footage. This is a defensible, but not the only possible, population choice; refining it further (e.g. `imprv_value > 0`-scoped, as `STAGE_A_...md` §A5.4 sketches) is future work.
- **Sparse-by-nature:** the structural gate (source column present + mapped) is the *primary* capability signal, per this registry's own explicit design choice (see "PM ruling needed" items below) — the resulting fraction denominator is all rows for `(county, tax_year)` for exemption_codes/hs_cap_loss, not a narrower population-scoped denominator. §7's open question (population-scoped vs. all-rows) is deliberately left unresolved. `tax_delinquent` is the exception: it is measured as whole-table dataset *presence*, not a per-row fraction at all — see "Post-review fix" below for why.

## Threshold used

`capability_state.COVERAGE_THRESHOLD = 0.30` — one named constant, per `parcelytics.md` §7's ruling and the Mission 4 preamble's item 4. Not 0.50. `data_coverage.py`'s pre-existing `is_reliable(min_coverage=0.50)` is untouched, confirmed still dead code (never called outside its own definition), and is not a competing threshold.

---

## Travis results

Fixture-demonstrated (`loaders/demonstrate_capability_m4.py`), every number cited to an existing on-file source — **not** a live measurement performed this mission:

| Field | Coverage | State | Evidence |
|---|---|---|---|
| state_cd1 | 96.68% (500,439/517,614) | Available | `KNOWN_LIMITATIONS.md` state_cd1 prefix table |
| neighborhood_cd | 96.7% (500,439/517,614) | Available | `KNOWN_LIMITATIONS.md` neighborhood_cd section, measured June 2026 |
| classi_cd | n/a | **Unknown** | No real overall % is on file anywhere in this thread's evidence for Travis specifically — reported honestly as not-yet-measured rather than guessed. This is itself a genuine demonstration of the Unknown state. |
| hs_cap_loss, 2023 | 99.9% | Available | `data_coverage.py` `HS_CAP_LOSS_COVERAGE[2023]` |
| hs_cap_loss, 2025 | 0.0% | Unavailable | `data_coverage.py` `HS_CAP_LOSS_COVERAGE[2025]` — "structurally always-false," a known, by-design state, not a bug |
| exemption_codes, 2025 | 55.1% | Available | `data_coverage.py` `EXEMPTION_CODES_COVERAGE[2025]` |

`[PENDING — live execution]`: real, current, database-measured coverage for every field above (this table pre-dates Mission 4 and may have shifted since these citations' original measurement dates), plus classi_cd's real number (currently Unknown for lack of any on-file figure).

## Dallas results

| Field | Coverage | State | Evidence |
|---|---|---|---|
| state_cd1 | ~99.99% | Available | Mission 4 preamble item 3 |
| situs_address | ~99.99% | Available | Mission 4 preamble item 3 |
| neighborhood_cd | 0% | Unavailable | Mission 4 preamble item 3 + PX-20260901-03 Task 1 |
| classi_cd | 0% (0 of 769,536) | **Unavailable** | Mission 4 preamble item 3, live-measured 2026-09-02 — the brief's own required proof case, "not a bug to investigate" |
| year_built | 0% | Unavailable | Mission 4 preamble item 3 |
| exemption_codes, 2025 | 0% (0 of 703,446) | Unavailable | `KNOWN_LIMITATIONS.md`, PX-20260901-02 Task 2 |

`[PENDING — live execution]`: a fresh, current re-measurement of all six (the 2025/2026 parcel counts above may also have grown since their original citation dates — Dallas's classi_cd figure alone shows the count grew from 703,446/705,536 to 769,536 between PX-20260901-02 and the Mission 4 preamble, consistent with ongoing onboarding).

## Examples of Available / Partial / Unavailable / Unknown

- **Available:** Travis `state_cd1` (96.68% ≥ 30% threshold, no sanity-floor concern).
- **Partial:** not demonstrated with a real number this mission — no field in the current registry has a registered sanity-floor reference against real Travis/Dallas data (see "known limitations" below). Demonstrated with a synthetic, explicitly-labeled test-only reference in `loaders/test_field_coverage_gate.py`'s `test_sanity_floor_pass_and_fail()` (coverage above threshold, sanity floor fails against the synthetic reference → Partial). No real-world Partial example exists yet because `SANITY_FLOOR_REFERENCES` ships empty by design (D4: never fabricate an external validation threshold).
- **Unavailable:** Dallas `classi_cd` (0% — below threshold), Travis `hs_cap_loss` 2025 (0.0% — below threshold, by design).
- **Unknown:** Travis `classi_cd` (no measurement exists yet for this field/county — reported honestly, not guessed).

---

## Tests run

`python3 loaders/test_field_coverage_gate.py` — 18/18 PASS, covering all 12 D9-required cases (attribute denominator, class-conditional denominator, sparse-by-nature structural gate, sanity-floor pass, sanity-floor fail, unavailable field, unknown field, partial coverage, available coverage, zero-population edge case, missing source-column case, Travis/Dallas regression) plus two tests added in the post-review fix: `test_tax_delinquent_low_but_nonzero_rate_still_available` and `test_tax_delinquent_zero_rows_reads_unavailable` (see "Post-review fix" below).

`python3 loaders/demonstrate_capability_m4.py` — 8/8 D6 proof-point assertions PASS.

`python3 run_offline_checks.py` — REQUIRED tier, 6/6 PASS (unchanged by this mission's `app.py` edit — confirms no regression in county-scoping, shadow-swap, template-scoping, copy-denylist, exemption-gating, or param-safety audits).

`python3 -m py_compile app.py capability_registry.py capability_state.py capability_contract.py render_contract.py loaders/field_coverage_gate.py loaders/test_field_coverage_gate.py loaders/demonstrate_capability_m4.py` — clean.

No live-database test was run (no access). `loaders/field_coverage_gate.py`'s `main()`/`--live` path has never been executed by this agent.

---

## Post-review fix: tax_delinquent rate-vs-presence bug

**Caught in Diego's Mission 4 review, before commit — not self-discovered.**

**What was wrong:** `measure_sparse_by_nature_field()`'s original `tax_delinquent` branch computed `coverage_fraction = delinquent_rows / all_parcels` and let that raw delinquency *rate* drive `capability_state()`, the same way exemption_codes/hs_cap_loss's genuine per-row sparsity fractions do. But `tax_delinquent` isn't a per-row-sparse field on `parcel`/`parcel_tax_year` — it's its own table, and the correct capability question is *whether the dataset has been loaded for this county at all*, not what fraction of parcels happen to be delinquent.

**Why it mattered:** a real, low, *good* delinquency rate (e.g. 12 delinquent parcels out of hundreds of thousands) would compute a `coverage_fraction` far below `COVERAGE_THRESHOLD` (0.30) and read as `Unavailable` — telling users "we don't have delinquency data for this county" when the dataset was in fact fully present, just naturally sparse because most properties aren't delinquent. This is backwards: a thriving, current tax roll is not a data-availability problem, and the field would have systematically mis-reported Available counties as Unavailable the moment their real-world delinquency rate fell (as intended) below 30%.

**What changed:** restructured to a boolean-shaped measurement, per the two options offered in review. `population_count` is now fixed at `1`; `populated_count` is `1` if any delinquent row exists for the county, else `0`. `coverage_fraction` is therefore always exactly `1.0` (dataset present) or `0.0` (dataset absent) — never a rate — and reuses `capability_state()`'s existing threshold comparison with zero special-casing. Applied in `loaders/field_coverage_gate.py` (`measure_sparse_by_nature_field()`'s `tax_delinquent` branch, which also dropped the now-unnecessary second `SELECT COUNT(*) FROM parcel` query and its `NOT_MEASURABLE`-on-zero-all-parcels branch) and mirrored in `capability_registry.py`'s `tax_delinquent` `FieldDefinition.measurement_method`/`population_predicate` text.

Zero rows for a county (Dallas today) now reads `Unavailable` — chosen over `NOT_MEASURABLE`/Unknown because it's consistent with how every other field in the registry works (measured-but-below-threshold, not unmeasured), and because the field's own registry docstring already frames "zero rows in tax_delinquent" as "has this dataset been acquired/loaded," which maps onto a genuine, measured Unavailable rather than an unmeasured Unknown.

**Regression coverage added:** two new tests in `loaders/test_field_coverage_gate.py` prove both directions — `test_tax_delinquent_low_but_nonzero_rate_still_available` (12 delinquent rows → `MEASURED`, `coverage_fraction == 1.0`, state `AVAILABLE` — the exact regression this fix prevents) and `test_tax_delinquent_zero_rows_reads_unavailable` (0 rows, Dallas fixture → `coverage_fraction == 0.0`, state `UNAVAILABLE`). Full suite re-run after the fix: 18/18 PASS. `loaders/demonstrate_capability_m4.py` doesn't exercise `tax_delinquent` and was unaffected; re-run post-fix, still 8/8 PASS. `run_offline_checks.py` re-run post-fix, still 6/6 PASS — no regression elsewhere.

---

## county_has_field() replacement (D3, preamble item 6)

**Chosen approach: thin wrapper**, not a new function call sites migrate to. `county_has_field(county_code, field)` now delegates to `capability_state.county_has_field()`, which reads a lazily-cached snapshot of `county_field_coverage` (populated the first time it's called in a given process) and falls back to the exact same hand-declared `COUNTY_PROFILES[...]["field_coverage"]` boolean it always has, for any `(county, field)` pair not yet measured.

**Why this, not the "new `county_shows_field()`, migrate over time" option:** only six real call sites exist (`app.py:6439`, `app.py:6940`, and four template `{% if county_has_field(...) %}` occurrences across `compare.html`/`property.html`/`search.html`), all gating on `exemption_codes`, all in boolean context, all already covered by `verify_exemption_gating.py`'s recurrence guard. A wrapper upgrades every one of them automatically and safely the moment Diego's live run populates `county_field_coverage`, with zero template changes and zero migration risk — directly consistent with D7's explicit "do not perform a broad property-page rewrite." The richer four-state API (`capability_state.county_capability_state()`, used by `render_contract.py`) exists alongside it for future callers that need to distinguish Available from Partial, not just render/don't-render.

**Verified this doesn't break anything:** `verify_exemption_gating.py` (16/16 checks) and the full offline scanner suite pass unchanged after the edit; `_FIELD_COVERAGE_SNAPSHOT` starts empty in any environment where `county_field_coverage` doesn't exist yet (guarded by a try/except), so today's production behavior is byte-for-byte identical until Diego's live run populates the table.

---

## Known limitations

- **No live measurement has been run.** Every Travis/Dallas number in this report is a fixture reproducing an already-cited, on-file fact, not a fresh measurement. `PX_M4_LIVE_MEASUREMENT_COMMANDS.md` has the exact commands.
- **`data_coverage.py`'s HS_CAP_LOSS_COVERAGE/EXEMPTION_CODES_COVERAGE manifests are not superseded by this mission** — they remain the current, hand-seeded source for those two fields' historical (2021-2024) figures until a live gate run backfills `county_field_coverage` for those years too. No file was deleted or deprecated.
- **The §7 population-scoped-denominator question is deliberately unresolved.** Per the Mission 4 preamble's own instruction not to reopen already-ruled decisions and the brief's own explicit deferral of this exact question, `exemption_codes`/`hs_cap_loss`'s sparse-by-nature measurement uses the structural gate (source column present + mapped) as the primary capability signal specifically so this doesn't block Mission 4's stop condition.
- **No real sanity-floor reference exists for any field yet.** `SANITY_FLOOR_REFERENCES` ships empty; the Partial state is proven only against a synthetic, explicitly-labeled test-only number. Populating a real one requires an external, county-published reference value and a PM ruling to register it (see below).
- **The population predicate for class-conditional fields (`prop_type_cd = 'R'`) is a reasonable but not uniquely-correct choice.** `STAGE_A_...md` §A5.4 sketches a more refined `imprv_value > 0` population for some of these fields; this mission used the simpler, still-defensible predicate to keep the measurement layer's first version small, per the brief's own "do not overbuild" instruction.
- **The Source Registry (D5) is unpopulated by design** — no real source-metadata infrastructure exists in this repo yet to draw from; building it is explicitly out of Mission 4's scope per the brief's own §7/§13.
- **`app.py`'s new snapshot cache is process-lifetime, not request-lifetime** — a deploy/restart is required to pick up a newly-measured `county_field_coverage` row. This matches `STAGE_A_...md`'s own proposed design and this codebase's existing "no connection pooling, cost-conscious" posture, but means a same-process re-measurement won't be visible until the next restart.

## [unknown] items

- Travis's real, current, overall `classi_cd` coverage (no on-file figure exists anywhere in this thread's evidence).
- Whether the Dallas parcel counts cited here (703,446/705,536 from PX-20260901-02 vs. 769,536 from the Mission 4 preamble) reflect two different, correctly-growing snapshots in time, or some other discrepancy — not investigated this mission, out of scope.
- Real coverage for `living_area_sqft`/`gross_building_area_sqft` in either county — registered, never measured.

## [PM ruling needed] items

1. **§7's population-scoped-denominator question** (carried forward, unresolved by design — see "Known limitations" above).
2. **Sanity-floor reference sourcing:** what real, external, county-published reference value (if any) should back a sanity floor for `exemption_codes`/`hs_cap_loss`/`tax_delinquent`, and where does it come from (a CAD-published statistic? a Comptroller figure?). No such source is registered today.
3. **Class-conditional population refinement:** whether `prop_type_cd = 'R'` is the final population predicate for `classi_cd`/`living_area_sqft`/`gross_building_area_sqft`/`year_built`, or whether the more refined `imprv_value > 0`-scoped population `STAGE_A_...md` sketches should replace it before Dallas Stage B (Mission 5) depends on these numbers.

## What remains design-only

- `capability_contract.py`'s `SourceRegistryEntry`/`sources` list — structure exists, never populated.
- The full parcel/parcel_tax_year field inventory beyond the 12 fields registered here.
- Any UI wiring of `render_contract.py` beyond `county_has_field()`'s existing exemption-gating call sites — the brief's own explicit deferral ("the actual large-scale UI composition work comes later").

---

## Before/after: county_has_field() behavior

**Before (production today, per commit `02e8e39`):** `county_has_field(county_code, field)` reads `COUNTY_PROFILES[county_code]["field_coverage"][field]` directly — a hand-declared `True`/`False` per county, never derived from a live query.

**After (this mission, working tree only, not committed):** same function signature, same six call sites, same return type. Internally it now checks a measured snapshot first (empty today, since `county_field_coverage` doesn't exist in production yet) and falls back to the identical hand-declared boolean otherwise. **Net effect on production today: none** — this is confirmed by the unchanged offline scanner suite results. The upgrade activates only once Diego runs `PX_M4_LIVE_MEASUREMENT_COMMANDS.md`.

---

## Stop condition

Per the Mission 4 brief §19: the three field classes are implemented (D1/D4); measurement methods are deterministic (D2, fixture-proven); capability states are machine-derived (D3); Travis and Dallas have been measured — with fixtures, evidence-cited, not live (D6, `[PENDING — live execution]` for the real pass); the UI-facing capability decision is defined (D7, not wired beyond `county_has_field()`); tests cover the capability logic (D9, 16/16 + 8/8); the confidence/capability relationship is documented (D8, `parcelytics.md` §6); this report is complete (D10); unresolved questions are recorded above; no unrelated architecture work was pulled in (no Pipeline v2, no Harris, no Dallas Stage B, no broad property-page rewrite, no production write, no deploy).

**Stopping here.** Not proceeding into Dallas Stage B (Mission 5) automatically, per §19's explicit instruction.
