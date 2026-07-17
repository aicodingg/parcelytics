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


# ── 2026 preliminary baseline derivation (Issue 1, "Homestead-Cap Data
# Integrity: Full Fix Set" Cowork brief, July 2026) ─────────────────────────
#
# Single assembly point for the 2026 row's DISPLAY/COMPUTATION values.
# Every consumer (build_projections()'s CAGR-endpoint base_assessed, the
# "Est. 2026 Total Tax" KPI, the Taxable Value 2026 card, property.html's
# HS Cap Loss table row) reads est_assessed_2026/est_taxable_2026/basis_2026
# from here instead of independently re-deriving (or, as the earlier
# "coherence fix" did, blindly trusting) the 2026 preliminary row's raw
# assessed_value/taxable_value.
#
# THE BUG THIS REPLACES: the earlier "coherence fix" treated
# assessed_value <= market_value on the 2026 preliminary row as "safe, use
# it" (a fair test IF assessed_value were always a real appraisal figure).
# But TCAD's preliminary export very often reports assessed_value ==
# market_value even for parcels with a real, currently-active homestead
# exemption -- the cap hasn't been applied to the preliminary snapshot yet,
# not because there's no cap. Equality is uninformative, not safe. Only
# assessed_value < market_value is trustworthy evidence the county actually
# applied the cap to this row.
#
# CORE RULE:
#   assessed_2026 <  market_2026  -> trust TCAD; the county already capped it.
#   assessed_2026 == market_2026  -> uninformative; derive our own estimate
#                                     IF a homestead exemption was active on
#                                     the 2025 CERTIFIED roll (not 2026
#                                     preliminary -- confirmed live,
#                                     exemption_codes is only 52.8% populated
#                                     on 2026 rows vs. 55.1% on 2025, so 2025
#                                     certified is the more reliable basis).
#   assessed_2026 >  market_2026  -> shouldn't happen (anomalous export row);
#                                     treated the same as equality (fall
#                                     through to the derivation path) since
#                                     it's equally uninformative/untrustworthy,
#                                     never treated as "capped."
#
# DERIVATION (only used for the equality/anomalous case, when a cap applies):
#   est_assessed_2026 = min(market_2026, 1.10 * assessed_2025_certified)
#   est_taxable_2026  = est_assessed_2026 - (assessed_2025 - taxable_2025)
#     i.e. subtract the SAME dollar amount of 2025's other (non-cap)
#     exemptions -- OV65/DP/etc -- from the derived assessed figure, rather
#     than assuming no other exemptions exist. If 2025's exemption amount
#     isn't available (taxable_2025 missing), est_taxable_2026 falls back to
#     est_assessed_2026 itself (conservative: doesn't invent a discount).
#
#   Validated against real TCAD data (Diego's live query, 54,950-parcel
#   cohort where TCAD's OWN 2026 preliminary row already shows a real cap,
#   assessed_2026 < market_2026): this formula reproduces TCAD's actual
#   assessed_2026 within $1 for 90.6% of cases, within 1% for 92.5% -- a
#   validated, trustworthy derivation, not a guess.
#
# BASIS FLAGS (stored, never overwrite the raw fields):
#   'tcad_capped'          -- assessed_2026 < market_2026: use TCAD's real
#                              preliminary numbers. Confidence: Preliminary.
#   'derived_cap_estimate'  -- equality/anomalous + HS on 2025 + cap actually
#                              binds (est_assessed_2026 < market_2026).
#                              Confidence: Estimated, '~' prefix required in
#                              copy, with the inline TCAD-disagreement note
#                              (see property.html Issue 3 for the exact
#                              wording) -- otherwise a parcel's own page
#                              visibly contradicts its own linked TCAD
#                              record, which is worse than the original bug.
#   'uncapped_no_cap'       -- no HS on the 2025 certified roll, OR HS is
#                              newly filed for 2026 with no 2025 HS (Tax Code
#                              §23.23(c): the cap doesn't apply until the
#                              exemption has been in place a full year, so a
#                              2026-only HS filing does NOT yet cap 2026), OR
#                              the cap is real but doesn't currently bind
#                              (1.10 * assessed_2025 >= market_2026 -- e.g. a
#                              market decline, or modest enough growth that
#                              the 10%/yr ceiling sits above market anyway).
#                              Raw TCAD values used as-is. Confidence:
#                              Preliminary.
def derive_2026_baseline(row_2025: Optional[dict], row_2026: Optional[dict]) -> Optional[dict]:
    """
    Given the 2025 certified row and 2026 preliminary row (each a dict with
    at least market_value/assessed_value/taxable_value/exemption_codes, or
    None if that year isn't on file), return a dict of derived fields to
    attach to the 2026 row -- or None if there's no 2026 row to derive
    anything for.

    Returned keys:
      est_assessed_2026, est_taxable_2026 (ints or None),
      basis_2026 ('tcad_capped' | 'derived_cap_estimate' | 'uncapped_no_cap'),
      is_approx_2026 (bool), confidence_2026 ('Preliminary' | 'Estimated')
    """
    if not row_2026 or not row_2026.get("market_value"):
        return None

    market_2026    = float(row_2026["market_value"])
    assessed_2026  = row_2026.get("assessed_value")
    taxable_2026   = row_2026.get("taxable_value")
    assessed_2026  = float(assessed_2026) if assessed_2026 is not None else None

    # TCAD already applied a real cap to this preliminary row -- trust it.
    if assessed_2026 is not None and assessed_2026 < market_2026:
        return {
            "est_assessed_2026": row_2026["assessed_value"],
            "est_taxable_2026":  taxable_2026 if taxable_2026 is not None else row_2026["assessed_value"],
            "basis_2026":        "tcad_capped",
            "is_approx_2026":    False,
            "confidence_2026":   "Preliminary",
        }

    # Equality (or an anomalous assessed > market row) -- uninformative.
    # Cap applies only if the 2025 CERTIFIED roll (not 2026 preliminary) had
    # an active HS exemption -- §23.23(c) timing means a 2026-only filing
    # doesn't cap 2026 itself.
    assessed_2025 = row_2025.get("assessed_value") if row_2025 else None
    taxable_2025  = row_2025.get("taxable_value")  if row_2025 else None
    cap_applies   = bool(row_2025) and _has_homestead_exemption(row_2025.get("exemption_codes"))

    if cap_applies and assessed_2025 is not None:
        est_assessed = min(market_2026, 1.10 * float(assessed_2025))
        if est_assessed < market_2026:
            # Cap genuinely binds -- derive both assessed and taxable.
            if taxable_2025 is not None:
                exemption_amt = max(0.0, float(assessed_2025) - float(taxable_2025))
                est_taxable = max(0.0, est_assessed - exemption_amt)
            else:
                est_taxable = est_assessed
            return {
                "est_assessed_2026": round(est_assessed),
                "est_taxable_2026":  round(est_taxable),
                "basis_2026":        "derived_cap_estimate",
                "is_approx_2026":    True,
                "confidence_2026":   "Estimated",
            }

    # No cap applies, or the cap is real but doesn't currently bind -- raw
    # TCAD figures are correct as-is.
    return {
        "est_assessed_2026": row_2026.get("assessed_value"),
        "est_taxable_2026":  taxable_2026,
        "basis_2026":        "uncapped_no_cap",
        "is_approx_2026":    False,
        "confidence_2026":   "Preliminary",
    }


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


# ── Local-option homestead exemption (S11.13(n)) — estimate_homestead_savings() only ──
#
# S11.13(n) separately lets ANY taxing unit (not just school districts) adopt
# its own local-option homestead exemption of up to 20% of appraised value
# (floor: $5,000, even if 20% of AV would compute lower). This is NOT modeled
# in estimate_post_acquisition() / _project_multiyear() (Tier 1, approved
# separately, not touched here) — those remain conservative/school-only by
# design. It IS modeled here, for exactly three entities confirmed against
# primary/official sources (not real-estate-blog aggregators):
#   * Travis County    — 20%, the maximum allowed by law. Travis County's own
#     FY2026 Taxpayer Impact Statement (traviscountytx.gov).
#   * City of Austin    — 20%. TCAD board-proceedings reporting (The Austin
#     Bulldog), sourcing TCAD's own chief appraiser.
#   * Central Health    — 20%. Same source as City of Austin above. Central
#     Health is the public-facing brand of the Travis County Healthcare
#     District; the taxing-entity record itself is named "Travis Central
#     Health" (see entity_code THD below).
#
# Matched primarily by entity_code, confirmed against
# 2025RatesHistory1990-2025.xlsx (the actual source file loaders/load_tax_rates.py
# uses to populate county_tax_rate.entity_name — this IS the real DB value,
# not an assumed string):
#   TCO -> "Travis County"          CAT -> "City of Austin"
#   THD -> "Travis Central Health"
# entity_name is checked too (exact match, not substring) as defense in depth
# in case a future reload ever changes codes. Substring matching was
# deliberately avoided: the same source file also has entity_code CAH ->
# "City of Austin (Hays)" — a DIFFERENT, unconfirmed entity that a naive
# "CITY OF AUSTIN" in name.upper() check would have incorrectly swept in.
#
# NOT extended to any other entity on a bill (MUDs, ESDs, Austin Community
# College [ACT], other cities/ISDs). Their local-option status is real
# taxing-unit-by-taxing-unit information this project has not confirmed
# against an authoritative source — left at $0 exemption (conservative),
# per this project's rule against guessing where sourcing doesn't reach.
LOCAL_OPTION_20PCT_ENTITY_CODES = {"TCO", "CAT", "THD"}
LOCAL_OPTION_20PCT_ENTITY_NAMES = {"TRAVIS COUNTY", "CITY OF AUSTIN", "TRAVIS CENTRAL HEALTH"}
LOCAL_OPTION_PCT           = 0.20
LOCAL_OPTION_MIN_EXEMPTION = 5_000   # S11.13(n) floor; won't bind at typical Travis AVs


def _local_option_20pct_entity(entity_code: Optional[str], entity_name: Optional[str]) -> bool:
    """True only for the 3 entities confirmed above -- code match first (authoritative),
    exact (non-substring) name match as a fallback. See constants block above for sourcing."""
    code = (entity_code or "").strip().upper()
    if code in LOCAL_OPTION_20PCT_ENTITY_CODES:
        return True
    name = (entity_name or "").strip().upper()
    return name in LOCAL_OPTION_20PCT_ENTITY_NAMES


def estimate_homestead_savings(
    entity_detail: List[dict],
    assessed_value: Optional[int],
    preliminary_assessed: Optional[int] = None,
    preliminary_market: Optional[int] = None,
) -> Optional[dict]:
    """
    Estimate the ANNUAL tax savings a parcel would see if it filed for the
    general residence homestead exemption, for parcels that do NOT currently
    have one.

    Models TWO exemption types, each applied per-entity to that entity's own
    portion of the bill (an entity gets at most one; they're mutually
    exclusive by construction):
      * School district entities: $140,000 mandatory exemption (SS11.13(b)(1)),
        same constant/detection as estimate_post_acquisition() (Tier 1, not
        touched here).
      * Travis County / City of Austin / Central Health specifically: 20% of
        assessed value local-option exemption (SS11.13(n)), floor $5,000 --
        see the sourcing block above _local_option_20pct_entity(). This is
        new; previously these three entities got no exemption modeled at all,
        which understated potential savings since they're a meaningful share
        of most Travis County bills.
      * All other entities (MUDs, ESDs, ACT, other cities): still $0 --
        unconfirmed, left conservative, unchanged from before.

    This is a hypothetical "what if you filed" figure on the CURRENT
    assessed value, not a post-acquisition or multi-year projection — it's
    a standalone estimate for display on the property page's Exemptions
    section. Caller is responsible for only invoking/displaying this for
    parcels that don't already have a homestead exemption (this function
    does not check exemption_codes itself — it estimates the hypothetical
    savings regardless of current status).

    Does NOT model the 65+/disabled-specific additional exemption amounts
    (e.g. Travis County's additional $143,220, or the combined $200,000
    school figure for 65+/disabled homeowners) -- scoped to the general
    homestead case only. Flagged as a possible future extension.

    current_est_tax reconciliation (Homestead Accuracy brief): this is now
    the REAL billed total (SUM of entity_detail's amount_due), not a figure
    recomputed from assessed_value * rate. It has to be, because amount_due
    already reflects whatever exemption the parcel ALREADY has that doesn't
    require homestead -- e.g. a Disabled Veteran exemption -- the same way
    the "How Your Exemptions Reduce Your Bill" table on this page already
    proves it does (that table back-derives each entity's existing exemption
    as assessed_value - (amount_due * 100 / rate)). A version of this
    function that recomputed current_est_tax from assessed_value * rate
    would ignore that existing exemption entirely and overstate the parcel's
    real current tax burden -- visually contradicting the real total shown
    elsewhere on the same page for any parcel with a non-homestead exemption
    already on file. estimated_annual_savings is unaffected by this either
    way (the existing exemption cancels out of the subtraction), so only
    current_est_tax / with_hs_est_tax needed reconciling, not the headline
    savings number.

    10% CAP TERM (July 2026, per Fable review P0-4): filing a homestead
    exemption doesn't just remove SCHOOL_HS_EXEMPTION/local-option dollars
    from the taxable base -- for a parcel whose value is climbing fast, it
    ALSO caps how much the taxable value can grow year-over-year (Tax Code
    SS23.23(a), HOMESTEAD_CAP_PCT = 10%/year). Without a homestead on file,
    a residential parcel gets NO such protection -- its assessed value can
    (and per TCAD's 2026 preliminary roll, sometimes does) jump straight to
    market value in one year. That uncapped jump is exactly the scenario
    `preliminary_assessed`/`preliminary_market` are for: when supplied
    (the parcel's 2026 preliminary row), this adds a second savings term --
    the tax that would be avoided on the portion of the preliminary value
    above 110% of the CURRENT (assessed_value param) assessed value --
    using the same combined entity rate this function already sums while
    walking entity_detail, so no second rate figure is threaded in from the
    caller. Uses min(preliminary_assessed, preliminary_market) as the
    uncapped-growth figure, defensively, the same "don't let AV exceed MV"
    guard already used elsewhere in this module.

    TIMING CAVEAT -- flagged, not guessed: Tax Code SS23.23(c) is explicit
    that the cap "takes effect ... on January 1 of the tax year FOLLOWING
    the first tax year the owner qualifies the property for" the exemption.
    Every parcel this function is invoked for (per the caller-responsibility
    note above) has NO current exemption, so the very first year they could
    file is necessarily their first qualifying year -- meaning, read
    literally, the cap CANNOT protect that first year's value at all; the
    protection begins the year after filing. The dollar magnitude below is
    still computed against the 2026 preliminary jump (the clearest available
    signal of the parcel's uncapped growth rate and the benefit this
    exemption starts unlocking), but is captured in `cap_savings_estimated`
    as a SEPARATE field from `estimated_annual_savings` specifically so the
    caller can label it accurately as a future-year benefit rather than a
    same-year one -- see property.html's rendering of this field for the
    resulting copy, and the Overnight Brief P0-4 reply for the full
    citation. This is a deliberate, flagged deviation from the brief's
    literal "~$7,500 from the 10% cap this year" framing.

    Returns None if there's no rate/billing data to compute from, or if
    neither the exemption savings nor the cap savings comes out positive.
    """
    if not entity_detail or not assessed_value:
        return None

    current_total   = 0.0  # REAL billed total -- see reconciliation note above
    savings_total   = 0.0  # marginal reduction from adding HS / local-option exemption
    combined_rate   = 0.0  # sum of every entity's own rate -- for the cap term below;
                            # equals property_detail()'s assumed_rate_2026 by construction
                            # (same entity_detail, same "sum of per-entity rate" arithmetic)
                            # but recomputed here rather than threaded in as an extra
                            # parameter, so this function never depends on a second,
                            # independently-computed copy of the same number.
    any_rate = False
    any_billed = False
    local_option_entities_applied = []
    for e in entity_detail:
        rate = e.get("rate")
        if not rate:
            continue
        any_rate = True
        rate = float(rate)
        combined_rate += rate
        entity_code = e.get("entity_code")
        entity_name = e.get("entity_name") or entity_code
        is_school = _is_school_entity(entity_name)
        is_local_option = (not is_school) and _local_option_20pct_entity(entity_code, entity_name)

        if is_school:
            exempt = SCHOOL_HS_EXEMPTION
        elif is_local_option:
            exempt = max(float(assessed_value) * LOCAL_OPTION_PCT, LOCAL_OPTION_MIN_EXEMPTION)
            local_option_entities_applied.append(entity_name or entity_code)
        else:
            exempt = 0

        # amount_due is the real, verified billed figure for this entity --
        # already net of any exemption currently on file. `is not None`
        # (not a truthy check) so a genuine $0 bill isn't confused with
        # missing data -- entity_detail is driven FROM tax_billing_entity
        # (see app.py), so every row here already carries a real amount_due
        # in the normal case; the assessed*rate fallback below only covers
        # the rare case where that field is unexpectedly null.
        amount_due = e.get("amount_due")
        if amount_due is not None:
            any_billed = True
            current_total += float(amount_due)
        else:
            current_total += round(float(assessed_value) * rate / 100)

        # Marginal savings from adding this exemption, independent of
        # whatever exemption (if any) already reduced amount_due -- algebraically
        # (assessed-E)*rate/100 - (assessed-E-exempt)*rate/100 == exempt*rate/100
        # for any existing exemption E, so this stacks correctly on the real
        # current_total above without needing to know E.
        # Left unrounded here (rounded once at the very end, on the totals) --
        # rounding each entity's marginal reduction before summing would add a
        # few dollars of avoidable drift between with_hs_est_tax and the true
        # per-entity "keep the existing exemption, add this one too" total.
        savings_total += exempt * rate / 100

    if not any_rate or not any_billed:
        return None

    savings = savings_total

    # 10% cap term -- see docstring. Only computed when the caller supplied a
    # 2026 preliminary row to model against; min() defensively guards against
    # a preliminary_assessed that (data anomaly) exceeds preliminary_market,
    # same pattern used elsewhere in this module.
    cap_savings = 0.0
    if preliminary_assessed and preliminary_market and combined_rate > 0:
        uncapped_value  = min(float(preliminary_assessed), float(preliminary_market))
        cap_protected_at = (1.0 + HOMESTEAD_CAP_PCT) * float(assessed_value)
        cap_savings = max(0.0, uncapped_value - cap_protected_at) * combined_rate / 100

    if savings <= 0 and cap_savings <= 0:
        return None

    with_hs_total = current_total - savings

    result = {
        "current_est_tax":          round(current_total),
        "with_hs_est_tax":          round(with_hs_total),
        "estimated_annual_savings": round(savings),
        "school_hs_exemption":      SCHOOL_HS_EXEMPTION,
        "local_option_pct":         LOCAL_OPTION_PCT,
        "local_option_entities":    local_option_entities_applied,
        # Cap-term fields (July 2026, P0-4). Deliberately separate from
        # estimated_annual_savings/with_hs_est_tax above rather than folded
        # in -- see TIMING CAVEAT in the docstring: this benefit doesn't
        # start until the tax year after filing, so it must never be
        # presented as part of "this year's" savings total.
        "cap_savings_estimated":    round(cap_savings) if cap_savings > 0 else 0,
        "total_savings_incl_cap":   round(savings + cap_savings),
        "homestead_cap_pct":        HOMESTEAD_CAP_PCT,
    }
    return result


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

    # Same root-cause fix as build_projections()'s has_hs_cap and
    # build_insights()'s hs_history_* (July 2026, "Fix Remaining
    # Homestead-Cap Gaps" Cowork brief, item 3): hs_cap_loss is only ever
    # populated by the 2021-2024 AJR loader, never by the 2025 Certified
    # loader current_yr_row is sourced from here (see load_certified_2025.py
    # -- no hs_cap_loss column in its INSERT). seller_has_homestead already
    # correctly reads exemption_codes (2025-sourced, real) and isn't
    # affected by this gap, but pairing it with "hs_cap_loss > 0" made
    # cap_was_active structurally always False regardless -- the seller
    # could have a real, active cap and this would still report none.
    #
    # Fixed: when hs_cap_loss is genuinely present (a real, precise
    # AJR-recorded figure, for any future row that does carry one), use it
    # as-is. Otherwise approximate it as market_value - assessed_value --
    # this module's OWN docstring already defines "cap loss" as exactly
    # "(market − assessed)" (see the file header, mechanic #1) and the
    # Value-vs-Taxable chart / homestead savings cap term compute this same
    # quantity elsewhere on the platform, so this isn't a new definition,
    # just applying the existing one where the AJR-specific field is
    # unavailable. hs_cap_loss_is_approx flags which case this is so the
    # assumptions string below can say so honestly rather than presenting an
    # estimate as an AJR-confirmed figure.
    hs_cap_loss_is_approx = False
    if hs_cap_loss <= 0 and market_value > assessed_value:
        hs_cap_loss = market_value - assessed_value
        hs_cap_loss_is_approx = True
    cap_was_active = seller_has_homestead and hs_cap_loss > 0

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
            (
                f"Cap reset: seller's HS cap loss ${hs_cap_loss:,.0f}"
                + (" (estimated as market − assessed value; no AJR-recorded cap-loss figure on file for this parcel)"
                   if hs_cap_loss_is_approx else "")
                + " does NOT transfer to buyer"
            )
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
        "hs_cap_loss_is_approx":     hs_cap_loss_is_approx,
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
        # Exposed for the property page's Custom-assumptions panel (July 2026,
        # per Diego's "Property Page Polish Round" item 2) -- these two growth
        # caps were already real constants driving the multi-year projection
        # loop above (HOMESTEAD_CAP_PCT / CIRCUIT_BREAKER_CAP_PCT), just never
        # returned to the caller before. Statutory, not tunable -- returned so
        # the UI can show them as read-only, sourced-from-server figures
        # rather than hardcoding a second copy of these numbers client-side.
        "homestead_cap_pct":         HOMESTEAD_CAP_PCT,
        "circuit_breaker_cap_pct":   CIRCUIT_BREAKER_CAP_PCT,
    }
