# Changelog

All notable changes to Parcelytics are tracked here, using [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

- **MAJOR** — structural changes (accounts, a new county live, a major redesign)
- **MINOR** — new features
- **PATCH** — bug fixes and small refinements

Version numbers are tied to actual production deploys, not every commit.

## [1.4.0] — 2026-07-29
- Added the unit-model architecture (Migration M2): a new prop_unit/prop_unit_tax_year layer stores TCAD's real per-unit data (a single geo_id can legitimately contain multiple prop_ids — condo regimes, multi-improvement accounts). parcel_tax_year is now a derived rollup computed from this unit layer (parcel_rollup.py), rather than being written directly by loaders
- All four loaders (certified 2025, 2026 preliminary, certified historical, AJR) refactored to write unit-grain data via a new shared parser (loaders/ears_format.py), fixing three independent data-loss mechanisms that were silently dropping units sharing a geo_id
- New Ingestion Conservation Gate (loaders/ingest_gate.py, checks G1-G6): exact internal reconciliation (source file counts and dollar sums must match the database precisely) plus banded external reconciliation against TCAD's published totals
- New multi-unit panel on the property detail page for accounts with more than one unit; centralized prop_id-to-geo_id resolution so previously-orphaned prop_ids are now searchable
- Homestead-cap risk signals (compute_metrics.py) now gated to single-unit parcels only, to avoid false signals from summed multi-unit values
- Full fixture-based test coverage (83 total checks across 4 new test suites) including deliberate-corruption cases proving the gate's alarms actually fire
- Schema changes are additive only (new tables, one new column) - existing geo_id-keyed queries, URLs, and joins are unaffected
- Live data reload against real production data (Migration M3) not yet run - this commit adds the capability only

## [1.3.8] — 2026-07-29
- Centralized parcel-exclusion filtering into a single canonical, NULL-safe module (parcel_filters.py) - fixes a NULL-propagation bug that silently dropped ~17K post-2024 parcels from every county-wide aggregate, and fixes a drifted /parcels route that had lost its N% exclusion leg
- Fixed an INNER JOIN in the Market Snapshot breakdown that could suppress a parcel's dollar total from both years if either year's data was incomplete
- Added a real, mechanically-verified regression test (verify_parcel_filters_coverage.py) proving every exclusion/peer-matching call site references the canonical definition - found the real call-site count is 8, not the 6 previously documented
- Corrected a false claim in KNOWN_LIMITATIONS.md that an automated verification harness had already run - it hadn't; the harness built here is the real one
- Real root cause of the ~20% Market Snapshot county-total undercount discovered while investigating a LinkedIn post's figures

## [1.3.7] — 2026-07-29
- Added a statement_timeout safety net on all database connections (8s), to prevent a genuinely regressed query from silently consuming a worker's entire timeout budget

## [1.3.6] — 2026-07-29
- Homeowner: Satellite View now appears first on mobile (desktop layout unchanged); homestead exemption explainer cites Tax Code §23.23
- Homeowner: pie chart tooltips show value only, no repeated category name
- Homeowner: acquisition estimator now says "Change vs Seller's Current Bill" instead of the Δ symbol (Investor mode keeps the technical symbol)
- Investor: Value Trend chart moved up, now right after the KPI cards
- Investor: removed the redundant "Tax by Entity" table (duplicated the Historical Tax Rates table and Entity Amount Bar; confirmed the PDF export uses its own independent query, unaffected)

## [1.3.5] — 2026-07-29
- Removed "Request access" entirely (nav CTA, About page section, homepage section) — the site has always been fully open, this was a stale lead-capture CTA left over from an earlier phase
- Reordered the top navbar: Home, About, Info, Search, Rate Trends, Market Snapshot

## [1.3.4] — 2026-07-21
- Added Plausible Analytics (privacy-friendly, no cookies) site-wide via base.html

## [1.3.3] — 2026-07-21
- Property page: added disclosure tooltips on the estimated annual tax figures (Investment Snapshot and the 2026 KPI card) explaining that the estimate applies one combined rate to a single taxable value and doesn't account for exemptions that vary by taxing entity — shown on every parcel, not just ones with a recorded exemption code, since that field is incomplete on preliminary-year data
- Added a caption on the 2025→2026 Total Tax change confirming the estimate uses unchanged 2025 rates (not a rate increase), while noting the two years' figures aren't computed identically

## [1.3.2] — 2026-07-21
- Property tax calendar: the "current" milestone is no longer mislabeled — a passed deadline now shows a plain checkmark, and the next upcoming milestone gets an accurate day countdown
- Redesigned "today" indicator (outside design review by Fable, after two in-house attempts): the connecting track is now a progress fill with a today tick at the true proportional position; the countdown moved into a normal-flow pill under the next milestone's own label, eliminating the text/label overlap bugs from earlier attempts by construction
- The "values still Preliminary" note is now a full-width row below the calendar instead of crowding a single milestone

## [1.3.1] — 2026-07-20
- Coverage Map: market county shapes are magnified in place (real silhouettes scaled around their true centroids to a minimum visible size via one computed formula) so all six markets are findable at national scale; disclosure microcopy added by the legend

## [1.3.0] — 2026-07-20
- Coverage Map redesigned (outside design review by Fable, amended by product decision): text-free national map with Live and Coming-soon counties colored directly — no marker glyphs — on a darkened backdrop that no longer blends into the page
- Market card row is now the map's legend: new leading Austin/Travis "Live" card, county-shape thumbnails, Live/Coming soon status pills, bidirectional hover linking (hovering a card makes its county glow on the map)
- County hover tooltips: every county names itself; market hover treated as one logical target
- Known cosmetic limitation: Louisiana parishes, Alaska boroughs, and Virginia independent cities display with a generic "County" suffix

## [1.2.0] — 2026-07-20
- Live typeahead search on all four search inputs, including the navbar bar for the first time (one shared script; three duplicate implementations removed)
- Full-address matching: pasted addresses with commas, city, state, and zip now resolve; city/zip act as ranking signals, never hard filters (handles TCAD's own missing/misspelled city tokens)
- Explicit "No results found" state shown right at the search bar
- Results page scrolls to results on load, including the no-results message
- Deterministic, prefix-biased candidate pool for broad queries (fixed missing ORDER BY before LIMIT)
- AJR% placeholder-account exclusion now consistent across both search paths
- KNOWN_LIMITATIONS: blank-situs_address parcels reachable only by account number

## [1.1.0] — 2026-07-19
- Terms of Service, Privacy Policy, and Disclaimer pages (/terms, /privacy, /disclaimer)
- Beta consent popup, shown once per browser, with explicit agreement tied to the real documents
- Footer links to all three legal pages plus a generic non-affiliation line

## [1.0.0] — 2026-07-17
- First public production deployment (Render, https://parcelytics.onrender.com)
- Everything built to date: Travis County data (~508K parcels, 35 years of rate history), Homeowner and Investor modes, confidence-labeling system, tax calendar, value-vs-taxable chart, Documents & Sources panel, county_benchmark and homestead-cap fix set, Taxable Value KPI cards, Sentry error monitoring, rate limiting, versioned deploy workflow
