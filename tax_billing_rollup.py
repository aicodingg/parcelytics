"""
tax_billing_rollup.py — single source of truth for computing tax_billing /
tax_billing_entity from tax_billing_account / tax_billing_account_entity
(TAX-BILLING-REKEY-3, SPEC_TAX_BILLING_REKEY.md §1.3/§7.3). Mirrors
parcel_rollup.py's "one canonical module" pattern directly: before this
fix, every PARCEL/TXACCNUM-keyed loader wrote tax_billing/tax_billing_entity's
value columns directly, keyed by (geo_id, tax_year) — meaning any parcel with
more than one real taxing account sharing that geo_id (1,696 confirmed real
multi-sub-account parcels, per load_pir_billing_2021_full.py's own full-file
scan) silently lost every account but the last one an `ON CONFLICT ... DO
UPDATE` happened to process, with no error, no warning, and no way to recover
the destroyed dollars after the fact. Measured real damage from this exact
mechanism: $5,794,968.90 (tax_billing) / $170,061,400.28 (tax_billing_entity)
lost across the retained source files (M0/M0-EXTENSION-1). See
KNOWN_LIMITATIONS.md's tax_billing_entity collision entry for the full
incident history.

Now: loaders write tax_billing_account / tax_billing_account_entity (the real
per-account grain — no geo_id collision possible, since account_id, not
geo_id, is the primary key there), and ONLY this module writes
tax_billing/tax_billing_entity's value columns, by SUM()-aggregating every
account that shares a geo_id.

Three responsibilities, all idempotent (safe to re-run any number of times
with no drift):
  1. rollup_tax_year() / rollup_all_years() — (re)compute tax_billing /
     tax_billing_entity rows from tax_billing_account /
     tax_billing_account_entity via SUM(), grouped by
     (county_code, geo_id, tax_year).
  2. merge_portal_scrape_year() / merge_portal_scrape_all_years() — apply
     tax_billing_portal_scrape rows on top of the account-grain rollup,
     per §7.3's own explicitly-deferred "real, concrete work for the
     implementation brief" preference-logic question. Design decision made
     HERE (not resolved by the spec amendment itself — flagged as such):
     an account-grain rollup row ALWAYS outranks a portal-scrape row for the
     same (county_code, geo_id, tax_year) — a real Tax Office billing export
     is categorically stronger evidence than a scraped payment-receipts
     page, and the account-grain data is what this whole migration exists
     to protect. This mirrors scrape_billing_history.py's own pre-existing
     _UPSERT_SQL WHERE guard (`data_source IS NULL OR data_source =
     'portal_scrape'`) exactly — the same non-destructive-upsert discipline
     that guard already encoded, just now applied from the rollup's side
     instead of the scraper's side, since the scraper no longer writes
     tax_billing directly (see §7.3 design (a) / loaders/scrape_billing_history.py).
  3. account_count semantics on the written tax_billing/tax_billing_entity
     row (see schema.sql), identical in spirit to parcel_tax_year.unit_count:
       NULL = row hasn't been rolled up yet from the unit layer (pre-
              migration legacy state, or a portal-scrape-only row that has
              never had a matching account-grain rollup)
       1    = simple single-account parcel; the row's values are that one
              account's own values
       >1   = true multi-account parcel; every value column is a SUM()
              across that many tax_billing_account rows for the year
     A portal-scrape-merged row does NOT get an account_count value (the
     portal has no account concept at all, confirmed live per §7.3) — the
     merge SQL leaves account_count untouched on both INSERT and UPDATE.

NULL-value semantics of SUM(): identical to parcel_rollup.py's own — Postgres
SUM() ignores NULL inputs and returns NULL only if EVERY input row was NULL
(never a silent 0).

    from tax_billing_rollup import rollup_all_years, merge_portal_scrape_all_years, run
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402  (repo-root import, same pattern as parcel_rollup.py)


# ── Production SQL (executed against Postgres; not exercised by fixture
#    tests in this sandbox — see loaders/test_tax_billing_rollup.py's module
#    docstring for the AC8 disclosure on why, and compute_rollup() /
#    compute_entity_rollup() / compute_portal_merge() below for the
#    hand-verified pure-Python mirror of this same logic that IS
#    fixture-tested). ───────────────────────────────────────────────────
ROLLUP_SQL = """
    INSERT INTO tax_billing
        (county_code, geo_id, tax_year, billing_num, owner_name,
         total_tax, total_paid, total_due, is_delinquent, exemption_codes,
         data_source, confidence_level, account_count)
    SELECT county_code, geo_id, tax_year,
           -- billing_num/owner_name: MIN() tiebreak, same rationale as
           -- parcel_rollup's data_source MIN() -- display convenience
           -- fields, not summed quantities.
           MIN(billing_num), MIN(owner_name),
           SUM(total_tax), SUM(total_paid), SUM(total_due),
           BOOL_OR(is_delinquent),
           string_agg(DISTINCT exemption_codes, ',' ORDER BY exemption_codes),
           MIN(data_source), MIN(confidence_level),
           COUNT(*)
    FROM tax_billing_account
    WHERE tax_year = %(tax_year)s
    GROUP BY county_code, geo_id, tax_year
    ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE
        SET billing_num      = EXCLUDED.billing_num,
            owner_name       = EXCLUDED.owner_name,
            total_tax        = EXCLUDED.total_tax,
            total_paid       = EXCLUDED.total_paid,
            total_due        = EXCLUDED.total_due,
            is_delinquent    = EXCLUDED.is_delinquent,
            exemption_codes  = EXCLUDED.exemption_codes,
            data_source      = EXCLUDED.data_source,
            confidence_level = EXCLUDED.confidence_level,
            account_count    = EXCLUDED.account_count
"""

ENTITY_ROLLUP_SQL = """
    INSERT INTO tax_billing_entity
        (county_code, geo_id, tax_year, entity_code, amount_due, amount_paid, account_count)
    SELECT county_code, geo_id, tax_year, entity_code,
           SUM(amount_due), SUM(amount_paid), COUNT(*)
    FROM tax_billing_account_entity
    WHERE tax_year = %(tax_year)s
    GROUP BY county_code, geo_id, tax_year, entity_code
    ON CONFLICT (county_code, geo_id, tax_year, entity_code) DO UPDATE
        SET amount_due    = EXCLUDED.amount_due,
            amount_paid   = EXCLUDED.amount_paid,
            account_count = EXCLUDED.account_count
"""

# See module docstring point 2 for the "account-grain always wins" design
# decision this WHERE guard encodes. total_tax is set to total_paid (not
# left NULL) for the same reason scrape_billing_history.py's original
# _UPSERT_SQL did this: a portal receipt is what was PAID, not necessarily
# what was LEVIED, but it's the only figure this source has -- same
# disclosed approximation, just relocated here from the scraper.
PORTAL_MERGE_SQL = """
    INSERT INTO tax_billing
        (county_code, geo_id, tax_year, total_tax, total_paid, data_source, confidence_level)
    SELECT county_code, geo_id, tax_year, total_paid, total_paid, data_source, confidence_level
    FROM tax_billing_portal_scrape
    WHERE tax_year = %(tax_year)s
    ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE
        SET total_tax        = EXCLUDED.total_tax,
            total_paid        = EXCLUDED.total_paid,
            data_source       = EXCLUDED.data_source,
            confidence_level  = EXCLUDED.confidence_level
        WHERE (tax_billing.data_source IS NULL
            OR tax_billing.data_source = 'portal_scrape')
"""

DISTINCT_ACCOUNT_YEARS_SQL = "SELECT DISTINCT tax_year FROM tax_billing_account ORDER BY tax_year"
DISTINCT_PORTAL_YEARS_SQL = "SELECT DISTINCT tax_year FROM tax_billing_portal_scrape ORDER BY tax_year"


# ── DB-facing wrappers (production code path — requires a live conn) ────
def rollup_tax_year(conn, tax_year):
    """(Re)compute tax_billing + tax_billing_entity rows for one tax_year
    from the account-grain unit layer. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(ROLLUP_SQL, {"tax_year": tax_year})
        n_billing = cur.rowcount
        cur.execute(ENTITY_ROLLUP_SQL, {"tax_year": tax_year})
        n_entity = cur.rowcount
    conn.commit()
    return n_billing, n_entity


def distinct_account_years(conn):
    with conn.cursor() as cur:
        cur.execute(DISTINCT_ACCOUNT_YEARS_SQL)
        return [r[0] for r in cur.fetchall()]


def rollup_all_years(conn):
    """(Re)compute tax_billing/tax_billing_entity for every tax_year present
    in tax_billing_account."""
    total_billing, total_entity = 0, 0
    for year in distinct_account_years(conn):
        n_billing, n_entity = rollup_tax_year(conn, year)
        total_billing += n_billing
        total_entity += n_entity
    return total_billing, total_entity


def merge_portal_scrape_year(conn, tax_year):
    """Apply tax_billing_portal_scrape rows for one tax_year on top of the
    account-grain rollup, per the account-always-wins policy in the module
    docstring. Idempotent. Should run AFTER rollup_tax_year() for the same
    year, never before -- otherwise a portal row could win a conflict
    against a tax_billing row that doesn't exist yet, then get silently
    protected from the real rollup by its own WHERE guard once the account
    rollup does run."""
    with conn.cursor() as cur:
        cur.execute(PORTAL_MERGE_SQL, {"tax_year": tax_year})
        rowcount = cur.rowcount
    conn.commit()
    return rowcount


def distinct_portal_years(conn):
    with conn.cursor() as cur:
        cur.execute(DISTINCT_PORTAL_YEARS_SQL)
        return [r[0] for r in cur.fetchall()]


def merge_portal_scrape_all_years(conn):
    total = 0
    for year in distinct_portal_years(conn):
        total += merge_portal_scrape_year(conn, year)
    return total


def run(conn, tax_year=None):
    """
    Full rollup entry point used by loaders/run_all.py after all source
    loaders finish. Runs the account-grain rollup FIRST, then the
    portal-scrape merge, so a portal row can only ever fill a gap the
    account-grain data didn't cover for that (county_code, geo_id, tax_year)
    -- never overwrite it. See module docstring point 2.
    """
    if tax_year is not None:
        n_billing, n_entity = rollup_tax_year(conn, tax_year)
        n_portal = merge_portal_scrape_year(conn, tax_year)
    else:
        n_billing, n_entity = rollup_all_years(conn)
        n_portal = merge_portal_scrape_all_years(conn)
    return {
        "tax_billing_rows": n_billing,
        "tax_billing_entity_rows": n_entity,
        "portal_scrape_merged_rows": n_portal,
    }


# ── Pure-Python mirror of the SQL above — no DB required. Used by
#    loaders/test_tax_billing_rollup.py to fixture-test NULL-semantics,
#    the account-always-wins portal policy, and idempotency in this sandbox
#    (see that file's docstring for why the SQL itself can't be
#    executed-verified here). Kept in hand-verified lockstep with
#    ROLLUP_SQL / ENTITY_ROLLUP_SQL / PORTAL_MERGE_SQL above -- any change
#    to one must be mirrored in the other. ────────────────────────────────
def compute_rollup(account_rows, tax_year):
    """
    account_rows: iterable of dicts, each a tax_billing_account row — keys:
    county_code, account_id, tax_year, geo_id, billing_num, owner_name,
    total_tax, total_paid, total_due, is_delinquent, exemption_codes,
    data_source, confidence_level.

    Returns a list of dicts (one per (county_code, geo_id) present for
    `tax_year`), matching exactly what ROLLUP_SQL would INSERT.
    """
    groups = {}
    for row in account_rows:
        if row["tax_year"] != tax_year:
            continue
        key = (row["county_code"], row["geo_id"])
        groups.setdefault(key, []).append(row)

    out = []
    for (county_code, geo_id), rows in groups.items():
        out.append({
            "county_code": county_code,
            "geo_id": geo_id,
            "tax_year": tax_year,
            "billing_num": _sql_min(rows, "billing_num"),
            "owner_name": _sql_min(rows, "owner_name"),
            "total_tax": _sql_sum(rows, "total_tax"),
            "total_paid": _sql_sum(rows, "total_paid"),
            "total_due": _sql_sum(rows, "total_due"),
            "is_delinquent": _sql_bool_or(rows, "is_delinquent"),
            "exemption_codes": _sql_code_union(rows),
            "data_source": _sql_min(rows, "data_source"),
            "confidence_level": _sql_min(rows, "confidence_level"),
            "account_count": len(rows),
        })
    return out


def compute_entity_rollup(account_entity_rows, tax_year):
    """
    account_entity_rows: iterable of dicts, each a tax_billing_account_entity
    row — keys: county_code, account_id, tax_year, geo_id, entity_code,
    amount_due, amount_paid.

    Returns a list of dicts (one per (county_code, geo_id, entity_code)
    present for `tax_year`), matching exactly what ENTITY_ROLLUP_SQL would
    INSERT.
    """
    groups = {}
    for row in account_entity_rows:
        if row["tax_year"] != tax_year:
            continue
        key = (row["county_code"], row["geo_id"], row["entity_code"])
        groups.setdefault(key, []).append(row)

    out = []
    for (county_code, geo_id, entity_code), rows in groups.items():
        out.append({
            "county_code": county_code,
            "geo_id": geo_id,
            "tax_year": tax_year,
            "entity_code": entity_code,
            "amount_due": _sql_sum(rows, "amount_due"),
            "amount_paid": _sql_sum(rows, "amount_paid"),
            "account_count": len(rows),
        })
    return out


def compute_portal_merge(existing_billing, portal_rows, tax_year):
    """
    existing_billing: dict {(county_code, geo_id, tax_year): {"data_source": ...}, ...}
        -- the tax_billing rows already present (e.g. from compute_rollup's
        own output, keyed the same way PORTAL_MERGE_SQL's ON CONFLICT target
        would see them) BEFORE the merge runs.
    portal_rows: iterable of dicts, each a tax_billing_portal_scrape row --
        keys: county_code, geo_id, tax_year, total_paid, data_source,
        confidence_level.

    Returns {(county_code, geo_id, tax_year): {total_tax, total_paid,
    data_source, confidence_level}} -- the rows the merge would actually
    write (INSERT for a new key, UPDATE for an existing key whose guard
    passes), mirroring PORTAL_MERGE_SQL's WHERE guard exactly: an existing
    row is only overwritten if its data_source is None or 'portal_scrape'.
    A portal row for a key with NO existing tax_billing row always applies
    (mirrors a real ON CONFLICT INSERT with nothing to conflict against).
    """
    out = {}
    for row in portal_rows:
        if row["tax_year"] != tax_year:
            continue
        key = (row["county_code"], row["geo_id"], row["tax_year"])
        existing = existing_billing.get(key)
        if existing is not None:
            existing_source = existing.get("data_source")
            if existing_source not in (None, "portal_scrape"):
                continue  # guard blocks the overwrite -- account data wins
        out[key] = {
            "total_tax": row["total_paid"],
            "total_paid": row["total_paid"],
            "data_source": row["data_source"],
            "confidence_level": row["confidence_level"],
        }
    return out


def _sql_sum(rows, key):
    """Mirror of Postgres SUM(): ignores NULLs; NULL only if every value was NULL."""
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return sum(vals)


def _sql_min(rows, key):
    """Mirror of Postgres MIN(): ignores NULLs; NULL only if every value was NULL."""
    vals = [r.get(key) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return min(vals)


def _sql_bool_or(rows, key):
    """Mirror of Postgres BOOL_OR(): true if any value is true, else false
    (NULLs are ignored, matching real BOOL_OR semantics -- a group of all
    NULL/False values rolls up to False, not NULL, since is_delinquent has
    a DEFAULT FALSE and BOOL_OR never returns NULL unless every input row
    is itself NULL, which cannot happen here since the column is NOT NULL
    by default)."""
    return any(bool(r.get(key)) for r in rows if r.get(key) is not None)


def _sql_code_union(rows):
    """Mirror of the string_agg(DISTINCT ...) call: union of every
    comma-split code, NULLs excluded, sorted."""
    codes = set()
    for r in rows:
        raw = r.get("exemption_codes")
        if not raw:
            continue
        codes.update(c for c in raw.split(",") if c)
    return ",".join(sorted(codes)) if codes else None


if __name__ == "__main__":
    import argparse
    from loaders.db import get_conn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=None, help="Roll up a single tax_year only")
    ap.add_argument("--all-years", action="store_true", help="Roll up every tax_year present in tax_billing_account")
    args = ap.parse_args()

    if not args.all_years and args.year is None:
        ap.error("pass --year YYYY or --all-years")

    conn = get_conn()
    result = run(conn, tax_year=args.year)
    print(f"tax_billing rolled up: {result['tax_billing_rows']:,} rows")
    print(f"tax_billing_entity rolled up: {result['tax_billing_entity_rows']:,} rows")
    print(f"portal_scrape rows merged: {result['portal_scrape_merged_rows']:,} rows")
    conn.close()
