#!/usr/bin/env python3
"""
test_classification_map_dallas.py — DALLAS-CLASS-1-rev fixture tests for
classification_map_dallas.py.

THE REAL, FULL, CORRECTED SPTB DISTRIBUTION this brief examined (705,536
records total) is reproduced in full below as SPTB_DISTRIBUTION, exactly
as given in the DALLAS-CLASS-1-rev brief. This is the corrected calibration
the alarm must actually encode, per the brief's own item 3 -- not a
simplified or partial stand-in.

DALLAS COUNTY PROFILE NOTE (belongs alongside MC-7.1's field-semantics
baseline, per DALLAS-CLASS-1-rev refinement 2 -- reproduced here since the
Dallas County Profile itself lives in Notion, not this repo, and this
sandbox has no local copy to edit directly):

    "M31 (11,699 records, 1.66% of the 705,536-record real-property-only
    DCAD roll export examined for DALLAS-CLASS-1) is a real, documented
    field-semantics quirk: Category M (Mobile Homes) records appear in a
    file whose own name states 'real property only'. This is how DCAD
    composes this specific roll export, not a data error -- Harris may
    compose its own roll differently; check before assuming the same
    export-naming convention holds there."

See this test file's own final report delivery for whether this note has
been relayed to Diego for entry into the real Notion-hosted Dallas County
Profile page (out of this repo's reach directly, per MC-6/MC-7.1 -- the
Source Registry and County Profile are Notion artifacts).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from classification_map_dallas import (
    DALLAS_SPTB_TO_BENCHMARK,
    UNMAPPED_DALLAS,
    classify_dallas_sptb_code,
    classify_dallas_distribution,
)
from tax_logic.classify import BENCHMARK_LABELS

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
results = []


def check(name, condition, got=None, want=None):
    ok = bool(condition)
    mark = PASS if ok else FAIL
    msg = f"  {mark}  {name}"
    if not ok and got is not None:
        msg += f"\n        got:  {got!r}"
        msg += f"\n        want: {want!r}"
    print(msg)
    results.append((name, ok))
    return ok


# ── The real, full, corrected SPTB distribution (verbatim from the brief) ──
SPTB_DISTRIBUTION = {
    "A11": 508_295, "A13": 39_486, "F10": 37_412, "C11": 28_799, "C12": 24_533,
    "A12": 19_885,  "B12": 15_298, "M31": 11_699,  "B11": 4_542,  "O10": 3_591,
    "G10": 3_129,   "D10": 2_304,  "C13": 1_985,   "A20": 1_354,  "C14": 1_083,
    "J30": 1_022,   "F20": 811,    "J51": 154,     "E11": 78,     "G30": 69,
    "O11": 7,
}


def run_all():
    print("\n" + "=" * 70)
    print("DALLAS-CLASS-1-rev fixture tests: classification_map_dallas.py")
    print("=" * 70)

    # ── 1. Independently re-verify the distribution sums exactly to
    #      705,536 -- the same conservation identity Fable's review
    #      required, computed here rather than trusted from the brief. ────
    print("\n── Distribution sanity: sums to the real, known total ──")
    raw_total = sum(SPTB_DISTRIBUTION.values())
    check("SPTB_DISTRIBUTION sums to 705,536 exactly",
          raw_total == 705_536, got=raw_total, want=705_536)
    check("SPTB_DISTRIBUTION has all 21 real codes",
          len(SPTB_DISTRIBUTION) == 21, got=len(SPTB_DISTRIBUTION), want=21)

    # ── 2. Per-code classification: every mapped code lands on the right
    #      benchmark label; every unmapped code (G/J/O) lands on the
    #      UNMAPPED_DALLAS sentinel, never force-mapped. ──────────────────
    print("\n── Per-code classification (first-letter grain) ──")
    expected_label = {
        "A11": "Residential", "A13": "Residential", "A12": "Residential", "A20": "Residential",
        "B12": "Multi-Family", "B11": "Multi-Family",
        "C11": "Land/Vacant", "C12": "Land/Vacant", "C13": "Land/Vacant", "C14": "Land/Vacant",
        "F10": "Commercial", "F20": "Commercial",
        "D10": "Agricultural", "E11": "Agricultural",
        "M31": "Residential",
        "G10": UNMAPPED_DALLAS, "G30": UNMAPPED_DALLAS,
        "J30": UNMAPPED_DALLAS, "J51": UNMAPPED_DALLAS,
        "O10": UNMAPPED_DALLAS, "O11": UNMAPPED_DALLAS,
    }
    all_ok = True
    for code, want in expected_label.items():
        got = classify_dallas_sptb_code(code)
        all_ok = check(f"{code} classifies to {want!r}", got == want, got=got, want=want) and all_ok
    check("all 21 codes classify to their expected label", all_ok, got=None, want=None)

    # ── 3. THE CORRECTED CALIBRATION ITSELF (brief's own item 3): mapped
    #      total 697,564, unmapped total 7,972 (G=3,198 / J=1,176 /
    #      O=3,598), summing to 705,536 exactly. ──────────────────────────
    print("\n── The corrected calibration: mapped/unmapped totals ──")
    report = classify_dallas_distribution(SPTB_DISTRIBUTION)
    check("mapped_total == 697,564",
          report["mapped_total"] == 697_564, got=report["mapped_total"], want=697_564)
    check("unmapped_total == 7,972",
          report["unmapped_total"] == 7_972, got=report["unmapped_total"], want=7_972)
    check("mapped_total + unmapped_total == 705,536 (real, exact conservation)",
          report["mapped_total"] + report["unmapped_total"] == 705_536,
          got=report["mapped_total"] + report["unmapped_total"], want=705_536)
    check("report['conserved'] is True",
          report["conserved"] is True, got=report["conserved"], want=True)
    check("report['total'] == 705,536",
          report["total"] == 705_536, got=report["total"], want=705_536)

    # ── 4. Per-benchmark breakdown -- the corrected group sums from the
    #      brief, verified independently. ────────────────────────────────
    print("\n── Per-benchmark breakdown ──")
    by_b = report["by_benchmark"]
    check("Residential == 569,020 + 11,699 = 580,719 (A-group + M31)",
          by_b.get("Residential") == 580_719, got=by_b.get("Residential"), want=580_719)
    check("Multi-Family == 19,840", by_b.get("Multi-Family") == 19_840,
          got=by_b.get("Multi-Family"), want=19_840)
    check("Land/Vacant == 56,400", by_b.get("Land/Vacant") == 56_400,
          got=by_b.get("Land/Vacant"), want=56_400)
    check("Commercial == 38,223", by_b.get("Commercial") == 38_223,
          got=by_b.get("Commercial"), want=38_223)
    check("Agricultural == 2,382", by_b.get("Agricultural") == 2_382,
          got=by_b.get("Agricultural"), want=2_382)
    # Cross-check: A-group alone (excluding M31) should be 569,020 per the
    # brief's own "A -> Residential" line item, independent of the M31
    # residential contribution folded into the same benchmark label above.
    a_group_only = sum(v for k, v in SPTB_DISTRIBUTION.items() if k.startswith("A"))
    check("A-group alone (A11+A13+A12+A20) == 569,020",
          a_group_only == 569_020, got=a_group_only, want=569_020)

    # ── 5. Per-unmapped-letter breakdown -- the corrected G/J/O split,
    #      including the G30 the original brief's send had dropped. ──────
    print("\n── Per-unmapped-letter breakdown (the two corrections) ──")
    by_u = report["by_unmapped_letter"]
    check("G == 3,198 (G10=3,129 + G30=69 -- G30 was dropped in the original send)",
          by_u.get("G") == 3_198, got=by_u.get("G"), want=3_198)
    check("J == 1,176 (J30=1,022 + J51=154)",
          by_u.get("J") == 1_176, got=by_u.get("J"), want=1_176)
    check("O == 3,598 (O10=3,591 + O11=7 -- moved here from the original "
          "send's incorrect Residential mapping)",
          by_u.get("O") == 3_598, got=by_u.get("O"), want=3_598)
    check("no 4th unmapped letter leaked in (only G/J/O)",
          set(by_u.keys()) == {"G", "J", "O"}, got=set(by_u.keys()), want={"G", "J", "O"})

    # ── 6. Regression guard against the ORIGINAL brief's specific bug: O
    #      must NOT classify to Residential (the precedent-inconsistency
    #      Fable's review caught and this rev corrected). ────────────────
    print("\n── Regression guard: O is NOT mapped to Residential ──")
    check("classify_dallas_sptb_code('O10') != 'Residential'",
          classify_dallas_sptb_code("O10") != "Residential",
          got=classify_dallas_sptb_code("O10"), want="unmapped (not Residential)")
    check("'O' is absent from DALLAS_SPTB_TO_BENCHMARK entirely",
          "O" not in DALLAS_SPTB_TO_BENCHMARK, got="O" in DALLAS_SPTB_TO_BENCHMARK, want=False)

    # ── 7. Travis-parity regression guard: G, J, and O must ALL be absent
    #      from DALLAS_SPTB_TO_BENCHMARK, matching tax_logic/classify.py's
    #      real, live label_case_sql() precedent exactly. ────────────────
    print("\n── Travis-parity regression guard: G/J/O all absent from the map ──")
    for letter in ("G", "J", "O"):
        check(f"'{letter}' is absent from DALLAS_SPTB_TO_BENCHMARK (matches "
              f"Travis's own live precedent -- none of G/J/O force-mapped)",
              letter not in DALLAS_SPTB_TO_BENCHMARK,
              got=letter in DALLAS_SPTB_TO_BENCHMARK, want=False)

    # ── 8. Every value in DALLAS_SPTB_TO_BENCHMARK must be a real
    #      BENCHMARK_LABELS member -- self-consistency against the single
    #      canonical vocabulary imported from tax_logic/classify.py (MC-5
    #      rule 1: never redefine the vocabulary independently). ─────────
    print("\n── Self-consistency: every mapped value is a real benchmark label ──")
    all_valid = True
    for letter, label in DALLAS_SPTB_TO_BENCHMARK.items():
        all_valid = check(f"'{letter}' -> {label!r} is a real BENCHMARK_LABELS member",
                           label in BENCHMARK_LABELS, got=label, want=BENCHMARK_LABELS) and all_valid
    check("all DALLAS_SPTB_TO_BENCHMARK values are valid benchmark labels", all_valid)

    # ── 9. NULL/blank handling -- conservative default per MC-5 rule 3. ──
    print("\n── NULL/blank SPTB value handling ──")
    check("None classifies to UNMAPPED_DALLAS (conservative, not guessed)",
          classify_dallas_sptb_code(None) == UNMAPPED_DALLAS,
          got=classify_dallas_sptb_code(None), want=UNMAPPED_DALLAS)
    check("'' classifies to UNMAPPED_DALLAS",
          classify_dallas_sptb_code("") == UNMAPPED_DALLAS,
          got=classify_dallas_sptb_code(""), want=UNMAPPED_DALLAS)
    check("'  ' (whitespace only) classifies to UNMAPPED_DALLAS",
          classify_dallas_sptb_code("   ") == UNMAPPED_DALLAS,
          got=classify_dallas_sptb_code("   "), want=UNMAPPED_DALLAS)

    # ── 10. classify_dallas_distribution() never raises on an empty input
    #       (degenerate case) and correctly reports zero/conserved. ──────
    print("\n── classify_dallas_distribution(): empty input ──")
    empty_report = classify_dallas_distribution({})
    check("empty distribution: total == 0", empty_report["total"] == 0)
    check("empty distribution: conserved is True (0 == 0)",
          empty_report["conserved"] is True)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    status = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} — {status}")
    print("=" * 70)
    return passed == total


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
