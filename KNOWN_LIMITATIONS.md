# Known Limitations — Parcelytics Phase 1

## Data Coverage

### taxable_value (2021–2024): Not available
- `taxable_value` is only populated for **2025** (source: TCAD Certified Appraisal Export, PROP_ENT.TXT).
- The Texas Comptroller AJR files (2021–2024) do not include taxable value. AJR carries market value, assessed value, and homestead cap loss only.
- **Resolution:** Would require historical Certified Export files (EARS format) for 2021–2024 from TCAD. Not currently in scope.

### land_value / imprv_value (2021–2024): Not available
- Land and improvement value breakdown is only populated for **2025**.
- AJR files do not carry segment-level land/improvement detail.
- **Resolution:** Same as above — requires historical Certified Exports.

### hs_cap_loss (2025): Not available
- The 2025 Certified Export (PROP.TXT / PROP_ENT.TXT) does not carry a homestead cap loss field.
- `hs_cap_loss` is populated for **2021–2024** from AJR field[35].
- The investor insight report and projection engine look for the most recent year with hs_cap_loss data rather than hardcoding to 2025, so homestead cap warnings still fire correctly from AJR data.

### tax_billing detail (2021–2024): Not available
- Full billing detail (`total_tax`, `total_paid`, `total_due`) is only available for **2025** (source: Travis County Tax Office TaxCurOpenData).
- Pre-2025 rows in `tax_billing` are delinquent-only records sourced from TaxDelqOpenData — they do not represent the full billing population for those years.
- **Resolution:** Would require historical TaxCurOpenData exports from the Travis County Tax Office for each prior year. Not currently in scope.

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
