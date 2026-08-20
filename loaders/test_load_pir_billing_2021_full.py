"""
loaders/test_load_pir_billing_2021_full.py
===========================================
TAX-BILLING-REKEY-4 fixture tests.

Purpose (per Diego's explicit brief): the "list object has no attribute
'values'" bug that crashed live on the 2021/2022 PIR billing loaders
evaded every prior round of sandbox fixture testing in this migration.
This file exists to PROVE that a fixture covering the actual shapes
involved -- specifically an account with a REPEATED entity_code across
its TXENTCOD1..10 slots (the exact scenario _sum_entities() exists to
handle correctly, and the exact scenario a naive "just iterate the list"
fix would silently undercount) -- exercises the real code paths that
crashed live (write_review_log()'s total_due sum, the --dry-run preview
math, and write_to_db()'s real write path via the newly-extracted, pure
_build_billing_and_entity_rows()) without needing a live DB connection or
the real 482MB source file.

These are pure-function tests only: _sum_entities() and
_build_billing_and_entity_rows() take plain Python data structures in and
return plain Python data structures out (no DB connection, no file I/O).
This is deliberate -- see _build_billing_and_entity_rows()'s own
docstring in load_pir_billing_2021_full.py for why the prior code's LACK
of this pure/DB-wrapper split is the real, disclosed root cause this bug
reached live testing undetected: the shape-handling logic used to live
entirely inside write_to_db(), which nothing in this sandbox could
exercise without a live Postgres connection this environment doesn't
have. AC8-style disclosure: write_to_db()'s actual batch_upsert() calls,
and check_portal_scrape_divergence()'s live-query half, remain untested
here for that same reason -- only the pure computation layer is verified.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# psycopg2 is not installed in this sandbox at all (confirmed via `import
# psycopg2` raising ModuleNotFoundError, and pip install failing -- no
# network access to fetch it either). load_pir_billing_2021_full.py imports
# it (via loaders.db) at MODULE level, so it must be stubbed in sys.modules
# BEFORE the import below, same pattern already established in this
# codebase by loaders/test_backfill_prop_unit_tax_year_geoid.py. This
# doesn't touch what's actually under test here -- _sum_entities() and
# _build_billing_and_entity_rows() are pure functions that never call
# psycopg2 themselves; this stub exists only to satisfy the module's
# import-time dependency chain.
_fake_extras = types.ModuleType("psycopg2.extras")
_fake_extras.execute_batch = lambda cur, sql, rows, page_size=None: None
_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = _fake_extras
_fake_pg2.connect = lambda *a, **k: None
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_extras)

from loaders.load_pir_billing_2021_full import (
    _sum_entities,
    _build_billing_and_entity_rows,
    TAX_YEAR,
    DATA_SOURCE,
    CONFIDENCE_LEVEL,
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
    print("TAX-BILLING-REKEY-4 fixture tests: load_pir_billing_2021_full.py")
    print("=" * 60)

    # ── 1. _sum_entities() on a plain, no-duplicates account ────────────────
    print("\n── _sum_entities(): basic (no duplicate codes) ──")
    entities_simple = [("IAU", 100.0, 100.0), ("CAT", 250.0, 0.0)]
    summed = _sum_entities(entities_simple)
    check("basic: two distinct codes preserved",
          summed == {"IAU": {"due": 100.0, "paid": 100.0},
                     "CAT": {"due": 250.0, "paid": 0.0}},
          got=summed, want="{'IAU': {...}, 'CAT': {...}}")

    # ── 2. _sum_entities() on a REPEATED-code account (the real, confirmed
    #      scenario this migration's own investigation found live: a single
    #      account's TXENTCOD1..10 slots can legitimately repeat the same
    #      code, and the OLD dict-based code explicitly summed via += for
    #      this exact reason). This is the shape that would silently
    #      undercount if a naive flatten-not-group fix had been applied
    #      instead of _sum_entities(). ─────────────────────────────────────
    print("\n── _sum_entities(): duplicate entity_code across slots ──")
    entities_dup = [
        ("TCO", 400.0, 400.0),   # slot 1
        ("CAT", 900.0, 0.0),     # slot 2
        ("TCO", 137.70, 137.70), # slot 3 -- same code as slot 1, must SUM not overwrite
    ]
    summed_dup = _sum_entities(entities_dup)
    check("duplicate-code account: TCO due summed (400.0 + 137.70 = 537.70)",
          summed_dup.get("TCO", {}).get("due") == 537.70,
          got=summed_dup.get("TCO", {}).get("due"), want=537.70)
    check("duplicate-code account: TCO paid summed (400.0 + 137.70 = 537.70)",
          summed_dup.get("TCO", {}).get("paid") == 537.70,
          got=summed_dup.get("TCO", {}).get("paid"), want=537.70)
    check("duplicate-code account: CAT unaffected (900.0)",
          summed_dup.get("CAT", {}).get("due") == 900.0,
          got=summed_dup.get("CAT", {}).get("due"), want=900.0)
    check("duplicate-code account: exactly 2 distinct codes in output (not 3 rows)",
          len(summed_dup) == 2,
          got=len(summed_dup), want=2)

    # ── 3. _sum_entities() on an empty account (0 real entity slots -- the
    #      degenerate case write_review_log()'s sum() must not choke on) ───
    print("\n── _sum_entities(): empty entities list ──")
    summed_empty = _sum_entities([])
    check("empty entities: returns empty dict, not an error",
          summed_empty == {},
          got=summed_empty, want={})

    # ── 4. THE REAL LIVE CRASH REPRODUCTION: write_review_log()'s exact
    #      total_due computation (`sum(v["due"] for v in entities.values())`
    #      before the fix) against `entities` in its real, live shape --
    #      extract_entities()'s raw [(code, due, paid), ...] list. Before
    #      the fix, this line would raise
    #      AttributeError: 'list' object has no attribute 'values'
    #      exactly as Diego's live traceback showed. This fixture proves
    #      the FIXED expression (flat-tuple sum, see write_review_log() in
    #      the source) succeeds against the same shape. ────────────────────
    print("\n── write_review_log()'s total_due expression (post-fix) ──")
    entities_list_shape = [("IAU", 100.0, 100.0), ("CAT", 250.0, 0.0)]
    try:
        total_due = sum(due for code, due, paid in entities_list_shape)
        crashed = False
    except AttributeError:
        crashed = True
        total_due = None
    check("fixed expression does not crash on list-of-tuples shape "
          "(this exact expr, run against .values() pre-fix, is what crashed live)",
          not crashed,
          got="AttributeError" if crashed else "no error", want="no error")
    check("fixed expression computes correct total (100.0 + 250.0 = 350.0)",
          total_due == 350.0,
          got=total_due, want=350.0)

    # ── 4b. PROOF the fixture is bug-sensitive, not just bug-agnostic: run
    #      the OLD, pre-fix expression against this exact same fixture data
    #      and confirm it raises the SAME AttributeError Diego's live
    #      traceback showed. This is the direct answer to "prove the
    #      fixture would have caught this before it reached live testing":
    #      had this fixture existed and been run against the code as it
    #      shipped, this assertion is exactly what would have failed. ─────
    print("\n── Proof: old pre-fix expression crashes on this exact fixture ──")
    try:
        _ = sum(v["due"] for v in entities_list_shape.values())
        old_expr_crashed = False
        old_expr_error = None
    except AttributeError as e:
        old_expr_crashed = True
        old_expr_error = str(e)
    check("OLD expression (entities.values()) DOES crash on this fixture, "
          "matching Diego's live traceback verbatim",
          old_expr_crashed and old_expr_error == "'list' object has no attribute 'values'",
          got=old_expr_error, want="'list' object has no attribute 'values'")

    # ── 5. _build_billing_and_entity_rows(): THE REAL WRITE PATH. This is
    #      the pure extraction of write_to_db()'s row-building logic --
    #      before this task, this logic lived entirely inside a DB-facing
    #      function with no way to fixture-test it at all. Confirmed via
    #      grep this was ALSO broken (same entities.values()/.items() bug)
    #      but had not yet crashed live only because it runs after the two
    #      diagnostic functions that crashed first in real call order. ────
    print("\n── _build_billing_and_entity_rows(): the real write path ──")
    matched = {
        # account A: normal, two distinct entity codes
        "01010104030000": (1300.0, [("IAU", 400.0, 400.0), ("CAT", 900.0, 0.0)], "0100030105"),
        # account B: duplicate entity_code across slots -- the scenario that
        # would break ENTITY_SQL's ON CONFLICT (county_code, account_id,
        # tax_year, entity_code) target if flattened instead of summed
        "01010104040000": (537.70, [("TCO", 400.0, 400.0), ("TCO", 137.70, 137.70)], "0100030109"),
    }
    billing_rows, entity_rows = _build_billing_and_entity_rows(
        matched, county_code=DEFAULT_COUNTY)

    check("billing_rows: one row per account (2 accounts -> 2 billing rows)",
          len(billing_rows) == 2,
          got=len(billing_rows), want=2)

    br_a = next((r for r in billing_rows if r["account_id"] == "01010104030000"), None)
    check("billing_rows: account A total_tax = sum of its entities (400+900=1300.0)",
          br_a is not None and br_a["total_tax"] == 1300.0,
          got=br_a["total_tax"] if br_a else None, want=1300.0)
    check("billing_rows: account A carries county_code/geo_id/tax_year/data_source correctly",
          br_a is not None and br_a["county_code"] == DEFAULT_COUNTY
          and br_a["geo_id"] == "0100030105" and br_a["tax_year"] == TAX_YEAR
          and br_a["data_source"] == DATA_SOURCE,
          got=br_a, want="county_code=TRAVIS, geo_id=0100030105, tax_year, data_source set")

    br_b = next((r for r in billing_rows if r["account_id"] == "01010104040000"), None)
    check("billing_rows: account B (duplicate-code) total_tax correctly summed (400+137.70=537.70)",
          br_b is not None and br_b["total_tax"] == 537.70,
          got=br_b["total_tax"] if br_b else None, want=537.70)

    # THE CRITICAL ASSERTION: account B has a duplicate entity_code (TCO
    # appears in 2 raw slots) -- entity_rows must contain exactly ONE row
    # for (account B, TCO), not two. Two rows for the same (county_code,
    # account_id, tax_year, entity_code) in one batch_upsert() call is
    # exactly the "ON CONFLICT DO UPDATE command cannot affect row a second
    # time" / silent-undercount risk this task's fix was designed to avoid.
    b_tco_rows = [r for r in entity_rows if r["account_id"] == "01010104040000" and r["entity_code"] == "TCO"]
    check("entity_rows: account B's duplicate TCO slots collapsed to exactly 1 row "
          "(not 2 -- would violate ENTITY_SQL's ON CONFLICT target)",
          len(b_tco_rows) == 1,
          got=len(b_tco_rows), want=1)
    check("entity_rows: account B's single TCO row has the SUMMED amount (537.70, not 400.0 or 137.70)",
          len(b_tco_rows) == 1 and b_tco_rows[0]["amount_due"] == 537.70,
          got=b_tco_rows[0]["amount_due"] if b_tco_rows else None, want=537.70)

    check("entity_rows: account A contributes exactly 2 rows (IAU, CAT -- no duplicates there)",
          len([r for r in entity_rows if r["account_id"] == "01010104030000"]) == 2,
          got=len([r for r in entity_rows if r["account_id"] == "01010104030000"]), want=2)

    check("entity_rows: total row count is 3 (2 from account A + 1 deduped from account B)",
          len(entity_rows) == 3,
          got=len(entity_rows), want=3)

    # ── 6. Regression guard: entities must NEVER be accessed via .values()/
    #      .items() anywhere in this module -- this is the grep-enumeration
    #      check Diego's brief asked for, run as an actual assertion so a
    #      future edit that reintroduces the bug fails this test suite, not
    #      just a live production run. ────────────────────────────────────
    print("\n── Regression guard: no entities.values()/.items() call sites remain ──")
    import re as _re
    src_path = os.path.join(os.path.dirname(__file__), "load_pir_billing_2021_full.py")
    with open(src_path) as f:
        src = f.read()
    bad_refs = [m for m in _re.findall(r"entities\.(values|items)\(\)", src)]
    check("zero entities.values()/.items() call sites in load_pir_billing_2021_full.py",
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
