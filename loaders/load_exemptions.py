"""
load_exemptions.py — populate parcel_tax_year.exemption_codes CORRECTLY.

Authoritative source: the TCAD Appraisal Export layout (TP_Legacy 8.0.32).
Exemptions in PROP.TXT are single-character 'T'/'F' FLAGS at fixed byte
positions (NOT the numeric amounts the earlier loader mistakenly read near
position 298). This reads those flags and writes a comma-separated exemption
code string per parcel-year — including the Solar / wind-powered energy
exemption (Tax Code 11.27, field `so_exempt`).

Positions below are 1-based (as in the layout doc); we index rec[p-1].

Run:
    python3 loaders/load_exemptions.py            # 2025 certified + 2026 preliminary
    python3 loaders/load_exemptions.py --year 2026
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn
import psycopg2.extras

# code -> 1-based byte position of its 'T'/'F' flag in PROP.TXT
EXEMPTION_FLAGS = [
    ("HS", 2609), ("OV65", 2610), ("OV65S", 2661), ("DP", 2662),
    ("DV1", 2663), ("DV1S", 2664), ("DV2", 2665), ("DV2S", 2666),
    ("DV3", 2667), ("DV3S", 2668), ("DV4", 2669), ("DV4S", 2670),
    ("EX", 2671), ("LVE", 2722), ("AB", 2723), ("EN", 2724), ("FR", 2725),
    ("HT", 2726), ("PRO", 2727), ("PC", 2728),
    ("SO", 2729),                      # Solar / wind-powered energy (Tax Code 11.27)
    ("EX366", 2730), ("CH", 2731),
    ("ECO", 5342), ("CHODO", 5408), ("LIH", 5433), ("GIT", 5434), ("DPS", 5435),
    ("DVHS", 5463), ("CLT", 7184), ("DVHSS", 7239),
    ("CCF", 9083), ("MED", 9138),
]

# Cert export dir per year (only versions matching the 8.0.32 layout)
YEAR_DIRS = {
    2025: config.CERT_DIR,
    2026: config.PRELIM_2026_DIR,
}


def _codes_for(rec):
    n = len(rec)
    return [c for c, p in EXEMPTION_FLAGS if n >= p and rec[p - 1:p].upper() == "T"]


def load_year(conn, year):
    cert_dir = YEAR_DIRS.get(year)
    if not cert_dir:
        print(f"  No export dir configured for {year}; skipping.")
        return 0
    prop_txt = os.path.join(cert_dir, "PROP.TXT")
    if not os.path.exists(prop_txt):
        print(f"  {prop_txt} not found; skipping {year}.")
        return 0
    print(f"  [{year}] scanning {prop_txt} ({os.path.getsize(prop_txt)/1e9:.1f} GB)…")
    t0 = time.time()
    seen = {}   # geo_id -> exemption_codes (last sup_num=0 record wins)
    with open(prop_txt, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 556:
                continue
            try:
                if int(line[22:34].strip()) != 0:   # sup_num 0 only
                    continue
            except ValueError:
                continue
            geo = line[546:556].strip()
            if not geo:
                continue
            codes = _codes_for(line)
            seen[geo] = ",".join(codes) if codes else None

    rows = [(ex, geo, year) for geo, ex in seen.items()]
    n_with = sum(1 for r in rows if r[0])
    print(f"    parsed {len(rows):,} parcels ({n_with:,} with ≥1 exemption) in {time.time()-t0:.1f}s")

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE parcel_tax_year SET exemption_codes = %s WHERE geo_id = %s AND tax_year = %s",
            rows, page_size=5000)
    conn.commit()
    # quick count of solar
    solar = sum(1 for r in rows if r[0] and "SO" in r[0].split(","))
    print(f"    updated parcel_tax_year.exemption_codes for {year}; {solar:,} parcels carry the Solar (SO) exemption.")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, help="Only this year (default: 2025 and 2026)")
    args = ap.parse_args()
    years = [args.year] if args.year else [2025, 2026]
    conn = get_conn()
    try:
        for y in years:
            load_year(conn, y)
        # Sanity sample
        print("\n  Sanity — exemption_codes after load:")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT geo_id, tax_year, exemption_codes
                FROM parcel_tax_year
                WHERE geo_id IN ('0426280206','0159180227') AND tax_year IN (2025,2026)
                ORDER BY geo_id, tax_year
            """)
            for r in cur.fetchall():
                print(f"    {r['geo_id']}  {r['tax_year']}  -> {r['exemption_codes']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
