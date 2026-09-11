"""
capability_state.py -- Mission 4, Deliverable D3: the capability-state
decision that consumes measured coverage, plus the county_has_field() /
county_shows_field() replacement.

Capability vs. confidence (brief §2.3, a PM ruling not reopened here):
capability answers "should this field exist on the page for this county?";
confidence answers "how trustworthy is the particular value being
displayed?". Ordering is capability first, confidence second -- an
Unavailable/Unknown field never reaches the confidence system because it
never renders. This module owns capability only; confidence is unchanged
(parcelytics.md §8's existing THE_FABLE_METHOD numbers-checklist labels),
wired together only in parcelytics.md §6's amendment (D8).

Design decision this module makes, disclosed here and in
PX_CAPABILITY_CONTRACT_M4_REPORT.md (the brief specifies four capability
states -- Available/Partial/Unavailable/Unknown -- but does not itself spell
out what distinguishes Available from Partial at the capability layer, only
what each state means for RENDERING, per §D7):

  UNKNOWN     -- measurement did not produce a trustworthy fraction at all
                 (not yet measured / not measurable / failed validation).
                 Never exposed publicly (D7).
  UNAVAILABLE -- measured, coverage_fraction below COVERAGE_THRESHOLD.
                 Omitted entirely (D7), matching parcelytics.md §7's
                 existing ruling exactly.
  AVAILABLE   -- measured, coverage_fraction at/above threshold, and (for
                 sparse_by_nature fields with a registered sanity floor)
                 the sanity floor passed or does not apply. Renders normally.
  PARTIAL     -- measured, coverage_fraction at/above threshold, BUT a
                 registered sanity-floor check against an external
                 reference FAILED (the county-internal fraction looks
                 anomalous against a trusted outside number). Renders, but
                 with an explicit coverage/quality indication (D7) -- this
                 is a capability-layer quality flag, distinct from and
                 upstream of the existing confidence-label system (§8).

This keeps Available/Partial from becoming a second, competing threshold
system (D3's own explicit warning): the ONE number that decides render-at-
all is COVERAGE_THRESHOLD below; Partial vs. Available is not a second
threshold, it is whether an (optional, per-field) external sanity check
passed.
"""
from __future__ import annotations

from typing import Optional


# ── D3: the one named threshold constant (parcelytics.md §7's 30% ruling,
# NOT 50%, NOT scattered literals -- every reader of this module gets this
# same number). ──────────────────────────────────────────────────────────
COVERAGE_THRESHOLD = 0.30

# data_coverage.py's existing is_reliable(min_coverage=0.50) is a SEPARATE,
# pre-existing, confirmed-dead-code threshold (parcelytics.md §7: "never
# called anywhere outside its own definition"). It is not touched or
# repurposed by this module -- COVERAGE_THRESHOLD above is the one
# authoritative capability-layer number; do not read is_reliable()'s 0.50
# as competing with it, and do not merge the two modules. See
# PX_CAPABILITY_CONTRACT_M4_REPORT.md's Known Limitations for this
# disclosure repeated in report form.


# ── Measurement status (consumed from D2/loaders/field_coverage_gate.py) ──
MEASURED = "measured"
NOT_MEASURABLE = "not_measurable"     # e.g. zero population -- a fraction
                                       # cannot be meaningfully computed
UNKNOWN_STATUS = "unknown"            # no measurement has been run yet
FAILED_VALIDATION = "failed_validation"  # the measurement ran but a sanity
                                       # check on the MEASUREMENT ITSELF
                                       # failed (e.g. source column check
                                       # errored) -- distinct from a
                                       # sanity-FLOOR failure, which is a
                                       # property of the measured VALUE,
                                       # not the measurement process.

VALID_MEASUREMENT_STATUSES = (MEASURED, NOT_MEASURABLE, UNKNOWN_STATUS, FAILED_VALIDATION)

# ── Sanity-floor status (only meaningful when a field has one registered) ──
SANITY_FLOOR_PASS = "pass"
SANITY_FLOOR_FAIL = "fail"
SANITY_FLOOR_NOT_APPLICABLE = "not_applicable"  # field has no registered
                                       # external reference -- per D4, this
                                       # is reported honestly, never
                                       # defaulted to "pass"

# ── Capability states (the four named in the brief, D3) ────────────────────
AVAILABLE = "Available"
PARTIAL = "Partial"
UNAVAILABLE = "Unavailable"
UNKNOWN = "Unknown"

VALID_CAPABILITY_STATES = (AVAILABLE, PARTIAL, UNAVAILABLE, UNKNOWN)


def capability_state(
    measurement_status: str,
    coverage_fraction: Optional[float],
    threshold: float = COVERAGE_THRESHOLD,
    sanity_floor_status: str = SANITY_FLOOR_NOT_APPLICABLE,
) -> str:
    """The D3 decision function. Pure, deterministic, no I/O -- takes an
    already-measured record's fields and returns one of the four capability
    states. Never returns anything else; an unrecognized measurement_status
    is treated as UNKNOWN, not as an error, matching this codebase's
    "unknown must never read as covered" convention (county_has_field()'s
    own existing docstring, app.py:2233-2238)."""
    if measurement_status not in (MEASURED,):
        return UNKNOWN
    if coverage_fraction is None:
        return UNKNOWN
    if coverage_fraction < threshold:
        return UNAVAILABLE
    if sanity_floor_status == SANITY_FLOOR_FAIL:
        return PARTIAL
    return AVAILABLE


def should_render(state: str) -> bool:
    """Available and Partial both render (per D7); Unavailable and Unknown
    both do not. This is the single boolean collapse of the four-state
    decision -- county_shows_field()/county_has_field() below both call
    this rather than re-deriving the Y/N logic themselves."""
    return state in (AVAILABLE, PARTIAL)


# ── county_shows_field() -- the new canonical API (D3) ──────────────────────
def county_shows_field(
    county_code: str,
    field_name: str,
    snapshot: Optional[dict] = None,
    legacy_field_coverage: Optional[dict] = None,
) -> bool:
    """Answers based on MEASURED capability, not merely whether a field
    happens to exist somewhere in the database (D3's own explicit
    distinction from the old county_has_field() behavior).

    snapshot: the cached, load-time-read county_field_coverage table
      contents, shaped {(county_code, field_name): CoverageRecord-like dict
      with 'measurement_status', 'coverage_fraction', 'sanity_floor_status'}.
      This is what app.py populates at startup once loaders/
      field_coverage_gate.py has actually run against production (D2) --
      until then it is legitimately empty/None, which is why:

    legacy_field_coverage: the OLD hand-declared
      COUNTY_PROFILES[county_code]["field_coverage"] dict of booleans. Used
      ONLY as a fallback when `snapshot` has no entry for (county_code,
      field_name) -- i.e. this field/county pair hasn't been measured yet.
      This is what makes county_has_field()'s existing six call sites (see
      PX_CAPABILITY_CONTRACT_M4_REPORT.md's before/after section) upgrade
      automatically the moment a real measurement exists, and behave
      EXACTLY as they do today until it does -- no call site needs to
      change, per the Mission 4 preamble's item 6.

    Returns False (never raises, never guesses True) for any county/field
    this function cannot resolve through either path -- same "unknown never
    reads as covered" convention as the function it replaces.
    """
    if snapshot is not None:
        record = snapshot.get((county_code, field_name))
        if record is not None:
            state = capability_state(
                measurement_status=record.get("measurement_status", UNKNOWN_STATUS),
                coverage_fraction=record.get("coverage_fraction"),
                threshold=record.get("min_coverage_threshold", COVERAGE_THRESHOLD),
                sanity_floor_status=record.get("sanity_floor_status", SANITY_FLOOR_NOT_APPLICABLE),
            )
            return should_render(state)
    if legacy_field_coverage is not None:
        return bool(legacy_field_coverage.get(field_name, False))
    return False


def county_capability_state(
    county_code: str,
    field_name: str,
    snapshot: Optional[dict] = None,
) -> str:
    """The full four-state answer (not just the boolean render decision) --
    for D7's render contract, which needs to distinguish Available from
    Partial (different render treatment), not just render-or-not. Has no
    legacy-boolean fallback (the old system only ever had two states, True/
    False -- there is no honest way to backfill Available vs. Partial from
    a boolean, so an unmeasured field correctly reports Unknown here even
    though county_shows_field() above might still resolve it True via the
    legacy fallback)."""
    if snapshot is None:
        return UNKNOWN
    record = snapshot.get((county_code, field_name))
    if record is None:
        return UNKNOWN
    return capability_state(
        measurement_status=record.get("measurement_status", UNKNOWN_STATUS),
        coverage_fraction=record.get("coverage_fraction"),
        threshold=record.get("min_coverage_threshold", COVERAGE_THRESHOLD),
        sanity_floor_status=record.get("sanity_floor_status", SANITY_FLOOR_NOT_APPLICABLE),
    )


def county_has_field(
    county_code: str,
    field_name: str,
    snapshot: Optional[dict] = None,
    legacy_field_coverage: Optional[dict] = None,
) -> bool:
    """Preamble item 6's chosen approach: county_has_field() becomes a THIN
    WRAPPER around the new measured logic, not a parallel/renamed function
    call sites must migrate to. See PX_CAPABILITY_CONTRACT_M4_REPORT.md for
    the full rationale (in short: only ~6 real call sites exist, all in
    app.py/templates behind PX-20260901-05's exemption gating, all boolean-
    context `{% if county_has_field(...) %}` -- a wrapper upgrades every one
    of them automatically the moment app.py's context processor starts
    passing a real snapshot, with zero template changes and zero migration
    risk, which is also the minimal-footprint choice consistent with D7's
    "do not perform a broad property-page rewrite.")

    Identical signature/behavior to county_shows_field() -- this function
    exists only so existing call sites (app.py, templates/*.html) do not
    need to change their function name, per the preamble's explicit
    instruction not to break PX-20260901-05."""
    return county_shows_field(county_code, field_name, snapshot, legacy_field_coverage)
