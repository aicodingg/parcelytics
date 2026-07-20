# Known Limitations — Parcelytics Platform
*Last updated: June 23, 2026*

## Data Coverage

### taxable_value, land_value, imprv_value (2021–2024): RESOLVED ✓
**TCAD PIR R010172-062126 — CLOSED**

Historical Certified Appraisal Exports for 2021–2024 have been obtained and loaded. All three fields are now populated for those years.

| Year | data_source | Method | land/imprv coverage |
|------|-------------|--------|---------------------|
| 2021 | `cert_2021` | PDF extraction (`parse_cert_2021_pdf.py`) | **90%** — two-column PDF layout collapses some land segments; personal property accounts (P-type) are null by design |
| 2022 | `cert_2022` | EARS fixed-width TXT (same format as 2025) | **99.9%** |
| 2023 | `cert_2023` | EARS fixed-width TXT | **99.9%** |
| 2024 | `cert_2024` | EARS fixed-width TXT | **99.9%** |

The 2021 PDF constraint (90% vs 99.9%) is a one-time limitation. TCAD only had a printed/PDF certified roll for 2021; structured TXT exports are available from 2022 onward. No further action needed on this item.

### hs_cap_loss (2025): Not available
- The 2025 Certified Export (PROP.TXT / PROP_ENT.TXT) does not carry a homestead cap loss field.
- `hs_cap_loss` is populated for **2021–2024** from AJR field[35].
- The investor insight report and projection engine look for the most recent year with hs_cap_loss data rather than hardcoding to 2025, so homestead cap warnings still fire correctly from AJR data.

### tax_billing detail (2021–2024): Partially available via portal scrape

Two sources available — use the better one when it arrives:

#### Source 1 — Portal payment receipts (`data_source = 'portal_scrape'`, `confidence_level = 'partial'`)
- The Travis County Tax Office portal at `travis.go2gov.net` exposes per-property payment receipt history going back to at least 2012.
- `loaders/scrape_billing_history.py` scrapes `total_tax = total_paid = payment_amount` for 2021–2024 using a single-threaded, rate-limited (~0.75 s/request) approach.
- **Data integrity caveat:** Payment amounts reflect what was *paid*, not necessarily what was *levied*. Deferrals, partial payments, and supplemental billings can cause them to differ. Stored with `confidence_level = 'partial'` — the UI should show an amber "Partial" badge for these rows, not a green "Verified" badge.
- **Entity-level breakdown not available** from this source — only the total paid amount per year. `total_due`, `billing_num`, and `exemption_codes` are NULL on portal_scrape rows.
- The upsert in `scrape_billing_history.py` protects existing better-quality rows: any row with `data_source NOT IN (NULL, 'portal_scrape')` (i.e. `'taxcur'` or `'pir_billing'`) is never overwritten.
- Run order: `--test` (500 parcels, verify sanity check) → full run → `--resume` if interrupted.

#### Source 2 — Travis County Tax Office bulk export (when received)
- Open Records Request submitted **June 21, 2026** via email to TaxOffice@TravisCountyTX.gov. Requesting TaxCurOpenData-format billing records for tax years 2021–2024. **Follow-up scheduled: June 30, 2026.**
- When bulk files arrive, load via `load_pir_billing.py` (already written and tested). That loader's upsert will replace any `portal_scrape` rows with authoritative data, and will also populate `tax_billing_entity` with entity-level breakdown.
- This is separate from the TCAD PIR — TCAD and the Travis County Tax Office are different agencies.

**Do not backfill from delinquent data or AJR sources** — they are structurally different datasets.

### living_area_sqft: Available from IMP_DET.TXT (loader written, pending run)
Building-area square footage by component is available in `IMP_DET.TXT` from the TCAD
Certified Export. Living-area component codes included: `1ST` (1st Floor), `2ND` (2nd Floor),
`3RD` (3rd Floor), `1/2` (Half Floor), `RSBLW` (Residence Below Grade), `FBSMT` (Finished
Basement). Excluded: `UBSMT` (unfinished basement) and all non-living components (garage,
carport, deck, etc.).

**Status:** `loaders/load_imp_det_sqft.py` + `loaders/migrate_add_sqft.py` written.
The migration adds `parcel.living_area_sqft NUMERIC(10,2)` (additive, IF NOT EXISTS).
Run order: `migrate_add_sqft.py` → `load_imp_det_sqft.py` (needs DB and CERT_DIR path).
After loading, the peer benchmark can be extended to per-SF comparisons.
The peer benchmark footnote in the property detail page notes "per-SF normalisation pending
loader ingestion" — remove that note once the column is populated.

### PID/Special-District entity codes missing from county_tax_rate (CORRECTNESS-CRITICAL)
**49 entity codes** in TaxCurOpenData billing data (2025) are absent from `county_tax_rate`.
These are primarily Public Improvement Districts (PIDs) administered by City of Austin and
Water Control and Improvement Districts (WCIDs) not in the rates XLSX.

**Impact:** The rate-based post-acquisition estimator (`/api/estimate_acq`) silently skips
entities with NULL rate. For ~12,142 parcels with PID charges, the buyer's estimated tax
is understated. The total missing billing in 2025 is **$38.05M** across 49 codes.

**What is CORRECT:** Historical billing display (amount_due from TaxCurOpenData) is correct
for all entities including PIDs. Seller's current tax in the estimator is correct
(sums amount_due directly). Only rate-based projections and buyer estimates are affected.

**Top missing codes by billing volume:**

| Code | Parcels | 2025 Billed | Category |
|------|--------:|------------:|----------|
| P2U | 2,558 | $14.1M | PID — Downtown Austin / 2nd Street |
| P2P | 806 | $2.7M | PID |
| P10T | 428 | $2.5M | PID |
| PWV | 942 | $2.3M | WCID — Point Venture |
| P11L | 222 | $2.2M | PID |

See `ENTITY_CODE_AUDIT.md` for the full table and recommended fix.

**Fix:** After the estimator session merges, apply a "billing-only pass-through" in
`api_estimate_acq`: for entities with `amount_due` but NULL `rate`, carry the prior-year
billed amount forward as a pass-through estimate labeled "PID/Special District assessments
(from prior billing, not rate-computed)". See `ENTITY_CODE_AUDIT.md` for implementation.

### tax_billing.total_tax (2025): 0.00 for ~93% of rows — source data, not a Parcelytics bug, but NEEDS REVIEW on two read sites

Confirmed by direct inspection of the raw `TaxCurOpenData (1).csv` (the file
`loaders/load_tax_current.py` loads as-is, no transformation): for a
representative sample across the whole file, **93.3%** of 2025 rows have
`TOTAL_TAX = "0.00"` and `TOTAL_DUE = "0.00"` in the source CSV itself, while
the same row's `ENTITY*`/`DUE*` columns carry real, nonzero per-entity
amounts. Example — `0100030105` (1201 S Lamar): source `TOTAL_TAX=0.00`,
`ENTITY1=IAU DUE1=40080.27` (just the first of several entities). Only ~3.5%
of 2025 rows have a populated, correct `TOTAL_TAX` that matches the entity
sum. **This is a Travis County Tax Office open-data quirk, not a Parcelytics
loader bug** — `load_tax_current.py` writes `TOTAL_TAX` straight from the
source field with no transformation, and the pattern is present in the raw
file before it ever reaches the database. It is **not** narrowly scoped to
"some property types (commercial, multi-family)" as an existing code comment
in `compute_metrics.py` suggests — it affects the large majority of all 2025
billing rows regardless of type.

**Where this is already handled correctly:**
- `compute_metrics.py` never reads `tax_billing.total_tax` for its
  calculations — it sums `tax_billing_entity.amount_due` instead (see the
  comment at the `effective_tax_rate` CASE block). `parcel_metrics` and
  `county_benchmark` are unaffected.
- `app.py`'s single-property page (`current["total_tax"]`) has a fallback —
  `if current is not None and not current.get("total_tax") and entity_detail:` —
  that backfills from the entity sum. Because Python treats `0` and `None` as
  equally falsy, this fallback fires correctly for the 0.00 case even though
  the comment above it was written describing blanks/NULLs, not 0. **This
  works today but is fragile**: a future refactor to an explicit `is None`
  check (often considered better practice) would silently break it and
  reintroduce a $0 display for ~93% of parcels. Worth a comment update at
  minimum.
- `templates/compare.html` (`/compare` route) checks `p.billing.total_tax`
  truthiness directly (`{% if ... and p.billing.total_tax %}`) — for the 93%
  case this is falsy, so it correctly falls back to "—" rather than showing
  a misleading $0. It does **not** backfill from `tax_billing_entity` the way
  the single-property page does, so the Compare page's "Total Tax" /
  "Effective Tax Rate" rows show "—" for the large majority of parcels even
  though the real figure is knowable. Safe, but incomplete.

**Where this is NOT handled correctly — live bug, not yet fixed:**
- `/api/peer_benchmark_local/<geo_id>` (`app.py` ~line 1746) queries
  `tb.total_tax` directly for the peer set and filters
  `taxes = sorted([float(r["total_tax"]) for r in peers if r.get("total_tax")])`
  — this silently drops ~93% of peers from the tax statistic (0 is falsy),
  with no fallback to `tax_billing_entity`. The resulting `peer_tax.median` /
  `p25` / `p75` are computed from a small, non-random ~7% slice of the peer
  set (whichever peers happen to have a populated source `TOTAL_TAX`), not
  the full comparable pool that `peer_mv`/`peer_av` use. This is displayed
  in `templates/property.html` as the "Peer Median Tax" chip and "Total Tax
  (2025)" row in the Peer Set table (~line 2776, 2801–2803) with **no
  confidence label or sample-size caveat** — it looks like a normal verified
  median to the user, investor or homeowner. **Not fixed as part of this
  note** — flagged for review per the Data Integrity Standard; the fix would
  mirror the property page's pattern (derive from `tax_billing_entity` when
  `tax_billing.total_tax` is 0/NULL) but touches a live query path and
  should be scoped and reviewed as its own task.

### 2025 tax_billing: 54,115 parcels with NO billing row at all (distinct from the 0.00-total issue above)

Separate from the "`total_tax` = 0.00 for ~93% of rows" issue above (those
parcels DO have a `tax_billing` row, just with a bad total) — this is a
distinct set of 54,115 parcels with no 2025 `tax_billing` row whatsoever.
`prop_type_cd`/`state_cd1` breakdown of the gap (confirmed against the
population, not a sample): `L1` (Commercial Personal Property) = 31,452,
`A` (Single-Family Residential) = 10,715, `M1` (Mobile Home, personal-
property variant) = 5,988, plus ~20 smaller categories filling out the rest.

**`state_cd1 = 'A'` sub-population — RESOLVED, confirmed genuinely absent
from the source, not a loader bug.** A live pull of 50 real "A" rows
confirmed ordinary, established, valued single-family homes
(`data_source='certified'`, market/taxable values $355K–$618K, taxable
value close to market value) — ruling out new-construction and full-
exemption explanations. `loaders/check_geo_ids_in_taxcur_source.py` (a
read-only diagnostic — never opens a DB connection, only scans the raw
`TaxCurOpenData` CSV against a supplied geo_id list) checked all 10,715
`A`-coded geo_ids against the source file, decomposing each row's raw
`PARCEL` field into every possible 10-char substring so a mismatched offset
or padding convention would still be caught. Result: **zero fuzzy matches —
all 10,715 came back `NOT_FOUND`, in no form, anywhere in the file.** These
accounts are genuinely not present in Travis County's current-year billing
extract. Not a Parcelytics matching/parsing bug — this needs a conversation
with the county (why are 10,715 ordinary residential accounts absent from
the current-year billing file?), not a code fix.

`L1`/`M1` (personal property / mobile-home-as-personal-property, ~69% of
the gap combined) remain a plausible but **not yet confirmed** structural
explanation — Travis County may bill these under an account-numbering
scheme distinct from TCAD's certified-export `geo_id`, which the loader's
`PARCEL[:10]` convention wouldn't catch even if they are billed. Not yet
run through the same fuzzy-match check as the `A` set.

### Address search: parcels with blank situs_address are reachable only by account number (July 2026)

`search_parcels_by_address()` (`app.py`, backing both the `/` route and
`/api/address_search` — see the Search overhaul Phase 2 build, July 2026)
matches only against `parcel.situs_address`. Investigation of the 2021 AJR
file (`227000`-entity rows, 472,403 total) found **0.80% of parcels have a
completely blank `situs_address`** — no street text at all, not just a
missing city/zip. These accounts (and any 2025/2026-only parcels never
present in an AJR file, which would inherit the same gap since
`situs_address` is written only by `load_ajr.py`'s upsert) cannot be found by
typing any address text into any of the four search boxes, no matter how the
query is phrased — there is nothing there to match against.

**Workaround, unaffected by this limitation:** these parcels remain fully
reachable by TCAD account number (geo_id) or prop_id, in every search box and
via direct URL (`/parcel/<geo_id>`). `resolve_exact_parcel()` has no
dependency on `situs_address`.

**Not fixed as part of the Phase 2 build** — out of scope per the go-ahead
brief (D5: document, don't build). A future fix would need a fallback match
against another field (e.g. owner_name, legal_desc) for the blank-address
population specifically, which is a separate scoping decision.

### tax_billing.data_source / confidence_level — write-time fix (July 2026)

`load_tax_current.py` now tags every row it writes with
`data_source='taxcur_current'` and `confidence_level` = `'verified'`
(source `TOTAL_TAX` genuinely populated), `'derived'` (source total was the
0.00 quirk above; real total is the entity-DUE sum instead, corrected at
write time), or `NULL` (no usable total at all). This moves the
verified/derived distinction `app.py` previously re-derived on every page
load into the data itself — see `app.py`'s `is_billing_verified` /
`total_tax_derived` computation, now a direct read of these columns instead
of a live recomputation. A one-time backfill
(`loaders/backfill_tax_billing_2025_confidence.py`) applied this tagging to
rows that predate the fix. The loader also gained a `--dry-run` mode (parses
and classifies the whole file, zero DB writes, no DB connection opened at
all unless combined with `--new-only`) and a `--new-only` mode (skips any
`(geo_id, tax_year)` already tagged with a `data_source`, so a catch-up load
against a grown source file only adds genuinely new rows instead of
unconditionally re-writing everything already correctly tagged — the
loader's upsert has no protective `WHERE` guard the way
`scrape_billing_history.py`'s does, so this was worth adding deliberately
rather than assuming a full rerun is safe).

## Out of Scope for Phase 1

The following were explicitly excluded from this phase and should not be backfilled without a separate scoping decision:

- ~~Historical taxable values (2021–2024)~~ — **RESOLVED** via cert_2021–cert_2024 loads
- ~~Historical land/improvement breakdown (2021–2024)~~ — **RESOLVED** via cert_2021–cert_2024 loads
- Historical full billing detail (2021–2024) — **partially covered via portal scrape** (`portal_scrape`/`partial`); authoritative bulk data pending Travis County response (request submitted June 21, 2026)
- AJR supplemental roll data (supplements to the main certified roll)
- Protest/ARB history (available in Certified Export ARB.TXT but not loaded)
- Agent/owner contact data (available in Certified Export AGENT.TXT but not loaded)

## 2026 Preliminary Appraisal Data

### What is loaded
The June 9, 2026 TCAD Preliminary Appraisal Export (Supp 0) has been loaded for tax year 2026. This includes:
- **PROP.TXT**: market value, assessed value, owner, property type for all parcels
- **PROP_ENT.TXT**: entity-level taxable value, exemption amounts (HS, OV65, DP, DV, and others)
- **LAND_DET.TXT**: land value per parcel; improvement value derived as max(0, market − land)
- **SB12.TXT**: over-65 Senate Bill 12 freeze exemption detail — richer than any prior year's data; flagged in exemption_codes as "SB12"

### What is NOT in 2026 data
- **No billing amounts**: tax amounts due/paid/levied require the certified tax roll, which is produced after the July 25, 2026 certification date. 2026 billing will not be available until late 2026 or early 2027 when the Tax Office processes the roll.
- **No hs_cap_loss field**: the Certified Export format (shared with 2025) does not carry a cap-loss field. Homestead cap detection uses 2021–2024 AJR data for historical context.

### Certification date
TCAD certifies the 2026 appraisal roll on **July 25, 2026**. Values in the preliminary export may be revised before then (ARB protests, corrections). The UI flags all 2026 values with a "Preliminary" badge. The `data_source` field is `'preliminary'` in the database to distinguish from `'certified'` (2025).

### Scheduled future action
After July 25, 2026: re-run the loader with the certified export to replace 2026 preliminary data with certified values. The "Preliminary" badge in the UI automatically flips to "Verified" when `data_source` changes to `'certified'`.

### Entity-level exemption detail (first time available for this platform)
The 2026 PROP_ENT.TXT entity-level detail is the first time this platform has per-entity exemption amounts for a full preliminary roll. This enables future per-entity taxable value analysis at a level of granularity not available from AJR sources.

---

## Known Data Anomalies

### AJR 2021–2024: assessed_value > market_value (systemic issue — 11.7% of parcels)
This is the most significant data quality issue in the platform. Deep investigation of raw 2021 AJR files (June 2026) found:

**What we know:**
- 11.74% of parcels (56,101 unique) show AV > MV in at least one AJR year (2021–2024).
- 2021 and 2022 show ~27–29K affected parcels each, with a modest average excess of $59–126K.
- 2023 and 2024 show ~5K parcels each, with a much larger average excess of $417–479K.
- field[33] in the 2021 AJR is non-empty for 2.72% of rows, confirming the format is not simply shifted.
- 20.8% of residential (A-type) parcels have field[35] values larger than their market value — impossible for homestead cap loss, indicating field[35] in 2021 represents something other than traditional cap loss for many records.

**Root cause assessment:**
- For residential and commercial parcels: Most likely **genuine TCAD source data encoding** in the 2021 AJR — values represent intermediate or prior-year appraisal states captured at the time of the AJR filing. This is NOT a loader field-position bug; field[32]=MV and field[34]=AV positions are confirmed correct.
- For agricultural parcels (D1/E) with 2x–3x excess: A field-swap is possible — the AJR may place productivity (ag use) value in field[32] and market value in field[34] for some ag parcel records. Cross-validation with 2022 raw files would confirm or deny this, but 2022 raw files are no longer available for inspection.
- For 2023–2024 large commercial anomalies: Likely individual valuation errors or ARB outcomes reducing market value without a corresponding assessed value reduction.

**What is displayed:** All values shown as-is (Data Integrity Standard Rule 1). Rows where AV > MV in any year show an amber `!` data anomaly badge next to the assessed value with a tooltip explaining the issue.

**What is NOT affected:** The projection CAGR uses `market_value`, not `assessed_value`. For the ~90% of baseline-year anomalies where `market_value ≥ $50K`, the MV appears valid and the CAGR calculation is unaffected. Only the assessment_ratio metric (computed by compute_metrics.py) is inflated for these rows.

### 2023: assessed_value > market_value for some parcels
Some parcels show assessed value exceeding market value in 2023. This is real TCAD data, not a loader bug. It typically reflects a valuation protest outcome that reduced the market value certification mid-cycle without a corresponding reduction in assessed value. The data is stored as-is for accuracy; no correction is applied.

### 2025: market_value and assessed_value are equal for most parcels
This is expected behavior. The 2025 Certified Export reflects full market value assessment; homestead cap adjustments that would create a gap are not surfaced in the PROP.TXT / PROP_ENT.TXT fields loaded here.

### Multi-family parcels with large market/assessed gaps (2021–2024)
Some multi-family properties show assessed values far below market value in AJR data. This reflects tax agreements, exemptions, or valuation caps that may have expired. The data is stored as-is. The investor insight report surfaces these gaps as "Assessment Gap" notes.

### AJR: hs_cap_loss values exceeding market_value
A small number of AJR parcels carry `hs_cap_loss` values larger than their `market_value` (e.g., geo_id `0284460113` shows hs_cap_loss of $72.97M against a 2024 market value of $36.04M). This is mathematically impossible for a genuine homestead cap and indicates a source data error in the AJR file. These records are stored as-is. The investor insight report will still surface a homestead cap warning for these parcels, but the cap loss figure should be treated with caution.

### AJR: market_value = 0 or missing for some parcels
Some AJR parcels have `market_value = 0` or NULL for specific years (e.g., `0284460113` shows market_value = 0 for 2022). This is a source data issue in the AJR file, not a loader bug. Parcels with no market value in a given year are excluded from CAGR and appreciation calculations in the insight report.

### 2025 Certified Export: ~9% of parcels have no land/imprv data
44,114 of 479,181 2025 parcels (9%) have no `land_value` or `imprv_value`. These are accounts that do not appear in `LAND_DET.TXT` — typically exempt properties (churches, government), mineral accounts, or personal property accounts that have no land segments in the certified export. This is expected behavior, not a loader bug.

### classi_cd source: IMP_INFO.TXT (improvement-level), not the parcel master record
`classi_cd` (the TCAD numeric use code displayed in Property Info) is sourced from `IMP_INFO.TXT`, not from `PROP.TXT` or the parcel master record. The loader selects the highest-value non-`"00"` improvement row per parcel as the property-level use code (`backfill_classi_cd.py`). This means:

- **Vacant land and some agricultural parcels** have no improvement records in `IMP_INFO.TXT`. For these, `classi_cd` will be NULL and the UI falls back to displaying the Comptroller `state_cd1` code instead. This is expected and correct behavior — vacant land has no improvement-level classification.
- **Multi-improvement parcels** (e.g., a commercial property with a main building plus a secondary structure) are represented by their highest-value improvement's code. This may occasionally understate secondary uses.

### Comptroller class / TCAD improvement code disagreement (example: 0284460113)
Parcel `0284460113` carries `state_cd1 = A` (Comptroller residential single-family classification) but `classi_cd = 08` (TCAD improvement code for "Apartment 100+ Units"). The Comptroller's aggregate roll code and TCAD's parcel-level improvement record disagree on property use. The UI displays the TCAD `classi_cd` when available (more specific), falling back to `state_cd1` only when `classi_cd` is NULL. Investors should be aware that these two classification systems can diverge, particularly for parcels with non-standard uses or recent rezoning.

### state_cd1 prefix population (517,614 total parcels as of June 2026)
Full prefix breakdown from `query_state_cd1_prefixes.py`:

| Prefix | Count | % | Notes |
|--------|------:|---:|-------|
| A | 334,227 | 64.6% | Residential single-family |
| L | 42,504 | 8.2% | Personal property (equipment, inventory, business personal property) — **not** commercial real estate; see the July 2026 correction below. Excluded from benchmarks as of the classify.py fix. |
| C | 38,719 | 7.5% | Land / vacant |
| O | 19,986 | 3.9% | Other real property (real estate, kept in benchmarks) |
| NULL | 17,175 | 3.3% | No state_cd1 — see note below |
| F | 15,132 | 2.9% | Commercial improved |
| X | 13,998 | 2.7% | Tax-exempt (churches, government — excluded from benchmarks) |
| B | 12,981 | 2.5% | Multi-family residential |
| M | 10,699 | 2.1% | Manufactured homes (real property under TX law, kept in benchmarks) |
| D | 5,078 | 1.0% | Agricultural |
| E | 4,831 | 0.9% | Rural / open space |
| J | 1,524 | 0.3% | Industrial / utility real property |
| S | 751 | 0.1% | State-assessed utility real property |
| G | 6 | 0.0% | Government-assessed |
| N | 3 | 0.0% | Personal property (excluded from benchmarks) |

**No unrecognized prefixes were found** — all 517,614 parcels use standard Texas Comptroller codes.

**Benchmark exclusion policy (as implemented in `compute_metrics.py` and `/api/benchmark`):**
- Excluded via `BENCHMARK_EXCLUDE_PREFIXES`: `X` (tax-exempt, 14K parcels) and `N` (personal property, 3 parcels).
- Also excluded, structurally — via `tax_logic/classify.py`'s `label_case_sql()` / `property_type_label()` simply not mapping these prefixes to any of the 5 benchmark categories (falls through to NULL, not on the exclude-list, but never produces a row either way): `O`, `G`, `J`, and — as of the July 2026 correction below — `L`.
- Genuinely kept (map to a real benchmark category): `A`, `B`, `C`, `D`/`E`, `F`, and `M` (manufactured homes — real property).
- `NULL` parcels (17,175) are naturally excluded because NULL doesn't match any `label_case_sql()` WHEN clause either.

**NULL state_cd1 parcels:** The 17,175 parcels with no state_cd1 are the same population as those with no `neighborhood_cd` — both fields come from AJR and are absent for accounts not included in the aggregate EARS/AJR roll (mineral accounts, personal property accounts, and some exempt special-use accounts that have no real-property segment). These accounts do not appear in any benchmark calculation.

### neighborhood_cd field: TCAD alphanumeric codes
`neighborhood_cd` is populated from AJR field[16] (loaded by `load_ajr.py`). Coverage as of June 2026: **96.7% of parcels** (500,439 of 517,614). The 17,175 NULL parcels are the same as the NULL state_cd1 group above.

The values are TCAD internal neighborhood codes (e.g., `A7100`, `J3000`, `B0810`) — not human-readable neighborhood names. These codes represent TCAD appraisal neighborhoods used for mass appraisal uniformity; they do not correspond to public neighborhood names or zip codes. The Benchmark filter uses them as-is for grouping — an investor can filter to parcels with the same TCAD appraisal neighborhood as the subject parcel, which is a meaningful comparability filter for mass-appraisal context.

Top 5 codes by parcel count: `A7100` (3,462), `A5850` (3,272), `J3000` (2,960), `J3100` (2,948), `B0810` (2,903).

### AJR* personal property supplement accounts — permanent exclusion policy

**What AJR* accounts are:** `geo_id` values starting with `AJR` (e.g., `AJR929676`, `AJR963828`) are **personal property supplement records** loaded from the TCAD AJR file, not real estate parcels from the Certified Appraisal Export. They are commercial personal property accounts (equipment, inventory, etc.) supplementally carried in the AJR. They carry `market_value = $1` as a placeholder in certified data and receive real assessed values in the preliminary roll.

**Why they distort benchmarks:** A $1 certified base value produces percentage changes of 115,000%–258,000,000% when a real preliminary value is assigned — pure arithmetic artifacts. These accounts produced a mean commercial MV change of **6,084%** (vs. 0.00% median) before the fix. After exclusion, the commercial mean converges to within single-digit percentage points of the median.

**Platform-wide exclusion policy** (applied at query time, per Data Integrity Standard Rule 1 — data stored as-is):

| Context | Exclusion applied |
|---------|------------------|
| `/snapshot` county comparison — type breakdown | `AND p.geo_id NOT LIKE 'AJR%%'` |
| `/snapshot` county totals | `AND t.geo_id NOT LIKE 'AJR%%'` / `AND geo_id NOT LIKE 'AJR%%'` |
| `/api/benchmark` live aggregation (all years incl. 2026) | `AND p.geo_id NOT LIKE 'AJR%%'` via `excl_filter` |
| `compute_metrics.py` county_benchmark INSERT | `AND p.geo_id NOT LIKE 'AJR%%'` |
| `query_2026_vs_2025.py` analysis script | `AND p.geo_id NOT LIKE 'AJR%%'` |

**Not excluded:** Parcel search and property detail pages include AJR* accounts — if someone searches for an AJR* geo_id directly, they can view the record. The exclusion applies only to aggregate/benchmark/comparison contexts.

**Impact of exclusion on county_benchmark (verified June 22, 2026):** 32,576 AJR* F/L accounts were removed from the commercial benchmark source. The commercial parcel_count dropped from ~46,103 to **13,527** real estate parcels. The Residential count was unaffected (317,461 parcels).

**Residual outliers after AJR* exclusion (verified June 22, 2026):** After excluding AJR*, the commercial 2025→2026 mean is +19,085% vs. a median of +6.49%. A further 27 non-AJR* F/L parcels have >500% increases. Investigation via `query_remaining_outliers.py` found these are **not** placeholder artifacts — 26 of 27 have real 2025 certified values and represent genuine large commercial reappraisals by TCAD. Notable examples:

| geo_id | Address | MV 2025 | MV 2026 | Change |
|--------|---------|---------|---------|--------|
| 0134180201 | 4408 Long Champ Dr | $15.9M | $111.9M | +602% |
| 0331310602 | 6005 S FM 973 | $5.6M | $55.2M | +878% |
| 0430130406 | 707 W Slaughter Ln | $2.6M | $36.4M | +1,312% |
| 0200160205 | 5119 E 7th St | $7.1M | $51.1M | +617% |

Only `0275010202` (Howard Ln) had a true $1 base. Excluding mv_2025 ≤ $100 brings the mean to +20.66%, which still reflects actual large commercial reappraisals. **No further exclusions are warranted** — these are legitimate TCAD valuations. The **median (+6.49%)** is the appropriate summary statistic for commercial; the mean is not meaningful given this distribution. Note: geo_id `2-001470-0` uses a non-standard format (possibly a utility or complex account) and warrants manual review if its commercial comparability is needed.

**The one prominent real-property outlier in the 2026 data:** Geo_id `0275010202` (HOWARD LN TX 78728) is a regular 10-digit TCAD F2 parcel with classi_cd=61. It went from $1 MV (2025 certified) to $2,588,746 MV (2026 preliminary) — a new lot receiving its first appraisal. Confirmed: not an AJR* account; no AV>MV anomaly in 2026 (AV=$1 < MV=$2.6M); included in the 2026 Commercial benchmark. Note: the 2026 preliminary export left AV and TV at $1 (matching the 2025 certified values) — the assessed and taxable values for this lot have not been finalized in the preliminary roll. The `risk_large_value_jump` flag fires for this parcel (expected).

### July 2026 correction: state_cd1='L' is Personal Property, not Commercial real estate

The state_cd1 table above (and this file's original characterization of "L\* — commercial real estate") was wrong. **L1/L2 is the Texas Comptroller's own Personal Property classification** (equipment, inventory, business personal property) per the Comptroller's PTAD state class code scheme — not Real Property. `tax_logic/classify.py` previously mapped `"L"` to `"Commercial"` in `_STATE_PREFIX_LABEL`, which put personal property into a "Commercial real estate" benchmark on the merits, independent of the separate AJR\* synthetic-geo_id issue documented above.

**Quantified before fixing (raw source files, not guessed):** of the 42,293 `state_cd1='L'` geo_ids found across all 4 AJR years (2021–2024):
- 42,082 (99.5%) already carried the synthetic `AJR`-prefixed geo_id and were already excluded from `county_benchmark` by the existing `geo_id NOT LIKE 'AJR%%'` filter — no incremental impact from these.
- 211 (0.5%) had a real, resolvable 10-digit geo_id and were **not** caught by the AJR\*-prefix exclusion. Of those 211: 196 are confirmed personal-property accounts (`prop_type_cd='P'` in the 2025 Certified Export's `PROP.TXT`), all with a real, nonzero 2025 `market_value` (range $32–$2,115,623,520, median $313,546) and none carrying a `classi_cd` improvement-override (zero of them appear in `IMP_INFO.TXT`, so none can hit `MULTI_FAMILY_CODES`/`COMMERCIAL_CODES` and land back in Commercial that way). The other 15 don't match any `PROP.TXT` record at all (likely closed/superseded accounts). **Zero of the 211 are real property** (`prop_type_cd='R'`) — there was no meaningful "legitimate commercial real estate coded L" population being protected by the old mapping.

**Fix:** `"L"` removed from `tax_logic/classify.py`'s `_STATE_PREFIX_LABEL` dict and from `label_case_sql()`'s SQL CASE, given the same treatment as `J`/`O`/`G` (falls through to `None`/NULL, excluded from every benchmark category rather than forced into Commercial). This is a classification-level fix, not a loader-level patch — it structurally excludes all current and future L1/L2 rows regardless of which loader or geo_id-resolution path produced them, unlike the AJR\*-prefix filter above which only catches rows that failed geo_id resolution specifically.

**Scope note:** this does not touch `app.py`'s separate `_snapshot_taxonomy_sql()` (the newer 8-tab-plus-Other Market Snapshot taxonomy) — that taxonomy's own state_cd1 fallback never included `F`/`L` in the first place, so unclassified L-prefix parcels already landed in its "Other" tab, not any real-estate sector tab.

## Build Workflow

See BUILD_WORKFLOW.md for the actual step-by-step process used to build and
verify changes to this project (Claude writes a brief, Cowork implements,
Diego verifies live, then commits).
