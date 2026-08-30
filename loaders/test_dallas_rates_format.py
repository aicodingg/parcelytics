"""
loaders/test_dallas_rates_format.py -- fixture tests for dallas_rates_format.py
and load_dallas_tax_rates.py (PX-20260829-07 Task 5).

HONEST DISCLOSURE -- REVISED after Diego's live --dry-run + direct markup
verification (the original fixtures below used an invented "<p>YEAR</p>"
tab-anchor shape that turned out NOT to match the real page; see
dallas_rates_format.py's find_year_tables() docstring for the full
correction). This revision replaces every year-marker fixture with the
REAL accordion structure Diego quoted directly from the saved page:

    <a ...><span class="chevron">...</span>&nbsp;&nbsp;2024 </a>
    <div id="displayN"> ... <table>...</table> ... </div>

What is Diego's own direct, verbatim quote: the toggle <a> containing a
<span class="chevron"> followed by "&nbsp;&nbsp;YYYY " as trailing text,
the immediately-following <div id="displayN"> wrapping each year's table,
and the confirmed real id/year pairings display10=2024, display1=2017,
display3=2015 (ids NOT in year order). What is REASONABLY INFERRED, not
directly quoted, and so may not byte-for-byte match the real page: the
toggle <a>'s href/data-toggle/aria attributes (not given in Diego's
quote), the chevron span's inner glyph content, and surrounding wrapper
divs/classes. The entity names, rate values, and 4-col/6-col shape split
are unchanged from the original disclosure -- those were independently
confirmed live in PX-20260829-06/-07 research and are not affected by
the accordion-structure correction.

Per Diego's explicit instruction after the situs/owner field-name bug
(an approximated fixture header let a real mismatch ship undetected):
paste the real, directly-quoted structure in rather than continue
approximating it. The fixtures below do that for every part Diego
verified himself; only the undisclosed attribute/wrapper details above
remain inferred, and are flagged as such rather than presented as
verified.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from loaders import dallas_rates_format as fmt
from loaders import load_dallas_tax_rates as loader

from bs4 import BeautifulSoup

PASS_COUNT = 0
FAIL_COUNT = 0


def check(label, condition):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"[PASS] {label}")
    else:
        FAIL_COUNT += 1
        print(f"[FAIL] {label}")


# ── canonicalize_name() ──────────────────────────────────────────────────
check("canonicalize_name closes hyphen/space variant (Grapevine-Colleyville)",
      fmt.canonicalize_name("Grapevine-Colleyville ISD") ==
      fmt.canonicalize_name("Grapevine - Colleyville ISD"))

check("canonicalize_name closes internal-space variant (SouthSide/South Side)",
      fmt.canonicalize_name("SouthSide PREM PID") ==
      fmt.canonicalize_name("South Side PREM PID"))

check("canonicalize_name does NOT close a genuine misspelling on its own",
      fmt.canonicalize_name("City of Lewisvile") !=
      fmt.canonicalize_name("City of Lewisville"))

check("canonicalize_name is case-insensitive",
      fmt.canonicalize_name("DALLAS COUNTY") == fmt.canonicalize_name("Dallas County"))


# ── resolve_entity_identity() / DALLAS_ENTITY_ALIASES ───────────────────
code_l14, skip1 = fmt.resolve_entity_identity("Levee District 14")
code_l4_typo, skip2 = fmt.resolve_entity_identity("Levee District l4")   # lowercase L typo, 2019
check("Levee District 14 and its lowercase-L typo resolve to the SAME entity_code",
      code_l14 == code_l4_typo)
check("resolve_entity_identity never sets skip_reason (name resolution always produces a code)",
      skip1 is None and skip2 is None)

code_l4_real, _ = fmt.resolve_entity_identity("Levee District 4")   # genuinely distinct entity
check("Levee District 4 (distinct real entity) does NOT collide with District 14's code "
      "(this is the REAL bug this fixture caught: the original truncated-prefix "
      "dallas_entity_code() collided these two; fixed via hashing the full "
      "canonical key -- see dallas_entity_code()'s own PIVOT docstring)",
      code_l4_real != code_l14)

check("entity_code fits VARCHAR(10) exactly (DAL + 7 hex chars)",
      len(code_l14) == 10 and len(code_l4_real) == 10)

code_lewisvile, _ = fmt.resolve_entity_identity("City of Lewisvile")     # missing 2nd l, most years
code_lewisville, _ = fmt.resolve_entity_identity("City of Lewisville")  # correct spelling, one year
check("City of Lewisvile (typo) and City of Lewisville (correct) resolve to the SAME entity_code "
      "via the maintained DALLAS_ENTITY_ALIASES crosswalk",
      code_lewisvile == code_lewisville)

# LIVE FINDING (Diego, deployed Dallas rates page, post-PX-20260829-07):
# "Carrollton-Farmers Br ISD" / "Carrollton-Farmers Branch ISD" rendered as
# two distinct entities (DAL1E6C7C5 / DALCA625EF) before this alias existed.
code_cfb_br, _ = fmt.resolve_entity_identity("Carrollton-Farmers Br ISD")
code_cfb_branch, _ = fmt.resolve_entity_identity("Carrollton-Farmers Branch ISD")
check("Carrollton-Farmers Br ISD and Carrollton-Farmers Branch ISD (real live-finding "
      "drift, 'Br'/'Branch' abbreviation) resolve to the SAME entity_code",
      code_cfb_br == code_cfb_branch)

code_wylie_plain, _ = fmt.resolve_entity_identity("City of Wylie")
code_wylie_suffixed, _ = fmt.resolve_entity_identity("City of Wylie (Collin Co)")
check("City of Wylie and 'City of Wylie (Collin Co)' resolve to the SAME entity_code",
      code_wylie_plain == code_wylie_suffixed)

code_unseen, skip_unseen = fmt.resolve_entity_identity("Some Brand New District Never Seen Before")
check("a never-seen name still gets a deterministic code (canonicalize_name fallback), not a crash",
      code_unseen is not None and skip_unseen is None)


# ── _parse_rate_cell() ────────────────────────────────────────────────────
check("_parse_rate_cell parses a real decimal rate", fmt._parse_rate_cell("0.532100") == 0.5321)
check("_parse_rate_cell returns None for blank", fmt._parse_rate_cell("") is None)
check("_parse_rate_cell returns None for N/A", fmt._parse_rate_cell("N/A") is None)
check("_parse_rate_cell returns None for n/a lowercase", fmt._parse_rate_cell("n/a") is None)
check("_parse_rate_cell returns None for a lone dash", fmt._parse_rate_cell("-") is None)
check("_parse_rate_cell never coerces blank to 0.0",
      fmt._parse_rate_cell("") != 0.0 and fmt._parse_rate_cell("") is None)


# ── find_year_tables() + parse_year_table(): 4-column shape (2016-2024) ──
# Real accordion structure (Diego's direct quote): toggle <a> with a
# <span class="chevron"> and trailing "&nbsp;&nbsp;YYYY " text, immediately
# followed by <div id="displayN"> wrapping the table. Ids deliberately out
# of numeric AND year order here (display10 for the FIRST/2024 panel,
# display2 for the SECOND/2023 panel) to prove the parser tracks document
# order + toggle text, never the id.
HTML_4COL = """
<html><body>
<div class="tab-content">
<a href="#display10" data-toggle="collapse" aria-expanded="false" class="accordion-toggle">
<span class="chevron">...</span>&nbsp;&nbsp;2024 </a>
<div id="display10">
<table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>0.216514</td><td>0.024316</td><td>0.240830</td></tr>
<tr><td>City of Lewisvile</td><td>0.410000</td><td>0.070000</td><td>0.480000</td></tr>
<tr><td>Grapevine-Colleyville ISD</td><td>n/a</td><td>n/a</td><td>n/a</td></tr>
</table>
</div>
<a href="#display2" data-toggle="collapse" aria-expanded="false" class="accordion-toggle">
<span class="chevron">...</span>&nbsp;&nbsp;2023 </a>
<div id="display2">
<table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>0.220000</td><td>0.025000</td><td>0.245000</td></tr>
<tr><td>City of Lewisville</td><td>0.415000</td><td>0.071000</td><td>0.486000</td></tr>
</table>
</div>
</div>
</body></html>
"""

soup4 = BeautifulSoup(HTML_4COL, "html.parser")
year_tables_4 = fmt.find_year_tables(soup4)
check("find_year_tables finds both 2024 and 2023 tables, in document order",
      [y for y, _ in year_tables_4] == [2024, 2023])

rows_2024 = fmt.parse_year_table(year_tables_4[0][1], 2024)
check("2024 table yields 3 data rows", len(rows_2024) == 3)

dallas_2024 = next(r for r in rows_2024 if r["entity_name"] == "Dallas County")
check("Dallas County 2024 mo_rate/is_rate/rate parsed correctly",
      dallas_2024["mo_rate"] == 0.216514 and dallas_2024["is_rate"] == 0.024316
      and dallas_2024["rate"] == 0.24083)
check("Dallas County 2024 row is accepted (skip_reason None)",
      dallas_2024["skip_reason"] is None)

grapevine_2024 = next(r for r in rows_2024 if r["entity_name"] == "Grapevine-Colleyville ISD")
check("Grapevine-Colleyville ISD 2024 (all n/a) gets skip_reason='no_rate_published_this_year', "
      "not a coerced 0.0",
      grapevine_2024["skip_reason"] == "no_rate_published_this_year"
      and grapevine_2024["mo_rate"] is None
      and grapevine_2024["rate"] is None)

rows_2023 = fmt.parse_year_table(year_tables_4[1][1], 2023)
lewisville_2023 = next(r for r in rows_2023 if r["entity_name"] == "City of Lewisville")
lewisvile_2024 = next(r for r in rows_2024 if r["entity_name"] == "City of Lewisvile")
check("City of Lewisvile (2024, typo) and City of Lewisville (2023, correct) share one entity_code "
      "across years, proving the crosswalk works end-to-end through the real parse path",
      lewisvile_2024["entity_code"] == lewisville_2023["entity_code"])


# ── 2015's 6-column shape (ACT CODE / DCAD CODE) ─────────────────────────
# Uses the real confirmed id: display3=2015 (per Diego's direct quote).
HTML_6COL_2015 = """
<html><body>
<a href="#display3" data-toggle="collapse" aria-expanded="false" class="accordion-toggle">
<span class="chevron">...</span>&nbsp;&nbsp;2015 </a>
<div id="display3">
<table>
<tr><th>ENTITY NAME</th><th>ACT CODE</th><th>DCAD CODE</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>1002</td><td>DC</td><td>0.200000</td><td>0.020000</td><td>0.220000</td></tr>
<tr><td>South Dallas/Fair Park PID</td><td>2201</td><td></td><td></td><td></td><td>0.000000</td></tr>
<tr><td>Oak Lawn-Hi Line</td><td>2210</td><td></td><td>0.030000</td><td>0.005000</td><td>0.035000</td></tr>
</table>
</div>
</body></html>
"""

soup2015 = BeautifulSoup(HTML_6COL_2015, "html.parser")
year_tables_2015 = fmt.find_year_tables(soup2015)
check("find_year_tables handles the 2015 6-column table (single year)",
      [y for y, _ in year_tables_2015] == [2015])

rows_2015 = fmt.parse_year_table(year_tables_2015[0][1], 2015)
check("2015 6-column table yields 3 data rows despite the extra ACT/DCAD code columns",
      len(rows_2015) == 3)

dallas_2015 = next(r for r in rows_2015 if r["entity_name"] == "Dallas County")
check("2015 header-driven column mapping still finds M&O/I&S/TOTAL correctly "
      "(not thrown off by the two extra leading code columns)",
      dallas_2015["mo_rate"] == 0.20 and dallas_2015["is_rate"] == 0.02
      and dallas_2015["rate"] == 0.22)

south_dallas_2015 = next(r for r in rows_2015 if r["entity_name"] == "South Dallas/Fair Park PID")
check("2015 PID row with blank M&O/I&S but TOTAL=0.000000 is a real published zero, "
      "not treated as no_rate_published (0.0 parses as a real float, distinct from blank/N/A)",
      south_dallas_2015["rate"] == 0.0 and south_dallas_2015["skip_reason"] is None)

oaklawn_2015 = next(r for r in rows_2015 if r["entity_name"] == "Oak Lawn-Hi Line")
code_oaklawn_2015, _ = fmt.resolve_entity_identity("Oak Lawn-Hi Line")
code_oaklawn_other, _ = fmt.resolve_entity_identity("Oak Lawn-Hi Line PID")
check("2015's 'Oak Lawn-Hi Line' and another year's 'Oak Lawn-Hi Line PID' share one entity_code",
      code_oaklawn_2015 == code_oaklawn_other)


# ── find_current_year_table(): the current-year page's REAL, different
# shape -- NOT an accordion at all (Diego's live --dry-run + direct markup
# verification, second real correction of this same brief). Real facts
# this fixture builds against, per his direct quote:
#   - the table's year comes from an <h2>2025 Tax Rates</h2> heading
#     elsewhere on the page, NOT from any element adjacent to the table
#     (the table's real immediate predecessor is contact-info widget
#     markup, deliberately included below to prove the parser doesn't
#     depend on "nearest preceding element" the way the history page's
#     accordion does);
#   - the header row's class lives on the <tr> itself
#     ("tableHeaderBlue"), not on the cells;
#   - header text has irregular spacing: "M &amp; O" / "I &amp;S" (decoded:
#     "M & O" / "I &S") -- inconsistent on each side of the ampersand;
#   - the first real data row, Diego's own verbatim quote:
#     <tr><td>Dallas County</td><td style="text-align: center;">0.208765
#     </td><td style="text-align: center;">0.006735</td>
#     <td style="text-align: center;">0.215500</td></tr>
# NOT directly quoted / reasonably inferred: the exact contact-info widget
# markup (only its general presence/position was described) and the
# header row's own cell tag (assumed <td>, consistent with "tableHeaderBlue"
# living on the row rather than <th> cells, but not independently quoted).
HTML_CURRENT_PAGE = """
<html><body>
<div class="page-header">
<h2>2025 Tax Rates</h2>
</div>
<div class="contact-widget">
<p>Dallas County Tax Office -- 500 Elm Street -- (214) 555-0100</p>
</div>
<table>
<tr class="tableHeaderBlue"><td>ENTITY NAME</td><td>M &amp; O</td><td>I &amp;S</td><td>TOTAL TAX RATE</td></tr>
<tr><td>Dallas County</td><td style="text-align: center;">0.208765</td><td style="text-align: center;">0.006735</td><td style="text-align: center;">0.215500</td></tr>
<tr><td>City of Lewisvile</td><td style="text-align: center;">0.400000</td><td style="text-align: center;">0.065000</td><td style="text-align: center;">0.465000</td></tr>
</table>
</body></html>
"""

soup_current = BeautifulSoup(HTML_CURRENT_PAGE, "html.parser")
current_year, current_table = fmt.find_current_year_table(soup_current)
check("find_current_year_table() finds year 2025 from the <h2> heading, not from "
      "anything adjacent to the table (whose real predecessor is unrelated "
      "contact-info widget markup)",
      current_year == 2025)

rows_current = fmt.parse_year_table(current_table, current_year)
check("current-page table yields 2 data rows despite the row-classed header "
      "(tableHeaderBlue on the <tr>, not on the cells)",
      len(rows_current) == 2)

dallas_current = next(r for r in rows_current if r["entity_name"] == "Dallas County")
check("current-page header matching normalizes irregular spacing ('M & O' / 'I &S') "
      "and still correctly finds Dallas County's real mo_rate/is_rate/rate "
      "(0.208765 / 0.006735 / 0.215500, Diego's own verbatim quote)",
      dallas_current["mo_rate"] == 0.208765 and dallas_current["is_rate"] == 0.006735
      and dallas_current["rate"] == 0.2155)
check("current-page Dallas County row is accepted (skip_reason None)",
      dallas_current["skip_reason"] is None)

lewisvile_current, _ = fmt.resolve_entity_identity("City of Lewisvile")
check("current-page's 'City of Lewisvile' (typo) still resolves through the same "
      "alias crosswalk as the history page's rows",
      lewisvile_current == lewisville_2023["entity_code"])


# ── find_current_year_table() fail-loud guards ───────────────────────────
try:
    fmt.find_current_year_table(BeautifulSoup("<html><body></body></html>", "html.parser"))
    check("find_current_year_table raises ValueError when the page has zero tables", False)
except ValueError:
    check("find_current_year_table raises ValueError when the page has zero tables", True)

try:
    fmt.find_current_year_table(BeautifulSoup(
        "<html><body><h2>2025 Tax Rates</h2>"
        "<table><tr><td>a</td></tr></table>"
        "<table><tr><td>b</td></tr></table>"
        "</body></html>", "html.parser"))
    check("find_current_year_table raises ValueError when the page has more than one table",
          False)
except ValueError:
    check("find_current_year_table raises ValueError when the page has more than one table",
          True)

try:
    fmt.find_current_year_table(BeautifulSoup(
        "<html><body><table><tr><td>a</td></tr></table></body></html>", "html.parser"))
    check("find_current_year_table raises ValueError when no heading names a tax year", False)
except ValueError:
    check("find_current_year_table raises ValueError when no heading names a tax year", True)

try:
    fmt.find_current_year_table(BeautifulSoup(
        "<html><body><h2>2024 Tax Rates</h2><h2>2025 Tax Rates</h2>"
        "<table><tr><td>a</td></tr></table></body></html>", "html.parser"))
    check("find_current_year_table raises ValueError when headings name two different "
          "years (ambiguous, does not guess)", False)
except ValueError:
    check("find_current_year_table raises ValueError when headings name two different "
          "years (ambiguous, does not guess)", True)


# ── find_year_tables() fail-loud guards (accordion-shaped) ───────────────
try:
    fmt.find_year_tables(BeautifulSoup(
        "<html><body><div id=\"display1\"><table><tr><td>x</td></tr></table></div>"
        "</body></html>", "html.parser"))
    check("find_year_tables raises ValueError on a table with no preceding toggle <a> year label",
          False)
except ValueError:
    check("find_year_tables raises ValueError on a table with no preceding toggle <a> year label",
          True)

try:
    fmt.find_year_tables(BeautifulSoup(
        "<html><body>"
        "<a><span class=\"chevron\">...</span>&nbsp;&nbsp;2020 </a>"
        "<div id=\"display5\"><table><tr><td>a</td></tr></table></div>"
        "<a><span class=\"chevron\">...</span>&nbsp;&nbsp;2020 </a>"
        "<div id=\"display6\"><table><tr><td>b</td></tr></table></div>"
        "</body></html>", "html.parser"))
    check("find_year_tables raises ValueError on two toggles mapped to the same year", False)
except ValueError:
    check("find_year_tables raises ValueError on two toggles mapped to the same year", True)


# ── displayN id-vs-year-order mismatch: the exact real case Diego flagged ─
# Real confirmed pairings: display10=2024, display1=2017, display3=2015.
# Deliberately laid out in document order 2015, 2024, 2017 -- neither
# ascending by year nor ascending by id number -- to prove year identity
# comes ONLY from the toggle <a>'s text, never from the id or from any
# assumed ordering.
HTML_ID_YEAR_MISMATCH = """
<html><body>
<a><span class="chevron">...</span>&nbsp;&nbsp;2015 </a>
<div id="display3"><table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>0.200000</td><td>0.020000</td><td>0.220000</td></tr>
</table></div>
<a><span class="chevron">...</span>&nbsp;&nbsp;2024 </a>
<div id="display10"><table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>0.216514</td><td>0.024316</td><td>0.240830</td></tr>
</table></div>
<a><span class="chevron">...</span>&nbsp;&nbsp;2017 </a>
<div id="display1"><table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Dallas County</td><td>0.210000</td><td>0.021000</td><td>0.231000</td></tr>
</table></div>
</body></html>
"""
soup_mismatch = BeautifulSoup(HTML_ID_YEAR_MISMATCH, "html.parser")
year_tables_mismatch = fmt.find_year_tables(soup_mismatch)
check("find_year_tables assigns years by toggle text + document order, ignoring displayN "
      "entirely -- 2015 (id display3) then 2024 (id display10) then 2017 (id display1), "
      "the exact real out-of-order id/year pairing Diego confirmed",
      [y for y, _ in year_tables_mismatch] == [2015, 2024, 2017])


# ── total-derived-from-components case ───────────────────────────────────
HTML_MISSING_TOTAL = """
<html><body>
<a href="#display7" data-toggle="collapse" aria-expanded="false" class="accordion-toggle">
<span class="chevron">...</span>&nbsp;&nbsp;2021 </a>
<div id="display7">
<table>
<tr><th>ENTITY NAME</th><th>M&amp;O</th><th>I&amp;S</th><th>TOTAL TAX RATE</th></tr>
<tr><td>Test Entity</td><td>0.100000</td><td>0.050000</td><td></td></tr>
</table>
</div>
</body></html>
"""
soup_mt = BeautifulSoup(HTML_MISSING_TOTAL, "html.parser")
rows_mt = fmt.parse_year_table(fmt.find_year_tables(soup_mt)[0][1], 2021)
check("a missing TOTAL with both components present is derived as mo_rate + is_rate, not skipped",
      rows_mt[0]["rate"] == 0.15 and rows_mt[0]["skip_reason"] is None)


# ── load_dallas_tax_rates.py: build_rows() + UPSERT_SQL scoping ─────────
mixed_parsed_rows = rows_2024 + rows_2015 + rows_mt
accepted, skip_counter, skipped_names = loader.build_rows(mixed_parsed_rows)

check("build_rows separates accepted vs skipped correctly (Grapevine-Colleyville is the only skip "
      "in this mixed batch)",
      skip_counter.get("no_rate_published_this_year") == 1
      and len(accepted) == len(mixed_parsed_rows) - 1)

check("every accepted tuple's first element is 'DALLAS' (county scoping)",
      all(t[0] == "DALLAS" for t in accepted))

check("build_rows preserves mo_rate/is_rate alongside the total in each accepted tuple",
      any(t[1] == dallas_2024["entity_code"] and t[5] == 0.216514 and t[6] == 0.024316
          for t in accepted))

check("UPSERT_SQL's ON CONFLICT target is (county_code, entity_code, tax_year)",
      "ON CONFLICT (county_code, entity_code, tax_year)" in loader.UPSERT_SQL)

check("UPSERT_SQL writes county_code as the first inserted column",
      loader.UPSERT_SQL.strip().split("(", 1)[1].split(")")[0].strip().startswith("county_code"))

check("UPSERT_SQL never references TRAVIS literally (this loader is Dallas-only, county_code "
      "comes from build_rows()'s COUNTY_CODE constant, not embedded in SQL)",
      "TRAVIS" not in loader.UPSERT_SQL)

check("loader.COUNTY_CODE constant is 'DALLAS'", loader.COUNTY_CODE == "DALLAS")


# ── check_entity_code_collisions(): the actual guard that would have caught
# the Levee District 14/4 bug BEFORE any write, plus proof it's a real,
# firing check (not a no-op) against a deliberately forced collision ──────
check("check_entity_code_collisions() passes clean on the real fixture rows "
      "(2024+2015+2021 tables, post-fix)",
      fmt.check_entity_code_collisions(mixed_parsed_rows) is None)

forced_collision_rows = [
    {"entity_code": "SAMECODE01", "entity_name": "Entity Alpha"},
    {"entity_code": "SAMECODE01", "entity_name": "Entity Beta"},   # different real name, same code -- forced
]
try:
    fmt.check_entity_code_collisions(forced_collision_rows)
    check("check_entity_code_collisions() raises DuplicateEntityCodeError on a forced collision", False)
except fmt.DuplicateEntityCodeError:
    check("check_entity_code_collisions() raises DuplicateEntityCodeError on a forced collision", True)

alias_rows_not_a_collision = [
    {"entity_code": "SAMECODE02", "entity_name": "City of Lewisvile"},
    {"entity_code": "SAMECODE02", "entity_name": "City of Lewisville"},  # same real entity via alias table
]
check("check_entity_code_collisions() does NOT flag two ALIASED spellings of the same real entity",
      fmt.check_entity_code_collisions(alias_rows_not_a_collision) is None)


# ── find_near_duplicate_names(): the audit tool built in direct response
# to the real Carrollton-Farmers Br/Branch ISD live finding ──────────────
already_aliased_names = ["City of Lewisvile", "City of Lewisville", "Dallas County"]
check("find_near_duplicate_names() does NOT flag two ALREADY-ALIASED spellings of the "
      "same real entity (they resolve to one canonical key before comparison, so "
      "there's only one representative to compare against everything else)",
      fmt.find_near_duplicate_names(already_aliased_names) == [])

undetected_drift_names = ["Grapevine-Colleyville ISD", "Grapevine-Colleyville lSD",  # lowercase-L typo
                           "Dallas County"]
undetected_candidates = fmt.find_near_duplicate_names(undetected_drift_names)
check("find_near_duplicate_names() flags a close-but-not-yet-aliased pair as a human-review "
      "candidate (simulates catching the NEXT Carrollton-Br/Branch-style drift before a "
      "live load, not after)",
      any({"Grapevine-Colleyville ISD", "Grapevine-Colleyville lSD"} == {a, b}
          for a, b, _ in undetected_candidates))

levee_pair_names = ["Levee District 14", "Levee District 4"]
levee_candidates = fmt.find_near_duplicate_names(levee_pair_names, threshold=0.80)
check("find_near_duplicate_names() DOES flag the real, genuinely-distinct 'Levee District "
      "14'/'Levee District 4' pair as a candidate at a lower threshold -- by design, a "
      "similarity score alone can't tell real drift from real distinct entities; a human "
      "reviewing this candidate is expected to correctly decline to alias it (as already "
      "happened for this exact pair -- see dallas_entity_code()'s own docstring)",
      any({"Levee District 14", "Levee District 4"} == {a, b} for a, b, _ in levee_candidates))

check("find_near_duplicate_names() results are sorted (by first representative name), "
      "per Diego's own 'sort by name' instruction",
      undetected_candidates == sorted(undetected_candidates))


# ── column_exists(): the check that closes the misleading "Schema applied." ─
# message (Diego, live finding: it printed unconditional success while
# applying nothing when mo_rate/is_rate were missing from an existing table).
# No live DB in this sandbox -- fixture-tests the SQL/logic shape only, via
# a minimal fake connection/cursor, mirroring this codebase's established
# convention of testing DB-adjacent logic without a real database.
class _FakeCursor:
    def __init__(self, existing_columns):
        self.existing_columns = existing_columns
        self.last_result = None

    def execute(self, sql, params):
        table, column = params
        found = (table, column) in self.existing_columns
        self.last_result = (1,) if found else None

    def fetchone(self):
        return self.last_result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, existing_columns):
        self._cols = existing_columns

    def cursor(self):
        return _FakeCursor(self._cols)


import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

if "psycopg2" not in _sys.modules:
    # No real psycopg2 install in this sandbox (confirmed via `import
    # psycopg2` -- same disclosed limitation as test_backfill_prop_unit_
    # tax_year_geoid.py / test_dcad_format.py) -- loaders/db.py imports it
    # at module level, so register minimal fake psycopg2/psycopg2.extras
    # stand-ins before importing, same convention as those two files.
    _fake_pg2 = _types.ModuleType("psycopg2")
    _fake_pg2_extras = _types.ModuleType("psycopg2.extras")
    _fake_pg2.extras = _fake_pg2_extras
    _sys.modules["psycopg2"] = _fake_pg2
    _sys.modules["psycopg2.extras"] = _fake_pg2_extras

from loaders import db as db_module  # noqa: E402

conn_with_cols = _FakeConn({("county_tax_rate", "mo_rate"), ("county_tax_rate", "is_rate")})
check("column_exists() returns True when the column is present",
      db_module.column_exists(conn_with_cols, "county_tax_rate", "mo_rate") is True)

conn_missing_cols = _FakeConn(set())
check("column_exists() returns False when the column is missing (this is the exact real "
      "scenario schema.sql's own comment describes: CREATE TABLE IF NOT EXISTS is a "
      "no-op against an existing table, so mo_rate/is_rate can be silently absent)",
      db_module.column_exists(conn_missing_cols, "county_tax_rate", "mo_rate") is False)


# ── assert_production_db(): the hard, fail-loud write-guard (PX-20260830-01
# Task 4) -- verify it actually raises on mismatch/NULL and passes on the
# real expected address, via a minimal fake connection returning a fixed
# "SELECT inet_server_addr()" result.
class _FakeAddrCursor:
    def __init__(self, addr_value):
        self.addr_value = addr_value

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.addr_value,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeAddrConn:
    def __init__(self, addr_value):
        self._addr = addr_value

    def cursor(self):
        return _FakeAddrCursor(self._addr)


prod_conn = _FakeAddrConn("10.30.105.217")
check("assert_production_db() returns the address (no exception) when it matches "
      "EXPECTED_PRODUCTION_HOST exactly",
      db_module.assert_production_db(prod_conn) == "10.30.105.217")

wrong_conn = _FakeAddrConn("127.0.0.1")
_raised_wrong = False
try:
    db_module.assert_production_db(wrong_conn)
except db_module.WrongDatabaseError as e:
    _raised_wrong = "127.0.0.1" in str(e) and "10.30.105.217" in str(e)
check("assert_production_db() raises WrongDatabaseError (naming BOTH the expected and the "
      "actual address) when inet_server_addr() does not match -- this is the real footgun "
      "Task 4 closes: DATABASE_URL silently pointed at the wrong environment",
      _raised_wrong)

null_conn = _FakeAddrConn(None)
_raised_null = False
try:
    db_module.assert_production_db(null_conn)
except db_module.WrongDatabaseError:
    _raised_null = True
check("assert_production_db() raises when inet_server_addr() is NULL (the real shape for a "
      "Unix-domain-socket connection per Postgres's own docs -- never this project's real "
      "production database, which is only ever reached over TCP)",
      _raised_null)


# ── FULL_RELOAD_DELETE_SQL: county-scoping proof (the orphaned-code cleanup
# Diego's alias fix needs) -- same string/regex-assertion convention as
# UPSERT_SQL's own county-scoping checks above ─────────────────────────────
check("FULL_RELOAD_DELETE_SQL targets county_tax_rate",
      "DELETE FROM county_tax_rate" in loader.FULL_RELOAD_DELETE_SQL)
check("FULL_RELOAD_DELETE_SQL filters by county_code (a parameterized placeholder, "
      "never a literal -- county_code is passed in as COUNTY_CODE at call time, so "
      "this can never accidentally hardcode 'TRAVIS' or omit the filter entirely)",
      "WHERE county_code = %s" in loader.FULL_RELOAD_DELETE_SQL)
check("FULL_RELOAD_DELETE_SQL never references TRAVIS literally",
      "TRAVIS" not in loader.FULL_RELOAD_DELETE_SQL)


# ── _require_html() fail-loud message ────────────────────────────────────
import io
import contextlib

buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        loader._require_html("/definitely/does/not/exist.html", "TEST_LABEL")
    check("_require_html sys.exit(1)s on a missing file", False)
except SystemExit as e:
    check("_require_html sys.exit(1)s on a missing file", e.code == 1)
check("_require_html's message names the label and gives Diego a concrete save instruction",
      "TEST_LABEL" in buf.getvalue() and "Save Page As" in buf.getvalue())


print(f"\n{PASS_COUNT} passed, {FAIL_COUNT} failed")
if FAIL_COUNT:
    sys.exit(1)
