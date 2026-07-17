#!/usr/bin/env python3
"""
quarantine_contamination.py — move contaminated tax_billing rows (July 14,
2026 TAXYEAR-scoping incident) into a reversible, auditable quarantine
table instead of deleting them outright.

Cowork brief "Homestead-Cap Data Integrity: Full Fix Set", Issue 4, July
2026. Context: the July 14, 2026 incident (see the incident report from
that date) let rows with a bad/blank TAXYEAR field slip into tax_billing
tagged with the current year's data_source. The hard TAXYEAR reject added
to load_tax_current.py at the time stopped NEW contamination; this script
is the cleanup pass for what already landed: any tax_billing row with
tax_year < 2021 OR tax_year = 9999 (the sentinel value confirmed used by
that incident) is not a real tax year for this dataset (parcel_tax_year's
earliest real year is 2021; TCAD started digital records well before that,
but this app has never carried billing data pre-2021) and was never
verified as fabricated the way the 56 rows deleted during the original
incident response were -- those were deleted only after being individually
confirmed absent from the true PIR source. Nothing pre-2021/9999 has met
that bar, so this script quarantines (moves to tax_billing_quarantine +
deletes from the live table, in one transaction, reversible) rather than
deleting outright.

THE 436-PARCEL CARVE-OUT (must run --investigate first, before --run):
Diego's brief explicitly requires investigating BEFORE quarantining any
parcel whose ONLY tax_billing history is contaminated rows -- quarantining
those unconditionally would leave real parcels with zero billing history
showing on the property page, a new instance of the Issue 5 gap. This
script's --investigate mode finds that set and reports, for each one,
whether it has a real geo_id with real 2021+ parcel_tax_year appraisal data
(i.e., a normal parcel that just happens to have only bad billing rows) or
looks like a genuine edge case (no parcel_tax_year row at all -- e.g. a
retired/subdivided account). --run then EXCLUDES this whole set from
quarantine by default (--include-orphans overrides, only after Diego has
reviewed the --investigate output and decided).

Usage:
    python3 loaders/quarantine_contamination.py --investigate
    python3 loaders/quarantine_contamination.py --run --dry-run
    python3 loaders/quarantine_contamination.py --run
    python3 loaders/quarantine_contamination.py --emit-class-a
    python3 loaders/quarantine_contamination.py --restore-class-a --dry-run
    python3 loaders/quarantine_contamination.py --restore-class-a
    python3 loaders/quarantine_contamination.py --verify

NOT YET RE-RUN AGAINST THE LIVE DATABASE SINCE THIS ROUND'S CHANGES. This
sandbox has no live DB access -- py_compile clean, logic reviewed (including
tracing the quarantine-state-invariance bug in _INVESTIGATE_SQL by hand),
but the actual row counts and the restore itself need Diego's live run.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn

INCIDENT_REF = "2026-07-14-taxyear"

# ── Tracked Class A exceptions (Cowork brief "Tighten the Contamination
# Assertion, Begin Class A Resolution", July 2026) ──────────────────────────
# The 12 Class A orphan parcels (real 2021-2026 appraisal data on file,
# ONLY their tax_billing rows are contaminated) are excluded from --run's
# quarantine by default, which means their contaminated rows are still
# sitting in tax_billing right now. verify_year_bounds()'s assertion used to
# be a bare "count = 0" that would therefore show a nonzero "known-expected"
# number (currently 325 rows across these parcels, soon just however many
# rows the remaining unresolved Class A parcels carry) -- silently depending
# on whoever reads the output remembering that specific number is fine. That
# is exactly the kind of assertion this project's own incidents have shown
# is dangerous: a REAL 13th contamination source appearing later blends
# invisibly into an already-nonzero "expected" total instead of failing loud.
#
# Fixed below by naming the exception explicitly and asserting to exactly 0
# against everything else. This list is a TRACKED, NAMED, TEMPORARY
# allowlist for parcels under active investigation -- not a permanent
# exemption. Each entry must carry a real resolution-status comment; an
# entry with no live investigation behind it does not belong here.
#
# geo_id AJR385736 -- investigated per this brief's Part 2, directly against
# the real 2021 AJR (20210925_000416_PTD.csv) and 2025 Certified (PROP.TXT)
# source files, not guessed. NOT a Travis County-owned exempt parcel,
# despite the brief's premise -- confirmed via the real 2025 Certified
# PROP.TXT record (prop_id 385736): owner is "ATMOS ENERGY/MID-TEX
# DISTRIBUTION" (a private, investor-owned gas utility), not Travis County
# government. "*TRAVIS CTY" is TCAD's generic situs-street placeholder for
# utility distribution-system accounts with no specific street address
# (confirmed: the same literal "*TRAVIS CTY" string appears as the situs
# field on many other, unrelated utility rows in the same file) -- it is a
# LOCATION marker, not an OWNER marker, and was misread as one in the
# brief's own hypothesis. state_cd1='J2' = "UTILITY (GAS) / GAS DISTRIBUTION
# SYSTEMS" per TCAD's own STATE_CD.TXT. Real market value ~$100,676,634 (2025
# Certified) / $48,490,384 (2021 AJR) -- a substantial, ordinarily-taxable
# personal-property utility account, not a $0/exempt one. Gas distribution
# infrastructure owned by an investor-owned utility does not qualify for
# Tax Code SS11.11's political-subdivision exemption; nothing found in the
# real source data supports an exemption for this specific account.
# CONCLUSION: this is a genuine billing-coverage gap (same class as the
# already-documented L1/personal-property portion of the 54,115-row gap in
# KNOWN_LIMITATIONS.md), not a "correctly zero" exempt case -- it stays
# tracked here pending real billing re-attribution, and gets the SAME
# honest Issue-5 "Not available from the county" disclosure every other
# billing-gap parcel gets (no exemption-specific copy needed or applied).
#
# The remaining 11 Class A geo_ids (per Diego's live --emit-class-a run,
# July 2026 -- see the Cowork brief "Restore the 12 Class A Parcels from
# Quarantine, and Complete the Tracked Exceptions List"): confirmed as
# Class A (real 2021-2026 appraisal data on file, only billing is
# contaminated), but NOT yet individually investigated the way AJR385736
# was. "pending individual investigation" is a placeholder status, not a
# resolution -- each of these needs its own real-source check (does it have
# an exemption, a genuine billing gap, something else?) before it can be
# resolved the way AJR385736 was, per this brief's explicit instruction not
# to guess at a reason. Restored to tax_billing from tax_billing_quarantine
# this same round (see restore_class_a() below) after an earlier
# `--run --include-orphans` execution incorrectly swept all 436 orphans
# (Class A and B together) into quarantine instead of just the 424 Class B
# ones -- these 12 needed to come back out first, since quarantining a
# Class A parcel before its own resolution is decided is exactly the
# premature-quarantine outcome Issue 4's original --investigate-first
# design was built to prevent.
CLASS_A_TRACKED_EXCEPTIONS = [
    "AJR385736",   # Atmos Energy gas distribution system -- see comment above. Confirmed NOT exempt.
    "0164800619",  # pending individual investigation
    "0202500315",  # pending individual investigation
    "0210110712",  # pending individual investigation
    "0215050312",  # pending individual investigation
    "0215050419",  # pending individual investigation
    "0242600206",  # pending individual investigation
    "0242700249",  # pending individual investigation
    "0244071202",  # pending individual investigation
    "0336100301",  # pending individual investigation
    "0339110404",  # pending individual investigation
    "0339110406",  # pending individual investigation
]

_CREATE_QUARANTINE_SQL = """
CREATE TABLE IF NOT EXISTS tax_billing_quarantine (
    geo_id              VARCHAR(20)  NOT NULL,
    tax_year            SMALLINT     NOT NULL,
    billing_num         VARCHAR(30),
    owner_name          TEXT,
    total_tax           NUMERIC(14,2),
    total_paid          NUMERIC(14,2),
    total_due           NUMERIC(14,2),
    is_delinquent       BOOLEAN      DEFAULT FALSE,
    first_delinquent_yr SMALLINT,
    cause_number        VARCHAR(50),
    exemption_codes     VARCHAR(50),
    data_source         VARCHAR(32),
    confidence_level    VARCHAR(16),
    quarantined_at      TIMESTAMP    NOT NULL DEFAULT now(),
    incident_ref        VARCHAR(64)  NOT NULL,
    reason              TEXT         NOT NULL,
    PRIMARY KEY (geo_id, tax_year)
);
"""

# Contamination definition -- reused verbatim from the July 14 incident
# response's own diagnostic (never redefined ad hoc): tax_year < 2021 (no
# loader has ever written a real year before 2021) OR tax_year = 9999 (the
# confirmed sentinel this specific incident produced).
_CONTAMINATION_WHERE = "(tax_year < 2021 OR tax_year = 9999)"

# Quarantine-state-invariance fix (Cowork brief "Restore the 12 Class A
# Parcels from Quarantine, and Complete the Tracked Exceptions List", July
# 2026). ORIGINAL BUG, caught during this brief rather than discovered live:
# contaminated_geo below used to read ONLY from tax_billing. That's correct
# the FIRST time this runs (before anything's been quarantined), but breaks
# the moment a parcel's contaminated rows are actually moved to
# tax_billing_quarantine -- at that point the parcel has ZERO rows left in
# tax_billing (neither contaminated nor clean, since an "orphan" by
# definition never had a clean row to begin with), so it silently vanishes
# from BOTH contaminated_geo and the final orphans set. Concretely: the
# erroneous `--run --include-orphans` execution that swept all 436 orphans
# into quarantine (this brief's own trigger) would have made a RE-RUN of
# --investigate / --emit-class-a report 0 orphans, not 436 or even 12 --
# not because the contamination was resolved, but because the query's own
# data source moved out from under it. Fixed by treating a row in
# tax_billing_quarantine as equally valid evidence a geo_id is
# contaminated -- every row that ever lands there is contaminated by
# construction (that's the only thing this script ever inserts into it), so
# no need to re-apply _CONTAMINATION_WHERE to it.
_INVESTIGATE_SQL = f"""
-- Parcels whose ONLY tax_billing rows are contaminated -- the carve-out
-- that must be investigated (not quarantined) until Diego reviews. Checks
-- BOTH tax_billing and tax_billing_quarantine for contamination evidence
-- (see comment above) so this query's result doesn't depend on whether a
-- given parcel's contaminated rows currently live in one table or the
-- other.
WITH contaminated_geo AS (
    SELECT geo_id FROM tax_billing WHERE {_CONTAMINATION_WHERE}
    UNION
    SELECT geo_id FROM tax_billing_quarantine
),
clean_geo AS (
    SELECT DISTINCT geo_id FROM tax_billing WHERE NOT {_CONTAMINATION_WHERE}
),
orphans AS (
    SELECT geo_id FROM contaminated_geo
    EXCEPT
    SELECT geo_id FROM clean_geo
)
SELECT
    o.geo_id,
    p.situs_address,
    p.state_cd1,
    (SELECT count(*) FROM parcel_tax_year pty
        WHERE pty.geo_id = o.geo_id AND pty.tax_year BETWEEN 2021 AND 2026) AS real_appraisal_years,
    (SELECT string_agg(all_years.tax_year::text, ',' ORDER BY all_years.tax_year)
        FROM (
            SELECT tax_year FROM tax_billing WHERE geo_id = o.geo_id
            UNION
            SELECT tax_year FROM tax_billing_quarantine WHERE geo_id = o.geo_id
        ) all_years
    ) AS contaminated_tax_years
FROM orphans o
LEFT JOIN parcel p ON p.geo_id = o.geo_id
ORDER BY real_appraisal_years DESC, o.geo_id;
"""


def investigate(conn):
    """
    Report the 436-parcel (or whatever the live count actually is) carve-out
    BEFORE any quarantine runs, per Diego's explicit brief instruction.
    Classifies each into:
      A. Real parcel, real 2021-2026 appraisal data on file, only billing is
         contaminated -- otherwise-normal parcel that would show zero
         billing history if quarantined unconditionally. Needs its own
         resolution plan (e.g. re-pull real billing from the PIR/portal
         source), not silent quarantine.
      B. No real appraisal data on file either (0 parcel_tax_year rows in
         2021-2026) -- likely a genuine edge case (retired/subdivided
         account, or a geo_id that only ever existed via this incident's
         own fabricated data). Safer to quarantine -- there's no real
         parcel page depending on this geo_id's billing history anyway.
    """
    with conn.cursor() as cur:
        cur.execute(_INVESTIGATE_SQL)
        rows = cur.fetchall()

    class_a, class_b = [], []
    for geo_id, situs, state_cd1, real_years, tax_years in rows:
        (class_a if real_years and real_years > 0 else class_b).append(
            (geo_id, situs, state_cd1, real_years, tax_years)
        )

    print(f"\n{'='*70}")
    print("  436-PARCEL CARVE-OUT INVESTIGATION (Issue 4)")
    print(f"{'='*70}")
    print(f"  Total orphan parcels (only contaminated billing rows): {len(rows):,}")
    print(f"  Class A -- real 2021-2026 appraisal data on file, only billing")
    print(f"             is contaminated (otherwise-normal parcel): {len(class_a):,}")
    print(f"  Class B -- no real appraisal data on file either (likely a")
    print(f"             genuine edge case, e.g. retired/subdivided account): {len(class_b):,}")
    print()
    if class_a:
        print("  Class A sample (first 20) -- DO NOT quarantine without a real-billing")
        print("  resolution plan; these parcels would show zero billing history otherwise:")
        for geo_id, situs, state_cd1, real_years, tax_years in class_a[:20]:
            print(f"    {geo_id}  {situs or '(no address)':<40}  state_cd1={state_cd1 or '?':<3}  "
                  f"real_appraisal_years={real_years}  contaminated_years=[{tax_years}]")
    if class_b:
        print("\n  Class B sample (first 20) -- safer to quarantine, no real parcel page")
        print("  depends on this geo_id's billing history:")
        for geo_id, situs, state_cd1, real_years, tax_years in class_b[:20]:
            print(f"    {geo_id}  {situs or '(no address)':<40}  state_cd1={state_cd1 or '?':<3}  "
                  f"contaminated_years=[{tax_years}]")
    print(f"\n{'='*70}")
    print("  RECOMMENDATION: --run (below) excludes ALL orphan parcels (both")
    print("  classes) from quarantine by default. Re-run this investigation live,")
    print("  review the Class A list specifically, and decide a resolution (re-pull")
    print("  real billing, or accept the gap and disclose it per Issue 5) before")
    print("  deciding whether Class B is safe to fold into --run --include-orphans.")
    print(f"{'='*70}\n")
    return {geo_id for geo_id, *_ in rows}


def run(conn, dry_run=True, include_orphans=False):
    with conn.cursor() as cur:
        cur.execute(_CREATE_QUARANTINE_SQL)
    conn.commit()

    orphan_geo_ids = investigate(conn) if not include_orphans else set()

    with conn.cursor() as cur:
        # Count first, for the dry-run report and the real run's sanity check.
        exclude_clause = ""
        params = []
        if orphan_geo_ids:
            exclude_clause = "AND geo_id != ALL(%s)"
            params.append(list(orphan_geo_ids))

        cur.execute(
            f"SELECT count(*) FROM tax_billing WHERE {_CONTAMINATION_WHERE} {exclude_clause}",
            params,
        )
        n_to_quarantine = cur.fetchone()[0]

    print(f"  Rows eligible for quarantine (contaminated, NOT in the "
          f"{'orphan carve-out' if orphan_geo_ids else 'excluded set (none)'}): {n_to_quarantine:,}")

    if dry_run:
        print("  DRY RUN -- nothing written. Re-run with --run (no --dry-run) to execute.")
        return n_to_quarantine

    reason = (
        "July 14, 2026 TAXYEAR-scoping incident: tax_year < 2021 or = 9999, "
        "never confirmed fabricated against the true PIR source (unlike the "
        "56 rows deleted during the original incident response) -- quarantined "
        "rather than deleted for reversibility/auditability."
    )
    insert_sql = f"""
        WITH moved AS (
            DELETE FROM tax_billing
            WHERE {_CONTAMINATION_WHERE} {exclude_clause}
            RETURNING geo_id, tax_year, billing_num, owner_name, total_tax,
                      total_paid, total_due, is_delinquent, first_delinquent_yr,
                      cause_number, exemption_codes, data_source, confidence_level
        )
        INSERT INTO tax_billing_quarantine
            (geo_id, tax_year, billing_num, owner_name, total_tax, total_paid,
             total_due, is_delinquent, first_delinquent_yr, cause_number,
             exemption_codes, data_source, confidence_level, incident_ref, reason)
        SELECT geo_id, tax_year, billing_num, owner_name, total_tax, total_paid,
               total_due, is_delinquent, first_delinquent_yr, cause_number,
               exemption_codes, data_source, confidence_level, %s, %s
        FROM moved
    """
    with conn.cursor() as cur:
        cur.execute(insert_sql, params + [INCIDENT_REF, reason])
        n_moved = cur.rowcount
    conn.commit()
    print(f"  → {n_moved:,} rows moved to tax_billing_quarantine and removed from tax_billing.")
    return n_moved


def restore_class_a(conn, geo_ids=None, dry_run=True):
    """
    Reverse an over-broad quarantine for a specific, named set of geo_ids --
    built for the Cowork brief "Restore the 12 Class A Parcels from
    Quarantine, and Complete the Tracked Exceptions List", July 2026.
    Context: an earlier `--run --include-orphans` execution swept ALL 436
    orphans (Class A and B together) into tax_billing_quarantine, instead of
    just the 424 Class B ones -- --include-orphans's whole purpose is to
    skip the orphan exclusion entirely, so it never distinguished Class A
    from Class B in the first place; that distinction only exists in
    investigate()'s REPORTING, not in --run's actual exclusion logic. The 12
    Class A parcels need to come back out: quarantining a Class A parcel
    before its own individual resolution is decided is exactly the
    premature-quarantine outcome Issue 4's original --investigate-first
    design was built to prevent.

    Reuses run()'s own transaction pattern in reverse: DELETE ... RETURNING
    ... INSERT ... SELECT ... FROM deleted, one transaction, same
    before/after row-count sanity check. Restores the row to tax_billing
    exactly as it was quarantined -- same 13 real tax_billing columns,
    dropping only the 3 quarantine-specific bookkeeping columns
    (quarantined_at, incident_ref, reason).

    geo_ids defaults to CLASS_A_TRACKED_EXCEPTIONS (the full named list) --
    pass an explicit subset only if you deliberately want to restore fewer
    than all of them (e.g. restoring in batches). Every geo_id passed here
    should already be a TRACKED exception (added to that list with its own
    resolution-status note) -- restoring an untracked geo_id would silently
    reopen a hole verify_year_bounds() is specifically designed to catch.
    """
    if geo_ids is None:
        geo_ids = list(CLASS_A_TRACKED_EXCEPTIONS)
    if not geo_ids:
        print("  No geo_ids given -- nothing to restore.")
        return 0

    untracked = [g for g in geo_ids if g not in CLASS_A_TRACKED_EXCEPTIONS]
    if untracked:
        print(f"  REFUSING: {len(untracked)} geo_id(s) not in CLASS_A_TRACKED_EXCEPTIONS: {untracked}")
        print("  Add them to the tracked list (with a real resolution-status note) before restoring.")
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM tax_billing_quarantine WHERE geo_id = ANY(%s)",
            (geo_ids,),
        )
        n_before = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM tax_billing WHERE geo_id = ANY(%s)",
            (geo_ids,),
        )
        n_live_before = cur.fetchone()[0]

    print(f"  Rows currently in tax_billing_quarantine for these {len(geo_ids)} geo_id(s): {n_before:,}")
    print(f"  Rows currently in tax_billing (should be 0 -- these were fully swept out): {n_live_before:,}")

    if dry_run:
        print("  DRY RUN -- nothing written. Re-run with dry_run=False / --restore-class-a "
              "(no --dry-run) to execute.")
        return n_before

    restore_sql = """
        WITH restored AS (
            DELETE FROM tax_billing_quarantine
            WHERE geo_id = ANY(%s)
            RETURNING geo_id, tax_year, billing_num, owner_name, total_tax,
                      total_paid, total_due, is_delinquent, first_delinquent_yr,
                      cause_number, exemption_codes, data_source, confidence_level
        )
        INSERT INTO tax_billing
            (geo_id, tax_year, billing_num, owner_name, total_tax, total_paid,
             total_due, is_delinquent, first_delinquent_yr, cause_number,
             exemption_codes, data_source, confidence_level)
        SELECT geo_id, tax_year, billing_num, owner_name, total_tax, total_paid,
               total_due, is_delinquent, first_delinquent_yr, cause_number,
               exemption_codes, data_source, confidence_level
        FROM restored
        -- Defensive ON CONFLICT: should never fire (a quarantined row's
        -- (geo_id, tax_year) has no live counterpart by construction -- it
        -- was DELETEd from tax_billing when quarantined in the first
        -- place), but guards against a hand-run INSERT elsewhere having
        -- since re-populated the same key while this row sat in quarantine.
        ON CONFLICT (geo_id, tax_year) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(restore_sql, (geo_ids,))
        n_restored = cur.rowcount
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM tax_billing_quarantine WHERE geo_id = ANY(%s)",
            (geo_ids,),
        )
        n_after_quarantine = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM tax_billing WHERE geo_id = ANY(%s)",
            (geo_ids,),
        )
        n_after_live = cur.fetchone()[0]

    print(f"  → {n_restored:,} rows moved from tax_billing_quarantine back to tax_billing.")
    print(f"  → tax_billing_quarantine now holds {n_after_quarantine:,} rows for these geo_id(s) "
          f"{'(PASS -- fully drained)' if n_after_quarantine == 0 else '(FAIL -- some rows did not move, check ON CONFLICT DO NOTHING skips above)'}")
    print(f"  → tax_billing now holds {n_after_live:,} rows for these geo_id(s) "
          f"{'(PASS -- matches quarantine count before restore)' if n_after_live == n_before else '(FAIL -- count mismatch, investigate before trusting this data)'}")
    return n_restored


def verify_year_bounds(conn):
    """
    Permanent regression assertion (Issue 4, tightened per Cowork brief
    "Tighten the Contamination Assertion, Begin Class A Resolution", July
    2026): every tax_billing row must satisfy the SAME contamination
    definition (_CONTAMINATION_WHERE: tax_year < 2021 OR tax_year = 9999)
    that investigate()/run()/restore_class_a() all use -- i.e. this is
    "confirm quarantine actually closed the gap" using the actual
    definition of the gap, not a separately-typed-out approximation of it.

    BUG FOUND AND FIXED (Cowork brief "Reconcile a Discrepancy in
    `--verify`'s Reporting", July 2026): this function previously used its
    OWN, different condition here -- `tax_year NOT BETWEEN 1990 AND
    current_year+1` (mirroring loaders/db.py's is_valid_tax_year(), a
    GENERIC "is this year plausible at all" sanity bound meant as a
    backstop for ANY loader/table). That is not the same test as
    _CONTAMINATION_WHERE: is_valid_tax_year()'s bound treats any year in
    [1990, 2020] as fine, while _CONTAMINATION_WHERE correctly treats
    anything < 2021 as contaminated, because tax_billing SPECIFICALLY has
    never had legitimate data before 2021 (see _CONTAMINATION_WHERE's own
    comment) -- a narrower, table-specific rule the generic bound doesn't
    know about. The two conditions only agree on tax_year = 9999 or
    tax_year < 1990; they DISAGREE on the whole 1990-2020 range.

    This is exactly what produced the reported "1 row" instead of "37 rows"
    after restoring the 12 Class A parcels: of those 37 restored rows, 36
    apparently carry a tax_year somewhere in 1990-2020 (a plausible-looking
    but still wrong year for tax_billing specifically) and were silently
    invisible to BOTH this function's n_bad and n_tracked counts -- not
    excluded by the tracked-exceptions allowlist, just never matched by the
    WHERE clause at all, because [1990, 2020] passes the generic
    "NOT BETWEEN 1990 AND current_year+1" bound. Only the 1 row with a
    tax_year actually < 1990 or = 9999 (or > current_year+1) tripped the old
    condition, hence "1" -- an undercounting bug, not a real reconciliation
    (the 36 missing rows were never "fine," they were invisible to this
    specific query).

    FIXED by reusing _CONTAMINATION_WHERE directly -- the single source of
    truth this whole script already uses everywhere else -- instead of a
    second, independently-typed-out definition of "bad" that could (and
    did) drift out of sync with it. n_tracked now correctly reports all 37.

    The generic is_valid_tax_year()-style sanity bound is still a
    legitimate, DIFFERENT check in its own right (it catches a class of
    garbage _CONTAMINATION_WHERE doesn't: e.g. a wildly out-of-range future
    year like 50000, which is neither < 2021 nor literally 9999) -- kept
    below as a clearly SEPARATE, distinctly-labeled assertion so it's never
    confused with "the" contamination-tracking count again.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM tax_billing WHERE {_CONTAMINATION_WHERE} AND geo_id != ALL(%s)",
            (CLASS_A_TRACKED_EXCEPTIONS,),
        )
        n_bad = cur.fetchone()[0]
        # Also report the excluded-but-still-contaminated count separately,
        # so a PASS here never silently implies "no contamination at all" --
        # the tracked exceptions still show up, just accounted for by name.
        cur.execute(
            f"SELECT count(*) FROM tax_billing WHERE {_CONTAMINATION_WHERE} AND geo_id = ANY(%s)",
            (CLASS_A_TRACKED_EXCEPTIONS,),
        )
        n_tracked = cur.fetchone()[0]
    ok = (n_bad == 0)
    print(f"  ASSERTION: SELECT count(*) FROM tax_billing WHERE {_CONTAMINATION_WHERE} "
          f"AND geo_id NOT IN ({len(CLASS_A_TRACKED_EXCEPTIONS)} tracked Class A geo_ids)")
    print(f"  → {n_bad:,} untracked rows "
          f"{'(PASS -- no contamination outside the tracked, named exceptions)' if ok else '(FAIL -- a NEW/untracked contamination source exists)'}")
    print(f"  → {n_tracked:,} rows still sitting in tax_billing under the "
          f"{len(CLASS_A_TRACKED_EXCEPTIONS)} tracked Class A exception(s) "
          f"(expected, not a failure -- see CLASS_A_TRACKED_EXCEPTIONS' own resolution notes)")

    # Separate, generic sanity-bound check -- distinctly labeled, never
    # merged with the counts above. Catches nonsensical tax_year values
    # _CONTAMINATION_WHERE's narrower definition wouldn't (e.g. a
    # far-future garbage value like 50000 that isn't literally the 9999
    # sentinel and wouldn't trip "< 2021" either). Scoped to the SAME
    # CLASS_A_TRACKED_EXCEPTIONS allowlist as the checks above -- a tracked
    # exception is tracked regardless of which specific rule happens to
    # flag one of its rows (some of the 37 restored rows -- e.g. any
    # literal 9999 sentinel among them -- satisfy BOTH this bound and
    # _CONTAMINATION_WHERE; that's not new information, so it must not
    # double-fail here). This check earns its keep only by catching
    # genuinely NEW garbage on an UNTRACKED geo_id that _CONTAMINATION_WHERE
    # itself would miss.
    import datetime
    max_year = datetime.date.today().year + 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM tax_billing "
            "WHERE (tax_year NOT BETWEEN 1990 AND %s) AND geo_id != ALL(%s)",
            (max_year, CLASS_A_TRACKED_EXCEPTIONS),
        )
        n_insane = cur.fetchone()[0]
    print(f"  SEPARATE CHECK (generic sanity bound, distinct from the contamination "
          f"count above -- same tracked-exceptions allowlist): SELECT count(*) FROM "
          f"tax_billing WHERE (tax_year NOT BETWEEN 1990 AND {max_year}) "
          f"AND geo_id NOT IN ({len(CLASS_A_TRACKED_EXCEPTIONS)} tracked Class A geo_ids)")
    print(f"  → {n_insane:,} untracked rows outside [1990, {max_year}] "
          f"{'(PASS)' if n_insane == 0 else '(FAIL -- a genuinely new out-of-range value exists on an untracked geo_id)'}")
    if n_insane > 0:
        ok = False
    return ok


def emit_class_a_list(conn):
    """
    Prints the CURRENT live Class A orphan geo_id list (real appraisal data
    on file, only tax_billing/tax_billing_quarantine is contaminated) as a
    ready-to-paste Python list literal, so CLASS_A_TRACKED_EXCEPTIONS above
    can be filled in completely without hand-transcribing geo_ids from
    --investigate's human-readable output. Does NOT itself update the
    constant in this file -- paste the output in manually, after adding a
    resolution-status comment for each new entry (per this brief's own
    requirement that the allowlist stay a NAMED, TRACKED exception list,
    not a bare ID dump).

    Creates tax_billing_quarantine first if it doesn't exist yet (same as
    investigate()) -- _INVESTIGATE_SQL reads from it unconditionally now
    (see that query's own comment on the quarantine-state-invariance fix),
    so this must exist even on a first-ever run before anything has been
    quarantined.
    """
    with conn.cursor() as cur:
        cur.execute(_CREATE_QUARANTINE_SQL)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(_INVESTIGATE_SQL)
        rows = cur.fetchall()
    class_a_ids = sorted(geo_id for geo_id, _situs, _sc1, real_years, _ty in rows
                          if real_years and real_years > 0)
    print(f"  Class A geo_ids ({len(class_a_ids)} total) -- paste into "
          f"CLASS_A_TRACKED_EXCEPTIONS, each with its own resolution note:")
    print("  [")
    for geo_id in class_a_ids:
        print(f'      "{geo_id}",')
    print("  ]")
    return class_a_ids


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--investigate", action="store_true",
                     help="Report the orphan-parcel carve-out only; no writes.")
    ap.add_argument("--run", action="store_true",
                     help="Create the quarantine table and move eligible rows.")
    ap.add_argument("--dry-run", action="store_true",
                     help="With --run: report counts only, write nothing.")
    ap.add_argument("--include-orphans", action="store_true",
                     help="DANGEROUS, requires Diego's explicit sign-off after "
                          "reviewing --investigate output: also quarantine the "
                          "orphan-parcel carve-out (skips the exclusion).")
    ap.add_argument("--verify", action="store_true",
                     help="Run the permanent tax_year-bounds assertion only "
                          "(no writes); non-zero exit if it fails.")
    ap.add_argument("--emit-class-a", action="store_true",
                     help="Print the current live Class A geo_id list as a "
                          "ready-to-paste Python literal for "
                          "CLASS_A_TRACKED_EXCEPTIONS (no writes).")
    ap.add_argument("--restore-class-a", action="store_true",
                     help="Move CLASS_A_TRACKED_EXCEPTIONS' rows back from "
                          "tax_billing_quarantine into tax_billing (reverses "
                          "an over-broad --include-orphans run). Combine with "
                          "--dry-run to preview counts only.")
    args = ap.parse_args()

    if not (args.investigate or args.run or args.verify
            or args.emit_class_a or args.restore_class_a):
        ap.error("Specify --investigate, --run, --verify, --emit-class-a, or --restore-class-a.")

    conn = get_conn()
    try:
        if args.investigate:
            with conn.cursor() as cur:
                cur.execute(_CREATE_QUARANTINE_SQL)
            conn.commit()
            investigate(conn)
        if args.run:
            run(conn, dry_run=args.dry_run, include_orphans=args.include_orphans)
        if args.restore_class_a:
            restore_class_a(conn, dry_run=args.dry_run)
        if args.verify:
            ok = verify_year_bounds(conn)
            if not ok:
                sys.exit(1)
        if args.emit_class_a:
            emit_class_a_list(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
