"""
capability_contract.py -- Mission 4, Deliverable D5: the first usable
County Capability Contract object.

Deliberately minimal, per the brief's own instruction: "Do not overbuild the
Source Registry in this mission if the current repository does not yet have
the necessary source metadata infrastructure." SourceRegistryEntry below is
a plain, empty-by-default shape -- this module does not populate it from
anywhere real (there is no Source Registry data store in this repo yet; the
Notion Source Registry pages referenced in parcelytics.md/DATA_LIFECYCLE.md
are the closest existing thing, and are out of scope to reach into here).
The Mission 4 objective is the field capability measurement layer (D1-D4);
this object exists to show the three pieces conceptually assemble into one
contract, not to be a fully wired subsystem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from capability_registry import FieldDefinition
from capability_state import county_capability_state, COVERAGE_THRESHOLD


@dataclass
class CountyIdentity:
    county_name: str
    state: str
    county_code: str
    slug: str
    certification_status: str = "unvalidated"
        # Free text for now (e.g. "measured", "planned") -- not a controlled
        # vocabulary yet; Mission 6/7 territory once more counties exist to
        # justify one.


@dataclass
class FieldCapability:
    field: str
    field_class: Optional[str]
    measurement_method: str
    population_definition: Optional[str]
    coverage: Optional[float]           # 0.0-1.0 fraction, or None if unmeasured
    capability_state: str               # Available/Partial/Unavailable/Unknown
    validation_status: str
    measured_at: Optional[str] = None   # ISO timestamp string, or None


@dataclass
class SourceRegistryEntry:
    """Minimal shape only -- see module docstring. Every field defaults to
    None/empty; this mission does not populate real values for it."""
    source: str = ""
    agency: str = ""
    source_type: str = ""
    url: str = ""
    acquisition_method: str = ""
    years_available: list = field(default_factory=list)
    archive_location: str = ""
    hash: str = ""
    acquisition_timestamp: Optional[str] = None


@dataclass
class CountyCapabilityContract:
    identity: CountyIdentity
    fields: list[FieldCapability] = field(default_factory=list)
    sources: list[SourceRegistryEntry] = field(default_factory=list)

    def field_capability(self, field_name: str) -> Optional[FieldCapability]:
        for fc in self.fields:
            if fc.field == field_name:
                return fc
        return None


def build_field_capability(field_def: FieldDefinition, snapshot: dict, county_code: str) -> FieldCapability:
    """Assembles one FieldCapability from a registered FieldDefinition (D1)
    plus a measured snapshot (D2's persisted county_field_coverage
    contents, keyed (county_code, field) -- see capability_state.py's
    county_capability_state() for the exact lookup)."""
    record = snapshot.get((county_code, field_def.canonical_field)) if snapshot else None
    coverage = record.get("coverage_fraction") if record else None
    measured_at = record.get("measured_at") if record else None
    state = county_capability_state(county_code, field_def.canonical_field, snapshot)
    return FieldCapability(
        field=field_def.canonical_field,
        field_class=field_def.field_class,
        measurement_method=field_def.measurement_method,
        population_definition=field_def.population_predicate,
        coverage=coverage,
        capability_state=state,
        validation_status=field_def.validation_status,
        measured_at=measured_at,
    )


def build_capability_contract(identity: CountyIdentity, field_defs: list, snapshot: dict) -> CountyCapabilityContract:
    """Assembles the full contract for one county from the registry (D1)
    and a measured snapshot (D2). `sources` is intentionally left empty
    (see SourceRegistryEntry's docstring) -- populating it is future work,
    not part of Mission 4's stop condition."""
    fields = [build_field_capability(fd, snapshot, identity.county_code) for fd in field_defs]
    return CountyCapabilityContract(identity=identity, fields=fields, sources=[])
