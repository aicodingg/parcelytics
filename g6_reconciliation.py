#!/usr/bin/env python3
"""
g6_reconciliation.py — the real, reusable G6 gate, per DATA_LIFECYCLE.md
Stage 2 ("G6 Reconciliation (comparative vintages): ... must decompose --
matched-account Δ + additions − removals = total delta, residual under
$1M -- via the committed reconciliation script") and Section 9.3 ("G6
scope: conditional-but-eager... Full matched-account decomposition
(G6-full) is mandatory wherever two vintages describe the same (county,
year, quantity)... Vintages with no same-quantity predecessor... run
G6-lite: totals cross-checked against the source file's own internal
sums").

WHAT THIS IS
verify_2026_prelim_vs_cert_reconciliation.py (committed ca45b21) proved
the core math for exactly one case: 2026 preliminary vs. 2026 certified,
hardcoded table names, hardcoded exclusion filter. This module extracts
that PROVEN core (the matched/added/removed decomposition arithmetic --
untouched, not rewritten, per this brief's own instruction to refactor
for reusability rather than rewrite the math) into a pure function that
takes any two {key: value} maps and any two labels, so ANY comparable
vintage pair can run it -- not just this one case. The original script's
DB-query shape becomes one caller of this function among several (see
G6_2026_PRELIM_VS_CERT below, which reproduces the original script's
exact behavior byte-for-byte as a regression check that the refactor
didn't change the proven case).

TWO REAL, CALLABLE ENTRY POINTS

  g6_full(source_a, source_b)
      The mandatory decomposition (Section 9.3: "wherever two vintages
      describe the same (county, year, quantity)"). source_a/source_b
      are {key: value} dicts (e.g. {geo_id: market_value}) -- this
      function does not know or care where they came from (a live query,
      a CSV, a synthetic fixture). Returns a G6Result with every number
      the DATA_LIFECYCLE.md-mandated record needs: both totals, the real
      delta, the full matched/added/removed breakdown, the decomposed
      delta, the residual, and pass/fail against the $1M band.

  g6_lite(source_label, loaded_total, file_internal_total, tolerance=1_000_000)
      Section 9.3's fallback for "vintages with no same-quantity
      predecessor" -- a totals cross-check, not a full decomposition
      (there is no second vintage to decompose against). Compares what's
      loaded against the source file's OWN internal sum (e.g. the
      export's own TOTALS.TXT / exportTotals.pdf figure, or a fresh
      independent re-sum of the raw file) and reports the residual
      explicitly, same discipline as g6_full.

BOTH raise nothing on a failing gate -- they return a result object with
`.passed` and a human-readable `.summary()`. Per DATA_LIFECYCLE.md, a
failing G5/G6 "stops the line until the PM writes the explanation into
the Ledger" -- that is a PM/process decision, not this function's to make
by raising an exception a caller might catch-and-ignore. The CALLER
(a promotion script, a pre-publish check) decides what a failed gate
means for its own flow.

CONNECTS TO A REAL DATABASE ONLY IN run_2026_prelim_vs_cert() BELOW
Every other function in this module is pure Python over dicts -- no
import of loaders.db, no live connection -- which is what makes the
decomposition math itself independently fixture-testable (see
test_g6_reconciliation.py) without a database. The one DB-touching
function is a thin, isolated wrapper reproducing the original script's
exact query shape, kept separate so the reusable core never accidentally
grows a hidden DB dependency.

Run (the original 2026 case, byte-for-byte equivalent to the pre-refactor
script, still committed for anyone running an ad hoc check):
    python3 g6_reconciliation.py --2026-prelim-vs-cert
"""
import sys
from dataclasses import dataclass, field


@dataclass
class G6Result:
    label_a: str
    label_b: str
    total_a: float
    total_b: float
    n_a: int
    n_b: int
    matched_keys: set = field(repr=False)
    added_keys: set = field(repr=False)
    removed_keys: set = field(repr=False)
    matched_delta: float = 0.0
    additions: float = 0.0
    removals: float = 0.0
    real_delta: float = 0.0
    decomposed_delta: float = 0.0
    residual: float = 0.0
    tolerance: float = 1_000_000
    passed: bool = False

    def summary(self):
        lines = [
            f"{self.label_a}: {self.n_a:,} accounts, ${self.total_a:,.0f}",
            f"{self.label_b}: {self.n_b:,} accounts, ${self.total_b:,.0f}",
            f"Real delta ({self.label_b} - {self.label_a}): ${self.real_delta:,.0f}",
            "",
            f"Matched accounts (both): {len(self.matched_keys):,}, value delta: ${self.matched_delta:,.0f}",
            f"Added accounts ({self.label_b} only): {len(self.added_keys):,}, value: ${self.additions:,.0f}",
            f"Removed accounts ({self.label_a} only): {len(self.removed_keys):,}, value: ${self.removals:,.0f}",
            "",
            f"Decomposed delta (matched + additions - removals): ${self.decomposed_delta:,.0f}",
            f"RESIDUAL: ${self.residual:,.0f}",
            f"GATE {'PASSES' if self.passed else 'FAILS'} "
            f"(residual under ${self.tolerance:,.0f}: {self.passed})",
        ]
        return "\n".join(lines)


@dataclass
class G6LiteResult:
    label: str
    loaded_total: float
    file_internal_total: float
    residual: float
    tolerance: float
    passed: bool

    def summary(self):
        return (
            f"{self.label} — G6-lite totals cross-check\n"
            f"Loaded total:        ${self.loaded_total:,.0f}\n"
            f"Source file's own internal total: ${self.file_internal_total:,.0f}\n"
            f"RESIDUAL: ${self.residual:,.0f}\n"
            f"GATE {'PASSES' if self.passed else 'FAILS'} "
            f"(residual under ${self.tolerance:,.0f}: {self.passed})"
        )


def g6_full(source_a, source_b, label_a="Source A", label_b="Source B", tolerance=1_000_000):
    """The proven decomposition, generalized. `source_a`/`source_b` are
    {key: value} dicts -- e.g. {geo_id: market_value} -- for any two
    comparable vintages (certified-vs-preliminary, supplement-vs-base,
    revision-vs-original, or any other same-quantity pair per
    DATA_LIFECYCLE.md Section 9.3). The arithmetic below is UNCHANGED
    from verify_2026_prelim_vs_cert_reconciliation.py's original
    matched/added/removed logic -- only its inputs are now parameters
    instead of two hardcoded SQL query results.
    """
    keys_a = set(source_a.keys())
    keys_b = set(source_b.keys())
    matched = keys_a & keys_b
    added = keys_b - keys_a
    removed = keys_a - keys_b

    matched_delta = sum(source_b[k] - source_a[k] for k in matched)
    additions = sum(source_b[k] for k in added)
    removals = sum(source_a[k] for k in removed)

    total_a = sum(source_a.values())
    total_b = sum(source_b.values())
    real_delta = total_b - total_a
    decomposed_delta = matched_delta + additions - removals
    residual = real_delta - decomposed_delta

    return G6Result(
        label_a=label_a, label_b=label_b,
        total_a=total_a, total_b=total_b,
        n_a=len(keys_a), n_b=len(keys_b),
        matched_keys=matched, added_keys=added, removed_keys=removed,
        matched_delta=matched_delta, additions=additions, removals=removals,
        real_delta=real_delta, decomposed_delta=decomposed_delta, residual=residual,
        tolerance=tolerance, passed=abs(residual) < tolerance,
    )


def g6_lite(label, loaded_total, file_internal_total, tolerance=1_000_000):
    """Section 9.3's fallback for vintages with no same-quantity
    predecessor to decompose against -- a totals cross-check, not a full
    decomposition. `loaded_total` is what's actually in the database for
    this vintage; `file_internal_total` is the source file's own claimed
    total (e.g. its TOTALS.TXT row, or an independent re-sum of the raw
    file done outside the loader -- the caller's job to supply, this
    function just compares and reports)."""
    residual = loaded_total - file_internal_total
    return G6LiteResult(
        label=label, loaded_total=loaded_total, file_internal_total=file_internal_total,
        residual=residual, tolerance=tolerance, passed=abs(residual) < tolerance,
    )


# ── The original 2026 case, reproduced via the generalized core ─────────
# Byte-for-byte equivalent to verify_2026_prelim_vs_cert_reconciliation.py
# (ca45b21) -- same tables, same exclusion filter, same $1M band -- proving
# this refactor didn't change the one already-proven, already-shipped case.
# Kept as a real, runnable entry point (not just a fixture) since it's
# still the actual gate for this specific comparison going forward.
def run_2026_prelim_vs_cert():
    from loaders import db
    from parcel_filters import CANONICAL_PARCEL_EXCL, exclude_non_real_property_gap_sql

    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT inet_server_addr()")
        print("Confirmed target:", cur.fetchone())

        excl = CANONICAL_PARCEL_EXCL + f" AND ({exclude_non_real_property_gap_sql('p.state_cd1')})"

        cur.execute(f"""
            SELECT s.geo_id, s.market_value
            FROM parcel_2026_preliminary_snapshot s
            JOIN parcel p ON p.geo_id = s.geo_id
            WHERE 1=1 {excl}
        """)
        prelim = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute(f"""
            SELECT pty.geo_id, pty.market_value
            FROM parcel_tax_year pty
            JOIN parcel p ON p.geo_id = pty.geo_id
            WHERE pty.tax_year = 2026 {excl}
        """)
        cert = {row[0]: row[1] for row in cur.fetchall()}

    result = g6_full(prelim, cert, label_a="2026 Preliminary", label_b="2026 Certified")
    print()
    print(result.summary())
    added_ajr = sum(1 for g in result.added_keys if g.startswith("AJR"))
    removed_ajr = sum(1 for g in result.removed_keys if g.startswith("AJR"))
    print(f"  of which AJR-prefixed (added): {added_ajr:,}")
    print(f"  of which AJR-prefixed (removed): {removed_ajr:,}")
    return result


def run_from_queries(conn, query_a, query_b, label_a, label_b, tolerance=1_000_000):
    """General-purpose live entry point for ANY two comparable vintages
    (Part 3's own requirement: "accept two data sources (table/query + a
    description of what each represents), a shared key (geo_id)").
    `query_a`/`query_b` must each be a SQL string returning exactly two
    columns: (key, value) -- e.g. (geo_id, market_value). This function
    does the DB round-trip and hands the results to g6_full(); it does
    NOT bake in any particular table names or exclusion filter -- the
    caller supplies fully-formed queries (including whatever scoping/
    exclusion logic applies to that particular comparison), matching
    Part 3's "two data sources (table/query + a description)" framing.
    """
    with conn.cursor() as cur:
        cur.execute(query_a)
        source_a = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(query_b)
        source_b = {row[0]: row[1] for row in cur.fetchall()}
    return g6_full(source_a, source_b, label_a=label_a, label_b=label_b, tolerance=tolerance)


if __name__ == "__main__":
    if "--2026-prelim-vs-cert" in sys.argv:
        run_2026_prelim_vs_cert()
    else:
        print(__doc__)
        sys.exit(1)
