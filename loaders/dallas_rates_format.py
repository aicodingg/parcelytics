"""
Parsing + entity-identity logic for Dallas County Tax Office's published
rate pages -- PX-20260829-07.

Two real source pages (see config.DALLAS_RATES_HTML_CURRENT /
DALLAS_RATES_HTML_HISTORY for the exact URLs and local path grammar), with
TWO STRUCTURALLY DIFFERENT layouts -- confirmed by Diego running this
loader's --dry-run against both real saved files, one correction at a
time. Do not assume these two pages share a parsing path.

  1. past-tax-rates.php  -- multiple prior years rendered as a Bootstrap-
     style accordion, NOT a flat list of tab-anchor/table pairs (an earlier
     pass here got this structure wrong -- see find_year_tables()'s own
     CORRECTION note below, from Diego's direct verification against the
     real saved file). Each year is one collapsible panel: a toggle `<a>`
     whose own text ends with the year (`<span class="chevron">...</span>
     &nbsp;&nbsp;2024 </a>`), immediately followed by its content `<div
     id="displayN">` wrapping that year's full entity table. Real coverage
     on this page is 2015-2024 inclusive (10 complete years, including
     2020 -- an earlier pass mis-scoped coverage as possibly gapped around
     2020; it is not) -- the separate current-year page covers 2025, for
     11 complete years total across both sources, no gap anywhere in the
     sequence.
  2. tax-rates.php       -- current tax year only, ONE table, but NO
     accordion at all (a second real correction, from Diego's direct
     verification -- see find_current_year_table()'s own CORRECTION note
     below): the table's own year comes from an `<h2>2025 Tax Rates</h2>`
     heading ELSEWHERE on the page, not from any element immediately
     preceding the table -- the table's real immediate predecessor is
     unrelated contact-info widget markup. This page also marks its header
     row differently: the class lives on the `<tr>` itself
     (`<tr class="tableHeaderBlue">`), not on the header cells (history's
     shape uses plain `<th>` cells, no row class needed), and the header
     text itself has irregular internal spacing around the ampersand
     ("M &amp; O", "I &amp;S" -- decoded: "M & O", "I &S") -- column
     matching must normalize away ALL whitespace, not assume one canonical
     spelling.

Table shape is NOT constant across years:
  - 2016-2024, and the current-year page: 4 columns --
    ENTITY NAME | M&O | I&S | TOTAL TAX RATE
  - 2015 only: 6 columns -- ENTITY NAME | ACT CODE | DCAD CODE | M&O | I&S |
    TOTAL TAX RATE. Confirmed live: most rows carry both codes (e.g. Dallas
    County = ACT 1002 / DCAD "DC"), but PID rows in 2015 carry only an ACT
    CODE, no DCAD CODE, and often only a TOTAL value with M&O/I&S blank.

Real, confirmed structural facts this module's parsing logic is built
against (all verified via live fetch, not assumed):
  - Entity IDENTITY is name-only for every year except 2015's ACT/DCAD
    codes, and even 2015's codes are not known to be stable/available in
    any OTHER Dallas source this codebase has already loaded (DCAD's own
    certified-roll ACCOUNT_APPRL_YEAR table has no entity-code column at
    all -- see dcad_format.py's own header comment). PX-20260829-06's
    research separately noted the PIR billing feed's TXENTCOD1-10 codes are
    numeric and COULD plausibly be the same code space as 2015's ACT CODE
    (both DCTO-sourced), but this is an unconfirmed lead, not something
    this module relies on -- it would need independent verification
    against real TXENTCOD values, which this sandbox cannot reach (vault
    not mounted). This module's real, load-bearing identity mechanism is
    the DALLAS_ENTITY_ALIASES crosswalk below, not the 2015 codes.
  - Entity NAMES drift across years (confirmed real examples, not
    hypothetical): "Grapevine-Colleyville ISD" vs "Grapevine - Colleyville
    ISD"; "SouthSide PREM PID" vs "South Side PREM PID"; "Levee District
    14" vs a literal typo "Levee District l4" (lowercase L for "1") in the
    2019 table; "City of Lewisvile" (missing second l) throughout, next to
    one single year's "City of Lewisville" (correct spelling); parenthetical
    county suffixes appearing/disappearing ("City of Wylie" vs "City of
    Wylie (Collin Co)"). A naive per-year name join would create duplicate
    entity rows or silently drop the drifted side.
  - Some entities carry N/A or 0.000000 for a given year in the SOURCE's
    own table (Grapevine-Colleyville ISD "n/a" in 2025; South Dallas/Fair
    Park PID and Levee District 14 zero/n-a in several years) -- this is a
    real property of the source, preserved as a NULL/None value, never
    coerced to 0.0.

Identity approach (approved PX-20260829-07 Task 3, modeled on the DCAD
ingest gate's own skip_reason/G1-ledger convention -- see
loaders/dcad_format.py's per-row skip_reason field and
loaders/ingest_gate.py's g1_conservation_check()):
  1. canonicalize_name() -- cheap, deterministic normalization (case,
     whitespace, punctuation) that closes the bulk of the drift above
     without any lookup table.
  2. DALLAS_ENTITY_ALIASES -- a maintained dict mapping every OTHER known
     canonical-name variant seen across the years this module's authors
     have actually read (not a live fuzzy-match -- see this module's own
     rationale in the PX-20260829-07 proposal report for why a maintained
     table beats a similarity threshold) to one single canonical
     entity_code. New, previously-unseen names do NOT get auto-matched --
     they fall into the skip ledger below for a human to add.
  3. Every row's outcome carries a skip_reason (None = accepted). No
     Dallas row is ever silently dropped -- an unmapped name is a loud,
     counted skip, not a guess.
"""
import re


# ── Name canonicalization ───────────────────────────────────────────────────
def canonicalize_name(raw_name):
    """Cheap, deterministic normalization -- closes spacing/punctuation/
    capitalization drift without a lookup table. Real examples this closes
    on its own: "Grapevine-Colleyville ISD" <-> "Grapevine - Colleyville
    ISD" (hyphen/space variants collapse to the same key); "SouthSide PREM
    PID" <-> "South Side PREM PID" (internal space variants collapse).
    Does NOT fix genuine misspellings ("Lewisvile" vs "Lewisville") or
    added/dropped parentheticals ("(Collin Co)") -- those need an explicit
    DALLAS_ENTITY_ALIASES entry, precisely because collapsing on a fuzzy
    edit-distance basis is the silent-duplicate/silent-merge risk this
    design deliberately avoids (see module docstring)."""
    name = raw_name.strip().lower()
    name = re.sub(r"[^\w\s]", "", name)   # drop punctuation (hyphens, /, #, etc.)
    name = re.sub(r"\s+", "", name)       # collapse ALL whitespace (closes "south side" vs "southside")
    return name


# ── Known real name-variant -> canonical entity_code crosswalk ─────────────
# Built from the actual years this module's authors have read (2015-2025,
# PX-20260829-06/-07 live fetches). NOT exhaustive of every entity's every
# historical spelling -- only the drift cases actually observed. New drift
# found on a future load surfaces via the skip ledger, not a guess.
#
# entity_code values below are synthetic, deterministic slugs derived from
# each entity's OWN canonicalized name (via dallas_entity_code()) -- NOT
# borrowed from 2015's ACT/DCAD codes (see module docstring for why that
# lead is unconfirmed, not load-bearing). Prefixed "DAL-" so these can never
# collide with Travis's own TDC-sourced entity_code values in the same
# county_tax_rate table (PK is (county_code, entity_code, tax_year), so a
# collision isn't a live bug even without the prefix, but the prefix makes
# a Dallas row visually unmistakable in any raw SQL/CSV export).
DALLAS_ENTITY_ALIASES = {
    # canonicalize_name(variant) -> canonicalize_name(the year that has the
    # OTHER shipping row's exact wording) -- i.e. both sides are run through
    # canonicalize_name() again at lookup time, so hyphen/space differences
    # are already handled by canonicalize_name() alone; this table only
    # needs the cases canonicalize_name() can't close by itself.
    canonicalize_name("Levee District l4"): canonicalize_name("Levee District 14"),
    canonicalize_name("City of Lewisvile"): canonicalize_name("City of Lewisville"),
    canonicalize_name("City of Lewisvile (Denton Co)"): canonicalize_name("City of Lewisville"),
    canonicalize_name("City of Wylie (Collin Co)"): canonicalize_name("City of Wylie"),
    canonicalize_name("Oak Lawn-Hi Line"): canonicalize_name("Oak Lawn-Hi Line PID"),
    canonicalize_name("High Point PID"): canonicalize_name("High Pointe PID"),
    canonicalize_name("University Crossing"): canonicalize_name("University Crossing PID"),
    canonicalize_name("Klyde Warren Park"): canonicalize_name("Klyde Warren Park PID"),
    canonicalize_name("VickeryMeadow Prem"): canonicalize_name("Midtown (Vickery) Prem PID"),
    canonicalize_name("VickeryMeadow STD"): canonicalize_name("Midtown (Vickery) Stand PID"),
    canonicalize_name("Vickery Meadow Prem"): canonicalize_name("Midtown (Vickery) Prem PID"),
    canonicalize_name("Vickery Meadow STD"): canonicalize_name("Midtown (Vickery) Stand PID"),
    # NOTE: "Levee District 4" is a genuinely DISTINCT real entity from
    # "Levee District 14" above (confirmed live, both appear separately in
    # Dallas's own published tables under different codes) -- deliberately
    # has NO entry here. It used to carry a self-referential no-op entry
    # as a documentation-only guard against a future accidental merge;
    # removed because dallas_entity_code()'s PIVOT below (hash-based, not
    # truncated-prefix-based) makes that specific collision structurally
    # impossible now, not just undocumented -- see that function's own
    # docstring for the real collision this pivot fixes.
}


import hashlib  # noqa: E402 -- kept near point of use, mirrors this module's small-function style


def dallas_entity_code(canonical_key):
    """Deterministic synthetic entity_code from a canonicalized name --
    stable, DAL-prefixed, fits entity_code's real VARCHAR(10) column
    exactly ("DAL" + 7 hex chars = 10).

    PIVOT (found by this module's OWN fixture tests, not a hypothetical):
    the original design here truncated a human-readable "DAL"+key prefix
    to fit VARCHAR(10). test_dallas_rates_format.py caught a REAL
    collision that produced: "Levee District 14" and "Levee District 4"
    (two genuinely distinct, live-confirmed Dallas entities -- see module
    docstring) both canonicalize to strings that are IDENTICAL for their
    first 13 characters ("leveedistrict..."), so both truncated to the
    exact same 10-character prefix "DALLEVEEDI" once the distinguishing
    "14" vs "4" fell past the truncation point. Left as truncation, this
    would have silently merged two real entities' rate histories under
    one entity_code on the very first real load -- not a rare edge case,
    a certainty given these two real, currently-published entity names.

    Fixed by hashing the FULL canonical key (sha1, hex, first 7 chars)
    instead of truncating a prefix of it: entity_code is no longer
    human-readable, but two different canonical keys -- however similar
    their prefix -- now hash to uncorrelated 7-hex-char values, closing
    this entire collision class rather than special-casing the one pair
    the fixtures happened to catch. Same trade-off this codebase already
    accepted for DCAD's alphanumeric prop_id (see
    dcad_format._hashed_prop_id()) -- a stable synthetic ID beats a
    readable one that can silently collide.

    A true SHA1-prefix collision across the ~100-200 real Dallas entities
    this module will ever see is astronomically unlikely (7 hex chars =
    ~268M values), but "astronomically unlikely" is exactly the class of
    assumption the truncation bug also started as -- so
    check_entity_code_collisions() below is still run by
    load_dallas_tax_rates.py before every write, as a loud, cheap,
    defense-in-depth backstop, never assumed away."""
    digest = hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()[:7]
    return ("DAL" + digest).upper()


class DuplicateEntityCodeError(Exception):
    """Raised by check_entity_code_collisions() -- see that function."""


def check_entity_code_collisions(rows):
    """rows: iterable of dicts carrying 'entity_code' and 'entity_name'
    (e.g. parse_year_table()'s output, across all years/both source
    files). Groups by entity_code; raises DuplicateEntityCodeError if any
    single entity_code maps to more than one DISTINCT canonicalized
    (alias-resolved) name -- names that are really the SAME entity via
    DALLAS_ENTITY_ALIASES/canonicalize_name are correctly treated as one
    name here, not flagged as a false collision. No return value on
    success (absence of an exception IS the pass signal, same convention
    as dcad_format.check_in_run_prop_id_collisions()) -- call this BEFORE
    any DB write, live or --dry-run, per Task 3's loud-never-silent
    design."""
    code_to_names = {}
    for row in rows:
        code = row.get("entity_code")
        if code is None:
            continue
        canon = canonicalize_name(row["entity_name"])
        canon = DALLAS_ENTITY_ALIASES.get(canon, canon)
        code_to_names.setdefault(code, set()).add(canon)
    collisions = {code: names for code, names in code_to_names.items() if len(names) > 1}
    if collisions:
        raise DuplicateEntityCodeError(
            f"{len(collisions)} entity_code value(s) each map to more than one "
            f"DISTINCT entity name -- refusing to load, since ON CONFLICT would "
            f"otherwise silently merge different entities' rate histories under "
            f"one code. Details: {collisions}"
        )


def resolve_entity_identity(raw_name):
    """Returns (entity_code, skip_reason). skip_reason is always None here
    -- canonicalize_name() + DALLAS_ENTITY_ALIASES together always produce
    SOME code, by construction (an unmapped name still canonicalizes to
    something and gets a code). The real "loud skip" case this module's
    callers need to watch is not name resolution itself but ROW-level
    problems (blank total rate, non-numeric value) -- see parse_year_table()
    below, which is where skip_reason is actually set to a non-None value.
    Kept as a separate function (rather than inlined) so a future stricter
    policy -- e.g. treating an alias-table MISS as its own loud, counted
    event distinct from an alias-table HIT -- has one call site to change."""
    key = canonicalize_name(raw_name)
    canonical_key = DALLAS_ENTITY_ALIASES.get(key, key)
    return dallas_entity_code(canonical_key), None


# ── Table parsing ────────────────────────────────────────────────────────────
_RATE_RE = re.compile(r"^-?\d+\.\d+$")


def _parse_rate_cell(text):
    """None for blank/N/A/n-a cells (a real, disclosed source gap -- see
    module docstring's Grapevine-Colleyville/South Dallas Fair Park/Levee
    District examples) -- never coerced to 0.0, which would silently
    misrepresent "not published" as "published as zero"."""
    text = (text or "").strip()
    if not text or text.upper() in ("N/A", "NA", "-"):
        return None
    if _RATE_RE.match(text):
        return float(text)
    return None  # non-numeric / unparseable -- treated as absent, not a crash


# Matches a TRAILING 4-digit 19xx/20xx token, not exact-equality against
# the whole string -- required because the real toggle text is "<chevron
# span text><nbsp><nbsp>2024 ", not a bare "2024" leaf (see
# find_year_tables()'s own CORRECTION note below for why the original
# exact-match version of this regex was wrong).
_YEAR_TOKEN_RE = re.compile(r"((?:19|20)\d{2})\s*$")


def find_year_tables(soup):
    """Walks the parsed page in document order, pairing each real <table>
    with the nearest preceding accordion-toggle year label.

    CORRECTION (real bug, caught by a --dry-run against the real saved
    past-tax-rates.php file and Diego's own direct verification of that
    file's markup -- PX-20260829-07 follow-up): the ORIGINAL version of
    this function assumed a flat "[2024](#)"-style tab-anchor immediately
    before each table, and looked for a standalone leaf element whose own
    text was EXACTLY a 4-digit year. That assumption was wrong -- it
    correctly failed loud (raised ValueError, did not silently misparse or
    guess) rather than shipping a bad parse. The REAL structure is a
    Bootstrap-style accordion: each year is one collapsible panel -- a
    toggle `<a>` whose own text ends with the year (real markup: `<a ...>
    <span class="chevron">...</span>&nbsp;&nbsp;2024 </a>`), immediately
    followed by its content `<div id="displayN">` wrapping that year's
    full entity `<table>`. The `displayN` id numbers are NOT in year order
    (confirmed real examples: display10=2024, display1=2017, display3=
    2015) -- this function takes the year ONLY from the toggle `<a>`'s own
    text via `_YEAR_TOKEN_RE`, NEVER from the id, precisely because the id
    ordering can't be trusted as a proxy for year. Real coverage on this
    page is 2015-2024 inclusive (10 complete years, including 2020 --
    an earlier pass mis-scoped coverage as possibly gapped around 2020;
    it is not) -- the current-year page covers 2025 separately, for 11
    complete years total across both sources, no gap anywhere.

    Returns a list of (year:int, table_soup) tuples, in document order.

    Fail-loud guard: raises ValueError if any table has no preceding
    toggle-derived year label, or if two tables end up mapped to the SAME
    year (both would silently misattribute or duplicate a year's data) --
    this is a structural-drift detector for when Dallas eventually changes
    this page's markup again, not a normal-operation code path."""
    results = []
    seen_years = set()
    current_year = None
    for el in soup.find_all(True):
        if el.name == "table":
            if current_year is None:
                raise ValueError(
                    "found a rate table with no preceding accordion-toggle "
                    "year label -- page structure has changed since this "
                    "parser was written; do not guess, fix the parser "
                    "against the real current markup first"
                )
            if current_year in seen_years:
                raise ValueError(
                    f"year {current_year} already has a table -- a second "
                    f"table mapped to the same year would silently "
                    f"duplicate or misattribute that year's rows"
                )
            seen_years.add(current_year)
            results.append((current_year, el))
            current_year = None  # require a fresh toggle label before the next table
        elif el.name == "a":
            # get_text() concatenates the chevron span's own (icon/empty)
            # text with the trailing year text node -- .replace("\xa0", " ")
            # normalizes &nbsp;&nbsp; padding before stripping, since NBSP
            # is whitespace to a human eye but str.strip() alone already
            # treats \xa0 as stripped whitespace too (Python's str.isspace()
            # is True for \xa0) -- the explicit replace is just for clarity
            # at the regex match site, not strictly required for stripping.
            text = el.get_text().replace("\xa0", " ").strip()
            m = _YEAR_TOKEN_RE.search(text)
            if m:
                current_year = int(m.group(1))
    return results


# Matches a leading/embedded 4-digit year immediately followed by "Tax
# Rates" (real example: "<h2>2025 Tax Rates</h2>") -- deliberately NOT a
# bare year-anywhere-on-the-page match, since that would also fire on
# unrelated copyright-year or footer text elsewhere on the current-year
# page. Case-insensitive since heading capitalization isn't guaranteed.
_CURRENT_YEAR_HEADING_RE = re.compile(r"((?:19|20)\d{2})\s*Tax Rates", re.IGNORECASE)


def find_current_year_table(soup):
    """The current-year page (tax-rates.php) is NOT an accordion at all --
    a SECOND real structural correction, distinct from find_year_tables()'s
    own above, both found the same way: Diego running --dry-run against
    the real saved file and verifying the actual markup directly.

    CORRECTION: the original assumption (before this fix) implicitly
    treated this page like a one-panel version of the history accordion --
    i.e. some year-labeling element immediately preceding the single
    table. That is wrong. The real page has no toggle/label anywhere near
    the table at all: the table's own immediate predecessor in the DOM is
    unrelated contact-info widget markup. The table's year is instead
    named by a heading ELSEWHERE on the page (real example: `<h2>2025 Tax
    Rates</h2>`) with no fixed structural relationship to the table's own
    position. This function does NOT try to locate "the nearest preceding
    label" (that concept doesn't apply here) -- it independently finds (a)
    the page's one rate table and (b) the page's one tax-year heading, and
    pairs them.

    Fail-loud guards (same philosophy as find_year_tables(), a different
    page shape does not mean a different tolerance for guessing):
      - raises ValueError if the page has zero or more than one <table>
        (this parser assumes exactly one -- if Dallas ever adds a second
        table to this page, that is a structural change to investigate,
        not something to guess about).
      - raises ValueError if no heading matches "<year> Tax Rates" (the
        page's real year-naming convention, per the h2 example above).
      - raises ValueError if headings name MORE THAN ONE distinct year
        (ambiguous which year the single table belongs to -- do not
        guess).

    Returns a single (year:int, table_soup) tuple."""
    tables = soup.find_all("table")
    if len(tables) != 1:
        raise ValueError(
            f"expected exactly one rate table on the current-year page, "
            f"found {len(tables)} -- page structure has changed since this "
            f"parser was written; do not guess, fix the parser against the "
            f"real current markup first"
        )

    years_found = set()
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = heading.get_text().replace("\xa0", " ")
        m = _CURRENT_YEAR_HEADING_RE.search(text)
        if m:
            years_found.add(int(m.group(1)))

    if not years_found:
        raise ValueError(
            "found the current-year rate table but no heading naming its "
            "tax year (expected something like '<h2>2025 Tax Rates</h2>' "
            "elsewhere on the page -- this page's year label is NOT "
            "adjacent to the table, unlike the history page's accordion) "
            "-- page structure has changed since this parser was written; "
            "do not guess, fix the parser against the real current markup "
            "first"
        )
    if len(years_found) > 1:
        raise ValueError(
            f"found more than one distinct tax year across this page's "
            f"headings ({sorted(years_found)}) -- ambiguous which year the "
            f"single table belongs to; do not guess"
        )

    return years_found.pop(), tables[0]


def _norm_header(text):
    """Whitespace-insensitive header-matching key. The current-year page's
    real header text has irregular internal spacing around the ampersand
    ("M &amp; O" / "I &amp;S" -- decoded: "M & O" / "I &S", inconsistent
    spacing on the two sides) -- a plain "M&O" in cell / "M&O" in target
    substring match would miss "M & O" entirely. Stripping ALL whitespace
    before comparing closes this without assuming one canonical spelling,
    and is a no-op for the history page's already-clean "M&O"/"I&S" text."""
    return re.sub(r"\s+", "", text).upper()


def _find_header_row_index(trs):
    """Prefer the <tr> whose own CLASS marks it as a header row -- the
    current-year page's real shape puts header-ness on the ROW
    (`<tr class="tableHeaderBlue">`) with plain `<td>` cells, not on `<th>`
    cells like the history accordion's tables. Falls back to the first row
    (index 0) when no row carries a header-ish class, which is exactly the
    history page's real shape (`<th>` cells in the first row, no special
    class needed) -- so this is additive, not a behavior change for the
    already-passing history fixtures."""
    for i, tr in enumerate(trs):
        classes = " ".join(tr.get("class") or []).lower()
        if "header" in classes:
            return i
    return 0


def parse_year_table(table_soup, tax_year):
    """table_soup: a BeautifulSoup <table> element for one year's rate
    table (any shape: history's 4-col/6-col accordion tables, or the
    current-year page's differently-classed table). Returns a list of
    dicts: entity_name, entity_code, tax_year, mo_rate, is_rate, rate,
    skip_reason.

    Header-driven column mapping (not positional-by-count) -- reads the
    real header row's cell text to find M&O / I&S / TOTAL TAX RATE column
    indices, so a shape variation (the 2015 6-column table vs everyone
    else's 4-column table, or the current-year page's row-classed header
    with irregular spacing -- see _find_header_row_index()/_norm_header()
    above) is handled by the SAME code path rather than a special case per
    page."""
    rows_out = []
    trs = table_soup.find_all("tr")
    if not trs:
        return rows_out
    header_idx = _find_header_row_index(trs)
    header_cells = [_norm_header(c.get_text()) for c in trs[header_idx].find_all(["th", "td"])]

    def _col(*names):
        for i, cell in enumerate(header_cells):
            if any(_norm_header(n) in cell for n in names):
                return i
        return None

    entity_idx = _col("ENTITY NAME") or 0
    mo_idx = _col("M&O")
    is_idx = _col("I&S")
    total_idx = _col("TOTAL TAX RATE", "TOTAL")

    for tr in trs[header_idx + 1:]:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells or len(cells) <= entity_idx or not cells[entity_idx]:
            continue
        raw_name = cells[entity_idx]
        entity_code, _ = resolve_entity_identity(raw_name)
        mo_rate = _parse_rate_cell(cells[mo_idx]) if mo_idx is not None and mo_idx < len(cells) else None
        is_rate = _parse_rate_cell(cells[is_idx]) if is_idx is not None and is_idx < len(cells) else None
        total = _parse_rate_cell(cells[total_idx]) if total_idx is not None and total_idx < len(cells) else None

        skip_reason = None
        if total is None and mo_rate is None and is_rate is None:
            # Real source gap (module docstring's N/A examples) -- this row
            # carries no usable rate at all for this year. Loud skip, not a
            # silent drop: counted in the loader's ledger under this reason.
            skip_reason = "no_rate_published_this_year"
        elif total is None and (mo_rate is not None or is_rate is not None):
            # Has a split but no published total -- derive it rather than
            # skip, since this is the one case where the source's own two
            # components fully determine the missing total.
            total = round((mo_rate or 0.0) + (is_rate or 0.0), 6)

        rows_out.append({
            "entity_name": raw_name,
            "entity_code": entity_code,
            "tax_year": tax_year,
            "mo_rate": mo_rate,
            "is_rate": is_rate,
            "rate": total,
            "skip_reason": skip_reason,
        })
    return rows_out
