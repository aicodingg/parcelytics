# Known Limitations — Parcelytics Phase 1

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

## Known Data Anomalies

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
