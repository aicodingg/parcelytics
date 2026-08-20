"""
loaders/test_pir_xlsx_common.py
================================
TAX-BILLING-REKEY-4 fixture tests for the shared 2022/2023/2024 PIR billing
module (pir_xlsx_common.py).

Purpose (per Diego's explicit brief): the live crash Diego reproduced twice
against the real 491MB 2022 source file was in THIS module --
check_portal_scrape_divergence() at line ~584 (now the pure
_compute_new_totals_by_geo() this file tests directly), one of two real,
live-confirmed AttributeError: 'list' object has no attribute 'values'
crash sites (the other being load_pir_billing_2021_full.py's
write_review_log(), covered in test_load_pir_billing_2021_full.py). Per
Diego's brief, this file specifically targets:
  1. the duplicate-resolution path (_sum_entities() / the entity-shape
     handling inside _build_billing_and_entity_rows()) -- shared logic,
     same fixture strategy as the 2021 file's test.
  2. the portal-scrape-divergence path (_compute_new_totals_by_geo()) --
     THIS is the actual function that crashed live for 2022, and had no
     fixture-testable form at all before this task's pure-function
     extraction (it used to be inline inside check_portal_scrape_
     divergence(), a DB-facing function this sandbox has no way to
     exercise without a live connection).

AC8-style disclosure: check_portal_scrape_divergence()'s live-DB-read half
(the SELECT of prior tax_billing rows, and the conn parameter) and
write_to_db()'s actual batch_upsert() calls are NOT exercised here -- this
sandbox has no live Postgres connection and no access to the real 491MB
2022 source file. Only the pure, DB-free computation layer
(_sum_entities, _build_billing_and_entity_rows, _compute_new_totals_by_geo)
is verified, which is the layer this task's fix specifically introduced to
make this class of bug catchable pre-live in the first place.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# psycopg2 is not installed in this sandbox at all (confirmed via `import
# psycopg2` raising ModuleNotFoundError, and pip install failing -- no
# network access). pir_xlsx_common.py doesn't import psycopg2 directly, but
# its `import tax_billing_rollup` does (transitively, via loaders.db), so
# the stub must be in place before that import chain runs -- same pattern
# already established by loaders/test_backfill_prop_unit_tax_year_geoid.py.
# This doesn't touch what's actually under test here -- _sum_entities(),
# _build_billing_and_entity_rows(), and _compute_new_totals_by_geo() are
# pure functions that never call psycopg2 themselves.
_fake_extras = types.ModuleType("psycopg2.extras")
_fake_extras.execute_batch = lambda cur, sql, rows, page_size=None: None
_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = _fake_extras
_fake_pg2.connect = lambda *a, **k: None
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_extras)

from loaders.pir_xlsx_common import (
    _sum_entities,
    _build_billing_and_entity_rows,
    _compute_new_totals_by_geo,
)
from loaders.scrape_billing_history import DEFAULT_COUNTY

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


def run_all():
    print("\n" + "=" * 60)
    print("TAX-BILLING-REKEY-4 fixture tests: pir_xlsx_common.py")
    print("=" * 60)

    # ── 1. _sum_entities(): same duplicate-code scenario as the 2021 file's
    #      test, confirming the duplicated helper behaves identically here
    #      (both copies must independently handle the real repeated-slot
    #      case the investigation found). ─────────────────────────────────
    print("\n── _sum_entities(): duplicate entity_code across slots ──")
    entities_dup = [
        ("ACT", 250.0, 250.0),
        ("THD", 800.0, 0.0),
        ("ACT", 42.30, 42.30),  # same code as slot 1 -- must sum, not overwrite
    ]
    summed_dup = _sum_entities(entities_dup)
    check("duplicate-code account: ACT due summed (250.0 + 42.30 = 292.30)",
          summed_dup.get("ACT", {}).get("due") == 292.30,
          got=summed_dup.get("ACT", {}).get("due"), want=292.30)
    check("duplicate-code account: exactly 2 distinct codes (not 3 rows)",
          len(summed_dup) == 2,
          got=len(summed_dup), want=2)

    # ── 2. _build_billing_and_entity_rows(): the real write path, shared
    #      logic with the 2021 file but this module's version takes
    #      tax_year/data_source/confidence_level as explicit params (no
    #      module-level TAX_YEAR constant here, since this one module backs
    #      2022/2023/2024 -- confirmed by reading run_cli()'s call site). ──
    print("\n── _build_billing_and_entity_rows(): the real write path ──")
    matched = {
        "01010104050000": (1150.0, [("IAU", 350.0, 350.0), ("CAT", 800.0, 0.0)], "0100030105"),
        "01010104060000": (292.30, [("ACT", 250.0, 250.0), ("ACT", 42.30, 42.30)], "0100030109"),
    }
    billing_rows, entity_rows = _build_billing_and_entity_rows(
        matched, 2022, "pir_billing_2022_full", "verified", county_code=DEFAULT_COUNTY)

    check("billing_rows: one row per account (2 accounts -> 2 billing rows)",
          len(billing_rows) == 2,
          got=len(billing_rows), want=2)

    br_dup = next((r for r in billing_rows if r["account_id"] == "01010104060000"), None)
    check("billing_rows: duplicate-code account total_tax correctly summed (250+42.30=292.30)",
          br_dup is not None and br_dup["total_tax"] == 292.30,
          got=br_dup["total_tax"] if br_dup else None, want=292.30)
    check("billing_rows: tax_year/data_source/confidence_level threaded through correctly",
          br_dup is not None and br_dup["tax_year"] == 2022
          and br_dup["data_source"] == "pir_billing_2022_full"
          and br_dup["confidence_level"] == "verified",
          got=br_dup, want="tax_year=2022, data_source=pir_billing_2022_full, confidence_level=verified")

    dup_entity_rows = [r for r in entity_rows if r["account_id"] == "01010104060000"]
    check("entity_rows: duplicate ACT slots collapsed to exactly 1 row, not 2",
          len(dup_entity_rows) == 1,
          got=len(dup_entity_rows), want=1)
    check("entity_rows: collapsed row carries the summed amount (292.30)",
          len(dup_entity_rows) == 1 and dup_entity_rows[0]["amount_due"] == 292.30,
          got=dup_entity_rows[0]["amount_due"] if dup_entity_rows else None, want=292.30)

    # ── 3. _compute_new_totals_by_geo(): THE ACTUAL FUNCTION THAT CRASHED
    #      LIVE for the 2022 dry-run (traceback: pir_xlsx_common.py line
    #      584, inside check_portal_scrape_divergence(), AttributeError:
    #      'list' object has no attribute 'values'). This is the most
    #      important fixture in this file -- before this task's extraction,
    #      this computation had NO standalone, fixture-testable form at
    #      all; it lived inline inside a function requiring a live conn
    #      parameter. Scenario: two accounts sharing one geo_id (the real,
    #      documented case this function exists for -- summing every
    #      matched account sharing a geo_id, a live preview of what
    #      tax_billing_rollup.py computes in Postgres), PLUS a second,
    #      unrelated geo_id, PLUS one account with a duplicate entity_code
    #      slot (proving this path is duplicate-safe too, since it sums
    #      raw due amounts directly rather than going through
    #      _sum_entities() -- confirmed correct because total tax due does
    #      not change whether duplicate slots are pre-grouped or not, only
    #      per-entity_code amounts do). ─────────────────────────────────────
    print("\n── _compute_new_totals_by_geo(): the real live-crashed function ──")
    matched_multi_geo = {
        # geo_id 0100030105: two accounts share this geo_id (sub-accounts)
        "acct-A1": (400.0, [("IAU", 400.0, 400.0)], "0100030105"),
        "acct-A2": (900.0, [("CAT", 900.0, 0.0)], "0100030105"),
        # geo_id 0100030109: one account, with a duplicate entity_code slot
        "acct-B1": (292.30, [("ACT", 250.0, 250.0), ("ACT", 42.30, 42.30)], "0100030109"),
    }
    try:
        totals = _compute_new_totals_by_geo(matched_multi_geo)
        crashed = False
    except AttributeError:
        crashed = True
        totals = None
    check("does not crash on list-of-tuples entities shape "
          "(this exact call site, pre-fix, is what crashed live on the real 2022 file)",
          not crashed,
          got="AttributeError" if crashed else "no error", want="no error")
    check("geo_id 0100030105: two sub-accounts correctly summed (400.0 + 900.0 = 1300.0)",
          totals is not None and totals.get("0100030105") == 1300.0,
          got=totals.get("0100030105") if totals else None, want=1300.0)
    check("geo_id 0100030109: duplicate-slot account correctly summed (250.0 + 42.30 = 292.30)",
          totals is not None and totals.get("0100030109") == 292.30,
          got=totals.get("0100030109") if totals else None, want=292.30)
    check("exactly 2 distinct geo_ids in output",
          totals is not None and len(totals) == 2,
          got=len(totals) if totals else None, want=2)

    # ── 3b. PROOF the fixture is bug-sensitive, not just bug-agnostic: run
    #      the OLD, pre-fix expression (the exact code that lived inline in
    #      check_portal_scrape_divergence() before this task's extraction)
    #      against this exact same fixture data, and confirm it raises the
    #      SAME AttributeError Diego's live 2022 dry-run traceback showed.
    #      This is the direct, concrete answer to "prove the fixture would
    #      have caught this before it reached live testing." ─────────────
    print("\n── Proof: old pre-fix expression crashes on this exact fixture ──")
    try:
        old_new_totals_by_geo = {}
        for account_id, (total, ents, geo_id) in matched_multi_geo.items():
            old_new_totals_by_geo.setdefault(geo_id, 0.0)
            old_new_totals_by_geo[geo_id] += sum(v["due"] for v in ents.values())
        old_expr_crashed = False
        old_expr_error = None
    except AttributeError as e:
        old_expr_crashed = True
        old_expr_error = str(e)
    check("OLD check_portal_scrape_divergence() expression (entities.values()) "
          "DOES crash on this fixture, matching Diego's live 2022 traceback verbatim",
          old_expr_crashed and old_expr_error == "'list' object has no attribute 'values'",
          got=old_expr_error, want="'list' object has no attribute 'values'")

    # ── 4. _compute_new_totals_by_geo(): empty matched dict (degenerate
    #      case -- e.g. a --dry-run against a file with zero rows matched
    #      to real geo_ids -- must not raise). ────────────────────────────
    print("\n── _compute_new_totals_by_geo(): empty matched dict ──")
    empty_totals = _compute_new_totals_by_geo({})
    check("empty matched dict returns empty totals dict, not an error",
          empty_totals == {},
          got=empty_totals, want={})

    # ── 5. Regression guard: entities must NEVER be accessed via .values()/
    #      .items() anywhere in this module -- run as an actual assertion,
    #      same discipline as the 2021 file's test. ─────────────────────
    print("\n── Regression guard: no entities.values()/.items() call sites remain ──")
    import re as _re
    src_path = os.path.join(os.path.dirname(__file__), "pir_xlsx_common.py")
    with open(src_path) as f:
        src = f.read()
    bad_refs = [m for m in _re.findall(r"entities\.(values|items)\(\)", src)]
    check("zero entities.values()/.items() call sites in pir_xlsx_common.py",
          len(bad_refs) == 0,
          got=bad_refs, want=[])

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    status = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} — {status}")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
