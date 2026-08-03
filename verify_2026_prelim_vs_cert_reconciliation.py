"""
Certified-export reconciliation gate (2026 preliminary vs certified).

Real, permanent version of the decomposition Fable specified: certified_total -
preliminary_total must decompose exactly into (matched-account deltas) +
(additions) - (removals), with residual under $1M. This is an accounting
identity -- if it doesn't hold, something about the two universes being
compared isn't actually comparable (different filter, different scope, or
real data drift between when each side was captured).

Run this any time a preliminary-vs-certified total is about to be published
anywhere (marketing, investor-facing, PM briefs) -- not just once. Commit its
output alongside whatever cites the number, so the derivation is never lost
again (this script was built specifically because an earlier $377.84B figure
was computed by a one-off, unsaved terminal script and could not be
reconciled against later -- see KNOWN_LIMITATIONS.md).
"""
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

prelim_ids = set(prelim.keys())
cert_ids = set(cert.keys())
matched_ids = prelim_ids & cert_ids
added_ids = cert_ids - prelim_ids
removed_ids = prelim_ids - cert_ids

matched_delta = sum(cert[g] - prelim[g] for g in matched_ids)
additions = sum(cert[g] for g in added_ids)
removals = sum(prelim[g] for g in removed_ids)

prelim_total = sum(prelim.values())
cert_total = sum(cert.values())
real_delta = cert_total - prelim_total
decomposed_delta = matched_delta + additions - removals
residual = real_delta - decomposed_delta

print(f"\nPreliminary total: {len(prelim_ids):,} accounts, ${prelim_total:,}")
print(f"Certified total:   {len(cert_ids):,} accounts, ${cert_total:,}")
print(f"Real delta (cert - prelim): ${real_delta:,}\n")

print(f"Matched accounts (both universes): {len(matched_ids):,}, value delta: ${matched_delta:,}")
print(f"Added accounts (cert only):        {len(added_ids):,}, value: ${additions:,}")
print(f"  of which AJR-prefixed: {sum(1 for g in added_ids if g.startswith('AJR')):,}")
print(f"Removed accounts (prelim only):     {len(removed_ids):,}, value: ${removals:,}")
print(f"  of which AJR-prefixed: {sum(1 for g in removed_ids if g.startswith('AJR')):,}\n")

print(f"Decomposed delta (matched + additions - removals): ${decomposed_delta:,}")
print(f"RESIDUAL: ${residual:,}")
print(f"GATE {'PASSES' if abs(residual) < 1_000_000 else 'FAILS'} (residual under $1M: {abs(residual) < 1_000_000})")
