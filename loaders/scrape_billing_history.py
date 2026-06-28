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
from loaders.db import get_conn
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

# Add data_source and confidence_level columns if they don't exist yet
_ENSURE_COLS_SQL = """
ALTER TABLE tax_billing
    ADD COLUMN IF NOT EXISTS data_source      VARCHAR(32),
    ADD COLUMN IF NOT EXISTS confidence_level VARCHAR(16);
"""

# Upsert: insert or update ONLY if the existing row has no better data source.
# Rows loaded from 'taxcur' or 'pir_billing' are preserved as-is.
_UPSERT_SQL = """
INSERT INTO tax_billing
    (geo_id, tax_year, total_tax, total_paid, data_source, confidence_level)
VALUES
    (%(geo_id)s, %(tax_year)s, %(total_tax)s, %(total_paid)s,
     'portal_scrape', 'partial')
ON CONFLICT (geo_id, tax_year) DO UPDATE
    SET total_tax         = EXCLUDED.total_tax,
        total_paid        = EXCLUDED.total_paid,
        data_source       = EXCLUDED.data_source,
        confidence_level  = EXCLUDED.confidence_level
    WHERE (tax_billing.data_source IS NULL
        OR tax_billing.data_source = 'portal_scrape')
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
) -> list[str]:
    """Return geo_ids that have parcel_tax_year rows for 2021–2024.

    Excludes:
      - AJR* personal-property supplement accounts (no real estate billing)
      - Any geo_id in `exclude` (already processed)

    Args:
        limit:        LIMIT N applied before returning (None = unlimited)
        exclude:      set of geo_ids to skip (for random fill, avoiding known parcels)
        random_order: ORDER BY RANDOM() for random sampling (test mode only)
    """
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
                       AND pty.tax_year BETWEEN 2021 AND 2024
                WHERE  p.geo_id NOT LIKE 'AJR%%'
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
                   AND pty.tax_year BETWEEN 2021 AND 2024
            WHERE  p.geo_id NOT LIKE 'AJR%%'
            {exclude_clause}
            {commercial_clause}
            ORDER BY p.geo_id
            {limit_clause}
        """
    with conn.cursor() as cur:
        cur.execute(sql)
        return [row[0] for row in cur.fetchall()]


def upsert_billing_rows(conn, records: list[dict]) -> None:
    """Upsert a batch of billing records. Raises on DB error (caller rolls back)."""
    if not records:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, _UPSERT_SQL, records, page_size=500)
    conn.commit()


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


def fetch_html(geo_id: str) -> tuple[str | None, int]:
    """Fetch the payment-receipts page for one geo_id.

    Returns (html_string, status) where:
      - status = HTTP_OK (0)           → html is the page content
      - status = HTTP_NOT_FOUND (404)  → account not in portal, html is None
      - status = HTTP_RATE_LIMIT (429) → caller should back off and retry
      - status = HTTP_SERVER_ERR (5xx) → transient server error
      - status = HTTP_NETWORK_ERR (-1) → network/SSL error, html is None

    Portal returns ISO-8859-1 encoded HTML.
    """
    account = geo_id + "0000"   # 10-digit geo_id → 14-digit portal account
    url     = BASE_URL.format(account=account)
    req     = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
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
                    "total_tax": r["payment_amount"],
                    "total_paid": r["payment_amount"],
                }
                for r in target
            ]
            try:
                upsert_billing_rows(conn, records)
                stats["inserted"] += len(records)
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
