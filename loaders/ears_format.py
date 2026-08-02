"""
loaders/ears_format.py — single source of truth for parsing TCAD's EARS
fixed-width export format (PROP.TXT / PROP_ENT.TXT / LAND_DET.TXT), used by
every certified/preliminary loader (Migration M2,
SPEC_UNIT_MODEL_AND_INGEST_GATE.md §3.4).

Before this fix, the field-slice table (EXEMPTION_FIELDS), the
_int_field/_str_field helpers, and the PROP_ENT streaming per-prop_id
accumulation loop were independently retyped THREE times — byte-identical
in load_certified_2025.py, load_2026_preliminary.py, and
load_certified_historical.py (confirmed by direct comparison of all three
files during this migration). That's exactly the "copy-drift" risk
parcel_filters.py's own docstring warns about: three copies that happen to
agree today have no mechanism keeping them in agreement tomorrow.

One real drift WAS found in the process of consolidating this: the TCO
(Travis County entity) preference check used two different code sets:
  - load_certified_2025.py / load_2026_preliminary.py:  ("100303", "TCO")
  - load_certified_historical.py:                        ("100303", "TCO", "03")
load_certified_historical.py's set is a strict superset. This module
adopts that superset (TCO_ENTITY_CODES below) for ALL callers, which is a
genuine, intentional behavior widening for the 2025/2026 loaders (an
entity_cd of bare "03" on a PROP_ENT row will now be treated as the TCO
row there too, same as it already was for 2022-2024). Flagged here
explicitly per this migration's honesty requirement — this is a judgment
call (use the broader, already-proven-correct set everywhere) not a
silent behavior-preserving refactor.

Design note on "skip ledger" support: every iterator in this module that
reads a source file yields ONE record per line in the file, in order,
with a `skip_reason` field (None if the row parsed as a normal usable
row). This is deliberate — it lets loaders/ingest_gate.py's G1 check
build its conservation ledger (every line classified into exactly one
bucket, counts must sum to total lines) by consuming the *same* iterator
the loaders use, instead of re-scanning the file with separate logic that
could itself drift from what the loaders actually do.

Pure functions only: everything here operates on a file path and yields
plain dicts. No DB access, no config import, no side effects — fully
unit-testable with in-memory fixture strings (see test_ears_format.py).

    from loaders.ears_format import iter_prop_records, iter_prop_ent_aggregates
"""

# ── Minimum line lengths (rows shorter than this can't contain the fields
#    we read; treated as a distinct skip_reason so G1's ledger can count
#    them, matching the `if len(line) < N: continue` guards already used
#    identically across all three source loaders). ──────────────────────
PROP_MIN_LEN     = 600
PROP_ENT_MIN_LEN = 180
LAND_DET_MIN_LEN = 155

# ── PROP.TXT field slices (0-based; confirmed identical across 2022-2026
#    exports by field inspection — see load_certified_2025.py's original
#    docstring, byte-identical in all three loaders' docstrings). ───────
PROP_SLICES = {
    "prop_id":      slice(0, 12),
    "prop_type_cd": slice(12, 17),
    "sup_num":      slice(22, 34),
    "geo_id":       slice(546, 596),
    "owner_id":     slice(596, 608),
    "owner_name":   slice(608, 678),
}

# ── PROP_ENT.TXT field slices (0-based; confirmed identical across all
#    three source loaders). ──────────────────────────────────────────────
PROP_ENT_SLICES = {
    "prop_id":       slice(0, 12),
    "prop_val_yr":   slice(12, 17),
    "sup_num":       slice(17, 29),
    "entity_cd":     slice(53, 63),
    "assessed_val":  slice(148, 163),
    "taxable_val":   slice(163, 178),
    "market_value":  slice(388, 403),
}

# ── LAND_DET.TXT field slices (0-based). ─────────────────────────────────
LAND_DET_SLICES = {
    "prop_id":         slice(0, 12),
    "land_seg_mkt_val": slice(140, 154),
}

# ── Exemption codes derived from non-zero PROP_ENT fields. Byte-identical
#    across all three source loaders — one copy now. ────────────────────
EXEMPTION_FIELDS = [
    ("hs",    slice(298, 313)),
    ("ov65",  slice(313, 328)),
    ("dp",    slice(328, 343)),
    ("dv",    slice(343, 358)),
    ("ab",    slice(178, 193)),
    ("fr",    slice(208, 223)),
    ("ht",    slice(223, 238)),
    ("ch",    slice(373, 388)),
    ("ex366", slice(283, 298)),
]

# Superset TCO (Travis County) entity-code set — see module docstring for
# why this is the historical loader's set applied everywhere, not the
# narrower 2025/2026 pair.
TCO_ENTITY_CODES = frozenset({"100303", "TCO", "03"})


# ── Shared low-level field parsers ───────────────────────────────────────
def int_field(line, s):
    try:
        v = line[s].strip()
        return int(v) if v else None
    except (ValueError, IndexError):
        return None


def str_field(line, s):
    try:
        return line[s].strip() or None
    except IndexError:
        return None


# ── PROP.TXT ──────────────────────────────────────────────────────────────
def iter_prop_lines(path=None, lines=None):
    """
    Yield one dict per line of PROP.TXT, in file order. Either `path` (a
    file opened internally with the loaders' standard latin-1/replace
    encoding) or `lines` (an iterable of already-decoded strings — for
    fixture tests) must be given.

    Every line gets a dict with keys:
      lineno, skip_reason, prop_id, geo_id, prop_type_cd, owner_id,
      owner_name, sup_num

    skip_reason is one of:
      None          — normal, usable row (sup_num == 0, has a geo_id)
      'short_line'  — line shorter than PROP_MIN_LEN
      'supplement'  — sup_num != 0 (not a certified/preliminary Supp-0 row)
      'no_geo_id'   — sup_num == 0 but geo_id field is blank
    When skip_reason is not None, the other fields are best-effort (may be
    None) — callers that only want clean rows should use
    iter_prop_records() instead.
    """
    src = _resolve_lines(path, lines)
    for lineno, line in enumerate(src, 1):
        if len(line) < PROP_MIN_LEN:
            yield {
                "lineno": lineno, "skip_reason": "short_line",
                "prop_id": None, "geo_id": None, "prop_type_cd": None,
                "owner_id": None, "owner_name": None, "sup_num": None,
            }
            continue

        sup_num = int_field(line, PROP_SLICES["sup_num"])
        prop_id = int_field(line, PROP_SLICES["prop_id"])
        geo_id  = (str_field(line, PROP_SLICES["geo_id"]) or "")[:10].strip() or None
        prop_type_cd = str_field(line, PROP_SLICES["prop_type_cd"])
        owner_id     = int_field(line, PROP_SLICES["owner_id"])
        owner_name   = str_field(line, PROP_SLICES["owner_name"])

        if sup_num != 0:
            skip_reason = "supplement"
        elif not geo_id:
            skip_reason = "no_geo_id"
        else:
            skip_reason = None

        yield {
            "lineno": lineno, "skip_reason": skip_reason,
            "prop_id": prop_id, "geo_id": geo_id, "prop_type_cd": prop_type_cd,
            "owner_id": owner_id, "owner_name": owner_name, "sup_num": sup_num,
        }


def iter_prop_records(path=None, lines=None):
    """
    Yield clean PROP.TXT records only (skip_reason is None) — one dict per
    prop_id with keys: prop_id, geo_id, prop_type_cd, owner_id, owner_name,
    sup_num (always 0 for a yielded record, kept for parity with the spec's
    named field list).
    """
    for rec in iter_prop_lines(path, lines):
        if rec["skip_reason"] is not None:
            continue
        yield {
            "prop_id": rec["prop_id"],
            "geo_id": rec["geo_id"],
            "prop_type_cd": rec["prop_type_cd"],
            "owner_id": rec["owner_id"],
            "owner_name": rec["owner_name"],
            "sup_num": rec["sup_num"],
        }


# ── PROP_ENT.TXT ──────────────────────────────────────────────────────────
def iter_prop_ent_lines(path=None, lines=None):
    """
    Yield one dict per line of PROP_ENT.TXT, in file order.

    Keys: lineno, skip_reason, prop_id, year, entity_cd, assessed, taxable,
    market, exemptions_on_line (frozenset of exemption codes with a
    non-zero amount on THIS line — a single prop_id spans multiple lines,
    one per entity, so this is a per-line partial, not the full per-unit
    set; iter_prop_ent_aggregates() unions these across a prop_id's lines).

    skip_reason is one of: None, 'short_line', 'supplement'.
    """
    src = _resolve_lines(path, lines)
    for lineno, line in enumerate(src, 1):
        if len(line) < PROP_ENT_MIN_LEN:
            yield {
                "lineno": lineno, "skip_reason": "short_line", "prop_id": None,
                "year": None, "entity_cd": None, "assessed": None,
                "taxable": None, "market": None, "exemptions_on_line": frozenset(),
            }
            continue

        prop_id = int_field(line, PROP_ENT_SLICES["prop_id"])
        sup_num = int_field(line, PROP_ENT_SLICES["sup_num"])

        if sup_num != 0:
            yield {
                "lineno": lineno, "skip_reason": "supplement", "prop_id": prop_id,
                "year": None, "entity_cd": None, "assessed": None,
                "taxable": None, "market": None, "exemptions_on_line": frozenset(),
            }
            continue

        year      = int_field(line, PROP_ENT_SLICES["prop_val_yr"])
        entity_cd = str_field(line, PROP_ENT_SLICES["entity_cd"])
        assessed  = int_field(line, PROP_ENT_SLICES["assessed_val"])
        taxable   = int_field(line, PROP_ENT_SLICES["taxable_val"])
        market    = int_field(line, PROP_ENT_SLICES["market_value"])

        exemptions = frozenset(
            code.upper() for code, sl in EXEMPTION_FIELDS
            if (int_field(line, sl) or 0) > 0
        )

        yield {
            "lineno": lineno, "skip_reason": None, "prop_id": prop_id,
            "year": year, "entity_cd": entity_cd, "assessed": assessed,
            "taxable": taxable, "market": market, "exemptions_on_line": exemptions,
        }


def iter_prop_ent_aggregates(path=None, lines=None, tco_codes=TCO_ENTITY_CODES):
    """
    Stream PROP_ENT.TXT (sorted by prop_id, per TCAD's export convention)
    and yield ONE accumulated dict per prop_id — the same
    current_pid/accum/flush pattern previously duplicated in all three
    loaders, now in one place.

    Yielded dict keys: prop_id, year, market_value, assessed_value,
    taxable_value, exemption_codes (comma-joined sorted string, or None).

    Per-unit accumulation rules (unchanged from the original loaders):
      market_value    = first non-null market value seen for this prop_id
                         (same across all entity rows for one unit)
      assessed/taxable = from the TCO entity row if present, else the
                         first entity row seen (tco_codes membership check)
      exemption_codes  = union of every non-zero exemption field across
                         every entity row for this prop_id
    """
    current_pid = None
    accum = None

    def _finalize(pid, acc):
        return {
            "prop_id": pid,
            "year": acc.get("year"),
            "market_value": acc.get("market_value"),
            "assessed_value": acc.get("assessed_value"),
            "taxable_value": acc.get("taxable_value"),
            "exemption_codes": ",".join(sorted(acc.get("exemptions", set()))) or None,
        }

    for rec in iter_prop_ent_lines(path, lines):
        if rec["skip_reason"] is not None:
            continue

        prop_id = rec["prop_id"]
        if prop_id != current_pid:
            if current_pid is not None and accum is not None:
                yield _finalize(current_pid, accum)
            current_pid = prop_id
            accum = {"year": rec["year"], "exemptions": set()}

        if rec["market"] and not accum.get("market_value"):
            accum["market_value"] = rec["market"]

        entity_cd = rec["entity_cd"]
        is_tco = bool(entity_cd) and entity_cd.strip().upper() in tco_codes
        if is_tco or not accum.get("assessed_value"):
            accum["assessed_value"] = rec["assessed"]
            accum["taxable_value"] = rec["taxable"]

        accum["exemptions"].update(rec["exemptions_on_line"])

    if current_pid is not None and accum is not None:
        yield _finalize(current_pid, accum)


# ── LAND_DET.TXT ──────────────────────────────────────────────────────────
def iter_land_det_lines(path=None, lines=None):
    """
    Yield one dict per line of LAND_DET.TXT.
    Keys: lineno, skip_reason ('short_line' or None), prop_id, land_seg_mkt_val.
    """
    src = _resolve_lines(path, lines)
    for lineno, line in enumerate(src, 1):
        if len(line) < LAND_DET_MIN_LEN:
            yield {"lineno": lineno, "skip_reason": "short_line",
                   "prop_id": None, "land_seg_mkt_val": None}
            continue
        prop_id = int_field(line, LAND_DET_SLICES["prop_id"])
        val = int_field(line, LAND_DET_SLICES["land_seg_mkt_val"])
        yield {"lineno": lineno, "skip_reason": None,
               "prop_id": prop_id, "land_seg_mkt_val": val}


def land_totals(path=None, lines=None):
    """Sum land_seg_mkt_val per prop_id. Returns {prop_id: total_land_value}."""
    totals = {}
    for rec in iter_land_det_lines(path, lines):
        if rec["skip_reason"] is not None or not rec["prop_id"] or not rec["land_seg_mkt_val"]:
            continue
        totals[rec["prop_id"]] = totals.get(rec["prop_id"], 0) + rec["land_seg_mkt_val"]
    return totals


# ── Shared write-side SQL ─────────────────────────────────────────────────
# Not "pure" in the same sense as the iterators above (these are DB SQL
# strings, not file-parsing logic) but they live here anyway, deliberately:
# the four loaders that write prop_unit / prop_unit_tax_year had the exact
# same copy-drift risk for their UPSERT statements as they did for the
# slice tables — one canonical copy here instead of four independently
# retyped ones, same rationale as the rest of this module.
#
# geo_id guard (fixed 2026-07-29, task M3-GEOID-CORRUPTION-FIX): geo_id
# used to be unconditionally overwritten with EXCLUDED.geo_id on every
# upsert -- load-order dependent, not recency dependent. Confirmed live
# tonight: loading 2022-2024 certified historical data AFTER 2025/2026 had
# already been loaded silently reassigned geo_id for properties whose
# account number changed between those years (replats/subdivisions/
# merges), which retroactively corrupted parcel_rollup.py's rollup for
# EVERY year that joins through prop_unit.geo_id -- 2025's G3 dropped
# ~$1.40B and 2026's dropped ~$2.40B, though the underlying
# prop_unit_tax_year value data was never touched. Guarded the same way
# last_seen_year already was (LEAST/GREATEST): only the row for the most
# recent year seen so far may set geo_id, using >= so same-year re-loads
# (the last one committed wins) keep their current behavior unchanged --
# only cross-year load-order is now protected against.
#
# This makes geo_id correctly mean "latest-known account membership as of
# the most recent year loaded so far", which is what SPEC_UNIT_MODEL_AND_
# INGEST_GATE.md §3.2 originally specified -- it does NOT add true
# per-year historical accuracy (a prop_unit_tax_year row from 2022 still
# has no geo_id of its own, only prop_unit's single latest-known value).
# That deeper gap is unchanged and is still open follow-up work -- see
# KNOWN_LIMITATIONS.md's "item 3: account-tracking granularity gap".
PROP_UNIT_UPSERT_SQL = """
    INSERT INTO prop_unit
        (prop_id, geo_id, prop_type_cd, situs_address, owner_id, owner_name,
         first_seen_year, last_seen_year)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (prop_id) DO UPDATE
        SET geo_id          = CASE
                                  WHEN EXCLUDED.last_seen_year >= prop_unit.last_seen_year
                                  THEN EXCLUDED.geo_id
                                  ELSE prop_unit.geo_id
                              END,
            prop_type_cd    = COALESCE(EXCLUDED.prop_type_cd, prop_unit.prop_type_cd),
            situs_address   = COALESCE(EXCLUDED.situs_address, prop_unit.situs_address),
            owner_id        = COALESCE(EXCLUDED.owner_id, prop_unit.owner_id),
            owner_name      = COALESCE(EXCLUDED.owner_name, prop_unit.owner_name),
            first_seen_year = LEAST(prop_unit.first_seen_year, EXCLUDED.first_seen_year),
            last_seen_year  = GREATEST(prop_unit.last_seen_year, EXCLUDED.last_seen_year)
"""


def resolve_prop_unit_conflict(existing, incoming):
    """
    Pure-Python mirror of PROP_UNIT_UPSERT_SQL's ON CONFLICT DO UPDATE
    conflict-resolution logic, kept in sync by hand -- same division of
    labor as parcel_rollup.py's ROLLUP_SQL vs compute_rollup(), so the
    upsert's actual semantics can be fixture-tested without a live DB
    (there is no DB in this sandbox to execute the real SQL against).

    existing: dict with keys geo_id, prop_type_cd, situs_address, owner_id,
        owner_name, first_seen_year, last_seen_year -- the current
        prop_unit row for this prop_id.
    incoming: same shape -- the row being upserted (SQL's EXCLUDED.*).
    Returns the new row dict prop_unit would contain after this upsert.

    geo_id is guarded by last_seen_year (>=, so a same-year re-load's
    LATER call still wins the tie, matching unchanged same-year
    behavior) -- only a row for a year that is actually >= the most
    recent year already seen may set geo_id. All other columns' logic
    (COALESCE / LEAST / GREATEST) is unchanged from before this fix.
    """
    return {
        "geo_id": (incoming["geo_id"]
                   if incoming["last_seen_year"] >= existing["last_seen_year"]
                   else existing["geo_id"]),
        "prop_type_cd": incoming["prop_type_cd"] if incoming["prop_type_cd"] is not None else existing["prop_type_cd"],
        "situs_address": incoming["situs_address"] if incoming["situs_address"] is not None else existing["situs_address"],
        "owner_id": incoming["owner_id"] if incoming["owner_id"] is not None else existing["owner_id"],
        "owner_name": incoming["owner_name"] if incoming["owner_name"] is not None else existing["owner_name"],
        "first_seen_year": min(existing["first_seen_year"], incoming["first_seen_year"]),
        "last_seen_year": max(existing["last_seen_year"], incoming["last_seen_year"]),
    }

PROP_UNIT_TAX_YEAR_UPSERT_SQL = """
    INSERT INTO prop_unit_tax_year
        (prop_id, tax_year, geo_id, market_value, assessed_value, taxable_value,
         hs_cap_loss, land_value, imprv_value, exemption_codes, data_source)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (prop_id, tax_year) DO UPDATE
        SET geo_id          = EXCLUDED.geo_id,
            market_value    = EXCLUDED.market_value,
            assessed_value  = EXCLUDED.assessed_value,
            taxable_value   = EXCLUDED.taxable_value,
            hs_cap_loss     = EXCLUDED.hs_cap_loss,
            land_value      = EXCLUDED.land_value,
            imprv_value     = EXCLUDED.imprv_value,
            exemption_codes = EXCLUDED.exemption_codes,
            data_source     = EXCLUDED.data_source
"""
# geo_id (Task M5-PERYEAR-GEOID, July 2026): the year's REAL, as-of-that-
# year account assignment -- every caller of this SQL now passes it as the
# 3rd tuple element (right after prop_id, tax_year), sourced from that same
# year's own PROP.TXT/AJR row, NOT from prop_unit.geo_id (see this column's
# own comment in schema.sql for why those are different values). Unlike
# PROP_UNIT_UPSERT_SQL's geo_id (which is guarded by a LEAST/GREATEST-style
# CASE so only the most-recent-year's load may overwrite it), this SQL's
# geo_id is unconditionally overwritten on every upsert -- correct here,
# unlike that guard's context, because this row is scoped to ONE specific
# (prop_id, tax_year), so "the value for this exact year" has no
# cross-year ordering ambiguity to guard against; a re-run of the same
# year's file re-derives the identical value, same reasoning already
# documented on this file's DO UPDATE semantics elsewhere.


# ── Internal ──────────────────────────────────────────────────────────────
def _resolve_lines(path, lines):
    if lines is not None:
        return lines
    if path is None:
        raise ValueError("iter_* functions require either path= or lines=")
    return open(path, encoding="latin-1", errors="replace")
