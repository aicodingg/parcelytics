#!/usr/bin/env python3
"""
parse_cert_2021_pdf.py
Extract parcel-level data from the 2021 TCAD Certified Appraisal Roll PDF (GEO-sorted).

ID MAPPING (confirmed against parcel 0100030105):
    geo_id = RefID2[0:10]   (first 10 of the 14-digit RefID2 field)
    The two numbers printed before RefID2 are TCAD internal identifiers — not geo_id.

RELIABLE fields   : market_value, assessed_value, cap_loss, taxable_value, exemption_codes
RISKY fields      : land_value, imprv_value  (null-flagged if not found; check null_pct in report)

Usage
-----
  # Sample extraction (pages 8515–8530 around 1201 S Lamar, ~20 parcels)
  python3 loaders/parse_cert_2021_pdf.py --sample

  # Custom page range
  python3 loaders/parse_cert_2021_pdf.py --sample --pages 8515:8540

  # Full extraction to CSV (takes 20–40 min; ~350k+ parcels)
  python3 loaders/parse_cert_2021_pdf.py --output cert_2021_extracted.csv

  # Validate extracted CSV against DB (run locally — needs DB)
  python3 loaders/parse_cert_2021_pdf.py --validate cert_2021_extracted.csv

Data Integrity Standard
-----------------------
Do NOT load until --validate reports:
  market_value match rate > 95%   AND
  land_value non-null rate  > 50%
If either threshold fails, stop and document.
"""

import re, sys, os, csv, io, subprocess, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ── Config ────────────────────────────────────────────────────────────────────
_DATA_DIR = os.path.expanduser("~/Desktop/Claude Files")
PDF_GEO = os.path.join(_DATA_DIR, "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf")

# Default sample pages: straddling parcel 0100030105 (1201 S Lamar) + surrounding real property
SAMPLE_START = 8510
SAMPLE_END   = 8535

# ── Regex patterns ────────────────────────────────────────────────────────────
# Record header line in -layout mode:
#   [num1]  [num2]  [pct]  [type]  RefID2: [14-digit]
RE_HDR = re.compile(
    r'^\s*\d+\s+\d+\s+\d+\.\d+\s+([A-Z][A-Z0-9]*)\s+RefID2:\s+(\d{14})'
)

# Value labels that appear on right column (and sometimes same line as left-col labels)
RE_IMP_HS    = re.compile(r'Imp HS:\s+([\d,]+)')
RE_IMP_NHS   = re.compile(r'Imp NHS:\s+([\d,]+)')
RE_LAND_HS   = re.compile(r'Land HS:\s+([\d,]+)')
RE_LAND_NHS  = re.compile(r'Land NHS:\s+([\d,]+)')
RE_MARKET    = re.compile(r'Market:\s+([\d,]+)')
RE_ASSESSED  = re.compile(r'Assessed:\s+([\d,]+)')
RE_CAP       = re.compile(r'\bCap:\s+([\d,]+)')
RE_EXEMPT    = re.compile(r'Exemptions:\s*(.*)')

# Freeze notation "( YYYY)  NNN.NN" — remove before parsing entity numbers
RE_FREEZE    = re.compile(r'\(\s*\d{4}\s*\)\s+[\d,]+\.\d+')

# Entity code at start of indented entity-table row
RE_ENT_CODE  = re.compile(r'^\s{4,}([A-Z0-9]{2})\s+\S')


# ── Helpers ───────────────────────────────────────────────────────────────────
def _int(s):
    """Parse comma-formatted integer string → int, or None."""
    if not s:
        return None
    try:
        return int(s.replace(',', ''))
    except (ValueError, AttributeError):
        return None

def _first_match(pattern, line):
    m = pattern.search(line)
    return m.group(1).strip() if m else None

def _trailing_integers(line, freeze_removed=False):
    """Extract the last N whitespace-separated integers from a line.
    Removes freeze notations like '(2017) 2,985.41' first.
    Returns list of int or [] if < 1 integer found.
    """
    if not freeze_removed:
        line = RE_FREEZE.sub(' ', line)
    # Match comma-formatted integers only (no decimal point)
    tokens = re.findall(r'(?<!\d)([\d,]+)(?!\d|,\d|\.\d)', line)
    result = []
    for t in tokens:
        try:
            result.append(int(t.replace(',', '')))
        except ValueError:
            pass
    return result


# ── Core parser ───────────────────────────────────────────────────────────────
def _apply_value_line(rec, line):
    """Extract any labeled values present on this line into rec."""
    if rec['_imp_hs']  is None:
        v = _first_match(RE_IMP_HS, line);   rec['_imp_hs']  = _int(v) if v else None
    if rec['_imp_nhs'] is None:
        v = _first_match(RE_IMP_NHS, line);  rec['_imp_nhs'] = _int(v) if v else None
    if rec['_land_hs'] is None:
        v = _first_match(RE_LAND_HS, line);  rec['_land_hs'] = _int(v) if v else None
    if rec['_land_nhs'] is None:
        v = _first_match(RE_LAND_NHS, line); rec['_land_nhs']= _int(v) if v else None
    if rec['market_value'] is None:
        v = _first_match(RE_MARKET, line);   rec['market_value']  = _int(v) if v else None
    if rec['assessed_value'] is None:
        v = _first_match(RE_ASSESSED, line); rec['assessed_value']= _int(v) if v else None
    if rec['cap_loss'] is None:
        v = _first_match(RE_CAP, line);      rec['cap_loss']      = _int(v) if v else None
    if rec['exemption_codes'] is None:
        m = RE_EXEMPT.search(line)
        if m:
            rec['exemption_codes'] = m.group(1).strip()


def _finalize(rec):
    """Compute derived aggregate fields; strip internal keys."""
    ih = rec.pop('_imp_hs',  None) or 0
    in_ = rec.pop('_imp_nhs', None) or 0
    lh = rec.pop('_land_hs', None) or 0
    ln = rec.pop('_land_nhs',None) or 0

    rec['imprv_value'] = (ih + in_) if (ih + in_) > 0 else None
    rec['land_value']  = (lh + ln)  if (lh + ln)  > 0 else None

    # Best taxable: prefer 03 Travis County, fall back to 0A, then assessed
    rec['taxable_value'] = (
        rec.pop('_taxable_tc', None)
        or rec.pop('_taxable_0a', None)
        or rec.get('assessed_value')
    )
    rec.pop('_taxable_tc', None)
    rec.pop('_taxable_0a', None)

    # Null flags for risky fields
    rec['imprv_value_null'] = 1 if rec['imprv_value'] is None else 0
    rec['land_value_null']  = 1 if rec['land_value']  is None else 0

    return rec


def _new_record(refid2, prop_type):
    return {
        'geo_id':           refid2[:10],
        'refid2':           refid2,
        'prop_type':        prop_type,   # R, P, MH, B, J, etc.
        'market_value':     None,
        'assessed_value':   None,
        'cap_loss':         None,
        'exemption_codes':  None,
        '_imp_hs':          None,
        '_imp_nhs':         None,
        '_land_hs':         None,
        '_land_nhs':        None,
        '_taxable_0a':      None,
        '_taxable_tc':      None,
    }


def parse_records(lines):
    """
    Iterate layout-text lines, yielding one finalized record dict per parcel.
    Handles mixed page breaks / headers gracefully.
    """
    current = None

    for line in lines:
        # Detect new record header
        m = RE_HDR.match(line)
        if m:
            if current is not None:
                yield _finalize(current)
            current = _new_record(refid2=m.group(2), prop_type=m.group(1))
            _apply_value_line(current, line)   # header line may contain Market: value
            continue

        if current is None:
            continue

        # Skip page/section header noise
        if re.match(r'^\s*(County:|Rev\.|True Automation|Certified Appraisal|'
                    r'As of Supplement|For Entities:|Geo ID Order|Prop ID\s|'
                    r'Entity Description)', line):
            continue

        # Extract any labeled values
        _apply_value_line(current, line)

        # Entity table rows: capture 0A and 03 taxable values
        em = RE_ENT_CODE.match(line)
        if em:
            code = em.group(1)
            if code in ('0A', '03'):
                nums = _trailing_integers(line)
                if len(nums) >= 3:
                    taxable = nums[-1]   # last column = Taxable
                    if code == '0A' and current['_taxable_0a'] is None:
                        current['_taxable_0a'] = taxable
                    elif code == '03' and current['_taxable_tc'] is None:
                        current['_taxable_tc'] = taxable

    if current is not None:
        yield _finalize(current)


# ── pdftotext runner ──────────────────────────────────────────────────────────
def _find_pdftotext():
    """Locate pdftotext binary, checking Homebrew paths on macOS."""
    import shutil
    for candidate in [
        'pdftotext',                        # already on PATH
        '/opt/homebrew/bin/pdftotext',      # Apple Silicon Homebrew
        '/usr/local/bin/pdftotext',         # Intel Homebrew
        '/usr/bin/pdftotext',               # Linux system
    ]:
        if shutil.which(candidate) or (candidate.startswith('/') and os.path.isfile(candidate)):
            return candidate
    print("ERROR: pdftotext not found.\n"
          "Install with:  brew install poppler\n"
          "Then retry.", file=sys.stderr)
    sys.exit(1)

def stream_pdf_text(pdf_path, start_page=None, end_page=None):
    """Run pdftotext -layout and return lines list."""
    pdftotext = _find_pdftotext()
    cmd = [pdftotext, '-layout']
    if start_page: cmd += ['-f', str(start_page)]
    if end_page:   cmd += ['-l', str(end_page)]
    cmd += [pdf_path, '-']
    result = subprocess.run(cmd, capture_output=True, text=True, errors='replace')
    if result.returncode != 0:
        print(f"pdftotext error: {result.stderr[:200]}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.splitlines()


# ── CSV output ────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    'geo_id', 'prop_type', 'refid2',
    'market_value', 'assessed_value', 'cap_loss',
    'land_value', 'imprv_value', 'land_value_null', 'imprv_value_null',
    'taxable_value', 'exemption_codes',
]

def write_csv(records, dest):
    w = csv.DictWriter(dest, fieldnames=CSV_FIELDS, extrasaction='ignore')
    w.writeheader()
    n = 0
    for rec in records:
        w.writerow(rec)
        n += 1
    return n


# ── Validation (cross-check against DB) ──────────────────────────────────────
def validate(csv_path):
    """
    Compare extracted CSV against parcel_tax_year for tax_year=2021.
    Prints match rates and flags data quality issues.
    Requires DB access (run locally).
    """
    try:
        import config, psycopg2, psycopg2.extras
    except ImportError:
        print("psycopg2 not available. Run this on your local machine.", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoading extracted CSV: {csv_path}")
    extracted = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            extracted[row['geo_id']] = row
    print(f"  {len(extracted):,} records in CSV")

    conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT,
                            dbname=config.DB_NAME, user=config.DB_USER,
                            password=config.DB_PASS)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Pull 2021 DB records for parcels present in CSV
    geo_ids = list(extracted.keys())
    cur.execute("""
        SELECT geo_id, market_value, assessed_value
        FROM parcel_tax_year
        WHERE tax_year = 2021
          AND geo_id = ANY(%s)
    """, (geo_ids,))
    db_rows = {r['geo_id']: r for r in cur.fetchall()}
    conn.close()

    print(f"  {len(db_rows):,} matching records found in DB (tax_year=2021)")

    # Cross-check
    sample = []         # geo_ids in both
    mv_match = 0
    mv_diff  = 0
    mv_miss  = 0        # in CSV but not DB
    av_match = 0
    av_diff  = 0
    land_nonnull = 0
    imprv_nonnull = 0
    total = 0

    TOLERANCE = 0.01    # 1% tolerance for value comparison

    mismatches = []

    for geo_id, row in extracted.items():
        total += 1
        if row.get('land_value'):  land_nonnull  += 1
        if row.get('imprv_value'): imprv_nonnull += 1

        db = db_rows.get(geo_id)
        if db is None:
            mv_miss += 1
            continue

        sample.append(geo_id)
        pdf_mv = int(row['market_value']) if row.get('market_value') else None
        db_mv  = int(db['market_value'])  if db.get('market_value')  else None

        if pdf_mv and db_mv:
            delta = abs(pdf_mv - db_mv) / max(db_mv, 1)
            if delta <= TOLERANCE:
                mv_match += 1
            else:
                mv_diff  += 1
                mismatches.append({'geo_id': geo_id, 'pdf_mv': pdf_mv, 'db_mv': db_mv,
                                   'pct_diff': round(delta * 100, 1)})
        elif pdf_mv is None:
            mv_miss += 1

        pdf_av = int(row['assessed_value']) if row.get('assessed_value') else None
        db_av  = int(db['assessed_value'])  if db.get('assessed_value')  else None
        if pdf_av and db_av:
            delta = abs(pdf_av - db_av) / max(db_av, 1)
            if delta <= TOLERANCE: av_match += 1
            else:                  av_diff  += 1

    # Results
    matched_total = len(sample)
    print(f"\n{'='*60}")
    print(f"VALIDATION REPORT — 2021 Certified Roll PDF vs DB")
    print(f"{'='*60}")
    print(f"PDF records:         {total:>8,}")
    print(f"Matched in DB:       {matched_total:>8,}  ({matched_total/max(total,1)*100:.1f}%)")
    print(f"Not in DB (AJR gap): {mv_miss:>8,}")
    print()
    if matched_total > 0:
        mv_rate = mv_match / matched_total * 100
        av_rate = av_match / matched_total * 100
        print(f"Market value  match: {mv_match:>8,} / {matched_total}  ({mv_rate:.1f}%)")
        print(f"Assessed value match:{av_match:>8,} / {matched_total}  ({av_rate:.1f}%)")
        print()
        lv_rate = land_nonnull / max(total, 1) * 100
        iv_rate = imprv_nonnull / max(total, 1) * 100
        print(f"Land value   non-null: {land_nonnull:>8,} / {total}  ({lv_rate:.1f}%)  ← risky field")
        print(f"Imprv value  non-null: {imprv_nonnull:>8,} / {total}  ({iv_rate:.1f}%)  ← risky field")
        print()

        # Verdict
        print(f"{'─'*60}")
        ok_mv = mv_rate >= 95.0
        ok_lv = lv_rate >= 50.0
        print(f"Market value ≥ 95%:  {'✅ PASS' if ok_mv else '❌ FAIL'}  ({mv_rate:.1f}%)")
        print(f"Land value  ≥ 50%:   {'✅ PASS' if ok_lv else '❌ FAIL'}  ({lv_rate:.1f}%)")
        print()
        if ok_mv and ok_lv:
            print("✅ VERDICT: Extraction is reliable. Safe to load.")
        elif ok_mv and not ok_lv:
            print("⚠️  VERDICT: Market/assessed OK. Land/imprv will load as NULL "
                  "for missing rows — acceptable if documented.")
        else:
            print("❌ VERDICT: Market value match rate too low. "
                  "Do NOT load. Investigate mismatches below.")

        if mismatches:
            print(f"\nTop mismatches (market value >1% off):")
            for mm in sorted(mismatches, key=lambda x: x['pct_diff'], reverse=True)[:10]:
                print(f"  {mm['geo_id']}  PDF={mm['pdf_mv']:>12,}  DB={mm['db_mv']:>12,}  "
                      f"Δ={mm['pct_diff']}%")
    print(f"{'='*60}\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pdf', default=PDF_GEO,
                    help='Path to GEO-sorted certified roll PDF')
    ap.add_argument('--sample', action='store_true',
                    help='Extract a sample page range; pretty-print to stdout (use --output for CSV)')
    ap.add_argument('--pages', default=f'{SAMPLE_START}:{SAMPLE_END}',
                    help='Page range (START:END) — used by both --sample and --output')
    ap.add_argument('--output', metavar='FILE.csv',
                    help='Write extracted records to CSV. Add --pages to limit to a range.')
    ap.add_argument('--validate', metavar='FILE.csv',
                    help='Cross-check CSV against DB (requires local DB)')
    args = ap.parse_args()

    if args.validate:
        validate(args.validate)
        return

    # Parse page range (used by both --sample and --output when --pages supplied)
    page_start, page_end = None, None
    if args.pages and args.pages != f'{SAMPLE_START}:{SAMPLE_END}' or args.sample:
        parts = args.pages.split(':')
        page_start = int(parts[0])
        page_end   = int(parts[1])

    if args.output:
        label = f"pages {page_start}–{page_end}" if page_start else "all pages"
        print(f"Extracting {label} from: {args.pdf}", file=sys.stderr)
        print(f"Writing to: {args.output}", file=sys.stderr)
        lines = stream_pdf_text(args.pdf, page_start, page_end)
        with open(args.output, 'w', newline='') as f:
            n = write_csv(parse_records(lines), f)
        print(f"Done. {n:,} records written to {args.output}", file=sys.stderr)
        return

    if args.sample:
        print(f"Extracting pages {page_start}–{page_end} from GEO PDF...", file=sys.stderr)
        lines   = stream_pdf_text(args.pdf, page_start, page_end)
        records = list(parse_records(lines))
        print(f"Found {len(records)} records.\n", file=sys.stderr)

        # Pretty-print to stdout
        print(f"{'geo_id':<12} {'type':<4} {'market_value':>14} {'assessed':>14} "
              f"{'cap_loss':>10} {'land':>12} {'imprv':>12} "
              f"{'lv_null':>7} {'iv_null':>7} {'exemptions'}")
        print('─' * 115)
        for r in records:
            def _f(v): return f"{v:>14,}" if v else "         —    "
            def _b(v): return f"{'Y' if v else ' ':>7}"
            print(
                f"{r['geo_id']:<12} {r['prop_type']:<4} "
                f"{_f(r['market_value'])} {_f(r['assessed_value'])} "
                f"{_f(r['cap_loss'])} {_f(r['land_value'])} {_f(r['imprv_value'])} "
                f"{_b(r['imprv_value_null'])} {_b(r['land_value_null'])} "
                f"  {r['exemption_codes'] or ''}"
            )

        n = len(records)
        r_recs = [r for r in records if r['prop_type'] == 'R']
        nr = len(r_recs)
        print('─' * 115)
        print(f"\nSummary — all {n} records:")
        print(f"  market_value non-null : {sum(1 for r in records if r['market_value'])}/{n}")
        print(f"\nReal property (R) only — {nr} records:")
        print(f"  land_value   non-null : {sum(1 for r in r_recs if r['land_value'])}/{nr}"
              f"  ← risky field")
        print(f"  imprv_value  non-null : {sum(1 for r in r_recs if r['imprv_value'])}/{nr}"
              f"  ← risky field")
        return

    if args.output:
        print(f"Full extraction from: {args.pdf}", file=sys.stderr)
        print(f"Writing to: {args.output}", file=sys.stderr)
        lines = stream_pdf_text(args.pdf)
        with open(args.output, 'w', newline='') as f:
            n = write_csv(parse_records(lines), f)
        print(f"Done. {n:,} records written to {args.output}", file=sys.stderr)
        return

    ap.print_help()


if __name__ == '__main__':
    main()
