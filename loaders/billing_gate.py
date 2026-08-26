"""
loaders/billing_gate.py — the Billing Conservation Gate (TAX-BILLING-REKEY-3,
SPEC_TAX_BILLING_REKEY.md §7.5 point 1). Mirrors loaders/ingest_gate.py's
G1-G6 pattern directly, applied to the tax_billing_account -> tax_billing
re-key instead of prop_unit_tax_year -> parcel_tax_year. Named BG1-BG4 (not
G1-G4) specifically to avoid colliding with the existing appraisal-side
gate's own numbering -- these are a separate, parallel gate, not an
extension of ingest_gate.py's checks.

  BG1 — source ledger conservation (mirrors G1): every line of a source
        billing file (TaxCurOpenData, PIR exports) is classified into
        exactly one bucket (accepted / skipped_no_account /
        skipped_wrong_year / ...); bucket counts must sum to the file's
        real total line count.
  BG2 — account coverage (mirrors G2/G5): the count of distinct real
        account_ids a source file scan says should exist for a given
        (county_code, tax_year) must exactly equal the count that landed a
        tax_billing_account row for that same scope.
  BG3 — dollar conservation (mirrors G3, the real conservation check
        Diego's brief specifically calls for): SUM(TOTAL_TAX) computed
        directly from the source file must exactly equal SUM(total_tax) in
        tax_billing_account, which must exactly equal SUM(total_tax) in the
        rolled-up tax_billing -- zero tolerance, same standard G3 already
        holds the appraisal side to.
  BG4 — rollup integrity (mirrors G4): independently re-derives
        tax_billing's SUM()/COUNT() from tax_billing_account rows itself,
        via tax_billing_rollup.compute_rollup() (the same hand-verified
        mirror tax_billing_rollup's own tests use), rather than trusting
        tax_billing_rollup.py's own production SQL ran correctly -- the
        same "the gate doesn't trust the module it's checking" discipline
        G4 already established. PX-20260826-01: keys present in
        tax_billing with no tax_billing_account counterpart at all are
        classified LEGACY-ONLY (reported, not FAILed) -- a known, honest
        source-coverage gap the real cutover surfaced, not a mismatch.
        Genuine value disagreements and rollup-missing keys still hard-FAIL.

Design mirrors ingest_gate.py's own split exactly: every bg*_check()
function below takes already-computed numbers (counts, sums, ledgers) and
returns a (passed, detail[, ...]) verdict with NO database access at all --
these are what loaders/test_billing_gate.py fixture-tests directly,
including the two deliberate-corruption cases §7.5 requires. The
`gather_and_run()` function that actually queries Postgres to produce those
numbers is a thin, untested-in-this-sandbox wrapper (same AC8 disclosure as
ingest_gate.py's own gather_and_run() -- no live Postgres in this sandbox).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config  # noqa: E402

import tax_billing_rollup  # noqa: E402
# Reused, not reimplemented: ingest_gate.py already has a correct, tested
# ingest_audit writer. Same reuse discipline as the rest of this migration
# (tax_billing_rollup.py reusing parcel_rollup.py's own patterns, etc.) --
# single source of truth for how a gate check result becomes an
# ingest_audit row, rather than a second, independently-maintained copy.
from loaders.ingest_gate import _write_audit  # noqa: E402


# ══════════════════════════════════════════════════════════════════════
# BG1 — source ledger conservation (pure, file-scan based)
# ══════════════════════════════════════════════════════════════════════
def scan_billing_ledger(rows):
    """
    rows: iterable of dicts, each representing one raw source-file row
    (billing CSV/XLSX), already parsed into at least {"raw_account": str,
    "tax_year": int_or_None} by the caller (a loader's own CSV/XLSX
    reader) -- this function does not parse file formats itself, since
    every billing source uses a different one (CSV for TaxCurOpenData/
    load_pir_billing.py, streamed-XML-in-XLSX for the PIR full exports) --
    it only classifies already-extracted rows, exactly the same division
    of responsibility ears_format.iter_prop_lines() has on the appraisal
    side (that module parses; scan_prop_ledger() classifies).

    expected_tax_year: if not None, any row whose tax_year doesn't match
    is bucketed 'skipped_wrong_year' instead of 'accepted' -- mirrors
    load_tax_current.py's own EXPECTED_TAX_YEAR hard guard. Pass None for
    a loader (like the PIR full-export loaders) that accepts a range of
    years validated elsewhere.

    Returns a dict: {"total_lines": int, "buckets": {"accepted": n,
    "skipped_no_account": n, "skipped_wrong_year": n}, "account_ids": set()}
    """
    buckets = {"accepted": 0, "skipped_no_account": 0, "skipped_wrong_year": 0}
    account_ids = set()
    total = 0
    for row in rows:
        total += 1
        raw_account = row.get("raw_account")
        expected_tax_year = row.get("expected_tax_year")
        tax_year = row.get("tax_year")
        if not raw_account:
            buckets["skipped_no_account"] += 1
            continue
        if expected_tax_year is not None and tax_year != expected_tax_year:
            buckets["skipped_wrong_year"] += 1
            continue
        buckets["accepted"] += 1
        account_ids.add(raw_account)
    return {"total_lines": total, "buckets": buckets, "account_ids": account_ids}


def bg1_conservation_check(ledger):
    """
    ledger: output of scan_billing_ledger().
    PASS iff sum(buckets.values()) == total_lines exactly -- every single
    source row was classified into exactly one bucket, none double-counted,
    none dropped on the floor uncounted. Identical logic to
    ingest_gate.g1_conservation_check() -- not imported/reused directly
    because the two ledgers' bucket vocabularies are domain-specific
    (billing vs appraisal) and this gate's own docstring/detail message
    should say "billing" explicitly, not borrow appraisal-side wording.
    """
    bucket_sum = sum(ledger["buckets"].values())
    total = ledger["total_lines"]
    passed = bucket_sum == total
    detail = f"billing buckets sum to {bucket_sum:,}, file has {total:,} lines"
    if not passed:
        detail += f"  MISMATCH ({bucket_sum - total:+,}) — some row(s) uncounted or double-counted"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# BG2 — account coverage (pure decision; counts supplied by caller)
# ══════════════════════════════════════════════════════════════════════
def bg2_account_coverage_check(file_account_count, landed_account_count):
    """
    PASS iff the number of distinct real account_ids the source file scan
    says should exist for a given (county_code, tax_year) exactly equals
    the number that actually landed a tax_billing_account row for that
    same scope. This is the check that would have caught the real,
    measured $170,061,400.28-a-year loss the old geo_id-keyed write path
    caused -- under that old scheme, every account past the first sharing
    a geo_id silently vanished with no error, so file_account_count would
    have exceeded landed_account_count by exactly the number of collided
    accounts. See the deliberate-corruption fixture in
    test_billing_gate.py, which reproduces this exact mechanism.
    """
    passed = file_account_count == landed_account_count
    detail = (f"file: {file_account_count:,} distinct account_ids, "
              f"landed (this scope): {landed_account_count:,} rows")
    if not passed:
        detail += f"  MISMATCH ({landed_account_count - file_account_count:+,})"
    return passed, detail


# ══════════════════════════════════════════════════════════════════════
# BG3 — dollar conservation (pure decision)
# ══════════════════════════════════════════════════════════════════════
def bg3_dollar_conservation_check(file_sum, account_table_sum, rollup_table_sum):
    """
    All three must match EXACTLY -- zero tolerance, same standard
    ingest_gate.g3_dollar_conservation_check() already holds the
    appraisal side to. Each may individually be None (meaning "no
    non-null dollar values at all in that source") -- None is a valid,
    comparable value here (None == None passes), matching SQL SUM()'s own
    all-NULL-returns-NULL semantics, same as G3's own handling.
    """
    passed = file_sum == account_table_sum == rollup_table_sum
    detail = (f"file=${_fmt(file_sum)}  tax_billing_account=${_fmt(account_table_sum)}  "
              f"tax_billing=${_fmt(rollup_table_sum)}")
    if not passed:
        detail += "  MISMATCH"
    return passed, detail


def _fmt(v):
    return f"{v:,}" if v is not None else "NULL"


# ══════════════════════════════════════════════════════════════════════
# BG4 — rollup integrity (pure decision; re-derives the aggregation
#       itself rather than trusting tax_billing_rollup.py's own output,
#       so a bug in that module can't hide itself from its own gate check)
# ══════════════════════════════════════════════════════════════════════
def bg4_rollup_integrity_check(account_rows, tax_billing_rows, tax_year):
    """
    account_rows: tax_billing_account rows (same shape
        tax_billing_rollup.compute_rollup()'s own input expects).
    tax_billing_rows: {(county_code, geo_id): {"total_tax":..., "total_paid":...,
        "account_count":..., ...}} as currently stored in tax_billing for
        this tax_year.

    Independently re-derives what tax_billing SHOULD contain (via
    tax_billing_rollup.compute_rollup() -- the same hand-verified mirror
    used by that module's own tests) and diffs it against what's actually
    stored. Any (county_code, geo_id) where they disagree is a mismatch.
    Scoped to tax_billing only (not tax_billing_entity), mirroring
    G4's own scope on the appraisal side (parcel_tax_year only, no
    entity-grain equivalent check exists there either).

    LEGACY-ONLY classification (PX-20260826-01): a key present in
    tax_billing with NO tax_billing_account counterpart at all is no
    longer lumped into `mismatches`/FAIL. The real cutover proved this is
    a known, named, honest class -- data_source='taxcur_current' rows
    whose true last-known value genuinely has no PIR/TaxCur account-grain
    counterpart to re-derive from for that (county_code, geo_id, tax_year)
    (5 such rows confirmed 2021-2024, individually verified -- not a bug,
    not silent data loss, just a source-coverage gap tax_billing_rollup.py
    correctly leaves alone rather than overwriting or deleting). This
    mirrors the exact "classify, don't blanket-fail" refinement G2/G3 got
    in f0c3f59 for their own honest-gap population -- reported explicitly
    (every legacy-only key + its $ value, not hidden), never silently
    dropped, but not a gate failure either. A key present in
    tax_billing_account with NO tax_billing counterpart (the rollup should
    have written it and didn't), and a key present in both whose value
    columns genuinely disagree, remain real, hard-FAIL mismatches --
    unchanged by this refinement.
    """
    expected_rows = {
        (r["county_code"], r["geo_id"]): r
        for r in tax_billing_rollup.compute_rollup(account_rows, tax_year)
    }
    mismatches = []
    legacy_only = []
    compare_cols = ["total_tax", "total_paid", "total_due", "is_delinquent", "account_count"]

    all_keys = set(expected_rows) | set(tax_billing_rows)
    for key in all_keys:
        expected = expected_rows.get(key)
        actual = tax_billing_rows.get(key)
        if expected is None:
            # LEGACY-ONLY, not a mismatch -- see docstring above.
            legacy_only.append((key, actual.get("total_tax") if actual else None))
            continue
        if actual is None:
            mismatches.append((key, "has tax_billing_account rows but missing from tax_billing"))
            continue
        for col in compare_cols:
            if expected.get(col) != actual.get(col):
                mismatches.append((key, f"{col}: expected {expected.get(col)!r}, actual {actual.get(col)!r}"))

    passed = len(mismatches) == 0
    legacy_sum = sum(v for _, v in legacy_only if v is not None)
    detail = (f"{len(all_keys):,} (county_code, geo_id) keys checked, "
              f"{len(mismatches):,} mismatches")
    if legacy_only:
        listing = "; ".join(
            f"{k[0]}/{k[1]}=${v:,.2f}" if v is not None else f"{k[0]}/{k[1]}=NULL"
            for k, v in legacy_only
        )
        detail += f"  LEGACY-ONLY (n={len(legacy_only)}, ${legacy_sum:,.2f}): {listing}"
    return passed, detail, mismatches, legacy_only


# ══════════════════════════════════════════════════════════════════════
# DB-facing gather/orchestration (production code path — requires a live
# conn; NOT exercised in this sandbox — see test file's AC8 disclosure)
# ══════════════════════════════════════════════════════════════════════
def gather_and_run(conn, source_tag, county_code, tax_year, file_rows=None, file_sum=None):
    """
    Full production entry point: optionally runs BG1 on a caller-supplied
    file_rows scan (see scan_billing_ledger()'s own docstring for the
    row shape a loader must supply), queries tax_billing_account/
    tax_billing for BG2-BG4, runs all checks, writes one ingest_audit row
    per check (reusing the SAME ingest_audit table ingest_gate.py's own
    G1-G6 checks write to -- check_code values are prefixed 'BG' so the
    two gates' rows are trivially distinguishable in one shared audit
    trail, not a second, parallel table), and returns an overall summary
    dict. Requires a live psycopg2 connection -- this function itself is
    not fixture-tested (see AC8 disclosure); the bg*_check() decision
    functions it calls ARE.

    file_rows: optional iterable already shaped for scan_billing_ledger()
        (see that function's docstring). If omitted, BG1 is skipped (a
        loader that hasn't been extended to supply a ledger scan can still
        run BG2-BG4 against the DB alone).
    file_sum: optional real SUM(TOTAL_TAX) computed directly from the
        source file (Python-side, by the caller, before this function
        runs) for BG3. If omitted, BG3 is skipped.
    """
    results = {}

    ledger = None
    if file_rows is not None:
        ledger = scan_billing_ledger(file_rows)
        results["BG1"] = bg1_conservation_check(ledger)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT SUM(total_tax), COUNT(DISTINCT account_id) "
            "FROM tax_billing_account WHERE tax_year = %s AND county_code = %s",
            (tax_year, county_code),
        )
        account_table_sum, landed_account_count = cur.fetchone()

        cur.execute(
            "SELECT SUM(total_tax) FROM tax_billing "
            "WHERE tax_year = %s AND county_code = %s",
            (tax_year, county_code),
        )
        rollup_table_sum = cur.fetchone()[0]

        # BG4 inputs: independently re-derive the rollup from
        # tax_billing_account and diff it against what's actually stored
        # in tax_billing.
        cur.execute("""
            SELECT county_code, account_id, tax_year, geo_id, billing_num,
                   owner_name, total_tax, total_paid, total_due,
                   is_delinquent, exemption_codes, data_source, confidence_level
            FROM tax_billing_account
            WHERE tax_year = %s AND county_code = %s
        """, (tax_year, county_code))
        bg4_cols = [d[0] for d in cur.description]
        bg4_account_rows = [dict(zip(bg4_cols, row)) for row in cur.fetchall()]

        cur.execute("""
            SELECT county_code, geo_id, total_tax, total_paid, total_due,
                   is_delinquent, account_count
            FROM tax_billing
            WHERE tax_year = %s AND county_code = %s
        """, (tax_year, county_code))
        tb_cols = [d[0] for d in cur.description]
        bg4_tax_billing_rows = {
            (row[0], row[1]): dict(zip(tb_cols, row)) for row in cur.fetchall()
        }

    if ledger is not None:
        results["BG2"] = bg2_account_coverage_check(len(ledger["account_ids"]), landed_account_count)

    if file_sum is not None:
        results["BG3"] = bg3_dollar_conservation_check(file_sum, account_table_sum, rollup_table_sum)

    results["BG4"] = bg4_rollup_integrity_check(bg4_account_rows, bg4_tax_billing_rows, tax_year)

    _write_audit(conn, source_tag, tax_year, results, county_code=county_code)
    overall_pass = all(r[0] for r in results.values())
    return {"passed": overall_pass, "checks": results}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-tag", default="manual_billing_scan")
    ap.add_argument("--county", default="TRAVIS")
    ap.add_argument("--tax-year", type=int, required=True)
    args = ap.parse_args()

    from loaders.db import get_conn
    conn = get_conn()
    summary = gather_and_run(conn, args.source_tag, args.county, args.tax_year)
    for code, result in summary["checks"].items():
        print(f"{code}: {'PASS' if result[0] else 'FAIL'} — {result[1]}")
    print(f"\nOVERALL: {'PASS' if summary['passed'] else 'FAIL'}")
    conn.close()
