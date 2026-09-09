# CLAUDE.md — Operational Entry Point

**Read `parcelytics.md` first.** This file is a thin map and a set of pointers into it — not a second copy of its rules. Where anything here seems to conflict with `parcelytics.md`, `parcelytics.md` wins; report the conflict rather than resolving it yourself [document].

## Identity, in three lines

Parcelytics is a multi-county Texas property-tax intelligence platform for CRE investors, acquisitions analysts, and consultants, built as a single-file Flask app over raw-SQL PostgreSQL, with both counties fully live in production (Travis; Dallas live since 2026-08-28, metrics as of 2026-09-01 [PM-supplied]) — Dallas is mid-field-completion (four verified-empty fields), not mid-onboarding. → `parcelytics.md` §1–§2.

## Repository map

- `app.py` — the entire Flask application: ~8,900 lines, 35 routes, no blueprints. → `parcelytics.md` §2.
- `loaders/` — per-county, per-source ingestion modules, the rollup/gate machinery (`ingest_gate.py`, `parcel_rollup.py`), and most of the loader-level test suite.
- `templates/`, `static/` — Jinja2 templates and frontend assets (incl. D3 visualizations).
- `tax_logic/` — shared tax-domain logic (classification, Texas-specific rules); grows by new module, never by county `if`-branches. → `parcelytics.md` §9.
- `verify_*.py`, `test_*.py`, `explain_*.py` (repo root and `loaders/`) — the scanner/fixture/EXPLAIN-proof suite. → `parcelytics.md` §11; full classification in `PX_ENGINEERING_FOUNDATION_M1_REPORT.md`'s Verification Inventory.
- `schema.sql` — declared schema; **known stale** for 15 tables' PRIMARY KEY text. Production is truth, not this file. → `parcelytics.md` §3.
- `config.py` — environment/secrets resolution, `DATABASE_URL`, vault paths.
- `run_offline_checks.py` — **the offline-runner command** (see below).
- `parcelytics.md`, `CLAUDE.md`, `BUILD_WORKFLOW.md`, `THE_FABLE_METHOD.md`, `MULTI_COUNTY_ONBOARDING_STANDARDS.md`, `DATA_LIFECYCLE.md`, `KNOWN_LIMITATIONS.md`, `CHANGELOG.md`, `VERSION`, plus the `SPEC_*.md` and `RUNBOOK_*.md` files — the doc set. `parcelytics.md` is the index; read it to find which of these governs a given question.
- `task_staging/` — scratch/in-flight work; not a source of truth for anything.
- Large raw data files (`.csv`, `.zip`, county exports) that appear in the working tree are `.gitignore`d scratch, not tracked deliverables — check `git ls-files` before treating one as authoritative.

## Source-of-truth hierarchy

| Question | Authoritative source |
|---|---|
| Engineering rules, architecture, boundaries | `parcelytics.md` (this repo) |
| What's actually true about the database schema | Production Postgres itself, not `schema.sql` — → `parcelytics.md` §3 |
| Engineering task queue / what's next | The Notion Task Log [PM-supplied] + `parcelytics.md` §17 (expected to change often) |
| Code and change history | Git, this repository |
| Software version / release identity | **Unresolved as of this mission** — see `PX_ENGINEERING_FOUNDATION_M1_REPORT.md`'s Version/Release Management Recommendation. Do not assume Git tags, `VERSION`, or Notion is authoritative until that's ruled on. |
| Production data | The live production database — never read/written directly by an agent without Diego. → `parcelytics.md` §14–§15 |
| Raw county source files | The external-drive Raw Vault [PM-supplied] — currently single-drive, no offsite backup. → `parcelytics.md` §16 |
| Project/planning state (Issues, Memory, Vintage Ledger) | Notion [PM-supplied] — this agent has no Notion access; treat anything stated about Notion's contents as reported, not independently verified |

## The offline-runner command

```
python3 run_offline_checks.py
```

Runs the required, DB-free pre-commit tier (`parcelytics.md` §11's named recurring set, restricted to the scripts confirmed to run without a live database). No database, no network, no vault, no production credentials. Exit 0 = required tier green; nonzero = stop, do not commit. `--advisory` runs a larger non-blocking tier; `--seed-failure` proves the exit code plumbing with a synthetic failing check. Full behavior, current pass/fail state, and why it is honest about the two things it does **not** catch (live-mode-only index findings; anything needing a real database) are in `PX_ENGINEERING_FOUNDATION_M1_REPORT.md`.

**This is not a substitute for the full pre-commit set.** `verify_index_coverage.py --index-source live` and any DB-touching check still require a human with real database access to run directly — see `parcelytics.md` §11.

## Autonomy boundaries — what you can do without asking

Full list, with the escalation triggers, is `parcelytics.md` §15. In short: read anything, build/test in your own sandbox, run read-only diagnostics, draft docs — freely. Any production write, any commit/push, any deploy, any credential action, any spend, or publishing any figure externally requires Diego. Any new architecture/schema decision, anything touching a canon figure, the first instance of a new pattern, cross-county semantics, or "checks pass but something feels off" requires a PM ruling before you proceed — do not resolve it yourself.

## County data — the one rule that matters most

Never infer, guess, or backfill a value a county doesn't actually publish. Sourced → show it. Not published → its field is absent, not dashed, not estimated. → `parcelytics.md` §7 (the current 30% coverage rule, with its open denominator question explicitly unresolved — do not resolve it) and §10 (provenance).

## G1–G6 (ingestion gates) — current status

Real, implemented, and run for Travis (full G1–G6 battery). Dallas runs a different, partial battery (its own G1/G2-analogs plus a field-coverage check; no G4/G5/G6 yet) — this is a disclosed, open gap, not a bug to fix opportunistically. **A failing gate currently does not produce a nonzero process exit code anywhere in the pipeline** — a real, open enforcement gap. → `parcelytics.md` §5.

## Known sandbox/live-environment hazards

- No `psycopg2` driver and no live database exist in most agent sandboxes — several scripts fail at import time for this reason alone (not because they need a live connection for their real logic). Confirmed 2026-09-02 — see the M1 report's Verification Results.
- `app.py`'s own in-code comments about gunicorn worker count have been shown stale against a direct Render dashboard read — do not trust an in-code comment over a fresh, dated, attributed check. → `parcelytics.md` §2.
- Fixing a "Travis-shaped assumption" without a structural guard is how the same bug (missing `county_code` scoping) got independently rediscovered three times. → `parcelytics.md` §9.
- Evidence tags are mandatory for any metric/state claim in a report you write: `[file:line]`, `[measured YYYY-MM-DD, artifact]`, `[dashboard, YYYY-MM-DD]`, `[document]`, `[unknown]`. Never cite a source you cannot reach (e.g. Render's dashboard, Notion, a live database) as something you personally checked.

## Expected workflow for a bounded change

1. Read `parcelytics.md`, then the specific spec/runbook that governs the area you're touching.
2. Inspect the actual code/data before writing anything — do not assume a doc is current.
3. Build and verify in your own sandbox; run `python3 run_offline_checks.py` before proposing anything as done.
4. Write files to the working tree. **You do not commit or push** — Diego does, after PM review. Do not commit/push if repository state, scope, or authority is unclear; stop and report instead.
5. Disclose every conflict you find between docs and code, and every unknown — do not silently reconcile or silently guess.

## Version / release management

Unresolved as a standing decision as of this mission — `VERSION` (currently `1.10.0`) and `CHANGELOG.md` are known to be out of sync (`parcelytics.md` §12), and whether Git tags, `VERSION`, or Notion should be authoritative going forward has not been ruled on. See `PX_ENGINEERING_FOUNDATION_M1_REPORT.md` for the recommendation; do not treat any one of these as authoritative until Diego/PM rule on it.
