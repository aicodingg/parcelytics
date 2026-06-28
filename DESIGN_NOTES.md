# Parcelytics — Design & UI Overhaul

---

## SESSION 3 (attended, 2026-06-23) — estimator polish + branding

**1. Lightened nav + original-color logos.** Nav is now a white, Stripe-style bar
(`--surface`, hairline border + `shadow-1`); links are crisp dark text, active link +
"Request access" CTA carry the accent. Both uploaded logos now show in their **original
colors on transparency** — the map mark sits directly on the bar (no white chip), the
two-tone wordmark as supplied (no inversion). Dropdown + mobile collapse panel re-tuned to
the light bar; dark hamburger icon. Retired the chip/inverted-wordmark and placeholder
accent-"P". (`static/parcelytics-logo.png` still unreferenced; mount blocks deletion.)

**2. Value-derivation card (property page, left column).** "How This Value Is Derived":
Market value → (Land + Improvements composition) → − Value-limitation adjustment (homestead
cap) → Net appraised (assessed) → − Exemptions (codes) → Taxable. Derived from the
authoritative stored 2025-certified values (`market − assessed` for the cap line, since the
`hs_cap_loss` column is empty on 2025 certified rows; `assessed − taxable` for exemptions).
Verified pill, plain-language labels.
> **DATA GAP flagged for the data session:** there is no special-use / agricultural
> *productivity* field in `parcel_tax_year` — that derivation line is omitted and noted on
> the card. `land_value`/`imprv_value` exist (2025 certified) and are shown.

**3. Estimator — multi-year, HS explainer, rate handling.**
- *Multi-year (3a):* backend projects Years 1–5 per buyer type. Owner-occupant: Year 1 gap
  (no exemption/cap), Year 2+ applies the $140k school HS exemption and the 10%/yr
  assessed-cap; investor: no exemption, 20%/yr circuit-breaker cap through TY2026 then
  uncapped. Market-growth assumption = the parcel's own certified CAGR, clamped 0–8% (shown).
  UI renders a projection table (Years 1/3/5 highlighted), unmistakably labeled a projection.
- *HS explainer (3b):* compact accent-tinted card (owner-occupant) — what the exemption is,
  $140k school exemption, 10% appraisal cap, owner-occupancy + file by Apr 30 of the
  following year, and the Year-1 gap.
- *Rate handling (3c):* the estimate now **states the tax year (TY2026)** and **labels the
  rate vintage**. A "2025 Certified" baseline (default) and a "Projected trend" scenario are
  offered as a labeled toggle. Projected rates are computed **per entity**, recency-weighted
  from `county_tax_rate` history, and **compression-aware**: declines pass through, any
  projected rise is clamped to ≤ +2% (we do not assume rates rise). Projected rate is shown
  with a "proj" marker + assumptions note; labeled a projection, not a certified rate.
- *Base case re-verified after the rate changes:* `0204063005` investor (certified)
  **$15,622 / Δ +$5,619.46**; `0100030105` buy-at-market (certified) **$88,655 / Δ −$0.07**. ✓

> **DATA GAP (per the brief's note):** estimator accuracy depends on the per-entity taxing-unit
> rate table being complete; a separate data session is reconciling units. These numbers will
> shift (upward) once missing units land — **re-verify the hand-calc base cases then.**

*(Earlier sessions' notes below.)*

---

## MORNING WORK (attended, 2026-06-23) — estimator wired up + logo swap

**A. Post-acquisition estimator now works (was a dead button).**
- Backend: the route imported `tax_logic.texas`, but the corrected logic was sitting unwired in
  `task_staging/task1/`. Copied it into the real module path `tax_logic/texas.py` (canonical logic
  no longer lives in staging). Also fixed a separate pre-existing crash: the route used `re.fullmatch`
  but `app.py` never imported `re` — added `import re`. Corrected constants now in effect: school-district
  HS exemption **$140,000**, TY2026 circuit-breaker threshold **$5,320,000**, tax = Σ(per-entity taxable ×
  entity rate), base = max(market, purchase price), owner-occupant Year-1 gap year + Year-2+ school HS.
- Client: implemented `runEstimator()` — reads price + buyer toggle, calls
  `GET /api/estimate_acq/<geo_id>?price&buyer`, renders summary chips, per-entity breakdown, delta vs
  seller's bill, and gap-year / circuit-breaker notes. Rendered as an explicit **estimate** (`~` prefix,
  muted italic figures, "Estimate · Not a prediction" badge, disclaimer). Loading, API-error, and
  invalid-input states handled — no more `ReferenceError`.
- **Verification (HTTP, against the Round 2 hand-calc):**
  - `0204063005` investor @ $763,384 → **$15,622**, Δ **+$5,619.46** ✓
  - `0204063005` owner-occupant Yr 2+ → **$14,327**, Δ **+$4,324**; gap-year **$15,622** ✓ (±$1 rounding)
  - `0100030105` buy-at-market $4,332,066 → **$88,655**, Δ **−$0.07** ✓ (per-entity summation invariant holds)
  - Verified end-to-end in the browser for both buyer types + invalid-input guard; console clean.

**B. Header now uses the uploaded logo files.**
- `Logo design.jpg` = the map/trend **symbol mark**; `Logo Name.jpg` = the **"Parcelytics" wordmark**.
- Converted each to a trimmed, transparent PNG (corner flood-fill at ~12–14% fuzz so interior whites
  and anti-aliased edges survive; exported ~2× for retina): `parcelytics-mark.png`,
  `parcelytics-wordmark.png` (two-tone original, light-bg asset), `parcelytics-wordmark-white.png`.
- Contrast: both files are dark-on-white, so on the dark navy nav the wordmark wouldn't read and the
  mark's navy outline would vanish. Resolution: the colourful **mark sits on a small white chip** (full
  fidelity) and the nav uses the **white-inverted wordmark**. The two-tone wordmark is kept as the
  light-background asset. Retired the placeholder accent-"P" mark + text wordmark from the design pass.
  (The old `parcelytics-logo.png` was already unreferenced; it remains in `static/` only because the
  working-tree mount blocks file deletion — safe to delete on your side.)

*(Original overhaul notes below.)*

---

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
