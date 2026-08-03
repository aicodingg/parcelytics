# Parcelytics Data Lifecycle — Acquisition to Seal
**Fable · August 3, 2026 (v1.1) · The standing process for all county data, all years, all future counties · Owner: PM · Enforcement built by Cowork · Promotions and seals approved by Diego**

---

## 1. Principles (each one bought with a real incident)

1. **Data is managed in vintages, and a sealed vintage is immutable.** A *vintage* = one (county, tax year, source type) — e.g., "Travis · 2026 · certified appraisal." Vintages move forward through named states and never silently change once sealed. *(Incident: July's totals became unreproducible because the substrate changed under them.)*
2. **Real property only, enforced at the door.** Personal property never enters the serving database — the boundary lives at load time, not in query filters, so an entire contamination class becomes structurally impossible rather than repeatedly excluded. *(Incident: 42,563 L-class rows in `group_stats`.)*
3. **Every number that leaves the building traces to a script, a commit, and a vintage.** No figure is ever published from terminal math. *(Incident: $377.84B, derivation unrecoverable, posts deleted.)*
4. **Anomalies get explained before they get narrated.** A surprising delta blocks the pipeline until decomposed; the explanation is written into the record, not into a caption. *(Incident: the protest-season story written twice, for opposite signs.)*
5. **Promotion is atomic and staleness is loud.** Users see the old vintage or the new one, never a blend; every derived surface carries its vintage stamp and a failing freshness assertion, never a quiet lag. *(Incidents: the stale billing badge; hold-the-flip.)*
6. **The raw file is forever.** Every file as-received is archived, checksummed, and never edited — reprocessing is always possible; un-acquiring never happens. *(Incident: the reconstruction test was only possible because a snapshot happened to exist. "Happened to" is not a process.)*

## 2. The vintage state machine
State lives in the **Vintage Ledger** (append-only; Notion database + mirrored repo file). One row per vintage: id (`travis-2026-certified`), state, dates, file checksums, gate results, promotion approval, seal date, and links to every artifact. The Ledger is the single answer to "what data do we have and how far along is it" — if the Ledger doesn't say SEALED, the number does not exist for publication purposes.

## 3. Stage by stage — with the PM's checklist at each

### Stage 0 — ACQUIRE
The **Source Registry** (one page per county, created before first acquisition) records: each source (CAD certified export, CAD preliminary/supplement exports, tax office billing data, adopted rates), its URL or PIR procedure, expected release calendar, expected format and approximate size, and the contact who fulfilled prior requests.
**On every acquisition, the PM:**
1. Downloads/receives the file(s); computes SHA-256 checksums immediately.
2. Writes the raw files, untouched, to the **Raw Vault**: `vault/{county}/{year}/{source}/{date}/` — local plus the offsite backup location. Raw files are never edited, renamed contents, or "cleaned in place."
3. Opens a Vintage Ledger row: state ACQUIRED, checksums, retrieval date, source reference (URL / PIR number), file inventory.
4. Runs the acquisition sanity check (script, not eyeball): files parse, row-count magnitude vs. registry expectation (±20% flags for review), layout fingerprint matches the county profile's schema version — a changed layout stops here, not mid-load.

### Stage 1 — CLASSIFY (the scope boundary)
Every county has a **County Profile** written *before* its first load (this is the Dallas/Harris prerequisite work): field mappings, the complete class-code taxonomy observed in its files, and — the load-bearing part — the **Classification Map**: every class prefix assigned to exactly one bucket: `REAL_PROPERTY` (the allowlist), `PERSONAL_PROPERTY`, `EXEMPT/SYNTHETIC`, or `UNKNOWN`.
**Rules:**
- Only `REAL_PROPERTY` rows pass beyond staging. Personal property is not filtered later — it is never loaded into production. (The raw vault retains it; if a BPP product ever exists, it gets its own pipeline.)
- `UNKNOWN` is a blocker, not a bucket: any prefix in the file absent from the map halts the load until a human classifies it and commits the map update. New codes appearing in a new year's file are *expected* — this is where they get caught.
- The map is one committed file, shared by every loader and asserted by the harness: production must contain zero rows outside the allowlist, ever.
**PM checklist:** run the classification report (counts + summed value per prefix per bucket); confirm zero UNKNOWN; eyeball the PERSONAL_PROPERTY share against last year's (a sudden shift means the county changed coding practices); attach the report to the Ledger row.

### Stage 2 — STAGE & GATE
Load into staging tables (vintage-tagged), never directly into production. Then the gate battery — every gate's output attaches to the Ledger row:
- **G1 Structure:** row counts, types, null profiles vs. the coverage manifest's expectations per column per year.
- **G2 Identity & conservation:** key uniqueness; unit-model conservation (sum of loaded units equals file totals to the dollar); duplicate-account detection (the supplement-double-load signature).
- **G3 Scope:** re-assert real-property-only and zero UNKNOWN on the staged rows (defense in depth on Stage 1).
- **G4 Temporal integrity:** tax-year bounds; the load touches only its own vintage's year and county; **any write that would alter a SEALED vintage's rows hard-fails** (this is immutability, enforced, not promised).
- **G5 Continuity vs. the prior sealed vintage:** parcel-count delta, total-value delta, and class-mix shift each inside declared bands (e.g., ±10% year-over-year value; bands live in the county profile and tighten with experience). Out-of-band is not necessarily wrong — but it *stops the line* until the PM writes the explanation into the Ledger and (for large anomalies) Fable reviews it. Explanations are decompositions, not stories: "new construction added $X across N accounts" with the query attached, not "probably new construction." **First vintages (new county, or Travis's own founding): see 9.1 — G5 is never skipped, it runs in external-reference mode.**
- **G6 Reconciliation (comparative vintages):** certified-vs-preliminary (and any successor comparison) must decompose — matched-account Δ + additions − removals = total delta, residual under $1M — via the committed reconciliation script, results in the Ledger. This is the gate that makes the certification *post* possible; it runs here, weeks before marketing wants the number. **Scope: see 9.3 — conditional-but-eager.**

### Stage 3 — PROMOTE (atomic, approved)
Two-key: the PM requests promotion with the full gate record; **Diego approves** (this is deliberately a human moment — it's the last cheap place to say no).
Promotion is one transaction: staging swaps into production; **all summary tables refresh as part of the same promotion** (compute-at-write — group_stats, snapshot summaries, benchmark stats), every summary row stamped with the vintage id; confidence labels flip atomically by vintage type (certified→Verified, preliminary→Preliminary); the site's "Data as of" line re-reads the Ledger. While a replacement vintage is anywhere pre-promotion, the site serves the prior vintage plus the reconciliation banner — hold-the-flip is now a pipeline feature, not an emergency measure.

### Stage 4 — VERIFY (live)
The post-promotion harness, same session: fixture parcels render correctly across both modes; summary-freshness assertions (every summary's vintage stamp = latest promoted vintage); named-query duration budgets pass on the new data volume; and a **totals cross-check** — production aggregates recomputed independently match the staged gate outputs to the dollar. Failures roll back (the transaction makes that possible) and the vintage returns to STAGED with the failure logged.

### Stage 5 — SEAL (finalize)
After VERIFIED plus a settling window (48 hours of live operation without incident), the PM seals: the Ledger row goes SEALED; the vintage's production rows become write-protected (loader refusal + G4 + a standing harness assertion that every sealed vintage's row count and value checksums are unchanged since seal); and the **canonical figures** are written to the Published Metrics Log — total market/assessed/taxable value, row count, by-type breakdown, each tied to the computing script, its commit, and the vintage id. **These Metrics Log entries are the only county-level figures anyone — marketing, press, Diego in a text message — may cite externally.** Then the vintage goes dormant until next year, which is exactly the "don't touch it again" property requested: not a hope, a write-protection.

## 4. The one honest exception: supplements and corrections
Counties issue supplemental rolls and corrections after certification; a process that pretends otherwise will be broken by September. The rule: **a sealed vintage is never edited — it is superseded.** A supplement becomes its own small vintage (`travis-2026-certified-supp1`) through the same lifecycle (its G5 compares against the sealed base; its G6 decomposes what changed), and promotion layers it with full lineage. If a supplement changes any *published* figure beyond rounding, the Metrics Log gains a superseding entry and the site changelog gets a dated note — the correction discipline as routine hygiene rather than crisis response. Same path for any internal error found post-seal: formal reopen, documented reason, revision vintage, changelog. Silent edits do not exist as an option.

## 5. Publication rules (where today's pain becomes policy)
1. External county-level claims quote SEALED-vintage Metrics Log entries. Not STAGED, not PROMOTED, not VERIFIED — sealed.
2. Comparative claims (vs. preliminary, vs. last year) additionally require their G6 decomposition on record — the explanation ships with the number or the number doesn't ship.
3. **The temporal rule:** before publishing, the figure is checked against the *previously published* figure for the same quantity in the Metrics Log. Agreement → publish; disagreement → the changelog note publishes with or before it. Two Parcelytics surfaces never disagree in public without a dated explanation standing between them.
4. Marketing's calendar keys off vintage state, not dates: "the certification post" is unlocked by `travis-2026-certified → SEALED`, and the marketing brief for it links the Ledger row. Because sealing requires verification plus the 48-hour settling window, **the post ships no earlier than 48 hours after live verification, whatever day that lands on** — the window is not negotiable under news pressure; annual milestones do not stale in two days, and the settling window is exactly where this platform has historically caught its errors. During the window, the site's hold-the-flip banner is the public voice.
5. The pre-publish surface audit (site totals, footer, About coverage, benchmark modules, a fresh PDF) runs whenever a promotion changed any figure a prior post cited.

## 6. The annual rhythm and the new-county on-ramp
**Per county, per year** (Travis calendar; each county profile carries its own): spring — acquire preliminary → lifecycle → sealed preliminary serves with Preliminary labels; July — acquire certified → lifecycle (G6 against sealed preliminary) → atomic flip to Verified → seal; fall — adopted rates vintage; fall/winter — billing vintage(s); quarterly — supplement vintages as issued. Between events: nothing to do, by design.
**New county onboarding** (Dallas is the rehearsal): (1) Source Registry + County Profile + Classification Map written and reviewed *before* any file is loaded — Fable reviews the classification decisions once, deliberately; (2) partitioning/infrastructure prerequisites confirmed (per the query-timeout architecture: county partitions exist before county #2's data does); (3) a full trial run to STAGED+GATED on the new county with gates expected to fail informatively — tuning bands and mappings on staging, never production; (4) first real vintage through the full lifecycle; **county launch = its first vintage set reaching SEALED**, which is also what the coverage map's "Live" dot legally means from now on.

## 7. Roles
- **PM** owns the Ledger, executes every checklist, writes anomaly explanations, requests promotions, performs seals.
- **Cowork** builds and maintains the enforcement (loaders, gates, refresh functions, write-protection, harness assertions) and runs live measurements.
- **Fable** reviews out-of-band anomaly explanations, new-county classification maps, and any seal-reopen.
- **Diego** approves promotions and seal-reopens — the two moments where the system deliberately requires the owner's eyes.

## 8. What Cowork must build to make this real (the enforcement list)
1. Vintage Ledger schema (Notion DB + repo mirror) and the loader changes to require a Ledger row before any load.
2. Raw Vault conventions + checksum tooling + offsite copy in the nightly backup job.
3. The Classification Map as one committed module; loader-level allowlist enforcement; UNKNOWN-prefix hard stop; the harness assertion (zero non-allowlist rows in production).
4. Staging schema + the G1–G6 battery as committed, reusable scripts (G6 already exists; G4's sealed-write refusal and G5's banded continuity checks are new).
5. Atomic promotion transaction incl. summary refresh + vintage stamping (this is the same work as the query-timeout architecture's Tier 1 — one build, two problems).
6. Seal mechanics: write-protection, the standing immutability assertion, Metrics Log entry generation from committed scripts.
7. County Profile + Source Registry templates, seeded retroactively for Travis so the current data becomes vintage-managed (backfill: declare and seal the current promoted state as the founding vintages, with today's known caveats recorded honestly in their Ledger rows — see 9.2).

---
*The test of this document is Dallas: if the PM can take a county Parcelytics has never touched and drive it from a download link to a sealed, citable, immutable vintage set using nothing but the checklists above — with every gate refusing politely at the right moments — the process works. Anything the Dallas run reveals as missing gets added here, through a dated revision, because the process document follows its own rules.*

---

## 9. Rulings & Amendments — August 3, 2026 (v1.1, from PM review)

**9.1 First-vintage G5 (new counties, and the Travis founding).** G5 is never skipped — a county's first vintage is its *highest*-risk load, not its exempt one. First vintages run G5 in **external-reference mode**: triangulate against the issuing body's own published figures (every CAD publishes roll totals and account counts; Texas adds the Comptroller's Property Value Study), with the expected divergence *banded, not ignored* — our real-property subset should land inside a declared share of the full published roll, derived from the Classification Map's personal-property proportion and written into the County Profile before load. The first vintage's observed values then become the baseline that ordinary G5 diffs against in year two, with bands declared from observation and tightened with experience.

**9.2 The Travis backfill: honest founding, not reconstructed theater.** Checksum the surviving raw files into the Vault now (cheap, and overdue protection), but do **not** reconstruct gate records for history that never ran through gates. The founding Ledger rows are explicitly typed `FOUNDING (retroactive)`: files and checksums as of today, links to the incident record, and a plain statement that these vintages predate the lifecycle. A founding row that *looks* gated is worse than one that says it wasn't — full rigor forward, honest declaration backward. The unresolved ~1.7% divergence vs. TCAD's published total is recorded in the founding rows as a **named open item with an investigation task — it does not block founding sealing** (sealing means "immutable, with caveats recorded," not "perfect"), with two conditions: any external claim that specifically compares our totals to TCAD's published totals discloses the divergence until closed, and its eventual resolution arrives via the supersede path with a changelog entry, never an edit.

**9.3 G6 scope: conditional-but-eager, deliberately.** Full matched-account decomposition (G6-full) is mandatory wherever two vintages describe the same (county, year, quantity) — certified vs. preliminary, supplement vs. base, revision vs. original — run at promotion, not deferred. Vintages with no same-quantity predecessor (billing, rates, a county's first) run **G6-lite**: totals cross-checked against the source file's internal sums and the issuing body's published figures where available. The unconditional rule that survives regardless: **no comparative external claim without its G6-full on record**, computed before the claim if not already required at promotion. Revisit after Dallas — the committed script's cost drops with reuse, and if it becomes trivial, unconditional is the simpler rule.

**9.4 Build sequencing: three phases, three atomicity rules.** A staged build is safe *except* for three specific pieces that create false rigor if shipped partially: (a) **Classification Map enforcement must land across all loaders at once** with its harness assertion — an allowlist with a bypass is a guarantee that lies; (b) **seal-state and write-protection ship together** — an advisory SEALED flag is worse than none; (c) **promotion and summary refresh share one transaction** — or the stale-summary class returns. The identified risk in the Metrics-Log-first approach is real and has a clean mitigation: the Log carries a `vintage_state` field from day one, and pre-seal-era entries are labeled `PROVISIONAL` — the Log tells you how much to trust itself, which is this brand's move applied to its own bookkeeping.

**Phase 1 (now — prevents this week's class):** Raw Vault backfill + checksums; Classification Map enforcement (atomic unit a); Metrics Log with state labels; the G6-before-comparative-claims rule; the Vintage Ledger as *record* (observing before it gates — safe).
**Phase 2:** staging schema + G1–G5; atomic promotion + summary refresh (unit c, shared build with the query-timeout architecture).
**Phase 3:** seal mechanics + immutability assertion (unit b).
**The forcing function:** county launch = first sealed vintage set, so Dallas *requires* all three phases complete — expansion pressure completes the system instead of bypassing it.
