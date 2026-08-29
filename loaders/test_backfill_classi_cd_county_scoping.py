"""
test_backfill_classi_cd_county_scoping.py — regression test for the Stage 4
MISSING_TENANT_SCOPE fix applied to loaders/backfill_classi_cd.py.

Context: verify_index_coverage.py's new Stage 4 check flagged four real,
unscoped SELECT statements in this script -- all read `parcel` (a
composite_pk-migrated, county_code-leading table) with no county_code
predicate at all. The most serious of the four (load_prop_id_lookup())
built a GLOBAL prop_id -> geo_id map across every county; since prop_id is
a county-assigned account number with no guaranteed global uniqueness, a
real collision between two counties' prop_ids would silently overwrite one
county's map entry with another's, and the paired UPDATE would then write
the wrong county's classi_cd onto the wrong geo_id. This script is Travis-
only-era and not scheduled to run today, but Diego's own instruction was
explicit: "it's not running today, but a Travis-era script that would
silently build a cross-county prop_id map on rerun is a loaded gun, not an
acceptable finding-to-triage." Fixed by threading county_code through
every real SELECT in the file, matching the UPDATE statements' own
pre-existing county_code scoping (PX-20260823-02).

This test does not need a live DB -- it inspects the real, shipping source
text directly (same "read the actual file, don't guess" convention as
verify_county_scoping.py's own fixtures), confirming:
  1. All 4 flagged SELECT statements now carry a county_code predicate.
  2. load_prop_id_lookup()'s signature accepts county_code and passes it
     as a real bind parameter (not string-interpolated).
  3. Stage 4 itself, run fresh against the real file, no longer reports
     any MISSING_TENANT_SCOPE finding for this file.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_classi_cd.py")

with open(_FILE, encoding="utf-8") as _f:
    _SOURCE = _f.read()


def test_load_prop_id_lookup_takes_county_code_and_scopes_its_select():
    assert "def load_prop_id_lookup(conn, county_code):" in _SOURCE
    # The real SELECT must carry a county_code predicate AND pass it as a
    # real bind param (not f-string interpolated into the SQL text).
    m = re.search(
        r'cur\.execute\(\s*"SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL AND county_code = %s",\s*\(county_code,\),\s*\)',
        _SOURCE,
    )
    assert m, "load_prop_id_lookup()'s SELECT must filter by county_code via a real bind param"


def test_2026_fill_in_null_pid_lookup_scopes_its_select():
    m = re.search(
        r'"SELECT geo_id, prop_id FROM parcel WHERE classi_cd IS NULL AND prop_id IS NOT NULL "\s*'
        r'"AND county_code = %s",\s*\(county_code,\),',
        _SOURCE,
    )
    assert m, "the 2026 fill-in null_pids lookup must filter by county_code via a real bind param"


def test_verification_distribution_query_scopes_its_select():
    assert "WHERE classi_cd IS NOT NULL AND county_code = %s" in _SOURCE


def test_spot_check_query_scopes_its_select():
    assert re.search(r"WHERE geo_id IN \([^)]*\)\s*\n\s*AND county_code = %s", _SOURCE), (
        "the spot-check SELECT must filter by county_code via a real bind param"
    )


def test_main_passes_county_code_into_load_prop_id_lookup():
    assert "load_prop_id_lookup(conn, county_code)" in _SOURCE


def test_stage4_no_longer_flags_this_file():
    """End-to-end: re-run the real Stage 4 check against this exact file
    and confirm zero MISSING_TENANT_SCOPE findings remain."""
    import verify_index_coverage as vic

    extracted, errors = vic.extract_calls_from_tree([_FILE])
    assert not errors, f"file should parse cleanly: {errors}"
    findings = vic.audit_tenant_scope(extracted, required_tables={"parcel"}, shared_exemptions={})
    gaps = [f for f in findings if f.status == "MISSING_TENANT_SCOPE"]
    assert gaps == [], f"expected zero MISSING_TENANT_SCOPE findings after the fix, got: {gaps}"


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in list(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
    sys.exit(0)
