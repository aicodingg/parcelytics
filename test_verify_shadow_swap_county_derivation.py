#!/usr/bin/env python3
"""
test_verify_shadow_swap_county_derivation.py — PX-20260831-02 Task 2 fixtures.

Proves verify_shadow_swap_county_derivation.py's two checks fire correctly
in both directions, per Diego's own standing rule for every scanner in this
repo ("an assertion that's never been proven to fire is a hope, not a
safeguard"):

  1. A module that does NOT match the DROP+RENAME shadow-swap shape at all
     (an ordinary upsert-based writer) is correctly classified
     is_shadow_swap=False and produces zero findings -- this tool must not
     fire on every writer in the codebase, only the specific architecture
     it targets.
  2. A COMPLIANT synthetic shadow-swap module (per-row county_code derived
     in the SELECT, carried through GROUP BY, no county_code write-path
     parameter) passes both checks.
  3. A synthetic module reproducing the EXACT retired PARTITION-2-FIX-1
     shape (build_shadow(conn, batch_id, county_code="TRAVIS") stamping
     that one external value onto every _shadow row, with the underlying
     aggregation query grouping by something other than county_code) fails
     BOTH check (a) and check (b) -- the real, historical bug shape, not a
     synthetic strawman.
  4. Real regression proof: running the tool against the actual, currently-
     fixed loaders/refresh_group_stats.py and loaders/
     refresh_snapshot_summary.py source files on disk passes cleanly.

Run: python3 test_verify_shadow_swap_county_derivation.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_shadow_swap_county_derivation as vs

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def _analyze(source, label):
    tree = ast.parse(source, filename=label)
    return vs.analyze_module(tree, label)


# ── Fixture 1: a non-shadow-swap writer must not be flagged at all ─────────
_PLAIN_UPSERT_MODULE = '''
def upsert_billing_rows(conn, records, county_code):
    sql = """
        INSERT INTO tax_billing (county_code, geo_id, tax_year, total_tax)
        VALUES (%(county_code)s, %(geo_id)s, %(tax_year)s, %(total_tax)s)
        ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE SET total_tax = EXCLUDED.total_tax
    """
    with conn.cursor() as cur:
        for r in records:
            cur.execute(sql, r)
    conn.commit()
'''


def test_plain_upsert_module_is_not_classified_as_shadow_swap():
    result = _analyze(_PLAIN_UPSERT_MODULE, "synthetic:plain_upsert.py")
    check("plain upsert module: is_shadow_swap is False (no DROP...+_shadow / RENAME shape present)",
          result["is_shadow_swap"] is False, result)
    check("plain upsert module: zero violations reported (nothing to check when not shadow-swap)",
          result["county_param_violations"] == [] and result["group_by_violations"] == [], result)


# ── Fixture 2: a COMPLIANT synthetic shadow-swap module passes both checks ─
_COMPLIANT_SHADOW_SWAP_MODULE = '''
def build_shadow(conn, batch_id, verbose=True):
    sql = """
        SELECT p.county_code AS county_code, p.neighborhood_cd AS neighborhood_cd,
               COUNT(*) AS n, AVG(p.market_value) AS avg_mv
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id AND pty.county_code = p.county_code
        GROUP BY p.county_code, p.neighborhood_cd
    """
    with conn.cursor() as cur:
        for tbl in ("widget_stats",):
            cur.execute(f"DROP TABLE IF EXISTS {tbl}_shadow")
            cur.execute(f"CREATE TABLE {tbl}_shadow (LIKE {tbl} INCLUDING ALL)")
        cur.execute(sql)
        rows = cur.fetchall()
        for r in rows:
            cur.execute(
                "INSERT INTO widget_stats_shadow (county_code, neighborhood_cd, n, avg_mv, batch_id) "
                "VALUES (%(county_code)s, %(neighborhood_cd)s, %(n)s, %(avg_mv)s, %(batch_id)s)",
                {"county_code": r["county_code"], "neighborhood_cd": r["neighborhood_cd"],
                 "n": r["n"], "avg_mv": r["avg_mv"], "batch_id": batch_id},
            )
    conn.commit()


def swap_shadow_in(conn, verbose=True):
    with conn.cursor() as cur:
        for tbl in ("widget_stats",):
            cur.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_old")
            cur.execute(f"ALTER TABLE {tbl}_shadow RENAME TO {tbl}")
            cur.execute(f"DROP TABLE {tbl}_old")
    conn.commit()


def assert_widget_stats_fresh(conn, county_code="TRAVIS"):
    """Legitimate kept per-county staleness read -- has a county_code
    parameter, but never touches a _shadow table, so it must NOT trip
    check (a)."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(batch_id) FROM widget_stats WHERE county_code = %(county_code)s",
                     {"county_code": county_code})
        return cur.fetchone()
'''


def test_compliant_synthetic_module_passes_both_checks():
    result = _analyze(_COMPLIANT_SHADOW_SWAP_MODULE, "synthetic:compliant_shadow_swap.py")
    check("compliant module: correctly classified as shadow-swap",
          result["is_shadow_swap"] is True, result)
    check("compliant module: check (a) passes -- no build function with a county_code param writes to _shadow",
          result["county_param_violations"] == [], result["county_param_violations"])
    check("compliant module: check (b) passes -- the one GROUP BY clause carries county_code",
          result["group_by_violations"] == [], result["group_by_violations"])


# ── Fixture 3: the REAL retired PARTITION-2-FIX-1 shape must fail BOTH ─────
_EXTERNALLY_STAMPED_SHADOW_SWAP_MODULE = '''
def build_shadow(conn, batch_id, county_code="TRAVIS", verbose=True):
    """The exact retired shape: county_code is an external write-path
    parameter, stamped onto every row regardless of which county each row's
    own parcels actually belong to; the aggregation query below has no
    county_code involvement anywhere -- it blends every county together."""
    sql = """
        SELECT p.neighborhood_cd AS neighborhood_cd,
               COUNT(*) AS n, AVG(p.market_value) AS avg_mv
        FROM parcel p
        JOIN parcel_tax_year pty ON pty.geo_id = p.geo_id
        GROUP BY p.neighborhood_cd
    """
    with conn.cursor() as cur:
        for tbl in ("widget_stats",):
            cur.execute(f"DROP TABLE IF EXISTS {tbl}_shadow")
            cur.execute(f"CREATE TABLE {tbl}_shadow (LIKE {tbl} INCLUDING ALL)")
        cur.execute(sql)
        rows = cur.fetchall()
        for r in rows:
            cur.execute(
                "INSERT INTO widget_stats_shadow (county_code, neighborhood_cd, n, avg_mv, batch_id) "
                "VALUES (%(county_code)s, %(neighborhood_cd)s, %(n)s, %(avg_mv)s, %(batch_id)s)",
                {"county_code": county_code, "neighborhood_cd": r["neighborhood_cd"],
                 "n": r["n"], "avg_mv": r["avg_mv"], "batch_id": batch_id},
            )
    conn.commit()


def swap_shadow_in(conn, verbose=True):
    with conn.cursor() as cur:
        for tbl in ("widget_stats",):
            cur.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_old")
            cur.execute(f"ALTER TABLE {tbl}_shadow RENAME TO {tbl}")
            cur.execute(f"DROP TABLE {tbl}_old")
    conn.commit()
'''


def test_externally_stamped_synthetic_module_fails_both_checks():
    result = _analyze(_EXTERNALLY_STAMPED_SHADOW_SWAP_MODULE, "synthetic:externally_stamped_shadow_swap.py")
    check("externally-stamped module: correctly classified as shadow-swap",
          result["is_shadow_swap"] is True, result)

    check("externally-stamped module: check (a) FAILS -- build_shadow() has a county_code param "
          "AND writes to widget_stats_shadow",
          len(result["county_param_violations"]) == 1 and
          result["county_param_violations"][0]["function"] == "build_shadow",
          result["county_param_violations"])

    check("externally-stamped module: check (b) FAILS -- the GROUP BY clause does not carry county_code",
          len(result["group_by_violations"]) == 1, result["group_by_violations"])
    if result["group_by_violations"]:
        check("externally-stamped module: the reported clause is the real offending GROUP BY p.neighborhood_cd",
              "neighborhood_cd" in result["group_by_violations"][0]["clause"],
              result["group_by_violations"][0])


# ── Fixture 4: real regression proof against the actual, already-fixed
# production files on disk -- both must pass cleanly today. ────────────────

def test_real_refresh_group_stats_passes():
    path = os.path.join(vs.REPO_ROOT, "loaders", "refresh_group_stats.py")
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    result = vs.analyze_module(tree, "loaders/refresh_group_stats.py")
    check("real loaders/refresh_group_stats.py: classified as shadow-swap",
          result["is_shadow_swap"] is True, result["is_shadow_swap"])
    check("real loaders/refresh_group_stats.py: zero check-(a) violations (already fixed, PX-20260828-13)",
          result["county_param_violations"] == [], result["county_param_violations"])
    check("real loaders/refresh_group_stats.py: zero check-(b) violations",
          result["group_by_violations"] == [], result["group_by_violations"])


def test_real_refresh_snapshot_summary_passes():
    path = os.path.join(vs.REPO_ROOT, "loaders", "refresh_snapshot_summary.py")
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    result = vs.analyze_module(tree, "loaders/refresh_snapshot_summary.py")
    check("real loaders/refresh_snapshot_summary.py: classified as shadow-swap",
          result["is_shadow_swap"] is True, result["is_shadow_swap"])
    check("real loaders/refresh_snapshot_summary.py: zero check-(a) violations (fixed, PX-20260831-02 Task 1)",
          result["county_param_violations"] == [], result["county_param_violations"])
    check("real loaders/refresh_snapshot_summary.py: zero check-(b) violations",
          result["group_by_violations"] == [], result["group_by_violations"])


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL VERIFY_SHADOW_SWAP_COUNTY_DERIVATION FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
