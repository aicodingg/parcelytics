# MULTI-COUNTY ONBOARDING STANDARDS

**Author:** Fable (per MULTI-COUNTY-ARCH-1)
**Date:** August 20, 2026
**Status:** Standing requirements — every new county, every new county-scoped table, every new data type.
**Companion documents:** `SPEC_UNIT_MODEL_AND_INGEST_GATE.md`, `SPEC_COUNTY_PARTITIONING.md`, `SPEC_TAX_BILLING_REKEY.md`, `DATA_LIFECYCLE.md`, `BUILD_WORKFLOW.md`

---

## 0. The principle behind every standard in this document

Every major data-loss incident this platform has had — the $26.07B unit-collision
loss, the $170.06M billing-entity loss, the three independently rediscovered
missing-`county_code` writers — shares one root cause: **an invariant that lived
in convention (something every writer had to independently remember) instead of
in structure (something the schema, a module boundary, or a failing test
enforces).** Conventions drift; structure doesn't. Every standard below converts
a convention this codebase has already watched fail into a structural mechanism,
and names the incident that proves the cost of not having it.

A standard here is not guidance. Each has a **rule**, an **enforcement
mechanism**, and a **provenance** line. If the mechanism isn't built, the
standard doesn't exist yet — "we agreed to do it" is the failure mode this
document exists to end.

---

## MC-1 — Identity Scope Review (every new table, before it ships)

**Provenance:** Pattern 1. `geo_id` assumed globally unique; it is unique only
within one CAD. Cost: $170M+ measured loss, two retrofit migrations.

**Rule.** No `CREATE TABLE` ships without a completed Identity Scope Checklist
in its spec or PR description:

1. **For every column appearing in a primary key, unique constraint, or
   `ON CONFLICT` target: who issues this identifier?** If the issuer is a
   county-level authority (CAD, tax office, clerk), the identifier is
   **county-scoped by default**. The burden of proof is inverted: an identifier
   is treated as county-scoped *until direct evidence shows it is global* —
   never the reverse. This single question, asked of the original `parcel`
   table, would have caught the entire collision class before row one.
2. **`county_code` leads every PK and unique constraint** on tables holding
   county-issued identifiers, and appears in every `ON CONFLICT` target.
3. **Natural keys are text, sized for the multi-county reality.** Travis tax
   accounts are 14 digits; Dallas's are 17 characters. `VARCHAR(20)+`, never
   numeric types, never sized to the first county observed.
4. **Born partitioned.** Any new county-scoped table expected to exceed ~1M
   rows at second-county scale is created natively partitioned by
   `county_code` on day one. Partitioning an empty table is free; partitioning
   a loaded one is a migration. (Established in the TAX-BILLING-REKEY ruling;
   generalized here.)
5. **Registered.** The table is added to the County-Scoped Table Registry
   (MC-2) in the same PR that creates it.

**Enforcement.** The checklist is a mandatory section of every schema spec
(the same way M-step plans are mandatory in migration specs). Fable review of
any new-table spec verifies it. A spec without it is returned unreviewed.

---

## MC-2 — The County-Scoping Audit (standing, automated, comprehensive)

**Provenance:** Pattern 2. The identical missing-`county_code` writer bug was
discovered three separate times by accident (DALLAS-GATE-4: five `tax_billing`
writers; PIR-XLSX-HOTFIX-1: `pir_xlsx_common.py`, missed because a prior audit
was scoped to a file list; `load_delinquent()`: found live, mid-backfill).
Incidental discovery is not an audit.

**Rule.** A permanent regression check, `verify_county_scoping.py`, in the
pattern of `verify_index_coverage.py` and the canonical-filter grep test:

1. **Source of truth:** a `COUNTY_SCOPED_TABLES` registry (one module,
   imported — never retyped) listing every county-scoped table and, per table,
   its **closed set of allowed writers** (integrating the single-writer-per-
   table rule from the re-key ruling).
2. **Scan scope: the entire repository.** Loaders, shared helper modules,
   `app.py` routes, scripts — everything. The PIR-XLSX miss was a scope
   failure, not a detection failure; a scoped audit is a partial audit, and a
   partial audit produces exactly the incidental-discovery pattern this rule
   ends.
3. **Checks, per writer found:** (a) the writer is in the table's allowed set —
   an unlisted writer is a failure even if correctly scoped; (b) `county_code`
   appears in the INSERT column list; (c) `county_code` appears in the
   `ON CONFLICT` / key target; (d) UPDATE/DELETE statements carry a
   county-scoped predicate or a documented exemption.
4. **Cadence:** runs in CI on every commit, and as a gate step inside
   `run_all.py` before `compute_metrics`. Not quarterly. Not "when we touch
   loaders." Every commit.
5. **Defense in depth at the schema layer:** `county_code NOT NULL` on every
   registry table, so a writer that evades the static audit fails loudly at
   write time instead of silently mis-keying. Structure backs up the scanner.

**Acceptance test for the audit itself** (per the tested-alarm principle,
`SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §4.3): three fixtures reproducing the
three real incidents — a `tax_billing`-style writer missing the conflict-target
scope, a shared-helper writer outside any prior audit list, and a
`load_delinquent`-style writer missing the column — and the audit must fail all
three. A safeguard that has never fired is a hope, not a safeguard.

---

## MC-3 — Unit Grain + Canonical Rollup + Conservation Gate: the required shape of every new data type

**Provenance:** Pattern 3. Two migrations in one season independently converged
on the identical architecture after the alternative (per-loader aggregation)
caused measured loss twice from the same root cause.

**Rule.** Any new data type — including anything Dallas or Harris provides that
Travis doesn't — ships as one indivisible unit or does not ship:

1. **Source grain, native key.** Data is stored at the source's true grain
   under the source's own identifier (county-scoped per MC-1). No loader
   aggregates on the way in.
2. **One canonical rollup module** computes every derived/account-level table.
   Loaders never write derived-table value columns. Enforced by the same
   grep-test family as `parcel_rollup.py` / `tax_billing_rollup.py`, with the
   allowed-writer set registered in MC-2.
3. **A conservation gate with tested alarms** ships in the same change: exact
   internal reconciliation (identity counts and value sums, source == unit
   layer == rollup), a skip ledger where every source record is accounted for
   by name, an `ingest_audit` row, exit-1 on failure, and
   deliberate-corruption fixtures proving each check fires. **No gate, no
   load** — this is Rule 4's family, extended from "every ingestion" to "every
   new data type, before its first ingestion."
4. **Pre-committed expectations.** Before any live run, the spec states the
   expected counts and sums the gate must re-derive. Surprising numbers cannot
   be narrated into acceptability after the fact.

This is no longer a pattern each data type rediscovers is necessary. It is the
default, and departing from it requires a written Fable-reviewed justification.

---

## MC-4 — The Canary Slice (real data, small scale, before every live run)

**Provenance:** Pattern 4. TAX-BILLING-REKEY-4: a real bug that 32 rigorous
fixture tests could not catch, because it only manifests against
production-scale real files. Fixtures are necessary and structurally
insufficient — they test the logic you anticipated; canaries test the data you
didn't.

**Rule.** For every (county × source) pair, a **canary slice** is maintained
alongside the archived source files, and every new or modified loader passes a
canary run before any live/full run:

1. **Slice construction — real lines, never synthetic:** head + middle + tail
   line ranges of the actual file (boundary conditions live at edges), plus a
   stratified sample covering **every record type / property type / layout
   variant the full file contains**. Target size ~50–100MB — large enough to
   hit real shapes, small enough to run in minutes.
2. **Representativeness is verified, not assumed:** the gate's G1 scan is a
   pure streaming file read with no DB — so **G1 always runs against the FULL
   real file** (cheap), and the slice is valid only if its record-type
   inventory matches the full file's G1 inventory. A slice missing a record
   type the full file contains is rebuilt before use. This split is the key
   economy: full-file scanning catches shape anomalies at zero DB cost; slice
   loading catches loader↔DB interaction at small cost.
3. **Canary run = the real pipeline in miniature:** loader + rollup + gate
   against the slice in a staging schema, with pre-committed expected outputs
   derived from the slice's own G1 scan. A canary without pass criteria stated
   in advance is a demo, not a test.
4. **Refresh per vintage:** slices are regenerated from each year's real
   export (a one-line `sed`/`head` operation), so the canary ages with the
   source format instead of fossilizing.
5. **Sequencing:** fixture tests (logic) → full-file G1 scan (shape) → canary
   run (integration) → live run (Diego, per the human/live workflow). Each
   step is cheaper than discovering its failure class one step later.

---

## MC-5 — Classification as an Evidenced Diff against a Canonical Root

**Provenance:** Pattern 5. DCAD's `SPTB CLASS CODE` follows the same statewide
Comptroller classification Travis's `state_cd1` uses — confirmed by direct
distribution comparison, not assumed.

**Rule.** Classification work for every new county is structured as a diff,
never a from-scratch taxonomy:

1. **One canonical root:** a single statewide Comptroller-scheme taxonomy table
   is the base artifact. Each county gets an **overlay** (its diff against the
   root), not a chain of county-to-county diffs — chained diffs compound
   drift; overlays against one root don't.
2. **Every divergence carries direct evidence:** a code-distribution
   comparison, county documentation citation, or measured sample — recorded in
   the county's Classification Map before Fable review. Review then covers the
   *diff*, which is small, instead of the whole map, which isn't.
3. **Unknown codes are conserved, not dropped:** any code in a county's export
   that maps to neither root nor overlay lands in an explicit `unmapped`
   bucket that the conservation gate counts and reports. Silent
   misclassification is the same disease as silent row loss, in a different
   organ.
4. **Standing requirement unchanged:** Source Registry + County Profile +
   Classification Map remain Fable-reviewed before any new county data load
   (per `DATA_LIFECYCLE.md`); this standard defines the *form* those documents
   take.

---

## MC-6 — Coordination Conventions for Concurrent County Workstreams

**Provenance:** Pattern 6. The duplicate-relay incident (fixed with the
`-rev`/`supersedes` header convention, adopted) — plus the empty-attachment
history — are previews of coordination load at two-counties-concurrent scale.

**Rules** (additions beyond the adopted header convention):

1. **County-prefixed brief IDs:** `PX-DAL-YYYYMMDD-nn`, `PX-HAR-...` for
   county-specific work; unprefixed IDs remain platform-wide. A brief's scope
   is readable from its ID at relay time.
2. **Explicit dependency headers:** every brief declares `BLOCKED-BY:` /
   `BLOCKS:` lines (or `none`). The Dallas hard gate, the re-key sequencing,
   and the tier checkpoint are all dependency facts that currently live in
   memory; at two concurrent counties, memory is the wrong place.
3. **One migration in flight per table family.** Two concurrent migrations
   touching the same tables is how "one migration, not three" fails silently.
   The Task Log marks the active migration per family; a second one queues.
4. **A single county-state surface:** one Notion table — county × stage
   (Source Registry → Classification Map → canary → gate → load → seal →
   marketing-eligible) — as the coordination artifact all sessions read.
   Onboarding state lives in exactly one place, like every other invariant in
   this document.
5. **Marketing freeze until seal:** no public figure references a county's
   data until that county's gate has passed and its figures are sealed per
   Rule 3 / the Published Metrics Log. The existing per-post discipline,
   promoted to a per-county gate — this is also the enforcement hook for the
   planned "all counties in one place" copy audit: copy generalizes only as
   counties actually cross this line.

---

## MC-7 — Risks the six patterns don't yet cover (named now, per single-pass)

1. **Cross-county semantic drift.** The same field name will not mean the same
   thing in two CADs — supplement cycles, exemption-code vocabularies, value
   timing, and "certified" semantics all vary by county. **Rule:** each County
   Profile documents field semantics against Travis's as a baseline, and
   **no cross-county comparison ships in product or marketing until a semantic
   parity check for the compared fields is on record.** Comparing a Travis
   "market value" to a Dallas "market value" that means something subtly
   different is a wrong-answer bug with a press release.
2. **Vintage skew.** Counties certify on different dates; any cross-county
   aggregate mixes vintages by construction. **Rule:** every cross-county
   figure carries per-county vintage labels (the confidence-label system,
   extended county-wise), and the Published Metrics Log records the vintage
   per county for any multi-county figure. No blended number without a
   blended-vintage disclosure.
3. **Per-county operational calendar.** Each county adds its own annual
   refresh cycle (preliminary → certified → rates → billing → delinquency) to
   the standing annual pattern. **Rule:** the county-state surface (MC-6.4)
   includes each county's cycle dates, so refresh work is scheduled, not
   remembered — three counties' cycles overlapping is an ops problem exactly
   once, when it's undesigned.
4. **Source licensing and suppression obligations.** Each CAD/tax office has
   its own data terms, and §25.025 address-suppression obligations apply per
   county source. **Rule:** the Source Registry records license/terms and the
   suppression-refresh obligation per source *before* first load — a legal
   review gate parallel to the technical one.
5. **Capacity checkpoint per county** (established in the Harris-scale ruling,
   recorded here as standing): before each county loads, project per-table
   rows and `pg_total_relation_size` at that county's scale against current
   hosting; pre-commit the threshold that forces the tier decision. The
   trigger is per-table absolute scale at next load, evaluated *before* the
   load.
6. **Observability by county.** Sentry events, gate results, and `ingest_audit`
   rows are tagged with `county_code`, so a Dallas ingestion incident is
   attributable at triage speed instead of after a diff. One line of tagging
   now; one saved incident-hour every time after.
7. **Institutional link fields, per county (PX-20260827-01).** A live Dallas
   spot-check found the property page's "Helpful Links" block hardcoded to
   Travis/TCAD's institutional URLs (protest portal, homestead-exemption
   portal, CAD contact, per-parcel deep link, tax-payment) for every county —
   a general-purpose page silently assuming Travis, the same pattern MC-1/MC-2
   exist to catch for data writes, just for user-facing links instead. **Rule:**
   every field this component needs (`cad_property_search_url`,
   `cad_parcel_detail_url_template`, `cad_homestead_exemption_url`(`_template`),
   `cad_protest_url`(`_template`), `cad_contact_url`) is added to `COUNTY_PROFILES`
   (app.py) for a county before that county reaches "data loaded" — populated
   with a real, independently-verified URL, or explicitly `None` where no real
   URL is confirmable yet (a `None` renders the link absent or a
   "not yet available" state; it must never fall back to another county's
   value). **Enforcement:** `verify_template_county_scoping.py`'s Class (C)
   check — a denylist of every real CAD abbreviation/name/domain already
   registered in `COUNTY_PROFILES`, checked against every `<a>` tag in every
   template — fails the moment a page hardcodes a literal CAD name or URL
   instead of sourcing it from `county_profile`/`county_cad_link()`. Because
   the denylist is derived from `COUNTY_PROFILES` itself (not hand-typed),
   this fires automatically the day a future county's real name/domain lands
   in the registry, with no edit to the scanner required — the county-level
   punch line MC-2 established for data writes, generalized to institutional
   links.

---

## Appendix A — The New County Checklist (the operational summary)

Per new county, in order; each line names its standard:

1. Source Registry entry incl. license/terms + §25.025 obligations (MC-7.4) — **Fable-reviewed**
2. County Profile incl. field-semantics baseline vs Travis (MC-7.1) — **Fable-reviewed**
2b. `COUNTY_PROFILES` (app.py) institutional-link fields populated or explicitly `None` (MC-7.7) — **`verify_template_county_scoping.py` Class (C) green**
3. Classification Map as evidenced overlay against the canonical root (MC-5) — **Fable-reviewed**
4. Identity Scope Checklist for any new tables; text keys; born partitioned (MC-1)
5. New data types ship as unit-grain + rollup + gate + tested alarms, or not at all (MC-3)
6. `verify_county_scoping.py` green with the county's writers registered (MC-2)
7. Capacity projection + pre-committed tier threshold (MC-7.5)
8. Canary slices built per source; representativeness verified vs full-file G1 (MC-4)
9. Full-file G1 scans green; canary runs green against pre-committed expectations (MC-4)
10. Live load (Diego, per human/live workflow) with per-county Sentry/audit tagging (MC-7.6)
11. Conservation gates green; figures sealed; Published Metrics Log updated with vintage (MC-6.5, MC-7.2)
12. County marked marketing-eligible on the county-state surface; copy generalization proceeds only from here (MC-6.4, MC-6.5)

---

*Amendment protocol: this document changes the way the specs it sits beside
change — by reviewed revision with a dated changelog entry, never by silent
edit. Standards that failed in practice are amended with the incident named,
the same way the §9.2 revisit trigger was corrected on the record.*

**Changelog**

- **2026-08-27 (PX-20260827-01):** Added MC-7.7 (institutional link fields per
  county) and Appendix A line 2b, gated on `verify_template_county_scoping.py`'s
  new Class (C) check. Provenance: a live Dallas spot-check found the property
  page's Helpful Links block hardcoded to Travis/TCAD for every county — the
  ~70-site copy pass (PX-20260826-01) didn't cover this component. Closed by
  extending `COUNTY_PROFILES` with real, independently-verified per-CAD URL
  fields (Dallas's confirmed via live web research: `dallascad.org`'s
  `SearchOwner.aspx`/`AcctDetailRes.aspx` and `hstead.dallascad.org`'s
  per-account homestead application — genuinely new URL shapes, not guessed
  from Travis's ProdigyCAD conventions) and rewiring the template to render
  entirely from `county_profile`/`county_cad_link()`, with a denylist scanner
  (derived from `COUNTY_PROFILES` itself, so it covers future counties
  automatically) enforcing it never regresses.
