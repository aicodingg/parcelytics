# Parcelytics — Design & UI Overhaul

**Run:** overnight, unattended · 2026-06-23
**Branch:** `integration/all-tasks` (all feature work + this design work). `main` untouched.
**Goal:** move the look from soft / pale-blue / govtech-adjacent ("Netgiro") to confident,
high-contrast, restrained, precise ("Stripe/Webflow") — so a first-time visitor trusts an
investment-grade, verified-government-data product within five seconds.

Everything below was applied live to the working tree and committed in granular `design:`
commits. The app reflects every change now — nothing is left to "run" in the morning.

---

## 1. How to review

- **Live style guide / single source of truth:** `/styleguide` — renders every token and
  component (color, type scale, spacing, radius, elevation, buttons, confidence pills,
  tables, forms, risk blocks, projection treatment, entity bar). Start here.
- **Front door:** `/` (new landing page) and `/about`.
- **App surfaces:** `/parcel/0100030105`, `/snapshot`, `/rates`, `/parcels?...`, `/compare?ids=...`.

Each open decision below is its own commit, so any single choice can be reverted with
`git revert <hash>` without unwinding the rest.

---

## 2. Open-design-decision defaults taken (reversible)

| Decision | Default taken | Where to change |
|---|---|---|
| Light-only vs dark hero | **Light app + dark, confident front-door hero** | `.hero`, `.fd-trust-panel` in `style.css`; hero markup in `index.html` / `about.html` |
| Logo / wordmark | **Clean wordmark in accent** — `Parcel`**`ytics`** with a small accent "P" mark. No complex logo invented. The old photo `parcelytics-logo.png` is no longer used in the nav. | `.navbar-brand` in `base.html` + `.brand-mark` in `style.css` |
| Front-door scope | **Built a genuine new landing page** at `/` (the trust fix), preserving all existing search + typeahead + disambiguation behavior | `templates/index.html` |
| Accent color | **`#4263EB`** (confident indigo / blue-violet), used sparingly for CTAs + signal. Easy to swap — see `--accent` family. | `:root` in `style.css` |

All four are surfaced on `/styleguide` so they're easy to evaluate and swap.

---

## 3. Design tokens (one source of truth: `static/style.css` `:root`)

**Color — soft → confident.**
- Ink/dark: `--ink #0B1220`, `--ink-2 #10182A` — headlines, nav, dark hero, dark panels.
- Canvas: `--bg #F7F8FA`, `--surface #FFFFFF`, `--surface-2 #F4F5F8`, `--surface-3 #ECEEF3`.
- Borders: `--border #E5E7EE`, `--border-strong #CBD0DC`.
- Accent (single, reserved): `--accent #4263EB` / hover `#3550C8` / active `#2C44AB` / soft tint `#EEF1FE`.
- **Retired** the old pale `#94B0DA` accent and `#DCEDFF` dominant field — pale blue now survives
  only as the faint `--accent-soft` tint, never as the page's personality.
- Functional signal set kept distinct from brand and used **only for data meaning**
  (risk ↑ red `--up #D92D20`, relief ↓ green `--down #067647`).

**Confidence system colors** (the data-integrity signature) refined for crisp legibility:
Verified (green), Preliminary (blue), Partial (amber), Estimated (violet), Not Available (muted italic).

**Type — decisive scale.** `12 / 13 / 14 / 16 / 20 / 28 / 40 / 56` (`--fs-xs … --fs-3xl`),
Inter (now incl. 800 for hero display) + JetBrains Mono. `tabular-nums` preserved on every figure.
Small-caps tracked labels for column/section headers.

**Spacing — 8px base:** `4 / 8 / 12 / 16 / 24 / 32 / 48 / 64 / 96` (`--s-1 … --s-9`). Most of the
old "dashboard" feeling was inconsistent, cramped spacing; consistent rhythm fixes a lot.

**Radius:** `--r-xs 4 / --r-sm 6 / --r 10 / --r-lg 14 / --r-pill`.
**Elevation:** restrained 3-step shadow scale (`--shadow-1/2/3`).
**Motion:** subtle only (`--t-fast .12s`, `--t .2s`) — hover states, smooth reveals, chart
transitions. No bouncy/decorative animation. (The old pulsing "Delinquent" badge is now a small,
calm dot pulse rather than a whole-badge flash.)

---

## 4. What changed, by surface

**Design system + style guide** — rebuilt `style.css` around the tokens above (all existing class
names preserved so templates keep working); added `/styleguide` route + page.

**Nav + footer (`base.html`)** — dark sticky top bar; accent wordmark; active-state link styling;
one distinct primary CTA ("Request access"); proper mobile collapse (toggler + collapsible menu);
simplified sector dropdown styling. New credible dark footer with Product / Data sources / Coverage
columns, contact, and "not legal or tax advice".

**Front door — landing (`index.html`)** — real hero: eyebrow trust chip, one confident value-prop
headline with accent, supporting sentence, the live search (behavior unchanged), a credible product
*motif* built in HTML (mini property card with KPIs, confidence pills, entity bar — no stock art),
and a trust strip (508K+ parcels, rates since 1990, 2021–26 coverage, 2 government sources / zero
aggregators). Below: "what makes it different" (the 3 data-integrity principles), a dark provenance
panel naming TCAD + Tax Office and previewing the confidence system, audience cards
(investors / developers / homeowners), and a **Request access / feedback** CTA (`#request`,
mailto:parcelytics@gmail.com) — there was previously no way for a visitor to request access.

**About (`about.html`)** — restyled to the same front-door craft: hero, feature cards, a clean
data-source table, a dark "data-integrity standard" panel listing the 5 non-negotiables, audience
cards, and the matching `#request` section the nav CTA targets.

**Property detail (`property.html`)** — inherits the new tokens (KPI cards, tables, pills, section
nav all elevated). Specific brief items:
- **Tax-burden donut removed**; the ranked horizontal entity bar is now the single 2025-burden
  visual (restyled with pill bars + token colors). The donut's data is preserved as a clean
  "Rate, Amount & Share" detail table, and the "Not Available" billing state is preserved.
- Estimator card given a premium accent treatment and kept near the top (flagship position).
- Section nav (Overview / History / Projection / Position) evolved — sticks below the now-sticky
  global nav (offsets adjusted); horizontal-scrolls cleanly on narrow screens.
- All data-integrity labeling preserved & elevated: per-year Verified/Preliminary/Partial pills,
  the property-level billing-aware confidence badge (computation untouched), the "illustrative
  scenarios, not statistical confidence ranges" projection wording (untouched), `~`/dashed/muted
  estimate treatment, and `tabular-nums`.
- **Chart-in-hidden-pane safety:** no tabbed `display:none` panes were introduced; the section nav
  is anchor-scroll, so all canvases remain visible and size correctly. Verified live: value-trend
  and entity-rate charts render.

**Snapshot (`snapshot.html`)** — headline numbers converted to the shared KPI-card pattern (with
Verified/Preliminary pills); investor-takeaway, by-type table, annual-trends all on the new craft;
by-type table reflows to stacked labelled rows on mobile; chart bars use token red/green.

**Rates (`rates.html`)** — token styling on sidebar, chart, and summary table; "Reset to key
entities" button now uses the primary button style.

**Parcels (`parcel_list.html`)** — table craft + pin/compare tray styling; table reflows to stacked
labelled rows on mobile.

**Compare (`compare.html`)** — inherits the softened bordered-table craft; comparison matrix keeps
horizontal scroll on mobile (the legitimate exception for a metric×parcel matrix).

---

## 5. Data integrity — preserved and elevated (never sacrificed for aesthetics)

- Confidence pills are now a signature branded component (status-dot + pill shape), legible at a
  glance, and **previewed on the front door** — not hidden to reduce clutter.
- "Not Available" remains an explicit, styled, italic state — never blank, never zero.
- Estimates keep distinct treatment (dashed border, muted italic text, `~` prefix).
- Source attribution, "certified", as-of dates, and the billing-aware property confidence badge
  are intact; **no computation logic was changed.**
- `tabular-nums` retained on every financial figure.

---

## 6. Mobile

Responsive to standard breakpoints (Bootstrap lg/md/sm + custom queries): nav collapses to a
toggler; KPI/feature/audience grids reflow; hero stacks (product motif hidden < lg); footer grid
collapses; **wide data tables reflow into stacked, labelled rows on phones** via a `.table-stack`
helper (applied to the snapshot by-type and parcel-list tables) rather than horizontal-scrolling.
A `body { overflow-x: hidden }` guard prevents the full-bleed hero's `100vw` from creating a
horizontal scrollbar.

> **Tooling caveat for review:** the browser used for live QA would not emulate a narrow viewport
> for screenshots (window-resize didn't change the render width), so mobile layouts were implemented
> and reviewed via the CSS/breakpoint logic rather than live mobile screenshots. Worth a quick pass
> on a real phone / devtools device mode in the morning.

---

## 7. ⚠️ Pre-existing issue found during QA (NOT design scope, NOT introduced here)

The **post-acquisition estimator's client-side JavaScript is missing.** The card markup
(`#acqPrice`, `#acqResults`, `#acqTbody`, buyer-type toggle) and the backend route
`/api/estimate_acq/<geo_id>` both exist, but no JS implements `runEstimator()` — the Estimate
button's `onclick="runEstimator()"` throws `ReferenceError: runEstimator is not defined`.

Confirmed this **predates all design work** (present in commit `09e08f3`, before the first `design:`
commit) — `git show main:templates/property.html` shows the estimator markup isn't even on `main`,
so it came in with the feature branches without its client logic. My design diff touched zero
estimator-logic lines.

I deliberately did **not** build it: that's feature work (explicitly out of design scope) and an
estimator that renders computed tax figures carries data-integrity stakes that shouldn't be coded
unattended. The card is styled and prominently placed; it needs its `fetchAndRender`/`runEstimator`
client logic wired to the existing API. **Recommend the owner implement/wire this.**

---

## 8. Before / after (qualitative — live before/after were viewed during the run)

- **Before:** pale-blue dominant, evenly-weighted, low-contrast; `/` was a bare centered search box;
  no real landing page; donut + bar duplicated the 2025 burden; soft photo logo; thin centered
  about page; no request-access path.
- **After:** confident near-black-navy + single indigo accent on a crisp neutral canvas; a genuine
  Stripe/Webflow-grade landing page with hero, product motif, trust/provenance and CTA; one
  analytical entity bar (donut removed); investment-grade table craft; a clean accent wordmark;
  a credible dark footer; request-access throughout.

---

## 9. Commit trail (granular, revertible — `git log main..integration/all-tasks`)

```
design: establish confident design-token system (ink navy, indigo accent, type/spacing/elevation scales)
design: add /styleguide component reference page (renders all tokens + components)
design: sticky dark nav with wordmark + CTA, mobile collapse, credible footer
design: build real landing page — dark hero, product motif, trust/provenance, audiences, request-access CTA
design: restyle about page to front-door craft (hero, feature cards, provenance, integrity standard)
design: property page — remove tax-burden donut, style ranked entity bar, elevate tables
design: snapshot KPI cards + mobile table reflow, parcel-list reflow, rates button polish
design: prevent full-bleed hero 100vw horizontal-scroll artifact (overflow-x guard)
```

`main` is untouched. Each commit is small and clearly scoped for easy individual revert.

*(Minor infra note: the working-tree git mount blocked file `unlink`, leaving occasional stale
`.git/*.lock` files; these were cleared by rename before each commit. Harmless; mentioned only so a
stray `*.lock.stale_*` file in `.git/` isn't a surprise.)*
