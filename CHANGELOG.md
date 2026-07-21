# Changelog

All notable changes to Parcelytics are tracked here, using [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

- **MAJOR** — structural changes (accounts, a new county live, a major redesign)
- **MINOR** — new features
- **PATCH** — bug fixes and small refinements

Version numbers are tied to actual production deploys, not every commit.

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
