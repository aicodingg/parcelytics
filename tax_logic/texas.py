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
   Non-owner-occupant buyer: no HS exemption.  Full base value is taxable.
   Owner-occupant buyer: must re-apply by April 30 of the year following
   purchase.  The exemption is NOT active in Year 1 (gap year).  The
   10 %/yr assessed-value cap also begins the following January 1.

   General residence homestead exemptions applied in this estimator
   (owner-occupant Year 2+ estimate only):
     * School district: $140,000 state-mandated exemption (SS11.13(b)(1),
       89th Legislature 2025 -- was $100k before; confirmed in 2025 billing).
       Applied ONLY to entities whose name contains "ISD".
     * All other entities: optional % HS exemption is NOT modelled --
       county_tax_rate does not carry per-entity exemption percentages.
       This makes the non-school total conservative (actual will be lower).
   NOT auto-applied: over-65, disabled-veteran, surviving-spouse, tax
   ceilings, or any other stacking exemption that requires separate
   qualification.

3. 20 % non-homestead circuit-breaker cap (SB 2, 88th Leg. Special Session 2023)
   Applies to non-homestead real property with 2025 market value < CPI-indexed
   threshold ($5.32 M for 2026).  Tax years 2024-2026 only; scheduled to
   expire after 2026 unless extended by the Legislature.  Resets on sale.

4. Base-value modelling
   TCAD appraises as of Jan 1; a sale is strong evidence of market value and is
   typically reflected in the next Jan 1 valuation.  This estimator models
   post-acquisition taxable base as max(current_market_value, purchase_price).

5. Invariant verification (Fix 1a -- confirmed correct)
   When base_value == current_market_value AND seller has no exemptions
   (commercial / non-HS), per-entity est_tax = round(MV * rate / 100) == amount_due
   within integer rounding ($0 delta).  Verified for parcel 0100030105:
     IAU $40,080 vs $40,080.27  |  CAT $22,701 vs $22,700.76
     TCO $16,282 vs $16,281.85  |  THD $5,113  vs $5,112.83
     ACT $4,479  vs $4,479.36   |  Total $88,655 vs actual $88,655.07
   Earlier MORNING_REVIEW '$152 phantom' was a manual-calc error
   (used rounded blended 2.05% instead of per-entity certified rates).

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

from __future__ import annotations
from typing import Optional, List

# ── Texas / Travis County constants ───────────────────────────────────────────

# General residence homestead exemption -- school district only (SS11.13(b)(1))
# Enacted at $140,000 by the 89th Legislature (2025); was $100,000 before.
# Confirmed: 2025 billing for AISD parcels shows $140k applied (not $100k).
# Applied ONLY to school-district entities.  Other taxing units may grant an
# optional exemption of up to 20% AV (SS11.13(n)), but those percentages are
# NOT in county_tax_rate -- non-school entities treated conservatively here.
SCHOOL_HS_EXEMPTION = 140_000

# 20% non-homestead circuit-breaker cap (SB 2, 88th special session, Jul 2023)
# CPI-indexed: $5.0M (2024), est. $5.16M (2025), $5.32M (2026).
# EXPIRES after tax year 2026 unless Legislature renews.
CIRCUIT_BREAKER_THRESHOLD_2026 = 5_320_000
CIRCUIT_BREAKER_MV_THRESHOLD   = CIRCUIT_BREAKER_THRESHOLD_2026   # alias for compat
CIRCUIT_BREAKER_TAX_YEARS      = (2024, 2025, 2026)
CIRCUIT_BREAKER_CAP_PCT        = 0.20

# Homestead cap: owner-occupied residential, 10%/year max AV increase
HOMESTEAD_CAP_PCT = 0.10

# Current reference tax year for rates
RATE_YEAR = 2025

# First full tax year the estimate represents (the post-acquisition cycle).
# A sale now is reflected in the next Jan 1 valuation / current appraisal cycle.
FIRST_TAX_YEAR = 2026

# Default annual market-appreciation assumption for multi-year projection,
# used only when a parcel-specific clamped CAGR isn't supplied by the caller.
DEFAULT_MARKET_GROWTH = 0.035


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


def _is_school_entity(entity_name: str) -> bool:
    """
    Return True if this taxing entity is a school district.

    Texas SS11.13(b)(1) mandates the $140,000 HS exemption specifically for
    school districts.  Other entities grant optional exemptions under SS11.13(n).
    Identified heuristically by 'ISD' or 'INDEPENDENT SCHOOL' in entity name --
    reliable for all Travis County school districts.
    """
    name = (entity_name or "").upper()
    return "ISD" in name or "INDEPENDENT SCHOOL" in name


def _circuit_breaker_eligible(market_value: int) -> bool:
    """True if parcel currently qualifies for the non-HS 20% circuit-breaker cap."""
    return 0 < market_value < CIRCUIT_BREAKER_THRESHOLD_2026


def _project_entity_rate(year_rates: dict, current_rate: float) -> float:
    """
    Project one forward per-entity rate from its recent trajectory.

    Recency-weighted mean of consecutive year-over-year deltas (more weight on
    recent years), added to the latest certified rate.  Texas rates have broadly
    been FALLING (school-M&O compression + the 3.5% voter-approval cap), so this
    is intentionally compression-aware: declines pass through, but any projected
    RISE is clamped to a small drift (≤ +2%) — we never assume a jump upward.
    Returns a single rate held flat across the projection horizon (not a
    certified rate).
    """
    if not year_rates or len([r for r in year_rates.values() if r is not None]) < 3:
        return current_rate
    yrs = sorted(year_rates.keys())
    deltas = []
    for i in range(1, len(yrs)):
        r0, r1 = year_rates[yrs[i - 1]], year_rates[yrs[i]]
        if r0 is not None and r1 is not None:
            deltas.append(float(r1) - float(r0))
    if not deltas:
        return current_rate
    # recency weights: oldest delta weight 1 … newest weight n
    acc = sum((i + 1) * d for i, d in enumerate(deltas))
    wsum = sum(i + 1 for i in range(len(deltas)))
    wdelta = acc / wsum if wsum else 0.0
    proj = current_rate + wdelta
    lo, hi = current_rate * 0.85, current_rate * 1.02   # allow decline; cap rise
    return max(lo, min(hi, proj))


def _project_multiyear(base_value, entities, buyer_status, market_growth,
                       horizon_years, first_tax_year):
    """
    Year-by-year projected recurring tax. Honest per-buyer mechanics:

      Owner-occupant: Year 1 = gap (no exemption, no cap, assessed = base).
                      Year 2+ = school HS exemption + 10%/yr assessed-growth cap.
      Investor:       no exemption ever; 20%/yr circuit-breaker cap while it
                      applies (non-HS, market < threshold, through TY2026),
                      uncapped thereafter.

    `entities` items carry: rate_used (float, %/$100) and is_school (bool).
    Returns a list of {year_index, tax_year, market, assessed, est_tax}.
    Rates are held flat at whatever vintage the caller resolved (rate_used).
    """
    rows = []
    prev_assessed = float(base_value)
    for n in range(1, horizon_years + 1):
        tax_year = first_tax_year + (n - 1)
        market_n = float(base_value) * ((1.0 + market_growth) ** (n - 1))
        if n == 1:
            assessed_n = float(base_value)            # gap / acquisition year
        elif buyer_status == "owner_occupant":
            assessed_n = min(market_n, prev_assessed * (1.0 + HOMESTEAD_CAP_PCT))
        else:  # investor
            cb_applies = (tax_year <= max(CIRCUIT_BREAKER_TAX_YEARS)
                          and market_n < CIRCUIT_BREAKER_THRESHOLD_2026)
            assessed_n = (min(market_n, prev_assessed * (1.0 + CIRCUIT_BREAKER_CAP_PCT))
                          if cb_applies else market_n)
        prev_assessed = assessed_n

        total = 0
        for e in entities:
            rate = e.get("rate_used")
            if not rate:
                continue
            exempt = SCHOOL_HS_EXEMPTION if (buyer_status == "owner_occupant"
                                             and n >= 2 and e.get("is_school")) else 0
            taxable = max(0.0, assessed_n - exempt)
            total += round(taxable * rate / 100)
        rows.append({
            "year_index": n,
            "tax_year":   tax_year,
            "market":     round(market_n),
            "assessed":   round(assessed_n),
            "est_tax":    int(total),
        })
    return rows


# ── Public interface ──────────────────────────────────────────────────────────

def estimate_post_acquisition(
    parcel:         dict,
    current_yr_row: dict,
    entity_detail:  List[dict],
    purchase_price: int,
    buyer_status:   str,   # 'non_owner_occupant' | 'owner_occupant'
    *,
    rate_mode:           str   = "certified",   # 'certified' | 'projected'
    entity_rate_history: dict  = None,          # {entity_code: {year: rate}}
    market_growth:       float = None,          # annual appreciation assumption
    horizon_years:       int   = 5,
) -> dict:
    """
    Estimate Year-1 / Year-2+ post-acquisition tax under Texas law.

    NON-OWNER-OCCUPANT:
      Returns Year-1 (and ongoing) estimate.  No HS exemption ever.
      Full base_value is taxable for every entity.

    OWNER-OCCUPANT:
      Returns Year-2+ estimate (after HS exemption activates).
      Year-1 (gap year, same as investor) is returned in gap_year_tax.
      School entity: $140,000 mandatory exemption.
      Non-school entities: conservative -- no optional exemption modelled.

    Returns a dict suitable for JSON serialisation and Jinja rendering.
    All monetary values are plain Python ints/floats -- no Decimal.
    """
    if buyer_status not in ("non_owner_occupant", "owner_occupant"):
        buyer_status = "non_owner_occupant"
    if rate_mode not in ("certified", "projected"):
        rate_mode = "certified"
    entity_rate_history = entity_rate_history or {}
    if market_growth is None:
        market_growth = DEFAULT_MARKET_GROWTH
    # keep the growth assumption in a sane band regardless of source.
    # Lower bound allows a declining-value projection (Task 5): the multi-year
    # estimate mirrors the parcel's actual CAGR instead of flooring flat at 0%.
    market_growth = max(-0.05, min(0.12, float(market_growth)))

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

    # ── Seller's current total tax (from actual billing) ──────────────────────
    seller_total_tax = sum(
        float(e["amount_due"]) for e in entity_detail if e.get("amount_due")
    )

    # ── Entity-level breakdown ────────────────────────────────────────────────
    # Compute both Year-1 (gap year) and Year-2+ (post-HS filing) in one pass.
    # For investor, yr1 == yr2 (no exemptions).
    entity_breakdown = []
    total_est_yr2 = 0     # main estimate (Year-2+ for owner-occ; Year-1 for investor)
    total_est_yr1 = 0     # Year-1 gap-year total (all buyers pay this in Year 1)
    proj_entities = []    # for multi-year projection (carries rate_used + is_school)

    for e in entity_detail:
        if not e.get("rate"):
            continue

        rate        = float(e["rate"])        # percent-per-$100; e.g. 0.9252 = 0.9252%
        seller_tax  = float(e["amount_due"]) if e.get("amount_due") else None
        entity_name = e.get("entity_name") or e["entity_code"]
        is_school   = _is_school_entity(entity_name)

        # Rate vintage: certified 2025 (default, verified) or a forward trend
        # projection. Default path leaves rate_used == rate → numbers unchanged.
        if rate_mode == "projected":
            rate_used = _project_entity_rate(entity_rate_history.get(e["entity_code"]) or {}, rate)
        else:
            rate_used = rate

        # Per-entity HS exemption for owner-occupant Year 2+
        if buyer_status == "owner_occupant" and is_school:
            exemption_for_entity = SCHOOL_HS_EXEMPTION
        else:
            exemption_for_entity = 0  # investor: never; non-school: conservative

        buyer_taxable_yr2 = max(0, base_value - exemption_for_entity)
        buyer_taxable_yr1 = base_value   # gap year: no exemption, all buyers

        # est_tax = taxable * rate / 100
        # Invariant: when base_value==MV and seller has no exemptions,
        # round(MV * rate / 100) == round(amount_due) within $1 rounding.
        est_tax_yr2 = round(buyer_taxable_yr2 * rate_used / 100)
        est_tax_yr1 = round(buyer_taxable_yr1 * rate_used / 100)

        total_est_yr2 += est_tax_yr2
        total_est_yr1 += est_tax_yr1

        proj_entities.append({"rate_used": rate_used, "is_school": is_school})

        entity_breakdown.append({
            "entity_code":      e["entity_code"],
            "entity_name":      entity_name,
            "rate":             rate,
            "rate_used":        round(rate_used, 6),
            "rate_projected":   (rate_mode == "projected" and abs(rate_used - rate) > 1e-9),
            "is_school":        is_school,
            "taxable":          buyer_taxable_yr2,      # primary (Year 2+ or investor)
            "yr1_taxable":      buyer_taxable_yr1,
            "exemption_entity": exemption_for_entity,
            "est_tax":          est_tax_yr2,            # primary estimate displayed
            "yr1_tax":          est_tax_yr1,
            "seller_tax":       seller_tax,
            "delta": (est_tax_yr2 - seller_tax) if seller_tax is not None else None,
        })

    # ── Totals and key notes ──────────────────────────────────────────────────
    if buyer_status == "owner_occupant":
        estimated_total_tax = total_est_yr2           # Year-2+ (post-HS filing)
        gap_year_tax        = total_est_yr1           # Year-1 (same as investor)
        exemption_applied   = SCHOOL_HS_EXEMPTION     # school entity only

        exemption_note = (
            f"Year 2+ estimate -- general residence homestead exemption applied. "
            f"School district (ISD): ${SCHOOL_HS_EXEMPTION:,} mandatory exemption "
            "(SS11.13(b)(1), 89th Legislature 2025, was $100k before). "
            "Non-school entities: conservative -- optional % HS not in public data; "
            "actual Year 2+ tax will likely be lower. "
            "NOT applied: over-65, disabled-veteran, surviving-spouse (require separate qualification)."
        )
        gap_year_note = (
            f"Year 1 gap year: HS exemption NOT yet active. "
            f"Year-1 est. tax = ${gap_year_tax:,.0f} (full base value taxable). "
            "File for homestead exemption by April 30 of the year after purchase. "
            "10%/yr cap begins the following January 1. "
            "This card shows the Year 2+ recurring estimate."
        )
    else:
        estimated_total_tax = total_est_yr1           # no exemptions ever
        gap_year_tax        = None
        exemption_applied   = 0

        exemption_note = (
            "No homestead exemption -- non-owner-occupant buyer. "
            "Full base value is taxable across all entities. "
            "If buyer later converts to primary residence, file HS by Apr 30 of following year."
        )
        gap_year_note = None

    delta = estimated_total_tax - seller_total_tax

    # ── Circuit-breaker exposure ──────────────────────────────────────────────
    cb_eligible_now = (
        not seller_has_homestead
        and _circuit_breaker_eligible(market_value)
    )
    circuit_breaker_note = None
    if cb_eligible_now and buyer_status == "non_owner_occupant":
        circuit_breaker_note = (
            "Warning: This parcel currently benefits from the 20% non-homestead "
            "circuit-breaker cap (SB 2, 88th Special Session 2023 -- applies 2024-2026 "
            f"to non-homestead real property under ${CIRCUIT_BREAKER_THRESHOLD_2026:,.0f}). "
            "This cap RESETS on change of ownership AND is scheduled to EXPIRE after "
            "tax year 2026 unless the Legislature extends it. "
            "Buyer faces double exposure: cap loss at sale + potential post-2026 rate increase."
        )

    # ── Combined rates (certified vs resolved) for display ────────────────────
    certified_combined_rate = round(sum(float(e["rate"]) for e in entity_detail if e.get("rate")), 6)
    used_combined_rate      = round(sum(b["rate_used"] for b in entity_breakdown), 6)

    # ── Rate vintage label ────────────────────────────────────────────────────
    if rate_mode == "projected":
        rate_vintage = (
            f"Projected rates (trend) — combined {used_combined_rate:.4f}% vs "
            f"{certified_combined_rate:.4f}% certified. Not a certified rate."
        )
    else:
        rate_vintage = f"2025 certified rates — combined {certified_combined_rate:.4f}%."

    # ── Multi-year projection (Year 1 … horizon) ──────────────────────────────
    multiyear = _project_multiyear(
        base_value, proj_entities, buyer_status,
        market_growth, horizon_years, FIRST_TAX_YEAR,
    )

    # ── Assumption strings for display ────────────────────────────────────────
    assumptions = [
        f"Estimate represents the first full post-acquisition tax year (TY{FIRST_TAX_YEAR}).",
        f"Base value = max(2025 certified market ${market_value:,.0f}, "
        f"purchase price ${purchase_price:,.0f}) = ${base_value:,.0f}",
        (
            f"Rates: 2025 certified entity rates, held flat — combined {certified_combined_rate:.4f}%."
            if rate_mode == "certified"
            else (f"Rates: per-entity recency-weighted trend projection (compression-aware; "
                  f"Texas rates have broadly fallen), held flat — combined {used_combined_rate:.4f}% "
                  f"vs {certified_combined_rate:.4f}% certified. A projection, not a certified rate.")
        ),
        (
            f"Cap reset: seller's HS cap loss ${hs_cap_loss:,.0f} does NOT transfer to buyer"
            if cap_was_active
            else "Cap: no active homestead cap on this parcel (hs_cap_loss = $0)"
        ),
        exemption_note,
        (f"Multi-year: assumes {market_growth*100:.1f}%/yr market appreciation. "
         + ("Owner-occupant assessed growth capped at 10%/yr (Year 2+)."
            if buyer_status == "owner_occupant"
            else f"Investor assessed growth capped at 20%/yr through TY{max(CIRCUIT_BREAKER_TAX_YEARS)} (circuit-breaker), uncapped after.")),
    ]
    if buyer_status == "owner_occupant":
        assumptions.append(
            f"Year 2+: school-entity HS ${SCHOOL_HS_EXEMPTION:,} applied. "
            "Non-school optional HS not modelled -- actual Year 2+ tax likely lower."
        )

    return {
        # Inputs
        "purchase_price":            purchase_price,
        "buyer_status":              buyer_status,
        # Certified values
        "market_value":              market_value,
        "base_value":                base_value,
        "taxable_new":               max(0, base_value - exemption_applied),
        "exemption_applied":         exemption_applied,
        # Seller context
        "assessed_value":            assessed_value,
        "hs_cap_loss":               hs_cap_loss,
        "cap_was_active":            cap_was_active,
        "seller_has_homestead":      seller_has_homestead,
        "seller_total_tax":          round(seller_total_tax, 2),
        # Output
        "entity_breakdown":          entity_breakdown,
        "estimated_total_tax":       estimated_total_tax,
        "gap_year_tax":              gap_year_tax,
        "delta":                     delta,
        # Rate handling
        "rate_mode":                 rate_mode,
        "rate_vintage":              rate_vintage,
        "first_tax_year":            FIRST_TAX_YEAR,
        "certified_combined_rate":   certified_combined_rate,
        "used_combined_rate":        used_combined_rate,
        # Multi-year projection
        "market_growth":             round(market_growth, 4),
        "multiyear":                 multiyear,
        # Notes
        "assumptions":               assumptions,
        "gap_year_note":             gap_year_note,
        "circuit_breaker_note":      circuit_breaker_note,
        "is_residential":            is_res,
        "school_hs_exemption":       SCHOOL_HS_EXEMPTION,
        "circuit_breaker_threshold": CIRCUIT_BREAKER_THRESHOLD_2026,
    }
