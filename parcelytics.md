# parcelytics.md — Master Operating Manual for AI/Agents

**Read this file first.** Every AI or agent (Cowork, Fable, or any future assistant) working anywhere in this repository reads this document before touching code, data, or documentation. It is the single entry point; where it points to a more detailed source document, that document is authoritative on its own topic, but this file is authoritative on priority, boundaries, and how the pieces fit together.

**Source note (disclosed per this brief's own instruction):** `PARCELYTICS_CONSOLIDATED_AUDIT_2026-09-02.md`, named as a source for this document, was not found in `code/` or in the parent `Parcelytics/` folder as of this writing (2026-09-02). This document was built from the repository's own docs and code instead: `BUILD_WORKFLOW.md`, `THE_FABLE_METHOD.md`, `MULTI_COUNTY_ONBOARDING_STANDARDS.md`, `DATA_LIFECYCLE.md`, `KNOWN_LIMITATIONS.md`, `CHANGELOG.md`, `VERSION`, `schema.sql`, `app.py`, `config.py`, `loaders/ingest_gate.py`, `verify_county_scoping.py`, `verify_index_coverage.py`, and related files, read directly. Every factual claim below is either confirmed against the repo or explicitly marked as a Diego/PM ruling, a target/planned state, or an open question — never invented.

**Evidence-tag convention.** Every metric or state-of-the-world claim below carries a bracketed tag saying how it's known: `[file:line]` — this agent read the cited source directly; `[measured DATE, ...]` — a live measurement, attributed to whoever ran it (this agent has no live database connection and has not run any live measurement itself); `[dashboard DATE]` — a Render dashboard read, attributed to whoever performed it (this agent has no Render access); `[PM-supplied]` — stated by the PM/Diego and not independently re-verified by this agent. Where an agent's own prior report in this thread turns out to have overstated what it checked, the correction is noted inline rather than silently fixed.

## Operating principle

**Optimize for correctness, reproducibility, maintainability, and automation — never merely making the immediate task work.** A fix that makes today's symptom disappear without addressing why it happened, a number that can't be re-derived from a script and a commit, a rule that lives in one person's memory instead of in structure — all of these fail this principle even when they "work." Every section below is an application of it to one part of the system.

---

## 1. What Parcelytics is, and who it serves

Parcelytics is a multi-county Texas property-tax intelligence platform. It ingests county appraisal-district (CAD) and tax-office data, computes derived metrics (rollups, benchmarks, historical trends), and serves it through a public Flask website.

Primary audience: commercial real estate (CRE) investors, acquisitions analysts, developers, brokers, and consultants — users making decisions that turn on property-tax exposure, valuation trends, and comparables. Homeowners are a secondary audience.

All public-facing copy (site text, marketing posts, published figures) speaks in an anonymous team voice — never a named individual's voice, and never a first-person "I."

## 2. Architecture & stack

State file paths, not aspirations. What's actually running, confirmed by direct inspection:

- **Backend:** Flask, server-rendered with Jinja2 templates. Database access is raw SQL via `psycopg2` — there is no ORM.
- **Single application file:** `app.py`, confirmed at roughly 8,900+ lines, with 35 `@app.route` GET routes (plus legacy-redirect routes) defined directly in it [PX-20260902-01 audit, direct file inspection]. There are no Flask blueprints — all routes live in one module.
- **Frontend:** server-rendered HTML/Jinja2 templates plus D3.js for the interactive maps and charts (coverage map, benchmark visualizations).
- **Process model:** production Start Command is `gunicorn app:app --workers 3` [dashboard 2026-09-02 — Render → Settings → Deploy, read by Diego; screenshot on record with PM]. **`app.py`'s own in-code comments (~line 1566, ~line 6144, ~line 8274) assert a single-worker default and are stale** — they predate this dashboard read and should not be trusted for worker count going forward [app.py:1566, 6144, 8274 — superseded, stale]. Whether the `--timeout` flag is absent (as those same comments claim) has not been separately re-confirmed against the dashboard and should be checked directly, not assumed from the same stale comment block.
  **Correction to a claim in this agent's own prior report:** an earlier report in this thread stated the worker count had been "confirmed against Render's dashboard directly." That was inaccurate — this agent has no Render access. What was actually inspected was `app.py`'s own source comments, which themselves *assert* a dashboard check was done by a prior engineer, without this agent independently verifying that assertion. The worker count above (3) is corrected per Diego's own dashboard read, not per anything this agent reached directly.
- **Deployment:** Render, **Manual Deploy only** (no auto-deploy on push) — deploys happen at a PM-named commit id, decided outside of Cowork's own commit/push actions. Production is a **single environment** — there is no staging deployment target on Render (staging, where it exists at all, means a Postgres schema, not a second app deployment).
- **Observability:** Sentry (error tracking) and Plausible (privacy-respecting analytics) are wired into the app.

## 3. Database architecture & constraints

- **Engine:** PostgreSQL 15, hosted on Render, on a plan documented [KNOWN_LIMITATIONS.md] at roughly **1 GB RAM** for a database whose live tables total on the order of **13 GB**, giving a documented heap cache hit rate around **22%** [KNOWN_LIMITATIONS.md] — well below the >99% a healthy OLTP workload wants. This agent has not independently re-run this measurement against a live connection this session; it is repeated here as the repo's own on-file figure, not a fresh measurement. This is the platform's central, load-bearing resource constraint: most performance work exists because of this gap, not despite it.
- **Statement timeout:** every app-opened connection gets an 8000ms (8s) `statement_timeout`, set once at `get_db()` connect time in `app.py`. There is no connection pooling — `query()` opens and closes its own connection per call.
- **Identity model:** county-issued identifiers (`geo_id`, tax-account numbers, etc.) are **county-scoped, never globally unique**, per `MULTI_COUNTY_ONBOARDING_STANDARDS.md`'s MC-1. County-scoped tables carry `county_code` leading every primary key and every `ON CONFLICT` target. This was learned the hard way — MC-1's own provenance line cites a $170M+ measured loss from `geo_id` once being assumed globally unique.
- **Two billing models coexist:**
  - The legacy `tax_billing_entity`-family tables are the **live read path** — what `app.py`'s routes actually query today.
  - A re-keyed, county-partitioned `tax_billing_account`-family (per `SPEC_TAX_BILLING_REKEY.md`'s ruling) is **written, but not yet read** — the migration exists on the write side ahead of the read-path cutover.
- **Unit layer:** `prop_unit` / `prop_unit_tax_year` hold source-grain data; `parcel_tax_year` is the canonical geo_id-level rollup, computed by one module (`parcel_rollup.py`), never by loaders directly.
- **Shadow-swap aggregate tables:** derived/summary tables (e.g. `group_stats`, snapshot summaries) are rebuilt into a shadow table and atomically swapped into place, rather than updated in place — this is what `verify_shadow_swap_county_derivation.py` polices.
- **`schema.sql` is stale, and production is truth.** `schema.sql`'s own `CREATE TABLE` bodies do not reflect the composite, county-code-leading primary keys that migrations have since applied in production. `verify_index_coverage.py` states this explicitly and names the count: **schema.sql's CREATE TABLE PRIMARY KEY text is confirmed stale for 15 tables** [verify_index_coverage.py:109, 765], computed dynamically (not hand-typed) by cross-referencing `migrate_county_partitioning.py`'s own `TABLE_SPECS` against what `schema.sql` currently shows. **This is why `verify_index_coverage.py --index-source live` (querying Postgres's real catalog) is the only pre-commit-authoritative invocation; `--index-source schema-sql` is offline-only and understates coverage for those 15 tables.** Do not treat `schema.sql` as ground truth for PK shape on any table without checking this.
- **No migration framework.** There is no Alembic/Django-migrations-style tool; schema changes are applied via one-off scripts (`migrate_county_partitioning.py` and siblings), tracked by convention and by `verify_index_coverage.py`'s live check, not by a migrations table.

## 4. County onboarding / data pipeline

Real file names, per `DATA_LIFECYCLE.md` and `MULTI_COUNTY_ONBOARDING_STANDARDS.md`:

1. **Acquire.** The PM manually downloads/receives county source files, computes SHA-256 checksums immediately, and writes the untouched raw files to the **Raw Vault** (`vault/{county}/{year}/{source}/{date}/`). **Current state:** the Vault lives on one external drive only — there is no offsite backup today [PM-supplied, 2026-09-02]. **Target:** offsite replication is planned under Pipeline v2's D6 (see §17), not built yet — do not describe the Vault as offsite-backed until that ships. A **Vintage Ledger** row (Notion + repo mirror) is opened in state `ACQUIRED`.
2. **Classify.** A **County Profile** and **Classification Map** (per-class-code bucket: `REAL_PROPERTY`, `PERSONAL_PROPERTY`, `EXEMPT/SYNTHETIC`, `UNKNOWN`) are written before first load. `UNKNOWN` codes hard-block the load until a human classifies them.
3. **Per-county format modules with verbatim-header fixtures.** Each county's loader (e.g. `loaders/load_dallas_certified.py`) is a distinct module; its test fixtures are built from the county's **real, verbatim source-file header row** — not a hand-typed guess. This rule exists because of a real incident: Dallas's `situs_address` and `owner_name` were unconditionally blank in production because a loader fixture had been built from a guessed header shape rather than the real one (`KNOWN_LIMITATIONS.md`, the DCAD situs/owner section; resolved under PX-20260827-06). The standing rule drawn from it: **loader fixtures must be built from a real source header row, verbatim, going forward.**
4. **Dry-run before write; canary before live.** Per MC-4, a canary slice (real lines — head/middle/tail plus a stratified sample of every record-type variant, ~50–100MB) runs the real pipeline in miniature against a staging schema before any live/full run.
5. **PM review, then production write, then gates.** **Current practice:** dry-run → PM review of the gate results → direct production write, protected by the `inet_server_addr()` write-guard (see §14) [PM-supplied, 2026-09-02] — there is no separate staging schema and no atomic staging-to-production swap today. **Target (`DATA_LIFECYCLE.md` §3, Stage 2-3):** loads land in staging tables first, never directly into production; the G1–G6 gate battery (below) runs against staged data; only after gates pass and Diego approves does promotion happen as one atomic transaction (staging → production, all summary tables refreshed in the same transaction, confidence labels flipped atomically). Treat the staging/atomic-promotion description as the documented target flow, not a description of how a load actually happens today — the same current-vs-target split as §6/§7.
6. **Derived layers per runbook.** Named runbooks (e.g. `RUNBOOK_DALLAS_METRICS_FIRST_RUN-rev.md`) govern first-time derived-table builds for a new county.
7. **Coverage-driven presentation.** What the UI shows for a given county is meant to be driven by that county's measured data coverage, not by a name-keyed conditional — see §6 and §7 for the current-vs-target state of this.

## 5. G1–G6 — the ingestion conservation gate

Defined in `loaders/ingest_gate.py` (Migration M2, per `SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §4.2). Two-tier standard: **G1–G5 are internal and exact** (zero tolerance — any drift means a loader silently dropped or duplicated something); **G6 is external and banded** (some deviation against an outside publisher is expected).

- **G1 — source scan conservation identity.** Every line of a source file is classified into exactly one bucket (`accepted`, `short_line`, `supplement`, `no_geo_id`, ...); bucket counts must sum to the file's total line count. Catches a line silently falling through every classification with no bucket at all.
- **G2 — identity coverage.** The count of distinct `prop_id`s the file scan says should exist must equal the count that actually landed a `prop_unit_tax_year` row for that same tax year (scoped by `data_source`, fixed under PX-20260824-04 after an earlier unscoped version could never match a single year's file scan).
- **G3 — dollar conservation.** `SUM(market_value)` from the source file must exactly equal the unit-table sum for that source. A separate, deliberately whole-year "G3_rollup" residual check compares the whole-year unit sum against the whole-year `parcel_tax_year` rollup, using a lower-bound residual rather than exact equality (rows with no resolved `geo_id` legitimately don't roll up).
- **G4 — rollup integrity.** For every `geo_id`, `parcel_tax_year`'s stored values must equal an independent re-derivation of `parcel_rollup.py`'s own aggregation logic — checked independently of `parcel_rollup.py`'s own code, so a bug in the rollup module can't hide itself from its own gate.
- **G5 — account coverage.** The count of distinct `geo_id`s with real unit data for a tax year must equal the row count in that year's `parcel_tax_year`.
- **G6 — external reconciliation.** Computed county-wide total vs. a CAD/Comptroller-published total, banded at ±5% (clean pass), ±5–8% (passes with a "warn" flag), beyond 8% (fails). All results write to `ingest_audit`.

**Enforcement gap, confirmed by direct inspection — flag this, do not assume it's handled — keep this finding prominent:** `ingest_gate.py`'s own `__main__` block [loaders/ingest_gate.py:742-793] never calls `sys.exit(1)` on gate failure, and `run_all.py` (the orchestrator that calls `gather_and_run()`) only uses a failed gate to skip the `compute_metrics` step — the loader run itself continues and `main()` returns normally with no nonzero exit code anywhere in the path. **A CI/cron caller checking `$?` today would see success even when G1–G6 failed.** This is a real, currently-open gap, not a documentation error — treat any brief's claim that "exit 1 halts" as the *intended* behavior, not the *current, verified* one, until this is fixed.

**Honest parity disclosure.** Travis runs the full G1–G6 battery described above. Dallas does **not** run the identical battery — it has its own gate function, `run_dallas_ingest_gate()` [loaders/load_dallas_certified.py:614], with its own multi-table G1-analog and G2-analog checks plus one Dallas-specific addition, **G3_FIELD_COVERAGE** [loaders/load_dallas_certified.py:675] (a hard-fail check requiring non-empty `situs_address` in ≥99% of accepted rows and non-empty `owner_name` in ≥95%, per `KNOWN_LIMITATIONS.md:351-358`). Dallas currently has **no G4 (rollup integrity), G5 (account coverage), or G6 (external reconciliation) analog**. So the precise state is: Dallas has its own G1/G2-style structural checks plus a field-coverage check that Travis doesn't have in that form, but is missing G4/G5/G6 entirely — describing this as "Dallas has G3-field-coverage only" understates what exists but correctly identifies G4–G6 as the real, open gap.

## 6. County Capability Contract (target state — not yet built)

**Target design:** each county declares, and the load gate measures, a capability set — per-field populated fractions and source-column-found flags, conceptually named `county_field_coverage` in the design work for this — which the UI, metric eligibility, and marketing-qualifier logic would all read from directly, with a Notion "Parcel Detail — Field Coverage Matrix" as the human-readable mirror.

**What exists today, confirmed by direct inspection, is not that.** `app.py`'s `COUNTY_PROFILES` dict [app.py:~2114] and `county_has_field()` [app.py:~2222] are real and shipped, but `field_coverage` inside `COUNTY_PROFILES` is a **hand-declared dict of booleans** per county — Travis is `True` across the board; Dallas is `False` for `exemption_codes`, `neighborhood_cd`, `year_built`, and billing [app.py:~2158-2163, ~2203-2208] — with an explicit in-code comment stating this is **not derived from a live query at read time** [app.py:~2147-2157]. `county_field_coverage` (the measured-fraction registry) and `county_shows_field()` (a fraction-aware renderer) do **not exist in shipped code today** — they appear only in a design document [STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md:185, 226], which itself states `county_has_field()` is meant to be deleted in the same change that ships the measured-fraction version. Treat the boolean system as **current state** and the fraction-measured system as **planned, queued work** (see §17) — do not describe `county_field_coverage`/`county_shows_field()` as if they already exist.

## 7. Parcel-detail coverage rule (Diego, 2026-09-02 — supersedes any prior 50% rule)

This is a current ruling, not yet fully implemented in shipped code (see the gap noted below):

- **Threshold: 30%,** meant to live as one named constant, not scattered literals.
- County coverage ≥ 30% for a field → that field/row renders for that county.
- Within a rendering county: if a given parcel has a value for that field, show it; if it lacks one, show **N/A** (an honest, per-parcel blank — not an inferred or guessed value).
- County coverage < 30% for a field → that field/row is **omitted entirely** from the page for that county — not dashed, not labeled "Not Available."
- **No inferred fields, ever.** A field is shown only if the county actually publishes/sources it. Travis's valuation-method field is sourced (per Diego's supplied source list) and stays. Dallas's equivalent, where not evidenced by a real source, is deleted rather than inferred.

**Implementation status, confirmed by direct inspection [repo-wide grep, this session]:** there is currently **no shipped `COVERAGE_THRESHOLD` constant anywhere in the repo**, at 30% or any other value. The only coverage-percentage-flavored code found is `data_coverage.py`'s `is_reliable(field, tax_year, min_coverage=0.50)` [data_coverage.py:112] — a **50%** default — which grep confirms is **never called anywhere outside its own definition**; it is dead code, not the active mechanism for anything on the parcel-detail page today. The proposed `fraction > 0.50` render-gating logic that a prior instruction may have referenced also lives only in the `STAGE_A_...md` design document, not in shipped code. **In short: the 30% rule above is Diego's current ruling and should be treated as the target/required behavior for any new work, but no coverage-threshold gating — 50%, 30%, or otherwise — is live in `app.py`/templates today.** Building the named constant and wiring it through is open work (see §17, Stage B).

**[OPEN QUESTION — PM ruling pending; not resolved here, per explicit instruction not to resolve it]:** what denominator should sparse-by-nature fields (exemptions, cap loss, delinquency data) use when computing their coverage fraction? One proposal on the table: source-column-found plus a per-field sanity floor, rather than a literal all-parcels fraction (a field that is genuinely rare for every parcel, like an exemption, would otherwise always compute near-zero coverage under an all-parcels denominator even when the county publishes the column faithfully). This document does not pick a side — whoever builds the Stage B work above must get this ruling from the PM first.

## 8. Parcel-page information hierarchy (planned — no template changes now)

Target ordering for future work, not a description of the current template's actual section order:

1. Basic Property Information
2. Current Tax Data
3. Historical Values/Taxes
4. Analysis & Projections
5. Benchmarking/Comps
6. Tax & Billing Details
7. Data & Methodology

Nothing in this section authorizes a template change; it records intent for whenever that work is scoped.

## 9. County-specific vs. shared logic

Standing rule, converting a convention this codebase has already watched fail into structure (`MULTI_COUNTY_ONBOARDING_STANDARDS.md`'s framing):

- **Shared code never contains a county conditional keyed by name** (e.g. `if county_code == "DALLAS"`) where a capability flag or profile lookup belongs instead.
- Per-county knowledge lives in: per-county format modules (`loaders/load_dallas_certified.py`), per-county classification maps (`classification_map_<county>.py`-style modules), `COUNTY_PROFILES` (app.py), and per-county constants — never scattered `if`-branches inside otherwise-shared functions.
- `tax_logic/` grows by adding new modules for new logic, not by adding `if`-branches to existing ones.
- Every fix to a "Travis-shaped assumption" ships with a structural guard against recurrence, not just a patch to the one call site found.

This standard exists because of real, named incidents, briefly: the identical missing-`county_code` writer bug was independently rediscovered three separate times by accident (a `tax_billing` writer, a shared helper `pir_xlsx_common.py`, and `load_delinquent()`), each time because a prior audit was scoped to a file list rather than the whole repo — "incidental discovery is not an audit," per `MULTI_COUNTY_ONBOARDING_STANDARDS.md`'s MC-2. Separately, a live Dallas spot-check found the property page's "Helpful Links" block hardcoded to Travis/TCAD's institutional URLs for every county (MC-7.7), and two independent "which counties are live" hardcoded arrays were found drifting from each other on the homepage vs. search page (MC-7.8) — both fixed by routing through a single registry (`COUNTY_PROFILES` / `_live_counties()`) instead of a second hand-maintained judgment call.

## 10. Data quality & provenance

- **Vault + manifest + checksums.** Every raw file is archived untouched, SHA-256-checksummed at acquisition (a two-checkpoint pattern — checksum on receipt, re-verified before load), and never edited in place.
- **Verbatim-header fixtures.** See §4 — this rule exists specifically because of the Dallas situs/owner incident where a hand-typed, unverified header guess let a real-data bug (unconditionally blank fields) survive undetected until a live spot-check caught it.
- **Dry-run before write; skip ledgers.** Every source record is accounted for by name in a skip ledger (G1's bucket accounting, per §5) — nothing silently falls through with no bucket.
- **Source-faithful data.** County arithmetic errors are kept verbatim in the data and disclosed, rather than "corrected" silently — Parcelytics reports what the county published, not what it believes the county should have published.
- **Confidence labels.** Data carries a confidence label reflecting its vintage state: Verified (green, sealed/certified), Preliminary (blue), Partial (amber), Estimated (purple), Not Available (slate) — per `THE_FABLE_METHOD.md`'s numbers checklist.
- **`load_batch` provenance** and the **Vintage Ledger** (append-only, Notion + repo mirror) are the record of what data exists and how far along it is — per `DATA_LIFECYCLE.md`, "if the Ledger doesn't say SEALED, the number does not exist for publication purposes."
- **Sealing rules for public figures:** a vintage moves to SEALED only after VERIFY plus a **48-hour settling window** of live operation without incident (`DATA_LIFECYCLE.md` §3, Stage 5) — this window is currently parked/not actively being exercised, but the rule itself (no publication earlier than 48 hours after live verification, regardless of news pressure) stands whenever sealing resumes.
- **Published Metrics Log.** After sealing, canonical figures (total market/assessed/taxable value, row count, by-type breakdown) are written to this log, each tied to a computing script, its commit, and the vintage id — **these are the only county-level figures anyone (marketing, press, Diego in a text message) may cite externally.**

## 11. Testing & verification

- **The scanner/fixture suite:** on the order of 100 Python scripts, most fixture-based and runnable offline (roughly 93 of them, per the repository's own script inventory, need no live database; roughly 7 are psycopg2-dependent live-DB scripts).
- **Invocation convention:** scripts are run with `/usr/bin/python3` directly; each prints an explicit pass/fail string and exits with a matching process exit code.
- **The recurring pre-commit set** (per `THE_FABLE_METHOD.md`'s engineering checklist), run together as complementary, non-redundant checks:
  - `verify_county_scoping.py` — the full-repo county-scoping audit (§9's MC-2), checked against the `COUNTY_SCOPED_TABLES` registry.
  - `verify_index_coverage.py --index-source live` — the **one authoritative** pre-commit invocation (queries Postgres's real catalog). `--index-source schema-sql` is offline-only and **understates** coverage for the 15 tables `schema.sql`'s own text is confirmed stale for (see §3) — never treat a `schema-sql`-mode pass as sufficient on its own.
  - `verify_shadow_swap_county_derivation.py` — required pre-commit for any shadow-table-then-atomic-swap writer.
  - Template scoping (`verify_template_county_scoping.py`), the copy denylist (`verify_unavailable_copy_denylist.py`), exemption gating, and param-placeholder safety checks round out the recurring set.
- **EXPLAIN-proof scripts** for any change to a plan-sensitive query — extracting the real, shipping SQL via AST parsing of the actual source file (never hand-retyped) and running `EXPLAIN` against it inside a rolled-back transaction. Precedent scripts: `loaders/explain_compute_metrics_passes.py`, `loaders/explain_snapshot_summary_county_derivation.py`.
- **Verify-then-approve.** No approval — of a fix, a migration, a claim — is given from a summary of what happened; the actual evidence (a passing scanner run, an EXPLAIN plan, a live-verified page) is checked directly. This is `THE_FABLE_METHOD.md`'s first law.
- **Fixtures must fail on the pre-fix shape.** A regression fixture that would have passed against the code *before* the fix it's meant to guard isn't testing anything — per MC-2's "acceptance test for the audit itself" principle (three fixtures reproducing three real incidents, and the audit must fail all three).

## 12. Git / branch / commit conventions

- **Trunk-based:** everything happens on `main`. There are no long-lived feature branches by convention — as of this writing, six stale local branches exist in the repo as clutter, not as an intended workflow; this is noted here as a cleanup candidate, **not** something to act on unless separately instructed.
- **One commit per brief**, with a descriptive commit message; push immediately after committing — work does not sit uncommitted or unpushed.
- **A known local quirk:** `rm -f .git/index.lock` is sometimes needed to clear a stale lock file before a commit can proceed.
- **Version bump + CHANGELOG entry per deploy.** Currently in arrears, confirmed by direct inspection: the `VERSION` file reads `1.10.0` [VERSION], while `CHANGELOG.md`'s most recent (topmost) entry is `[1.5.0] — 2026-07-30` [CHANGELOG.md, top entry] — five minor versions and over a month have passed with no corresponding CHANGELOG entries for 1.6.0 through 1.10.0. A `v1.11.0` catch-up entry is queued (see §17).

## 13. Security & secrets

- Secrets (database URLs, `FLASK_SECRET`, Sentry DSN, etc.) live **only** in Render's environment variables or a local shell environment — never in code, commits, chat logs, or documentation. `config.py` reads these via `os.environ` and raises `RuntimeError` at startup if `FLASK_SECRET` is unset and `DEBUG` is off, rather than silently running insecurely.
- **Names, not values, when reporting.** When discussing configuration in a report, brief, or commit message, name the environment variable, never paste its value.
- **Internal-DB-URL convention:** Render's internal database URL (not the external one) is the one the running app should use.
- **Credential rotation** happens only via a written, three-step runbook executed in one sitting — never partially, never improvised.
- Direct inspection confirms **no hardcoded-secret patterns** currently exist in the codebase — keep it that way; this is a standing property to verify, not a one-time finding.

## 14. Production DB / deployment approval

- **Production writes happen only after:** a dry run, PM review of the gate results, and Diego personally running the live load — per `DATA_LIFECYCLE.md`'s two-key promotion (PM requests, Diego approves) and the explicit statement that this is "deliberately a human moment — it's the last cheap place to say no."
- **`inet_server_addr()` write-guard:** any script capable of a production write checks the connected server's address before writing, so a script accidentally pointed at production from a dev/test invocation fails loudly instead of writing silently.
- **Runbook deviations mean STOP,** not "improvise and continue" — any live run that departs from its written runbook halts for review rather than proceeding on judgment.
- **Pre-run `pg_dump` + per-column diff** is standard practice before any derived-table rebuild, so a rebuild's actual effect can be verified column-by-column against the pre-rebuild state.
- **Deploys are Manual** on Render, at a PM-named commit id, on the service named in PM communications as `srv-d9dfgctaeets7393bq2g`. **Disclosure:** this service id does not appear anywhere in the repository itself (confirmed by a full-repo search) — it is Render-dashboard-only information, included here as a PM-stated fact but not independently verifiable from static repo inspection.
- **Scanners green first, live-verify after.** The pre-commit scanner set (§11) passes before a deploy is requested; after the deploy, the live site is checked directly (not assumed from the scanner pass) before considering the deploy complete.

## 15. Autonomy boundaries

**Agents (Cowork, or any AI working in this repo) may act autonomously, without asking first, to:**
- Read anything in the repository.
- Build code and fixtures inside their own sandbox.
- Run read-only diagnostics and hand the results to Diego.
- Draft documents and briefs.
- Update Notion logs the PM owns (e.g. the Vintage Ledger, Task Log), where the PM has asked for that.

**The following always require Diego, specifically, before happening:**
- Any production database write.
- Any commit or push to the repository.
- Any deploy (Render Manual Deploy is triggered by Diego/PM, at a named commit).
- Any credential action (rotation, viewing, entering).
- Any spend.
- Publishing any figure externally.

**The following require a PM ruling before an agent proceeds** — mirroring `THE_FABLE_METHOD.md` §7's escalation rule, which every agent should treat as binding here too:
1. Any new architecture or schema decision.
2. Anything that changes or contradicts a canon figure (a number already on record, e.g. in the Published Metrics Log).
3. The first instance of any new pattern (the first time a kind of check, migration, or structural fix is done at all).
4. Any legal-adjacent judgment call.
5. Any cross-county semantic question (does "market value" mean the same thing in two CADs? does a comparison need a semantic-parity check first?).
6. Any situation where the checks pass but something still feels off — a real, named category in `THE_FABLE_METHOD.md`, not a hedge.
7. Anything that would delete a disclosure, weaken a gate, or trade rigor for speed.

An agent hitting any of these seven should stop and escalate rather than resolve it unilaterally — the §7 open question in §7 above (the coverage-denominator ruling) is a live example of exactly this category.

## 16. Known limitations & risks

Condensed from `KNOWN_LIMITATIONS.md`, other named spec docs, and this session's own inspection (sourced per-item below) — treat each as a currently-true constraint, not a historical footnote:

- **Single-machine operations, single-drive vault, no offsite backup.** **Current:** the Raw Vault and much of the operational tooling run from one machine with one external drive only — there is no offsite backup and no redundant multi-location setup today [PM-supplied, 2026-09-02]. **Target:** offsite replication is planned under Pipeline v2's D6 (§17), not built yet.
- **No staging deployment.** Production is the only Render environment; "staging" where it exists means a Postgres schema, not a second deployed app.
- **The database is memory-bound.** ~13 GB of live tables against roughly 1 GB of available RAM, with a documented ~22% heap cache hit rate [KNOWN_LIMITATIONS.md — repeated as the repo's on-file figure, not independently re-measured by this agent this session]. Real, measured pathologies exist as a result. `api_peer_set`'s Tier 2 has two on-record figures from two different points in time, and both should be kept, not merged: **8.2 seconds** (round 1) and **16.4 seconds** (round 2), from an earlier incident, per `app.py`'s own in-code comments [app.py:~8038-8041]; and **3.83 seconds**, a live production `EXPLAIN ANALYZE` measurement dated **2026-09-02**, per the PM's Report 1, Appendix A [PM-supplied, measured 2026-09-02, EXPLAIN ANALYZE, PM Report 1 Appendix A — not independently re-run by this agent, which has no live database access]. Read together: the app.py figures describe an earlier incident's numbers (undated in-code, superseded by later fixes per the surrounding comments), while 3.83s is the more recent, dated, live measurement — treat 3.83s as the current figure for Tier 2 and the 8.2/16.4s pair as historical context, not as competing current claims.
- **Dallas has real, confirmed data gaps.** `exemption_codes` [KNOWN_LIMITATIONS.md:~397] and `neighborhood_cd` [KNOWN_LIMITATIONS.md:~405] are each documented, separately, as 100% empty for Dallas — not as one grouped "four fields" item; `year_built` is mentioned only in passing, inside the `neighborhood_cd` section, as "the same class of gap" [KNOWN_LIMITATIONS.md:~407]. Dallas also has no billing data loaded. `classi_cd` does **not** appear in `KNOWN_LIMITATIONS.md` as a Dallas-empty field — it appears in an unrelated Travis-sourcing context [KNOWN_LIMITATIONS.md:~487]. **Do not describe these four fields as one uniformly-documented "verified-empty" group** — verify each field's actual status individually before citing it.
- **`load_delinquent()` is an unscoped writer.** Confirmed, per `verify_county_scoping.py`'s own module docstring and a live scanner run: it has no `county_code` parameter, no `county_code` in its INSERT column list, and uses `ON CONFLICT (geo_id)` instead of `(county_code, geo_id)` — a live, currently-unpatched gap against the `tax_delinquent` table [verify_county_scoping.py, module docstring].
- **Unpinned dependencies, no CI/runner.** `requirements.txt` uses `>=` version bounds throughout (no lockfile, no `runtime.txt` pinning the Python version) [requirements.txt]; there is no CI pipeline running the scanner suite automatically — the ~100-script suite is run manually.
- **CHANGELOG/version arrears.** See §12 — five minor versions of undocumented history as of this writing.
- **Sandbox-blind implementer risk.** An agent implementing a change from a sandbox with no live database has, historically, hit first-execution incidents (things that only surface against real data or a real connection) — this is named as a standing risk category, not a one-time event, and is part of why canary slices (§4, MC-4) and EXPLAIN-proof scripts (§11) exist.
- **`pg_stat_statements` is not installed** on the production database — a real observability gap for diagnosing slow-query patterns beyond what individual EXPLAIN runs can show. **Correction to source attribution:** this session's inspection found this stated in `SPEC_AGGREGATE_PRECOMPUTATION.md`, not in `KNOWN_LIMITATIONS.md` as this section's own intro line implies [SPEC_AGGREGATE_PRECOMPUTATION.md] — noted here so a future reader doesn't search the wrong file.
- **Gate-failure exit codes are not enforced** [loaders/ingest_gate.py:742-793; run_all.py] — a real, currently-open structural gap, not a documentation error. Repeated here deliberately from §5 because it is easy to miss and directly affects how much trust to place in "the pipeline halted" claims.

## 17. Priorities & order (current queue — expected to change often)

This section is explicitly the one section on this page expected to be revised frequently; the live version of the queue lives in the Notion Task Log, not here. As of this writing, in order:

1. Version bump to v1.11.0 + the CHANGELOG catch-up entry (§12).
2. Stage B of PX-20260907-02-rev — building out Dallas field coverage per the County Capability Contract (§6) and the 30% rule (§7) — **blocked on the PM's ruling on the coverage-denominator open question in §7.**
3. Password re-rotation (Diego, via the written runbook, §13).
4. PX-20260907-03 — Pipeline v2 (D1–D6).
5. Dallas billing/delinquency data acquisition.
6. Harris County as the first pipeline-native county (built against the v2 pipeline from day one, rather than retrofitted).

A consultant's recommendations, or a new PM brief, may reorder this list — when that happens, update this section and the Notion Task Log together; don't let them drift apart from each other the way the "what's live" registries once did (§9).

---

*Amendment note: this document should be revised the way the specs it draws from are — by a reviewed, dated change, not a silent edit. If something here turns out to be wrong or out of date, say so in the next revision rather than quietly retyping it.*
