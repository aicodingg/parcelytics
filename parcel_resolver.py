"""
parcel_resolver.py — PARTITION-2-IMPLEMENT, Part 4.

Real implementation of SPEC_COUNTY_PARTITIONING.md finding 9.5's resolver
seam design: ONE function every one of app.py's real 218 geo_id-lookup call
sites would eventually route through, instead of each inlining its own
`WHERE geo_id = %s` (with no county concept at all, today).

county_code is hardcoded to 'TRAVIS' at THIS ONE SEAM, and nowhere else —
until Diego's separate, still-undecided application-level routing/UI
question (SPEC_COUNTY_PARTITIONING.md §7) gives real call sites an actual
county_code to pass instead of the default. That converts what would
otherwise be a future 218-call-site sweep across app.py into a future
1-place change (this function's default, or its callers passing a real
value) — the whole point of building this seam now, ahead of the routing
decision, per finding 9.5.

── NOT WIRED UP (explicit, per this brief's own boundary) ──────────────────
Nothing in app.py calls this function yet. PARTITION-2-IMPLEMENT's brief is
explicit: build resolve_parcel() for real, tested, ready to be adopted
later — do NOT wire up app.py's 218 real call sites in this brief. That's
deferred to whenever Diego's routing/UI decision (§7) actually lands,
alongside it, not before. Wiring it up today would also be premature for a
more basic reason: `parcel.county_code` does not exist as a real column
yet — migrate_county_partitioning.py has not been run against production.
Calling this function against a pre-migration database would fail with
"column county_code does not exist," the same real deployment-sequencing
constraint every other PARTITION-2-IMPLEMENT Part 3 change carries (see
app.py's _snapshot_summary_freshness() docstring for the fullest version of
this warning).

── NAMING COLLISION, CHECKED AND DISTINGUISHED (per this brief's own
instruction) ────────────────────────────────────────────────────────────
An unrelated, pre-existing `resolve_parcel(conn, ident)` already exists in
task_staging/data_integrity/verify_task1.py:67 — a one-off investigation
script's own helper that takes a raw psycopg2 connection and a single
`ident` string, tries it against geo_id, prop_id, and a LIKE-wildcard
partial match (three different match strategies in one function, no
county concept at all — it predates this entire migration). Different
signature `(conn, ident)` vs. this module's `(geo_id, county_code, query_fn)`,
different purpose (ad hoc investigation-script lookup vs. the real,
production resolver seam every app.py route would route through), and a
different, unrelated module — no real technical conflict (Python's import
system scopes both by their own module path), but named here explicitly so
a future reader searching for "resolve_parcel" doesn't confuse the two.
That older function is untouched by this brief.

Usage (once wired up, later, per the boundary above):
    from parcel_resolver import resolve_parcel
    parcel = resolve_parcel(geo_id)                       # defaults to 'TRAVIS'
    parcel = resolve_parcel(geo_id, county_code="DALLAS")  # once real Dallas
                                                            # data + a real
                                                            # routing decision
                                                            # exist
"""

DEFAULT_COUNTY = "TRAVIS"


def resolve_parcel(geo_id, county_code=DEFAULT_COUNTY, query_fn=None):
    """
    The real resolver seam. Returns the same shape app.py's own query()
    helper already returns for a `one=True` call (a RealDictRow / dict-like
    single row, or None if not found) — a drop-in replacement for the
    inlined `query("SELECT * FROM parcel WHERE geo_id = %s", (geo_id,),
    one=True)` pattern repeated across app.py's real call sites today.

    Parameters:
      geo_id      -- the real TCAD-style account number to look up.
      county_code -- which county's `parcel` rows to search. Defaults to
                     'TRAVIS' -- the one hardcoded seam this whole design
                     exists to create (finding 9.5). Real callers, once
                     wired up, would eventually pass a real value here
                     instead of relying on the default, once Diego's
                     routing/UI decision (§7) exists to tell them which
                     county a given request is even about.
      query_fn    -- injectable DB-query callable, real signature
                     query_fn(sql, params, one=True) -> dict|None, matching
                     app.py's own query() helper's exact signature. Defaults
                     to a lazy import of app.query so this module works as
                     a drop-in for real app.py call sites without every
                     caller having to pass it explicitly -- but the
                     injection point exists specifically so this function
                     is fully unit-testable without Flask/psycopg2 being
                     importable (this sandbox has neither -- see
                     test_parcel_resolver.py, which passes a real stub here
                     instead of relying on the lazy import).

    Real SQL, once county_code is a genuine column on `parcel` (post-
    migration): `SELECT * FROM parcel WHERE county_code = %s AND geo_id = %s`
    -- a plain composite-key equality lookup, deliberately as simple as
    possible; this seam's whole value is in being the ONE place that
    lookup shape lives, not in being clever.
    """
    if query_fn is None:
        # Lazy import -- keeps this module importable (e.g. for the
        # unit tests below) even in an environment where Flask/psycopg2
        # aren't installed, as long as no REAL call is ever made without
        # an explicit query_fn in that environment.
        from app import query as query_fn

    return query_fn(
        "SELECT * FROM parcel WHERE county_code = %s AND geo_id = %s",
        (county_code, geo_id),
        one=True,
    )
