#!/usr/bin/env python3
"""
loaders/pir_xlsx_common.py — Shared parsing/aggregation core for the Travis
County Tax Office PIR billing bulk exports (264-column XLSX, one row per
taxing account), used by the thin per-year entry points:
    load_pir_billing_2021_full.py  (already existed before this module)
    load_pir_billing_2022.py
    load_pir_billing_2023.py
    load_pir_billing_2024.py

WHY THIS MODULE EXISTS (July 2026, per Diego's build brief, following the
2022-2024 investigation): 2021 and 2022 use a genuinely different in-file
string encoding than 2023 and 2024 (confirmed by direct byte-level decoding,
not assumed):
  - 2021/2022: shared-string table format. Text cells carry `t="s"` and a
    `<v>N</v>` that's an INDEX into xl/sharedStrings.xml, e.g.
    `<c r="A2" t="s"><v>264</v></c>`. Numeric cells often have no `t=` at
    all, just `<v>NUMBER</v>` directly.
  - 2023/2024: inline-string format. Text cells carry `t="inlineStr"` and the
    value is embedded directly, e.g. `<c r="A2" t="inlineStr"><is><t>010003
    0105 0000</t></is></c>` — there is NO `<v>` tag on these cells at all.
    Numeric cells carry `t="n"` with a normal `<v>NUMBER</v>`.
    xl/sharedStrings.xml is a real-but-empty stub in these two files
    (`<sst count="0" uniqueCount="0"/>`) — confirmed directly, not assumed.
  - A consequence of 2023/2024's format: TXACCYER comes through as a FLOAT
    STRING ("2023.0", "2024.0"), not a clean integer like 2021/2022's "2021"
    /"2022". Comparing this to str(TAX_YEAR) with plain string equality (as
    2021's original loader did, since 2021 never needed to worry about this)
    would silently treat every single row as the wrong tax year and load
    nothing -- see _year_matches() below, which fixes this by parsing both
    sides as float before comparing.

Everything below this point (name-driven column lookup, entity extraction,
majority-vote-by-similarity duplicate resolution, two-tier geo_id
aggregation) is carried over UNCHANGED in behavior from
load_pir_billing_2021_full.py -- only the cell-value decoding step
(parse_header, build_row_cell_regex, iter_rows) was extended to branch on
each cell's real `t=` attribute rather than assuming shared-string-only, so
a single code path now serves all four years. The name-driven column map
means the confirmed slot-3 field-order quirk (TXBASTAX3 before TXENTCOD3 --
present in 2022 and 2023, expected in 2024 per the investigation) was
already a non-issue before this module existed; nothing needed to change
for it here.

Row-number duplication (investigation finding: exactly 2 `<row r="N">` tags
share the same N in each of 2022/2023/2024) is likewise a non-issue for this
module, because rows are keyed by TXACCNUM content during aggregation, never
by their row number -- see load_and_aggregate() below and finding 2 in
load_pir_billing_2021_full.py's original docstring for why that dedup
already exists and needs no change.
"""
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Entity slots 1-10. Field name per slot is looked up dynamically from the
# header row (not hardcoded to a fixed column offset) because the layout is
# NOT perfectly uniform across slots -- confirmed by inspection across all
# four years: slot 5 has no TXTAXDUE5 (has TXTAXAMT5/TXTAXOVR5 instead), no
# TXPENINT5 in 2022 either, and slots 8/9 have no TXATTFEE8/TXATTFEE9 in
# 2021. Looking up each field by name per slot, independently, means a
# missing field for a given slot is just None (handled), never a silent
# off-by-one into the wrong slot's data -- and is also what makes this
# module immune to the confirmed slot-3 BASTAX/ENTCOD column-order swap.
ENTITY_FIELD_TEMPLATES = ["TXENTCOD", "TXBASTAX", "TXTAXDUE", "TXPENINT",
                           "TXATTFEE", "TXAMTTP1", "TXAMTCOL",
                           "TXTAXAMT", "TXTAXOVR"]  # slot-5-only fields


def _f(v):
    """Defensive float parse -- returns None for blank/non-numeric (e.g. the
    literal 'S' status string seen in TXTAXOVR5/TXATTFEE), never raises."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _year_matches(raw_year, tax_year):
    """True if this row's TXACCYER value real-equals tax_year.

    Investigation finding: 2023/2024 store TXACCYER as a float-string
    ("2023.0"), 2021/2022 store it as a clean integer string ("2021"). A
    plain str(raw_year) == str(tax_year) comparison (fine for 2021/2022)
    would silently reject every 2023/2024 row. Comparing as float handles
    both forms uniformly, and any genuinely malformed value just fails the
    try/except and is treated as non-matching (counted, never silently
    dropped -- see n_bad_year in load_and_aggregate)."""
    if raw_year is None or raw_year == "":
        return False
    try:
        return float(raw_year) == float(tax_year)
    except ValueError:
        return False


def load_shared_strings(z):
    """Load xl/sharedStrings.xml if present. For 2023/2024 this is a real
    but empty stub (<sst count="0" uniqueCount="0"/>, confirmed directly) --
    iterparse over it just yields zero <si> elements, which is fine: those
    two files never reference a shared-string index anyway (see
    detect_string_format below)."""
    shared = []
    try:
        f = z.open("xl/sharedStrings.xml")
    except KeyError:
        return shared
    with f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == NS + "si":
                shared.append("".join(t.text or "" for t in elem.iter(NS + "t")))
                elem.clear()
    return shared


def detect_string_format(z):
    """Report which string encoding this file actually uses -- for the
    --inspect report and startup log, NOT to change the decode logic (that
    already branches per-cell on the real t= attribute regardless, so it's
    correct even if a future file mixes both). Checked two independent ways
    so a genuine surprise gets flagged rather than silently guessed:
      1. xl/sharedStrings.xml's real size -- a real table for a 400K+ row
         file is tens of MB (2021: several MB; 2022: 144MB); 2023/2024's
         stub is 138 bytes, confirmed directly.
      2. The first header cell's actual t= attribute.
    If the two disagree, raises rather than guessing -- per this
    investigation's own "don't guess, show real values" discipline.
    """
    try:
        info = z.getinfo("xl/sharedStrings.xml")
        shared_strings_trivial = info.file_size < 10_000
    except KeyError:
        shared_strings_trivial = True

    with z.open("xl/worksheets/sheet1.xml") as f:
        head = f.read(20_000).decode("utf-8", errors="replace")
    m = re.search(r'<c r="[A-Z]+1"[^>]*t="(\w+)"', head)
    sample_ttype = m.group(1) if m else None

    by_size = "inline" if shared_strings_trivial else "shared"
    by_ttype = ("inline" if sample_ttype == "inlineStr"
                else "shared" if sample_ttype == "s" else None)

    if by_ttype and by_ttype != by_size:
        raise RuntimeError(
            f"String-format detection disagreement: sharedStrings.xml size "
            f"suggests '{by_size}' but the first header cell's t=\"{sample_ttype}\" "
            f"suggests '{by_ttype}'. Stop and re-inspect this file by hand -- "
            f"do not guess which is right.")
    return by_size


# Matches EITHER value form a cell can take:
#   <v>TEXT_OR_INDEX_OR_NUMBER</v>              (t missing/"n"/"s"/"str")
#   <is><t optional-attrs>TEXT</t></is>         (t="inlineStr", no <v> at all)
_CELL_VALUE_RE = r'(?:<v>([^<]*)</v>|<is><t(?:[^>]*)>([^<]*)</t></is>)'


def _decode_cell(ttype, v_val, is_val, shared):
    """One shared decision point for turning (t=, <v> match, <is><t> match)
    into a real Python value, used identically by parse_header() (row 1) and
    iter_rows() (every other row) so header and data can never disagree on
    what a given cell type means."""
    if ttype == "s":
        return shared[int(v_val)] if v_val not in (None, "") else None
    if ttype == "inlineStr":
        return is_val
    # t missing (implicit number), t="n", or t="str" (formula-string literal)
    # all carry their real value directly in <v>.
    return v_val


def parse_header(z, shared):
    """Read just enough of sheet1.xml to get row 1, return {field_name: col_letter}.
    Handles both cell-value forms (see _decode_cell) since 2023/2024's own
    header row uses inlineStr, not just their data rows."""
    with z.open("xl/worksheets/sheet1.xml") as f:
        head = f.read(4_000_000).decode("utf-8", errors="replace")
    m = re.search(r'<row r="1"[^>]*>(.*?)</row>', head, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find header row (row 1) in sheet1.xml")
    cell_re = re.compile(
        r'<c r="([A-Z]+)\d+"(?:[^>]*?t="(\w+)")?[^>]*>' + _CELL_VALUE_RE + r'</c>'
    )
    name_to_letter = {}
    for cm in cell_re.finditer(m.group(1)):
        col, ttype, v_val, is_val = cm.group(1), cm.group(2), cm.group(3), cm.group(4)
        name = _decode_cell(ttype, v_val, is_val, shared)
        if name:
            name_to_letter[name] = col
    return name_to_letter


def build_row_cell_regex(needed_letters):
    """One compiled BYTES regex matching any <c> cell whose column letter is
    in needed_letters, for a single row's raw XML substring -- extended from
    the 2021-only version to match either value form (see _CELL_VALUE_RE),
    so the same compiled pattern serves shared-string rows (2021/2022) and
    inline-string rows (2023/2024) without knowing in advance which this
    file is. Compiled as bytes -- row blocks are extracted from the raw
    (streamed, never fully materialized) sheet XML as bytes and never
    decoded to str, since decoding the whole multi-GB stream up front is
    exactly the overhead this approach is designed to avoid."""
    colpat = "|".join(sorted(needed_letters, key=len, reverse=True))
    pattern = (
        rf'<c r="({colpat})\d+"(?:[^>]*?t="(\w+)")?[^>]*>'
        rf'(?:<v>([^<]*)</v>|<is><t(?:[^>]*)>([^<]*)</t></is>)'
        rf'</c>'
    )
    return re.compile(pattern.encode())


def iter_rows(z, shared, name_to_letter):
    """Yield (row_no_str, {field_name: value}) dicts, one per data row (row 2
    onward), streaming the sheet XML in 32MB chunks -- never loading the
    full multi-GB tree. Only extracts the ~45 field names this loader
    actually needs. Identical in structure to the 2021-only version; the
    only change is build_row_cell_regex/the per-cell decode step now
    handling both string-encoding formats."""
    letter_to_name = {}
    needed_names = ["TXACCNUM", "TXACCYER"]
    for slot in range(1, 11):
        for tmpl in ENTITY_FIELD_TEMPLATES:
            needed_names.append(f"{tmpl}{slot}")
    for name in needed_names:
        letter = name_to_letter.get(name)
        if letter:
            letter_to_name[letter] = name

    cell_re = build_row_cell_regex(set(letter_to_name.keys()))
    row_re = re.compile(rb'<row r="(\d+)"[^>]*>(.*?)</row>', re.DOTALL)

    CHUNK = 32 * 1024 * 1024
    tail = b""
    with z.open("xl/worksheets/sheet1.xml") as f:
        first = True
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            matches = list(row_re.finditer(buf))
            if matches:
                tail = buf[matches[-1].end():]
                if len(tail) > 5_000_000:
                    tail = tail[-2_000_000:]
            else:
                tail = buf[-2_000_000:] if len(buf) > 2_000_000 else buf
            for m in matches:
                rownum, block = m.group(1), m.group(2)
                if first:
                    first = False
                    continue  # row 1 is the header, already parsed separately
                row = {}
                for cm in cell_re.finditer(block):
                    letter_bytes, ttype_b, v_val_b, is_val_b = (
                        cm.group(1), cm.group(2), cm.group(3), cm.group(4)
                    )
                    letter = letter_bytes.decode()
                    name = letter_to_name.get(letter)
                    if not name:
                        continue
                    ttype = ttype_b.decode() if ttype_b else None
                    v_val = v_val_b.decode() if v_val_b is not None else None
                    is_val = is_val_b.decode() if is_val_b is not None else None
                    row[name] = _decode_cell(ttype, v_val, is_val, shared)
                yield rownum.decode(), row


def extract_entities(row):
    """Return [(entity_code, amount_due, amount_paid), ...] for one row,
    applying the slot-5 TXTAXDUE-fallback and defensive numeric parsing
    documented in load_pir_billing_2021_full.py's original docstring
    (finding 3). Unchanged across all four years -- entity semantics were
    confirmed identical in the investigation (same field names, same slot-5
    irregularity, present in 2022/2023 and expected in 2024)."""
    out = []
    for slot in range(1, 11):
        code = row.get(f"TXENTCOD{slot}")
        if not code:
            continue
        due = _f(row.get(f"TXTAXDUE{slot}"))
        if due is None:
            due = _f(row.get(f"TXBASTAX{slot}"))
        paid = _f(row.get(f"TXAMTCOL{slot}"))
        if due is None and paid is None:
            continue
        out.append((code, due or 0.0, paid or 0.0))
    return out


def _cluster_by_similarity(occurrences, tolerance=1.00):
    """occurrences: [(row_no, total, entities), ...] all belonging to one
    TXACCNUM. Single-linkage cluster by total-amount similarity. Unchanged
    from load_pir_billing_2021_full.py -- see its docstring finding 2(b) for
    the full reasoning behind the $1.00 tolerance and majority-vote design."""
    ordered = sorted(occurrences, key=lambda o: o[1])
    clusters = [[ordered[0]]]
    for occ in ordered[1:]:
        if occ[1] - clusters[-1][-1][1] <= tolerance:
            clusters[-1].append(occ)
        else:
            clusters.append([occ])
    clusters.sort(key=lambda c: (len(c), max(o[1] for o in c)), reverse=True)
    return clusters


def _resolve_accnum_occurrences(occurrences):
    """occurrences: [(row_no, total, entities, geo_id), ...] for one TXACCNUM
    (length 1 for the common non-duplicated case). Unchanged from
    load_pir_billing_2021_full.py's majority-vote-by-similarity algorithm.

    This is also what makes the investigation's confirmed "2 duplicate
    <row r=N> tags per file" finding a non-issue here: those two rows are
    handled exactly like any other duplicate-TXACCNUM occurrence -- clustered
    by amount similarity and resolved by majority vote / magnitude fallback,
    never assumed-away by row number."""
    geo_id = occurrences[0][3]
    if len(occurrences) == 1:
        row_no, total, entities, _ = occurrences[0]
        return row_no, total, entities, geo_id, "single", 1

    clusters = _cluster_by_similarity([(o[0], o[1], o[2]) for o in occurrences])
    top = clusters[0]
    if len(clusters) == 1 or len(top) > len(clusters[1]):
        method = "majority_vote"
    else:
        method = "magnitude_fallback"

    top_sorted = sorted(top, key=lambda o: o[1])
    median_total = top_sorted[len(top_sorted) // 2][1]
    winner = min(top, key=lambda o: abs(o[1] - median_total))
    row_no, total, entities = winner
    return row_no, total, entities, geo_id, method, len(top)


def load_and_aggregate(filepath, tax_year, progress_every=100_000, row_limit=None):
    """Full parse + two-tier aggregation. Unchanged in structure from
    load_pir_billing_2021_full.py, parameterized by tax_year instead of a
    module-level constant, and using _year_matches() (see module docstring)
    instead of plain string equality so 2023/2024's float-string TXACCYER
    doesn't silently reject every row.

    Returns:
        by_geo: {geo_id: {entity_code: {"due": float, "paid": float}}}
        stats: dict of counts for the final report
        dup_review_rows: [(full_accnum, method, kept_row_no, kept_total,
                            n_occurrences, winning_cluster_size,
                            other_totals), ...]
    """
    t0 = time.time()
    z = zipfile.ZipFile(filepath)
    detected_format = detect_string_format(z)
    print(f"  Detected string format: {detected_format}", flush=True)

    print("  Loading shared strings…", flush=True)
    shared = load_shared_strings(z)
    print(f"    {len(shared):,} shared strings [{time.time()-t0:.1f}s]", flush=True)

    name_to_letter = parse_header(z, shared)
    missing = [n for n in ("TXACCNUM", "TXACCYER", "TXENTCOD1", "TXBASTAX1",
                            "TXTAXDUE1", "TXAMTCOL1") if n not in name_to_letter]
    if missing:
        raise RuntimeError(f"Header is missing expected fields: {missing} -- "
                            f"file layout may have changed, stop and re-inspect.")

    occurrences_by_accnum = defaultdict(list)
    n_rows = 0
    n_bad_accnum = 0
    n_bad_year = 0

    t1 = time.time()
    for row_no, row in iter_rows(z, shared, name_to_letter):
        n_rows += 1
        accnum = row.get("TXACCNUM")
        if not accnum or len(accnum) != 14 or not accnum.isdigit():
            n_bad_accnum += 1
            continue
        year = row.get("TXACCYER")
        if not _year_matches(year, tax_year):
            n_bad_year += 1
            continue

        entities = extract_entities(row)
        total = sum(d for _, d, _ in entities)
        geo_id = accnum[:10]
        occurrences_by_accnum[accnum].append((row_no, total, entities, geo_id))

        if n_rows % progress_every == 0:
            print(f"    … {n_rows:,} rows parsed [{time.time()-t1:.1f}s]", flush=True)
        if row_limit and n_rows >= row_limit:
            print(f"    (stopped at --limit {row_limit:,} rows)", flush=True)
            break

    print(f"  Parsed {n_rows:,} rows [{time.time()-t1:.1f}s]  "
          f"({n_bad_accnum:,} bad TXACCNUM format, {n_bad_year:,} wrong tax year)")

    best_by_accnum = {}
    dup_review_rows = []
    n_majority_vote = 0
    n_magnitude_fallback = 0
    for accnum, occs in occurrences_by_accnum.items():
        row_no, total, entities, geo_id, method, cluster_size = _resolve_accnum_occurrences(occs)
        best_by_accnum[accnum] = (total, row_no, entities, geo_id)
        if len(occs) > 1:
            other_totals = [o[1] for o in occs if o[0] != row_no]
            dup_review_rows.append((accnum, method, row_no, total, len(occs),
                                     cluster_size, other_totals))
            if method == "majority_vote":
                n_majority_vote += 1
            else:
                n_magnitude_fallback += 1

    by_geo = defaultdict(lambda: defaultdict(lambda: {"due": 0.0, "paid": 0.0}))
    accnums_per_geo = defaultdict(set)
    for accnum, (total, row_no, entities, geo_id) in best_by_accnum.items():
        accnums_per_geo[geo_id].add(accnum)
        for code, due, paid in entities:
            by_geo[geo_id][code]["due"] += due
            by_geo[geo_id][code]["paid"] += paid

    multi_account_geos = {g: accs for g, accs in accnums_per_geo.items() if len(accs) > 1}

    n_duplicate_accounts = n_majority_vote + n_magnitude_fallback
    n_excess_rows = sum(len(occs) - 1 for occs in occurrences_by_accnum.values() if len(occs) > 1)

    stats = {
        "detected_format": detected_format,
        "n_rows": n_rows,
        "n_bad_accnum": n_bad_accnum,
        "n_bad_year": n_bad_year,
        "n_distinct_accnum": len(occurrences_by_accnum),
        "n_duplicate_accounts": n_duplicate_accounts,
        "n_exact_duplicate_resolutions": n_excess_rows,
        "n_majority_vote_resolutions": n_majority_vote,
        "n_magnitude_fallback_resolutions": n_magnitude_fallback,
        "n_distinct_geo_ids": len(by_geo),
        "n_multi_account_geo_ids": len(multi_account_geos),
        "elapsed_s": time.time() - t0,
    }
    return by_geo, stats, dup_review_rows


def reconcile_geo_ids(conn, by_geo):
    """Split by_geo into (matched, unmatched) against the real `parcel`
    table. Never silently drops -- unmatched geo_ids and their count are
    returned for explicit reporting. Needs a live DB connection -- cannot
    run in an environment without one (see run_cli's handling below)."""
    with conn.cursor() as cur:
        cur.execute("SELECT geo_id FROM parcel")
        real_geo_ids = {r[0] for r in cur.fetchall()}
    matched = {g: v for g, v in by_geo.items() if g in real_geo_ids}
    unmatched = {g: v for g, v in by_geo.items() if g not in real_geo_ids}
    return matched, unmatched


BILLING_SQL = """
    INSERT INTO tax_billing
        (geo_id, tax_year, total_tax, total_paid, data_source, confidence_level)
    VALUES (%(geo_id)s, %(tax_year)s, %(total_tax)s, %(total_paid)s,
            %(data_source)s, %(confidence_level)s)
    ON CONFLICT (geo_id, tax_year) DO UPDATE
        SET total_tax        = EXCLUDED.total_tax,
            total_paid       = EXCLUDED.total_paid,
            data_source      = EXCLUDED.data_source,
            confidence_level = EXCLUDED.confidence_level
"""
# No WHERE guard on the upsert, same rationale as 2021: this PIR bulk export
# is the strongest available source for these years (comprehensive Tax
# Office export, verified byte-for-byte against sanity-check parcels), so it
# should unconditionally supersede whatever was there before. See
# check_portal_scrape_divergence() below for how the specific, real
# discrepancy this brief investigated (base tax vs. actual-paid-with-
# penalty/interest) is preserved rather than silently destroyed.

ENTITY_SQL = """
    INSERT INTO tax_billing_entity (geo_id, tax_year, entity_code, amount_due, amount_paid)
    VALUES (%(geo_id)s, %(tax_year)s, %(entity_code)s, %(amount_due)s, %(amount_paid)s)
    ON CONFLICT (geo_id, tax_year, entity_code) DO UPDATE
        SET amount_due  = EXCLUDED.amount_due,
            amount_paid = EXCLUDED.amount_paid
"""


def check_portal_scrape_divergence(conn, matched, tax_year, tolerance=1.00):
    """DESIGN NOTE (per Diego's build brief, judgment call flagged): before
    this loader's unconditional upsert overwrites tax_billing for a
    (geo_id, tax_year), check whether a portal_scrape row already there
    disagrees with the new PIR total by more than `tolerance` dollars.

    This is the investigation's core finding applied at scale: PIR's
    TXBASTAX/TXTAXDUE is base tax billed; portal_scrape can legitimately
    include penalty/interest for a late payment (confirmed on 0100030105:
    a $4,090.26 / 7.0000% gap matching Texas Tax Code §33.01(a)'s first-
    month delinquency rate almost exactly). A divergence here is NOT
    necessarily an error in either source -- it may be a real, meaningful
    delinquency signal.

    Chosen approach (Diego's option (a) vs (b) -- I picked a middle path):
    rather than adding a new schema column to carry this forward live in
    tax_billing (a bigger design decision I didn't think this round should
    make unilaterally), the old figure is preserved in the SAME review-log
    CSV this loader already produces for duplicate-TXACCNUM resolutions and
    unmatched geo_ids (see write_review_log below) -- a durable, human-
    reviewable trace of exactly which parcels changed and by how much,
    without silently discarding the old number. This needs a live DB read
    (of the existing tax_billing rows) BEFORE the upsert runs, so it must
    execute before write_to_db() -- see run_cli() ordering below.

    Returns: [(geo_id, old_data_source, old_confidence, old_total,
               new_total, delta, delta_pct), ...] for every (geo_id, year)
    where a differing prior row existed beyond `tolerance`.
    """
    geo_ids = list(matched.keys())
    if not geo_ids:
        return []
    divergences = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geo_id, data_source, confidence_level, total_tax "
            "FROM tax_billing WHERE tax_year = %s AND geo_id = ANY(%s)",
            (tax_year, geo_ids),
        )
        existing = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

    for geo_id, entities in matched.items():
        prior = existing.get(geo_id)
        if not prior:
            continue
        old_source, old_confidence, old_total = prior
        if old_total is None:
            continue
        new_total = sum(v["due"] for v in entities.values())
        delta = float(new_total) - float(old_total)
        if abs(delta) <= tolerance:
            continue
        delta_pct = (delta / float(old_total) * 100) if old_total else None
        divergences.append((geo_id, old_source, old_confidence, float(old_total),
                             new_total, delta, delta_pct))
    return divergences


def write_to_db(conn, matched, tax_year, data_source, confidence_level):
    from loaders.db import batch_upsert
    billing_rows = []
    entity_rows = []
    for geo_id, entities in matched.items():
        total_due = sum(v["due"] for v in entities.values())
        total_paid = sum(v["paid"] for v in entities.values())
        billing_rows.append({
            "geo_id": geo_id, "tax_year": tax_year,
            "total_tax": round(total_due, 2), "total_paid": round(total_paid, 2),
            "data_source": data_source, "confidence_level": confidence_level,
        })
        for code, v in entities.items():
            entity_rows.append({
                "geo_id": geo_id, "tax_year": tax_year, "entity_code": code,
                "amount_due": round(v["due"], 2), "amount_paid": round(v["paid"], 2),
            })

    n_billing = batch_upsert(conn, BILLING_SQL, billing_rows)
    n_entity = batch_upsert(conn, ENTITY_SQL, entity_rows)
    return n_billing, n_entity


def verify_sanity_parcels(conn, tax_year, expected):
    """expected: {geo_id: expected_total_or_None}. None means "no
    independently-confirmed figure for this parcel/year -- report what's
    found, don't grade it" (this applies to 0100030109 for 2023/2024, which
    the investigation didn't independently confirm against a known-good
    total the way it did for 0100030105 and for both parcels in 2022)."""
    print(f"\n  Sanity-check parcel verification ({tax_year}):")
    all_pass = True
    with conn.cursor() as cur:
        for geo_id, exp in expected.items():
            cur.execute(
                "SELECT total_tax FROM tax_billing WHERE geo_id = %s AND tax_year = %s",
                (geo_id, tax_year),
            )
            row = cur.fetchone()
            actual = float(row[0]) if row and row[0] is not None else None
            if exp is None:
                print(f"    [INFO] {geo_id}: no independently-confirmed expected "
                      f"figure for this parcel/year -- found "
                      f"{'$' + format(actual, ',.2f') if actual is not None else 'NULL'}")
                continue
            ok = actual is not None and abs(actual - exp) < 0.01
            all_pass = all_pass and ok
            status = "PASS" if ok else "FAIL"
            print(f"    [{status}] {geo_id}: expected ${exp:,.2f}, got "
                  f"{'$' + format(actual, ',.2f') if actual is not None else 'NULL'}")
    return all_pass


def write_review_log(dup_review_rows, unmatched, divergences, path):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "detail_1", "detail_2", "detail_3", "detail_4", "detail_5"])
        w.writerow(["=== Duplicate-TXACCNUM resolutions ===", "", "", "", "", ""])
        w.writerow(["full_accnum", "method", "kept_row", "kept_total",
                     "n_occurrences", "other_totals"])
        for accnum, method, kept_row, kept_total, n_occ, cluster_size, other_totals in dup_review_rows:
            others = "; ".join(f"{t:.2f}" for t in other_totals)
            w.writerow([accnum, method, kept_row, f"{kept_total:.2f}", n_occ, others])
        w.writerow(["=== geo_ids in file with no match in parcel table ===", "", "", "", "", ""])
        w.writerow(["geo_id", "n_entities_billed", "total_due", "", "", ""])
        for geo_id, entities in sorted(unmatched.items()):
            total = sum(v["due"] for v in entities.values())
            w.writerow([geo_id, len(entities), f"{total:.2f}", "", "", ""])
        w.writerow(["=== prior portal_scrape total diverged from new PIR total "
                     "(old figure preserved here, not silently discarded) ===",
                     "", "", "", "", ""])
        w.writerow(["geo_id", "old_data_source", "old_total", "new_pir_total",
                     "delta", "delta_pct"])
        for geo_id, old_source, old_confidence, old_total, new_total, delta, delta_pct in divergences:
            pct_str = f"{delta_pct:.2f}%" if delta_pct is not None else ""
            w.writerow([geo_id, old_source, f"{old_total:.2f}", f"{new_total:.2f}",
                        f"{delta:+.2f}", pct_str])


def inspect(filepath):
    z = zipfile.ZipFile(filepath)
    detected_format = detect_string_format(z)
    print(f"Detected string format: {detected_format}")
    shared = load_shared_strings(z)
    print(f"Shared strings loaded: {len(shared):,}")
    name_to_letter = parse_header(z, shared)
    print(f"Header fields found ({len(name_to_letter)}): "
          f"{sorted(name_to_letter.items(), key=lambda kv: kv[1])[:20]} ...")
    gen = iter_rows(z, shared, name_to_letter)
    for i, (row_no, row) in enumerate(gen):
        print(f"\nRow {row_no}: TXACCNUM={row.get('TXACCNUM')} "
              f"TXACCYER={row.get('TXACCYER')}")
        for code, due, paid in extract_entities(row):
            print(f"    {code}: due={due} paid={paid}")
        if i >= 1:
            break


def run_cli(tax_year, data_source, confidence_level, filepath_default,
            sanity_expected, review_log_default, config_attr=None):
    """Shared CLI entry point -- each per-year script just supplies its
    constants and calls this. Mirrors load_pir_billing_2021_full.py's
    original CLI (--inspect / --dry-run / full load / --skip-metrics /
    --limit / --review-log) exactly, so the operational process Diego
    already knows (inspect, then dry-run, then real load, each reviewed
    before the next) is unchanged for these three new years."""
    import argparse
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import config

    parser = argparse.ArgumentParser(
        description=f"Load the {tax_year} PIR full billing export (264-col XLSX)")
    parser.add_argument("--inspect", action="store_true",
                         help="Print header + first 2 rows, then exit")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse, aggregate, reconcile -- report everything, write nothing")
    parser.add_argument("--skip-metrics", action="store_true",
                         help="Skip parcel_metrics coverage-level refresh after load")
    parser.add_argument("--review-log", default=None,
                         help="Path for the duplicate-resolution + unmatched-geo_id + "
                              "portal-scrape-divergence review CSV")
    parser.add_argument("--limit", type=int, default=None,
                         help="Smoke-test: stop after N data rows (implies partial "
                              "results -- always combine with --dry-run)")
    args = parser.parse_args()

    filepath = getattr(config, config_attr, filepath_default) if config_attr else filepath_default
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    if args.inspect:
        inspect(filepath)
        return

    review_path = args.review_log or review_log_default

    print(f"Loading {filepath} ({os.path.getsize(filepath)/1e6:.0f} MB)…")
    if args.limit:
        print(f"  *** --limit {args.limit:,} set -- PARTIAL run, results are a preview only ***")
    by_geo, stats, dup_review_rows = load_and_aggregate(filepath, tax_year, row_limit=args.limit)

    print("\n  Parse + aggregation summary:")
    for k, v in stats.items():
        if isinstance(v, int):
            print(f"    {k}: {v:,}")
        elif isinstance(v, float):
            print(f"    {k}: {v:.1f}")
        else:
            print(f"    {k}: {v}")

    if stats["n_duplicate_accounts"]:
        print(f"\n  {stats['n_duplicate_accounts']:,} accounts had duplicate TXACCNUM rows "
              f"({stats['n_exact_duplicate_resolutions']:,} excess rows total, folded down to "
              f"one record per account) -- resolved by majority-vote-by-similarity clustering:")
        print(f"    {stats['n_majority_vote_resolutions']:,} resolved by MAJORITY VOTE.")
        if stats["n_magnitude_fallback_resolutions"]:
            print(f"    *** {stats['n_magnitude_fallback_resolutions']:,} resolved by MAGNITUDE "
                  f"FALLBACK -- no majority could be found. Review {review_path} "
                  f"(method column) before trusting this load. ***")

    from loaders.db import get_conn
    conn = get_conn()
    try:
        matched, unmatched = reconcile_geo_ids(conn, by_geo)
        print(f"\n  geo_id reconciliation against live `parcel` table:")
        print(f"    matched:   {len(matched):,}")
        print(f"    unmatched: {len(unmatched):,}  (skipped -- see review log)")

        print(f"\n  Checking for prior portal_scrape totals that diverge from this "
              f"file's PIR totals…")
        divergences = check_portal_scrape_divergence(conn, matched, tax_year)
        print(f"    {len(divergences):,} parcels have a prior total_tax that differs "
              f"from the new PIR total by more than $1.00.")
        if divergences:
            print(f"    (This may reflect real penalty/interest on late payments, not "
                  f"an error in either source -- see the investigation report. Old "
                  f"figures preserved in the review log, not silently discarded.)")

        write_review_log(dup_review_rows, unmatched, divergences, review_path)
        print(f"    review log written: {review_path}")

        if args.dry_run:
            total_due_all = sum(
                sum(e["due"] for e in ents.values()) for ents in matched.values()
            )
            print(f"\n  DRY RUN -- would write {len(matched):,} tax_billing rows, "
                  f"total ${total_due_all:,.2f} across all matched parcels.")
            for geo_id, exp in sanity_expected.items():
                ents = matched.get(geo_id)
                total = sum(e["due"] for e in ents.values()) if ents else None
                if exp is None:
                    print(f"    [INFO] {geo_id}: no independently-confirmed expected "
                          f"figure -- would write "
                          f"{'$' + format(total, ',.2f') if total is not None else 'NOT FOUND'}")
                    continue
                ok = total is not None and abs(total - exp) < 0.01
                print(f"    [{'PASS' if ok else 'FAIL'}] {geo_id}: expected "
                      f"${exp:,.2f}, would write "
                      f"{'$' + format(total, ',.2f') if total is not None else 'NOT FOUND'}")
            return

        print("\n  Ensuring tax_billing.data_source/confidence_level columns exist…")
        from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols
        ensure_billing_cols(conn)

        print("  Writing to database…")
        n_billing, n_entity = write_to_db(conn, matched, tax_year, data_source, confidence_level)
        print(f"    {n_billing:,} tax_billing rows, {n_entity:,} tax_billing_entity rows upserted")

        if not args.skip_metrics:
            from loaders.load_pir_billing import update_coverage_level
            update_coverage_level(conn, {tax_year})
            print("\nRun python3 loaders/compute_metrics.py to recompute all derived metrics.")

        all_pass = verify_sanity_parcels(conn, tax_year, sanity_expected)
        if not all_pass:
            print("\n  *** SANITY CHECK FAILED -- do not trust this load without investigating. ***")
        else:
            print("\n  Sanity check passed.")
    finally:
        conn.close()
