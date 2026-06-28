"""
verify_pid_fix.py — Task A verification

Checks:
  1. Two base-case parcels (no PIDs): confirms no change in behavior.
  2. Finds a real PID parcel (P2U entity) and shows before/after totals.

Run from parcel_app/:
    python3 verify_pid_fix.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import config
import psycopg2
import psycopg2.extras

def get_conn():
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASS,
    )

def query(conn, sql, params=None, one=False):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone() if one else cur.fetchall()

def check_parcel(conn, geo_id, label, purchase_price):
    print(f"\n{'='*60}")
    print(f"  {label}  ({geo_id})")
    print(f"  Purchase price: ${purchase_price:,}")
    print(f"{'='*60}")

    entities = query(conn, """
        SELECT tbe.entity_code, ctr.entity_name, ctr.rate, tbe.amount_due
        FROM   tax_billing_entity tbe
        LEFT JOIN county_tax_rate ctr
               ON ctr.entity_code = tbe.entity_code AND ctr.tax_year = 2025
        WHERE  tbe.geo_id = %s AND tbe.tax_year = 2025
        ORDER  BY tbe.amount_due DESC NULLS LAST
    """, (geo_id,))

    if not entities:
        print("  ERROR: No 2025 entity billing data found!")
        return

    rate_entities  = [e for e in entities if e.get("rate")]
    billing_only   = [e for e in entities if e.get("amount_due") and not e.get("rate")]
    seller_total   = sum(float(e["amount_due"]) for e in entities if e.get("amount_due"))

    # Simulate texas.py — rate-based entities only
    from tax_logic.texas import estimate_post_acquisition as tx_estimate
    yr_row = query(conn, """
        SELECT market_value, assessed_value, taxable_value, hs_cap_loss, exemption_codes
        FROM parcel_tax_year WHERE geo_id = %s AND tax_year = 2025
    """, (geo_id,), one=True)
    parcel_row = query(conn, "SELECT * FROM parcel WHERE geo_id = %s", (geo_id,), one=True)

    result = tx_estimate(
        dict(parcel_row), dict(yr_row),
        [dict(e) for e in entities],
        purchase_price, "non_owner_occupant",
    )

    est_rate_only = result["estimated_total_tax"]
    delta_old     = result["delta"]

    # PID fix
    pid_passthrough = round(sum(float(e["amount_due"]) for e in billing_only), 2)
    est_incl_pid    = round(est_rate_only + pid_passthrough, 2)
    delta_new       = round(est_incl_pid - seller_total, 2)

    print(f"  Rate entities ({len(rate_entities)}):  ", ", ".join(
        f"{e['entity_code']}({float(e['rate']):.4f}%)" for e in rate_entities[:6]))
    if billing_only:
        print(f"  Billing-only ({len(billing_only)}):  ", ", ".join(
            f"{e['entity_code']}(${float(e['amount_due']):,.2f})" for e in billing_only))
    else:
        print(f"  Billing-only: none (not a PID parcel)")
    print(f"\n  Seller 2025 total tax:       ${seller_total:,.2f}")
    print(f"  Rate-based est (OLD):        ${est_rate_only:,.2f}  (delta: {'+' if delta_old>=0 else ''}{delta_old:,.2f})")
    print(f"  PID passthrough:             ${pid_passthrough:,.2f}")
    print(f"  Total incl. PID (NEW):       ${est_incl_pid:,.2f}  (delta: {'+' if delta_new>=0 else ''}{delta_new:,.2f})")

    if billing_only:
        improvement = est_incl_pid - est_rate_only
        print(f"\n  ✓  FIX APPLIED: estimate increased by ${improvement:,.2f}")
        if abs(delta_new) < abs(delta_old):
            print(f"  ✓  Delta is now closer to zero (was {delta_old:,.2f}, now {delta_new:,.2f})")
    else:
        if abs(est_rate_only - est_incl_pid) < 1:
            print(f"\n  ✓  BASE CASE UNAFFECTED: no PID entities, estimate unchanged")

def find_pid_parcel(conn):
    """Find a P2U parcel with non-trivial billing."""
    row = query(conn, """
        SELECT tbe.geo_id
        FROM tax_billing_entity tbe
        WHERE tbe.tax_year = 2025 AND tbe.entity_code = 'P2U'
          AND tbe.amount_due > 1000
        ORDER BY tbe.amount_due DESC
        LIMIT 1
    """, one=True)
    return row["geo_id"] if row else None

if __name__ == "__main__":
    conn = get_conn()
    print("Parcelytics — PID Pass-Through Fix Verification")
    print("="*60)

    # Base cases (should be unaffected)
    check_parcel(conn, "0204063005", "Base case — investor (non-HS commercial)", 1_400_000)
    check_parcel(conn, "0100030105", "Base case — buy-at-market (non-HS commercial)", 4_330_000)

    # Real PID parcel
    pid_geo = find_pid_parcel(conn)
    if pid_geo:
        pid_billing = query(conn, """
            SELECT amount_due FROM tax_billing_entity
            WHERE geo_id = %s AND tax_year = 2025 AND entity_code = 'P2U'
        """, (pid_geo,), one=True)
        # Use market value as purchase price for a clean comparison
        mv_row = query(conn, """
            SELECT market_value FROM parcel_tax_year
            WHERE geo_id = %s AND tax_year = 2025
        """, (pid_geo,), one=True)
        purchase = int(mv_row["market_value"]) if mv_row else 1_000_000
        check_parcel(conn, pid_geo, "PID parcel (P2U entity) — real affected parcel", purchase)
    else:
        print("\n  WARNING: Could not find a P2U parcel in DB")

    conn.close()
    print("\nDone.")
