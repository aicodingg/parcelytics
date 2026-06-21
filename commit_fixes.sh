#!/usr/bin/env bash
# Commit tonight's bug fixes to parcelytics
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "Committing Parcelytics fixes..."

git add app.py \
        loaders/load_certified_2025.py \
        templates/property.html \
        KNOWN_LIMITATIONS.md \
        review_check.py

git commit -m "Fix hs_cap_loss, land/imprv values, estimate labels, data-source note

- app.py: build_insights() and build_projections() now check the most
  recent year with hs_cap_loss data instead of hardcoding to 2025.
  The 2025 Certified Export does not carry this field; AJR 2021-2024 does.
  hs_buyer_risk and homestead cap projection now fire correctly.

- loaders/load_certified_2025.py: fixed land_seg_mkt_val field position
  from [112:126] to [140:154] (confirmed by file inspection). Dropped
  IMP_INFO.TXT parsing (values are cost-basis, not market); imprv_value
  is now derived as max(0, market_value - land_value), which is both more
  accurate and consistent with TCAD certified methodology.

- templates/property.html:
  - Data-source note added to 5-Year History table footer explaining
    AJR vs Certified Export coverage differences.
  - 5-Year Tax Projection table header now shows ESTIMATES ONLY badge;
    all projected values prefixed with ~; footnotes explain each column.
  - Homestead cap warning now shows the year the cap data is from.

- KNOWN_LIMITATIONS.md: documents taxable_value, tax_billing, land/imprv,
  and hs_cap_loss coverage gaps; confirms all are out of scope for Phase 1.

- review_check.py: updated sanity check script to verify all fixes."

git push origin main

echo ""
echo "Done. Changes pushed to GitHub."
