# Mission 5 — Dallas Stage B Report

**From:** Consultant
**To:** Diego (Owner) / PM
**Date:** 2026-09-11
**Repo state:** `parcelytics.md` read at commit `66b4e0c` (current HEAD). Working tree only — no commit, no push, per standing repo-tree discipline.
**Standing constraint:** no database/network access for this agent. Every live-validation step below is a command list for Diego (and the PM, synchronously) to run — the same pattern as every prior mission's dry-runs and the `hs_cap_loss` backfill.

This report distinguishes, throughout: **measured facts** (cited `[file:line]` or `[measured DATE]`), **validated conclusions** (confirmed against live data by Diego), and **unresolved questions** (flagged `[PM ruling needed]` or "pending live validation"). Nothing here is claimed as live-validated unless a specific measurement date and source is cited.

---

## Task 0 / Deliverable D1 — Dallas population-predicate certification

### The problem, confirmed

`capability_registry.py` registers `population_predicate="prop_type_cd = 'R'"` identically for all four class-conditional fields (`classi_cd`, `living_area_sqft`, `gross_building_area_sqft`, `year_built`). ISS-0910-06 confirmed live that this predicate returns **zero Dallas rows**, causing all four fields to measure `not_measurable` → `Unknown` for Dallas — "real live proof of Mission 4's own flagged [PM ruling needed] #3 (population predicate not portable across counties)."

**Root cause, file-cited:** `loaders/dcad_format.py:948`'s `derive_parcel_class_fields(sptd_code)` writes Dallas's `parcel.prop_type_cd` as the **raw DCAD SPTD_CODE** (`"A11"`, `"F10"`, `"D10"`, `"A20"`, ...) — Travis's single-letter convention (`"R"`/`"P"`/`"MH"`) never appears in that column for Dallas at all. `load_dallas_certified.py:334` confirms `"prop_type_cd": sptd_code` is written verbatim.

The same `derive_parcel_class_fields()` function also derives `state_cd1` via `classification_map_dallas.py:169`'s `DCAD_SPTD_CD_XREF_2011` — a real, official DCAD cross-reference (transcribed from DCAD's own `SPTD_CD_XREF.pdf` in a prior session with `pdftotext` access), e.g. `"A11"→"A"`, `"A12"→"A"`, `"A13"→"A"`, `"A20"→"A"`, `"B11"→"B"`, `"F10"→"F1"`. This is the **same statewide PTAD state-code convention** Travis's own `state_cd1` uses (sourced from AJR's `ptd_state_cd`), per the function's own docstring.

`classification_map_dallas.py` (lines 128-133, 342-349) confirms the real measured distribution: "A (Residential): 569,020 (A11+A13+A12+A20)" = 80.7% of the file — matching `tax_logic/classify.py`'s own pre-existing `_STATE_PREFIX_LABEL` convention (`"A" → "Residential"`).

### Candidates, evaluated

1. **`state_cd1 = 'A'`** (preferred). Exact match on the already-derived, cross-referenced PTAD letter. Reuses a derivation Dallas's own loader already performs at parcel-write time; matches Travis's own `state_cd1` semantic for the identical field; matches `tax_logic/classify.py`'s existing precedent. Notably, `STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md`'s own §A5.4 sketch SQL (line 260-261) already uses `p.state_cd1 LIKE 'A%'` as the population for a *different* field (`exemption_codes_class_a`) — so this general shape is a standing pattern already present in this repo's own prior investigation, not a new invention.
2. **`state_cd1 LIKE 'A%'`**. Same idea, defensive against any future sub-lettered PTAD variant.
3. **`prop_type_cd LIKE 'A%'`**. Matches directly against Dallas's raw SPTD_CODE prefix (`A11`/`A12`/`A13`/`A20`), bypassing the derived `state_cd1` layer entirely.

### Live validation, run 2026-09-11 — result and PM ruling

The three read-only queries proposed above were run live by Diego/PM against production Dallas data on 2026-09-11:

```sql
-- Confirm the A-bucket count and distribution
SELECT state_cd1, COUNT(*) FROM parcel WHERE county_code='DALLAS' GROUP BY state_cd1 ORDER BY 2 DESC;
-- -> state_cd1 = 'A': 617,446 rows.

-- Confirm prop_type_cd membership within the A bucket (checks candidate 3 against candidate 1/2)
SELECT prop_type_cd, COUNT(*) FROM parcel WHERE county_code='DALLAS' AND state_cd1='A' GROUP BY prop_type_cd ORDER BY 2 DESC;
-- -> cleanly A11 / A12 / A13 / A20 -- matching this report's §Workstream C / Stage A cited
--    distribution ("A (Residential): 569,020 (A11+A13+A12+A20)" = 80.7% of the file; the
--    live certified count of 617,446 differs from that earlier file-based estimate, most
--    likely reflecting a fuller/more current loaded dataset -- not reconciled as a
--    discrepancy in this mission, noted here for the record).

-- Sanity: any state_cd1 value that isn't a single uppercase letter (would break candidate 2's LIKE 'A%' assumption)
SELECT DISTINCT state_cd1 FROM parcel WHERE county_code='DALLAS' AND state_cd1 !~ '^[A-Z]$';
-- -> blank/non-letter check performed; no anomaly reported that would favor candidate 2 over candidate 1.
```

**PM ruling, live-validated 2026-09-11: `state_cd1 = 'A'` is the certified Dallas class-conditional population predicate** (617,446 rows; `prop_type_cd` cleanly A11/A12/A13/A20 within it).

- **Candidate 2 (`state_cd1 LIKE 'A%'`) — rejected.** PM reasoning, verbatim: "DCAD's scheme uses real two-character codes (F1/F2/G1/G3/M1/D1) as distinct categories, not sub-lettered variants; exact match is unambiguous and doesn't carry hidden fragility if a future `A2`-style code is ever introduced."
- **Candidate 3 (`prop_type_cd LIKE 'A%'`) — rejected** as the weaker, duplicative-of-`state_cd1` option, per this report's own §Candidates analysis above.

**D1 status: CERTIFIED.** `capability_registry.py` now carries `"DALLAS": "state_cd1 = 'A'"` in `population_predicate_by_county` for all four class-conditional fields (`classi_cd`, `living_area_sqft`, `gross_building_area_sqft`, `year_built`), per PX-20260911-01 Task 1. This does **not** mean all four fields now read Available for Dallas: `classi_cd`, `living_area_sqft`, and `gross_building_area_sqft` remain genuinely unmapped for Dallas (see Workstream C below — unchanged by this certification) and will measure `MEASURED`/0% coverage → `Unavailable`, an honest "measured and missing" result. `year_built` specifically was re-verified by a new fixture test (`test_dallas_year_built_stays_unavailable_after_predicate_certification`, `loaders/test_field_coverage_gate.py`) to confirm it does not silently read as Available merely because the predicate is now certified — it resolves `MEASURED` with `coverage_fraction=0.0` → `Unavailable`, since `check_source_column()` is schema-level only (the `year_built` column exists on `parcel`, from Travis's usage) and the query then finds 0 non-null Dallas rows. This is a more precise technical description than "remains NOT_MEASURABLE" — the certified predicate gets the field past the predicate gate, and the pre-existing, separate mapping gap is what keeps it from reading Available. The full fixture suite (`loaders/test_field_coverage_gate.py`) passes 21/21 with this change, including two new tests: `test_dallas_year_built_stays_unavailable_after_predicate_certification` and `test_dallas_sqft_fields_also_certified`, plus a rewritten `test_travis_dallas_classi_cd_regression` reflecting that Dallas now resolves a real predicate (the prior "must never query Dallas" assertion is retired, since no class-conditional field remains uncertified for Dallas as of this ruling).

---

## Workstream B — county-scoped predicate mechanism (implemented, reversible)

Per the mission's autonomy policy ("create reversible code changes" is autonomous-safe) and its core principle ("Do not measure Dallas against Travis's assumptions"), the underlying architectural gap — a single, county-agnostic `population_predicate` string per field — has been fixed. This is a mechanism change, not a predicate certification: **no Dallas value was hardcoded.**

**`capability_registry.py`:**
- Added `FieldDefinition.population_predicate_by_county: Optional[dict]` — a per-county override map. Resolution order (in new `get_population_predicate(field_def, county_code)`): if the field has no such map at all, fall back to the single `population_predicate` string (fully backward-compatible with fields that were never flagged as cross-county-risky). If the map exists but the requested county has no key in it, return `None` — "not yet certified for this county" — never silently fall back to the single string.
- All four class-conditional fields (`classi_cd`, `living_area_sqft`, `gross_building_area_sqft`, `year_built`) now register `population_predicate_by_county={"TRAVIS": "prop_type_cd = 'R'"}` — **deliberately no `"DALLAS"` key**. Adding one, once Diego confirms a candidate, is a one-line, PM-reviewable change.

**`loaders/field_coverage_gate.py`:**
- `measure_class_conditional_field()` now resolves the predicate via `get_population_predicate()` instead of reading `field_def.population_predicate` directly. When it returns `None`, the function short-circuits to `NOT_MEASURABLE` (population_definition: `"NOT CERTIFIED for county=<code> (see capability_registry.FieldDefinition.population_predicate_by_county)"`) **without issuing any SQL against that county's data** — the exact fix for ISS-0910-06's pattern (a Travis literal silently evaluated against Dallas, returning a specific, wrong "0% coverage" answer instead of an honest "not measurable").

**Behavior change, confirmed by the updated fixture suite:** Dallas's four class-conditional fields now report `NOT_MEASURABLE` → `Unknown` for a *different, more honest* reason than before Mission 5. Before: `NOT_MEASURABLE` came from `population_count=0` (the query ran, using Travis's predicate, and found zero matching rows — a real query result, but computed from the wrong population). After: `NOT_MEASURABLE` comes from `get_population_predicate()` returning `None` before any query runs — "we have not certified how to measure this for Dallas," which is the true state. `capability_state()`'s consumer-facing behavior is unchanged (both paths render as `Unknown`, never a false `Unavailable`) — this is an internal-honesty fix, not a UI change, matching the mission's UI-scope boundary.

---

## Workstream C / Deliverable D3 — Dallas unmapped-field semantic findings

`load_dallas_certified.py`'s `PARCEL_SQL` writes exactly `county_code, geo_id, prop_id, prop_type_cd, state_cd1, owner_name, situs_address, zip_code` to `parcel` (confirmed twice: `STAGE_A_...md` line 36, `KNOWN_LIMITATIONS.md:407`). Every other `parcel`/`parcel_tax_year` column this registry cares about that isn't in that list is, by construction, unmapped for Dallas. Findings per field, drawing on `STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md` §A1.2 (already-completed investigation, cited rather than re-derived):

| Field | Source meaning | Product value | Canonical fit | Decision |
|---|---|---|---|---|
| `legal_desc` | `ACCOUNT_INFO.CSV`'s `LEGAL1`–`LEGAL5` (**H** header name, **V** sample confirmed live 2026-09-01: "WRIGHTS" / "BLK 3 LT 1" / blank / deed-instrument string / legacy account cross-ref) | Yes — Travis already renders this field; Dallas parity is a real gap, not cosmetic | Fits `legal_desc` directly | **Map**: `legal_desc = join(LEGAL1, LEGAL2, LEGAL3)`. **[PM ruling needed]**, unresolved: include `LEGAL4` (deed instrument/date) and `LEGAL5` (legacy cross-ref)? Stage A recommends excluding both as out-of-scope extras. Documented in `KNOWN_LIMITATIONS.md` (this session). This is the **"mapped under a different name, never wired"** case the PM preamble asked about — parallel to `situs_address`/`owner_name` pre-PX-20260827-06, **not** parallel to `hs_cap_loss`'s genuine Certified-Export absence. |
| `neighborhood_cd` | `ACCOUNT_INFO.CSV`'s `NBHD_CD` (**H**, RES division; **V** published label "Neighborhood: 1HSS13"). Commercial template shows "Market Area" in the same slot — possibly a second column, unconfirmed. | Yes — feeds Market Snapshot movers, Peer Set/benchmark neighborhood match | Fits `neighborhood_cd` directly, same peer-grouping role Travis's field plays | **Map** (Stage A §A3 proposal): `parcel.neighborhood_cd = NBHD_CD` for both divisions if one column serves both; if COM uses a separate market-area column, load it into the same `neighborhood_cd` column. **[PM ruling needed]** only if the real header shows two distinct columns — Diego's read-only header/distinct-value check (Stage A §A1.3) resolves this. Already documented in `KNOWN_LIMITATIONS.md`. |
| `classi_cd` | No direct DCAD analog identified. DCAD's finest published classification is `SPTD_CODE` (already loaded → `state_cd1`/`prop_type_cd`/`benchmark_label`); Travis's `classi_cd` is a separate, finer improvement-level use code sourced from `IMP_INFO.TXT`, a table DCAD has no equivalent of in the confirmed table set. | Coarse classification is already served by `state_cd1`/`benchmark_label` for Dallas | Does not fit — mapping DCAD's `SPTD_CODE` into `classi_cd` would blur two genuinely different granularities (Travis's `classi_cd` is finer than `state_cd1`; forcing SPTD in would just duplicate `state_cd1` under a different column name) | **Do not map.** Leave `classi_cd` unmapped for Dallas; it remains a real, honest `Unknown`/`Unavailable` gap rather than a manufactured mapping that "resolves" the finding without adding real information. This is a direct application of the mission's own instruction: "Do not create mappings merely to eliminate an 'unmapped' finding." |
| `living_area_sqft` | `RES_DETAIL.CSV`'s `TOT_LIVING_AREA_SF` (**H**; **V** published "Living Area 2,240 sqft") | Yes — Basic Property Information display | Fits directly, RES-only | **Map** (Stage A proposal): `parcel.living_area_sqft = TOT_LIVING_AREA_SF`, RES division only. Currently blocked by `dcad_format.py`'s own `TABLE_LOAD_POLICY` (line 1080), which marks `RES_DETAIL` "DELIBERATELY UNLOADED... no existing Travis-side schema column at this granularity" — **this comment is stale**: `living_area_sqft` demonstrably is an existing Travis-side schema column (registered in `capability_registry.py`). Flagging this as a documentation gap for whoever picks up the `RES_DETAIL` mapping work — not fixed in this mission (implementation is Mission 6-scope; Task C is investigation-only). |
| `gross_building_area_sqft` | RES: `RES_DETAIL.CSV`'s `TOT_AREA_SF` + `RES_ADDL.CSV`'s `AREA_SIZE` (additional structures). COM: `COM_DETAIL.CSV`'s `GROSS_BLDG_AREA`/`NET_LEASE_AREA` | Yes — same display | Fits directly | **Map** (Stage A proposal): RES = `TOT_AREA_SF + Σ RES_ADDL areas` (mirroring Travis's own enclosed-area-sum convention, excluding open/site items by description); COM = `GROSS_BLDG_AREA`. Same `TABLE_LOAD_POLICY` staleness note applies (`RES_ADDL`/`COM_DETAIL` also marked "DELIBERATELY UNLOADED... no existing column"). |
| `year_built` | RES: `RES_DETAIL.CSV`'s `YR_BUILT`; COM: `COM_DETAIL.CSV`'s `YR_BUILT`. (**V**, published: "Year Built 1939", "Effective Year Built 1975" — `EFF_YR_BUILT` also exists but is a distinct field) | Yes — already a registered class-conditional field, already flagged in `capability_registry.py`'s own docstring as a Dallas gap (PX-20260901-04 Task 3) | Fits directly | **Map** (Stage A proposal): `parcel.year_built = YR_BUILT` of the main building (`BLDG_ID = 1` / lowest). Keep `EFF_YR_BUILT` out of scope unless a future brief wants a separate field. Same `RES_DETAIL`/`COM_DETAIL` load-policy blocker as the two rows above. |
| `exemption_codes` | `APPLIED_STD_EXEMPT.CSV`'s per-applicant flag/date/percent columns (no single `EXEMPT_CODE`-shaped column confirmed — DCAD publishes discrete flags: Homestead Date/%, Other/%, Disabled Person, ISD/COUNTY Ceiling, Capped Homestead, not a code list) | Yes — already the subject of a full, closed investigation and gating fix (PX-20260901-05) | Needs derivation, not a direct column copy | **Already resolved architecturally, distinct from the "newly discovered" `legal_desc` finding**: every exemption-dependent UI surface already gates on `county_has_field(county_code, "exemption_codes")`, correctly showing "Not Available" for Dallas rather than a false negative (`KNOWN_LIMITATIONS.md:397-403`). The remaining work is deriving actual codes from DCAD's flag columns (Stage A §A2) — out of this mission's scope; not re-opened here. |

**Cross-cutting finding, flagged not fixed (per Task 0's sequencing note and this mission's own scope boundary):** three of the six unmapped `parcel` fields above (`living_area_sqft`, `gross_building_area_sqft`, `year_built`) are blocked by the *same* stale assumption baked into `dcad_format.py`'s `TABLE_LOAD_POLICY` docstring — that `RES_DETAIL`/`COM_DETAIL`/`RES_ADDL` carry "no existing Travis-side schema column at this granularity." That claim was true when originally written but is no longer true now that these three fields are registered canonical columns. This is exactly the kind of "true data field not currently loaded at all" finding the PM preamble's sequencing note said to flag for D1/D6 rather than fix inside this mission — flagging it here, not fixing it.

---

## Workstream D / Deliverable D4 — Dallas `hs_cap_loss` semantic finding

**No production data was queried or modified for this investigation.**

### Current code path (confirmed, file-cited)

`dcad_format.py:871`'s `derive_value_mapping()` computes:
```
has_cap = hmstd_cap_val is not None and hmstd_cap_val > 0
hs_cap_loss = (tot_val - hmstd_cap_val) if has_cap and tot_val is not None else None
```
Both `tot_val` and `hmstd_cap_val` are `_int_or_none()`-cast dollar-value columns from `ACCOUNT_APPRL_YEAR.CSV`. `HMSTD_CAP_VAL` was resolved (commit `1cac92b7c`, 2026-08-26, "PX-20260826-04") to mean the **capped value itself** (post-cap assessed value), not a loss amount — unlike Travis's own `hs_cap_loss` column, which stores the loss directly. Per this design, `hs_cap_loss = 0` (not NULL) when a cap is present but not currently binding (`HMSTD_CAP_VAL == TOT_VAL`).

**Real corroborating evidence already in-repo:** Stage A §A1.2 (line 51) cites DCAD's own live public account page (tier **V**, 2026-09-01): "Capped Homestead" = $744,854 in 2008 vs. Market $819,310 (a genuine, binding, six-figure cap loss), and `$0` in 2026 (not binding). This one real example matches `derive_value_mapping()`'s design exactly — it is not obviously broken.

### The tension this investigation cannot resolve without live data

The mission brief's dry-run finding (values like `0, 4, 5, 6, 7`, "100% populated") does not match the shape a working `derive_value_mapping()` should produce at full-file scale: if `has_cap` only triggers for the subset of accounts that genuinely have a homestead cap on file, `hs_cap_loss` should be **NULL for the (large) non-homestead/commercial population**, not populated at 100%. Two competing, evidence-grounded hypotheses, **neither validated**:

1. **The values are genuine, and the "100% populated" framing overstates what was actually observed.** If most Dallas homes have a cap on file (even a non-binding one, `HMSTD_CAP_VAL == TOT_VAL` most years) real estate under Texas's 10%-per-year cap, non-binding-but-present caps producing `0` most years, with occasional small dollar amounts (`4`, `5`, `6`, `7`) for accounts whose cap is only barely exceeded, is not intrinsically implausible for a subset of the file. This does not explain why the brief's finding reads "100% populated" unless the sample itself was drawn from an unrepresentative or narrow slice.
2. **`has_cap = hmstd_cap_val > 0` is testing the wrong condition.** If DCAD populates `HMSTD_CAP_VAL` with a non-zero value for essentially every account (residential or not) as a general assessed-value field rather than a homestead-specific flag, `has_cap` would fire for ~100% of rows regardless of actual homestead status, and `TOT_VAL - HMSTD_CAP_VAL` would mostly reduce to small rounding-scale noise for the (large) population that never has a real cap, with genuine large cap-loss values buried in the minority that do. This would make `has_cap`'s test condition — not the arithmetic itself — the actual bug: it should likely test something like `hmstd_cap_val < tot_val` (a genuine discount) rather than merely `hmstd_cap_val > 0` (a populated field).

I could not distinguish between these from repo evidence alone — this genuinely requires the live distribution, which this agent cannot query.

**Timing check performed (ruling out a third hypothesis):** I dispatched a sub-investigation into whether Dallas's live production data could simply predate the 2026-08-26 `HMSTD_CAP_VAL` fix (the same "stale load, current code is fine" pattern that explained the Travis `hs_cap_loss` clobber, PX-20260910-01). Finding: the fix (`1cac92b7c`, 17:44) landed **before** `load_dallas_certified.py` gained parcel-write capability at all (`787f8c5`, 22:08, same day) and well before the earliest confirmed live Dallas data (`PX-20260901-02`, ~2026-09-01/02). There is no evidence Dallas production data was ever written by a pre-fix loader version — this specific hypothesis is not supported and is not the leading explanation.

### D4 classification (per the brief's three options)

Given the evidence, I cannot yet classify this as "valid and correctly named," "valid but incorrectly named/mapped," or "incorrect and requiring repair" with confidence — the two hypotheses above point in different directions on that exact question (hypothesis 1 leans toward "valid," hypothesis 2 leans toward "incorrectly mapped, needs a corrected `has_cap` predicate"). **This is the honest, current state: unresolved, pending live validation.**

**Live validation commands for Diego / PM (read-only, no writes):**

```sql
-- Full distribution, not just a handful of sampled values
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE hs_cap_loss IS NULL) AS null_count,
  COUNT(*) FILTER (WHERE hs_cap_loss = 0) AS zero_count,
  COUNT(*) FILTER (WHERE hs_cap_loss > 0 AND hs_cap_loss < 100) AS tiny_positive,
  COUNT(*) FILTER (WHERE hs_cap_loss >= 100 AND hs_cap_loss < 10000) AS small,
  COUNT(*) FILTER (WHERE hs_cap_loss >= 10000) AS real_dollar_scale,
  MIN(hs_cap_loss), MAX(hs_cap_loss), AVG(hs_cap_loss)
FROM parcel_tax_year
WHERE county_code = 'DALLAS' AND tax_year = (SELECT MAX(tax_year) FROM parcel_tax_year WHERE county_code='DALLAS');

-- Does hs_cap_loss correlate with the state_cd1 = 'A' (residential) population,
-- or is it populated across commercial/personal accounts too (tests hypothesis 2)?
SELECT p.state_cd1, COUNT(*) AS n,
       COUNT(*) FILTER (WHERE pty.hs_cap_loss IS NOT NULL) AS has_value,
       COUNT(*) FILTER (WHERE pty.hs_cap_loss = 0) AS is_zero
FROM parcel p JOIN parcel_tax_year pty
  ON p.county_code = pty.county_code AND p.geo_id = pty.geo_id
WHERE p.county_code = 'DALLAS' AND pty.tax_year = (SELECT MAX(tax_year) FROM parcel_tax_year WHERE county_code='DALLAS')
GROUP BY p.state_cd1 ORDER BY n DESC;
```

If the second query shows `hs_cap_loss` populated at a similarly high rate across every `state_cd1` bucket (including clearly non-residential ones like commercial/industrial), that confirms hypothesis 2 (`has_cap`'s test condition is wrong) and points to a specific, scoped code fix — a separate, approval-required repair per the brief's own "no Dallas production data modification is authorized as part of the investigation" boundary. **No repair is proposed or executed here.**

---

## Deliverable D5 — Tests / regression coverage

- `loaders/test_field_coverage_gate.py`'s `test_travis_dallas_classi_cd_regression()` was revised **again** (PX-20260911-01 Task 2), now that the PM has certified `state_cd1 = 'A'` for Dallas: it asserts Dallas classi_cd resolves the real certified predicate and returns a genuine `MEASURED` result (0% coverage, since `classi_cd` itself remains genuinely unmapped for Dallas — Workstream C, unchanged), not `NOT_MEASURABLE`. The prior revision's "must never issue SQL for Dallas" assertion is **retired** for this field, since a certified predicate means SQL now correctly runs; no class-conditional field remains uncertified for Dallas as of this ruling.
- Two new fixture tests added, per PX-20260911-01 Task 1's explicit request: `test_dallas_year_built_stays_unavailable_after_predicate_certification` (empirically confirms `year_built` resolves `MEASURED`/0% coverage → `Unavailable`, not silently "fixed" to Available, by certifying the predicate alone) and `test_dallas_sqft_fields_also_certified` (confirms `living_area_sqft`/`gross_building_area_sqft` also resolve the certified predicate).
- Full offline suite re-run: `loaders/test_field_coverage_gate.py` → **21 test functions, 0 FAIL, ALL CHECKS PASSED** (was 19/19 before this round's additions).
- `python3 -m py_compile capability_registry.py loaders/field_coverage_gate.py loaders/test_field_coverage_gate.py` → clean.
- No existing Travis-path test behavior changed (`test_measure_class_conditional_field_basic`, county="TRAVIS", passes unchanged — confirms full backward compatibility of the `population_predicate_by_county` mechanism for every field that doesn't opt into it).
- No `hs_cap_loss`/Travis-protection tests were touched; PX-20260910-03's COALESCE guard and its 4 fixture tests are untouched and still pass (verified via the same full-suite run in `loaders/test_ears_format.py`, unaffected by this mission's changes — no shared code path).

---

## Files changed (working tree, uncommitted)

- `capability_registry.py` — added `population_predicate_by_county` field + `get_population_predicate()` helper; registered Travis-only entries for the four class-conditional fields; **PX-20260911-01 Task 1:** added the PM-certified `"DALLAS": "state_cd1 = 'A'"` entry to all four fields' `population_predicate_by_county`, with updated comments (see Task 0 section above).
- `loaders/field_coverage_gate.py` — `measure_class_conditional_field()` now resolves the predicate via the new helper; short-circuits to `NOT_MEASURABLE` when uncertified for a county, before any query. Unchanged in this round — the mechanism itself required no edits to correctly handle the newly certified Dallas predicate.
- `loaders/test_field_coverage_gate.py` — revised `test_travis_dallas_classi_cd_regression()` to assert the corrected (honest) behavior; **PX-20260911-01 Task 2:** revised again to reflect the now-certified Dallas predicate, plus two new fixture tests (see Deliverable D5 above).
- `KNOWN_LIMITATIONS.md` — added the `legal_desc` Dallas entry (parallel to the existing `exemption_codes`/`neighborhood_cd` entries).
- `parcelytics.md` — added §4 point 8, documenting the vault → operational staging → loader recovery pattern established during the Mission 4 `hs_cap_loss` backfill, per the brief's §8 instruction. The two existing Travis vault directory structures were not renamed, deduplicated, or declared canonical — that question remains explicitly deferred.
- `PX_DALLAS_STAGE_B_M5_REPORT.md` — **PX-20260911-01 Task 3:** this addendum — Task 0 section updated with the certified result and evidence trail; D5/Files-changed/Definition-of-Done updated for consistency. Workstream D (`hs_cap_loss`) left untouched, per explicit instruction — that investigation is still next.

No schema changes, no migrations, no production writes, no commit, no push.

---

## Definition of Done — status against the brief's own checklist

| Item | Status |
|---|---|
| Dallas population predicate identified and evidence-backed | **Certified** — PM ruling, live-validated 2026-09-11: `state_cd1 = 'A'` (617,446 rows) |
| Predicate validated against live Dallas data | **Done** — three queries run live 2026-09-11 (distribution, within-bucket `prop_type_cd` membership, blank/non-letter `state_cd1` sanity check); results and PM's candidate-2/3 rejection reasoning in Task 0 above |
| Dallas class-conditional measurements re-run using certified denominator | **Certified in code** (`capability_registry.py`, all four fields) and proven via fixture tests; a live re-measurement pass against production is the natural next synchronous step but is not itself gated on any further code change |
| Unknown states resolved where evidence supports it | **Resolved for the predicate question**: Dallas class-conditional fields now measure `MEASURED` (real query, real population) instead of `NOT_MEASURABLE`/`Unknown`. `classi_cd`/`living_area_sqft`/`gross_building_area_sqft`/`year_built` correctly still read `Unavailable`, not `Available` — a separate, genuine mapping gap (Workstream C), not a predicate-certification question |
| Dallas unmapped fields documented with semantic decisions | **Done** (Workstream C table above) — `legal_desc`/`neighborhood_cd` mapping proposals carry open `[PM ruling needed]` items; `classi_cd` explicitly decided not to map |
| Dallas `hs_cap_loss` semantics investigated and documented | **Done, unresolved** — two hypotheses, live queries needed to distinguish them; no repair proposed |
| No unsupported Dallas data silently mapped or exposed | **Confirmed** — the mechanism change makes this structurally harder to violate for class-conditional fields going forward |
| Relevant regression tests pass | **Confirmed** (21/21, 0 FAIL) |
| Travis capability measurement / `hs_cap_loss` protection intact | **Confirmed** — no shared code touched beyond the county-scoping helper itself, which is backward-compatible by design |
| `parcelytics.md` documents vault → staging pattern | **Done** (§4.8) |
| This report complete | **Done** |
| Any production repair separately scoped, awaiting approval | **N/A this round** — no repair identified with sufficient confidence yet; Workstream D's live queries are the prerequisite to knowing whether one is needed |

**STOP, per the mission's own instruction.** Task 0's candidate predicates, Workstream D's distribution queries, and the `RES_DETAIL`/`COM_DETAIL` load-policy staleness flag are all queued for the next synchronous session with Diego and the PM.
