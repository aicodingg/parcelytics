#!/usr/bin/env python3
"""
loaders/scrape_billing_history.py
Scrape 2021–2024 tax payment receipts from Travis County Tax Office portal.

URL pattern:
  https://travis.go2gov.net/showPaymentReceipts.do?account=GEOID0000
  (10-digit geo_id + literal "0000" = 14-digit account number)

Data integrity note:
  Amounts are what was PAID, not necessarily what was LEVIED (tax due).
  Deferrals, partial payments, or supplemental billings can cause them to differ.
  Stored with data_source='portal_scrape' and confidence_level='partial'.
  Do NOT overwrite rows that have better data (taxcur / pir_billing).

TAX-BILLING-REKEY-3 (§7.3 design (a)): this loader now writes
tax_billing_portal_scrape, NOT tax_billing directly. Real, live-confirmed
finding this design is built on (fetch_html() run against 0259410216, the
largest known collision group at 1,210 sub-accounts): the county portal
exposes NO distinct real sub-account numbers anywhere on the page it
returns for a given geo_id -- the only 14-digit number found anywhere on
the page is the synthetic geo_id+"0000" account used to REQUEST the page
itself. This source has no account-number field to re-key against at all,
so unlike the four PARCEL/TXACCNUM-keyed writers, it keeps writing at
geo_id grain -- just to its own real, separate table now, so its genuinely
weaker/different-grain data can never again be silently commingled with
tax_billing_account's real per-account data in one shared table.
tax_billing_rollup.py reads tax_billing_portal_scrape and merges it into
tax_billing on top of the account-grain rollup, per an account-always-wins
policy -- see that module's own docstring.

Rate limit:  0.5–1.0 s between requests — single-threaded, polite scraping only.
Checkpoint:  writes loaders/.scrape_checkpoint.json every 1,000 parcels.

Usage
-----
  # ALWAYS start here — test 500 parcels (3 known sanity-check parcels + 497 random):
  python3 loaders/scrape_billing_history.py --test

  # After validating test results, run the full dataset:
  python3 loaders/scrape_billing_history.py

  # Resume an interrupted run (reads checkpoint, skips processed parcels):
  python3 loaders/scrape_billing_history.py --resume
  python3 loaders/scrape_billing_history.py --test --resume
"""

from __future__ import annotations  # allows X | Y union hints on Python 3.7–3.9

import os, sys, json, time, random, argparse, urllib.request, urllib.error, ssl
from html.parser import HTMLParser
from datetime import datetime

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, is_valid_tax_year
import psycopg2.extras

# ── constants ─────────────────────────────────────────────────────────────────
BASE_URL            = "https://travis.go2gov.net/showPaymentReceipts.do?account={account}"
TARGET_YEARS        = {2021, 2022, 2023, 2024}
CHECKPOINT_FILE     = os.path.join(os.path.dirname(__file__), ".scrape_checkpoint.json")
ERROR_LOG_FILE      = os.path.join(os.path.dirname(__file__), ".scrape_errors.log")
CHECKPOINT_INTERVAL = 1_000   # save checkpoint every N parcels
DELAY_MIN           = 2.0     # seconds between requests — stays under portal rate limit (~30 req/min)
DELAY_MAX           = 3.0
REQUEST_TIMEOUT     = 20      # seconds per request
RATE_LIMIT_BACKOFF  = 15      # seconds to pause after a 429 (short — just let the window reset)
MAX_RETRIES         = 3       # retry count for transient errors (not 404)
# Transparent User-Agent identifies the scraper and provides contact info
USER_AGENT = (
    "Parcelytics/1.0 Tax Research Tool "
    "(Travis County public property data; contact: parcelytics@gmail.com)"
)

# Known sanity-check parcels — scraped first in test mode so we can verify
KNOWN_PARCELS = ["0100030105", "0100030109", "0284460113"]
TEST_LIMIT    = 500   # total parcels in test run


# ── HTML parser ───────────────────────────────────────────────────────────────

class _ReceiptTableParser(HTMLParser):
    """Parse the View Payment Receipts table from the portal page.

    Table structure (4 columns):
      Receipt (link) | Tax Year | Payment Date | Payment Amount
    """

    def __init__(self):
        super().__init__()
        self._in_table  = False
        self._in_row    = False
        self._in_cell   = False
        self._cell_buf  = ""
        self._cells: list[str] = []
        self.rows: list[dict]  = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._cells  = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell  = True
            self._cell_buf = ""

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
            self._in_row   = False
            self._in_cell  = False
        elif tag == "tr" and self._in_row:
            self._in_row = False
            self._try_commit_row()
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cells.append(self._cell_buf.strip())

    def handle_data(self, data):
        if self._in_cell:
            self._cell_buf += data

    def _try_commit_row(self):
        if len(self._cells) != 4:
            return
        _receipt_id, yr_s, date_s, amt_s = self._cells
        yr_s = yr_s.strip()
        if not yr_s.isdigit():
            return  # header row ("Tax Year") — skip
        try:
            year   = int(yr_s)
            # Generalized year-bounds backstop (Issue 4, "Homestead-Cap Data
            # Integrity" Cowork brief, July 2026): this loader's real
            # DB-write path already filters to TARGET_YEARS ({2021..2024})
            # downstream (see upsert loop below), so this specific check is
            # defense-in-depth, not the only gate -- but this parser is the
            # least-structured of tax_billing's 6 writers (a scraped HTML
            # table, not a government-issued CSV/TXT export), so it's the
            # one most worth rejecting an implausible year at the earliest
            # possible point rather than trusting a later filter to catch it.
            if not is_valid_tax_year(year):
                return
            amount = float(amt_s.strip().replace(",", ""))
            self.rows.append({
                "tax_year":       year,
                "payment_date":   date_s.strip(),
                "payment_amount": amount,
            })
        except ValueError:
            pass  # malformed row — skip silently


def parse_receipts(html: str) -> list[dict]:
    """Return [{tax_year, payment_amount}] from page HTML.

    Sums multiple receipts for the same year (installment payments).
    """
    parser = _ReceiptTableParser()
    parser.feed(html)

    # Aggregate: sum all receipts for the same tax year
    by_year: dict[int, float] = {}
    for r in parser.rows:
        by_year[r["tax_year"]] = by_year.get(r["tax_year"], 0.0) + r["payment_amount"]

    return [
        {"tax_year": yr, "payment_amount": round(amt, 2)}
        for yr, amt in sorted(by_year.items())
    ]


# ── database ──────────────────────────────────────────────────────────────────

# TAX-BILLING-REKEY-3: tax_billing_portal_scrape already carries
# data_source/confidence_level from its own CREATE TABLE (schema.sql) --
# this ALTER TABLE against tax_billing is retained only as a defensive
# no-op safety net for any environment whose schema.sql hasn't been
# reapplied yet (mirrors the same "CREATE TABLE IF NOT EXISTS defensively
# at several real call sites" discipline tax_billing_quarantine's own
# comment documents). tax_billing itself no longer needs these columns
# ALTERed by this loader specifically -- they were added here originally
# because this was, pre-rekey, the first writer to need them; that
# historical reason no longer applies, but the ALTER is harmless to leave
# as a defensive default.
_ENSURE_COLS_SQL = """
ALTER TABLE tax_billing
    ADD COLUMN IF NOT EXISTS data_source      VARCHAR(32),
    ADD COLUMN IF NOT EXISTS confidence_level VARCHAR(16);
"""

# DALLAS-GATE-2 Part 1 (real, live-breaking bug found while wiring
# api_billing()'s /<county_slug>/api/billing/<geo_id> route -- this is the
# ONLY write path that route's real-time portal-scrape success case uses):
# tax_billing is a county_code-leading composite-PK table per
# migrate_county_partitioning.py's TABLE_SPECS (old_pk ["geo_id","tax_year"]
# -> new_pk ["county_code","geo_id","tax_year"]). This migration already ran
# and deployed against production (DALLAS-GATE-1). Before this fix,
# _UPSERT_SQL neither wrote county_code (a real NOT NULL column on the live
# table once migrated -- every INSERT here would hard-fail) nor targeted the
# real live unique constraint in its ON CONFLICT clause (still named the old
# (geo_id, tax_year) columns, which is no longer the table's actual
# constraint post-migration -- Postgres errors with "there is no unique or
# exclusion constraint matching the ON CONFLICT specification" against a
# migrated table). Both api_billing()'s async fetch path AND this file's own
# CLI batch-scrape mode call upsert_billing_rows() -- so both were broken by
# this the moment DALLAS-GATE-1's migration landed on production, until now.
DEFAULT_COUNTY = "TRAVIS"  # matches parcel_resolver.py's own DEFAULT_COUNTY convention

# TAX-BILLING-REKEY-3 (§7.3 design (a)): retargeted from tax_billing to
# tax_billing_portal_scrape -- its own real, separate table, since the
# portal has no account-number field to re-key against at all (confirmed
# live, see module docstring). Unconditional upsert here (no WHERE guard):
# this table is exclusively this loader's own write path now, so there is
# no other, better-quality writer for it to protect itself against --
# unlike the old tax_billing target, which also received real
# taxcur/pir_billing rows this loader had to avoid clobbering. That
# protection now lives in tax_billing_rollup.py's own PORTAL_MERGE_SQL
# (account-grain rollup always wins over a portal_scrape row for the same
# key) instead of here.
_UPSERT_SQL = """
INSERT INTO tax_billing_portal_scrape
    (county_code, geo_id, tax_year, total_paid, data_source, confidence_level)
VALUES
    (%(county_code)s, %(geo_id)s, %(tax_year)s, %(total_paid)s,
     'portal_scrape', 'partial')
ON CONFLICT (county_code, geo_id, tax_year) DO UPDATE
    SET total_paid        = EXCLUDED.total_paid,
        data_source       = EXCLUDED.data_source,
        confidence_level  = EXCLUDED.confidence_level
"""


def ensure_columns(conn) -> None:
    """Add data_source + confidence_level to tax_billing if not present."""
    with conn.cursor() as cur:
        cur.execute(_ENSURE_COLS_SQL)
    conn.commit()
    print("  [db] Ensured data_source + confidence_level columns on tax_billing.")


def get_eligible_geo_ids(
    conn,
    limit: int | None = None,
    exclude: set[str] | None = None,
    random_order: bool = False,
    commercial_only: bool = False,
    county_code: str = None,
) -> list[str]:
    """Return geo_ids that have parcel_tax_year rows for 2021–2024.

    Excludes:
      - AJR* personal-property supplement accounts (no real estate billing)
      - Any geo_id in `exclude` (already processed)

    Args:
        limit:        LIMIT N applied before returning (None = unlimited)
        exclude:      set of geo_ids to skip (for random fill, avoiding known parcels)
        random_order: ORDER BY RANDOM() for random sampling (test mode only)
        county_code:  PX-20260830-05 Task 2 (Bucket B): both parcel and
                      parcel_tax_year are composite_pk-migrated
                      (county_code-leading). This formalizes the gap the
                      --county help text used to only disclose (DALLAS-GATE-2:
                      "NOT county-scoped -- harmless today since only Travis
                      has real data loaded"); county_code IS available at
                      both real call sites in main() below (args.county), so
                      it's now threaded and predicated rather than left open.
                      Defaults to None only to keep this a backwards-compatible
                      addition for any other caller; main() always passes it.
    """
    county_clause = "AND p.county_code = %(county_code)s" if county_code else ""
    exclude_clause = ""
    if exclude:
        # Build safe exclusion — geo_ids are always 10-char alphanumeric from TCAD
        quoted = ", ".join(f"'{g}'" for g in exclude)
        exclude_clause = f"AND p.geo_id NOT IN ({quoted})"

    # Commercial-priority filter: multi-family (B*), commercial (F*/L*)
    commercial_clause = (
        "AND LEFT(p.state_cd1, 1) IN ('B', 'F', 'L')"
        if commercial_only else ""
    )

    limit_clause = f"LIMIT {int(limit)}" if limit else ""

    if random_order:
        # RANDOM() can't appear in ORDER BY with SELECT DISTINCT — wrap in subquery
        sql = f"""
            SELECT geo_id FROM (
                SELECT DISTINCT p.geo_id
                FROM   parcel p
                JOIN   parcel_tax_year pty
                       ON pty.geo_id = p.geo_id
                       AND pty.county_code = p.county_code
                       AND pty.tax_year BETWEEN 2021 AND 2024
                WHERE  p.geo_id NOT LIKE 'AJR%%'
                {county_clause}
                {exclude_clause}
                {commercial_clause}
            ) sub
            ORDER BY RANDOM()
            {limit_clause}
        """
    else:
        sql = f"""
            SELECT DISTINCT p.geo_id
            FROM   parcel p
            JOIN   parcel_tax_year pty
                   ON pty.geo_id = p.geo_id
                   AND pty.county_code = p.county_code
                   AND pty.tax_year BETWEEN 2021 AND 2024
            WHERE  p.geo_id NOT LIKE 'AJR%%'
            {county_clause}
            {exclude_clause}
            {commercial_clause}
            ORDER BY p.geo_id
            {limit_clause}
        """
    with conn.cursor() as cur:
        cur.execute(sql, {"county_code": county_code})
        return [row[0] for row in cur.fetchall()]


def upsert_billing_rows(conn, records: list[dict]) -> int:
    """Upsert a batch of billing records. Commits and returns len(records) on
    success. Rolls back automatically on any DB error and re-raises -- the
    caller does NOT need to commit or roll back separately.

    BILLING-DIAG-7: this docstring previously read "Raises on DB error
    (caller rolls back)" -- an implicit, negatively-framed contract (what the
    function does NOT do) rather than a positive statement of what it DOES
    do. Per Fable's architectural review, that's the same class of failure
    as a convention that has to be correctly re-derived at every call site
    instead of being structurally enforced. Rewritten to state ownership
    positively, and the function itself now uses `with conn:` (commits on
    clean exit, rolls back automatically on exception) instead of a bare
    conn.commit() call after the `with cursor` block -- structurally
    equivalent on the success path (this function already committed
    correctly before this change; see the BILLING-DIAG-7 report for the full
    correction of the "missing commit" theory this brief was originally
    built around), but now also rolls back automatically on the error path,
    which the previous version did not do on its own (callers without their
    own explicit `except: conn.rollback()`, e.g. app.py's api_billing(),
    relied on the implicit rollback-on-close instead).

    DALLAS-GATE-2: each record dict must now include a "county_code" key
    (_UPSERT_SQL's %(county_code)s placeholder) -- both callers (this file's
    own CLI batch loop below, and app.py's api_billing() route) were updated
    to supply it. Records missing the key will raise a KeyError from
    execute_batch before anything is written, not silently skip the column.

    TAX-BILLING-REKEY-3: writes tax_billing_portal_scrape now, not
    tax_billing directly -- see _UPSERT_SQL's own comment and the module
    docstring. Records no longer need a "total_tax" key (portal_scrape has
    no total_tax column -- it's a real, separate figure only
    tax_billing_account/tax_billing carries); an extra "total_tax" key in a
    caller's record dict is harmless (ignored by execute_batch, since
    _UPSERT_SQL no longer references %(total_tax)s) but no longer
    necessary.
    """
    if not records:
        return 0
    with conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, _UPSERT_SQL, records, page_size=500)
    return len(records)


# ── checkpoint ────────────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            print(f"  [warn] Could not read checkpoint file — starting fresh.")
    return {"completed": [], "stats": {}}


def save_checkpoint(completed: list[str], stats: dict) -> None:
    data = {
        "completed": completed,
        "stats":     stats,
        "saved_at":  datetime.utcnow().isoformat() + "Z",
    }
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(data, f)


# ── HTTP fetch ────────────────────────────────────────────────────────────────

# Build an SSL context. On macOS + Python.org install the default context may
# fail certificate verification; try certifi first, then system certs, then
# fall back to unverified (acceptable for read-only public government data).
def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(capath="/etc/ssl/certs")   # Linux
    except (FileNotFoundError, ssl.SSLError):
        pass
    # Last resort — disable verification (public read-only portal, low risk)
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx

_SSL_CTX = _make_ssl_context()

# Return codes from fetch_html (makes error handling in the loop explicit)
HTTP_OK          = 0
HTTP_NOT_FOUND   = 404   # account doesn't exist in portal → treat as "no data"
HTTP_RATE_LIMIT  = 429   # server asking us to slow down → back off + retry
HTTP_SERVER_ERR  = 500
HTTP_NETWORK_ERR = -1    # connection-level failure (timeout, DNS, etc.)


def fetch_html(geo_id: str, timeout: int = REQUEST_TIMEOUT) -> tuple[str | None, int]:
    """Fetch the payment-receipts page for one geo_id.

    Returns (html_string, status) where:
      - status = HTTP_OK (0)           → html is the page content
      - status = HTTP_NOT_FOUND (404)  → account not in portal, html is None
      - status = HTTP_RATE_LIMIT (429) → caller should back off and retry
      - status = HTTP_SERVER_ERR (5xx) → transient server error
      - status = HTTP_NETWORK_ERR (-1) → network/SSL error, html is None

    Portal returns ISO-8859-1 encoded HTML.

    BILLING-DIAG-1: `timeout` param added (default unchanged, REQUEST_TIMEOUT
    = 20s — this file's own CLI batch loop below still gets that, plus its
    own MAX_RETRIES=3 wrapper, unaffected by this change). app.py's
    api_billing() route now passes a shorter, explicit timeout for its own
    bounded retry loop -- see that function's own comment for why it can't
    reuse this file's 20s/3-retry pattern verbatim (gunicorn's 30s default
    worker timeout is a hard SIGKILL boundary a live route must respect;
    this batch script has no such constraint).
    """
    account = geo_id + "0000"   # 10-digit geo_id → 14-digit portal account
    url     = BASE_URL.format(account=account)
    req     = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_SSL_CTX) as resp:
            raw = resp.read()
            return raw.decode("iso-8859-1", errors="replace"), HTTP_OK
    except urllib.error.HTTPError as e:
        # Server responded with an error code — capture it for smarter handling
        return None, e.code
    except (urllib.error.URLError, OSError):
        return None, HTTP_NETWORK_ERR


# ── reporting ─────────────────────────────────────────────────────────────────

def print_report(
    stats:         dict,
    known_results: dict[str, dict],   # geo_id → {tax_year: amount, ...}
    geo_ids_total: int,
    elapsed:       float,
) -> None:
    """Print a formatted summary report."""
    print()
    print("=" * 65)
    print("  SCRAPE REPORT")
    print("=" * 65)
    print(f"  Parcels processed        : {stats['scraped']:>8,}")
    print(f"  With 2021–24 data found  : {stats['found']:>8,}  "
          f"({100*stats['found']/max(stats['scraped'],1):.1f}%)")
    print(f"  Rows inserted/updated    : {stats['inserted']:>8,}")
    print(f"  Errors (network/other)   : {stats['errors']:>8,}")
    print(f"  Elapsed                  : {elapsed:>8.1f} s")

    if stats["scraped"] > 0:
        per_req = elapsed / stats["scraped"]
        print(f"  Avg time per request     : {per_req:>8.2f} s")
        # Estimate time for the remaining ~430K parcels
        remaining = max(0, 430_000 - stats["scraped"])
        est_hrs   = remaining * per_req / 3600
        print(f"  Est. full scrape time    : ~{est_hrs:.1f} hrs  "
              f"({remaining:,} parcels at {per_req:.2f}s/req)")

    # Known-parcel sanity check
    print()
    print("  KNOWN-PARCEL SANITY CHECK:")
    print(f"  {'geo_id':<15}  {'Year':<6}  {'Amount Paid':>13}  Note")
    print(f"  {'-'*15}  {'-'*6}  {'-'*13}  {'-'*30}")
    for geo_id in KNOWN_PARCELS:
        yr_map = known_results.get(geo_id, {})
        if not yr_map:
            print(f"  {geo_id:<15}  —       {'(no data returned)':>13}")
            continue
        for yr in sorted(yr_map):
            amt  = yr_map[yr]
            flag = "← target year" if yr in TARGET_YEARS else ""
            print(f"  {geo_id:<15}  {yr:<6}  ${amt:>12,.2f}  {flag}")

    print("=" * 65)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scrape 2021–2024 billing history from Travis County portal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Always run --test first and verify the output before running full.\n"
            "See KNOWN_LIMITATIONS.md for data-quality notes on portal_scrape data."
        ),
    )
    ap.add_argument(
        "--test", action="store_true",
        help=f"Run on exactly {TEST_LIMIT} parcels "
             f"(3 known sanity-check parcels + {TEST_LIMIT - len(KNOWN_PARCELS)} random).",
    )
    ap.add_argument(
        "--resume", action="store_true",
        help="Resume from checkpoint file, skipping already-processed geo_ids.",
    )
    ap.add_argument(
        "--priority-commercial", action="store_true",
        help=(
            "Only scrape commercial, industrial, and multi-family parcels "
            "(state_cd1 starting with B, F, or L). "
            "~15,000-20,000 parcels — designed for weekend batch runs."
        ),
    )
    ap.add_argument(
        "--diagnose", action="store_true",
        help="Fetch 20 parcels from the error log with verbose output — use to identify error types.",
    )
    ap.add_argument(
        "--county", default=DEFAULT_COUNTY,
        help=(
            f"county_code written to every upserted row, AND used to scope "
            f"the geo_id-discovery query (default: {DEFAULT_COUNTY}). "
            "PX-20260830-05 Task 2 (Bucket B): get_eligible_geo_ids() is now "
            "county-scoped on both parcel and parcel_tax_year -- resolves the "
            "DALLAS-GATE-2 disclosed gap this help text used to describe "
            "('NOT county-scoped -- harmless today since only Travis has "
            "real data loaded')."
        ),
    )
    args = ap.parse_args()

    # ── Diagnostic mode ───────────────────────────────────────────────────────
    if args.diagnose:
        print("=" * 65)
        print("  DIAGNOSTIC MODE — fetching 20 parcels from error log")
        print("=" * 65)
        if not os.path.exists(ERROR_LOG_FILE):
            print("  No error log found. Run --test first.")
            return
        with open(ERROR_LOG_FILE) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("---")]
        geo_ids_to_check = [l.split(":")[0] for l in lines[:20]]
        print(f"  Checking: {geo_ids_to_check}\n")
        for geo_id in geo_ids_to_check:
            account = geo_id + "0000"
            url     = BASE_URL.format(account=account)
            req     = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                            context=_SSL_CTX) as resp:
                    body = resp.read().decode("iso-8859-1", errors="replace")
                    receipts = parse_receipts(body)
                    years    = [r["tax_year"] for r in receipts]
                    print(f"  {geo_id}  HTTP 200  years={years}")
            except urllib.error.HTTPError as e:
                print(f"  {geo_id}  HTTP {e.code}  reason={e.reason}")
            except urllib.error.URLError as e:
                print(f"  {geo_id}  URLError  reason={e.reason}")
            except OSError as e:
                print(f"  {geo_id}  OSError   {e!r}")
            time.sleep(DELAY_MIN)
        print("\nDone. Paste this output and we'll diagnose from it.")
        return

    mode_label = (f"TEST ({TEST_LIMIT} parcels)" if args.test else "FULL")
    print("=" * 65)
    print("  Parcelytics — Travis County Payment History Scraper")
    print(f"  Mode: {mode_label}{'  +RESUME' if args.resume else ''}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    print()

    conn = get_conn()

    # 1. Schema migration — add data_source + confidence_level if absent
    ensure_columns(conn)
    print()

    # 2. Load checkpoint (for resume)
    checkpoint    = load_checkpoint() if args.resume else {"completed": [], "stats": {}}
    already_done  = set(checkpoint.get("completed", []))
    completed     = list(already_done)   # mutable list for this run
    if args.resume and already_done:
        print(f"  [resume] Skipping {len(already_done):,} already-processed geo_ids.")
        print()

    # 3. Build the geo_id list
    if args.test:
        # Known parcels go first (skip any already done in a resumed test)
        known_to_run = [g for g in KNOWN_PARCELS if g not in already_done]
        n_random     = TEST_LIMIT - len(KNOWN_PARCELS)  # always reserve 3 slots

        print(f"  Building test batch: {len(KNOWN_PARCELS)} known parcels "
              f"+ {n_random} random …")
        random_geo_ids = get_eligible_geo_ids(
            conn,
            limit=n_random,
            exclude=(set(KNOWN_PARCELS) | already_done),
            random_order=True,
            commercial_only=args.priority_commercial,
            county_code=args.county,
        )
        geo_ids = known_to_run + random_geo_ids
        geo_ids = geo_ids[:TEST_LIMIT]
        print(f"  → {len(known_to_run)} known + {len(random_geo_ids)} random "
              f"= {len(geo_ids):,} parcels to process.")
    else:
        print(f"  Querying eligible geo_ids (parcel_tax_year 2021–2024, not AJR*) …")
        geo_ids = get_eligible_geo_ids(
            conn,
            exclude=already_done,
            commercial_only=args.priority_commercial,
            county_code=args.county,
        )
        label = "commercial/MF" if args.priority_commercial else "all eligible"
        print(f"  → {len(geo_ids):,} {label} geo_ids to process.")

    print()

    if not geo_ids:
        print("  Nothing to do — all eligible parcels already in checkpoint.")
        conn.close()
        return

    # 4. Main scrape loop
    stats: dict[str, int] = {"scraped": 0, "found": 0, "inserted": 0, "errors": 0}
    known_results: dict[str, dict] = {g: {} for g in KNOWN_PARCELS}
    error_lines:   list[str]       = []
    t_start = time.perf_counter()

    for i, geo_id in enumerate(geo_ids):
        # Progress line every 50 parcels
        if i > 0 and i % 50 == 0:
            elapsed_so_far = time.perf_counter() - t_start
            rate           = elapsed_so_far / i
            eta_min        = (len(geo_ids) - i) * rate / 60
            print(
                f"  [{i:>5,}/{len(geo_ids):,}]  "
                f"found={stats['found']:,}  "
                f"errors={stats['errors']:,}  "
                f"{rate:.2f}s/req  "
                f"ETA {eta_min:.0f}m"
            )

        # ── Fetch (with retry for transient errors) ────────────────────────────
        html, status = None, HTTP_NETWORK_ERR
        for attempt in range(MAX_RETRIES):
            html, status = fetch_html(geo_id)
            if html is not None:
                break                            # success
            if status == HTTP_NOT_FOUND:
                break                            # 404 → no data, don't retry
            if status == HTTP_RATE_LIMIT:
                wait = RATE_LIMIT_BACKOFF * (attempt + 1)
                print(f"  [rate-limit] 429 received — waiting {wait}s before retry …")
                time.sleep(wait)
            else:
                # Network error / 5xx — short pause, then retry
                time.sleep(DELAY_MIN * (attempt + 1))

        stats["scraped"] += 1

        if html is None:
            if status == HTTP_NOT_FOUND:
                # Account not in portal — not a true error, just no data
                pass
            else:
                stats["errors"] += 1
                error_lines.append(f"{geo_id}: HTTP {status}")
            completed.append(geo_id)
            time.sleep(DELAY_MIN)
            continue

        # ── Parse ──────────────────────────────────────────────────────────────
        receipts = parse_receipts(html)
        target   = [r for r in receipts if r["tax_year"] in TARGET_YEARS]

        # Capture all years for known-parcel report (including 2025)
        if geo_id in known_results:
            known_results[geo_id] = {r["tax_year"]: r["payment_amount"] for r in receipts}

        # ── Upsert ────────────────────────────────────────────────────────────
        if target:
            stats["found"] += 1
            records = [
                {
                    "geo_id":    geo_id,
                    "tax_year":  r["tax_year"],
                    "total_paid": r["payment_amount"],
                    "county_code": args.county,
                }
                for r in target
            ]
            try:
                # BILLING-DIAG-7: upsert_billing_rows() now returns the real
                # row count it wrote -- use that instead of len(records) as
                # the single source of truth. The explicit conn.rollback()
                # below is now redundant with the function's own internal
                # `with conn:` auto-rollback (harmless to call twice; kept
                # for clarity and because this loop's own error accounting
                # depends on reaching this except block regardless).
                stats["inserted"] += upsert_billing_rows(conn, records)
            except Exception as exc:
                stats["errors"] += 1
                err_msg = f"{geo_id}: DB error — {exc!r}"
                error_lines.append(err_msg)
                print(f"  [error] {err_msg}")
                conn.rollback()

        completed.append(geo_id)

        # ── Checkpoint every 1,000 parcels ────────────────────────────────────
        if len(completed) % CHECKPOINT_INTERVAL == 0:
            save_checkpoint(completed, stats)
            print(f"  [checkpoint] {len(completed):,} parcels saved to checkpoint.")

        # ── Polite rate limit ─────────────────────────────────────────────────
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # Final checkpoint
    save_checkpoint(completed, stats)

    elapsed = time.perf_counter() - t_start

    # ── Final report ──────────────────────────────────────────────────────────
    print_report(stats, known_results, len(geo_ids), elapsed)

    # ── Write error log if any errors occurred ────────────────────────────────
    if error_lines:
        with open(ERROR_LOG_FILE, "a") as f:
            f.write(f"\n--- Run {datetime.utcnow().isoformat()}Z ---\n")
            f.write("\n".join(error_lines) + "\n")
        print(f"\n  Error log appended to: {ERROR_LOG_FILE}")

    conn.close()
    print("\n  Done.")


if __name__ == "__main__":
    main()
