# PX — Engineering Foundation Mission 1: Completion Report

**Verification, Agent Context & CI Foundation**
Prepared by: Cowork · Date: 2026-09-02 · Governed by: `parcelytics.md` @ commit `301f81a` (binding) and the PM preamble attached to this mission

Evidence tags used throughout: `[file:line]` (repository evidence), `[measured YYYY-MM-DD, artifact]` (a result this agent actually ran and observed), `[dashboard, YYYY-MM-DD]` (production/dashboard evidence — this agent has none; any dashboard fact here is `[PM-supplied]`), `[document]` (a documented operating rule), `[unknown]` (not established from available evidence), `[PM-supplied]` (stated by PM/Diego, not independently verified by this agent — used for all Notion content per the mission preamble's point 4).

**Revision note (2026-09-02, post-review):** this report was revised after PM review to apply three corrections: (1) `verify_index_coverage.py` moves from the required tier to advisory, per the PM's ruling that its schema-mode red is a documented false signal (`parcelytics.md` §3/§11) — required tier is now 6 checks and exits 0 on the current tree; (2) both counties are corrected as fully live in production, Dallas mid-field-completion rather than mid-onboarding; (3) the CI workflow's Python version is corrected from an unconfirmed 3.11 guess to the PM-supplied, evidenced 3.14. The sections below reflect the corrected, current state; the original natural-failure evidence from the initial draft is preserved inline (not deleted) wherever it remains relevant history.

---

## Executive Summary

Mission 1 built the minimum scaffolding needed for a future agent session to work on Parcelytics safely: a thin `CLAUDE.md` that points into `parcelytics.md` rather than duplicating it; a full, measured (not assumed) inventory of the ~170 verification/scanner scripts in the repo; one authoritative, DB-free verification runner (`run_offline_checks.py`) built from `parcelytics.md`'s own named pre-commit set; a minimal GitHub Actions workflow around that runner; and an honest assessment of runtime/dependency reproducibility and version/release management, with an explicit recommendation.

The required tier, run today, exits **0** — 6/6 green. `verify_index_coverage.py` sits in the advisory tier instead, per the PM's ruling that its DB-free schema-mode invocation is a documented false signal, not a real red: `parcelytics.md` §§3/11 already establish that `schema.sql`'s PRIMARY KEY text is stale for 15 tables, and this mission's own measurement confirms all 40 of the script's "UNCONFIRMED" findings resolve only via that already-known-stale text. The script's authoritative invocation is `--index-source live`, run by a human directly against Postgres before any schema-sensitive change (`parcelytics.md` §11) — not something this DB-free runner can ever execute, required or not. Its 10 "REAL GAPS FOUND" schema-mode findings are preserved verbatim in Known Conflicts below, tagged `[unknown — requires live-mode triage]`, not silently dropped. Failure-propagation is now demonstrated by the synthetic `--seed-failure` canary as the standing proof; the real, natural `verify_index_coverage.py` failure this mission originally measured was genuine at the time and is preserved below as history, not deleted, even though it no longer gates the required tier.

No production, database, deployment, or architectural work was performed. No file was committed or pushed — all four new files sit in the working tree awaiting Diego's review and commit, per the preamble's unchanged commit-authority rule.

## Files Changed

Working-tree changes only — nothing committed or pushed by this agent:

- **`CLAUDE.md`** (new) — thin operational entry point.
- **`run_offline_checks.py`** (new) — the authoritative offline verification runner.
- **`.github/workflows/offline-checks.yml`** (new) — minimal CI wired to the runner's required tier.
- **`PX_ENGINEERING_FOUNDATION_M1_REPORT.md`** (new, this file).

`parcelytics.md` itself is **not** a change from this mission — it was already committed at `301f81a` before this mission started [file:git log `301f81a`]. One pre-existing untracked file, `STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md`, sits in the working tree from an earlier, separate brief; this mission did not create or touch it — confirmed via `git status --short` before and after this mission's work.

## `CLAUDE.md`

Built to the preamble's explicit shape: identity in three lines, a repository map, a source-of-truth table, the offline-runner command, autonomy boundaries (pointing at `parcelytics.md` §15), the one county-data rule stated directly (never infer unpublished data), G1–G6 current status, known sandbox hazards, the expected bounded-change workflow, and an explicit "version/release management is unresolved" note rather than picking a side. It is 75 lines [measured, `wc -l CLAUDE.md`]. Every substantive rule is a pointer (`→ parcelytics.md §N`), not a restatement — the one exception is the evidence-tag legend and the "never cite a source you can't reach" rule, repeated verbatim because it is the rule this mission's own corrections (in the parcelytics.md thread) exist to enforce, and a pointer alone risked being skipped.

## Verification Inventory

**Method, disclosed:** two static/AST scripts (built for this mission, not committed) walked every `git ls-files '*.py'` result (168 files [measured 2026-09-02]) and classified each by keyword/AST heuristics (DB access markers, write-verb SQL, production-sensitive flags, county-name markers, fixture/test markers, template/render markers, static-lint markers, EXPLAIN markers, vault-path markers, network markers). This is a heuristic, not a semantic analysis — flagged explicitly where it produced false positives (below). A second pass filtered to scanner/test-like files by naming convention (`test_*`, `verify_*`, `explain_*`, `validate_*`, `investigate_*`, `check_*`, plus three named exceptions) — 101 files [measured] — and **actually executed** every one of these classified as DB-free, bare, no arguments, 12-second timeout, capturing the real exit code. This measured-execution step is what caught two real problems the static heuristic alone would have missed (below); this is why the report leans on measured evidence rather than the static classification alone, per this mission's own evidence rules.

**Aggregate counts (168 tracked .py files, static classification):** county-specific 122, db-free 73, fixture/parser 56, db-write 52, static/lint 47, db-read-only 43, template/render 38, production-sensitive 17, vault-dependent 13, performance/EXPLAIN 11, network-dependent 10 [measured 2026-09-02]. A file can carry multiple tags.

**Scanner/test-like subset (101 files): measured execution results.**
- 54 were classified DB-free by the static heuristic and were actually run. 44 exited 0; 10 exited nonzero [measured 2026-09-02].
- Of the 10 nonzero exits: 4 are **static-heuristic false negatives**, not real DB requirements — `loaders/check_geo_ids_in_pir_source.py`, `loaders/test_snapshot_2026_preliminary.py`, `test_parcel_rollup.py`, and `verify_2026_prelim_vs_cert_reconciliation.py` all fail with `ModuleNotFoundError: No module named 'psycopg2'` because they transitively import `loaders/db.py`, which imports `psycopg2` unconditionally with no fallback guard — the heuristic only checks a file's own text, not its import graph [measured 2026-09-02]. This is itself worth noting: `loaders/explain_compute_metrics_passes.py` uses a deliberate real-driver-guard pattern (`_install_fake_psycopg2()`) precisely so AST-only work can proceed without a live driver; these four files don't, and would benefit from the same pattern if they're ever meant to run logic-only in a sandbox.
- 2 fail because they require CLI arguments (`loaders/check_geo_ids_in_taxcur_source.py`, exits 2 with a usage message) or a live running Flask app on `localhost:5000` (`ui_test.py`) — expected behavior for their design, not a defect, but confirms they are unsuitable for a bare, arg-free "required tier" invocation.
- 2 (`verify_px_20260828_09_provenance_and_hints.py`, `verify_px_20260828_15_task1_neutral_county_base.py`) fail on real, disclosed limitations of their own render harness or an explicit "needs a live post-deploy check" self-report — not blocking regressions.
- **2 fail for a reason worth flagging prominently: `verify_rollup_canonical.py` and `verify_tax_billing_rollup_canonical.py`, both part of the canonical-writer-enforcement family named in `MULTI_COUNTY_ONBOARDING_STANDARDS.md`'s MC-3, currently exit 1 on the committed tree** [measured 2026-09-02]. Reading their own output: both fail because their own grep-based canonical-writer check matches literal SQL text (`INSERT INTO parcel_tax_year`, `INSERT INTO tax_billing`) embedded inside *other scripts' test fixtures* (`test_verify_county_scoping.py`, `test_verify_shadow_swap_county_derivation.py`), not a real production writer. This reads as a false positive from a test fixture that happens to contain realistic SQL strings — but this mission did not attempt to confirm that conclusion by tracing every fixture line, and does not fix scanner logic under any circumstances (out of scope; "do not weaken verification to make CI pass" cuts both ways — this mission also does not strengthen or patch a scanner's own logic). **Recorded as an unresolved, currently-red condition** in Known Conflicts below, and deliberately excluded from both the required and advisory tiers of the runner rather than silently included as if healthy.
- Of the 47 remaining scanner-like files not classified DB-free by the static heuristic, most correctly need a live database (`--check-db` modes, `loaders/db.get_conn()`), but two — `verify_county_scoping.py` and `verify_index_coverage.py` — were **misclassified** by the static heuristic (they contain DB-related keywords for their optional `--check-db`/`--index-source live` modes, but their *default*, argument-free invocation is a pure static/AST scan). Both were probed by hand and confirmed DB-free at default invocation [measured 2026-09-02]. `verify_county_scoping.py` sits in the required tier; `verify_index_coverage.py` sits in advisory, per the PM ruling — see Offline Runner and Verification Results below.

## Offline Runner

**Command:** `python3 run_offline_checks.py` (repo root). `--advisory` additionally runs a larger non-blocking tier; `--seed-failure` appends one synthetic, always-failing check to prove exit-code propagation; `--quiet` suppresses each check's own stdout/stderr tail.

**Required tier (6 checks) — this mission's mapping of `parcelytics.md` §11's named recurring pre-commit set onto scripts confirmed runnable bare, DB-free, in a sandbox with no PostgreSQL/psycopg2 and no network access:**

1. `verify_county_scoping.py` — full-repo county-scoping audit
2. `verify_shadow_swap_county_derivation.py`
3. `verify_template_county_scoping.py`
4. `verify_unavailable_copy_denylist.py`
5. `verify_exemption_gating.py` (exemption gating, named in `parcelytics.md` §11)
6. `loaders/test_param_sql_placeholder_safety.py` (param-placeholder safety, named in `parcelytics.md` §11)

**`verify_index_coverage.py` moved to advisory (PM ruling, post-draft correction).** It was initially placed in the required tier as `parcelytics.md` §11's own naming would suggest, and its schema-mode invocation genuinely exits 1 on the committed tree (see below). On review, the PM ruled that this is a documented false signal, not a real red: `parcelytics.md` §§3/11 already disclose that `schema.sql`'s PRIMARY KEY text is stale for 15 tables, and this mission's own measurement confirms all 40 of the script's "UNCONFIRMED" findings resolve only via that already-known-stale text — so a bare/schema-mode run of this specific script was never a meaningful required-tier signal to begin with. The script's authoritative invocation is `--index-source live`, which needs a real Postgres connection this DB-free runner cannot ever provide, required or not — per `parcelytics.md` §11, that mode must still be run by a human directly against Postgres before any schema-sensitive change. The script now runs under `--advisory` instead, where its current red state is still visible and reported, just non-blocking.

**Advisory tier (41 scripts, non-blocking):** `verify_index_coverage.py` plus the measured-DB-free, exit-0 scanner/test files not in the required list — see `run_offline_checks.py`'s own `ADVISORY` list for the exact 41 file paths. Failures here are printed but never change the process exit code.

**Deliberately excluded from both tiers:** `verify_rollup_canonical.py`, `verify_tax_billing_rollup_canonical.py` — currently red on what reads as a false positive against test fixtures (see above); including them as "required" would make the runner permanently, uninformatively red for a reason unrelated to any real change, and including them silently as "advisory" would hide a real, if likely spurious, finding. Excluded with a documented reason instead, per the runner's own docstring.

**What it explicitly does not require:** a live database of any kind, `psycopg2`, `DATABASE_URL`, the raw-source vault (`PARCELYTICS_DATA_ROOT`/`PARCELYTICS_ARCHIVE_ROOT`), any network access beyond what Python's own interpreter needs, or any credential.

## Verification Results (actual, measured 2026-09-02, post-correction)

Required-tier run, this sandbox, no database, no network:

```
[PASS] county-scoping audit (verify_county_scoping.py)                                  (0.42s, exit=0)
[PASS] shadow-swap county-derivation audit (verify_shadow_swap_county_derivation.py)     (0.21s, exit=0)
[PASS] template-layer county-scoping audit (verify_template_county_scoping.py)           (0.29s, exit=0)
[PASS] data_unavailable copy denylist (verify_unavailable_copy_denylist.py)              (0.09s, exit=0)
[PASS] exemption-gating selftest (verify_exemption_gating.py)                            (0.08s, exit=0)
[PASS] param/SQL placeholder safety (loaders/test_param_sql_placeholder_safety.py)       (0.09s, exit=0)
------------------------------------------------------------------------------
REQUIRED TIER: PASS -- 6/6 checks green
```
Runner process exit code: `0` [measured 2026-09-02].

**Advisory tier**, run separately: 40/41 green, `verify_index_coverage.py` red and disclosed [measured 2026-09-02]:

```
[FAIL] verify_index_coverage.py  (0.23s, exit=1)
... (40 other advisory checks, all PASS)
------------------------------------------------------------------------------
ADVISORY TIER: 1/41 failed (non-blocking):
  - verify_index_coverage.py
```

**On `verify_index_coverage.py`'s advisory-tier failure, in detail — preserved from this mission's original measurement, now non-blocking.** Its bare invocation prints two distinct finding categories and exits 1 if either is nonempty [file: `verify_index_coverage.py:1373`, `sys.exit(1 if (gaps or unconfirmed_pk or tenant_gaps or tenant_stale) else 0)`]:
- **40 "UNCONFIRMED" findings** — every one of these resolves only via `schema.sql`'s already-known-stale inline PRIMARY KEY text, and each finding's own message names `parcelytics.md`'s exact disclosed gap (schema.sql's stale PK text for 15 tables; the real, live PK now leads with `county_code` per `migrate_county_partitioning.py`'s `TABLE_SPECS`). This is the tracked issue `parcelytics.md` §3 and §11 already name — not new. This is exactly the PM's basis for ruling the check's schema-mode red a documented false signal.
- **10 "REAL GAPS FOUND" findings** — cases where a table *does* have real indexes in `schema.sql`, but none of them start with a column a query filters/joins by equality (e.g. `app.py:5167` filters `snapshot_totals` by `county_code`, but neither `idx_snapshot_totals_view` nor `snapshot_totals_pkey` starts with that column; similar findings against `parcel_tax_year`, `tax_billing`, `prop_unit`, and `parcel_2026_preliminary_snapshot`). **This is a newly surfaced finding from this mission's own measurement — it does not appear to be named anywhere in `parcelytics.md`, `KNOWN_LIMITATIONS.md`, or any spec this mission read.** Per the PM's instruction, this finding is kept, verbatim, in Known Conflicts below as `[unknown — requires live-mode triage]` — not dropped just because the check moved to advisory. It may or may not represent a real, live performance risk (schema-mode index data can itself be stale, per the same tool's own warning); this mission does not investigate further or fix it, and neither confirms nor dismisses it — that determination requires the live-mode run this DB-free runner cannot perform.

## Failure Propagation

The mission's requirement to demonstrate that a required-tier failure correctly produces a nonzero process exit code is now met by the synthetic canary as the **standing proof**, since the required tier is green on the current tree:

`python3 run_offline_checks.py --seed-failure` appends one always-failing check (`SYNTHETIC CANARY`) to the required tier. Measured output:
```
[FAIL] SYNTHETIC CANARY (--seed-failure, not a real check)  (0.00s, exit=1)
  --- output tail ---
  SYNTHETIC CANARY: this check is designed to always fail (--seed-failure was passed).
  -------------------
------------------------------------------------------------------------------
REQUIRED TIER: FAIL -- 1 check(s) failed:
  - SYNTHETIC CANARY (--seed-failure, not a real check)
```
Runner process exit code: `1` [measured 2026-09-02].

**For the record, preserved from this mission's original measurement:** before the PM's ruling moved `verify_index_coverage.py` to advisory, its required-tier failure was real, non-synthetic, and produced the same result — the runner's exit code on that earlier run was `1`, with `verify_index_coverage.py` as the sole failing required check. That was genuine, contemporaneous proof at the time it was measured; it is recorded here as history rather than deleted, precisely because the mission's own evidence rules call for disclosing what was found, not just what's convenient after a correction.

## GitHub Actions

`.github/workflows/offline-checks.yml` — triggers on push/PR to `main` and manual dispatch; checks out the repo, sets up Python **3.14** (production's real version, evidenced by `.venv/lib/python3.14` appearing in Render production tracebacks [Render logs 2026-09-01, PM-supplied] — no longer a guess; see Runtime & Dependency Assessment below), installs `requirements.txt` via pip (this is normal PyPI package installation, not the kind of "network dependency" the mission asked to avoid — it does not reach any Parcelytics-specific or production endpoint), then runs `python3 run_offline_checks.py` (required tier only; `--advisory` is deliberately not part of the CI gate).

It does not connect to any database, does not write to production, does not deploy, does not require or reference any production credential, does not rotate anything, and does not touch the raw-source vault.

**`CI execution: VERIFIED`** — first run (run #1, "Offline verification (required tier)", trigger: push of `826944a`) completed green in 26 s [dashboard 2026-09-09, github.com/aicodingg/parcelytics/actions]. The paragraph below records the pre-push state for history. Original status line: `CI execution: NOT VERIFIED`. Per the mission preamble's point 2: this agent writes to the working tree only; it does not commit or push. This workflow file has therefore never actually run on GitHub — it cannot have, since GitHub Actions only executes workflow files that exist on a pushed branch. Confirmed the real GitHub remote is `https://github.com/aicodingg/parcelytics.git` [file: `git remote -v`].

**Exact instructions for Diego to verify the first real run**, once this file (and `run_offline_checks.py`, `CLAUDE.md`) are committed and pushed to `main`:
1. Push the commit containing `.github/workflows/offline-checks.yml` to `main` (or open a PR against `main` — either trigger fires the workflow).
2. Go to `https://github.com/aicodingg/parcelytics/actions` and look for a run named "Offline verification (required tier)" corresponding to that push/PR.
3. Open the run, expand the "Run authoritative offline verification (required tier)" step, and confirm it shows the same required-tier output as this report's Verification Results section (6/6 PASS) and an overall job status of **green**, since the required tier now exits 0 on the current tree with `verify_index_coverage.py` moved to advisory. **A red run at this point is now the discrepancy to investigate** — e.g. a different Python/dependency environment behaving differently than this sandbox, or a real regression introduced since this report was written — not something to assume away.
4. If Diego/PM later decide to promote `verify_index_coverage.py` back to required (e.g. once the underlying index/schema issues are fixed or the 15-table PK migration lands), move it back from `ADVISORY` to `REQUIRED` in `run_offline_checks.py` and re-run the workflow via "Run workflow" (workflow_dispatch) to confirm the new expected state.

## Runtime & Dependency Assessment

- **Declared dependencies** (`requirements.txt`, 7 lines): `flask>=3.0`, `psycopg2-binary>=2.9`, `openpyxl>=3.1`, `reportlab>=4.0`, `sentry-sdk[flask]>=2.0`, `Flask-Limiter>=3.5`, `gunicorn>=21.2` [file: `requirements.txt`]. Every bound is a floor (`>=`), none is pinned to an exact or capped version.
- **No lockfile** of any kind (no `requirements.lock`, no `Pipfile.lock`, no `poetry.lock`).
- **No Python runtime declaration anywhere in the repo** — confirmed absent: `runtime.txt`, `.python-version`, `Pipfile`, `pyproject.toml` [measured 2026-09-02, `ls` in repo root]. This sandbox runs Python 3.10.12 [measured, `python3 --version`], which is this sandbox's own interpreter, not production's.
- **Production's actual Python version is now known: 3.14** [Render logs 2026-09-01, PM-supplied] — `.venv/lib/python3.14` appears directly in Render production tracebacks. This resolves what was previously flagged as `[unknown]` in this mission's initial draft. It is still not committed anywhere in the repository itself, so a fresh clone still cannot derive it without this report or the PM's own record — the P1 recommendation to add a committed `runtime.txt`/`.python-version` stating `3.14` stands, to close that remaining gap.
- **No `render.yaml` or `Procfile`** in the repository — confirmed absent by the same listing. Render's Start Command and runtime selection therefore live entirely in Render's own dashboard configuration, outside version control.
- **CI implication**: this mission's GitHub Actions workflow now pins Python `3.14`, sourced from the PM-supplied Render-log evidence above — no longer a guess, but still not self-derived from the repo, which is exactly the gap the P1 `runtime.txt` recommendation closes.
- **Agent/environment compatibility hazard, confirmed by measurement**: `psycopg2` is not installable in this sandbox (no network access to PyPI/wheels here), and several scripts import it unconditionally with no fallback guard, so they fail at import time for an environment reason unrelated to their actual logic (see Verification Inventory above). Any future agent sandbox without a live Postgres driver will hit the identical failure on those same files.
- **Recommendation (not implemented — Deliverable E explicitly forbids a broad migration):** a committed `runtime.txt` or `.python-version` naming the real, Render-confirmed Python version would close the biggest single reproducibility gap here at near-zero cost. Pinning `requirements.txt` to exact versions (or adding a lockfile) is a larger, separate decision this mission does not recommend forcing during foundation work — see P1 below.

## Repository / Process Hygiene

- **Branches**: `main` (active) plus six stale local branches — `data/backend-followups`, `integration/all-tasks`, `task/1-post-acq-estimator`, `task/2-address-typeahead`, `task/3-scenario-bands-peer-bench`, `task/4-page-hierarchy`, `task/5-drill-through-compare` — every one last committed in late June 2026 and 200+ commits behind `main` [measured 2026-09-02, `git rev-list --left-right --count main...<branch>`]. Confirms `parcelytics.md`'s own disclosure. Not deleted or touched — no destructive cleanup is authorized in this mission, and branch deletion is exactly that.
- **Untracked files**: only one pre-existing untracked file, `STAGE_A_PX-20260907-02-rev_dallas_field_coverage.md`, plus this mission's four new files — confirmed via `git status --short` before and after this mission's work. No other stray generated/scratch artifacts were added.
- **Large data files present but gitignored** (`trwfile.748978.zip` at ~277MB, `cert_2021_full.csv` at ~32MB, and others) are correctly excluded from version control by `.gitignore`'s `*.csv`/`*.zip`/etc. rules [file: `.gitignore`] — confirmed via `git ls-files` (they do not appear) — not a repository-hygiene problem, just scratch data sitting in the working directory.
- **No CI/automation existed before this mission** — no `.github/` directory was present.
- **Agent-specific hazard, confirmed**: an in-code comment in `app.py` about gunicorn's worker count was shown stale against a direct Render dashboard read during this same PM thread (a real, dated correction, not hypothetical) — the concrete example `parcelytics.md` §2 and `CLAUDE.md` both now cite for "don't trust an in-code comment over a fresh dashboard check."
- **Git vs. Notion**: Git is authoritative for code and its history; Notion [PM-supplied] is described as holding four PM-maintained logs — Task, Issues, Version/Metrics, and Memory. This agent has no Notion access and did not inspect any of them directly; every claim about what Notion currently holds is reported from `parcelytics.md`/PM-supplied convention, not independently verified — tagged `[PM-supplied]` throughout this report and in `CLAUDE.md`.

## Version / Release Management Recommendation

**What's actually there, measured:** the `VERSION` file is hand-edited per deploy, with a real commit convention ("Bump VERSION to X.Y.Z") going back to `v1.2.0` [file: `git log --oneline -- VERSION`]. `CHANGELOG.md` was updated in lockstep through `1.5.0` (2026-07-30), then stopped — the very next `VERSION`-touching commit jumped straight to `1.9.2` in one step, with **no CHANGELOG entries and no intermediate commits for 1.6.0 through 1.9.1** [measured 2026-09-02, `git log --oneline -- VERSION`] — meaning this isn't five small skipped updates, it's one commit that silently absorbed an unknown amount of undocumented version history. **Zero Git tags exist** [measured, `git tag -l`, count 0] and no GitHub Releases exist (no tags, no releases possible). Deploys are Manual on Render, at a PM-named commit SHA — meaning the actual "what's live in production" fact already lives in a commit SHA today, just not one Git itself labels.

**Recommendation: Option C (hybrid), specifically —**

- **Git/GitHub should own**: the exact code-to-version mapping, via **annotated Git tags** (`git tag -a v1.10.0 -m "..."`) created at the same commit already chosen for each Render deploy, and (once tags exist) **GitHub Releases** built from those tags, each with release notes that can simply reproduce the `CHANGELOG.md` entry for that version. This is the smallest possible change that fixes the actual, measured problem: today, "what commit is version 1.10.0" is answerable only by reading the `VERSION` file's current content plus scanning `git log` for the bump commit — a tag makes it a single `git show v1.10.0` away, and survives even if the `VERSION` file convention is ever dropped.
- **Notion should own**: everything it already owns per PM-supplied convention — the Task Log, Issues Log, Memory, and the *planning* side of the Version/Metrics log (what's queued, what's being decided, narrative context around a release) — none of which Git is a good tool for.
- **What changes, concretely, and what doesn't**: the `VERSION` file and its commit-message convention can stay exactly as-is (no reason to remove a working, if under-used, signal) — the only addition is tagging the same commit at the same moment `VERSION` is bumped and a deploy happens. This is small, reversible (a tag can be deleted), and directly closes the gap this mission measured, so it satisfies Section 8's "small, reversible, and necessary" bar for anything beyond pure recommendation — **but this mission does not create any tag itself**, since retroactively tagging the correct historical commits for the undocumented 1.6.0–1.9.1 gap requires a judgment call (which commit, if any, really was "1.6.0") that belongs to Diego/PM, not to this agent guessing from git log.
- **What future autonomous agents should treat as the source of truth**: once tags exist, **the nearest tag reachable from `HEAD` (or from the commit Render says is deployed) is the version**, full stop — not the `VERSION` file's current content (which can drift ahead of what's actually live, as it does today) and not a Notion page. Until tags exist, the honest answer is `[unknown]` — the `VERSION` file states an intent, not a confirmed live fact, and this report does not paper over that gap.
- **This satisfies the mission's stated priorities in order**: unambiguous agent interpretation (a tag is one unambiguous fact, not a file someone might forget to bump first); exact code/version traceability (a tag *is* a commit); reproducibility (works the same for any clone, with no Notion access required); low operational overhead (one `git tag` command added to an already-existing deploy ritual); minimal duplication (Notion keeps doing planning, Git keeps doing code identity — no new system to maintain); and compatibility with the existing workflow (nothing about the current Manual-Deploy-at-a-named-commit process needs to change).

**No migration is implemented.** This is a recommendation, exactly as the mission asks for; the smallest concrete next step (tag the current `HEAD`/`v1.10.0` commit as a starting point, and PM decides whether to attempt reconstructing the missing 1.6.0–1.9.1 history or start clean from here) is listed as P0/P1 below, not performed by this agent.

## Known Conflicts

1. **`verify_index_coverage.py` currently fails on the DB-free tree (advisory tier, non-blocking per PM ruling)** — one already-disclosed reason (the 15-stale-PK-table gap `parcelytics.md` names, the PM's basis for ruling this a documented false signal) and one newly-surfaced reason: **10 "REAL GAPS FOUND" index-coverage findings, not previously named anywhere this mission read, preserved here verbatim as `[unknown — requires live-mode triage]`.** These 10 findings are cases where a table has real indexes in `schema.sql` but none start with a column a query filters/joins by equality (`app.py:5167`'s `snapshot_totals` filter and similar findings against `parcel_tax_year`, `tax_billing`, `prop_unit`, and `parcel_2026_preliminary_snapshot`). See Verification Results. Not resolved by this mission — resolving whether these 10 are a real performance risk requires the live-mode (`--index-source live`) run this DB-free runner cannot perform.
2. **`verify_rollup_canonical.py` and `verify_tax_billing_rollup_canonical.py` currently fail**, apparently on false positives against other scripts' test fixtures. Not resolved, not silently hidden — excluded from both runner tiers with the reason documented in-file and here.
3. **The static classification heuristic under-counts real DB dependence** — four scripts (`loaders/check_geo_ids_in_pir_source.py`, `loaders/test_snapshot_2026_preliminary.py`, `test_parcel_rollup.py`, `verify_2026_prelim_vs_cert_reconciliation.py`) looked DB-free by keyword scan but fail at import time via a transitive, unguarded `import psycopg2` in `loaders/db.py`. This is a real gap between "looks DB-free" and "is DB-free" worth knowing before trusting any future static-only script inventory.
4. **`verify_county_scoping.py` and `verify_index_coverage.py` both contain DB-related keywords for optional flags but run DB-free by default** — the inverse problem, a static heuristic false positive in the other direction. Manual, measured confirmation was required to place each correctly — `verify_county_scoping.py` in the required tier, `verify_index_coverage.py` in advisory (per the PM ruling).
5. **The `VERSION` file and `CHANGELOG.md` disagree** — `VERSION` reads `1.10.0`; `CHANGELOG.md`'s newest entry is `1.5.0`. Already known (`parcelytics.md` §12); restated here because it's directly load-bearing for the version-management recommendation above.

## Unknowns

- Render's exact Start Command and worker configuration beyond what Diego's own dashboard read (relayed earlier in this PM thread) established. This mission did not re-verify it and has no way to. (The Python runtime version itself is now known — 3.14, per Render production tracebacks [PM-supplied 2026-09-01] — and is no longer listed here; see Runtime & Dependency Assessment.)
- Whether the 10 newly-surfaced `verify_index_coverage.py` "REAL GAPS" findings represent a genuine live performance risk or a schema-mode artifact — schema-mode index data can itself be incomplete or stale in ways this mission did not further investigate.
- Whether `verify_rollup_canonical.py`/`verify_tax_billing_rollup_canonical.py`'s failures are, in fact, false positives against test fixtures, or whether they're catching something real that merely looks like a fixture-string coincidence — this mission read the output but did not trace every fixture line to rule out the second possibility with certainty.
- The exact current contents of the four Notion logs (Task, Issues, Version/Metrics, Memory) — this agent has no Notion access; everything stated about them is `[PM-supplied]` convention, not inspected.
- Whether any commit between the `1.5.0` and `1.9.2` `VERSION`-bump commits was ever meant to represent an intermediate version number, or whether the jump was intentional.

## Recommendations

**P0 — required before further autonomous expansion**
- Have Diego push `CLAUDE.md`, `run_offline_checks.py`, and `.github/workflows/offline-checks.yml` (after review) and verify the first real CI run per the exact steps in the GitHub Actions section above — this report's "CI execution: NOT VERIFIED" needs to be closed out with a real, observed green run before anyone treats CI as working.
- Tag the current `HEAD` (or the commit `VERSION` `1.10.0` actually corresponds to) as `v1.10.0` in Git, and adopt "tag at deploy time" as a standing step in the existing deploy ritual going forward — the smallest concrete move toward the version-management recommendation above.

**P1 — important next improvements**
- Add a committed `runtime.txt` or `.python-version` stating the confirmed `3.14` — closes the last reproducibility gap this mission found (the version itself is now known, per PM-supplied Render-log evidence; only the repo-level declaration is still missing), at near-zero cost.
- Investigate the 10 `verify_index_coverage.py` "REAL GAPS FOUND" schema-mode findings against a live database (a human running `--index-source live`, per `parcelytics.md` §11), and decide whether the check should be promoted back to required once the underlying index/schema issues (or the 15-table PK migration) are addressed.
- Confirm or rule out whether `verify_rollup_canonical.py`/`verify_tax_billing_rollup_canonical.py`'s current failures are false positives, and either fix the scanner's own fixture-detection logic (a scanner-quality fix, not a "weakening verification" fix) or confirm a real gap exists.
- Add the fake-psycopg2 real-driver-guard pattern (already used by `loaders/explain_compute_metrics_passes.py`) to the four scripts that currently fail at import time for lack of a live driver, so sandboxed/CI logic-only runs of those specific scripts become possible.
- The queued `v1.11.0` CHANGELOG catch-up brief (already tracked separately in `parcelytics.md` §17) should proceed on its own — not folded into this mission, per the preamble.

**P2 — later optimization**
- Consider pinning `requirements.txt` to exact versions or adding a lockfile, once the team decides how much reproducibility overhead is worth the tradeoff against `>=`'s current flexibility.
- Consider whether the advisory tier should eventually be promoted, in whole or part, into CI as a non-blocking, informational job (separate from the required-tier gate).
- Clean up the six stale local branches, once someone confirms none of them contain work still worth cherry-picking.

## Proposed Mission 2

Not implemented — recommendation only. The natural next mission, given this foundation, is closing the P0 items above (the first verified CI run and the first real Git tag) plus extending the offline runner's required tier to cover `verify_county_scoping.py`'s and `verify_index_coverage.py`'s `--check-db`/`--index-source live` modes in a *separate*, clearly-labeled "live" tier that a human runs manually against a real database before any schema-sensitive change — since this mission's own measurement reconfirmed that schema-mode understates real coverage and cannot substitute for it, and that live-mode triage of `verify_index_coverage.py`'s 10 real-gap findings is still an open item.

---

*This report was written from direct, dated inspection and measured command execution in this session (2026-09-02), not from memory of prior sessions' claims. Every metric above is tagged per the convention at the top of this file.*
