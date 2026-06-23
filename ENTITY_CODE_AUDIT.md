# Entity-Code Completeness Audit
**Branch:** data/backend-followups  
**Date:** 2026-06-23  
**Status:** CORRECTNESS-CRITICAL finding — estimator under-reports tax for ~12,000 parcels

---

## Summary

The app uses two separate code systems for taxing entities:

| Source | Codes used | Where used |
|--------|-----------|-----------|
| TaxCurOpenData billing CSV | TDC codes (TCO, ACT, CAT, IAU, P2U…) | `tax_billing_entity.entity_code` |
| 2025RatesHistory XLSX | TDC codes (same format) | `county_tax_rate.entity_code` |
| PROP_ENT.TXT / ENTITY.TXT | Internal TCAD numeric codes (01, 02, 03, 2J…) | Not used in app logic |

The JOIN in `property_detail` and `api_estimate_acq` is:
```sql
LEFT JOIN county_tax_rate ctr
       ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = %s
```
When `ctr.rate IS NULL`, the entity is silently skipped in the rate-based estimate.

---

## Missing Entity Codes

**49 entity codes** in TaxCurOpenData billing with >$1 billed in 2025 are **absent from county_tax_rate**.

| Code | Parcels | 2025 Billed | Avg/Parcel | Category |
|------|--------:|------------:|-----------:|----------|
| P2U | 2,558 | $14,093,340 | $5,509 | PID — Downtown Austin / 2nd St District |
| P2P | 806 | $2,742,223 | $3,401 | PID |
| P10T | 428 | $2,488,597 | $5,814 | PID |
| PWV | 942 | $2,290,709 | $2,431 | WCID — Point Venture Water Control |
| P11L | 222 | $2,175,871 | $9,801 | PID (high avg → commercial) |
| P10G | 3 | $1,211,391 | $403,797 | PID (3 commercial parcels) |
| P1R | 435 | $1,120,703 | $2,576 | PID |
| P12H | 414 | $1,094,642 | $2,644 | PID |
| P10I | 34 | $860,460 | $25,308 | PID (commercial) |
| P11G | 307 | $779,326 | $2,539 | PID |
| P11D | 347 | $754,078 | $2,173 | PID |
| P10L | 324 | $655,798 | $2,024 | PID |
| P10A | 558 | $632,419 | $1,133 | PID |
| P10D | 771 | $576,127 | $747 | PID |
| P5T | 1,256 | $575,475 | $458 | PID |
| P11F | 248 | $535,810 | $2,161 | PID |
| P12D | 102 | $524,430 | $5,141 | PID |
| P12E | 146 | $455,827 | $3,122 | PID |
| P11K | 468 | $427,097 | $913 | PID |
| P12G | 2 | $423,180 | $211,590 | PID (2 large commercial parcels) |
| P10J | 397 | $366,134 | $922 | PID |
| P1U | 355 | $351,005 | $989 | PID |
| P10U | 302 | $346,189 | $1,146 | PID |
| P10K | 135 | $335,217 | $2,483 | PID |
| P1T | 351 | $307,102 | $875 | PID |
| P6N | 41 | $300,231 | $7,323 | PID (Congress Ave district?) |
| 25D | 37 | $294,743 | $7,561 | Unknown special district |
| PWH | 322 | $272,876 | $847 | WCID — Pedernales Hills? |
| P10B | 275 | $260,459 | $947 | PID |
| P10C | 262 | $246,088 | $939 | PID |
| P3T | 593 | $224,179 | $378 | PID |
| PIH | 11 | $206,058 | $18,733 | PID (commercial) |
| P3J | 115 | $55,150 | $480 | PID |
| P12I | 2 | $29,546 | $14,773 | PID |
| U3P | 45 | $21,449 | $477 | WCID |
| W1D | 1 | $7,521 | $7,521 | WCID |
| X2B–X9B | ~170 | $11,882 | ~$65 | Unknown (possibly special levies) |
| U30, U8A | ~23 | $1,213 | — | MUD |
| TFM | 101 | $112 | $1 | Unknown (likely admin) |
| TST, F03, F06, F11 | ~17 | $13 | — | Admin/test codes |
| **TOTAL** | **12,142** | **$38,054,696** | | |

*Unique parcel count after deduplication across codes.*

---

## Root Cause: PIDs Are Not in the County Rates XLSX

Public Improvement Districts (PIDs) in Texas are administered by individual cities (most here by City of Austin), NOT by the Travis County Appraisal District. Their assessments are:

- Set by city ordinance per district, often as a per-$100 value charge *within that PID's geographic area*
- NOT included in the Travis County `2025RatesHistory1990-2025.xlsx` because they're city-administered
- Appear in TaxCurOpenData because the tax collector (Travis County Tax Office) collects PID charges alongside property taxes
- Have no historical rate series in county records

This is structurally different from missing data — PID rates simply aren't available in the XLSX source.

---

## Impact on the Application

### What is CORRECT
- `tax_billing.total_tax` — correct (from TaxCurOpenData TOTAL_TAX field)
- `tax_billing_entity.amount_due` — correct for all entities including PIDs
- Historical billing display in the 5-Year History table — correct
- Seller's current tax shown in the estimator (`seller_total_tax` = sum of `amount_due`) — correct

### What is WRONG or UNDERSTATED

**Estimated buyer tax (`estimate_acq` route):**
```python
for e in entity_detail:
    if not e.get("rate"):
        continue   # ← PID entities silently skipped here
```
For a parcel with P2U (avg $5,509/year), the estimator omits this charge entirely. The estimated total is understated, and the delta (buyer vs. seller) is meaningless.

**Affected scope:**
- ~12,142 parcels (~6% of the county's taxable parcels) have at least one PID/WCID charge
- Most are within City of Austin's urban core PIDs
- Commercial parcels disproportionately affected (P10G avg $404K, P11L avg $9,801)

**Effective rate calculation:**
The `eff_rate` computed from `taxable_value × combined_rate` also under-reports for these parcels.

---

## Recommended Fix (post-estimator-session merge)

**Do NOT modify `tax_logic/texas.py` or the estimator card markup** until the parallel estimator session has landed.

After merging, apply this pattern in `api_estimate_acq`:

```python
# Separate entities into rate-computable vs. billing-only (PIDs etc.)
billing_only = [
    e for e in entity_detail
    if e.get("amount_due") and not e.get("rate")
]
rate_entities = [e for e in entity_detail if e.get("rate")]

# Existing texas.py call uses rate_entities only
result = estimate_post_acquisition(rate_entities, ...)

# Add PID pass-through (prior year billing = best available estimate)
pid_passthrough = sum(float(e["amount_due"]) for e in billing_only)
pid_entity_names = [e.get("entity_name") or e["entity_code"] for e in billing_only]

result["pid_passthrough"]      = round(pid_passthrough, 2)
result["pid_entity_codes"]     = [e["entity_code"] for e in billing_only]
result["estimated_total_incl_pid"] = round(
    result.get("estimated_total", 0) + pid_passthrough, 2
)
```

In the estimator card JS, show the PID supplement separately:
```
Estimated tax (rate-based):  $12,450
+ PID/Special Districts:     + $5,509  (prior-year billing, not rate-computed)
─────────────────────────────────────
Total estimated tax:          $17,959
```

This approach:
- Is transparent (user sees the PID amount separately)
- Uses the only available data (prior-year billing)
- Doesn't require PID rate history (which doesn't exist)
- Passes through `amount_due` as a conservative same-year estimate

---

## MUD / PID / TIRZ Special-District Overlay (Task 3 finding)

**PIDs present in 2025 billing (partial list by parcel count):**

| TDC Code | Parcels | Annual Billing | Likely District |
|----------|--------:|---------------:|-----------------|
| P2U | 2,558 | $14.1M | 2nd Street District / Downtown PID |
| P5T | 1,256 | $0.6M | Rainey St or similar residential PID |
| P10D | 771 | $0.6M | Unknown |
| P2P | 806 | $2.7M | Domain / North Austin PID |
| P10A | 558 | $0.6M | Unknown |
| P3T | 593 | $0.2M | Unknown |
| P10J | 397 | $0.4M | Unknown |
| P1R | 435 | $1.1M | Unknown |
| P11K | 468 | $0.4M | Unknown |

**WCIDs present but not in rate table:**

| TDC Code | Parcels | Annual Billing | Likely District |
|----------|--------:|---------------:|-----------------|
| PWV | 942 | $2.3M | Point Venture WCID |
| PWH | 322 | $0.3M | Pedernales Hills WCID? |
| W1D | 1 | $7.5K | Unknown small WCID |
| W15 | 8 | $0.1K | Unknown small WCID |

**25D:** 37 parcels, $295K total — likely a TIRZ or tax increment district; requires further research.

**No live changes recommended** for PIDs/WCIDs/TIRZs at this time. The correct action is:
1. Apply the estimator pass-through fix above
2. Optionally source PID rate schedules from City of Austin and add them to a separate `pid_rate` table

---

## Cross-Check: Parcel 128387 (travis.prodigycad.com/property-detail/128387/2026)

This parcel's TCAD account number was not located in TaxCurOpenData (the Prodigy CAD internal PID 128387 does not correspond directly to a 10-char TCAD account ID in the billing file). The app's geo_id is the 10-char TCAD account from PROP.TXT (e.g., `0100030105`), while Prodigy CAD uses its own sequential property ID. To cross-check this specific parcel, look it up at travis.prodigycad.com and note its TCAD account number, then search TaxCurOpenData for that account.

---

## Estimator Hand-Calc Re-Verification Note

The estimator's earlier hand-verification (MORNING_REVIEW.md, parcel 0204063005) was performed with the entity set loaded from TaxCurOpenData. That parcel's billing shows no P-prefix entities in its ENTITY columns, so the hand-calc was unaffected by this bug. However, for any parcel with PID charges, all prior estimator outputs are understated.

---

## Files Affected / Next Steps

| File | Action |
|------|--------|
| `app.py` (api_estimate_acq) | Add billing-only pass-through after estimator merge |
| `templates/property.html` | Show PID supplement in estimator card (after estimator merge) |
| `KNOWN_LIMITATIONS.md` | Add PID/missing-rate limitation |
| `county_tax_rate` table | PIDs deliberately absent — by design, not error |

Do NOT merge this finding to main until the pass-through fix is also applied.
