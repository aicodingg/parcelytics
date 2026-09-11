# PX_HS_CAP_LOSS_FIX_REPORT

**Brief:** PX-20260910-03 — Fix `hs_cap_loss` clobber: COALESCE guard + AJR backfill (Travis 2022–2024)
**Follows directly from:** `PX_HS_CAP_LOSS_INVESTIGATION_REPORT.md` (PX-20260910-01) — root cause is not re-investigated here.
**PM ruling (binding, already made):** Option A (COALESCE guard on the shared UPSERT) + Option B (AJR backfill), in that order. Option C rejected.
**Repo state:** code changes applied to the working tree; **nothing committed, nothing pushed, no production write performed by this agent.**

---

## Task 1: the guard

### Diff

`loaders/ears_format.py` — one functional line changed (`PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s `hs_cap_loss` `SET` clause), plus a documentation block above it and a new pure-Python mirror function below it:

```diff
             market_value    = EXCLUDED.market_value,
             assessed_value  = EXCLUDED.assessed_value,
             taxable_value   = EXCLUDED.taxable_value,
-            hs_cap_loss     = EXCLUDED.hs_cap_loss,
+            hs_cap_loss     = COALESCE(EXCLUDED.hs_cap_loss, prop_unit_tax_year.hs_cap_loss),
             land_value      = EXCLUDED.land_value,
             imprv_value     = EXCLUDED.imprv_value,
             exemption_codes = EXCLUDED.exemption_codes,
             data_source     = EXCLUDED.data_source
```

Every other column in this `SET` clause (`geo_id`, `market_value`, `assessed_value`, `taxable_value`, `land_value`, `imprv_value`, `exemption_codes`, `data_source`) is **untouched** — still unconditional overwrite, exactly as before. The guard is scoped to `hs_cap_loss` only, per the ruling.

Full diff (`loaders/ears_format.py`, `loaders/test_ears_format.py`) is in the repo working tree, viewable via `git diff loaders/ears_format.py loaders/test_ears_format.py`. A documentation block (`ears_format.py:435-467`) was added directly above the SQL explaining the fix, citing PX-20260910-01/-03, and recording the caller-by-caller check below. A pure-Python mirror function, `resolve_hs_cap_loss()` (`ears_format.py:508-529`), was added following this project's established pattern (`resolve_prop_unit_conflict()`, used for the earlier `PROP_UNIT_UPSERT_SQL` geo_id guard) so the guard's semantics can be fixture-tested without a live DB.

### Caller-by-caller verification (per Task 1's instruction — checked, not assumed)

All five real callers of `PROP_UNIT_TAX_YEAR_UPSERT_SQL` were re-confirmed via `grep -rn PROP_UNIT_TAX_YEAR_UPSERT_SQL loaders/`:

| Caller | Writes `hs_cap_loss` from | Ever needs to NULL out an existing value? |
|---|---|---|
| `load_ajr.py:193-194,204` | Real, extracted AJR field[35] value | No — a real value is written whenever the source has one; only `None` when the AJR row itself is blank for that field, and AJR years are never re-supplied by a *different* later AJR file for the same year, so this never overwrites a real prior value with `None`. |
| `load_certified_historical.py:161,173-179` | Hardcoded `None` (source format has no field) | This is the caller whose `None` was destructive before the guard — it has no legitimate case for wanting its `None` to win, ever. |
| `load_certified_2025.py:149,171` | Hardcoded `None` (source format has no field) | Same as above — non-destructive after the guard, and never legitimately needs to clear a value. |
| `load_2026_preliminary.py:154-158,176` | Hardcoded `None` (source format has no field) | Same as above. |
| `load_dallas_certified.py:457-469` | `dcad_format.derive_value_mapping()`'s real, derived `hs_cap_loss` (`TOT_VAL - HMSTD_CAP_VAL` when a cap is present, else `None` — `dcad_format.py:896-902`) | **Flagged, not fixed (out of scope — see below).** |

**Dallas caveat, flagged per Task 1's instruction to verify each caller's actual behavior rather than assume:** Dallas's `None` is semantically different from the three Travis loaders above. For Travis, `None` always means "this source format structurally cannot supply the field" — never a legitimate current-state answer. For Dallas, `dcad_format.derive_value_mapping()`'s `None` can mean a genuine "no homestead cap present this year" — a real, current answer, not a format limitation (`dcad_format.py:880-885,896-902`). If the same `(county_code, prop_id, tax_year)` key is ever re-loaded from a corrected DCAD export where a previously-present cap has genuinely been removed, this guard would incorrectly preserve the stale non-`NULL` value instead of updating it to `NULL`. **This does not affect Travis** (the two loaders never overlap on the same rows the way `load_ajr.py`/`load_certified_historical.py` do) and is explicitly **not fixed here** — Dallas's `hs_cap_loss` semantics are PX-20260910-02's scope, not this brief's. Documented in the code comment (`ears_format.py:456-467`) so it isn't lost.

This satisfies the PM's ruling's own premise ("`hs_cap_loss` is never intentionally cleared to NULL by a later loader, by design, permanently") for all three Travis-affecting loaders — the one caller where that premise doesn't cleanly hold (Dallas) is flagged, not silently accepted.

---

## Task 2: fixture tests (3 directions)

Added to `loaders/test_ears_format.py`, following this project's established fixture-test conventions (pure-Python mirror + direct string assertion against the shipping SQL, matching the precedent set by `test_geoid_*` for the earlier `PROP_UNIT_UPSERT_SQL` fix):

1. **`test_hs_cap_loss_sql_text_contains_coalesce_guard`** — direct string assertion against the real, shipping `PROP_UNIT_TAX_YEAR_UPSERT_SQL` text: confirms the `COALESCE(...)` form is present and the old bare `hs_cap_loss = EXCLUDED.hs_cap_loss,` form is gone. Catches drift between the pure-Python mirror and the actual SQL constant.
2. **Direction (a) — `test_hs_cap_loss_real_value_into_null_row_is_written`**: a real incoming value (`4321`) into a row that was `NULL` → written (`4321`), unaffected by the guard.
3. **Direction (b) — `test_hs_cap_loss_null_into_real_row_is_preserved`**: `NULL` incoming into a row with a real existing value (`7890`) → preserved (`7890`). This is the exact regression PX-20260910-01 found (a Certified-Export loader's `None` clobbering AJR's real value) — proven to no longer reproduce.
4. **Direction (c) — `test_hs_cap_loss_null_into_null_row_stays_null`**: `NULL` incoming into a row that's already `NULL` → stays `None`. Guards against a false "preserved a value" claim when there was never a real value.

### Fixture test results

```
$ python3 loaders/test_ears_format.py
...
[PASS] PROP_UNIT_TAX_YEAR_UPSERT_SQL: hs_cap_loss uses COALESCE(EXCLUDED.hs_cap_loss, prop_unit_tax_year.hs_cap_loss), not a bare unconditional overwrite
[PASS] PROP_UNIT_TAX_YEAR_UPSERT_SQL: bare 'hs_cap_loss = EXCLUDED.hs_cap_loss' (the old, destructive form) no longer appears anywhere in the SQL
[PASS] hs_cap_loss guard: real incoming value into a previously-NULL row is written, unaffected by the guard
[PASS] hs_cap_loss guard: NULL from a source that structurally lacks the field does NOT clobber an existing real value -- PX-20260910-01's exact regression no longer reproduces
[PASS] hs_cap_loss guard: NULL into an already-NULL row stays NULL (no false 'preserved' claim)

ALL EARS_FORMAT FIXTURE TESTS PASSED
```

All 21 test functions in `test_ears_format.py` (17 pre-existing + 4 new hs_cap_loss tests, producing 42 individual `[PASS]` checks total) pass — zero regressions in the pre-existing geo_id-guard, PROP.TXT/PROP_ENT.TXT parsing, or 24-unit collision fixtures.

### Full offline suite (no regressions elsewhere)

```
$ python3 run_offline_checks.py
==============================================================================
Parcelytics offline verification runner -- REQUIRED tier
No database. No network. No vault. No production credentials.
==============================================================================
[PASS] county-scoping audit (verify_county_scoping.py)  (0.39s, exit=0)
[PASS] shadow-swap county-derivation audit (verify_shadow_swap_county_derivation.py)  (0.22s, exit=0)
[PASS] template-layer county-scoping audit (verify_template_county_scoping.py)  (0.27s, exit=0)
[PASS] data_unavailable copy denylist (verify_unavailable_copy_denylist.py)  (0.09s, exit=0)
[PASS] exemption-gating selftest (verify_exemption_gating.py)  (0.08s, exit=0)
[PASS] param/SQL placeholder safety (loaders/test_param_sql_placeholder_safety.py)  (0.09s, exit=0)
------------------------------------------------------------------------------
REQUIRED TIER: PASS -- 6/6 checks green
==============================================================================
```

`python3 -m py_compile loaders/ears_format.py loaders/test_ears_format.py` — clean, no syntax errors.

(`run_offline_checks.py`'s REQUIRED tier doesn't itself invoke `test_ears_format.py` — that suite was run directly, separately, above; both are clean.)

---

## Task 3: live backfill runbook (commands only — Diego executes, no DB access used by this agent)

**Honest disclosure before the commands:** `load_ajr.py` has **no `--dry-run` flag** — unlike several other loaders in this project (`load_pir_billing.py`, `load_dallas_certified.py`, `load_tax_current.py`, etc.), it goes straight to a live write when run. It also has **no `--year` selector** — `load()` always iterates every year present in `config.AJR_FILES` (currently 2021, 2022, 2023, 2024) in one pass, not a single chosen year. Both are true today, confirmed by reading `load_ajr.py:212-258` fresh for this brief — I'm not inventing flags that don't exist. Given that, step (a) below is the closest available read-only substitute for a formal dry-run: confirm the source files are real and reachable, and preview parseable row counts, before running the loader live. The loader's writes are themselves idempotent (upsert, not insert) and, after Task 1's guard, can only set real values or leave existing ones untouched — never destroy data — so a live run misfiring on bad input would at worst re-write correct values, not corrupt them, but confirming file reachability first is still good practice.

### (a) Confirm `config.AJR_FILES`' 2022/2023/2024 paths are valid and reachable

This resolves PX-20260910-01's own `[unknown]` item. Run from the Parcelytics repo root:

```bash
python3 -c "
import config
for year in (2022, 2023, 2024):
    path = config.AJR_FILES[year]
    import os
    exists = os.path.exists(path)
    size = os.path.getsize(path) if exists else None
    print(f'{year}: {path}  exists={exists}  size={size}')
"
```

If any path reports `exists=False`, **stop** — `load_ajr.py:222-224` will print a `WARNING: ... not found, skipping {year}` and silently skip that year rather than failing loudly, so a missing file at this step means that year's backfill will not happen on the next run, without an error. Confirm/relocate the file before proceeding.

Optional read-only row-count preview (parses the file without touching the DB, using the same iterator the loader itself uses — closest available substitute for a formal dry-run given `load_ajr.py` has none):

```bash
python3 -c "
import config
from loaders import ears_format as ef
for year in (2022, 2023, 2024):
    path = config.AJR_FILES[year]
    with open(path, encoding='latin-1', errors='replace') as f:
        lines = list(f)
    n_hs_cap = 0
    n_total = 0
    for line in lines:
        fields = line.rstrip('\n').split(',')
        if len(fields) > 35:
            n_total += 1
            if fields[35].strip():
                n_hs_cap += 1
    print(f'{year}: {n_total:,} lines, {n_hs_cap:,} with a non-blank field[35] (hs_cap_loss)')
"
```

(Adjust the delimiter/parsing above if the real AJR CSVs use a different quoting convention than a plain split(',') — check one real line by eye first if unsure; this is a sanity preview only, not a substitute for the loader's own real parsing logic in `ears_format.py`/`load_ajr.py`.)

### (b) Run the AJR backfill live, for all three years in one pass

**Run this only after Task 1's guard (already applied to the working tree, per this report) has been deployed to production** — running the backfill before the guard ships would just have the next `load_certified_historical.py`/`load_certified_2025.py`/`load_2026_preliminary.py` run re-clobber it, per PX-20260910-01's original finding.

```bash
cd ~/Desktop/Claude\ Files/parcel_app   # or wherever the production checkout lives
python3 loaders/load_ajr.py --county TRAVIS
```

This single command processes **all four** configured AJR years (2021, 2022, 2023, 2024) — including 2021, which is already correct and unaffected by this bug; re-running it is safe and idempotent (same file, same real value re-written, no-op in effect). It ends by calling `parcel_rollup.run()` for each processed year (`load_ajr.py:235-239`), so `parcel_tax_year.hs_cap_loss` is re-derived from the repaired `prop_unit_tax_year` rows in the same run — no separate rollup step needed.

### (c) Re-measurement: prove the fix, both tables, all affected years

```bash
python3 loaders/field_coverage_gate.py --county TRAVIS --tax-year 2022
python3 loaders/field_coverage_gate.py --county TRAVIS --tax-year 2023
python3 loaders/field_coverage_gate.py --county TRAVIS --tax-year 2024
```

(Dry-run/print-only by default — `field_coverage_gate.py`'s own `--dry-run` defaults to `True`; add `--live` only if you also want the measured coverage written to `county_field_coverage`, which requires `assert_production_db()` to pass first, per `field_coverage_gate.py:437-450`.)

**Expected result:** `hs_cap_loss` coverage for 2022/2023/2024 should land at roughly the same ~79-80% level `prop_unit_tax_year` already shows for 2021 (per PX-20260910-01's confirmed facts table: 78.7%/80.3%), not the 99.9% `data_coverage.py` currently (and incorrectly) claims, and not the 0.00% currently measured live. A result near 79-80% (not near-100%, and not still 0%) is consistent with AJR's own real, historical field[35] coverage for those years — this is the expected honest result, not a bug if it isn't exactly 99.9%. If the re-measurement instead comes back 0% again, the guard did not ship correctly or the backfill did not run against the same database this measurement reads from — stop and re-check, don't assume success.

A direct row-level spot check is also available if you want stronger confirmation than the aggregate coverage percentage:

```sql
SELECT tax_year, COUNT(*) FILTER (WHERE hs_cap_loss IS NOT NULL) AS non_null, COUNT(*) AS total
FROM prop_unit_tax_year
WHERE county_code = 'TRAVIS' AND tax_year IN (2022, 2023, 2024)
GROUP BY tax_year ORDER BY tax_year;
```

(This mirrors the exact query shape PX-20260910-01's "Confirmed facts" table used, so the before/after numbers are directly comparable.)

### (d) After confirming the fix, update `data_coverage.py`

Not part of this brief's code changes (out of scope — flagged for a future, separate step, since `data_coverage.py`'s own docstring says its numbers must be "SEEDED directly from confirmed live-DB queries Diego ran himself," not edited from this sandbox). Once step (c) above confirms the real post-backfill percentage, `HS_CAP_LOSS_COVERAGE[2022]`/`[2023]`/`[2024]` in `data_coverage.py:37-40` should be updated to match — currently `0.999` for all three, which was already confirmed wrong twice over (first by PX-20260910-01's investigation, and it will remain wrong even after the fix, since the real AJR-based coverage is ~79-80%, not 99.9%). `[PM ruling needed]`: who updates this constant and when (immediately after backfill, or bundled into a later broader `data_coverage.py` reconciliation).

---

## Confirmation: scope boundaries respected

- **`land_value` / `imprv_value` — untouched.** These two columns' `SET` clauses in `PROP_UNIT_TAX_YEAR_UPSERT_SQL` are unchanged (`ears_format.py:488-489`, still bare `EXCLUDED.land_value` / `EXCLUDED.imprv_value`). They don't need this guard — `load_certified_historical.py`'s own `load_land_imprv()` step already repairs them from `LAND_DET.TXT` after the initial write, a mechanism PX-20260910-01 confirmed has no `hs_cap_loss`-equivalent source to repair from, which is exactly why `hs_cap_loss` needed a different fix (the guard) instead.
- **Every other column in the same `SET` clause** (`geo_id`, `market_value`, `assessed_value`, `taxable_value`, `exemption_codes`, `data_source`) — untouched, still unconditional overwrite, unchanged behavior.
- **No other SQL constant touched.** `PROP_UNIT_UPSERT_SQL` (the `prop_unit`-table upsert, with its own, pre-existing geo_id guard from an earlier fix) is untouched by this brief.
- **Dallas (`load_dallas_certified.py`) — not touched**, per the brief's explicit scope boundary. Its interaction with the now-shared guard is flagged (see Task 1 above and the code comment at `ears_format.py:456-467`) but not fixed; that's PX-20260910-02's scope.
- **No commit, no push, no branch created** — changes exist only in the working tree, exactly as with every prior production-adjacent change in this engagement.
- **No production write, no live DB access performed by this agent** — Task 3's runbook is a command list for Diego to run himself, synchronously, same pattern as every prior live change in this project.

---

## Summary

Applied the PM's ruling exactly: a single-column `COALESCE` guard on `PROP_UNIT_TAX_YEAR_UPSERT_SQL`'s `hs_cap_loss` clause (`ears_format.py:487`), verified caller-by-caller (four of five callers have no legitimate case for wanting to clear the field; the fifth, Dallas, is flagged as a real but out-of-scope edge case), five new fixture tests covering all three required directions plus a shipping-SQL string assertion (all passing), zero regressions across the full 37-test `test_ears_format.py` suite and the 6/6 offline verification suite, and a command-list runbook for Diego covering path validation, the live AJR backfill, and post-fix re-measurement. Nothing committed, nothing pushed, no production write or live DB access performed by this agent.
