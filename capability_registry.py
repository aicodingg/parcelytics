"""
capability_registry.py -- Mission 4 (Capability Contract measurement layer),
Deliverable D1: the canonical field capability taxonomy.

This is a MEASUREMENT/CAPABILITY registry -- what a "field" is, how it is
measured, what population it is measured against, and what threshold governs
whether it renders. It deliberately does NOT contain any county-specific
source-column mapping (e.g. "DCAD's SPTD_CODE maps to classi_cd") -- that is
Mission 6's declarative source-field mapping registry, a different and later
concern per the Mission 4 brief §3/§13.

Three field classes only (per the Mission 4 brief §2.1/§2.2 -- a PM ruling
already made, not reopened here):

  attribute        -- literal all-parcels fraction denominator.
  class_conditional -- fraction over a definable internal population
                       (a predicate this registry states explicitly and
                       reproducibly, per §4/D4's requirement).
  sparse_by_nature -- source-column-found-and-mapped structural gate, plus a
                      per-field sanity floor ONLY where an appropriate
                      external county-published reference exists; otherwise
                      the structural measurement is reported honestly with
                      no external validation threshold invented for it.

The brief explicitly forbids inventing a fourth class now (§2.2) -- any field
that doesn't fit one of the three below must be registered with
field_class=None and a `[PM ruling needed]` note, never silently forced into
the closest-fitting class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Field class constants (the three, and only three, per §2.2) ────────────
ATTRIBUTE = "attribute"
CLASS_CONDITIONAL = "class_conditional"
SPARSE_BY_NATURE = "sparse_by_nature"

VALID_FIELD_CLASSES = (ATTRIBUTE, CLASS_CONDITIONAL, SPARSE_BY_NATURE)


@dataclass(frozen=True)
class FieldDefinition:
    """One canonical field's measurement contract. Every attribute here is
    the MEASUREMENT-layer definition of the field -- never a per-county
    fact (that lives in the measured county_field_coverage table / D2)."""

    # Identity
    canonical_field: str          # e.g. "classi_cd" -- matches the real column
    display_label: str            # human-readable, for UI/report use

    # Classification (§2.1/§2.2)
    field_class: Optional[str]    # one of VALID_FIELD_CLASSES, or None if
                                   # this field doesn't fit any of the three
                                   # and needs a [PM ruling needed] (see
                                   # `pm_ruling_needed` below)

    # Measurement
    table: str                    # table the field's column actually lives on
    measurement_method: str       # short, reproducible description of HOW
                                   # coverage is computed (the exact
                                   # predicate/SQL shape lives in
                                   # loaders/field_coverage_gate.py -- this is
                                   # the human-readable summary of it)
    population_predicate: Optional[str] = None
        # For class_conditional: the WHERE-clause fragment defining the
        # applicable population (e.g. "prop_type_cd = 'R'"). None for
        # attribute fields (population = all county parcels, unconditionally).
        # For sparse_by_nature fields this documents the DENOMINATOR used
        # for the structural-measurement fraction, not a gating condition.

    # Threshold / sanity floor
    min_coverage_threshold: float = 0.30
        # Named constant reference -- see capability_state.COVERAGE_THRESHOLD,
        # the single authoritative source of the number itself. This field
        # exists so a future field COULD override the default with a
        # different, explicitly-ruled threshold without touching
        # capability_state.py's global constant; no field in this initial
        # registry does that (per brief §D3: "one named configuration/
        # constant", not scattered literals) -- every entry below inherits
        # the default and none pins a different value.
    has_sanity_floor: bool = False
        # True only for sparse_by_nature fields where an external,
        # county-published reference value is registered (see
        # loaders/field_coverage_gate.py's SANITY_FLOOR_REFERENCES). False
        # (the correct, honest default) when no such reference exists yet --
        # per brief §D4, "otherwise report the structural measurement without
        # fabricating an external validation threshold."

    # Provenance / UI wiring
    source_provenance_note: str = ""
        # What kind of source evidence this field's presence depends on
        # (free text -- not a mapping, just a note for whoever builds the
        # Mission 6 mapping registry later).
    ui_component: str = ""        # which template/module renders this field
    validation_status: str = "unvalidated"
        # one of: "unvalidated", "measured", "reviewed" -- whether this
        # field's registry entry itself (not a per-county measurement) has
        # been checked against real source-file evidence. Every entry below
        # is "unvalidated" except where stated otherwise, per the evidence
        # discipline this repo already applies elsewhere.
    pm_ruling_needed: Optional[str] = None
        # Non-None only when field_class is None or when some other aspect
        # of this field's registry entry needs a PM ruling before it can be
        # measured for real (per §2.2's explicit instruction).


# ── The initial registry ────────────────────────────────────────────────────
# Deliberately small and exactly matches the fields the Mission 4 brief
# itself names as class examples (§2.1) plus the two Dallas-passing-case
# fields the PM preamble supplies (situs_address/owner_name, state_cd1).
# This is NOT meant to be the final, complete field inventory -- extending it
# to every parcel/parcel_tax_year column is future work once Mission 6's
# mapping registry exists to feed it real per-county source evidence.

FIELD_REGISTRY: dict[str, FieldDefinition] = {

    # ── Attribute fields (literal all-parcels denominator, brief §2.1) ─────
    "situs_address": FieldDefinition(
        canonical_field="situs_address",
        display_label="Property Address",
        field_class=ATTRIBUTE,
        table="parcel",
        measurement_method="COUNT(situs_address IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) over all county parcels",
        source_provenance_note="Real, verbatim source-header derivation "
            "(PX-20260827-06 for Dallas; the incident that established the "
            "verbatim-header-fixture rule in parcelytics.md §4).",
        ui_component="property.html header / search results",
        validation_status="measured",
    ),
    "owner_name": FieldDefinition(
        canonical_field="owner_name",
        display_label="Owner Name",
        field_class=ATTRIBUTE,
        table="parcel",
        measurement_method="COUNT(owner_name IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) over all county parcels",
        source_provenance_note="Same PX-20260827-06 derivation as situs_address.",
        ui_component="property.html header",
        validation_status="measured",
    ),
    "legal_desc": FieldDefinition(
        canonical_field="legal_desc",
        display_label="Legal Description",
        field_class=ATTRIBUTE,
        table="parcel",
        measurement_method="COUNT(legal_desc IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) over all county parcels",
        ui_component="property.html Basic Property Information",
    ),
    "state_cd1": FieldDefinition(
        canonical_field="state_cd1",
        display_label="Property Type (State Code)",
        field_class=ATTRIBUTE,
        table="parcel",
        measurement_method="COUNT(state_cd1 IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) over all county parcels",
        source_provenance_note="Comptroller/PTAD state property-use code -- "
            "coarse type classification, sourced independently of the more "
            "specific classi_cd improvement-level code (see classi_cd below).",
        ui_component="property.html Basic Property Information; "
                      "parcel_filters.py classification fallback",
    ),
    "neighborhood_cd": FieldDefinition(
        canonical_field="neighborhood_cd",
        display_label="Neighborhood Code",
        field_class=ATTRIBUTE,
        table="parcel",
        measurement_method="COUNT(neighborhood_cd IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) over all county parcels",
        source_provenance_note="Travis: AJR field[16], loaded by load_ajr.py -- "
            "96.7% of parcels (500,439 of 517,614) as of June 2026 "
            "(KNOWN_LIMITATIONS.md's neighborhood_cd section). Dallas: DCAD's "
            "neighborhood field was never mapped by load_dallas_certified.py "
            "-- 0% (PX-20260901-03 Task 1's _county_has_neighborhood_data() "
            "finding). Included in this registry specifically because both "
            "counties already have an independently-sourced, real, on-file "
            "measurement -- a second genuine Available-vs-Unavailable "
            "demonstration pair alongside classi_cd (D6), not just one.",
        ui_component="Market Snapshot Top/Bottom Moving Neighborhoods; "
                      "Peer Set/benchmark neighborhood match",
        validation_status="measured",
    ),

    # ── Class-conditional fields (population predicate, brief §2.1) ────────
    "classi_cd": FieldDefinition(
        canonical_field="classi_cd",
        display_label="Property Use Code (Classification)",
        field_class=CLASS_CONDITIONAL,
        table="parcel",
        measurement_method="COUNT(classi_cd IS NOT NULL AND TRIM != '') "
                            "/ COUNT(*) WHERE population_predicate",
        population_predicate="prop_type_cd = 'R'",
        source_provenance_note="Improvement-level use code, sourced from "
            "IMP_INFO.TXT for Travis (KNOWN_LIMITATIONS.md's classi_cd "
            "section) -- only meaningful for real property; personal/"
            "mineral accounts never carry one, so 'R'-only is the correct "
            "population, not an all-parcels denominator.",
        ui_component="property.html Property Info; Peer Set / Market "
                      "Snapshot classification (label_case_sql())",
        validation_status="measured",
    ),
    "living_area_sqft": FieldDefinition(
        canonical_field="living_area_sqft",
        display_label="Living Area (sq ft)",
        field_class=CLASS_CONDITIONAL,
        table="parcel",
        measurement_method="COUNT(living_area_sqft > 0) / COUNT(*) "
                            "WHERE population_predicate",
        population_predicate="prop_type_cd = 'R'",
        source_provenance_note="Structure square footage -- meaningless for "
            "personal/mineral accounts; same population reasoning as classi_cd.",
        ui_component="property.html Basic Property Information",
    ),
    "gross_building_area_sqft": FieldDefinition(
        canonical_field="gross_building_area_sqft",
        display_label="Gross Building Area (sq ft)",
        field_class=CLASS_CONDITIONAL,
        table="parcel",
        measurement_method="COUNT(gross_building_area_sqft > 0) / COUNT(*) "
                            "WHERE population_predicate",
        population_predicate="prop_type_cd = 'R'",
        ui_component="property.html Basic Property Information",
    ),
    "year_built": FieldDefinition(
        canonical_field="year_built",
        display_label="Year Built",
        field_class=CLASS_CONDITIONAL,
        table="parcel",
        measurement_method="COUNT(year_built IS NOT NULL) / COUNT(*) "
                            "WHERE population_predicate",
        population_predicate="prop_type_cd = 'R'",
        source_provenance_note="Dallas: unmapped by load_dallas_certified.py "
            "as of PX-20260901-04 Task 3's RES_DETAIL/COM_DETAIL investigation "
            "-- 0% (Mission 4 preamble). Included as the second explicit "
            "Dallas class-conditional 'Unavailable' case alongside classi_cd.",
        ui_component="property.html Basic Property Information; "
                      "search.html Year Built range filter",
        validation_status="measured",
    ),

    # ── Sparse-by-nature fields (structural gate + optional sanity floor,
    #    brief §2.1/§4) ───────────────────────────────────────────────────
    "exemption_codes": FieldDefinition(
        canonical_field="exemption_codes",
        display_label="Exemptions",
        field_class=SPARSE_BY_NATURE,
        table="parcel_tax_year",
        measurement_method="structural: source column exists + mapped, then "
            "COUNT(exemption_codes IS NOT NULL AND TRIM != '') / COUNT(*) "
            "over parcel_tax_year rows for (county, tax_year) -- a low "
            "fraction here is NOT itself evidence of a loading gap (see "
            "data_coverage.py's existing 46.5%-55.1% Travis figures); it is "
            "evidence only when the STRUCTURAL gate (source column mapped) "
            "also fails, which is exactly Dallas's confirmed case.",
        population_predicate="county_code = %s AND tax_year = %s "
                              "(no further filter -- see note: an all-rows "
                              "denominator is deliberately used for the "
                              "STRUCTURAL fraction; §7's population-scoped "
                              "open question governs whether a NARROWER "
                              "denominator should also be computed and is "
                              "explicitly NOT resolved by this registry)",
        has_sanity_floor=False,
        source_provenance_note="PX-20260901-02: Dallas's DCAD exemption "
            "field was never mapped by load_dallas_certified.py/"
            "dcad_format.py -- 0 of 703,446 (2025) / 705,536 (2026) per "
            "KNOWN_LIMITATIONS.md. This is the field's own prior, more "
            "narrowly-scoped live measurement; see D6/D10 for how this "
            "registry's structural gate would have caught the same fact.",
        ui_component="property.html Exemptions row; compare.html; "
                      "Post-Acquisition Estimator; homestead lead filter",
        validation_status="measured",
        pm_ruling_needed="§7's open denominator question (all-parcels vs. "
            "population-scoped fraction for sparse fields) is explicitly "
            "NOT resolved here -- this registry entry uses the structural "
            "gate (source column mapped Y/N) as the PRIMARY capability "
            "signal for sparse_by_nature fields specifically so that "
            "resolution of §7 is not a blocker for Mission 4's stop "
            "condition. A future population-scoped fraction can be added "
            "alongside once ruled.",
    ),
    "hs_cap_loss": FieldDefinition(
        canonical_field="hs_cap_loss",
        display_label="Homestead Cap Loss",
        field_class=SPARSE_BY_NATURE,
        table="parcel_tax_year",
        measurement_method="structural: source column exists + mapped, then "
            "COUNT(hs_cap_loss IS NOT NULL) / COUNT(*) over parcel_tax_year "
            "rows for (county, tax_year)",
        population_predicate="county_code = %s AND tax_year = %s",
        has_sanity_floor=False,
        source_provenance_note="data_coverage.py's existing HS_CAP_LOSS_COVERAGE "
            "manifest (91.1%/99.9%/99.9%/99.9% 2021-2024, 0.0% 2025-2026 for "
            "Travis) is the field's own prior, hand-seeded measurement -- "
            "this registry's structural gate is the generalized, "
            "county-aware, mechanically-measured version of the same fact.",
        ui_component="property.html Homestead Cap History panel",
        validation_status="measured",
    ),
    "tax_delinquent": FieldDefinition(
        canonical_field="tax_delinquent",
        display_label="Delinquency Data",
        field_class=SPARSE_BY_NATURE,
        table="tax_delinquent",
        measurement_method="structural, boolean-shaped: does this county "
            "have ANY row in tax_delinquent at all (has the county's "
            "delinquency dataset been acquired/loaded)? population_count is "
            "always 1; populated_count is 1 if >=1 row exists for this "
            "county, else 0 -- so coverage_fraction is always exactly 1.0 "
            "(dataset present) or 0.0 (dataset absent), never a delinquency "
            "RATE. Fixed post-M4 (PM review): the original version measured "
            "delinquent_rows / all_parcels, which meant a low, GOOD real-"
            "world delinquency rate would read as Unavailable -- backwards. "
            "Presence of the dataset, not the rate within it, is the "
            "capability signal, matching how exemption_codes/hs_cap_loss "
            "already treat a low rate as data, not as a gap.",
        population_predicate="county_code = %s (whole-table presence check "
            "only -- no all-parcels denominator; see measurement_method)",
        has_sanity_floor=False,
        source_provenance_note="Dallas has no billing/delinquency data "
            "loaded at all (parcelytics.md §16) -- this is the field's "
            "clearest 'entirely unmapped source' case, distinct from "
            "exemption_codes/hs_cap_loss which DO have a mapped column, "
            "just an empty one for Dallas.",
        ui_component="property.html Delinquency panel",
    ),
}


def get_field_definition(field_name: str) -> Optional[FieldDefinition]:
    """Look up a field's canonical definition. Returns None for an
    unregistered field -- callers (county_field_coverage.py,
    capability_state.py) must treat that as "not measurable" per the D2
    requirement to never turn an inability to measure into a false zero."""
    return FIELD_REGISTRY.get(field_name)


def all_registered_fields() -> list[str]:
    return sorted(FIELD_REGISTRY.keys())


def fields_by_class(field_class: str) -> list[str]:
    if field_class not in VALID_FIELD_CLASSES:
        raise ValueError(
            f"'{field_class}' is not one of the three ruled field classes "
            f"{VALID_FIELD_CLASSES} -- per brief §2.2, do not invent a "
            f"fourth class; register the field with field_class=None and a "
            f"pm_ruling_needed note instead."
        )
    return sorted(
        name for name, d in FIELD_REGISTRY.items() if d.field_class == field_class
    )


if __name__ == "__main__":
    print("── Capability Registry (D1) ──")
    for cls in VALID_FIELD_CLASSES:
        print(f"\n{cls}:")
        for name in fields_by_class(cls):
            d = FIELD_REGISTRY[name]
            floor = " [sanity floor registered]" if d.has_sanity_floor else ""
            print(f"  {name:28s} table={d.table:16s} {d.measurement_method[:40]}...{floor}")
