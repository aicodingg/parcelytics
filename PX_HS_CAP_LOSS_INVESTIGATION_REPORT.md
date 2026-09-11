# PX_HS_CAP_LOSS_INVESTIGATION_REPORT

**Brief:** PX-20260910-01 — hs_cap_loss missing for Travis 2022–2026, production data-integrity issue
**Priority:** P0 (live production correctness issue)
**Scope:** investigation only, repo-side, no production writes, no live DB access performed by this agent
**Repo state:** working tree at time of writing, no commit/push

---

## Confirmed facts (as supplied by PM, live-measured 2026-09-10)

| tax_year | prop_unit_tax_year non_null | parcel_tax_year non_null |
|---|---|---|
| 2021 | 364,130 / 462,367 (78.7%) | 410,170 / 510,664 (80.3%) |
| 2022 | 0 / 470,483 | 0 / 445,135 |
| 2023 | 0 / 477,092 | 0 / 449,493 |
| 2024 | 0 / 481,796 | 0 / 452,909 |
| 2025 | 0 / 486,859 | 0 / 455,557 |
| 2026 | 0 / 493,246 | 0 / 461,088 |

2021 is populated. Every year 2022 forward is exactly zero, in both tables, identically. `data_coverage.py`'s `HS_CAP_LOSS_COVERAGE[2023] = 0.999` does not match production — confirmed wrong.

2025/2026 being zero is already-known, by-design ("2025 Certified Export does not carry a homestead cap loss field" — `KNOWN_LIMITATIONS.md` line 20-23). **2022–2024 being zero is not by design** — AJR field[35] is documented as the source for exactly those years, and this investigation confirms that source data is still correctly extracted, just overwritten downstream.

---

## Root cause: confirmed, with file:line citations

**The bug is an unconditional-overwrite `ON CONFLICT` clause, not a missing or broken extraction.** A later loader writes a correct `NULL` for a field it genuinely doesn't have — but writes that `NULL` through a shared UPSERT statement that has no `COALESCE` guard, so it destroys an earlier loader's correct value instead of leaving it alone.

### 1. `loaders/load_ajr.py` — the correct, still-intact source of truth for 2021–2024

`loaders/load_ajr.py:23` documents `[35] hs_cap_loss` as a stable field position across the AJR CSV format, with no year-qualified caveat (unlike `neighborhood_cd`, which the same docstring at lines 15/169 explicitly notes shifts position between 2021 and 2022+ format). `loaders/load_ajr.py:175` extracts it for every year the loader processes: `hs_cap = _int_or_none(fields[35])`. `loaders/load_ajr.py:193-194` places it correctly in the `pty_rows` tuple at the position matching `hs_cap_loss` in `PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s column list (verified against `ears_format.py:437-438`, position 8 of 12: `county_code, prop_id, tax_year, geo_id, market_value, assessed_value, taxable_value, hs_cap_loss, ...`). `config.py:198-203` (`AJR_FILES`) points at real, named 2021–2024 source files (`227EARS092822.csv`, `227EARS083023.csv`, `227EARS082824.csv`, plus 2021's `_PTD.csv`) — this loader is designed to and does process all four years uniformly. **Conclusion: `load_ajr.py`'s extraction and write logic for `hs_cap_loss` is correct for 2021-2024 today, in the code currently in the repo.** This rules out a mapping-loss/extraction bug (item 3 of the brief's request).

### 2. `loaders/load_cert_2021.py` — writes 2021 only, never touches 2022+, and correctly re-supplies its own cap_loss value

`loaders/load_cert_2021.py:37` states explicitly: *"Does NOT touch 2022–2024 rows."* Its `--csv` input (produced by `parse_cert_2021_pdf.py`) carries its own `cap_loss` column (`loaders/load_cert_2021.py:30`), which this loader maps to `hs_cap_loss` and writes with `SET hs_cap_loss = EXCLUDED.hs_cap_loss` (`loaders/load_cert_2021.py:134`, part of `build_upsert_sql()`). This is also an unconditional overwrite — but it's harmless for 2021, because the 2021 Certified Roll PDF *does* carry a homestead-cap figure of its own, so this loader always supplies a real value, never a destructive `NULL`. **This is why 2021 is intact: it's the one year with no downstream loader that both (a) shares the AJR-sourced row and (b) supplies `NULL` for `hs_cap_loss`.**

### 3. `loaders/load_certified_historical.py` — the loader that clobbers 2022–2024 (and would clobber 2026 too, if AJR ever covered it)

This is the confirmed mechanism. `loaders/load_certified_historical.py:149` (`load_prop_ent()`, reading `PROP_ENT.TXT` for years 2022/2023/2024/2026 — `argparse` `choices=[2022, 2023, 2024, 2026]` at line 282) builds each row tuple as:

```python
rows_to_insert.append((
    county_code,
    agg["prop_id"], agg.get("year") or year, geo_id,
    agg["market_value"], agg["assessed_value"], agg["taxable_value"],
    None, None, None,                      # ← hs_cap_loss, land_value, imprv_value
    agg["exemption_codes"], data_source,
))
```
(`loaders/load_certified_historical.py:173-179`)

Position 8 of this tuple (the first `None` in the `None, None, None` group) is `hs_cap_loss`, matching `PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s column order exactly. This `None` is *correct* on its own terms — confirmed below (§4) that the Certified Export's `PROP_ENT.TXT` genuinely has no cap-loss-equivalent field to extract. The row is then written via `ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL` (`loaders/load_certified_historical.py:161`), whose `ON CONFLICT (county_code, prop_id, tax_year) DO UPDATE` clause is:

```sql
SET geo_id          = EXCLUDED.geo_id,
    market_value    = EXCLUDED.market_value,
    assessed_value  = EXCLUDED.assessed_value,
    taxable_value   = EXCLUDED.taxable_value,
    hs_cap_loss     = EXCLUDED.hs_cap_loss,     -- ← unconditional, no COALESCE
    land_value      = EXCLUDED.land_value,
    imprv_value     = EXCLUDED.imprv_value,
    exemption_codes = EXCLUDED.exemption_codes,
    data_source     = EXCLUDED.data_source
```
(`loaders/ears_format.py:441-449`)

**Every column here is unconditionally overwritten with whatever the calling loader supplies — including `NULL` — for all nine non-key columns.** If this UPSERT runs for a `(county_code, prop_id, tax_year)` key that `load_ajr.py` already wrote a real `hs_cap_loss` value into, that value is destroyed and replaced with `NULL`. There is no `COALESCE(EXCLUDED.hs_cap_loss, prop_unit_tax_year.hs_cap_loss)` anywhere in this statement.

`load_certified_historical.py`'s own `land_value`/`imprv_value` for the *same* row are also unconditionally set to `None` in this same tuple — but that isn't destructive, because **Step 3** of this same loader (`load_land_imprv()`, `loaders/load_certified_historical.py:194-228`) runs immediately afterward and issues a dedicated `UPDATE prop_unit_tax_year SET land_value = %s, imprv_value = %s ...` from `LAND_DET.TXT`, repairing those two columns from a real source file the Certified Export *does* provide. **`hs_cap_loss` has no equivalent repair step**, because the Certified Export has no file that carries it — there is nothing for a Step-3-style pass to read from. This is the asymmetry: `load_certified_historical.py` was clearly built with the *general* awareness that this shared UPSERT can lose data it doesn't itself carry (that's exactly why the `LAND_DET.TXT` repair pass exists) — but the fix was only applied to `land_value`/`imprv_value`, not to `hs_cap_loss`, which has no analogous second file to repair from and so needed a `COALESCE` guard instead, which was never added.

### 4. Confirmed: the Certified Export genuinely never carries a cap-loss field (not a missed mapping)

`loaders/ears_format.py`'s `iter_prop_ent_aggregates()` (the parser `load_prop_ent()` calls, `loaders/ears_format.py:243`) never references `hs_cap_loss` or any cap-loss-equivalent field anywhere in its body — confirmed via a full-file grep, zero matches inside that function. This matches `KNOWN_LIMITATIONS.md:20-21`'s existing, correct disclosure: *"The 2025 Certified Export (PROP.TXT / PROP_ENT.TXT) does not carry a homestead cap loss field."* `load_certified_historical.py:32` states *"Field positions (same across 2022-2026 exports): see loaders/ears_format.py"* — i.e., the 2022-2024 Certified Exports share the exact same PROP.TXT/PROP_ENT.TXT format as 2025/2026, which is independently confirmed to lack this field. **This is not a silent mapping loss (brief's question 3) — there is nothing to map. The `None` `load_certified_historical.py` writes is the honest, correct answer to "what does the Certified Export say for this field." The bug is entirely in what happens to the AJR value that was already there.**

---

## Loader-boundary hypothesis: confirmed

The brief asked whether the 2021-vs-2022+ break lines up with a loader change. **Yes, exactly:**

- **2021**: only `load_ajr.py` (writes real `hs_cap_loss`) and `load_cert_2021.py` (writes its own real `cap_loss`, never `NULL`) ever touch this year. No loader ever writes `NULL` into `hs_cap_loss` for 2021.
- **2022, 2023, 2024**: `load_ajr.py` writes a real `hs_cap_loss` first; `load_certified_historical.py` — a real, existing loader whose `--year` choices are exactly `[2022, 2023, 2024, 2026]` (`loaders/load_certified_historical.py:282`) — writes `NULL` for the same rows afterward via the unguarded shared UPSERT.
- **2025**: `load_certified_2025.py` also writes `None` for `hs_cap_loss` via the same shared SQL (`loaders/load_certified_2025.py:149`, confirmed) — but 2025 AJR data was intentionally never loaded in the first place (`config.py:203`: *"2025 AJR is intentionally omitted — use Certified Export instead"*), so there's no prior correct value to destroy. This year's zero is genuinely by-design, unrelated to the 2022-2024 bug, exactly as `KNOWN_LIMITATIONS.md` already states.
- **2026**: same shape as 2025 — no AJR source exists for 2026, so there's nothing to destroy. Confirmed via two loaders: `loaders/load_2026_preliminary.py:154-158` builds the same `None, None, None` (hs_cap_loss/land_value/imprv_value) tuple as `load_certified_historical.py`, written via the identical shared `ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL` (`loaders/load_2026_preliminary.py:176`) — non-destructive for the same reason 2025 is (no prior AJR value exists to overwrite). `loaders/snapshot_2026_preliminary.py` is out of scope entirely: its own module docstring (`loaders/snapshot_2026_preliminary.py:31-34`) confirms it "Writes ONLY to the new, standalone `parcel_2026_preliminary_snapshot` table... Never touches `parcel_tax_year`, `prop_unit`, `prop_unit_tax_year`, or any other live table" — and separately notes (`:27-30`) that its rollup computes an `hs_cap_loss` value but the target table's schema has no column for it, so it's computed and discarded, not written anywhere. This script cannot contribute to or repair the live bug either way. `load_certified_historical.py --year 2026` (`loaders/load_certified_historical.py:282`) follows the same by-design, non-destructive `None`-write pattern as 2022-2024 structurally, but with no prior AJR value at stake for 2026.

**Run-order evidence (design-level, not live-confirmed):** `load_certified_historical.py`'s own `post_load_summary()` function prints, unconditionally, *"AJR rows before load"* and *"Updated (AJR→cert) ← replaced AJR values"* (`loaders/load_certified_historical.py:262,265`) — this loader's own logging is written under the explicit, designed assumption that AJR data already exists for the year being loaded and is *expected* to be superseded by Certified Export values (market/assessed/taxable/exemption_codes — the columns Certified Export is more authoritative for). This is strong circumstantial evidence for the run order (`load_ajr.py` before `load_certified_historical.py`, for the same years) but is a **design inference from the code's own comments and print statements, not a live-measured execution-timestamp confirmation** — this agent has no DB access to check `ingest_audit` or any run log for actual historical run order. Flagged as `[unknown — needs live confirmation]` below.

---

## Is this fixable? Yes.

This is **not** a case where the source data genuinely never existed (which would call for a `KNOWN_LIMITATIONS.md` honest-disclosure entry instead of a fix, per the brief's own instruction to check this before assuming a fix is possible). The evidence says otherwise:

- `load_ajr.py`'s field[35] extraction is present, uniform across 2021-2024, and already proven correct for 2021 (independently corroborated by `load_cert_2021.py`'s own PDF-sourced `cap_loss` for that year).
- `config.AJR_FILES` still points at real, named 2022/2023/2024 source files that `load_ajr.py` can re-read.
- `parcel_rollup.py`'s `SUM(y.hs_cap_loss)` (`parcel_rollup.py:134`) is a faithful, correct aggregation of whatever `prop_unit_tax_year.hs_cap_loss` contains — it is not itself broken (this matches and confirms what the brief already ruled out: *"prop_unit_tax_year itself ... is also zero for 2022+. Whatever's wrong is upstream of the rollup"* — this investigation locates that upstream point precisely, at `load_certified_historical.py`'s UPSERT).

The data was captured correctly at some point in the pipeline and is destroyed by a specific, identifiable, later write. A backfill re-run is a real remediation path, not a data-recovery-from-nothing problem.

---

## Proposed remediation paths (not implemented — production-write decision, PM ruling needed)

**Option A — Guard the shared UPSERT.** Add `hs_cap_loss = COALESCE(EXCLUDED.hs_cap_loss, prop_unit_tax_year.hs_cap_loss)` to `PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s `SET` clause (`ears_format.py:445`), mirroring the exact pattern `load_cert_2021.py`'s own (separate, pre-Unit-Model) upsert already uses for `land_value`/`imprv_value` (`loaders/load_cert_2021.py:135-136`: *"For land_value / imprv_value: COALESCE — keeps existing DB value if PDF had NULL"*). This is a shared SQL constant used by every writer (`load_ajr.py`, `load_certified_historical.py`, `load_certified_2025.py`, `load_2026_preliminary.py`, `load_dallas_certified.py`) — a `COALESCE` guard changes behavior for all of them, not just the two implicated here. **`[PM ruling needed]`**: is there ever a legitimate future case where a newer load must overwrite `hs_cap_loss` with `NULL` to correct a bad prior value? A blanket `COALESCE` would silently prevent that. If the answer is "no, `hs_cap_loss` should only ever be set by AJR loaders, never cleared by a later one," the guard is safe and correct; if not, a more targeted fix (e.g., only `load_certified_historical.py`/`load_certified_2025.py` pass through the *existing* DB value explicitly, rather than a blanket SQL-level guard) may be preferable.

**Option B — Backfill re-run.** After Option A ships (or independently, as a one-time repair), re-run `python3 loaders/load_ajr.py --county TRAVIS` for 2022-2024 (the loader already iterates `config.AJR_FILES` and calls `parcel_rollup.run()` per year at the end of `load()` — `loaders/load_ajr.py:235-239`). Because `PROP_UNIT_UPSERT_SQL`/`PROP_UNIT_TAX_YEAR_UPSERT_SQL` are both `ON CONFLICT ... DO UPDATE`, this re-run is idempotent and safe to run against already-loaded years — it will re-supply the correct `hs_cap_loss` for every 2022-2024 row and the subsequent `parcel_rollup.run()` call will re-derive `parcel_tax_year.hs_cap_loss` from the repaired `prop_unit_tax_year` data. **This must run AFTER Option A's SQL fix ships** (or, if run before, it will only produce a correct state until the next time `load_certified_historical.py` runs for these years again, at which point the same clobber recurs) — sequencing note only, not a recommendation to skip Option A.

**Option C — Backfill repair step inside `load_certified_historical.py` itself** (alternative/complement to A): mirror the `LAND_DET.TXT` pattern (Step 3) with a fourth step that reads the *existing* `prop_unit_tax_year.hs_cap_loss` for the rows about to be touched, before the `load_prop_ent()` UPSERT runs, and re-applies it afterward — functionally equivalent to Option A's `COALESCE` but scoped to this one loader rather than the shared SQL constant, avoiding the cross-caller ambiguity Option A's `[PM ruling needed]` raises.

This investigation does not recommend one option over another beyond noting Option A is the smallest, most centrally-enforced fix but has the broadest blast radius (affects every caller of the shared SQL); Option C is more surgical but duplicates a pattern rather than fixing it once. **Both require a PM ruling before implementation**, and neither should be built as part of this investigation per the brief's explicit instruction.

---

## Dallas: not affected by this bug, but the PM's stated premise needs a small correction

The brief states Dallas "never had this field mapped in the first place (already known, unrelated)." **This is worth updating, not just confirming:** Dallas's own loader *does* compute and write a real, derived `hs_cap_loss` — `loaders/dcad_format.py:899` (`hs_cap_loss = (tot_val - hmstd_cap_val) if tot_val is not None else None`), consumed by `loaders/load_dallas_certified.py:339-341,458-462`, written via the same shared `PROP_UNIT_TAX_YEAR_UPSERT_SQL`. This is a genuine, intentional DCAD-native derivation (`TOT_VAL - HMSTD_CAP_VAL`), unrelated to Travis's AJR-sourced approach — Dallas has no separate AJR-then-Certified two-pass sequence for the same table, so **Dallas does not share this specific clobbering bug** (there is no second, later Dallas loader that writes `NULL` over this same field for the same rows). Whether Dallas's `hs_cap_loss` is actually correctly populated live is unverified here (no live DB access, and out of this brief's Travis-focused scope) — flagged as `[unknown]`, not asserted either way, but the code path itself is real and does write a value, contradicting the "never mapped" framing as stated.

---

## `[PM ruling needed]`

1. Which remediation option (A, B, C, or A+B) to pursue for the 2022-2024 `hs_cap_loss` gap.
2. Whether a `COALESCE` guard on `PROP_UNIT_TAX_YEAR_UPSERT_SQL` (Option A) is safe for every current and future caller of that shared SQL constant, or whether a narrower, loader-scoped fix (Option C) is preferred specifically because some future loader may legitimately need to clear `hs_cap_loss`.
3. Whether the "Dallas never had this field mapped" premise in `COUNTY_PROFILES`/prior docs (per the PM's own framing) should be corrected now that this investigation found a real, intentional Dallas-native derivation in `dcad_format.py`/`load_dallas_certified.py` — separate scope from this P0, flagged for awareness only.

## `[unknown]` — not verified this session (no live DB access)

- The actual historical execution order/timestamps of `load_ajr.py` vs. `load_certified_historical.py` runs against production for 2022-2024 — inferred from code/print-statement design intent (§"Loader-boundary hypothesis"), not confirmed via `ingest_audit` or any run log.
- Whether Dallas's own `hs_cap_loss` is actually non-null/correct in live production today.
- Whether `config.AJR_FILES`' 2022-2024 paths still point at valid, reachable files in Diego's current environment (a precondition for Option B's backfill re-run).

---

## Summary

Root cause confirmed: `loaders/load_certified_historical.py` (2022, 2023, 2024, and 2026 Certified Export loads) writes `hs_cap_loss = None` for every row it touches — a correct value, since the Certified Export genuinely lacks this field — through `ears_format.PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s unconditional `ON CONFLICT ... DO UPDATE SET hs_cap_loss = EXCLUDED.hs_cap_loss` (no `COALESCE`), which destroys the correct value `loaders/load_ajr.py` had already written from AJR field[35] for those same years. 2021 is unaffected because its only downstream loader (`load_cert_2021.py`) always supplies its own real cap-loss value, never `NULL`. 2025/2026 being zero is unrelated and already correctly documented as by-design (no AJR source exists for those years). This is fixable — the source data is intact and re-loadable — not a case for a `KNOWN_LIMITATIONS.md` honest-disclosure-instead-of-fix entry. Dallas does not share this specific bug mechanism, though the "never mapped" premise about Dallas itself needs a small correction. No code was changed and nothing was written to production in this investigation, per the brief's explicit instruction.
