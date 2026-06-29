"""
load_parcel_attrs.py — Populate parcel size/age attributes for the Property Info
card (Task 4) and the per-SF benchmark (Task 6).

Adds and fills three columns on `parcel` (additive, safe to re-run):
  land_sqft               — total land area in SF, summed from LAND_DET.area_sf
  year_built              — main improvement's actual year built (from IMP_DET)
  gross_building_area_sqft — sum of ALL IMP_DET component areas for the parcel

Reliability notes (data-integrity standard):
  • land_sqft  — RELIABLE. LAND_DET[83:97] is always square feet regardless of the
    parcel's pricing unit (SF / AC / LOT / FF). Acres = land_sqft / 43,560.
  • year_built — RELIABLE. IMP_DET[85:89]; we take the year of the largest
    living-area component (the main structure); values outside 1800–2026 ignored.
  • gross_building_area_sqft — PROVISIONAL. Summed across every IMP_DET component
    because the TCAD component-code dictionary that separates enclosed building
    area from site improvements (paving, canopies) is not in this repo. The
    per-code area report printed at the end lets you build an exclusion set and
    refine GROSS_EXCLUDE_CODES below. Until then this is "total improvement-detail
    area," and the UI labels its $/SF accordingly — never presented as a precise,
    verified gross building area.

living_area_sqft (Main Area) is loaded separately by load_imp_det_sqft.py and is
left untouched here.

Run:
    python3 loaders/migrate_add_sqft.py      # ensures living_area_sqft exists
    python3 loaders/load_imp_det_sqft.py     # Main Area (living)
    python3 loaders/load_parcel_attrs.py     # land_sqft, year_built, gross area
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn
from loaders.load_imp_det_sqft import _build_prop_id_map, LIVING_AREA_CODES

import psycopg2.extras

# Component codes to EXCLUDE from the gross-building-area sum. Empty by default —
# populate from the per-code report (e.g. paving/site codes) to refine gross area.
# Gross Building Area = sum of ENCLOSED building-component areas from IMP_DET.
# We exclude site improvements and SF-measured *attributes* (which duplicate or
# don't represent enclosed floor area), identified by IMP_DET's own component
# description [50:75]. Matching by description keyword is robust to code variants.
#
# Excluded categories (with the codes observed in the 2025 export):
#   HVAC area (duplicates conditioned floor area):  095, 093
#   Fire-sprinkler coverage (attribute):            491
#   Masonry trim (attribute):                       591
#   Paving / private streets (site):                551, 438
#   Open porches (unenclosed):                      011, 012, 011C, 012C, 013C
#   Canopy / carport (roof, no walls):              501, 051, 061
#   Decks & terraces (outdoor):                     512, 611, 612, 613
#   Tennis / sport courts (site):                   412, 450
#   Pools / spas / fences (site, if present)
# KEPT as building area: all floors (1ST–5TH, MEZZ, ADDL, RSBLW, FBSMT),
#   garages (041/031), storage (571/581), barns (301), utility buildings (298),
#   commercial finish-out (881), parking-garage structures (187/387/487),
#   and sketch-only building area (SO).
GROSS_EXCLUDE_KEYWORDS = (
    "HVAC", "SPRINKLER", "PAVED", "STREET", "PORCH OPEN",
    "DECK", "TERRACE", "CANOPY", "CARPORT", "COURT",
    "MASONRY TRIM", "POOL", "SPA", "FENCE",
    # "Sketch Only" (code SO) is an unconfirmed appraiser outline. Investigation of
    # the 2025 export: of 14,890 parcels with SO, 99% also carry confirmed itemized
    # floors (1ST/2ND/…), and 1,281 show SO ≈ the floor area — i.e. SO overlaps/
    # duplicates the confirmed detail. Summing it on top would inflate gross, so we
    # build gross only from confirmed enclosed components. (190 SO-only parcels,
    # ~1.2M SF / 0.8% of SO area, will show no gross — the honest "Not Available".)
    "SKETCH ONLY",
)


def _is_excluded_from_gross(desc):
    d = (desc or "").upper()
    return any(k in d for k in GROSS_EXCLUDE_KEYWORDS)


def _excl_category(desc):
    """Group an excluded component into a human-readable category for the UI note."""
    d = (desc or "").upper()
    if "PAVED" in d or "STREET" in d:  return "surface parking / paving"
    if "HVAC" in d:                    return "HVAC area (duplicate of conditioned floor area)"
    if "SPRINKLER" in d:               return "sprinkler coverage"
    if "CANOPY" in d:                  return "canopy"
    if "CARPORT" in d:                 return "carport"
    if "PORCH" in d:                   return "open porch"
    if "DECK" in d:                    return "deck"
    if "TERRACE" in d:                 return "terrace"
    if "COURT" in d:                   return "sport / tennis court"
    if "POOL" in d or "SPA" in d:      return "pool / spa"
    if "FENCE" in d:                   return "fencing"
    if "MASONRY TRIM" in d:            return "masonry trim"
    if "SKETCH" in d:                  return "unconfirmed sketch area"
    return "other site improvement"


def _build_excl_detail(cat_areas):
    """Top excluded categories as a compact string, e.g.
    '17,100 SF surface parking / paving, 402 SF canopy'."""
    top = sorted(cat_areas.items(), key=lambda x: -x[1])[:3]
    return ", ".join(f"{a:,.0f} SF {cat}" for cat, a in top)


MIGRATION = """
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS land_sqft                 NUMERIC(14, 2);
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS year_built                SMALLINT;
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS gross_building_area_sqft  NUMERIC(12, 2);
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS gross_excluded_sqft       NUMERIC(12, 2);
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS gross_excluded_detail     TEXT;
ALTER TABLE parcel ADD COLUMN IF NOT EXISTS imp_det_json              TEXT;
"""

SANITY = ["0100030105", "0100030109", "0284460113", "0204140408", "0133040418"]


def _f(s):
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None


def migrate(conn):
    with conn.cursor() as cur:
        cur.execute(MIGRATION)
    conn.commit()
    print("  Schema: land_sqft, year_built, gross_building_area_sqft ensured on parcel.")


def load_land(cert_dir, prop_map):
    """Sum LAND_DET.area_sf (always SF) per parcel."""
    path = os.path.join(cert_dir, "LAND_DET.TXT")
    print(f"  Scanning LAND_DET.TXT ({os.path.getsize(path)/1e6:.0f} MB)…")
    land_by_geo = {}
    t0 = time.time()
    with open(path, encoding="latin-1", errors="replace") as f:
        for line in f:
            if len(line) < 154:
                continue
            area = _f(line[83:97])
            if not area or area <= 0:
                continue
            geo = prop_map.get(line[0:12].strip())
            if geo:
                land_by_geo[geo] = land_by_geo.get(geo, 0.0) + area
    print(f"    → {len(land_by_geo):,} parcels with land area in {time.time()-t0:.1f}s")
    return land_by_geo


def load_imp(cert_dir, prop_map):
    """Per parcel: gross area (sum all components) + year_built (main structure)."""
    path = os.path.join(cert_dir, "IMP_DET.TXT")
    print(f"  Scanning IMP_DET.TXT ({os.path.getsize(path)/1e9:.1f} GB)…")
    gross_by_prop = {}
    excl_by_prop = {}        # pid -> total excluded SF
    excl_cat_by_prop = {}    # pid -> {category: SF}
    comp_by_prop = {}        # pid -> {code: total SF}  (all components, for detail table)
    code_desc = {}           # code -> description (shared, small)
    # year of the single largest living-area component per parcel:
    best_living_area = {}
    year_by_prop = {}
    code_area = {}      # per-code area KEPT in gross (with description)
    excl_area = 0.0     # total SF excluded as site/attribute
    t0 = time.time()
    with open(path, encoding="latin-1", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if len(line) < 103:
                continue
            code = line[40:50].strip()
            desc = line[50:75].strip()
            area = _f(line[93:103]) or 0.0
            if area > 0:
                pid = line[0:12].strip()
                cb = comp_by_prop.setdefault(pid, {})
                cb[code] = cb.get(code, 0.0) + area
                if code not in code_desc:
                    code_desc[code] = desc
                if _is_excluded_from_gross(desc):
                    excl_area += area
                    excl_by_prop[pid] = excl_by_prop.get(pid, 0.0) + area
                    cats = excl_cat_by_prop.setdefault(pid, {})
                    cat = _excl_category(desc)
                    cats[cat] = cats.get(cat, 0.0) + area
                else:
                    gross_by_prop[pid] = gross_by_prop.get(pid, 0.0) + area
                    code_area[code] = (code_area.get(code, [desc, 0.0])[0], code_area.get(code, [desc, 0.0])[1] + area)
            if code in LIVING_AREA_CODES and area > 0:
                yr = None
                try:
                    yr = int(line[85:89].strip())
                except ValueError:
                    yr = None
                if yr and 1800 <= yr <= 2026:
                    pid = line[0:12].strip()
                    if area > best_living_area.get(pid, 0.0):
                        best_living_area[pid] = area
                        year_by_prop[pid] = yr
            if lineno % 2_000_000 == 0:
                print(f"    … {lineno/1e6:.0f}M rows")
    kept_area = sum(v[1] for v in code_area.values())
    print(f"    → {len(gross_by_prop):,} parcels with enclosed building area in {time.time()-t0:.1f}s")
    print(f"    → gross area kept: {kept_area:,.0f} SF | excluded (site/attribute): {excl_area:,.0f} SF "
          f"({excl_area/(kept_area+excl_area)*100:.0f}% of all components)")

    gross_by_geo = {prop_map[p]: a for p, a in gross_by_prop.items() if p in prop_map}
    year_by_geo  = {prop_map[p]: y for p, y in year_by_prop.items()  if p in prop_map}
    excl_by_geo  = {prop_map[p]: a for p, a in excl_by_prop.items()  if p in prop_map}
    detail_by_geo = {prop_map[p]: _build_excl_detail(c)
                     for p, c in excl_cat_by_prop.items() if p in prop_map}

    # Per-parcel improvement-detail list (JSON), sorted by SF desc, for the
    # collapsible "Improvement Detail" table on the property page.
    impdet_by_geo = {}
    for p, codes in comp_by_prop.items():
        geo = prop_map.get(p)
        if not geo:
            continue
        rows = []
        for code, sf in codes.items():
            d = code_desc.get(code, "")
            rows.append({"code": code, "desc": d, "sqft": round(sf, 0),
                         "excluded": _is_excluded_from_gross(d)})
        rows.sort(key=lambda r: -r["sqft"])
        impdet_by_geo[geo] = json.dumps(rows)
    return gross_by_geo, year_by_geo, code_area, excl_by_geo, detail_by_geo, impdet_by_geo


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate-only", action="store_true",
                    help="Just add the columns (instant) so routes don't error; skip the load.")
    args = ap.parse_args()

    cert_dir = config.CERT_DIR
    conn = get_conn()
    try:
        migrate(conn)
        if args.migrate_only:
            print("  --migrate-only: columns ensured, skipping data load.")
            return
        prop_map = _build_prop_id_map(cert_dir)
        land_by_geo = load_land(cert_dir, prop_map)
        gross_by_geo, year_by_geo, code_area, excl_by_geo, detail_by_geo, impdet_by_geo = load_imp(cert_dir, prop_map)

        # Merge keys and upsert
        geos = set(land_by_geo) | set(gross_by_geo) | set(year_by_geo) | set(excl_by_geo) | set(impdet_by_geo)
        rows = [(
            round(land_by_geo[g], 2) if g in land_by_geo else None,
            year_by_geo.get(g),
            round(gross_by_geo[g], 2) if g in gross_by_geo else None,
            round(excl_by_geo[g], 2) if g in excl_by_geo else None,
            detail_by_geo.get(g),
            impdet_by_geo.get(g),
            g,
        ) for g in geos]

        print(f"  Upserting {len(rows):,} parcels…")
        t0 = time.time()
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, """
                UPDATE parcel
                   SET land_sqft                = COALESCE(%s, land_sqft),
                       year_built               = COALESCE(%s, year_built),
                       gross_building_area_sqft = COALESCE(%s, gross_building_area_sqft),
                       gross_excluded_sqft      = %s,
                       gross_excluded_detail    = %s,
                       imp_det_json             = %s
                 WHERE geo_id = %s
            """, rows, page_size=5000)
        conn.commit()
        print(f"    → done in {time.time()-t0:.1f}s")

        # Sanity report
        print("\n  Sanity parcels:")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT geo_id, living_area_sqft, gross_building_area_sqft,
                       gross_excluded_sqft, gross_excluded_detail,
                       land_sqft, year_built
                FROM parcel WHERE geo_id = ANY(%s) ORDER BY geo_id
            """, (SANITY,))
            for r in cur.fetchall():
                ac = (float(r["land_sqft"]) / 43560.0) if r["land_sqft"] else None
                print(f"    {r['geo_id']}  main={r['living_area_sqft']}  "
                      f"gross={r['gross_building_area_sqft']}  "
                      f"excluded={r['gross_excluded_sqft']}  "
                      f"land_sf={r['land_sqft']}  acres={('%.3f' % ac) if ac else '—'}  "
                      f"year_built={r['year_built']}")
                if r["gross_excluded_detail"]:
                    print(f"             excluded detail: {r['gross_excluded_detail']}")

        # Per-code area report — components KEPT in gross (with description)
        print("\n  Components KEPT in gross building area (top 25 by SF):")
        for code, (desc, a) in sorted(code_area.items(), key=lambda x: -x[1][1])[:25]:
            print(f"    {code:<8}{desc:<24}{a:>16,.0f} SF")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
