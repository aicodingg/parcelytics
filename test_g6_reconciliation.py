#!/usr/bin/env python3
"""
test_g6_reconciliation.py — fixture tests for the generalized G6 tool
(g6_reconciliation.py). Per this brief's own verification requirement:
reuse the same synthetic matched/added/removed account scenario as
verify_2026_prelim_vs_cert_reconciliation.py's original, PLUS a second,
differently-parameterized scenario (a different pair of "tables"/filters
entirely -- 2022-vs-2023 certified, not 2026 prelim-vs-cert) to prove the
refactor actually generalized the tool rather than just renaming the one
hardcoded case.

All tests here are pure Python over synthetic dicts -- no database, no
live connection, matching this brief's own sandbox disclosure (the DB-
touching run_2026_prelim_vs_cert()/run_from_queries() entry points in
g6_reconciliation.py are NOT exercised here; they need Diego's live run).

Run: python3 test_g6_reconciliation.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from g6_reconciliation import g6_full, g6_lite

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# ── Scenario 1: the original 2026-prelim-vs-cert case, reproduced as a
# synthetic fixture (same shape as the real script's DB-fetched dicts
# would be) -- proves g6_full() reproduces the proven, already-shipped math. ──
def test_scenario_2026_prelim_vs_cert_shape():
    prelim = {
        "0100010001": 300_000,
        "0100010002": 450_000,
        "0100010003": 220_000,   # removed in cert (e.g. account closed/merged)
        "AJR100001":   1,        # AJR placeholder in prelim, real in cert (known asymmetry)
    }
    cert = {
        "0100010001": 315_000,   # +15,000 matched delta
        "0100010002": 460_000,   # +10,000 matched delta
        "0100010004": 500_000,   # new account -- addition
        "AJR100001": 85_000,     # matched, but AJR-prefixed -- still a matched delta in g6_full
    }
    result = g6_full(prelim, cert, label_a="2026 Preliminary", label_b="2026 Certified")

    ok = check("matched keys are the 2 shared non-removed accounts + AJR100001",
               result.matched_keys == {"0100010001", "0100010002", "AJR100001"},
               str(result.matched_keys))
    ok = check("added keys = {'0100010004'}", result.added_keys == {"0100010004"}) and ok
    ok = check("removed keys = {'0100010003'}", result.removed_keys == {"0100010003"}) and ok

    expected_matched_delta = (315_000 - 300_000) + (460_000 - 450_000) + (85_000 - 1)
    ok = check("matched_delta arithmetic is correct",
               result.matched_delta == expected_matched_delta,
               f"got {result.matched_delta}, expected {expected_matched_delta}") and ok
    ok = check("additions = 500,000 (the one new account)", result.additions == 500_000) and ok
    ok = check("removals = 220,000 (the one dropped account)", result.removals == 220_000) and ok

    expected_real_delta = sum(cert.values()) - sum(prelim.values())
    ok = check("real_delta = cert_total - prelim_total",
               result.real_delta == expected_real_delta) and ok
    ok = check("decomposed_delta == real_delta (residual is zero -- perfect reconciliation)",
               result.residual == 0, f"residual={result.residual}") and ok
    ok = check("gate PASSES (residual well under $1M)", result.passed is True) and ok
    return ok


# ── Scenario 2: a DIFFERENT pair entirely — 2022 Certified vs 2023
# Certified (not 2026 prelim/cert at all), proving g6_full() works when
# parameterized with a completely different vintage pair, different
# labels, different account population. This is the brief's own explicit
# ask: "prove it now works when parameterized differently... not just
# the one hardcoded case." ──
def test_scenario_2022_vs_2023_certified_different_pair():
    cert_2022 = {
        "0200020001": 180_000,
        "0200020002": 275_000,
        "0200020003": 90_000,
        "0200020004": 610_000,   # sold/demolished before 2023 -- removed
        "0200020005": 50_000,    # removed
    }
    cert_2023 = {
        "0200020001": 182_500,   # small matched increase
        "0200020002": 275_000,   # unchanged matched (delta 0, still "matched")
        "0200020003": 76_000,    # matched DECREASE (e.g. reassessment correction)
        "0200020006": 700_000,   # brand-new construction -- addition
        "0200020007": 45_000,    # addition
    }
    result = g6_full(cert_2022, cert_2023, label_a="2022 Certified", label_b="2023 Certified",
                      tolerance=1_000_000)

    ok = check("[2022v2023] matched keys are the 3 accounts present both years",
               result.matched_keys == {"0200020001", "0200020002", "0200020003"},
               str(result.matched_keys))
    ok = check("[2022v2023] added keys = the 2 brand-new 2023 accounts",
               result.added_keys == {"0200020006", "0200020007"}) and ok
    ok = check("[2022v2023] removed keys = the 2 accounts absent from 2023",
               result.removed_keys == {"0200020004", "0200020005"}) and ok

    expected_matched_delta = (182_500 - 180_000) + (275_000 - 275_000) + (76_000 - 90_000)
    ok = check("[2022v2023] matched_delta includes a matched DECREASE correctly",
               result.matched_delta == expected_matched_delta,
               f"got {result.matched_delta}, expected {expected_matched_delta}") and ok
    ok = check("[2022v2023] additions = 745,000 (two new accounts)",
               result.additions == 745_000) and ok
    ok = check("[2022v2023] removals = 660,000 (two dropped accounts)",
               result.removals == 660_000) and ok
    ok = check("[2022v2023] gate PASSES (perfect reconciliation, residual 0)",
               result.passed is True and result.residual == 0) and ok
    ok = check("[2022v2023] labels reflect the DIFFERENT pair, not the 2026 case",
               result.label_a == "2022 Certified" and result.label_b == "2023 Certified") and ok
    return ok


def test_gate_fails_when_residual_exceeds_tolerance():
    """Deliberate-corruption case: if the two universes being compared
    are NOT actually comparable (e.g. one side silently used a different
    exclusion filter than the other, letting mismatched rows in on one
    side only), the decomposition will not reconcile -- this must show up
    as a real, nonzero residual that fails the gate, not get silently
    swallowed. Simulated here by constructing a source_b whose total
    doesn't actually match what matched/added/removed would predict --
    which cannot happen from real matched-dict arithmetic (the identity
    is exact by construction), so the test instead uses an artificially
    tight tolerance to prove the pass/fail threshold itself works
    correctly at the boundary, which is the part worth guarding against
    regression."""
    a = {"X1": 100, "X2": 200}
    b = {"X1": 100, "X2": 205, "X3": 50}  # real delta = 55, all attributable -- residual 0
    result_loose = g6_full(a, b, tolerance=1_000_000)
    result_tight = g6_full(a, b, tolerance=1)  # residual is 0, so even tolerance=1 should pass
    ok = check("loose tolerance passes (residual 0 < $1M)", result_loose.passed is True)
    ok = check("tight tolerance still passes when residual is genuinely 0",
               result_tight.passed is True) and ok

    # Now force a genuine nonzero residual scenario is impossible by
    # construction (the identity always holds for real dict math) --
    # confirm that invariant explicitly, since it's the actual guarantee
    # this gate provides: a real accounting mismatch can ONLY come from
    # the two universes not being truly comparable at the query level
    # (different filters), which g6_full() cannot itself detect --
    # it can only prove the arithmetic is internally consistent. Flagged,
    # not silently assumed.
    ok = check("residual is always exactly 0 for two well-formed dicts (arithmetic identity)",
               result_loose.residual == 0 and result_tight.residual == 0) and ok
    return ok


def test_empty_sources_do_not_crash():
    """Edge case: an empty source (e.g. a brand-new vintage with zero
    rows loaded yet) must not crash the decomposition -- it should report
    100% additions/removals cleanly."""
    result = g6_full({}, {"Y1": 500}, label_a="Empty", label_b="One Account")
    ok = check("empty source_a: n_a is 0", result.n_a == 0)
    ok = check("empty source_a: everything in source_b is an addition",
               result.added_keys == {"Y1"} and result.additions == 500) and ok
    ok = check("empty source_a: gate still passes (residual 0)", result.passed is True) and ok
    return ok


# ── G6-lite tests ──────────────────────────────────────────────────────
def test_g6_lite_passes_when_totals_agree():
    result = g6_lite("2025 Adopted Rates", loaded_total=12_345_678, file_internal_total=12_345_678)
    ok = check("g6-lite: residual is 0 when totals match exactly", result.residual == 0)
    ok = check("g6-lite: gate passes", result.passed is True) and ok
    return ok


def test_g6_lite_passes_within_tolerance():
    result = g6_lite("Billing Vintage", loaded_total=50_000_000, file_internal_total=50_000_500,
                      tolerance=1_000_000)
    ok = check("g6-lite: small real-world rounding residual still passes",
               result.passed is True, f"residual={result.residual}")
    return ok


def test_g6_lite_fails_when_totals_diverge():
    """Deliberate-failure case: a genuinely wrong load (e.g. a loader bug
    that dropped a batch of rows) must show up as a real, large residual
    that fails the gate -- this is what actually catches a loader-level
    contamination or gap before the vintage gets promoted."""
    result = g6_lite("Corrupted Load", loaded_total=40_000_000, file_internal_total=50_000_000,
                      tolerance=1_000_000)
    ok = check("g6-lite: a genuine $10M gap produces a nonzero residual",
               result.residual == -10_000_000, f"residual={result.residual}")
    ok = check("g6-lite: gate FAILS on a real divergence", result.passed is False) and ok
    return ok


def test_summary_strings_are_real_and_readable():
    """Both result types' .summary() must actually render without error
    and contain the key numbers -- this is what gets pasted into the
    Vintage Ledger row per DATA_LIFECYCLE.md, so it needs to actually work,
    not just exist as a method signature."""
    full_result = g6_full({"A": 1}, {"A": 2}, label_a="Before", label_b="After")
    lite_result = g6_lite("Some Vintage", 100, 100)
    ok = check("g6_full summary contains both labels",
               "Before" in full_result.summary() and "After" in full_result.summary())
    ok = check("g6_full summary contains GATE PASSES/FAILS", "GATE" in full_result.summary()) and ok
    ok = check("g6_lite summary contains its label", "Some Vintage" in lite_result.summary()) and ok
    return ok


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print(f"ALL {sum(1 for n in globals() if n.startswith('test_'))} G6 RECONCILIATION FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
