"""
parcel_filters.py — single source of truth for "real, non-exempt,
non-personal-property parcel" scoping, used by every county-wide aggregate
query in this codebase (Market Snapshot, /api/benchmark, the
county_benchmark loader, the /parcels drill-through route).

Before this fix (July 2026), this exclusion intent was independently typed
out FOUR separate times:
  - app.py's CANONICAL_PARCEL_EXCL module constant
  - app.py's /api/benchmark route's own `excl_filter` local variable
  - loaders/compute_metrics.py's BENCHMARK_EXCLUDE_PREFIXES / _exclude_clause()
  - app.py's /parcels route, which had already DRIFTED from the other
    three — it was missing the state_cd1 'N%' leg entirely, silently
    letting a handful of personal-property rows back into that one route's
    parcel listing.
That drift is exactly why this module exists: with one canonical
definition, a future change only has to happen once, and an audit (see
verify_parcel_filters_coverage.py, at the repo root, alongside its own
fixture/corruption tests in test_verify_parcel_filters_coverage.py) can
mechanically prove every consumer still matches it instead of relying on
a comment promising they do.

That same investigation also found a real, user-facing bug: the pre-fix
fragment ("p.state_cd1 NOT LIKE 'X%%' AND p.state_cd1 NOT LIKE 'N%%'") is
NOT NULL-safe. SQL's NOT LIKE evaluates to NULL — not TRUE — when
state_cd1 IS NULL, and Postgres's WHERE clause silently DROPS any row
whose condition evaluates to NULL. state_cd1 is populated ONLY by the
2021-2024 AJR loader (see data_coverage.py's own note on this), so any
parcel newer than that extract — new construction, new subdivisions, new
commercial development — has state_cd1 IS NULL and was being silently
zeroed out of every county-wide dollar total on the site, not merely left
unlabeled in a breakdown table. A prior comment in compute_metrics.py
reasoned this was benign because such rows "also can't match any
label_case_sql() WHEN clause" — that reasoning doesn't hold in general: a
NULL-state_cd1 parcel with a valid classi_cd CAN classify correctly via
label_case_sql() (which is classi_cd-first), but was still being dropped
by THIS clause's WHERE-level NULL propagation before classification ever
ran.

Import from anywhere that needs it — no sys.path changes required. This
module lives at the repo root: app.py is already there, and every
loaders/*.py script that needs it already inserts the repo root onto
sys.path before its own `import config` (same mechanism that already
makes `import config` and `from tax_logic.classify import ...` work from
inside loaders/).

    from parcel_filters import CANONICAL_PARCEL_EXCL
"""

# ── NULL-safety fix ──────────────────────────────────────────────────────
# COALESCE(p.state_cd1, '') defaults a NULL to '', which no 'X%'/'N%' LIKE
# pattern ever matches — so a NULL-state_cd1 row now correctly SURVIVES
# this exclusion (it isn't confirmed exempt or personal property; we just
# don't know its exact type yet) instead of being silently dropped by SQL's
# three-valued logic. This changes real query results for the ~17K
# post-2024 parcels with NULL state_cd1 (they now count) — that's the
# actual fix, not a no-op refactor.
#
# ── BPP (AJR-prefixed) scope decision ────────────────────────────────────
# Diego's decision (July 2026, "Fix parcel-exclusion filtering" brief):
# county-total dollar figures EXCLUDE business personal property
# (AJR-prefixed geo_ids) CONSISTENTLY in both years being compared. This is
# a deliberate, named policy — not an accidental byproduct of some other
# filter — so state it explicitly here rather than leaving it to be
# inferred from a bare `NOT LIKE 'AJR%%'` with no rationale attached:
#
#   AJR-sourced BPP accounts carry a $1 PLACEHOLDER market value in
#   CERTIFIED years but a REAL value in PRELIMINARY years (a known loader
#   asymmetry — see KNOWN_LIMITATIONS.md's AJR/BPP section). Including BPP
#   in a county total would make a 2025->2026 comparison read as if BPP
#   value grew from ~$0 to its real figure — not an actual market trend,
#   an artifact of which year's data happens to carry real numbers.
#   Excluding BPP consistently keeps every year-over-year total on this
#   site apples-to-apples, at the acknowledged cost of undercounting
#   TCAD's own published "all property types" total by BPP's real value
#   (TCAD's total includes BPP; ours, by this explicit choice, does not).
#
#   This is a DATA-QUALITY WORKAROUND, not a permanent product stance — if
#   a future brief fixes the certified-year BPP placeholder-value gap at
#   the loader level (i.e., certified years get BPP's real value instead
#   of $1), this decision should be revisited, since the reason for
#   exclusion (the cross-year asymmetry) will no longer hold.
CANONICAL_PARCEL_EXCL = (
    "AND COALESCE(p.state_cd1, '') NOT LIKE 'X%%' "
    "AND COALESCE(p.state_cd1, '') NOT LIKE 'N%%' "
    "AND p.geo_id NOT LIKE 'AJR%%'"
)

# Same fragment, without the leading "AND " — for call sites that build
# their own WHERE clause via `WHERE ... AND (<this>)` rather than
# concatenating a pre-"AND"-ed fragment onto an existing WHERE (e.g.
# compute_metrics.py's `_exclude_clause()`-style callers, or a query whose
# only WHERE condition is this one). Derived from CANONICAL_PARCEL_EXCL by
# slicing, not retyped, so the two forms cannot drift from each other the
# way the original four independent copies did.
CANONICAL_PARCEL_EXCL_BARE = CANONICAL_PARCEL_EXCL[len("AND "):]


def peer_state_cd1_match_sql(column="p.state_cd1", param="%(sc1)s", upper=False):
    """
    NULL-safe equivalent of `LEFT(<column>, 1) = <param>` for peer-matching
    queries (Peer Set, $/SF Benchmark, Submarket Position widgets on the
    property detail page). These don't use CANONICAL_PARCEL_EXCL — they're
    matching a subject parcel's OWN type prefix, not excluding exempt/
    personal-property rows — but share the identical NULL-propagation risk:
    `LEFT(state_cd1, 1) = %(sc1)s` silently drops any CANDIDATE peer row
    with NULL state_cd1 from the pool, regardless of what the subject's own
    prefix is, the same 3-valued-logic mechanism as the bug above.

    The Python-side subject value (`state_cd1`/`sc1` in app.py's peer-set
    functions) already coalesces None to '' via `(parcel.get("state_cd1")
    or "").strip()[:1]` before it's bound as a query parameter — that half
    was already safe. This helper fixes the other half: wrapping the
    CANDIDATE column in the same COALESCE so a NULL-state_cd1 candidate can
    match a subject whose own state_cd1 is ALSO unknown (both coalesce to
    ''), rather than being silently and unconditionally excluded from every
    peer pool. This does not fabricate a type match — a NULL-state_cd1
    subject and a NULL-state_cd1 candidate genuinely share the same "type
    unknown" status, which is the closest honest match available; a
    NULL-state_cd1 candidate still cannot match a subject with a REAL known
    prefix (COALESCE('','') == '' only equals a real prefix like 'A' if sc1
    itself is 'A', which a coalesced-to-'' candidate never produces).

    `upper=True` matches call sites that already UPPER() the column before
    comparing (api_peer_set's Tier 2 fallback: `LEFT(UPPER(p.state_cd1),1)`).
    """
    expr = f"UPPER(COALESCE({column}, ''))" if upper else f"COALESCE({column}, '')"
    return f"LEFT({expr}, 1) = {param}"
