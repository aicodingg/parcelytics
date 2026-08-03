"""
Certified-export reconciliation gate (2026 preliminary vs certified).

GENERALIZED (Aug 2026, DATA_LIFECYCLE.md Phase 1 / Section 9.3): the real
matched/added/removed decomposition math that used to live directly in
this file has been extracted into g6_reconciliation.py's g6_full() -- a
reusable core that works for ANY two comparable vintages sharing a key
(geo_id), not just this one case. This file is kept as a thin, named
entry point for the ORIGINAL 2026 case specifically (same tables, same
exclusion filter, same $1M band as always) so nothing that already
depends on running this exact script by name breaks -- but the actual
logic now lives in one place (g6_reconciliation.py), not duplicated here,
so the two copies can no longer drift from each other the way this
codebase's other duplicated-logic incidents did (see parcel_filters.py's
own module docstring for that pattern's history).

For any OTHER vintage pair (a future supplement vs. its base, a revision
vs. an original, a different county entirely), use g6_reconciliation.py's
run_from_queries() or g6_full() directly -- see that module's docstring.

Run this any time a preliminary-vs-certified total is about to be published
anywhere (marketing, investor-facing, PM briefs) -- not just once. Commit its
output alongside whatever cites the number, so the derivation is never lost
again (this script was built specifically because an earlier $377.84B figure
was computed by a one-off, unsaved terminal script and could not be
reconciled against later -- see KNOWN_LIMITATIONS.md).
"""
from g6_reconciliation import run_2026_prelim_vs_cert

if __name__ == "__main__":
    run_2026_prelim_vs_cert()
