#!/usr/bin/env python3
"""
loaders/test_gate_source_scoping.py — PX-20260824-04 Task 2 fixture tests.

Exercises ingest_gate.gather_and_run() itself (not just the pure g*_check()
decision functions, already covered by loaders/test_ingest_gate.py) against
a SQL-aware in-memory fake DB, proving the actual data_source-scoping SQL
this brief added does the right thing on real (fixture) PROP.TXT/PROP_ENT.TXT
files plus a synthetic multi-source prop_unit_tax_year/parcel_tax_year state.

Two scenarios, per the brief's own framing:

  Scenario A (the fix's whole point): a multi-source tax_year where TWO
    data_sources (cert_2022, ajr_2022) both have live rows for tax_year
    2022. cert_2022's own file scan matches its own data_source-scoped DB
    rows exactly -- G2/G3 PASS. A naive, UNSCOPED whole-year comparison
    (file_sum vs every source's combined unit_table_sum) is computed
    independently in this test (gather_and_run() itself no longer computes
    that naive version at all -- that's the fix) and shown to disagree,
    proving scoping is load-bearing, not cosmetic.

  Scenario B (the inverse -- the fix must not paper over real gaps): a
    genuinely incomplete load for cert_2022 -- its own PROP_ENT.TXT names 3
    prop_ids/$600, but only 2 of those rows actually landed under
    data_source='cert_2022' in the DB ($300). G2 and G3 must still FAIL
    loudly through the scoped path -- scoping by data_source must not mask
    a real per-source loss.

AC8-style disclosure: psycopg2 is not installed in this sandbox -- a fake
module is injected into sys.modules before import (established convention,
see loaders/test_ingest_gate.py, loaders/test_gate_wiring.py). The FakeCursor
below is SQL-aware (dispatches on distinctive substrings of the exact query
text gather_and_run() issues, per PX-20260824-04's known query shapes) rather
than a fixed-tuple-return stub -- this is deliberately a step up from
test_gate_wiring.py's fully-mocked gather_and_run() (which tests caller
wiring only) because this file's whole point is to prove gather_and_run()'s
OWN SQL scoping logic, which a mocked-out gather_and_run() can't exercise at
all. If gather_and_run()'s query text changes shape in a future edit without
updating the matching substrings here, this file will raise
AssertionError("FakeCursor: unrecognized query: ...") rather than silently
returning wrong data -- a deliberate fail-loud choice over fail-silent.

Run: python3 loaders/test_gate_source_scoping.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

_fake_pg2 = types.ModuleType("psycopg2")
_fake_pg2.extras = types.ModuleType("psycopg2.extras")
sys.modules.setdefault("psycopg2", _fake_pg2)
sys.modules.setdefault("psycopg2.extras", _fake_pg2.extras)

import config  # noqa: E402
from loaders import ingest_gate as gate  # noqa: E402
from loaders.test_ears_format import build_prop_line, build_prop_ent_line  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# ── SQL-aware fake DB ────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, world):
        self.world = world
        self._result = None
        self._desc = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        w = self.world
        params = params or ()

        if "COUNT(DISTINCT prop_id)" in normalized:
            tax_year, county_code, data_source = params
            rows = [r for r in w["prop_unit_tax_year"]
                    if r["tax_year"] == tax_year and r["county_code"] == county_code
                    and r["data_source"] == data_source]
            vals = [r["market_value"] for r in rows if r["market_value"] is not None]
            self._result = [(sum(vals) if vals else None, len({r["prop_id"] for r in rows}))]
            self._desc = None

        elif "LEFT JOIN prop_unit u" in normalized:
            tax_year, county_code = params
            unit_by_key = {(r["county_code"], r["prop_id"]): r for r in w["prop_unit"]}
            rows_out = []
            for r in w["prop_unit_tax_year"]:
                if r["tax_year"] != tax_year or r["county_code"] != county_code:
                    continue
                u = unit_by_key.get((r["county_code"], r["prop_id"]))
                rows_out.append((
                    r["prop_id"], r["geo_id"], u["geo_id"] if u else None, r["tax_year"],
                    r["market_value"], r["assessed_value"], r["taxable_value"],
                    r["hs_cap_loss"], r["land_value"], r["imprv_value"],
                    r["exemption_codes"], r["data_source"],
                ))
            self._result = rows_out
            self._desc = [(c,) for c in (
                "prop_id", "geo_id", "prop_unit_geo_id", "tax_year", "market_value",
                "assessed_value", "taxable_value", "hs_cap_loss", "land_value",
                "imprv_value", "exemption_codes", "data_source",
            )]

        elif "FROM parcel_tax_year" in normalized and "unit_count" in normalized:
            tax_year, county_code = params
            rows_out = [
                (r["geo_id"], r["market_value"], r["assessed_value"], r["taxable_value"],
                 r["hs_cap_loss"], r["land_value"], r["imprv_value"], r["exemption_codes"],
                 r["unit_count"])
                for r in w["parcel_tax_year"]
                if r["tax_year"] == tax_year and r["county_code"] == county_code
            ]
            self._result = rows_out
            self._desc = [(c,) for c in (
                "geo_id", "market_value", "assessed_value", "taxable_value",
                "hs_cap_loss", "land_value", "imprv_value", "exemption_codes", "unit_count",
            )]

        elif "FROM parcel_tax_year" in normalized:
            tax_year, county_code = params
            vals = [r["market_value"] for r in w["parcel_tax_year"]
                    if r["tax_year"] == tax_year and r["county_code"] == county_code
                    and r["market_value"] is not None]
            self._result = [(sum(vals) if vals else None,)]
            self._desc = None

        elif "FROM prop_unit_tax_year" in normalized:
            tax_year, county_code = params
            vals = [r["market_value"] for r in w["prop_unit_tax_year"]
                    if r["tax_year"] == tax_year and r["county_code"] == county_code
                    and r["market_value"] is not None]
            self._result = [(sum(vals) if vals else None,)]
            self._desc = None

        else:
            raise AssertionError(f"FakeCursor: unrecognized query: {normalized[:160]}")

    def executemany(self, sql, rows):
        self.world.setdefault("_audit_rows", []).extend(rows)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

    @property
    def description(self):
        return self._desc


class FakeConn:
    def __init__(self, world):
        self.world = world

    def cursor(self):
        return FakeCursor(self.world)

    def commit(self):
        pass

    def close(self):
        pass


def _unit_row(county_code, prop_id, tax_year, geo_id, market_value, data_source):
    return {
        "county_code": county_code, "prop_id": prop_id, "tax_year": tax_year,
        "geo_id": geo_id, "market_value": market_value, "assessed_value": 0,
        "taxable_value": 0, "hs_cap_loss": 0, "land_value": 0, "imprv_value": 0,
        "exemption_codes": None, "data_source": data_source,
    }


def _parcel_row(county_code, tax_year, geo_id, market_value):
    return {
        "county_code": county_code, "tax_year": tax_year, "geo_id": geo_id,
        "market_value": market_value, "assessed_value": 0, "taxable_value": 0,
        "hs_cap_loss": 0, "land_value": 0, "imprv_value": 0, "exemption_codes": None,
        "unit_count": 1,
    }


def _write_fixture_files(tmpdir, prop_specs, ent_specs):
    """prop_specs: [(prop_id, geo_id), ...]. ent_specs: [(prop_id, market), ...]."""
    prop_path = os.path.join(tmpdir, "PROP.TXT")
    ent_path = os.path.join(tmpdir, "PROP_ENT.TXT")
    with open(prop_path, "w", encoding="latin-1") as f:
        for prop_id, geo_id in prop_specs:
            f.write(build_prop_line(prop_id=prop_id, geo_id=geo_id, prop_type_cd="R") + "\n")
    with open(ent_path, "w", encoding="latin-1") as f:
        for prop_id, market in ent_specs:
            f.write(build_prop_ent_line(prop_id=prop_id, year=2022, entity_cd="TCO",
                                         assessed=market, taxable=market, market=market) + "\n")
    return prop_path, ent_path


# ── Scenario A: multi-source year, per-source PASS, naive whole-year FAIL ──
def test_scenario_a_multisource_per_source_pass_naive_would_fail():
    tmpdir = tempfile.mkdtemp(prefix="px_gate_scoping_test_")
    try:
        # cert_2022: 3 prop_ids, $100+$200+$300 = $600, all geo-resolved.
        cert_prop_specs = [(1001, "G001"), (1002, "G002"), (1003, "G003")]
        cert_ent_specs = [(1001, 100), (1002, 200), (1003, 300)]
        prop_path, ent_path = _write_fixture_files(tmpdir, cert_prop_specs, cert_ent_specs)

        # World state: cert_2022's own rows ALREADY landed correctly, PLUS
        # an independent ajr_2022 source that also has live rows for the
        # same tax_year -- the multi-source scenario G2/G3 scoping exists
        # to handle. ajr_2022 contributes $400+$500 = $900 across 2 more
        # geo_ids this run's own file (cert_2022's) knows nothing about.
        world = {
            "prop_unit_tax_year": [
                _unit_row("TRAVIS", 1001, 2022, "G001", 100, "cert_2022"),
                _unit_row("TRAVIS", 1002, 2022, "G002", 200, "cert_2022"),
                _unit_row("TRAVIS", 1003, 2022, "G003", 300, "cert_2022"),
                _unit_row("TRAVIS", 2001, 2022, "G004", 400, "ajr_2022"),
                _unit_row("TRAVIS", 2002, 2022, "G005", 500, "ajr_2022"),
            ],
            "prop_unit": [
                {"county_code": "TRAVIS", "prop_id": pid, "geo_id": geo}
                for pid, geo in [(1001, "G001"), (1002, "G002"), (1003, "G003"),
                                  (2001, "G004"), (2002, "G005")]
            ],
            "parcel_tax_year": [
                _parcel_row("TRAVIS", 2022, "G001", 100),
                _parcel_row("TRAVIS", 2022, "G002", 200),
                _parcel_row("TRAVIS", 2022, "G003", 300),
                _parcel_row("TRAVIS", 2022, "G004", 400),
                _parcel_row("TRAVIS", 2022, "G005", 500),
            ],
        }

        # ── The naive-comparison arithmetic gather_and_run() DELIBERATELY
        #    no longer performs -- proven explicitly here, independent of
        #    gather_and_run(), to demonstrate why the scoping fix matters.
        cert_file_sum = 600
        whole_year_unit_sum_naive = sum(
            r["market_value"] for r in world["prop_unit_tax_year"]
            if r["tax_year"] == 2022 and r["county_code"] == "TRAVIS"
        )
        check(
            "naive (unscoped) whole-year comparison would have FAILED: "
            "cert_2022's file_sum ($600) != combined whole-year unit sum ($1,500)",
            cert_file_sum != whole_year_unit_sum_naive,
            f"file_sum={cert_file_sum} whole_year_unit_sum_naive={whole_year_unit_sum_naive}",
        )

        conn = FakeConn(world)
        summary = gate.gather_and_run(
            conn, source_tag="cert_2022", tax_year=2022,
            prop_path=prop_path, prop_ent_path=ent_path,
            county_code="TRAVIS", data_source="cert_2022",
        )

        g2_passed, g2_detail = summary["checks"]["G2"]
        check("Scenario A: G2 PASSES when correctly scoped to cert_2022's own data_source",
              g2_passed, g2_detail)

        g3_passed, g3_detail = summary["checks"]["G3"]
        check("Scenario A: G3 PASSES when correctly scoped to cert_2022's own data_source "
              "($600 file == $600 landed, NOT compared against the $1,500 whole-year total)",
              g3_passed, g3_detail)

        g3r_passed, g3r_detail = summary["checks"]["G3_rollup"]
        check("Scenario A: G3_rollup (the deliberately whole-year check) PASSES too -- "
              "whole_year_unit_sum ($1,500) == account_table_sum ($1,500), zero residual, "
              "zero expected (no no-geo rows in this fixture)",
              g3r_passed, g3r_detail)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Scenario B: the inverse -- a genuinely incomplete cert_2022 load must
#    still fail loudly through the SAME scoped path, not be masked by it. ──
def test_scenario_b_incomplete_load_still_fails_loudly_when_scoped():
    tmpdir = tempfile.mkdtemp(prefix="px_gate_scoping_test_")
    try:
        # cert_2022's OWN file still names all 3 prop_ids / $600 -- the file
        # itself is fine. The bug is purely on the DB-landed side: only 2 of
        # those 3 rows actually made it into prop_unit_tax_year under
        # data_source='cert_2022' (prop_id 1003 / $300 never landed).
        cert_prop_specs = [(1001, "G001"), (1002, "G002"), (1003, "G003")]
        cert_ent_specs = [(1001, 100), (1002, 200), (1003, 300)]
        prop_path, ent_path = _write_fixture_files(tmpdir, cert_prop_specs, cert_ent_specs)

        world = {
            "prop_unit_tax_year": [
                _unit_row("TRAVIS", 1001, 2022, "G001", 100, "cert_2022"),
                _unit_row("TRAVIS", 1002, 2022, "G002", 200, "cert_2022"),
                # prop_id 1003 / $300 is MISSING -- the genuinely incomplete load.
                _unit_row("TRAVIS", 2001, 2022, "G004", 400, "ajr_2022"),
                _unit_row("TRAVIS", 2002, 2022, "G005", 500, "ajr_2022"),
            ],
            "prop_unit": [
                {"county_code": "TRAVIS", "prop_id": pid, "geo_id": geo}
                for pid, geo in [(1001, "G001"), (1002, "G002"),
                                  (2001, "G004"), (2002, "G005")]
            ],
            "parcel_tax_year": [
                _parcel_row("TRAVIS", 2022, "G001", 100),
                _parcel_row("TRAVIS", 2022, "G002", 200),
                _parcel_row("TRAVIS", 2022, "G004", 400),
                _parcel_row("TRAVIS", 2022, "G005", 500),
            ],
        }

        conn = FakeConn(world)
        summary = gate.gather_and_run(
            conn, source_tag="cert_2022", tax_year=2022,
            prop_path=prop_path, prop_ent_path=ent_path,
            county_code="TRAVIS", data_source="cert_2022",
        )

        g2_passed, g2_detail = summary["checks"]["G2"]
        check("Scenario B: G2 still FAILS loudly through the scoped path "
              "(3 file prop_ids, only 2 landed under cert_2022 -- scoping "
              "doesn't hide a real per-source loss)",
              not g2_passed, g2_detail)

        g3_passed, g3_detail = summary["checks"]["G3"]
        check("Scenario B: G3 still FAILS loudly through the scoped path "
              "($600 file != $300 landed under cert_2022)",
              not g3_passed, g3_detail)

        check("Scenario B: overall gate result is FAIL (not silently masked)",
              summary["passed"] is False, summary)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"SOME TESTS FAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("ALL GATE SOURCE-SCOPING FIXTURE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
