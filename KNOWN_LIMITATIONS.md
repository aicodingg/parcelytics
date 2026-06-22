# Known Limitations — Parcelytics Platform
*Last updated: June 22, 2026*

## Data Coverage

### taxable_value (2021–2024): Not publicly available — requires PIR
- `taxable_value` is only populated for **2025** (source: TCAD Certified Appraisal Export, PROP_ENT.TXT).
- The Texas Comptroller AJR/EARS files (2021–2024) do not include taxable value. AJR carries market value, assessed value, and homestead cap loss only.
- **TCAD only publishes the current year's Certified Export** on their public information page (traviscad.org/publicinformation). Historical Certified Export files for 2021–2024 are **not available for public download**.
- **Path to resolution:** Submit a Public Information Request (PIR) directly to TCAD:
  - Online portal: https://traviscad.govqa.us/WEBAPP/_rs/SupportHome.aspx
  - Email: CSInfo@tcadcentral.org
  - Request: "Certified Appraisal Export files (PROP.TXT and PROP_ENT.TXT) for tax years 2021, 2022, 2023, and 2024 in EARS fixed-width format." The 2025 export we already have came from this same source and TCAD does retain prior-year exports internally.
- Do not attempt to estimate or derive taxable_value from market/assessed values — it depends on exemption amounts which vary per entity per parcel.

### land_value / imprv_value (2021–2024): Not available
- Land and improvement value breakdown is only populated for **2025**.
- AJR files do not carry segment-level land/improvement detail.
- **Resolution:** Same as above — requires historical Certified Exports.

### hs_cap_loss (2025): Not available
- The 2025 Certified Export (PROP.TXT / PROP_ENT.TXT) does not carry a homestead cap loss field.
- `hs_cap_loss` is populated for **2021–2024** from AJR field[35].
- The investor insight report and projection engine look for the most recent year with hs_cap_loss data rather than hardcoding to 2025, so homestead cap warnings still fire correctly from AJR data.

### tax_billing detail (2021–2024): Hard limit — no public archive exists
- Full billing detail (`total_tax`, `total_paid`, `total_due`) is only available for **2025** (source: Travis County Tax Office TaxCurOpenData).
- Pre-2025 rows in `tax_billing` are delinquent-only records sourced from TaxDelqOpenData — they do not represent the full billing population for those years.
- **The Travis County Tax Office does not publish historical billing snapshots.** The open data portal (traviscountytx.gov/open-data-portal) provides only current-year billing and rolling delinquent data — there is no archive of prior-year TaxCurOpenData files available for public download.
- **Path to resolution:** Submit an Open Records Request to Travis County:
  - Portal: https://www.traviscountytx.gov/departments/records-request
  - Request: "TaxCurOpenData export files (current year billing roll) as of certification date for tax years 2021, 2022, 2023, and 2024." The Tax Office may or may not retain snapshots from prior years — this is genuinely uncertain and depends on their internal data retention policy.
- This is a hard limitation on publicly available data, not a gap in our loader. Do not attempt to backfill billing from delinquent data or AJR sources — they are structurally different datasets.

## Out of Scope for Phase 1

The following were explicitly excluded from this phase and should not be backfilled without a separate scoping decision:

- Historical taxable values (2021–2024)
- Historical land/improvement breakdown (2021–2024)
- Historical full billing detail (2021–2024)
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

**Residual outliers after AJR* exclusion:** After excluding AJR*, the commercial 2025→2026 mean is +19,085% vs. a median of +6.49%. A further 27 non-AJR* F/L parcels have >500% increases — these appear to be newly-platted lots or previously-unvalued parcels that received a $1 placeholder value in the 2025 certified export and their first real appraisal in 2026 preliminary. Investigation script: `query_remaining_outliers.py`. The median (6.49%) is the appropriate summary statistic; the mean is distorted by these extreme-base-value records and should not be used as a market indicator.

**The one prominent real-property outlier in the 2026 data:** Geo_id `0275010202` (HOWARD LN TX 78728) is a regular 10-digit TCAD F2 parcel with classi_cd=61. It went from $1 MV (2025 certified) to $2,588,746 MV (2026 preliminary) — a new lot receiving its first appraisal. Confirmed: not an AJR* account; no AV>MV anomaly in 2026 (AV=$1 < MV=$2.6M); included in the 2026 Commercial benchmark. Note: the 2026 preliminary export left AV and TV at $1 (matching the 2025 certified values) — the assessed and taxable values for this lot have not been finalized in the preliminary roll. The `risk_large_value_jump` flag fires for this parcel (expected).
