"""
render_contract.py -- Mission 4, Deliverable D7: the UI compatibility
contract.

This is deliberately NOT a property-page rewrite (the brief is explicit:
"Do not perform a broad property-page rewrite. Instead, establish the
interface that future UI code will consume."). It is the one function
future template/route code calls to get a deterministic answer to:

    Should this field/module render for this county?

The rule (brief §D7, quoted rather than reinterpreted):
    Available   -> Render normally.
    Partial     -> Render, with the appropriate coverage/quality indication.
    Unavailable -> Do not render the component at all.
    Unknown     -> Do not expose publicly.

No template in this repo is wired to this module yet as part of Mission 4
(that broad wiring is exactly the "large-scale UI composition work" the
brief defers to a later mission). county_has_field()'s existing six call
sites continue to work through capability_state.county_has_field()'s
boolean wrapper -- this module is the richer, four-state interface those
call sites will eventually migrate to when a caller needs to distinguish
Available from Partial (e.g. to show a quality badge), not just render/
don't-render.
"""
from capability_state import (
    AVAILABLE,
    PARTIAL,
    UNAVAILABLE,
    UNKNOWN,
)

RENDER_NORMAL = "render_normal"
RENDER_WITH_QUALITY_INDICATOR = "render_with_quality_indicator"
DO_NOT_RENDER = "do_not_render"

_STATE_TO_ACTION = {
    AVAILABLE: RENDER_NORMAL,
    PARTIAL: RENDER_WITH_QUALITY_INDICATOR,
    UNAVAILABLE: DO_NOT_RENDER,
    UNKNOWN: DO_NOT_RENDER,
}


def render_action(capability_state: str) -> str:
    """One of RENDER_NORMAL / RENDER_WITH_QUALITY_INDICATOR / DO_NOT_RENDER
    for a given capability state. Raises on an unrecognized state rather
    than silently defaulting to "render" -- an unrecognized state is a bug
    in the caller (capability_state.capability_state() never returns
    anything outside the four named states), not a case to paper over."""
    if capability_state not in _STATE_TO_ACTION:
        raise ValueError(
            f"{capability_state!r} is not one of the four capability states "
            f"this contract knows how to render -- do not guess a default; "
            f"fix the caller."
        )
    return _STATE_TO_ACTION[capability_state]


def should_render_field(capability_state: str) -> bool:
    """The simplest possible answer: render at all, yes/no. Available and
    Partial both render (with different treatment, see render_action()
    above); Unavailable and Unknown both do not."""
    return render_action(capability_state) != DO_NOT_RENDER


def is_publicly_exposed(capability_state: str) -> bool:
    """D7's explicit Unknown rule ("do not expose publicly") stated as its
    own predicate -- distinct from should_render_field() because a future
    caller (an internal admin/QA view, say) might legitimately want to see
    an Unknown-state field's raw measurement without it ever reaching a
    public page. Available/Partial/Unavailable are all "not secret" (an
    Unavailable field's ABSENCE is itself visible/honest, per §7's existing
    "omitted entirely, not dashed" rule) -- only Unknown is non-public."""
    return capability_state != UNKNOWN
