"""
Backfill classi_cd from IMP_INFO.TXT (2025 Certified + 2026 Preliminary).

IMP_INFO.TXT field positions (0-based, fixed-width, 114 chars/row):
  [0:12]   prop_id   (TCAD integer account)
  [12:16]  tax_year
  [16:28]  impr_id   (improvement segment ID)
  [28:38]  classi_cd (10 chars, numeric code left-justified, trailing spaces)
  [38:63]  impr_desc (25 chars)
  [63:68]  state_cd  (5 chars)
  [68]     condition flag
  [69:84]  impr_value (15 chars, decimal like "0087628.000000")

Strategy: for each parcel, pick the improvement row with the highest value
and a non-"00" classi_cd as the "primary" use code. Falls back to "00" only
if no better row exists.

Run AFTER migrate_add_classi_cd.py:
  cd ~/Desktop/Claude\ Files/parcel_app
  python3 loaders/backfill_classi_cd.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn

import psycopg2.extras


def load_prop_id_lookup(conn):
    """Return {prop_id: geo_id} from parcels already in the DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT prop_id, geo_id FROM parcel WHERE prop_id IS NOT NULL")
        return {row[0]: row[1] for row in cur.fetchall()}


def _float_val(line, s):
    try:
        return float(line[s].strip()) if line[s].strip() else 0.0
    except (ValueError, IndexError):
        return 0.0


def _int_val(line, s):
    try:
        return int(line[s].strip()) if line[s].strip() else None
    except (ValueError, IndexError):
        return None


def build_classi_map(imp_info_path, label=""):
    """
    Read IMP_INFO.TXT and return {prop_id: classi_cd} selecting the
    highest-value non-"00" improvement per parcel.
    """
    print(f"  Reading {label}: {os.path.basename(imp_info_path)} …")
    t0 = time.time()

    # {prop_id: (best_classi_cd, best_value)}
    best = {}
    rows_read = 0

    with open(imp_info_path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 84:
                continue
            prop_id_raw = _int_val(line, slice(0, 12))
            if prop_id_raw is None:
                continue
            classi = line[28:38].strip()
            if not classi:
                continue
            value = _float_val(line, slice(69, 84))
            rows_read += 1

            prev_classi, prev_val = best.get(prop_id_raw, ("", -1))
            # Prefer non-"00" codes; among those, prefer highest value
            if prev_classi == "" or (
                classi != "00" and (prev_classi == "00" or value > prev_val)
            ):
                best[prop_id_raw] = (classi, value)

    print(f"    → {rows_read:,} rows → {len(best):,} unique parcels ({time.time()-t0:.1f}s)")
    return {pid: cc for pid, (cc, _) in best.items()}


def apply_classi_cd(conn, prop_id_to_classi, pid_lookup, label=""):
    """UPDATE parcel.classi_cd for all parcels in the map."""
    update_sql = "UPDATE parcel SET classi_cd = %s WHERE geo_id = %s"
    rows = []
    missed = 0
    for prop_id, classi in prop_id_to_classi.items():
        geo_id = pid_lookup.get(prop_id)
        if geo_id:
            rows.append((classi, geo_id))
        else:
            missed += 1

    if not rows:
        print(f"  {label}: no rows to update.")
        return 0

    print(f"  Updating {len(rows):,} parcels ({missed:,} prop_ids not in DB) …")
    t0 = time.time()
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, update_sql, rows, page_size=5000)
    conn.commit()
    print(f"    → committed in {time.time()-t0:.1f}s")
    return len(rows)


def main():
    conn = get_conn()
    print("=" * 64)
    print("  Backfilling classi_cd from IMP_INFO.TXT")
    print("=" * 64)

    pid_lookup = load_prop_id_lookup(conn)
    print(f"  Parcel prop_id lookup: {len(pid_lookup):,} entries\n")

    total = 0

    # ── 2025 Certified ────────────────────────────────────────────────
    cert_imp = os.path.join(config.CERT_DIR, "IMP_INFO.TXT")
    if os.path.exists(cert_imp):
        m = build_classi_map(cert_imp, "2025 Certified")
        n = apply_classi_cd(conn, m, pid_lookup, "2025")
        total += n
    else:
        print(f"  !! 2025 IMP_INFO.TXT not found: {cert_imp}")

    print()

    # ── 2026 Preliminary ──────────────────────────────────────────────
    prelim_imp = os.path.join(config.PRELIM_2026_DIR, "IMP_INFO.TXT")
    if os.path.exists(prelim_imp):
        m = build_classi_map(prelim_imp, "2026 Preliminary")
        # Only update parcels that don't yet have a classi_cd (don't overwrite 2025)
        with conn.cursor() as cur:
            cur.execute("SELECT geo_id, prop_id FROM parcel WHERE classi_cd IS NULL AND prop_id IS NOT NULL")
            null_pids = {row[1]: row[0] for row in cur.fetchall()}
        filtered = {pid: cc for pid, cc in m.items() if pid in null_pids}
        print(f"  2026 fill-in: {len(filtered):,} parcels without 2025 classi_cd")
        update_sql = "UPDATE parcel SET classi_cd = %s WHERE geo_id = %s"
        rows_2026 = [(cc, null_pids[pid]) for pid, cc in filtered.items()]
        if rows_2026:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, update_sql, rows_2026, page_size=5000)
            conn.commit()
            print(f"    → committed {len(rows_2026):,} rows")
            total += len(rows_2026)
    else:
        print(f"  !! 2026 IMP_INFO.TXT not found: {prelim_imp}")

    print()
    print(f"  Total parcels updated: {total:,}")

    # ── Verification ──────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT classi_cd, COUNT(*) AS n
            FROM parcel
            WHERE classi_cd IS NOT NULL
            GROUP BY classi_cd
            ORDER BY n DESC
            LIMIT 15
        """)
        rows = cur.fetchall()
    print(f"\n  Top classi_cd values:")
    print(f"  {'code':<8} {'count':>9}")
    for r in rows:
        print(f"  {r[0]:<8} {r[1]:>9,}")

    # Spot-check 3 known parcels
    with conn.cursor() as cur:
        cur.execute("""
            SELECT geo_id, state_cd1, classi_cd
            FROM parcel
            WHERE geo_id IN ('0100030105','0100030109','0284460113')
            ORDER BY geo_id
        """)
        spot = cur.fetchall()
    print(f"\n  Spot-check parcels:")
    print(f"  {'geo_id':<14} {'state_cd1':<12} {'classi_cd':<10}")
    for r in spot:
        print(f"  {r[0]:<14} {r[1]:<12} {r[2] or '(null)':<10}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
