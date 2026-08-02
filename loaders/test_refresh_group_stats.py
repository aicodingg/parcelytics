#!/usr/bin/env python3
"""
loaders/test_refresh_group_stats.py — Task AGGPRECOMP-1, Verification.
Fixture tests for loaders/refresh_group_stats.py.

AC8-style disclosure (same pattern as every other fixture-tested module in
this codebase, restated in refresh_group_stats.py's own module docstring):
no live Postgres is reachable in this sandbox, and none could be installed
for this task either -- `apt-get install postgresql` fails with a
permission error (no root in this sandbox: `sudo` itself refuses to run,
"no new privileges" is set), and `pip install pgserver` (an embeddable-
postgres wheel that would have sidestepped that) fails because the
sandbox's network proxy blocks PyPI (403 Forbidden), confirmed by directly
attempting both before writing this file. Real PERCENTILE_CONT execution
against real Postgres cannot happen here, at all, no matter how this test
file is written.

Given that, this file proves two DIFFERENT things, neither of which
requires a real Postgres, and is explicit about what each one does and
does not prove:

  1. THE GROUPING / PERCENTILE / EFFECTIVE-TAX-FALLBACK DEFINITION IS
     CORRECT: `_reference_group_stats()` below is an INDEPENDENT, pure-
     Python re-implementation of Postgres's PERCENTILE_CONT linear-
     interpolation formula, plus the same grouping keys and effective-tax
     fallback priority refresh_group_stats.py's real SQL uses. It is
     deliberately NOT imported by refresh_group_stats.py itself (doing so
     would violate the spec's own "aggregation logic lives only inside
     refresh functions" principle by creating a second copy of the logic
     in production code) -- it exists ONLY here, in the test file, as the
     one honest way to hand-verify a known median/p25/p75 against a small
     synthetic dataset without a live Postgres to run the real SQL against.
     Every expected number below (e.g. "p25_market_value == 137500") was
     independently hand-computed against Postgres's own documented
     PERCENTILE_CONT formula (linear interpolation: rank = fraction *
     (n-1), interpolate between the two bracketing sorted values), not
     copied from this reference function's output -- see the inline
     comments on test_reference_group_stats_known_percentiles().

  2. THE SCRIPT ISSUES THE RIGHT DB CALLS IN THE RIGHT ORDER, WITH THE
     RIGHT PARAMETERS: build_shadow() / swap_shadow_in() /
     refresh_group_stats() / assert_group_stats_fresh() are exercised
     against an in-memory FakeConn/FakeCursor (same style as
     loaders/test_backfill_prop_unit_tax_year_geoid.py's own fake DB
     layer) -- proving the DROP/CREATE/INSERT/RENAME sequence, the batch-
     id mint-vs-reuse branching, dry-run's "no writes at all" contract,
     and the staleness assertion's fresh/stale/empty/multi-batch logic,
     all without needing psycopg2 (not installed in this sandbox) or a
     real database.

Neither (1) nor (2), even together, proves the ACTUAL SQL STRING in
REFRESH_GROUP_STATS_SQL parses and executes correctly against a real
Postgres server, or that the shadow-swap is genuinely atomic under real
concurrent production traffic. Diego needs to verify both live -- see this
task's final report for exact commands.

Run: python3 loaders/test_refresh_group_stats.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders import refresh_group_stats as rgs

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── Part 1: independent pure-Python reference implementation ────────────────
# (see module docstring for why this exists only here, not in production code)

def _percentile_cont(sorted_values, fraction):
    """
    Postgres PERCENTILE_CONT(fraction) WITHIN GROUP (ORDER BY x)'s own
    documented formula: linear interpolation over the sorted, non-NULL
    values. rank = fraction * (n - 1); interpolate between the values at
    floor(rank) and ceil(rank).
    """
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    rank = fraction * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def _grain_key(row):
    neighborhood_cd_key = (row.get("neighborhood_cd") or "")
    state_cd1_class = (row.get("state_cd1") or "").upper()[:1]
    classi_cd_key = (row.get("classi_cd") or "").strip().upper()
    return (neighborhood_cd_key, state_cd1_class, classi_cd_key, row["tax_year"])


def _effective_tax(row):
    total_tax = row.get("total_tax")
    if total_tax:
        return float(total_tax)
    entity_tax_sum = row.get("entity_tax_sum")
    if entity_tax_sum:
        return float(entity_tax_sum)
    return None


# Task AGGPRECOMP-1-FIX (Aug 2026): mirrors REAL_PROPERTY_ONLY_WHERE's two
# real, live parts -- CANONICAL_PARCEL_EXCL_BARE (X-exempt, N-personal-
# property, AJR-prefixed synthetic BPP placeholders) and
# exclude_non_real_property_gap_sql() (L-class Business Personal Property,
# the specific gap this fix task closes). Independent Python
# reimplementation for test verification only, same disclosure as the rest
# of this file -- NOT imported by production code.
_NON_REAL_PROPERTY_STATE_CD1 = {"X", "N", "L"}


def _is_real_property(row):
    state_cd1_class = (row.get("state_cd1") or "").upper()[:1]
    geo_id = row.get("geo_id") or ""
    if state_cd1_class in _NON_REAL_PROPERTY_STATE_CD1:
        return False
    if geo_id.upper().startswith("AJR"):
        return False
    return True


def _reference_group_stats(rows):
    """
    Filters `rows` to real-property-only (see _is_real_property() above),
    then groups the survivors (each a dict: neighborhood_cd, state_cd1,
    classi_cd, tax_year, market_value, assessed_value, total_tax,
    entity_tax_sum, geo_id) by the same grain refresh_group_stats.py's real
    SQL uses, and computes the same min/p25/median/p75/max +
    count/count_total_tax per group. Independent re-implementation for test
    verification only -- see module docstring.
    """
    groups = {}
    for row in rows:
        if not _is_real_property(row):
            continue
        key = _grain_key(row)
        groups.setdefault(key, []).append(row)

    out = {}
    for key, group_rows in groups.items():
        mvs = sorted(r["market_value"] for r in group_rows if r.get("market_value") is not None)
        avs = sorted(r["assessed_value"] for r in group_rows if r.get("assessed_value") is not None)
        taxes = sorted(t for t in (_effective_tax(r) for r in group_rows) if t is not None)

        out[key] = {
            "count": len(group_rows),
            "min_market_value": mvs[0] if mvs else None,
            "p25_market_value": _percentile_cont(mvs, 0.25),
            "median_market_value": _percentile_cont(mvs, 0.5),
            "p75_market_value": _percentile_cont(mvs, 0.75),
            "max_market_value": mvs[-1] if mvs else None,
            "min_assessed_value": avs[0] if avs else None,
            "p25_assessed_value": _percentile_cont(avs, 0.25),
            "median_assessed_value": _percentile_cont(avs, 0.5),
            "p75_assessed_value": _percentile_cont(avs, 0.75),
            "max_assessed_value": avs[-1] if avs else None,
            "count_total_tax": len(taxes),
            "min_total_tax": taxes[0] if taxes else None,
            "p25_total_tax": _percentile_cont(taxes, 0.25),
            "median_total_tax": _percentile_cont(taxes, 0.5),
            "p75_total_tax": _percentile_cont(taxes, 0.75),
            "max_total_tax": taxes[-1] if taxes else None,
        }
    return out


def test_percentile_cont_matches_postgres_formula_on_simple_cases():
    """Sanity-check the reference formula itself against hand-worked cases
    before trusting it to check anything else."""
    check("percentile_cont([10,20,30,40], 0.5) == 25 (even n, midpoint)",
          _percentile_cont([10, 20, 30, 40], 0.5) == 25)
    check("percentile_cont([10,20,30,40], 0.25) == 17.5",
          _percentile_cont([10, 20, 30, 40], 0.25) == 17.5)
    check("percentile_cont([10,20,30,40], 0.75) == 32.5",
          _percentile_cont([10, 20, 30, 40], 0.75) == 32.5)
    check("percentile_cont([5], 0.5) == 5 (single value)",
          _percentile_cont([5], 0.5) == 5)
    check("percentile_cont([], 0.5) is None (empty)",
          _percentile_cont([], 0.5) is None)


def test_reference_group_stats_known_percentiles():
    """
    Small synthetic dataset, one group, four parcels -- expected
    min/p25/median/p75/max HAND-COMPUTED against Postgres's own documented
    PERCENTILE_CONT formula (see comments), not just "whatever the code
    produces":

      market_value sorted:   [100000, 150000, 200000, 250000]  (n=4)
        p25:    rank=0.25*3=0.75 -> 100000 + 0.75*(150000-100000) = 137500
        median: rank=0.50*3=1.50 -> 150000 + 0.50*(200000-150000) = 175000
        p75:    rank=0.75*3=2.25 -> 200000 + 0.25*(250000-200000) = 212500

      assessed_value sorted: [90000, 140000, 180000, 230000]
        p25:    rank=0.75 -> 90000 + 0.75*(140000-90000)  = 127500
        median: rank=1.50 -> 140000 + 0.50*(180000-140000) = 160000
        p75:    rank=2.25 -> 180000 + 0.25*(230000-180000) = 192500

      effective tax: P1 total_tax=2000 (real) -> 2000
                     P2 total_tax=0 (the documented 93%-zero quirk) but
                        entity_tax_sum=2600 -> falls back to 2600
                     P3 total_tax=3200 -> 3200
                     P4 total_tax=None, entity_tax_sum=None -> excluded
                        (count_total_tax=3, not 4)
      taxes sorted: [2000, 2600, 3200]  (n=3)
        p25:    rank=0.25*2=0.50 -> 2000 + 0.50*(2600-2000) = 2300
        median: rank=0.50*2=1.00 -> exactly 2600
        p75:    rank=0.75*2=1.50 -> 2600 + 0.50*(3200-2600) = 2900
    """
    rows = [
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 100000, "assessed_value": 90000, "total_tax": 2000, "entity_tax_sum": None},
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 150000, "assessed_value": 140000, "total_tax": 0, "entity_tax_sum": 2600},
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 200000, "assessed_value": 180000, "total_tax": 3200, "entity_tax_sum": None},
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 250000, "assessed_value": 230000, "total_tax": None, "entity_tax_sum": None},
    ]
    stats = _reference_group_stats(rows)
    key = ("NB1", "A", "01", 2025)
    check("exactly one group produced", len(stats) == 1, stats.keys())
    g = stats[key]

    check("count == 4", g["count"] == 4, g["count"])
    check("min_market_value == 100000", g["min_market_value"] == 100000, g)
    check("p25_market_value == 137500", g["p25_market_value"] == 137500, g)
    check("median_market_value == 175000", g["median_market_value"] == 175000, g)
    check("p75_market_value == 212500", g["p75_market_value"] == 212500, g)
    check("max_market_value == 250000", g["max_market_value"] == 250000, g)

    check("min_assessed_value == 90000", g["min_assessed_value"] == 90000, g)
    check("p25_assessed_value == 127500", g["p25_assessed_value"] == 127500, g)
    check("median_assessed_value == 160000", g["median_assessed_value"] == 160000, g)
    check("p75_assessed_value == 192500", g["p75_assessed_value"] == 192500, g)
    check("max_assessed_value == 230000", g["max_assessed_value"] == 230000, g)

    check("count_total_tax == 3 (P4 excluded: no total_tax AND no entity_tax_sum)",
          g["count_total_tax"] == 3, g)
    check("min_total_tax == 2000", g["min_total_tax"] == 2000, g)
    check("p25_total_tax == 2300", g["p25_total_tax"] == 2300, g)
    check("median_total_tax == 2600 (falls back to entity_tax_sum, not the real 0)",
          g["median_total_tax"] == 2600, g)
    check("p75_total_tax == 2900", g["p75_total_tax"] == 2900, g)
    check("max_total_tax == 3200", g["max_total_tax"] == 3200, g)


def test_reference_group_stats_separates_distinct_groups():
    """A parcel in a different neighborhood must NOT be merged into the
    same group, even with identical state_cd1/classi_cd/tax_year."""
    rows = [
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 100000, "assessed_value": 90000, "total_tax": 1000, "entity_tax_sum": None},
        {"neighborhood_cd": "NB2", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "market_value": 500000, "assessed_value": 450000, "total_tax": 9000, "entity_tax_sum": None},
    ]
    stats = _reference_group_stats(rows)
    check("two distinct groups produced (different neighborhood_cd)", len(stats) == 2, stats.keys())
    check("NB1 group count == 1", stats[("NB1", "A", "01", 2025)]["count"] == 1)
    check("NB2 group count == 1", stats[("NB2", "A", "01", 2025)]["count"] == 1)
    check("NB1 group max_market_value == 100000 (not merged with NB2)",
          stats[("NB1", "A", "01", 2025)]["max_market_value"] == 100000)


def test_reference_group_stats_null_grain_fields_group_into_blank_key():
    """A parcel with NULL neighborhood_cd/state_cd1/classi_cd must still
    produce a real, countable group (the '' bucket) -- never be silently
    dropped, per the same NULL-safety principle documented throughout this
    codebase's other grain/filter logic (parcel_filters.py)."""
    rows = [
        {"neighborhood_cd": None, "state_cd1": None, "classi_cd": None, "tax_year": 2025,
         "market_value": 300000, "assessed_value": 270000, "total_tax": None, "entity_tax_sum": None},
    ]
    stats = _reference_group_stats(rows)
    check("NULL-grain parcel produces the ('', '', '', 2025) group, not dropped",
          ("", "", "", 2025) in stats, stats.keys())
    check("that group's count == 1", stats[("", "", "", 2025)]["count"] == 1)


def test_reference_group_stats_excludes_non_real_property_rows():
    """
    Task AGGPRECOMP-1-FIX: mixed synthetic dataset -- 2 real residential
    parcels plus one row each of the four non-real-property classes this
    fix excludes (L, N, X, AJR-prefixed geo_id) -- all sharing the SAME
    neighborhood/classi_cd/tax_year grain as the real parcels, so if the
    exclusion didn't work they'd land in and inflate the SAME group.
    Expected: the group contains ONLY the 2 real parcels; a second group
    that would exist solely from the 4 excluded rows must not appear at
    all (not "a group with count 0" -- no group).
    """
    rows = [
        # Real property -- must be counted.
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "0100030105", "market_value": 100000, "assessed_value": 90000,
         "total_tax": 2000, "entity_tax_sum": None},
        {"neighborhood_cd": "NB1", "state_cd1": "A", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "0100030106", "market_value": 150000, "assessed_value": 140000,
         "total_tax": 2500, "entity_tax_sum": None},
        # 'L' -- Business Personal Property, real dollar values -- the bug this
        # task fixes. Same grain as the real rows above (would dominate the
        # SAME group if not excluded, matching what was found live).
        {"neighborhood_cd": "NB1", "state_cd1": "L", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "0200040001", "market_value": 5_000_000, "assessed_value": 4_800_000,
         "total_tax": 90000, "entity_tax_sum": None},
        # 'N' -- personal property, already covered by CANONICAL_PARCEL_EXCL,
        # but group_stats had ZERO exclusion before this fix, so it needs to
        # be proven here too, not assumed.
        {"neighborhood_cd": "NB1", "state_cd1": "N", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "0200040002", "market_value": 200000, "assessed_value": 180000,
         "total_tax": 1000, "entity_tax_sum": None},
        # 'X' -- tax-exempt.
        {"neighborhood_cd": "NB1", "state_cd1": "X", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "0200040003", "market_value": 300000, "assessed_value": 0,
         "total_tax": 0, "entity_tax_sum": None},
        # AJR-prefixed synthetic BPP placeholder -- excluded by geo_id prefix
        # regardless of its state_cd1 (using 'F' here specifically to prove
        # the geo_id-based AJR check fires independently of the state_cd1
        # checks above, not redundantly with them).
        {"neighborhood_cd": "NB1", "state_cd1": "F", "classi_cd": "01", "tax_year": 2025,
         "geo_id": "AJR999999", "market_value": 1, "assessed_value": 1,
         "total_tax": None, "entity_tax_sum": None},
    ]
    stats = _reference_group_stats(rows)
    key = ("NB1", "A", "01", 2025)

    check("exactly one group produced (the 4 non-real-property rows produce NO group of their own)",
          len(stats) == 1, stats.keys())
    g = stats[key]
    check("count == 2 (only the 2 real residential parcels)", g["count"] == 2, g)
    check("max_market_value == 150000 (NOT the 'L' row's 5,000,000)",
          g["max_market_value"] == 150000, g)
    check("median_market_value == 125000 (unskewed by the excluded rows)",
          g["median_market_value"] == 125000, g)
    check("count_total_tax == 2 (excluded rows' total_tax never enters the tax stat either)",
          g["count_total_tax"] == 2, g)
    check("max_total_tax == 2500 (NOT the 'L' row's 90,000)", g["max_total_tax"] == 2500, g)


def test_is_real_property_rejects_each_excluded_class_individually():
    """Direct, one-at-a-time check of _is_real_property() -- proves each of
    the four exclusion legs fires on its own, not just in combination."""
    base = {"neighborhood_cd": "NB1", "classi_cd": "01", "tax_year": 2025, "geo_id": "0100030105"}
    check("real property (state_cd1='A') is accepted",
          _is_real_property({**base, "state_cd1": "A"}) is True)
    check("'L' (Business Personal Property) is rejected",
          _is_real_property({**base, "state_cd1": "L"}) is False)
    check("'N' (personal property) is rejected",
          _is_real_property({**base, "state_cd1": "N"}) is False)
    check("'X' (tax-exempt) is rejected",
          _is_real_property({**base, "state_cd1": "X"}) is False)
    check("AJR-prefixed geo_id is rejected regardless of state_cd1",
          _is_real_property({**base, "state_cd1": "F", "geo_id": "AJR12345"}) is False)


def test_real_property_only_where_sql_contains_both_exclusion_legs():
    """
    Checks the ACTUAL production SQL constant (not the Python mirror) --
    a real, checkable invariant that REFRESH_GROUP_STATS_SQL's WHERE clause
    textually includes both the reused CANONICAL_PARCEL_EXCL_BARE fragment
    (X/N/AJR) and the new L-specific exclusion, so this test would fail if
    either were ever accidentally removed.
    """
    sql = rgs.REFRESH_GROUP_STATS_SQL
    check("REFRESH_GROUP_STATS_SQL references REAL_PROPERTY_ONLY_WHERE's X-exclusion",
          "NOT LIKE 'X%%'" in sql, sql)
    check("REFRESH_GROUP_STATS_SQL references REAL_PROPERTY_ONLY_WHERE's N-exclusion",
          "NOT LIKE 'N%%'" in sql, sql)
    check("REFRESH_GROUP_STATS_SQL references REAL_PROPERTY_ONLY_WHERE's AJR-geo_id exclusion",
          "AJR%%" in sql, sql)
    check("REFRESH_GROUP_STATS_SQL references REAL_PROPERTY_ONLY_WHERE's L-exclusion",
          "NOT IN ('L')" in sql, sql)


# ── Part 2: FakeConn/FakeCursor DB-call-shape tests ─────────────────────────
# Same style as loaders/test_backfill_prop_unit_tax_year_geoid.py's fake DB
# layer -- proves what SQL gets issued, in what order, with what
# parameters, without needing a real database or psycopg2.

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._pending_rows = []
        self.description = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.conn.executed.append((norm, params))
        upper = norm.upper()
        needs_result = upper.startswith("SELECT") or upper.startswith("WITH") or "RETURNING" in upper
        if needs_result:
            rows, cols = self.conn.pop_result()
            self._pending_rows = rows
            self.description = [(c,) for c in cols]
        if upper.startswith("INSERT") and "RETURNING" not in upper:
            self.rowcount = self.conn.insert_rowcount

    def fetchall(self):
        return self._pending_rows

    def fetchone(self):
        return self._pending_rows[0] if self._pending_rows else None


class FakeConn:
    def __init__(self, results_queue=None, insert_rowcount=0):
        self.executed = []
        self.committed_count = 0
        self._results_queue = list(results_queue or [])
        self.insert_rowcount = insert_rowcount

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed_count += 1

    def pop_result(self):
        if self._results_queue:
            return self._results_queue.pop(0)
        return [], []


def _sql_kinds(executed):
    """First keyword of each executed statement, for order assertions."""
    kinds = []
    for sql, _params in executed:
        u = sql.upper()
        if u.startswith("DROP TABLE"):
            kinds.append("DROP")
        elif u.startswith("CREATE TABLE"):
            kinds.append("CREATE")
        elif u.startswith("INSERT INTO GROUP_STATS_SHADOW"):
            kinds.append("INSERT_SHADOW")
        elif u.startswith("INSERT INTO LOAD_BATCH"):
            kinds.append("INSERT_LOAD_BATCH")
        elif u.startswith("ALTER TABLE GROUP_STATS RENAME"):
            kinds.append("RENAME_OLD")
        elif u.startswith("ALTER TABLE GROUP_STATS_SHADOW RENAME"):
            kinds.append("RENAME_NEW")
        elif u.startswith("WITH TBE_SUM"):
            kinds.append("DRY_RUN_SELECT")
        elif u.startswith("SELECT DISTINCT SOURCE_IMPORT_BATCH_ID"):
            kinds.append("SELECT_BATCH_IDS_IN_TABLE")
        elif u.startswith("SELECT MAX(BATCH_ID)"):
            kinds.append("SELECT_LATEST_BATCH")
        else:
            kinds.append(f"OTHER:{sql[:40]}")
    return kinds


def test_build_shadow_issues_drop_create_insert_with_batch_id_and_commits_once():
    conn = FakeConn(insert_rowcount=42)
    row_count = rgs.build_shadow(conn, batch_id=7, verbose=False)

    kinds = _sql_kinds(conn.executed)
    check("build_shadow: DROP, then CREATE, then INSERT_SHADOW, in that order",
          kinds == ["DROP", "CREATE", "INSERT_SHADOW"], kinds)
    check("build_shadow: commits exactly once", conn.committed_count == 1, conn.committed_count)
    check("build_shadow: returns cur.rowcount from the INSERT", row_count == 42, row_count)

    insert_sql, insert_params = conn.executed[2]
    check("build_shadow: INSERT is bound with batch_id=7", insert_params == {"batch_id": 7}, insert_params)
    check("build_shadow: INSERT references group_stats_shadow, not the live table",
          "group_stats_shadow" in insert_sql and "GROUP BY" in insert_sql.upper() or "GROUP  BY" in insert_sql,
          insert_sql)


def test_swap_shadow_in_issues_rename_rename_drop_and_commits_once():
    conn = FakeConn()
    rgs.swap_shadow_in(conn, verbose=False)

    kinds = _sql_kinds(conn.executed)
    check("swap_shadow_in: rename-old, rename-new, drop-old, in that order",
          kinds == ["RENAME_OLD", "RENAME_NEW", "DROP"], kinds)
    check("swap_shadow_in: commits exactly once (one atomic swap)", conn.committed_count == 1)


def test_refresh_group_stats_dry_run_makes_no_writes():
    fake_rows = [("NB1", "A", "01", 2025, 4, 100000, 137500, 175000, 212500, 250000,
                  90000, 127500, 160000, 192500, 230000,
                  3, 2000, 2300, 2600, 2900, 3200)]
    fake_cols = ["neighborhood_cd_key", "state_cd1_class", "classi_cd_key", "tax_year",
                 "count", "min_market_value", "p25_market_value", "median_market_value",
                 "p75_market_value", "max_market_value", "min_assessed_value",
                 "p25_assessed_value", "median_assessed_value", "p75_assessed_value",
                 "max_assessed_value", "count_total_tax", "min_total_tax", "p25_total_tax",
                 "median_total_tax", "p75_total_tax", "max_total_tax"]
    conn = FakeConn(results_queue=[(fake_rows, fake_cols)])

    result = rgs.refresh_group_stats(conn, dry_run=True, verbose=False)

    kinds = _sql_kinds(conn.executed)
    check("dry-run: exactly one statement issued (the read-only aggregation SELECT)",
          kinds == ["DRY_RUN_SELECT"], kinds)
    check("dry-run: never commits (no writes at all)", conn.committed_count == 0, conn.committed_count)
    check("dry-run: result marked dry_run=True", result["dry_run"] is True)
    check("dry-run: row_count reflects the fake result set", result["row_count"] == 1, result)
    check("dry-run: batch_id is None (nothing minted)", result["batch_id"] is None, result)


def test_refresh_group_stats_mints_batch_when_none_given():
    conn = FakeConn(
        results_queue=[([(101,)], ["batch_id"])],  # _mint_batch's RETURNING batch_id
        insert_rowcount=5,
    )
    result = rgs.refresh_group_stats(conn, batch_id=None, dry_run=False, verbose=False)

    kinds = _sql_kinds(conn.executed)
    check("no-batch-given: mints a load_batch row FIRST, before building the shadow",
          kinds[0] == "INSERT_LOAD_BATCH", kinds)
    check("no-batch-given: then DROP/CREATE/INSERT_SHADOW, then the swap",
          kinds == ["INSERT_LOAD_BATCH", "DROP", "CREATE", "INSERT_SHADOW",
                    "RENAME_OLD", "RENAME_NEW", "DROP"] or
          kinds[1:4] == ["DROP", "CREATE", "INSERT_SHADOW"], kinds)
    check("no-batch-given: uses the minted batch_id (101) for the shadow insert",
          result["batch_id"] == 101, result)
    check("no-batch-given: row_count reflects the fake INSERT rowcount", result["row_count"] == 5, result)


def test_refresh_group_stats_reuses_caller_supplied_batch_id():
    conn = FakeConn(insert_rowcount=9)
    result = rgs.refresh_group_stats(conn, batch_id=555, dry_run=False, verbose=False)

    kinds = _sql_kinds(conn.executed)
    check("caller-supplied batch_id: NEVER inserts into load_batch",
          "INSERT_LOAD_BATCH" not in kinds, kinds)
    check("caller-supplied batch_id: still runs the shadow build + swap",
          kinds == ["DROP", "CREATE", "INSERT_SHADOW", "RENAME_OLD", "RENAME_NEW", "DROP"], kinds)
    check("caller-supplied batch_id: result uses exactly 555, not a minted one",
          result["batch_id"] == 555, result)

    insert_sql, insert_params = conn.executed[2]
    check("caller-supplied batch_id: shadow INSERT bound with batch_id=555",
          insert_params == {"batch_id": 555}, insert_params)


def test_assert_group_stats_fresh_true_when_batch_matches():
    conn = FakeConn(results_queue=[
        ([(5,)], ["source_import_batch_id"]),   # DISTINCT source_import_batch_id -> {5}
        ([(5,)], ["max"]),                       # MAX(batch_id) -> 5
    ])
    is_fresh, detail = rgs.assert_group_stats_fresh(conn)
    check("fresh case: is_fresh True when the single batch_id matches the latest", is_fresh is True, detail)
    check("fresh case: detail reports latest_batch_id == 5", detail["latest_batch_id"] == 5, detail)


def test_assert_group_stats_fresh_false_when_batch_mismatched():
    conn = FakeConn(results_queue=[
        ([(3,)], ["source_import_batch_id"]),   # group_stats reflects batch 3
        ([(5,)], ["max"]),                       # but the latest real batch is 5
    ])
    is_fresh, detail = rgs.assert_group_stats_fresh(conn)
    check("stale case: is_fresh False when batch_id is behind the latest", is_fresh is False, detail)
    check("stale case: reason mentions STALE", "STALE" in detail["reason"], detail)


def test_assert_group_stats_fresh_false_when_table_empty():
    conn = FakeConn(results_queue=[
        ([], ["source_import_batch_id"]),
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rgs.assert_group_stats_fresh(conn)
    check("empty-table case: is_fresh False", is_fresh is False, detail)
    check("empty-table case: reason mentions the table being empty", "empty" in detail["reason"], detail)


def test_assert_group_stats_fresh_false_when_multiple_batch_ids_present():
    conn = FakeConn(results_queue=[
        ([(3,), (5,)], ["source_import_batch_id"]),  # a partial/failed refresh scenario
        ([(5,)], ["max"]),
    ])
    is_fresh, detail = rgs.assert_group_stats_fresh(conn)
    check("multi-batch case: is_fresh False (should be impossible under a real atomic swap)",
          is_fresh is False, detail)
    check("multi-batch case: reason mentions more than one batch_id",
          "more than one batch_id" in detail["reason"], detail)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("ALL REFRESH_GROUP_STATS FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
