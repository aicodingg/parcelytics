#!/usr/bin/env python3
"""
loaders/load_pir_billing_2021_full.py — Load the 2021 Travis County Tax Office
PIR billing response ("DiegoPIR2021 Revised.xlsx"): a real, comprehensive
418,159-row bulk export, one row per taxing *account* (not necessarily one row
per parcel — see "Two-tier aggregation" below), with up to 10 taxing entities
packed into repeated column groups (TXENTCOD1..10, each with its own
TXBASTAX/TXTAXDUE/TXPENINT/TXATTFEE/TXAMTTP1/TXAMTCOL).

This is a SEPARATE script from loaders/load_pir_billing.py because the column
layout is completely different: load_pir_billing.py expects a simple
TaxCurOpenData-style CSV (PARCEL/TAXYEAR/ENTITY1/DUE1/PAID1...); this file is a
264-column XLSX with a much richer, but also much messier, per-entity layout.
Where the two scripts overlap conceptually (upsert pattern, coverage-level
refresh), this script reuses load_pir_billing.py's own code rather than
duplicating it — see the `update_coverage_level` import below.

=============================================================================
INVESTIGATION FINDINGS (July 2026, per Diego's task brief) — read before
touching the parsing/aggregation logic below; every design choice here is a
direct consequence of one of these findings, not a guess.
=============================================================================

1. TXACCNUM -> geo_id mapping (CONFIRMED, not assumed):
   Every one of the 418,159 data rows has a TXACCNUM that is exactly 14
   characters, all-digit, with no exceptions (verified by scanning the ENTIRE
   file's column A, not a sample). geo_id = TXACCNUM[:10] matches our 10-char
   TCAD account convention, and was verified byte-for-byte against the two
   sanity-check parcels (0100030105, 0100030109) plus a full-file join against
   every geo_id in the 2025 Certified Export (the authoritative source of the
   live `parcel` table's geo_id set, per load_certified_2025.py) --
   397,698 distinct geo_ids derived from the file; 392,387 (98.66%) matched an
   existing geo_id, 5,311 (1.34%) did not (see reconciliation logic below --
   these are skipped and counted, never silently dropped).

2. TXACCNUM is NOT one row per parcel -- TWO distinct duplicate patterns exist,
   and they require OPPOSITE handling. Getting this wrong either double-counts
   or clobbers real billing data, so both are handled explicitly:

   (a) SAME 10-char geo_id prefix, DIFFERENT 14-char TXACCNUM suffix
       (e.g. geo_id 0252281024 has 217 distinct TXACCNUM values
       0252281024**0001** .. 0252281024**0217**). Verified this is a real,
       legitimate one-parcel-to-many-billable-units relationship (large
       apartment/condo complexes etc. billed as separate sub-accounts under
       one parent geo_id) -- 1,696 geo_ids exhibit this (post-dedup count;
       see the reconciliation note at the end of this finding for why an
       earlier investigation pass reported 4,636 here -- that number was
       computed differently, not wrong data). These must be SUMMED per
       (geo_id, entity_code): each sub-account is a genuinely separate
       billable unit, and the parent parcel's real total is the sum of all
       of them.

   (b) The EXACT SAME 14-char TXACCNUM appearing more than once: 3,008
       distinct accounts, 8,883 total "excess" rows beyond one-per-account
       (418,159 total rows - 409,276 distinct accounts = 8,883, confirmed
       exactly). Distribution of how many times a duplicated account repeats:
       132 accounts x2, 7 accounts x3, **2,865 accounts x4** (the
       overwhelming majority), 1 account x5, 1 x6, 1 x7, and one genuine
       outlier at x128 (account 02513103110000 -- flagged for a manual look,
       not specially handled below).

       DISAMBIGUATOR SEARCH, ROUND 1 (per Diego's explicit follow-up: a
       magnitude heuristic could get a genuine *downward* correction --
       successful protest, newly granted exemption -- backwards). Tested
       TRANDT, CREATEDT, TERMDT, SUPPLE, BTCHNUMPRV, BTCHNUMCUR (and
       TXRECTYP, checked earlier) against 9 real accounts / 34 rows: every
       one of these fields is IDENTICAL across every occurrence of the same
       account, with no exception. They are account-level extract metadata
       (when the account was last transacted/batched as a whole in the
       source system), not per-occurrence fields -- useless for telling two
       duplicate rows of the SAME account apart.

       DISAMBIGUATOR SEARCH, ROUND 2 (this round, per Diego's follow-up
       question about the 3,008-vs-8,883 and 4,636-vs-1,696 count gaps):
       reconciling those counts required pulling full, all-264-column diffs
       for real 4-occurrence accounts, which surfaced the actual structure
       of this duplication -- not a per-account coin flip between two
       candidate values, but a MAJORITY/OUTLIER pattern:
         - Account 01434809080000 (4 occurrences): all 4 rows agree to
           within $0.01 on every entity -- pure re-export noise, harmless
           either way.
         - Account 01020702220000 (4 occurrences): same -- all 4 agree to
           within $0.01.
         - Account 01010104030000 (4 occurrences): row 993 shows TXTAXDUE1
           = $1,892.28; rows 408913, 408914, AND 408915 all independently
           show TXTAXDUE1 = $1,197.70 (same 3-way agreement on every other
           entity on the account too). Row 993 is the lone outlier; three
           separate re-extracts of the SAME corrected figure agree with each
           other. This is exactly the scenario Diego was worried about --
           and here, the correct value is clearly the LOWER one, which the
           old magnitude-max heuristic would have gotten backwards.

       Across a 3-account sample, the pattern held cleanly both ways
       (unanimous-4 and 3-vs-1). This script now resolves duplicates by
       CLUSTERING an account's occurrences by total-amount similarity
       (tolerance $1.00 -- comfortably above the ~$0.01-0.02 rounding noise
       observed, comfortably below the ~$400-700 gaps seen in real
       corrections) and keeping the occurrence closest to the LARGEST
       cluster's median, i.e. majority vote, not magnitude. Magnitude
       (largest total) is used ONLY as a fallback when there is no majority
       to find -- concretely, every 2-occurrence account (132 of them, a
       1-vs-1 tie by construction) and any account whose occurrences all
       disagree with each other (no cluster larger than 1). That fallback
       carries the same disclosed risk as before, but now only for ~132-150
       accounts instead of all 3,008 -- see verify_sanity_parcels()-adjacent
       reporting and the review CSV, which now records which method
       (majority_vote vs magnitude_fallback) resolved each account, plus how
       large the winning cluster was, so Diego can see exactly how much
       evidence backed each resolution.

       COUNT RECONCILIATION (both discrepancies Diego flagged, confirmed by
       direct computation, not assumption):
         - "3,008" (investigation) vs "n_exact_duplicate_resolutions: 8,883"
           (first dry-run) -- 3,008 is the distinct-account count; 8,883 is
           the total excess-row count summed across those accounts
           (sum of occurrences-1 per account). They're both correct, they
           just count different things -- 3,008 accounts needed SOME
           resolution; 8,883 is how many individual rows got folded into
           those 3,008 resolutions (most accounts fold 3 rows into 1, i.e.
           contribute 3 to the excess count, since 2,865 of them are
           4-occurrence). n_exact_duplicate_resolutions has been renamed
           conceptually below to make this explicit in the stats output.
         - "4,636" (investigation) vs "n_multi_account_geo_ids: 1,696" (first
           dry-run) -- the investigation's 4,636 was computed from a raw,
           NON-deduplicated per-row count (counting how many geo_id prefixes
           appeared on more than one ROW of the file), which conflates two
           different things: genuine multi-sub-account geo_ids (pattern a)
           AND geo_ids whose single account merely happens to repeat via
           pattern (b) (e.g. account 01015007020000's geo_id, 0101500702,
           would show up as "geo_id appears on >1 row" purely because its
           OWN account repeats twice -- zero sub-accounts involved). The
           loader's n_multi_account_geo_ids is computed AFTER tier-1
           dedup (one row per distinct TXACCNUM first), so it only counts
           geo_ids with more than one DISTINCT sub-account -- the correct,
           narrower definition. 1,696 is the trustworthy number for pattern
           (a); 4,636 was an inflated proxy from investigation and should be
           discarded in favor of it.

3. Field semantics (TXBASTAX vs TXTAXDUE vs TXAMTCOL):
   - Master-record-level fields (TOTTAX, PRETXAMT, CURTXAMT, AMTDUE, AMTPAID)
     are 100% blank/zero in EVERY row of this file (verified exhaustively --
     2,090,795 cells checked across all 5 fields x 418,159 rows, zero nonzero
     values found anywhere). They are NOT used by this loader. total_tax is
     instead computed as SUM(entity amount_due) -- the only real total this
     file actually contains, mirroring the existing total_tax_derived pattern
     used elsewhere in this codebase for the same reason (no independent
     master total available).
   - TXBASTAX vs TXTAXDUE: identical in most rows, but diverge in ~2.8% of
     entity-slot comparisons (verified on a 50K-row sample). Where they
     diverge, TXAMTCOL (amount collected) always matches TXTAXDUE, never
     TXBASTAX -- and finding 2(b) above shows a real example where a
     correction zeroed BASTAX while TAXDUE carried the true corrected figure.
     TXTAXDUE is therefore treated as the authoritative "amount due" figure;
     TXBASTAX is used ONLY as a fallback when TXTAXDUE is absent (i.e. entity
     slot 5, see below). TXBASTAX itself is not stored -- see the schema
     question in the task report about whether Diego wants it captured
     separately as a "base tax before correction" figure.
   - TXAMTCOL -> amount_paid (matches column semantics and the empirical
     pattern above).
   - Entity slot 5 breaks the naming pattern: TXENTCOD5/TXBASTAX5/
     **TXTAXAMT5**/**TXTAXOVR5**/TXATTFEE5/TXAMTTP15/TXAMTCOL5 (no TXTAXDUE5,
     TXTAXAMT5 was 0 in every row checked even when real tax existed,
     TXTAXOVR5 held a literal status-like string 'S', not a dollar amount).
     For slot 5, this loader uses TXBASTAX5 as the due-equivalent (TXTAXAMT5
     is unreliable/always-zero; TXTAXOVR5 is not a dollar field at all).
   - TXAMTTP1{slot}: blank in the vast majority of rows; observed non-blank
     values are short status codes (e.g. 'DEF'), never a dollar figure. Not a
     total/amount field -- safe to ignore for our schema, which has no place
     to store a status code at this granularity anyway.
   - TXATTFEE{slot}: mostly numeric $0, but occasionally a literal status
     string ('S') instead of a number. Parsed defensively (non-numeric ->
     treated as no fee), never stored (see schema question below -- our
     tax_billing_entity table has no attorney-fee column today).
   - TXPENINT{slot} (penalty & interest): present but likewise has no column
     in tax_billing_entity today. Not stored -- flagged as a schema question,
     not decided here.

4. Entity codes: 157 distinct real codes across the full file (167 raw
   distinct values found, minus 10 header-row artifacts from an off-by-one in
   the scan, i.e. "TXENTCOD1".."TXENTCOD10" each matching once). This is far
   beyond the 5 codes (IAU/CAT/TCO/THD/ACT) visible in the two sample
   parcels, exactly as Diego suspected. Cross-referenced against
   ENTITY_CODE_AUDIT.md (an existing, already-landed finding from a prior
   session): the extra codes are overwhelmingly PIDs/WCIDs/MUDs/ESDs that are
   already known to be absent from county_tax_rate by design (city-
   administered, not county-rated) -- e.g. P-prefixed PID codes, U-prefixed
   MUD codes, W-prefixed WCID codes, E-prefixed ESD codes all appear here too.
   NO entity-code translation is implemented or needed: entity_code is stored
   verbatim, exactly like every existing billing loader
   (load_pir_billing.py, load_tax_current.py, scrape_billing_history.py)
   already does, and the existing app-level LEFT JOIN county_tax_rate +
   "no rate" badge pattern (property.html) already handles any entity code
   with no rate-table match -- reused, not reinvented.

5. Performance: plain openpyxl.load_workbook() and even a naive
   xml.etree.ElementTree.iterparse() over the full 4.4GB uncompressed sheet
   XML were both measured as impractically slow for a 264-column x 418K-row
   sheet (iterparse: ~1,800 rows/sec => ~4 minutes just to scan, before any
   aggregation). This loader instead does a chunked *regex*-based extraction
   directly over the raw (streamed, never fully materialized) sheet XML: each
   32MB chunk is scanned for complete <row r="N">...</row> blocks (carrying a
   small tail-overlap buffer across chunk boundaries for rows that straddle a
   chunk), and only the ~45 named columns this loader actually needs are
   pulled out of each row via a header-driven column-letter map -- built once
   from row 1, not hardcoded, specifically so a header irregularity (like
   entity slot 5's TXTAXAMT5/TXTAXOVR5 vs. TXTAXDUE5) can never silently
   misalign a fixed offset. This measured 5-10x faster than
   ElementTree.iterparse for the same extraction in direct testing, because it
   skips per-cell Element/tree construction entirely. shared_strings (a
   ~3.9M-entry table this file uses to store even simple numeric-looking
   TXACCNUM values, since they have leading zeros) is still loaded via
   ElementTree.iterparse once at startup -- that specific file is small enough
   (~5s) that the DOM-construction overhead doesn't matter there.

Usage:
    python3 loaders/load_pir_billing_2021_full.py --inspect
        # Print header + first 2 rows, confirm column layout, then exit.
    python3 loaders/load_pir_billing_2021_full.py --dry-run
        # Parse, aggregate, and reconcile against real geo_ids -- report
        # everything (row counts, duplicate-resolution counts, match/no-match
        # counts, sanity-check parcel totals) without writing to the DB.
    python3 loaders/load_pir_billing_2021_full.py
        # Full load: upserts tax_billing + tax_billing_entity for 2021,
        # then calls load_pir_billing.py's update_coverage_level() and
        # verifies the two sanity-check parcels against Diego's confirmed
        # real totals ($64,459.78 / $1,192,820.09) before declaring success.
    python3 loaders/load_pir_billing_2021_full.py --skip-metrics
        # Same as above but skip the parcel_metrics coverage-level refresh
        # (e.g. if you plan to run compute_metrics.py separately afterward).

Do NOT run this against the live database without reviewing the review-log
CSV it produces on a --dry-run pass first (see finding 2(b) above) --
this is investigation + design + build, per the task brief; Diego runs the
real load himself.
"""
import argparse
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from loaders.db import get_conn, batch_upsert
# Reused, not reimplemented: load_pir_billing.py already has a correct,
# tested coverage-level refresh (flips parcel_metrics.coverage_level from
# 'value_only' to 'full' and computes effective_tax_rate/yoy_tax_amount_pct
# for the newly-billed year). Same reuse discipline as the rest of this
# session's loader work.
from loaders.load_pir_billing import update_coverage_level
# Reused: scrape_billing_history.py already added data_source/confidence_level
# to tax_billing and knows how to ensure those columns exist. Don't
# reimplement that migration a third time.
from loaders.scrape_billing_history import ensure_columns as ensure_billing_cols

TAX_YEAR = 2021
DATA_SOURCE = "pir_billing_2021_full"     # distinct from 'portal_scrape' and
                                           # from load_pir_billing.py's rows
                                           # (which today set NO data_source at
                                           # all -- see schema question in the
                                           # task report) so this run's
                                           # provenance is fully auditable.
CONFIDENCE_LEVEL = "verified"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Entity slots 1-10. Field name per slot is looked up dynamically from the
# header row (not hardcoded to a fixed column offset) because the layout is
# NOT perfectly uniform across slots -- confirmed by inspection: slot 5 has no
# TXTAXDUE5 (has TXTAXAMT5/TXTAXOVR5 instead), and slots 8/9 have no
# TXATTFEE8/TXATTFEE9. Looking up each field by name per slot, independently,
# means a missing field for a given slot is just None (handled), never a
# silent off-by-one into the wrong slot's data.
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


def load_shared_strings(z):
    shared = []
    with z.open("xl/sharedStrings.xml") as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == NS + "si":
                shared.append("".join(t.text or "" for t in elem.iter(NS + "t")))
                elem.clear()
    return shared


def parse_header(z, shared):
    """Read just enough of sheet1.xml to get row 1, return {field_name: col_letter}."""
    with z.open("xl/worksheets/sheet1.xml") as f:
        head = f.read(2_000_000).decode("utf-8", errors="replace")
    m = re.search(r'<row r="1"[^>]*>(.*?)</row>', head)
    if not m:
        raise RuntimeError("Could not find header row (row 1) in sheet1.xml")
    cell_re = re.compile(r'<c r="([A-Z]+)\d+"(?:[^>]*?t="(\w+)")?[^>]*><v>([^<]*)</v></c>')
    name_to_letter = {}
    for cm in cell_re.finditer(m.group(1)):
        col, ttype, val = cm.group(1), cm.group(2), cm.group(3)
        name = shared[int(val)] if (ttype == "s" and val != "") else val
        if name:
            name_to_letter[name] = col
    return name_to_letter


def build_row_cell_regex(needed_letters):
    """One compiled regex that matches any <c> cell whose column letter is in
    needed_letters, for a single row's raw XML substring. Compiled as a BYTES
    pattern -- row blocks are extracted from the raw (bytes) sheet stream via
    row_re in iter_rows() and never decoded to str, since decoding the whole
    4.4GB stream up front is exactly the overhead this loader is designed to
    avoid (see module docstring finding 5)."""
    colpat = "|".join(sorted(needed_letters, key=len, reverse=True))
    pattern = rf'<c r="({colpat})\d+"(?:[^>]*?t="(\w+)")?[^>]*><v>([^<]*)</v></c>'
    return re.compile(pattern.encode())


def iter_rows(z, shared, name_to_letter):
    """Yield {field_name: value} dicts, one per data row (row 2 onward),
    streaming the sheet XML in chunks -- never loading the full 4.4GB tree.

    Only extracts the field names we actually need (ENTITY_FIELD_TEMPLATES x
    10 slots + TXACCNUM/TXACCYER) -- see module docstring finding 5 for why
    this beats a full-column ElementTree.iterparse pass.
    """
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
                # keep tail bounded -- a single row is at most a few KB here
                # since we've excluded the ~220 unused columns already
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
                    letter_bytes, ttype, val = cm.group(1), cm.group(2), cm.group(3)
                    letter = letter_bytes.decode()
                    name = letter_to_name.get(letter)
                    if not name:
                        continue
                    if ttype == b"s":
                        row[name] = shared[int(val)] if val else None
                    else:
                        row[name] = val.decode() if val else None
                yield rownum.decode(), row


def extract_entities(row):
    """Return [(entity_code, amount_due, amount_paid), ...] for one row,
    applying the slot-5 TXTAXDUE-fallback and defensive numeric parsing
    documented in the module docstring (finding 3)."""
    out = []
    for slot in range(1, 11):
        code = row.get(f"TXENTCOD{slot}")
        if not code:
            continue
        due = _f(row.get(f"TXTAXDUE{slot}"))
        if due is None:
            # Slot 5 (and defensively, any other slot) has no TXTAXDUE value
            # -- fall back to TXBASTAX, per finding 3.
            due = _f(row.get(f"TXBASTAX{slot}"))
        paid = _f(row.get(f"TXAMTCOL{slot}"))
        if due is None and paid is None:
            continue
        out.append((code, due or 0.0, paid or 0.0))
    return out


def _cluster_by_similarity(occurrences, tolerance=1.00):
    """occurrences: [(row_no, total, entities), ...] all belonging to one
    TXACCNUM. Single-linkage cluster by total-amount similarity (dollar
    tolerance -- see module docstring finding 2(b) for why $1.00: observed
    rounding/re-export noise is $0.01-0.02, observed real corrections are
    $400-700+, so $1.00 cleanly separates "same value, float noise" from
    "genuinely different value" without needing a wider margin).

    Returns clusters sorted by (size desc, max-total-in-cluster desc) --
    i.e. the biggest-evidence cluster first, with ties broken by magnitude
    (this is exactly the old heuristic, but now scoped to only fire on true
    ties instead of on every duplicate)."""
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
    (length 1 for the common non-duplicated case). Implements the
    majority-vote-by-similarity algorithm from module docstring finding
    2(b): cluster occurrences by total-amount similarity, keep the
    occurrence closest to the LARGEST cluster's median (i.e. the value most
    independent re-extracts agree on). Magnitude (larger total) is used only
    to break a genuine tie between equally-sized clusters -- e.g. a true
    2-occurrence 1-vs-1 split, where there is no majority to find and the
    old heuristic is the only signal left.

    Returns (row_no, total, entities, geo_id, method, cluster_size) where
    method is "single" (no duplication), "majority_vote" (winning cluster
    strictly larger than the runner-up, or unanimous agreement), or
    "magnitude_fallback" (tied cluster sizes, resolved by magnitude)."""
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


def load_and_aggregate(filepath, progress_every=100_000, row_limit=None):
    """Full parse + two-tier aggregation (module docstring finding 2).

    row_limit: stop after this many data rows (smoke-test / --limit CLI flag
        below). None (default) processes the whole file. Since row order
        matters for the exact-duplicate-TXACCNUM heuristic (finding 2(b)),
        a limited run's duplicate-resolution counts are only a preview, not
        a substitute for a full run's review log.

    Returns:
        by_geo: {geo_id: {entity_code: {"due": float, "paid": float}}}
        stats: dict of counts for the final report
        dup_review_rows: [(full_accnum, method, kept_row_no, kept_total,
                            n_occurrences, winning_cluster_size,
                            other_totals), ...]
                          -- every duplicated-TXACCNUM resolution (whether
                          resolved by majority_vote or magnitude_fallback),
                          for Diego to spot-check (finding 2(b)).
    """
    t0 = time.time()
    z = zipfile.ZipFile(filepath)
    print("  Loading shared strings…", flush=True)
    shared = load_shared_strings(z)
    print(f"    {len(shared):,} shared strings [{time.time()-t0:.1f}s]", flush=True)

    name_to_letter = parse_header(z, shared)
    missing = [n for n in ("TXACCNUM", "TXACCYER", "TXENTCOD1", "TXBASTAX1",
                            "TXTAXDUE1", "TXAMTCOL1") if n not in name_to_letter]
    if missing:
        raise RuntimeError(f"Header is missing expected fields: {missing} -- "
                            f"file layout may have changed, stop and re-inspect.")

    # ── Tier 1a: buffer every occurrence per full 14-char TXACCNUM ─────────
    # (Row order no longer decides the winner -- see _resolve_accnum_occurrences
    # below -- so all occurrences of a duplicated account must be collected
    # before any resolution decision can be made.)
    occurrences_by_accnum = defaultdict(list)   # accnum -> [(row_no, total, entities, geo_id), ...]
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
        # Hardened (July 2026, incident response -- same "check the actual
        # handling, don't trust the filename" scrutiny just applied to
        # load_tax_current.py): the original condition here was
        # `if year and str(year).strip() not in (str(TAX_YEAR),):` -- since
        # Python's `and` short-circuits on a falsy `year`, a row with a
        # blank/None/0 TXACCYER would skip the whole check and fall through
        # as INCLUDED, unverified. pir_xlsx_common.py's _year_matches() (used
        # by the 2022/2023/2024 loaders) already gets this right by
        # explicitly treating None/"" as non-matching. This rewrite requires
        # an affirmative match instead of just checking for a mismatch, so
        # a blank/missing year is correctly rejected here too. This is a
        # narrower bug than load_tax_current.py's -- a stray row could only
        # ever be folded into 2021's own totals (TAX_YEAR is a fixed
        # constant, never row-derived, in write_to_db() below), never
        # written to a different year -- but worth closing before this file
        # is relied on for restoration.
        if not year or str(year).strip() != str(TAX_YEAR):
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

    # ── Tier 1b: resolve each account's occurrences by majority-vote-by-
    # similarity clustering, falling back to magnitude only on a genuine tie
    # (finding 2(b)) ─────────────────────────────────────────────────────
    best_by_accnum = {}   # full_accnum -> (total, row_no, entities, geo_id)
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

    # ── Tier 2: sum distinct TXACCNUM sub-accounts sharing a geo_id ────────
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
    """Split by_geo into (matched, unmatched) against the real `parcel` table.
    Never silently drops -- unmatched geo_ids and their count are returned
    for explicit reporting."""
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
# No WHERE guard on the upsert (unlike scrape_billing_history.py's own
# upsert, which protects itself from being overwritten by weaker data): this
# IS the strongest available 2021 source (comprehensive Tax Office bulk
# export, verified byte-for-byte against both sanity-check parcels), so per
# Diego's own framing it should unconditionally supersede whatever was there
# before -- portal_scrape rows, prior pir_billing rows, or nothing. See the
# task report for the explicit tradeoff discussion.

ENTITY_SQL = """
    INSERT INTO tax_billing_entity (geo_id, tax_year, entity_code, amount_due, amount_paid)
    VALUES (%(geo_id)s, %(tax_year)s, %(entity_code)s, %(amount_due)s, %(amount_paid)s)
    ON CONFLICT (geo_id, tax_year, entity_code) DO UPDATE
        SET amount_due  = EXCLUDED.amount_due,
            amount_paid = EXCLUDED.amount_paid
"""


def write_to_db(conn, matched):
    billing_rows = []
    entity_rows = []
    for geo_id, entities in matched.items():
        total_due = sum(v["due"] for v in entities.values())
        total_paid = sum(v["paid"] for v in entities.values())
        billing_rows.append({
            "geo_id": geo_id, "tax_year": TAX_YEAR,
            "total_tax": round(total_due, 2), "total_paid": round(total_paid, 2),
            "data_source": DATA_SOURCE, "confidence_level": CONFIDENCE_LEVEL,
        })
        for code, v in entities.items():
            entity_rows.append({
                "geo_id": geo_id, "tax_year": TAX_YEAR, "entity_code": code,
                "amount_due": round(v["due"], 2), "amount_paid": round(v["paid"], 2),
            })

    n_billing = batch_upsert(conn, BILLING_SQL, billing_rows)
    n_entity = batch_upsert(conn, ENTITY_SQL, entity_rows)
    return n_billing, n_entity


def verify_sanity_parcels(conn):
    """Diego's explicit verification requirement: re-query the two known
    sanity-check parcels after load and confirm exact totals. Prints
    PASS/FAIL -- does not raise, so a --dry-run caller can still see full
    stats even if this hasn't run yet."""
    expected = {
        "0100030105": 64459.78,
        "0100030109": 1192820.09,
    }
    print("\n  Sanity-check parcel verification:")
    all_pass = True
    with conn.cursor() as cur:
        for geo_id, exp in expected.items():
            cur.execute(
                "SELECT total_tax FROM tax_billing WHERE geo_id = %s AND tax_year = %s",
                (geo_id, TAX_YEAR),
            )
            row = cur.fetchone()
            actual = float(row[0]) if row and row[0] is not None else None
            ok = actual is not None and abs(actual - exp) < 0.01
            all_pass = all_pass and ok
            status = "PASS" if ok else "FAIL"
            print(f"    [{status}] {geo_id}: expected ${exp:,.2f}, got "
                  f"{'$' + format(actual, ',.2f') if actual is not None else 'NULL'}")
    return all_pass


def write_review_log(dup_review_rows, unmatched, path):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "detail_1", "detail_2", "detail_3", "detail_4", "detail_5"])
        w.writerow(["=== Duplicate-TXACCNUM resolutions (finding 2b) ===", "", "", "", "", ""])
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


def inspect(filepath):
    z = zipfile.ZipFile(filepath)
    shared = load_shared_strings(z)
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


def main():
    parser = argparse.ArgumentParser(
        description="Load the 2021 PIR full billing export (264-col XLSX)")
    parser.add_argument("--inspect", action="store_true",
                         help="Print header + first 2 rows, then exit")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse, aggregate, reconcile -- report everything, write nothing")
    parser.add_argument("--skip-metrics", action="store_true",
                         help="Skip parcel_metrics coverage-level refresh after load")
    parser.add_argument("--review-log", default=None,
                         help="Path for the duplicate-resolution + unmatched-geo_id "
                              "review CSV (default: loaders/.pir_2021_review.csv)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Smoke-test: stop after N data rows (implies partial "
                              "results -- always combine with --dry-run)")
    args = parser.parse_args()

    filepath = config.PIR_2021_FULL_XLSX
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        print("Set config.PIR_2021_FULL_XLSX to the real file path.")
        return

    if args.inspect:
        inspect(filepath)
        return

    review_path = args.review_log or os.path.join(
        os.path.dirname(__file__), ".pir_2021_review.csv")

    print(f"Loading {filepath} ({os.path.getsize(filepath)/1e6:.0f} MB)…")
    if args.limit:
        print(f"  *** --limit {args.limit:,} set -- PARTIAL run, results are a preview only ***")
    by_geo, stats, dup_review_rows = load_and_aggregate(filepath, row_limit=args.limit)

    print("\n  Parse + aggregation summary:")
    for k, v in stats.items():
        print(f"    {k}: {v:,}" if isinstance(v, int) else f"    {k}: {v:.1f}")

    if stats["n_duplicate_accounts"]:
        print(f"\n  {stats['n_duplicate_accounts']:,} accounts had duplicate TXACCNUM rows "
              f"({stats['n_exact_duplicate_resolutions']:,} excess rows total, folded down to "
              f"one record per account) -- resolved by majority-vote-by-similarity clustering "
              f"(see module docstring finding 2(b)):")
        print(f"    {stats['n_majority_vote_resolutions']:,} resolved by MAJORITY VOTE -- a "
              f"clear winning cluster of occurrences that agree with each other (within $1.00), "
              f"outnumbering any disagreeing occurrence. High confidence.")
        if stats["n_magnitude_fallback_resolutions"]:
            print(f"    *** {stats['n_magnitude_fallback_resolutions']:,} resolved by MAGNITUDE "
                  f"FALLBACK -- no majority could be found (occurrences split into equally-sized "
                  f"clusters, e.g. a true 1-vs-1 split), so the larger total was kept as before. "
                  f"No reliable field to break this kind of tie was found in this file "
                  f"(TRANDT/CREATEDT/TERMDT/SUPPLE/BTCHNUMPRV/BTCHNUMCUR all tested, all identical "
                  f"across duplicate rows of the same account -- see module docstring). This is a "
                  f"DISCLOSED, UNCONFIRMED assumption for these specific accounts only: a genuine "
                  f"downward correction would be resolved backwards. Review {review_path} "
                  f"(method column) before trusting this load. ***")

    conn = get_conn()
    try:
        matched, unmatched = reconcile_geo_ids(conn, by_geo)
        print(f"\n  geo_id reconciliation against live `parcel` table:")
        print(f"    matched:   {len(matched):,}")
        print(f"    unmatched: {len(unmatched):,}  (skipped -- see review log)")

        write_review_log(dup_review_rows, unmatched, review_path)
        print(f"    review log written: {review_path}")

        if args.dry_run:
            total_due_all = sum(
                sum(e["due"] for e in ents.values()) for ents in matched.values()
            )
            print(f"\n  DRY RUN -- would write {len(matched):,} tax_billing rows, "
                  f"total ${total_due_all:,.2f} across all matched parcels.")
            # Show what the two sanity-check parcels WOULD get, without writing.
            for geo_id, exp in (("0100030105", 64459.78), ("0100030109", 1192820.09)):
                ents = matched.get(geo_id)
                total = sum(e["due"] for e in ents.values()) if ents else None
                ok = total is not None and abs(total - exp) < 0.01
                print(f"    [{'PASS' if ok else 'FAIL'}] {geo_id}: expected "
                      f"${exp:,.2f}, would write "
                      f"{'$' + format(total, ',.2f') if total is not None else 'NOT FOUND'}")
            return

        print("\n  Ensuring tax_billing.data_source/confidence_level columns exist…")
        ensure_billing_cols(conn)

        print("  Writing to database…")
        n_billing, n_entity = write_to_db(conn, matched)
        print(f"    {n_billing:,} tax_billing rows, {n_entity:,} tax_billing_entity rows upserted")

        if not args.skip_metrics:
            update_coverage_level(conn, {TAX_YEAR})
            print("\nRun python3 loaders/compute_metrics.py to recompute all derived metrics.")

        all_pass = verify_sanity_parcels(conn)
        if not all_pass:
            print("\n  *** SANITY CHECK FAILED -- do not trust this load without investigating. ***")
        else:
            print("\n  Sanity check passed for both known parcels.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
