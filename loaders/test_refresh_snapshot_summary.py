#!/usr/bin/env python3
"""
loaders/test_refresh_snapshot_summary.py — Task AGGPRECOMP-2, Verification.
Fixture tests for loaders/refresh_snapshot_summary.py.

Same disclosure pattern as loaders/test_refresh_group_stats.py (see that
file's own module docstring for the full sandbox-limitation evidence: no
root to apt-get install postgresql, pip install pgserver blocked by the
sandbox's PyPI proxy). No live Postgres is reachable here, and
psycopg2 itself is not installed in this sandbox either (confirmed via
`python3 -c "import psycopg2"` -> ModuleNotFoundError before writing this
file) — _compute_one_view() in the module under test imports
psycopg2.extras directly, so it cannot be exercised at all here, live or
mocked at the cursor level.

Given that, this file proves three DIFFERENT things, neither of which
requires psycopg2 or a real Postgres, explicit about what each does and
does not prove:

  1. THE FIVE SQL BUILDERS ARE CORRECT IN SHAPE for all 11 real /snapshot
     views: each of breakdown_sql/single_year_mv_sql/part4_agg_sql/
     cert_agg_sql/neighborhoods_sql is checked, per view, for the
     structural invariants that matter (CANONICAL_EXCL present, the
     HAVING COUNT(*) >= 10 neighborhood-size floor, GROUPING SETS shape).
     This does NOT prove the SQL strings execute correctly against real
     Postgres — see Diego's live-verification commands in this task's
     final report.

  2. THE PYTHON-SIDE MERGE LOGIC (merge_breakdown_rows) IS CORRECT: an
     independent, hand-computed synthetic fixture proves the INNER JOIN
     suppression override (a parcel present in only one of 2025/2026 must
     not be silently dropped from either year's dollar total) fires
     correctly, both per-ptype and on the grand-total row, and that a
     ptype missing from the single-year query results falls back to the
     paired-JOIN value rather than raising or going None. merge_breakdown_
     rows() is a direct line-for-line port of app.py's own inline merge
     (see app.py's _compute_snapshot_data(), the "rows"/"totals" assembly
     around the INNER JOIN suppression fix comment) — these fixtures prove
     the PORT is correct against the same hand-worked arithmetic, not
     against a live database.

  3. THE ORCHESTRATION FUNCTIONS ISSUE THE RIGHT DB CALLS IN THE RIGHT
     ORDER: build_shadow() / swap_shadow_in() / refresh_snapshot_summary()
     / assert_snapshot_summary_fresh() are exercised against an in-memory
     FakeConn/FakeCursor (same style as test_refresh_group_stats.py's own
     fake DB layer), with _compute_one_view() MONKEYPATCHED to a canned
     Python function returning synthetic per-view results — sidestepping
     the psycopg2 dependency entirely, since build_shadow()/
     refresh_snapshot_summary() only call _compute_one_view() by name and
     never touch cursor_factory themselves. This proves the DROP/CREATE/
     per-view-INSERT/RENAME sequence, the batch-id mint-vs-reuse branching,
     dry-run's "no writes" contract, and the 3-table staleness assertion's
     fresh/stale/empty/multi-batch-within-table/disagreement-across-tables
     logic — all without needing psycopg2 or a real database. It does NOT
     prove _compute_one_view()'s own internals (the SET LOCAL enable_
     nestloop=off + RealDictCursor fetch logic) run correctly against real
     data — that needs psycopg2 installed and a live connection, both
     unavailable here.

Neither (1), (2), nor (3), even together, proves the actual SQL strings
parse and execute correctly against a real Postgres server, that the
3-table shadow-swap is genuinely atomic under real concurrent production
traffic, or that a full 11-view refresh completes within the spec's
"minutes of pipeline time" cost estimate on real production-scale data.
Diego needs to verify all of this live — see this task's final report for
exact commands.

── PX-20260831-02 Task 1 (2026-08-31) ──────────────────────────────────────
Every fixture in this file has been rewritten for the single-county
build-time seam's retirement: build_shadow()/refresh_snapshot_summary() no
longer take a county_code parameter (mirrors refresh_group_stats.py's own
signature exactly); the five SQL builders now derive county_code from
parcel.county_code per row; merge_breakdown_rows() returns a LIST of
per-county totals dicts, not a single dict-or-None; _compute_one_view()
returns (rows, totals_rows, neighborhoods_by_county), not the old 7-tuple.
New Part 8 fixtures specifically prove the multi-county behavior that is
the actual point of this fix: two counties' rows in the same view are kept
genuinely separate through the merge and the INSERT calls, and the
post-refresh consistency check is now wired into refresh_snapshot_summary()
itself (not just available behind --check-consistency).

Run: python3 loaders/test_refresh_snapshot_summary.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders import refresh_snapshot_summary as rss
import snapshot_taxonomy as _st
from snapshot_taxonomy import _SNAPSHOT_VALID_VIEWS

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Part 1: SQL-builder shape assertions, all 11 real views ─────────────────

def test_sql_builders_produce_valid_shapes_for_every_real_view():
    check("_SNAPSHOT_VALID_VIEWS has all 11 real /snapshot views",
          len(_SNAPSHOT_VALID_VIEWS) == 11, sorted(_SNAPSHOT_VALID_VIEWS))

    canonical_leg = rss.CANONICAL_EXCL.split(" AND ")[0]
    for view in sorted(_SNAPSHOT_VALID_VIEWS):
        b = rss.breakdown_sql(view)
        mv25 = rss.single_year_mv_sql(view, 2025)
        mv26 = rss.single_year_mv_sql(view, 2026)
        p4 = rss.part4_agg_sql(view)
        ca = rss.cert_agg_sql(view)
        nb = rss.neighborhoods_sql(view)

        check(f"breakdown_sql({view}): GROUPING SETS present", "GROUPING SETS" in b)
        check(f"breakdown_sql({view}): canonical_excl applied", canonical_leg in b)
        check(f"single_year_mv_sql({view}, 2025): tax_year=2025 pinned", "tax_year = 2025" in mv25)
        check(f"single_year_mv_sql({view}, 2026): tax_year=2026 pinned", "tax_year = 2026" in mv26)
        check(f"single_year_mv_sql({view}): canonical_excl applied (both years)",
              canonical_leg in mv25 and canonical_leg in mv26)
        check(f"part4_agg_sql({view}): year_built >= 2025 cutoff (matches app.py's Aug-2026 sync)",
              "p.year_built >= 2025" in p4)
        check(f"part4_agg_sql({view}): canonical_excl applied", canonical_leg in p4)
        check(f"cert_agg_sql({view}): data_source = 'preliminary' filter present",
              "data_source = 'preliminary'" in ca)
        check(f"cert_agg_sql({view}): canonical_excl applied", canonical_leg in ca)
        check(f"neighborhoods_sql({view}): HAVING COUNT(*) >= 10 floor present",
              "HAVING COUNT(*) >= 10" in nb)
        check(f"neighborhoods_sql({view}): canonical_excl applied", canonical_leg in nb)
        check(f"neighborhoods_sql({view}): NULL/blank neighborhood_cd excluded",
              "neighborhood_cd IS NOT NULL" in nb and "neighborhood_cd != ''" in nb)

        # PX-20260831-02 Task 1: every builder now derives county_code from
        # p.county_code (a real SELECT column, not an external parameter)
        # and carries it in every GROUP BY/GROUPING SETS clause -- and every
        # join to a second parcel_tax_year/parcel_metrics alias is now also
        # equality-scoped on county_code, not just geo_id/tax_year.
        check(f"breakdown_sql({view}): p.county_code selected", "p.county_code" in b)
        check(f"breakdown_sql({view}): county_code carried in GROUPING SETS",
              "GROUPING SETS ((p.county_code" in b and "), (p.county_code))" in b)
        check(f"breakdown_sql({view}): both parcel_tax_year joins equality-scoped on county_code",
              b.count("t25.county_code = p.county_code") == 1 and b.count("t26.county_code = p.county_code") == 1)
        check(f"single_year_mv_sql({view}): p.county_code selected", "p.county_code" in mv25)
        check(f"single_year_mv_sql({view}): county_code carried in GROUPING SETS",
              "GROUPING SETS ((p.county_code" in mv25 and "), (p.county_code))" in mv25)
        check(f"single_year_mv_sql({view}): parcel_tax_year join equality-scoped on county_code",
              "t.county_code = p.county_code" in mv25)
        check(f"part4_agg_sql({view}): p.county_code selected", "p.county_code" in p4)
        check(f"part4_agg_sql({view}): GROUP BY p.county_code present (one row per county, not one per view)",
              "GROUP BY p.county_code" in p4)
        check(f"part4_agg_sql({view}): both parcel_tax_year joins + the parcel_metrics LEFT JOIN "
              f"equality-scoped on county_code",
              p4.count("t25.county_code = p.county_code") == 1
              and p4.count("t26.county_code = p.county_code") == 1
              and "pm.county_code = p.county_code" in p4)
        check(f"cert_agg_sql({view}): p.county_code selected", "p.county_code" in ca)
        check(f"cert_agg_sql({view}): GROUP BY p.county_code present", "GROUP BY p.county_code" in ca)
        check(f"neighborhoods_sql({view}): p.county_code selected", "p.county_code" in nb)
        check(f"neighborhoods_sql({view}): GROUP BY (county_code, neighborhood_cd), not neighborhood_cd alone",
              "GROUP BY p.county_code, p.neighborhood_cd" in nb)


def test_breakdown_and_neighborhoods_sql_differ_by_view_where():
    """Two different sector views must produce DIFFERENT SQL (the view-scoping
    fragment actually varies), not the same string reused by accident."""
    b_res = rss.breakdown_sql("residential")
    b_land = rss.breakdown_sql("land")
    check("breakdown_sql('residential') != breakdown_sql('land')", b_res != b_land)

    nb_office = rss.neighborhoods_sql("office")
    nb_hotel = rss.neighborhoods_sql("hotel")
    check("neighborhoods_sql('office') != neighborhoods_sql('hotel')", nb_office != nb_hotel)


# ── Part 2: merge_breakdown_rows() — independent hand-computed fixtures ─────
# (port-correctness verification only; see module docstring)

def test_merge_breakdown_rows_applies_inner_join_suppression_override():
    """
    Synthetic fixture: the paired-JOIN breakdown query UNDERCOUNTS 2026
    dollars for Residential (and therefore the grand total) by exactly
    $2.0B, because it drops a parcel present in 2026 but not 2025 — the
    exact defect the INNER JOIN suppression fix (app.py, July 2026 "Fix
    parcel-exclusion filtering" brief item 5) exists to correct. Expected
    values hand-derived from the fixture's own numbers, not from running
    the code first. Single-county fixture (all rows county_code='TRAVIS')
    -- see test_merge_breakdown_rows_keeps_two_counties_separate below for
    the genuinely new multi-county behavior PX-20260831-02 Task 1 adds.
    """
    breakdown_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "sort_key": "Residential", "n_parcels": 100, "n_up": 60,
         "n_down": 30, "n_flat": 10, "median_pct": 5.0, "p25_pct": 1.0, "p75_pct": 9.0,
         "total_mv25_b": 10.0, "total_mv26_b": 11.0},
        {"county_code": "TRAVIS", "ptype": "Commercial", "sort_key": "Commercial", "n_parcels": 50, "n_up": 20,
         "n_down": 25, "n_flat": 5, "median_pct": -1.0, "p25_pct": -3.0, "p75_pct": 2.0,
         "total_mv25_b": 20.0, "total_mv26_b": 19.5},
        {"county_code": "TRAVIS", "ptype": None, "sort_key": None, "n_parcels": 150, "n_up": 80, "n_down": 55,
         "n_flat": 15, "median_pct": 2.0, "p25_pct": -1.0, "p75_pct": 5.0, "total_mv25_b": 30.0, "total_mv26_b": 30.5},
    ]
    mv25_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "total_mv_b": 10.0},
        {"county_code": "TRAVIS", "ptype": "Commercial", "total_mv_b": 20.0},
        {"county_code": "TRAVIS", "ptype": None, "total_mv_b": 30.0},
    ]
    mv26_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "total_mv_b": 13.0},   # +2.0 vs paired-JOIN's 11.0
        {"county_code": "TRAVIS", "ptype": "Commercial", "total_mv_b": 19.5},
        {"county_code": "TRAVIS", "ptype": None, "total_mv_b": 32.5},             # +2.0 vs paired-JOIN's 30.5
    ]

    rows, totals_rows = rss.merge_breakdown_rows(breakdown_rows, mv25_rows, mv26_rows)

    check("2 per-ptype rows returned (total row excluded)", len(rows) == 2, rows)
    res = next(r for r in rows if r["ptype"] == "Residential")
    com = next(r for r in rows if r["ptype"] == "Commercial")

    check("Residential total_mv26_b overridden to single-year value (13.0, not paired-JOIN's 11.0)",
          res["total_mv26_b"] == 13.0, res)
    check("Residential total_mv25_b unchanged (single-year and paired-JOIN agree at 10.0)",
          res["total_mv25_b"] == 10.0, res)
    check("Commercial total_mv26_b unchanged where single-year matches paired value (19.5)",
          com["total_mv26_b"] == 19.5, com)
    check("non-dollar fields (n_parcels, n_up, ...) pass through untouched",
          res["n_parcels"] == 100 and res["n_up"] == 60 and res["n_down"] == 30 and res["n_flat"] == 10, res)

    check("exactly one totals row produced (one county in the fixture)", len(totals_rows) == 1, totals_rows)
    totals = totals_rows[0]
    check("totals row's county_code is TRAVIS", totals["county_code"] == "TRAVIS", totals)
    check("grand total total_mv26_b overridden to single-year value (32.5, not paired-JOIN's 30.5)",
          totals["total_mv26_b"] == 32.5, totals)
    check("grand total total_mv25_b unchanged (30.0)", totals["total_mv25_b"] == 30.0, totals)
    check("grand total n_total/n_up/n_down/n_flat pass through from the paired-JOIN row",
          totals["n_total"] == 150 and totals["n_up"] == 80 and totals["n_down"] == 55 and totals["n_flat"] == 15,
          totals)
    check("grand total median_pct passes through unchanged (2.0 — a % stat, not a dollar stat)",
          totals["median_pct"] == 2.0, totals)


def test_merge_breakdown_rows_empty_view_produces_no_total_rows():
    """A view with zero qualifying parcels (e.g. a sector with no data in
    the current load) must return (rows=[], totals_rows=[]) — distinct from
    'table missing/stale', per the Tier 1 design (see schema.sql's
    snapshot_totals comment: a view genuinely absent from load-time results
    is a real, valid 'no data' state, not an error)."""
    rows, totals_rows = rss.merge_breakdown_rows([], [], [])
    check("empty breakdown_rows -> rows == []", rows == [], rows)
    check("empty breakdown_rows -> totals_rows == [] (PX-20260831-02: a list now, never None)",
          totals_rows == [], totals_rows)


def test_merge_breakdown_rows_falls_back_when_single_year_data_missing_for_ptype():
    """If a ptype exists in the paired-JOIN breakdown but the independent
    single-year query returns nothing for it (e.g. a genuinely empty single-
    year slice), the merge must fall back to the paired-JOIN's own value via
    .get((county_code, ptype), fallback) — never KeyError, never silently
    become None."""
    rows, totals_rows = rss.merge_breakdown_rows(
        [{"county_code": "TRAVIS", "ptype": "Land", "sort_key": "Land", "n_parcels": 5, "n_up": 1, "n_down": 1,
          "n_flat": 3, "median_pct": 0.0, "p25_pct": 0.0, "p75_pct": 0.0, "total_mv25_b": 1.0, "total_mv26_b": 1.0},
         {"county_code": "TRAVIS", "ptype": None, "sort_key": None, "n_parcels": 5, "n_up": 1, "n_down": 1,
          "n_flat": 3, "median_pct": 0.0, "p25_pct": 0.0, "p75_pct": 0.0, "total_mv25_b": 1.0, "total_mv26_b": 1.0}],
        [], [],
    )
    check("fallback: per-ptype total_mv25_b/26_b keep paired-JOIN value when single-year is missing",
          rows[0]["total_mv25_b"] == 1.0 and rows[0]["total_mv26_b"] == 1.0, rows)
    check("fallback: grand total total_mv25_b keeps paired-JOIN value when single-year is missing",
          totals_rows[0]["total_mv25_b"] == 1.0, totals_rows)


def test_merge_breakdown_rows_keeps_two_counties_separate():
    """THE genuinely new behavior PX-20260831-02 Task 1 requires: two
    counties' grand-total rows both have ptype IS NULL, but DIFFERENT
    county_code values. Matching mv25/mv26 rows by ptype alone (the old
    single-county behavior) would silently blend or misattribute one
    county's single-year dollar total onto the other's grand-total row --
    exactly the class of cross-county contamination this whole brief exists
    to close off. This fixture makes Dallas's 2026 single-year total
    diverge from Travis's by an amount ($5.0B) that would be immediately
    obvious if the two got crossed."""
    breakdown_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "sort_key": "Residential", "n_parcels": 100, "n_up": 60,
         "n_down": 30, "n_flat": 10, "median_pct": 5.0, "p25_pct": 1.0, "p75_pct": 9.0,
         "total_mv25_b": 10.0, "total_mv26_b": 11.0},
        {"county_code": "TRAVIS", "ptype": None, "sort_key": None, "n_parcels": 100, "n_up": 60, "n_down": 30,
         "n_flat": 10, "median_pct": 5.0, "p25_pct": 1.0, "p75_pct": 9.0, "total_mv25_b": 10.0, "total_mv26_b": 11.0},
        {"county_code": "DALLAS", "ptype": "Residential", "sort_key": "Residential", "n_parcels": 200, "n_up": 120,
         "n_down": 60, "n_flat": 20, "median_pct": 3.0, "p25_pct": 0.5, "p75_pct": 6.0,
         "total_mv25_b": 40.0, "total_mv26_b": 42.0},
        {"county_code": "DALLAS", "ptype": None, "sort_key": None, "n_parcels": 200, "n_up": 120, "n_down": 60,
         "n_flat": 20, "median_pct": 3.0, "p25_pct": 0.5, "p75_pct": 6.0, "total_mv25_b": 40.0, "total_mv26_b": 42.0},
    ]
    mv25_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "total_mv_b": 10.0},
        {"county_code": "TRAVIS", "ptype": None, "total_mv_b": 10.0},
        {"county_code": "DALLAS", "ptype": "Residential", "total_mv_b": 40.0},
        {"county_code": "DALLAS", "ptype": None, "total_mv_b": 40.0},
    ]
    mv26_rows = [
        {"county_code": "TRAVIS", "ptype": "Residential", "total_mv_b": 11.0},
        {"county_code": "TRAVIS", "ptype": None, "total_mv_b": 11.0},
        {"county_code": "DALLAS", "ptype": "Residential", "total_mv_b": 47.0},  # +5.0 vs paired-JOIN's 42.0
        {"county_code": "DALLAS", "ptype": None, "total_mv_b": 47.0},
    ]

    rows, totals_rows = rss.merge_breakdown_rows(breakdown_rows, mv25_rows, mv26_rows)

    check("2 per-ptype rows returned, one per county", len(rows) == 2, rows)
    check("2 totals rows returned, one per county", len(totals_rows) == 2, totals_rows)

    travis_total = next(t for t in totals_rows if t["county_code"] == "TRAVIS")
    dallas_total = next(t for t in totals_rows if t["county_code"] == "DALLAS")

    check("TRAVIS grand total total_mv26_b is TRAVIS's own value (11.0), unaffected by Dallas's override",
          travis_total["total_mv26_b"] == 11.0, travis_total)
    check("DALLAS grand total total_mv26_b picks up DALLAS's own override (47.0), not TRAVIS's",
          dallas_total["total_mv26_b"] == 47.0, dallas_total)
    check("DALLAS grand total total_mv25_b unaffected (40.0)", dallas_total["total_mv25_b"] == 40.0, dallas_total)

    dallas_row = next(r for r in rows if r["county_code"] == "DALLAS")
    check("DALLAS per-ptype row picks up its own single-year override (47.0), not TRAVIS's value",
          dallas_row["total_mv26_b"] == 47.0, dallas_row)


# ── Part 3: FakeConn/FakeCursor DB-call-shape tests ──────────────────────────
# Same style as loaders/test_refresh_group_stats.py's own fake DB layer.
# _compute_one_view() is monkeypatched to a canned Python function (see
# module docstring, Part 3) since it imports psycopg2.extras directly and
# psycopg2 is not installed in this sandbox — build_shadow()/
# refresh_snapshot_summary() only ever call it by (module-global) name, so
# reassigning rss._compute_one_view works without touching cursor_factory.

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.executed.append((norm, params))
        upper = norm.upper()
        if upper.startswith("INSERT INTO LOAD_BATCH"):
            self._pending = self.conn.pop_result()
        elif upper.startswith("SELECT"):
            self._pending = self.conn.pop_result()
        else:
            self._pending = ([], [])

    def fetchone(self):
        rows, _cols = self._pending
        return rows[0] if rows else None

    def fetchall(self):
        rows, _cols = self._pending
        return rows


class FakeConn:
    def __init__(self, results_queue=None):
        self.executed = []
        self.committed_count = 0
        self._results_queue = list(results_queue or [])

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.committed_count += 1

    def pop_result(self):
        if self._results_queue:
            return self._results_queue.pop(0)
        return [], []


def _sql_kinds(executed):
    kinds = []
    for sql, _params in executed:
        u = sql.upper()
        if u.startswith("DROP TABLE"):
            kinds.append("DROP")
        elif u.startswith("CREATE TABLE"):
            kinds.append("CREATE")
        elif u.startswith("INSERT INTO SNAPSHOT_BREAKDOWN_SHADOW"):
            kinds.append("INSERT_BREAKDOWN")
        elif u.startswith("INSERT INTO SNAPSHOT_TOTALS_SHADOW"):
            kinds.append("INSERT_TOTALS")
        elif u.startswith("INSERT INTO SNAPSHOT_NEIGHBORHOOD_MOVERS_SHADOW"):
            kinds.append("INSERT_NEIGHBORHOOD")
        elif u.startswith("INSERT INTO LOAD_BATCH"):
            kinds.append("INSERT_LOAD_BATCH")
        elif "RENAME TO SNAPSHOT_BREAKDOWN_OLD" in u:
            kinds.append("RENAME_BREAKDOWN_OLD")
        elif "RENAME TO SNAPSHOT_BREAKDOWN" in u:
            kinds.append("RENAME_BREAKDOWN_NEW")
        elif "RENAME TO SNAPSHOT_TOTALS_OLD" in u:
            kinds.append("RENAME_TOTALS_OLD")
        elif "RENAME TO SNAPSHOT_TOTALS" in u:
            kinds.append("RENAME_TOTALS_NEW")
        elif "RENAME TO SNAPSHOT_NEIGHBORHOOD_MOVERS_OLD" in u:
            kinds.append("RENAME_NEIGHBORHOOD_OLD")
        elif "RENAME TO SNAPSHOT_NEIGHBORHOOD_MOVERS" in u:
            kinds.append("RENAME_NEIGHBORHOOD_NEW")
        elif u.startswith("SELECT DISTINCT SOURCE_IMPORT_BATCH_ID"):
            kinds.append("SELECT_BATCH_IDS_IN_TABLE")
        elif u.startswith("SELECT MAX(BATCH_ID)"):
            kinds.append("SELECT_LATEST_BATCH")
        else:
            kinds.append(f"OTHER:{sql[:50]}")
    return kinds


# PX-20260831-02 Task 1: _compute_one_view() now returns a 3-tuple
# (rows, totals_rows, neighborhoods_by_county) -- totals_rows is a LIST (one
# dict per county with qualifying parcels) and neighborhoods_by_county is a
# {county_code: [rows]} dict, replacing the old 7-tuple single-county shape
# (rows, totals_row-or-None, new_construction_count, risk_flagged_count,
# n_preliminary_2026, n_total_2026, flat neighborhood_rows). This single-
# county fixture (all TRAVIS) exercises the plumbing through build_shadow()/
# refresh_snapshot_summary() unchanged; see Part 7 below for the
# genuinely-new two-county fixtures.
_FAKE_ONE_VIEW_RESULT = (
    [{"county_code": "TRAVIS", "ptype": "TypeA", "sort_key": "TypeA", "n_parcels": 5, "n_up": 2, "n_down": 2,
      "n_flat": 1, "median_pct": 1.0, "p25_pct": 0.5, "p75_pct": 1.5, "total_mv25_b": 1.0, "total_mv26_b": 1.1}],
    [{"county_code": "TRAVIS", "n_total": 5, "n_up": 2, "n_down": 2, "n_flat": 1, "total_mv25_b": 1.0,
      "total_mv26_b": 1.1, "median_pct": 1.0, "new_construction_count": 1, "risk_flagged_count": 0,
      "n_preliminary_2026": 2, "n_total_2026": 5}],
    {"TRAVIS": [{"neighborhood_cd": "NB1", "n_parcels": 10, "median_pct": 2.0}]},
)


def _fake_compute_one_view(conn, view, verbose=True):
    return _FAKE_ONE_VIEW_RESULT


def test_build_shadow_drops_and_creates_all_three_tables_before_any_insert():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn()
        breakdown_n, totals_n, nb_n = rss.build_shadow(conn, batch_id=7, verbose=False)

        kinds = _sql_kinds(conn.executed)
        ddl_kinds = [k for k in kinds if k in ("DROP", "CREATE")]
        check("build_shadow: 3 DROP + 3 CREATE (one pair per shadow table)",
              ddl_kinds == ["DROP", "CREATE", "DROP", "CREATE", "DROP", "CREATE"], ddl_kinds)

        n_views = len(_SNAPSHOT_VALID_VIEWS)
        check("build_shadow: one breakdown row inserted per view (11 views x 1 row each)",
              breakdown_n == n_views, breakdown_n)
        check("build_shadow: one totals row inserted per view (every fake view has a totals row)",
              totals_n == n_views, totals_n)
        check("build_shadow: one neighborhood row inserted per view",
              nb_n == n_views, nb_n)

        check("build_shadow: commits exactly once",
              conn.committed_count == 1, conn.committed_count)

        insert_kinds = [k for k in kinds if k.startswith("INSERT_") and k != "INSERT_LOAD_BATCH"]
        check("build_shadow: total INSERT count == 3 x 11 views (breakdown+totals+neighborhood each view)",
              len(insert_kinds) == 3 * n_views, len(insert_kinds))

        first_insert = next(p for k, p in zip(kinds, conn.executed) if k == "INSERT_BREAKDOWN")
        check("build_shadow: breakdown INSERT is bound with batch_id=7",
              first_insert[1]["batch_id"] == 7, first_insert[1])
    finally:
        rss._compute_one_view = orig


def test_build_shadow_skips_totals_and_neighborhood_inserts_for_empty_view():
    """An empty view (no qualifying parcels for any county -> totals_rows is
    an empty list) must still get its breakdown/neighborhood rows attempted
    per the real function's `if counties_with_data:` guard on the part4/
    cert/neighborhood fetches -- but with zero rows in every list, produces
    zero inserts for that view without erroring."""
    def _fake_empty_view(conn, view, verbose=True):
        return ([], [], {})

    orig = rss._compute_one_view
    rss._compute_one_view = _fake_empty_view
    try:
        conn = FakeConn()
        breakdown_n, totals_n, nb_n = rss.build_shadow(conn, batch_id=1, verbose=False)
        check("build_shadow: empty view across all 11 views -> zero breakdown rows",
              breakdown_n == 0, breakdown_n)
        check("build_shadow: empty view across all 11 views -> zero totals rows",
              totals_n == 0, totals_n)
        check("build_shadow: empty view across all 11 views -> zero neighborhood rows",
              nb_n == 0, nb_n)
    finally:
        rss._compute_one_view = orig


def test_swap_shadow_in_issues_nine_statements_across_three_tables_and_commits_once():
    conn = FakeConn()
    rss.swap_shadow_in(conn, verbose=False)

    kinds = _sql_kinds(conn.executed)
    expected = [
        "RENAME_BREAKDOWN_OLD", "RENAME_BREAKDOWN_NEW", "DROP",
        "RENAME_TOTALS_OLD", "RENAME_TOTALS_NEW", "DROP",
        "RENAME_NEIGHBORHOOD_OLD", "RENAME_NEIGHBORHOOD_NEW", "DROP",
    ]
    check("swap_shadow_in: 9 statements (3 tables x rename-old/rename-new/drop-old), in order",
          kinds == expected, kinds)
    check("swap_shadow_in: commits exactly once (all 3 tables swap atomically together)",
          conn.committed_count == 1, conn.committed_count)


def test_refresh_snapshot_summary_dry_run_makes_no_writes():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn()
        result = rss.refresh_snapshot_summary(conn, dry_run=True, verbose=False)

        check("dry-run: issues zero DB statements (pure computation, no cursor use of its own)",
              conn.executed == [], conn.executed)
        check("dry-run: never commits (no writes at all)", conn.committed_count == 0, conn.committed_count)
        check("dry-run: result marked dry_run=True", result["dry_run"] is True)
        n_views = len(_SNAPSHOT_VALID_VIEWS)
        check("dry-run: breakdown_row_count reflects 1 row x 11 views",
              result["breakdown_row_count"] == n_views, result)
        check("dry-run: totals_row_count reflects 11 views all having a totals row",
              result["totals_row_count"] == n_views, result)
        check("dry-run: neighborhood_row_count reflects 1 row x 11 views",
              result["neighborhood_row_count"] == n_views, result)
        check("dry-run: batch_id is None (nothing minted)", result["batch_id"] is None, result)
        check("dry-run: a sample is included for spot-checking", result["sample"] is not None, result)
    finally:
        rss._compute_one_view = orig


def test_refresh_snapshot_summary_mints_batch_when_none_given():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn(results_queue=[([(101,)], ["batch_id"])])
        result = rss.refresh_snapshot_summary(conn, batch_id=None, dry_run=False, verbose=False)

        kinds = _sql_kinds(conn.executed)
        check("no-batch-given: mints a load_batch row FIRST, before building any shadow table",
              kinds[0] == "INSERT_LOAD_BATCH", kinds[:3])
        check("no-batch-given: uses the minted batch_id (101) for the result",
              result["batch_id"] == 101, result)
        check("no-batch-given: swap statements present after the build phase",
              "RENAME_BREAKDOWN_OLD" in kinds, kinds)
    finally:
        rss._compute_one_view = orig


def test_refresh_snapshot_summary_reuses_caller_supplied_batch_id():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn()
        result = rss.refresh_snapshot_summary(conn, batch_id=555, dry_run=False, verbose=False)

        kinds = _sql_kinds(conn.executed)
        check("caller-supplied batch_id: NEVER inserts into load_batch",
              "INSERT_LOAD_BATCH" not in kinds, kinds)
        check("caller-supplied batch_id: result uses exactly 555, not a minted one",
              result["batch_id"] == 555, result)

        insert_params = next(p for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_BREAKDOWN")
        check("caller-supplied batch_id: breakdown INSERT bound with batch_id=555",
              insert_params["batch_id"] == 555, insert_params)
    finally:
        rss._compute_one_view = orig


# ── Part 4: assert_snapshot_summary_fresh() — 3-table staleness assertion ──

def test_assert_snapshot_summary_fresh_true_when_all_three_tables_match_latest():
    conn = FakeConn(results_queue=[
        ([(5,)], ["source_import_batch_id"]),  # snapshot_breakdown
        ([(5,)], ["source_import_batch_id"]),  # snapshot_totals
        ([(5,)], ["source_import_batch_id"]),  # snapshot_neighborhood_movers
        ([(5,)], ["max"]),                      # MAX(batch_id)
    ])
    is_fresh, detail = rss.assert_snapshot_summary_fresh(conn)
    check("fresh case: is_fresh True when all 3 tables + latest batch all agree (5)",
          is_fresh is True, detail)
    check("fresh case: detail reports latest_batch_id == 5", detail["latest_batch_id"] == 5, detail)


def test_assert_snapshot_summary_fresh_false_when_stale_vs_latest_batch():
    conn = FakeConn(results_queue=[
        ([(3,)], ["source_import_batch_id"]),
        ([(3,)], ["source_import_batch_id"]),
        ([(3,)], ["source_import_batch_id"]),
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rss.assert_snapshot_summary_fresh(conn)
    check("stale case: is_fresh False when all 3 tables agree with each other but lag the latest batch",
          is_fresh is False, detail)
    check("stale case: reason mentions STALE", "STALE" in detail["reason"], detail)


def test_assert_snapshot_summary_fresh_false_when_one_table_empty():
    conn = FakeConn(results_queue=[
        ([], ["source_import_batch_id"]),        # snapshot_breakdown empty
        ([(5,)], ["source_import_batch_id"]),
        ([(5,)], ["source_import_batch_id"]),
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rss.assert_snapshot_summary_fresh(conn)
    check("empty-table case: is_fresh False", is_fresh is False, detail)
    check("empty-table case: reason names the empty table", "snapshot_breakdown" in detail["reason"], detail)


def test_assert_snapshot_summary_fresh_false_when_one_table_has_multiple_batches():
    conn = FakeConn(results_queue=[
        ([(3,), (5,)], ["source_import_batch_id"]),  # a partial/failed refresh
        ([(5,)], ["source_import_batch_id"]),
        ([(5,)], ["source_import_batch_id"]),
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rss.assert_snapshot_summary_fresh(conn)
    check("multi-batch-within-table case: is_fresh False (should be impossible under a real atomic swap)",
          is_fresh is False, detail)
    check("multi-batch-within-table case: reason mentions more than one batch_id",
          "more than one batch_id" in detail["reason"], detail)


def test_assert_snapshot_summary_fresh_false_when_tables_disagree_with_each_other():
    """The genuinely NEW failure mode vs. group_stats's single-table
    assertion: all three tables are individually internally-consistent
    (one batch_id each), but DISAGREE with each other — proof the 3-table
    atomic swap did not actually keep them in sync (a real bug), distinct
    from ordinary staleness."""
    conn = FakeConn(results_queue=[
        ([(5,)], ["source_import_batch_id"]),  # snapshot_breakdown -> batch 5
        ([(5,)], ["source_import_batch_id"]),  # snapshot_totals -> batch 5
        ([(4,)], ["source_import_batch_id"]),  # snapshot_neighborhood_movers -> batch 4 (mismatch!)
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rss.assert_snapshot_summary_fresh(conn)
    check("cross-table-disagreement case: is_fresh False", is_fresh is False, detail)
    check("cross-table-disagreement case: reason mentions the tables disagreeing with each other",
          "disagree with each other" in detail["reason"], detail)


# ── Part 5: AGGPRECOMP-2-FIX — sort_key width (StringDataRightTruncation) ──
#
# Real bug found running the real refresh against production: build_shadow()
# writes sort_key = str(ptype) for every non-"overall" view (confirmed via
# ptype_and_sort_case_for_view() -- sort_case IS ptype_case for those views),
# but schema.sql's snapshot_breakdown.sort_key was VARCHAR(10). Real
# USE_CODE_LOOKUP descriptions/fallback labels/size-tier labels routinely
# exceed 10 characters (measured real max: 29 chars, e.g.
# "Self-Service (Car Wash Booth)" and "Mini-Warehouse / Self-Storage") ->
# psycopg2.errors.StringDataRightTruncation on the real INSERT.
#
# Why --dry-run didn't catch it: refresh_snapshot_summary()'s dry_run branch
# only calls _compute_one_view() and does Python-side len() counts -- it
# never calls build_shadow() and never issues a single INSERT statement, so
# the VARCHAR(10) constraint was never evaluated by dry-run at all. Only the
# real (non-dry-run) path calls build_shadow(), which is the only code path
# that actually sends sort_key to Postgres for constraint checking.
#
# Fix: widened sort_key to VARCHAR(120), matching ptype's own column width
# exactly (schema.sql's own comment already documented the contract
# "== ptype for other views" -- sort_key is meant to be byte-identical to
# ptype for these views, not a derived/truncated code, so truncating it
# would violate that documented contract and risk silently conflating two
# distinct long ptype values that happen to share a truncated prefix).
# sort_key is not used for read-time ORDER BY on non-"overall" views either
# (app.py's order_sql uses "ORDER BY n_parcels DESC NULLS LAST" for
# sector/commercial views; "ORDER BY sort_key::int NULLS LAST" only applies
# to "overall", whose sort_key is always a short "1".."9"/"99" string and is
# unaffected by this bug) -- so there is no functional reason to keep it
# artificially short, and matching ptype's already-established width closes
# off the whole failure mode permanently rather than picking a new number
# that could be outgrown again if TCAD lengthens a use-code description.

def test_build_shadow_does_not_truncate_a_realistically_long_sort_key():
    """Reproduces the exact real-world shape of the bug: a non-'overall'
    view whose sort_key equals a real, long USE_CODE_LOOKUP-style ptype
    string (29 chars -- the actual measured real-data maximum, taken
    verbatim from USE_CODE_LOOKUP's real 'Self-Service (Car Wash Booth)'
    entry). Proves build_shadow()'s own Python code does not itself
    mangle/truncate the value before sending it to Postgres -- the fix here
    is schema-side (column width), not a code change, and this test proves
    the code path faithfully passes the full string through unmodified."""
    long_ptype = "Self-Service (Car Wash Booth)"  # 29 chars, real USE_CODE_LOOKUP value
    check("sanity: fixture ptype really is longer than the old VARCHAR(10)",
          len(long_ptype) > 10, len(long_ptype))

    fake_result = (
        [{"county_code": "TRAVIS", "ptype": long_ptype, "sort_key": long_ptype, "n_parcels": 5, "n_up": 2,
          "n_down": 2, "n_flat": 1, "median_pct": 1.0, "p25_pct": 0.5, "p75_pct": 1.5,
          "total_mv25_b": 1.0, "total_mv26_b": 1.1}],
        [{"county_code": "TRAVIS", "n_total": 5, "n_up": 2, "n_down": 2, "n_flat": 1, "total_mv25_b": 1.0,
          "total_mv26_b": 1.1, "median_pct": 1.0, "new_construction_count": 1, "risk_flagged_count": 0,
          "n_preliminary_2026": 2, "n_total_2026": 5}],
        {"TRAVIS": [{"neighborhood_cd": "NB1", "n_parcels": 10, "median_pct": 2.0}]},
    )

    orig = rss._compute_one_view
    rss._compute_one_view = lambda conn, view, verbose=True: fake_result
    try:
        conn = FakeConn()
        rss.build_shadow(conn, batch_id=9, verbose=False)

        kinds = _sql_kinds(conn.executed)
        insert_params = next(p for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_BREAKDOWN")
        check("build_shadow: long sort_key passed through to the INSERT params unmodified",
              insert_params["sort_key"] == long_ptype, insert_params["sort_key"])
        check("build_shadow: long sort_key not silently truncated to <=10 chars by our own code",
              len(insert_params["sort_key"]) == len(long_ptype), len(insert_params["sort_key"]))
    finally:
        rss._compute_one_view = orig


def _real_max_ptype_or_sort_key_length():
    """Computes the TRUE maximum length across every real string that can
    appear as ptype (and therefore as sort_key, for the 10 non-'overall'
    views where sort_key == ptype byte-for-byte) -- USE_CODE_LOOKUP
    descriptions, every sector's fallback label, and both land/agricultural
    size-tier label sets. Deliberately NOT hardcoded/eyeballed -- computed
    fresh from the real snapshot_taxonomy.py constants every time this test
    runs, so if TCAD's own use-code descriptions (an external, versioned
    reference table Parcelytics does not control) ever get longer, this
    test fails BEFORE a production StringDataRightTruncation does."""
    lengths = [len(desc) for desc, _method in _st.USE_CODE_LOOKUP.values()]

    fallback_labels = set()
    for view, sector_label in _st._SNAPSHOT_SECTOR_VIEWS.items():
        fallback_labels.add("Uncategorized" if sector_label == "Other" else f"Other {sector_label}")
    fallback_labels.add("Other Commercial")  # legacy "commercial" view
    lengths.extend(len(lbl) for lbl in fallback_labels)

    lengths.extend(len(label) for _cond, label in _st.SNAPSHOT_LAND_SIZE_TIERS)
    lengths.extend(len(label) for _cond, label in _st.SNAPSHOT_AG_SIZE_TIERS)

    return max(lengths)


def test_schema_sort_key_width_covers_the_real_measured_maximum():
    """Static check against the real schema.sql text (no live DB needed):
    proves the sort_key column is declared wide enough for every real
    ptype/sort_key value snapshot_taxonomy.py can actually produce today.
    This is the regression guard Diego asked for -- it fails loudly if
    someone re-narrows the column, or if a future taxonomy change produces
    a longer label than the column can hold, instead of waiting to find out
    via a live StringDataRightTruncation again."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema.sql")
    with open(schema_path) as f:
        schema_text = f.read()

    import re
    m = re.search(r"^\s*sort_key\s+VARCHAR\((\d+)\)", schema_text, re.MULTILINE)
    check("schema.sql: snapshot_breakdown.sort_key VARCHAR(N) declaration found", m is not None)
    if not m:
        return
    declared_width = int(m.group(1))

    real_max = _real_max_ptype_or_sort_key_length()
    check(f"schema.sql: sort_key VARCHAR({declared_width}) >= real measured max ptype length ({real_max})",
          declared_width >= real_max, (declared_width, real_max))
    check("schema.sql: sort_key is no longer the old, too-narrow VARCHAR(10)",
          declared_width != 10, declared_width)


# ── Part 6: AGGPRECOMP-2-FIX-2, Fix 3 — cross-table consistency assertion ──
#
# Fable's review flagged the real gap: proving the 3-table SWAP is atomic
# (Part 4's tests above) is not the same claim as proving the DATA is
# internally consistent -- a refresh could complete, swap cleanly, and
# still write a snapshot_totals row whose numbers don't actually match the
# real sum of that view's own snapshot_breakdown rows, if breakdown_sql()
# and single_year_mv_sql() ever drift out of sync. Per Diego: prove this
# assertion two ways -- passing on real/correct data, AND correctly firing
# on deliberately corrupted data -- "an assertion that's never been proven
# to fire is a hope, not a safeguard."

def test_consistency_passes_on_matching_breakdown_and_totals():
    conn = FakeConn(results_queue=[
        # SUM(...) FROM snapshot_breakdown GROUP BY view, county_code -- 2 (view, county_code) pairs
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0),
          ("retail", "TRAVIS", 50, 30, 15, 5, 12.5, 13.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        # view, county_code, n_total, n_up, n_down, n_flat, total_mv25_b, total_mv26_b FROM snapshot_totals
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0),
          ("retail", "TRAVIS", 50, 30, 15, 5, 12.5, 13.0)],
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn)
    check("consistency: is_consistent True when breakdown sums exactly match totals",
          is_consistent is True, detail)
    check("consistency: zero mismatches reported", detail["mismatches"] == [], detail["mismatches"])
    check("consistency: both (view, county_code) pairs checked (PX-20260830-05 Task 3: grouped "
          "by (view, county_code), not just view)",
          detail["views_and_counties_checked"] == [("overall", "TRAVIS"), ("retail", "TRAVIS")], detail)


def test_consistency_passes_within_dollar_tolerance():
    """A few cents of independent-rounding drift between breakdown_sql()'s
    per-ptype ROUND(...,3) and single_year_mv_sql()'s own independent
    ROUND(...,3) is expected, benign behavior -- must NOT false-positive."""
    conn = FakeConn(results_queue=[
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.004, 190.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0)],
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn, tolerance_b=0.01)
    check("consistency: tiny rounding drift ($4K on a billions column) within tolerance -> still consistent",
          is_consistent is True, detail)


def test_consistency_fails_on_corrupted_n_total():
    """Deliberate corruption: snapshot_totals.n_total doesn't match the real
    sum of snapshot_breakdown.n_parcels for the same view -- proves the
    assertion actually FIRES, not just that it can pass."""
    conn = FakeConn(results_queue=[
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        ([("overall", "TRAVIS", 995, 600, 300, 100, 180.0, 190.0)],  # n_total corrupted: 995 != real sum 1000
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn)
    check("consistency CORRUPTION CASE: is_consistent False when n_total doesn't match real breakdown sum",
          is_consistent is False, detail)
    check("consistency CORRUPTION CASE: mismatch identifies the n_total field",
          any(m.get("field") == "n_total" for m in detail["mismatches"]), detail["mismatches"])
    check("consistency CORRUPTION CASE: mismatch records both the real sum and the wrong stored value",
          any(m.get("breakdown_sum") == 1000 and m.get("totals_value") == 995
              for m in detail["mismatches"]), detail["mismatches"])


def test_consistency_fails_on_corrupted_dollar_total_beyond_tolerance():
    """Deliberate corruption: a genuine drift (not rounding noise) between
    breakdown_sql()'s and single_year_mv_sql()'s dollar totals -- the exact
    'refresh-logic drift' failure mode this assertion exists to catch
    (matches the real SNAPSHOT-CORRECTNESS-1 bug class: one query site
    missing a WHERE-clause fragment the other one has)."""
    conn = FakeConn(results_queue=[
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 205.0)],  # total_mv26_b way off: 205 != real sum 190
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn)
    check("consistency CORRUPTION CASE: is_consistent False on a real (non-rounding) dollar-total drift",
          is_consistent is False, detail)
    check("consistency CORRUPTION CASE: mismatch identifies total_mv26_b specifically",
          any(m.get("field") == "total_mv26_b" for m in detail["mismatches"]), detail["mismatches"])


def test_consistency_fails_when_view_missing_from_one_table():
    """A view present in snapshot_totals but with zero snapshot_breakdown
    rows (or vice versa) should be impossible under a correct refresh --
    proves this genuinely different corruption shape is also caught."""
    conn = FakeConn(results_queue=[
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0),
          ("retail", "TRAVIS", 50, 30, 15, 5, 12.5, 13.0)],  # "retail" totals row with NO matching breakdown rows
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn)
    check("consistency CORRUPTION CASE: is_consistent False when a view's totals row has no breakdown rows",
          is_consistent is False, detail)
    check("consistency CORRUPTION CASE: mismatch names the orphaned view",
          any(m["view"] == "retail" for m in detail["mismatches"]), detail["mismatches"])


def test_consistency_catches_cross_county_blending_regression():
    """PX-20260830-05 Task 3 (Bucket C): the exact false-pass this fix
    closes. Two counties, same view: if the assertion still grouped by
    view alone, TRAVIS's breakdown sum (1000) would get blended with
    DALLAS's breakdown sum (500) into 1500, which happens to equal
    neither county's real snapshot_totals row (1000 and 500) -- so a
    view-only grouping would have reported BOTH counties' rows as
    mismatches even though each one is individually, correctly
    consistent. Grouped by (view, county_code), this must report zero
    mismatches -- proving the fix actually changed behavior, not just
    added an unused column to the SELECT list."""
    conn = FakeConn(results_queue=[
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0),
          ("overall", "DALLAS", 500, 300, 150, 50, 90.0, 95.0)],
         ["view", "county_code", "sum_n_parcels", "sum_n_up", "sum_n_down", "sum_n_flat", "sum_mv25", "sum_mv26"]),
        ([("overall", "TRAVIS", 1000, 600, 300, 100, 180.0, 190.0),
          ("overall", "DALLAS", 500, 300, 150, 50, 90.0, 95.0)],
         ["view", "county_code", "n_total", "n_up", "n_down", "n_flat", "total_mv25_b", "total_mv26_b"]),
    ])
    is_consistent, detail = rss.assert_snapshot_breakdown_totals_consistent(conn)
    check("consistency: two counties, same view, each internally consistent -> "
          "is_consistent True (would have been False under the old view-only grouping)",
          is_consistent is True, detail)
    check("consistency: both counties' (view, county_code) pairs present in the checked set",
          set(detail["views_and_counties_checked"]) == {("overall", "TRAVIS"), ("overall", "DALLAS")}, detail)


# ── Part 7: PX-20260831-02 Task 1 — county_code is PER-ROW-DERIVED, not a
# write-path parameter, on all three real INSERTs ───────────────────────────
#
# Real, urgent gap this replaces: the retired PARTITION-2-FIX-1 seam threaded
# ONE externally-supplied county_code into every row of a run -- correct only
# because Travis was the sole county with real data. Now that Dallas has real
# parcels too, that seam would silently attribute every county's rows to
# whichever single county_code the caller happened to pass. These tests
# prove two things the old ones couldn't: (a) build_shadow()/
# refresh_snapshot_summary() no longer ACCEPT a county_code argument at all
# (TypeError if you try), and (b) when _compute_one_view() returns rows for
# TWO different counties in the same view, each row's OWN county_code reaches
# its own INSERT call -- not a single blended or externally-stamped value.

def test_build_shadow_signature_no_longer_accepts_county_code():
    """Source-level proof of the retired write-path parameter: calling
    build_shadow() with a county_code kwarg must raise TypeError, exactly
    mirroring refresh_group_stats.py's own build_shadow() signature."""
    import inspect
    sig = inspect.signature(rss.build_shadow)
    check("build_shadow() signature has no county_code parameter",
          "county_code" not in sig.parameters, sig)

    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn()
        raised = False
        try:
            rss.build_shadow(conn, batch_id=7, county_code="DALLAS", verbose=False)
        except TypeError:
            raised = True
        check("build_shadow(county_code=...) raises TypeError -- the write-path parameter is gone",
              raised)
    finally:
        rss._compute_one_view = orig


def test_refresh_snapshot_summary_signature_no_longer_accepts_county_code():
    import inspect
    sig = inspect.signature(rss.refresh_snapshot_summary)
    check("refresh_snapshot_summary() signature has no county_code parameter",
          "county_code" not in sig.parameters, sig)

    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        conn = FakeConn()
        raised = False
        try:
            rss.refresh_snapshot_summary(conn, dry_run=True, county_code="DALLAS", verbose=False)
        except TypeError:
            raised = True
        check("refresh_snapshot_summary(county_code=...) raises TypeError -- the write-path parameter is gone",
              raised)
    finally:
        rss._compute_one_view = orig


def test_build_shadow_stamps_each_rows_own_county_code_not_a_single_value():
    """THE point of this whole task: _compute_one_view() returns one view's
    worth of TWO counties' data at once (TRAVIS and DALLAS both present in
    the same fake result) -- build_shadow() must write EACH row with ITS OWN
    county_code, not blend them or stamp one county's value onto the other's
    rows. This is the fixture the old externally-stamped-parameter design
    could never have needed (there was only ever one county to stamp)."""
    def _fake_two_county_view(conn, view, verbose=True):
        rows = [
            {"county_code": "TRAVIS", "ptype": "TypeA", "sort_key": "TypeA", "n_parcels": 5, "n_up": 2, "n_down": 2,
             "n_flat": 1, "median_pct": 1.0, "p25_pct": 0.5, "p75_pct": 1.5, "total_mv25_b": 1.0, "total_mv26_b": 1.1},
            {"county_code": "DALLAS", "ptype": "TypeA", "sort_key": "TypeA", "n_parcels": 9, "n_up": 4, "n_down": 4,
             "n_flat": 1, "median_pct": 0.5, "p25_pct": 0.1, "p75_pct": 0.9, "total_mv25_b": 3.0, "total_mv26_b": 3.2},
        ]
        totals_rows = [
            {"county_code": "TRAVIS", "n_total": 5, "n_up": 2, "n_down": 2, "n_flat": 1, "total_mv25_b": 1.0,
             "total_mv26_b": 1.1, "median_pct": 1.0, "new_construction_count": 1, "risk_flagged_count": 0,
             "n_preliminary_2026": 2, "n_total_2026": 5},
            {"county_code": "DALLAS", "n_total": 9, "n_up": 4, "n_down": 4, "n_flat": 1, "total_mv25_b": 3.0,
             "total_mv26_b": 3.2, "median_pct": 0.5, "new_construction_count": 2, "risk_flagged_count": 1,
             "n_preliminary_2026": 9, "n_total_2026": 9},
        ]
        neighborhoods_by_county = {
            "TRAVIS": [{"neighborhood_cd": "NB1", "n_parcels": 10, "median_pct": 2.0}],
            "DALLAS": [{"neighborhood_cd": "NB9", "n_parcels": 20, "median_pct": 0.7}],
        }
        return rows, totals_rows, neighborhoods_by_county

    orig = rss._compute_one_view
    rss._compute_one_view = _fake_two_county_view
    try:
        conn = FakeConn()
        breakdown_n, totals_n, nb_n = rss.build_shadow(conn, batch_id=7, verbose=False)

        n_views = len(_SNAPSHOT_VALID_VIEWS)
        check("build_shadow: 2 breakdown rows per view (one per county) x 11 views",
              breakdown_n == 2 * n_views, breakdown_n)
        check("build_shadow: 2 totals rows per view (one per county) x 11 views",
              totals_n == 2 * n_views, totals_n)
        check("build_shadow: 2 neighborhood rows per view (one per county) x 11 views",
              nb_n == 2 * n_views, nb_n)

        kinds = _sql_kinds(conn.executed)
        breakdown_ccs = [p["county_code"] for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_BREAKDOWN"]
        totals_ccs = [p["county_code"] for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_TOTALS"]
        nb_ccs = [p["county_code"] for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_NEIGHBORHOOD"]

        check("build_shadow: breakdown INSERTs carry BOTH TRAVIS and DALLAS county_codes across the run",
              set(breakdown_ccs) == {"TRAVIS", "DALLAS"}, set(breakdown_ccs))
        check("build_shadow: every breakdown INSERT for view 1 carries its OWN county_code "
              "(TRAVIS row -> 'TRAVIS', DALLAS row -> 'DALLAS', never swapped)",
              breakdown_ccs[0] == "TRAVIS" and breakdown_ccs[1] == "DALLAS", breakdown_ccs[:2])
        check("build_shadow: totals INSERTs carry BOTH counties' own county_code",
              set(totals_ccs) == {"TRAVIS", "DALLAS"}, set(totals_ccs))
        check("build_shadow: neighborhood INSERTs carry BOTH counties' own county_code",
              set(nb_ccs) == {"TRAVIS", "DALLAS"}, set(nb_ccs))

        # Cross-check the actual dollar values travel with the right county --
        # not just that the right LABEL is attached, but that DALLAS's own
        # $3.2B total_mv26_b never lands on a TRAVIS-labeled row or vice versa.
        totals_params_by_cc = {
            p["county_code"]: p for k, (_s, p) in zip(kinds, conn.executed) if k == "INSERT_TOTALS"
        }
        check("build_shadow: TRAVIS totals row keeps its own total_mv26_b (1.1), not DALLAS's (3.2)",
              totals_params_by_cc["TRAVIS"]["total_mv26_b"] == 1.1, totals_params_by_cc["TRAVIS"])
        check("build_shadow: DALLAS totals row keeps its own total_mv26_b (3.2), not TRAVIS's (1.1)",
              totals_params_by_cc["DALLAS"]["total_mv26_b"] == 3.2, totals_params_by_cc["DALLAS"])
    finally:
        rss._compute_one_view = orig


# ── Part 8: PX-20260831-02 Task 1 — post-refresh SnapshotConsistencyError ──
#
# refresh_snapshot_summary() now runs assert_snapshot_breakdown_totals_
# consistent() automatically after every real (non-dry-run) swap and raises
# SnapshotConsistencyError if it finds a mismatch -- no longer gated behind
# a separate --check-consistency flag. FakeConn's results_queue feeds the
# two SELECTs assert_snapshot_breakdown_totals_consistent() issues (sums
# over snapshot_breakdown, then snapshot_totals' own rows) in that order.

def test_refresh_snapshot_summary_raises_when_post_swap_consistency_check_fails():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        # Row shape matches assert_snapshot_breakdown_totals_consistent()'s
        # own SELECTs: (view, county_code, n_total/n_up/n_down/n_flat, mv25, mv26).
        breakdown_sums_row = ("overall", "TRAVIS", 5, 2, 2, 1, 1.0, 1.1)
        totals_row_MISMATCHED = ("overall", "TRAVIS", 999, 2, 2, 1, 1.0, 1.1)  # n_total deliberately wrong
        conn = FakeConn(results_queue=[
            ([breakdown_sums_row], []),
            ([totals_row_MISMATCHED], []),
        ])
        raised = False
        try:
            rss.refresh_snapshot_summary(conn, batch_id=555, dry_run=False, verbose=False)
        except rss.SnapshotConsistencyError:
            raised = True
        check("refresh_snapshot_summary: raises SnapshotConsistencyError when the post-swap "
              "consistency check finds a real mismatch", raised)
        check("refresh_snapshot_summary: the swap itself still completed before the alarm fired "
              "(RENAME statements present in conn.executed)",
              any("RENAME" in k for k in _sql_kinds(conn.executed)), _sql_kinds(conn.executed))
    finally:
        rss._compute_one_view = orig


def test_refresh_snapshot_summary_does_not_raise_when_consistent():
    orig = rss._compute_one_view
    rss._compute_one_view = _fake_compute_one_view
    try:
        breakdown_sums_row = ("overall", "TRAVIS", 5, 2, 2, 1, 1.0, 1.1)
        totals_row_MATCHING = ("overall", "TRAVIS", 5, 2, 2, 1, 1.0, 1.1)
        conn = FakeConn(results_queue=[
            ([breakdown_sums_row], []),
            ([totals_row_MATCHING], []),
        ])
        result = rss.refresh_snapshot_summary(conn, batch_id=555, dry_run=False, verbose=False)
        check("refresh_snapshot_summary: does not raise when the post-swap check is consistent",
              result["dry_run"] is False, result)
        check("refresh_snapshot_summary: result includes the consistency_detail dict",
              "consistency_detail" in result and result["consistency_detail"]["mismatches"] == [], result)
    finally:
        rss._compute_one_view = orig


# ── Part 9: PX-20260831-02 delta (2026-08-31) — SELECT-vs-GROUP-BY-scoped ────
# county_code floor, all 5 builders x all 11 real views
#
# Diego's live EXPLAIN proof (explain_snapshot_summary_county_derivation.py)
# found single_year_mv_sql('overall') FAILing a PLANNER-level check even
# though this builder's own SQL text was never missing county_code -- Part 1
# above already asserts "p.county_code" appears somewhere in each builder's
# string, and separately asserts county_code inside single_year_mv_sql's own
# GROUPING SETS clause, for every view (see lines ~146-150). Root cause was
# in the live-proof script's checker (it only recognized "Group Key:" lines;
# single_year_mv_sql's narrower 2-column GROUPING SETS shape planned as
# HashAggregate, which Postgres renders as "Hash Key:" instead -- fixed in
# that script's _county_code_is_a_group_key()), not in this builder.
#
# This Part tightens Part 1's scattered anywhere-in-string asserts into one
# explicit, SELECT-list-vs-GROUP-BY-clause-SCOPED check per builder, per
# view, so a future regression can't hide by having county_code appear only
# in a WHERE/JOIN clause while actually being absent from the aggregation
# itself -- and proves (next function) that this check would have caught the
# pre-Task-1 externally-stamped shape had that been the real bug here.

def _select_and_group_by_halves(sql):
    """Splits a builder's rendered SQL at the first 'GROUP BY' keyword:
    select_half is everything before it (SELECT/FROM/JOIN/WHERE), group_by_half
    is 'GROUP BY' onward (plain GROUP BY or GROUPING SETS, to end of string).
    All 5 real builders have exactly one GROUP BY; a builder with none at all
    would get group_by_half == '', which the check below correctly fails."""
    idx = sql.find("GROUP BY")
    if idx == -1:
        return sql, ""
    return sql[:idx], sql[idx:]


def test_every_builder_carries_county_code_in_select_and_group_by_for_every_view():
    builders = {
        "breakdown_sql": lambda view: rss.breakdown_sql(view),
        "single_year_mv_sql": lambda view: rss.single_year_mv_sql(view, 2025),
        "part4_agg_sql": lambda view: rss.part4_agg_sql(view),
        "cert_agg_sql": lambda view: rss.cert_agg_sql(view),
        "neighborhoods_sql": lambda view: rss.neighborhoods_sql(view),
    }
    for view in sorted(_SNAPSHOT_VALID_VIEWS):
        for name, build in builders.items():
            sql = build(view)
            select_half, group_by_half = _select_and_group_by_halves(sql)
            check(f"{name}({view}): GROUP BY clause present at all",
                  group_by_half != "", "no 'GROUP BY' found in rendered SQL")
            check(f"{name}({view}): county_code present in SELECT list",
                  "county_code" in select_half, select_half.strip()[:200])
            check(f"{name}({view}): county_code present in GROUP BY / GROUPING SETS clause",
                  "county_code" in group_by_half, group_by_half.strip())


def test_new_select_group_by_check_would_have_failed_pre_task_1_shape():
    """Proof the check above has teeth: reconstructs the shape single_year_mv_sql
    had BEFORE PX-20260831-02 Task 1 (per that task's own before/after diff --
    no p.county_code in SELECT, plain non-county GROUP BY -- county was stamped
    onto every row by the caller after the fact, not derived per row) and
    confirms _select_and_group_by_halves + the same assertions used above would
    correctly flag it on both halves."""
    pre_task_1_single_year_mv_sql = """
        SELECT
            ({ptype_case})                                  AS ptype,
            ROUND(SUM(t.market_value)::NUMERIC / 1e9, 3)     AS total_mv_b
        FROM parcel p
        JOIN parcel_tax_year t ON t.geo_id = p.geo_id AND t.tax_year = {year}
        WHERE t.market_value > 0
        GROUP BY GROUPING SETS ((({ptype_case})), ())
    """
    select_half, group_by_half = _select_and_group_by_halves(pre_task_1_single_year_mv_sql)
    check("pre-Task-1 shape: GROUP BY clause is present (so the failure is specifically "
          "about county_code, not a vacuous no-GROUP-BY pass)", group_by_half != "")
    check("pre-Task-1 shape: new check correctly flags county_code MISSING from SELECT",
          "county_code" not in select_half)
    check("pre-Task-1 shape: new check correctly flags county_code MISSING from GROUP BY",
          "county_code" not in group_by_half)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL REFRESH_SNAPSHOT_SUMMARY FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
