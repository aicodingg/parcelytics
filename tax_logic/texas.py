"""
Texas post-acquisition tax estimator — Travis County implementation.

All Texas-specific tax law is encoded ONLY in this module.  The Flask routes and
Jinja templates call the generic interface below; they contain no Texas-specific
logic.  Adding a second state means writing a new module against the same
interface, not editing this file or the views.

Texas mechanics implemented here
---------------------------------
1. Cap reset on change of ownership (§11.26 Tax Code)
   The 10 % homestead cap does not transfer.  The seller's "cap loss"
   (market − assessed) disappears; buyer's base reverts to market value.

2. Homestead exemption loss / re-application (§11.13)
   Non-owner-occupant buyer: no HS exemption. Full market value is taxable.
   Owner-occupant buyer: must re-apply; exemption is NOT granted in Year 1 (gap
   year).  Cap protection also begins the *following* January 1.

3. 20 % non-homestead circuit-breaker cap (SB 2, 88th Leg. Special Session 2023)
   Applies to non-homestead real property with 2025 market value < CPI-indexed
   threshold (~$5 M).  Tax years 2024 – 2026 only; scheduled to expire after
   2026 unless extended.  Resets on sale AND may disappear after 2026.

4. Base-value modeling
   TCAD appraises as of Jan 1; a sale is strong evidence of market value and is
   typically reflected in the next Jan 1 valuation.  This estimator models
   post-acquisition taxable base as max(current_market_value, purchase_price).

Data inputs required
--------------------
  parcel         : dict from the `parcel` table
  current_yr_row : dict from parcel_tax_year WHERE tax_year = 2025
  entity_detail  : list of dicts from tax_billing_entity + county_tax_rate
                   fields: entity_code, entity_name, rate, amount_due
  purchase_price : int  (positive)
  buyer_status   : 'non_owner_occupant' | 'owner_occupant'

Returns
-------
  A dict with all inputs, entity-level breakdown, totals, delta, and
  human-readable assumption strings for display.  No state is mutated.
"""

from typing import Optional, List

# ── Texas / Travis County constants (2025) ────────────────────────────────────

# 20 % non-homestead circuit-breaker (SB 2, 88th special session, July 2023)
CIRCUIT_BREAKER_MV_THRESHOLD = 5_000_000   # ~$5.32 M for 2026 per CPI; use 5M conservatively
CIRCUIT_BREAKER_TAX_YEARS    = (2024, 2025, 2026)
CIRCUIT_BREAKER_CAP_PCT      = 0.20

# Homestead cap: owner-occupied residential, 10 % / year max AV increase
HOMESTEAD_CAP_PCT = 0.10

# Current reference tax year for rates
RATE_YEAR = 2025


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_homestead_exemption(exemption_codes: Optional[str]) -> bool:
    """Return True if 'HS' appears in the exemption code string."""
    if not exemption_codes:
        return False
    codes = {c.strip().upper() for c in exemption_codes.replace(";", ",").split(",")}
    return "HS" in codes


def _is_residential(parcel: dict) -> bool:
    sc1 = (parcel.get("state_cd1") or "").strip()[:1].upper()
    return sc1 == "A"


def _circuit_breaker_eligible(market_value: int) -> bool:
    """True if this parcel currently qualifies for the non-HS circuit-breaker cap."""
    return 0 < market_value < CIRCUIT_BREAKER_MV_THRESHOLD


# ── Public interface ──────────────────────────────────────────────────────────

def estimate_post_acquisition(
    parcel:         dict,
    current_yr_row: dict,
    entity_detail:  List[dict],
    purchase_price: int,
    buyer_status:   str,   # 'non_owner_occupant' | 'owner_occupant'
) -> dict:
    """
    Estimate Year-1 post-acquisition tax under Texas law.

    Returns a dict suitable for JSON serialisation and Jinja rendering.
    All monetary values are plain Python ints/floats — no Decimal.
    """
    if buyer_status not in ("non_owner_occupant", "owner_occupant"):
        buyer_status = "non_owner_occupant"

    # ── Pull 2025 certified values ────────────────────────────────────────────
    market_value   = int(current_yr_row.get("market_value")   or 0)
    assessed_value = int(current_yr_row.get("assessed_value") or market_value)
    taxable_value  = int(current_yr_row.get("taxable_value")  or assessed_value)
    hs_cap_loss    = int(current_yr_row.get("hs_cap_loss")    or 0)
    exemption_codes = current_yr_row.get("exemption_codes") or ""

    is_res               = _is_residential(parcel)
    seller_has_homestead = _has_homestead_exemption(exemption_codes) and is_res
    cap_was_active       = seller_has_homestead and hs_cap_loss > 0

    # ── Base value: max(current market, purchase price) ───────────────────────
    base_value = max(market_value, purchase_price)

    # ── Exemptions & taxable value for new buyer ──────────────────────────────
    if buyer_status == "non_owner_occupant":
        taxable_new        = base_value
        exemption_applied  = 0
        exemption_note     = (
            "No homestead exemption — non-owner-occupant buyer. "
            "Full base value is taxable across all entities."
        )
        gap_year_note      = None
    else:
        # Owner-occupant: Year 1 is the gap year — HS exemption not yet granted;
        # cap protection begins the following January 1.
        taxable_new        = base_value
        exemption_applied  = 0
        exemption_note     = (
            "Homestead exemption shown as Year-1 (gap year) estimate. "
            "Exemption is NOT active in Year 1 — it takes effect the January 1 "
            "after you qualify. File by April 30 of the year following purchase."
        )
        gap_year_note = (
            "Gap year: Year 1 = full market value is taxable. "
            "Year 2+: 10 %/yr cap begins; HS exemption reduces taxable value further. "
            "Owner-occupant Year-2+ tax will be lower than this estimate."
        )

    # ── Seller's current tax (from actual billing entities) ───────────────────
    seller_total_tax = sum(
        float(e["amount_due"]) for e in entity_detail if e.get("amount_due")
    )

    # ── Entity-level breakdown ────────────────────────────────────────────────
    entity_breakdown = []
    total_est_tax = 0

    for e in entity_detail:
        if not e.get("rate"):
            continue
        rate       = float(e["rate"])
        est_tax    = round(taxable_new * rate / 100)
        seller_tax = float(e["amount_due"]) if e.get("amount_due") else None
        total_est_tax += est_tax
        entity_breakdown.append({
            "entity_code": e["entity_code"],
            "entity_name": e.get("entity_name") or e["entity_code"],
            "rate":        rate,
            "taxable":     taxable_new,
            "est_tax":     est_tax,
            "seller_tax":  seller_tax,
            "delta":       (est_tax - seller_tax) if seller_tax is not None else None,
        })

    delta = total_est_tax - seller_total_tax

    # ── Circuit-breaker exposure ──────────────────────────────────────────────
    cb_eligible_now = (
        not seller_has_homestead
        and _circuit_breaker_eligible(market_value)
    )
    circuit_breaker_note = None
    if cb_eligible_now and buyer_status == "non_owner_occupant":
        circuit_breaker_note = (
            "⚠ This parcel currently benefits from the 20 % non-homestead circuit-breaker cap "
            "(SB 2, 88th Special Session, 2023 — applies 2024–2026 to non-homestead "
            f"real property under ${CIRCUIT_BREAKER_MV_THRESHOLD:,.0f}). "
            "This cap resets on change of ownership AND is scheduled to expire after "
            "tax year 2026 unless the Legislature extends it. "
            "Buyer faces double exposure: cap loss at sale + potential post-2026 rate increase."
        )

    # ── Assumption strings for UI display ─────────────────────────────────────
    assumptions = [
        f"Base value = max(2025 certified market ${market_value:,.0f}, "
        f"purchase price ${purchase_price:,.0f}) = ${base_value:,.0f}",
        f"Rates: 2025 certified entity rates — set annually; will change each year",
        (
            f"Cap reset: seller's HS cap loss ${hs_cap_loss:,.0f} does NOT transfer to buyer"
            if cap_was_active
            else "Cap: no active homestead cap on this parcel (cap loss = $0)"
        ),
        exemption_note,
    ]
    if buyer_status == "owner_occupant":
        assumptions.append(
            "Year 2+: file for HS exemption (apply by Apr 30). "
            "10 %/yr cap protection begins Jan 1 of the qualifying year."
        )

    return {
        # Inputs
        "purchase_price":       purchase_price,
        "buyer_status":         buyer_status,
        # Values
        "market_value":         market_value,
        "base_value":           base_value,
        "taxable_new":          taxable_new,
        "exemption_applied":    exemption_applied,
        # Seller context
        "assessed_value":       assessed_value,
        "hs_cap_loss":          hs_cap_loss,
        "cap_was_active":       cap_was_active,
        "seller_has_homestead": seller_has_homestead,
        "seller_total_tax":     round(seller_total_tax, 2),
        # Output
        "entity_breakdown":     entity_breakdown,
        "estimated_total_tax":  total_est_tax,
        "delta":                delta,
        # Notes
        "assumptions":          assumptions,
        "gap_year_note":        gap_year_note,
        "circuit_breaker_note": circuit_breaker_note,
        "is_residential":       is_res,
    }
