"""
data_coverage.py -- Field-level data coverage manifest.

("Homestead-Cap Data Integrity: Full Fix Set" Cowork brief, July 2026,
Cross-cutting deliverable.)

Why this exists: three separate bugs fixed in this same brief -- Issue 1's
2026 "assessed == market" ambiguity, Issue 2's risk_homestead_cap_expiry
fan-out, and Issue 3's structurally-always-false `hs_cap_loss > 0` checks --
all trace back to the same root pattern: a field that LOOKS reliably
populated is actually sparse, zero, or scope-limited in specific years or
data sources, and nothing in the codebase declared that up front. Each bug
was found only after it produced a wrong answer on a real, named parcel.

This manifest is the single, explicit, checked-in source of truth for "how
much can you actually trust field X, in year Y" -- so the next engineer
consults a table before writing a new consumer, instead of discovering the
gap live, in production, on a real user's page.

The percentages below are SEEDED directly from confirmed live-DB queries
Diego ran himself (per his July 2026 Cowork brief) -- they are NOT
re-derived, estimated, or sampled in this sandbox, which has no live DB
access. Do not hand-edit these numbers without a fresh live query backing
the change; this module's own harness assertions (see
verify_property_html_render.py, "Data Coverage Manifest" section) guard
against silent drift by checking DECLARED BANDS around these numbers, not
by re-deriving them independently -- a sandbox with no DB access has no way
to independently confirm an exact percentage, only to notice if a future
edit pushes a number outside the range Diego confirmed.
"""

# ── hs_cap_loss ─────────────────────────────────────────────────────────
# % of parcel_tax_year rows for that tax year carrying a REAL, AJR-recorded
# homestead-cap-loss dollar figure in hs_cap_loss (i.e. a genuine, sourced
# number -- not merely "column is non-null due to a default").
HS_CAP_LOSS_COVERAGE = {
    2021: 0.911,
    2022: 0.999,
    2023: 0.999,
    2024: 0.999,
    2025: 0.000,
    2026: 0.000,
}
# hs_cap_loss is a real AJR-recorded figure only for 2021-2024 (91.1% /
# 99.9% / 99.9% / 99.9% of rows respectively). 2025 and 2026 are CONFIRMED
# EXACTLY 0.0% -- the field is never populated for those years by any
# current loader, not "usually empty." Any `hs_cap_loss > 0` (or bare
# truthiness) check against a 2025 or 2026 row is not merely unreliable, it
# is a tautological False -- this was the exact bug fixed twice this round
# (property.html line ~3203, and the newly-found 5th instance in
# compare.html's Cap Loss row) and is the pattern the code-side lint below
# exists to catch permanently.

# ── exemption_codes ─────────────────────────────────────────────────────
# % of parcel_tax_year rows for that tax year carrying ANY value in
# exemption_codes (not specifically "HS" -- any exemption code at all).
EXEMPTION_CODES_COVERAGE = {
    2021: 0.465,
    2022: 0.504,
    2023: 0.541,
    2024: 0.548,
    2025: 0.551,
    2026: 0.528,
}
# Sits at roughly half of all rows in every year on file (46.5%-55.1%) --
# NOT because half of parcels lack any exemption, but because this field is
# only meaningfully populated for residential/homestead-eligible parcel
# classes; commercial, land, and other non-homestead-eligible rows
# legitimately carry no exemption_codes value at all. A missing/blank
# exemption_codes value is therefore NOT on its own evidence of "no
# homestead exemption on this parcel" -- it must be read together with
# prop_type/state_cd1 to distinguish "ineligible class, correctly blank"
# from "eligible class, exemption data genuinely absent." Use
# `tax_logic.texas._has_homestead_exemption()` (the established accessor,
# already null/case/separator-safe) rather than a raw membership check.

# ── state_cd1 ────────────────────────────────────────────────────────────
# state_cd1 is not a year-by-year coverage percentage -- it is populated
# ONLY on AJR-sourced rows (data_source starting 'ajr_'). Certified,
# preliminary, and taxcur rows do not carry this field at all.
STATE_CD1_SCOPE = "AJR-only"
# Any filter of the shape `state_cd1 LIKE 'A%'` (or any other state_cd1
# predicate) implicitly scopes its result set to the AJR-sourced subset of
# parcel_tax_year, whether or not that scoping was the intent of whoever
# wrote the query. This is exactly the mechanism behind the 85,565-vs-66,920
# risk_homestead_cap_expiry population discrepancy earlier in this same
# round: Diego's own live count included a `state_cd1 LIKE 'A%'` filter: an
# earlier simplified query he'd been given to approximate it did not, and
# the two counts diverged for that reason alone, not because of a bug in
# the underlying flag logic.

# Fields this manifest currently covers with a year-keyed percentage table
# (used by coverage_band() / is_reliable() below).
_PERCENT_TABLES = {
    "hs_cap_loss": HS_CAP_LOSS_COVERAGE,
    "exemption_codes": EXEMPTION_CODES_COVERAGE,
}


def coverage_band(field, tax_year):
    """Return the manifest's confirmed population fraction (0.0-1.0) for
    (field, tax_year), or None if this manifest doesn't cover that
    field/year combination. state_cd1 is intentionally not covered here --
    see is_ajr_scoped_field() for its scope-only note instead of a
    per-year percentage."""
    table = _PERCENT_TABLES.get(field)
    if table is None:
        return None
    return table.get(tax_year)


def is_reliable(field, tax_year, min_coverage=0.50):
    """True only if the manifest's confirmed population fraction for this
    field/year meets or exceeds min_coverage. An unknown field/year
    combination returns False, not None/True -- a field this manifest
    doesn't yet cover must be treated as unverified by callers, never
    assumed safe by default."""
    pct = coverage_band(field, tax_year)
    return pct is not None and pct >= min_coverage


def is_ajr_scoped_field(field):
    """True for fields (currently just state_cd1) that only exist on
    AJR-sourced rows -- a reminder to callers that filtering on this field
    silently scopes the whole query to the AJR subset of the data."""
    return field == "state_cd1"


if __name__ == "__main__":
    print("── Data Coverage Manifest ──")
    print()
    print("hs_cap_loss (real AJR-recorded cap-loss $ figure):")
    for yr, pct in sorted(HS_CAP_LOSS_COVERAGE.items()):
        flag = "  <- structurally always-false zone, do not read raw" if pct == 0 else ""
        print(f"  {yr}: {pct * 100:5.1f}%{flag}")
    print()
    print("exemption_codes (any value present, not specifically 'HS'):")
    for yr, pct in sorted(EXEMPTION_CODES_COVERAGE.items()):
        print(f"  {yr}: {pct * 100:5.1f}%")
    print()
    print(f"state_cd1: {STATE_CD1_SCOPE} -- absent on certified/preliminary/taxcur rows")
