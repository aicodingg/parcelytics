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
| L | 42,504 | 8.2% | Commercial real estate |
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
- Excluded: `X` (tax-exempt, 14K parcels) and `N` (personal property, 3 parcels).
- Kept: all others including `M` (manufactured homes — real property) and `O` (other real property).
- `NULL` parcels (17,175) are naturally excluded because NULL does not match any `LIKE` pattern in the TYPE_GROUPS WHERE clause.

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
