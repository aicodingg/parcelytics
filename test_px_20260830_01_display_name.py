"""
test_px_20260830_01_display_name.py -- fixture test for app.py's
_most_recent_entity_names() (PX-20260830-01 Task 3: deterministic
display-name-per-entity_code rule).

app.py can't be imported directly in this sandbox (Flask and psycopg2 are
both unavailable -- `import flask` / `import psycopg2` both fail at
module load time, confirmed). Rather than hand-duplicate the function's
logic in this test file (which could silently drift from the shipped
code and stop actually testing anything), this file extracts the real
FunctionDef node for _most_recent_entity_names out of app.py via the ast
module and execs ONLY that function body in an isolated namespace -- the
callable under test is byte-identical to what app.py actually ships,
just without pulling in Flask/psycopg2 to get it.
"""
import ast
import os

APP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")

with open(APP_PY, encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=APP_PY)

_func_node = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_most_recent_entity_names":
        _func_node = node
        break

if _func_node is None:
    raise RuntimeError("app.py no longer defines _most_recent_entity_names() -- "
                        "this test's extraction target has moved/been renamed")

_namespace = {}
exec(compile(ast.Module(body=[_func_node], type_ignores=[]), filename=APP_PY, mode="exec"), _namespace)
most_recent_entity_names = _namespace["_most_recent_entity_names"]

FAILURES = []


def check(label, condition):
    if condition:
        print(f"  OK   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}")


def row(code, name, year):
    return {"entity_code": code, "entity_name": name, "tax_year": year}


def main():
    # 1. Real merged-entity scenario (PX-20260830-01 Task 1): DAL6A7524C now
    #    carries THREE raw spellings across its merged 2015-2025 span. The
    #    most recent year's spelling ("Oak Hollow Sheffield PID", used
    #    2021-2025) must win regardless of row order.
    rows = [
        row("DAL6A7524C", "OakHollowSheffield", 2015),
        row("DAL6A7524C", "OakHollowSheffield PID", 2016),
        row("DAL6A7524C", "OakHollowSheffield PID", 2018),
        row("DAL6A7524C", "Oak Hollow Sheffield PID", 2019),
        row("DAL6A7524C", "OakHollowSheffield PID", 2020),
        row("DAL6A7524C", "Oak Hollow Sheffield PID", 2025),
    ]
    result = most_recent_entity_names(rows)
    check("merged 3-spelling entity resolves to the MOST RECENT year's spelling",
          result["DAL6A7524C"] == "Oak Hollow Sheffield PID")

    # 2. Order independence -- shuffle the same rows into a different order
    #    (descending by year, i.e. the OPPOSITE of the real query's own
    #    ORDER BY tax_year ASC) and confirm the same answer comes out. This
    #    is the actual bug class being closed: the OLD code was correct
    #    only as a side effect of one specific row order.
    shuffled = list(reversed(rows))
    result_shuffled = most_recent_entity_names(shuffled)
    check("same result regardless of input row order (not order-dependent)",
          result_shuffled["DAL6A7524C"] == "Oak Hollow Sheffield PID")

    # 3. Dallas College (DCCCD) real case (Task 1 pair #4) -- the "most
    #    recent" spelling is the one with the LEGACY acronym appended
    #    (parenthetical "(DCCCD)"), which is chronologically backwards from
    #    what a naive reviewer might expect (see Task 5's docstring note) --
    #    this function must not be fooled by that; it only looks at
    #    tax_year, never at which string looks "newer."
    dcccd_rows = [
        row("DAL474910A", "Dallas College", 2015),
        row("DAL474910A", "Dallas College", 2022),
        row("DAL474910A", "Dallas College (DCCCD)", 2023),
        row("DAL474910A", "Dallas College (DCCCD)", 2025),
    ]
    result_dcccd = most_recent_entity_names(dcccd_rows)
    check("Dallas College: most-recent (2025) spelling 'Dallas College (DCCCD)' wins "
          "despite looking like the 'older' name",
          result_dcccd["DAL474910A"] == "Dallas College (DCCCD)")

    # 4. Multiple distinct codes in one call -- each code's own max-year row
    #    must be picked independently (no cross-contamination between codes).
    multi_rows = [
        row("AAA", "Alpha Old", 2020),
        row("AAA", "Alpha New", 2024),
        row("BBB", "Beta New", 2018),
        row("BBB", "Beta Old", 2015),
    ]
    result_multi = most_recent_entity_names(multi_rows)
    check("multiple codes resolved independently (AAA)",
          result_multi["AAA"] == "Alpha New")
    check("multiple codes resolved independently (BBB)",
          result_multi["BBB"] == "Beta New")

    # 5. Single-year entity (the common case -- most Dallas/Travis entities
    #    never had a name change at all) -- must still work trivially.
    single_rows = [row("CCC", "Only Ever This Name", 2025)]
    check("single-year entity (no drift) resolves to its only name",
          most_recent_entity_names(single_rows)["CCC"] == "Only Ever This Name")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
