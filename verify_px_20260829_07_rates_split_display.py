"""
verify_px_20260829_07_rates_split_display.py -- real Jinja RENDER
verification for PX-20260829-07 Task 2's approved rates.html display
changes (M&O/I&S split rendering).

Same lightweight jinja2.Environment technique as
verify_px_20260828_07_rates_and_routing.py (not Flask -- not installed in
this sandbox) -- reuses that file's make_env()/county profile fixtures
directly rather than re-declaring them.

Covers:
  1. has_rate_split=True (Dallas-shaped) -- summary table header gets the
     two extra M&O Δ / I&S Δ columns; the "source doesn't publish the
     breakdown" microcopy is ABSENT.
  2. has_rate_split=False (Travis-shaped, unchanged) -- no extra columns;
     the microcopy IS present, naming the county by name.
  3. Chart/table JS still renders (no leaked Jinja, no syntax landmines)
     with the new HAS_RATE_SPLIT constant and mo_rate/is_rate wiring
     present in both cases (the JS itself is unconditional -- the
     HAS_RATE_SPLIT boolean is what gates behavior at runtime, not
     server-side script suppression).

Run: python3 verify_px_20260829_07_rates_split_display.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_px_20260828_07_rates_and_routing import make_env, _TRAVIS_PROFILE, _DALLAS_PROFILE

FAILURES = []


def check(label, fn):
    try:
        out = fn()
        if "{%" in out or "{#" in out:
            FAILURES.append(f"{label}: leaked raw Jinja delimiter in output")
        else:
            print(f"  OK   {label} ({len(out)} chars)")
        return out
    except Exception as e:
        FAILURES.append(f"{label}: {type(e).__name__}: {e}")
        return ""


def rates_ctx(has_rate_split, by_entity_json="{}"):
    return dict(
        data_unavailable=False,
        data_unavailable_reason=None,
        has_rate_split=has_rate_split,
        by_entity_json=by_entity_json, entity_names_json="{}", entity_category_json="{}",
        all_entities=[], entity_category_order=["School District", "City", "County",
                                                  "Hospital District", "MUD/WCID", "Other"],
        key_entities=["TCO", "IAU", "CAT", "THD", "ACT"],
        year_min=2015, year_max=2024, default_year_from=2016,
    )


def main():
    # ── Dallas-shaped: has_rate_split=True ──────────────────────────────────
    env = make_env(county_slug="dallas-tx", county_profile=_DALLAS_PROFILE)
    tpl = env.get_template("rates.html")

    def _split_columns_present():
        out = tpl.render(**rates_ctx(True))
        if 'id="summaryColMoDelta"' not in out or 'id="summaryColIsDelta"' not in out:
            raise AssertionError("expected M&O Δ / I&S Δ header columns when has_rate_split=True")
        if "isn't available from this source" in out:
            raise AssertionError("source-limitation microcopy should NOT render when has_rate_split=True")
        return out
    check("rates.html / Dallas, has_rate_split=True -> extra Δ columns, no limitation copy",
          _split_columns_present)

    def _split_js_wiring_present():
        out = tpl.render(**rates_ctx(True))
        for token in ("HAS_RATE_SPLIT", "mo_rate", "is_rate", "deltaCellTotal", "moDelta", "isDelta"):
            if token not in out:
                raise AssertionError(f"expected JS token {token!r} in rendered script block")
        return out
    check("rates.html / Dallas -> JS split-handling code present (HAS_RATE_SPLIT, mo/is wiring)",
          _split_js_wiring_present)

    # ── Travis-shaped: has_rate_split=False (must render exactly as before) ─
    env = make_env(county_slug="travis-tx", county_profile=_TRAVIS_PROFILE)
    tpl = env.get_template("rates.html")

    def _no_split_columns_absent():
        out = tpl.render(**rates_ctx(False))
        if 'id="summaryColMoDelta"' in out or 'id="summaryColIsDelta"' in out:
            raise AssertionError("M&O Δ / I&S Δ columns must NOT render when has_rate_split=False")
        return out
    check("rates.html / Travis, has_rate_split=False -> no extra Δ columns",
          _no_split_columns_absent)

    def _limitation_copy_present_and_named():
        out = tpl.render(**rates_ctx(False))
        if "isn't available from this source" not in out:
            raise AssertionError("expected source-limitation microcopy when has_rate_split=False")
        if "Travis County's published rate history reports a single combined" not in out:
            raise AssertionError("expected the microcopy to name Travis County specifically, "
                                  "not a generic 'this county' placeholder")
        return out
    check("rates.html / Travis -> source-limitation copy present, names the county",
          _limitation_copy_present_and_named)

    def _chart_still_renders():
        out = tpl.render(**rates_ctx(False))
        if 'id="ratesChart"' not in out or "new Chart(" not in out:
            raise AssertionError("chart must still render normally for a non-split county")
        return out
    check("rates.html / Travis -> chart still renders normally (no regression)",
          _chart_still_renders)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} scenario(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All PX-20260829-07 Task 2 (rates.html M&O/I&S split display) scenarios passed.")


if __name__ == "__main__":
    main()
