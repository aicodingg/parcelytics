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
