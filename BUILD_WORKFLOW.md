# Parcelytics Build Workflow

How Parcelytics actually gets built and shipped — updated July 17, 2026, now that the site is live in production and every push carries real weight.

## The loop

1. **Task identified** — from the backlog, a live issue, or a Fable/Marketing Director review
2. **Claude writes a brief** — investigate-before-fix, copy-paste ready for Cowork
3. **Diego pastes the brief to Cowork**
4. **Cowork investigates and builds** — sandbox-verified, reports back with diffs and honest disclosure of anything it couldn't verify (e.g. no live DB/network access in its sandbox)
5. **Diego pastes Cowork's results back to Claude**
6. **Claude reviews critically** — checks the actual diff, confirms a real test ran (not just a claim), catches issues before they go further. Loops back to step 2 if something real is found.
7. **Diego runs live verification locally** — restart the server, check the browser, run real psql queries against real data
8. **Claude reviews the live results** — loops back to step 2 if problems show up
9. **Diego commits and pushes to GitHub** — this updates the code repository. With Auto-Deploy off, it does NOT go live yet.
10. **Claude recommends a version number** — MAJOR/MINOR/PATCH, with reasoning, based on what the change actually is
11. **Diego manually triggers Deploy on Render** — the real "go live" moment, deliberate and separate from the push. This is the review gate.
12. **Quick live-site check** — confirm parcelytics.onrender.com (or parcelytics.ai once verified) actually matches what was checked locally, especially for anything touching the database connection, environment variables, or other things that can behave differently in production
13. **Loop back to step 1**

## Two inputs feed step 1

- **Fable** — periodic full-site outside review
- **Marketing Director persona** — same brief-then-review pattern; Claude reviews for factual accuracy but never drafts the creative content itself

## Why the extra steps after the push

Before the site was live, a push just updated a file in a repository — low stakes either way. Now a push can reach real visitors the moment it's live, so the process needed a genuine pause between "the code is ready" and "the code is public," not just a habit of being careful. Turning off Render's Auto-Deploy setting is what makes that pause real and structural, not just something everyone remembers to do.

## Versioning

Semantic versioning (MAJOR.MINOR.PATCH), tracked in `CHANGELOG.md` and mirrored on the Notion Version Log. V1.0.0 was the first public release (July 17, 2026). Version bumps are tied to actual production deploys, not every commit.

## Parcel-Filtering & Public-Number Safeguards

**Origin:** July 2026 incident — a LinkedIn post published county-total dollar
figures that were ~20-25% below the real TCAD certified/preliminary totals.
Root cause: a NULL-propagation bug in a parcel-exclusion filter
(`state_cd1 NOT LIKE 'X%'` silently drops rows where `state_cd1 IS NULL`),
independently re-implemented (not shared) across 4+ query call sites, one of
which had already drifted out of sync with the others undetected. This is the
same failure class as the earlier sparse-column-read-as-if-universal bug
family — recurring in a new subsystem because the earlier fix wasn't
generalized into a project-wide rule.

### Rule 1 — One filter, never copied
Any query that filters or excludes parcels (by state_cd1, geo_id prefix, or
similar) must reference a single shared, NULL-safe predicate — never a
re-typed copy. `COALESCE(column, '') NOT LIKE 'X%'`, not
`column NOT LIKE 'X%'` alone. A regression test asserts every parcel-exclusion
fragment in the codebase matches (or literally imports) the canonical
predicate, so drift fails loudly instead of silently.

### Rule 2 — Reconciliation check on every certified/preliminary ingestion
Whenever a new certified or preliminary totals report is ingested, compare
the platform's own computed aggregate(s) against the source report's real
total(s). Flag anything outside a tolerance band for human review before use;
pass anything within it silently. Tolerance exists deliberately — normal
appraisal-cycle movement (protests, corrections) is expected and should never
trigger a false alarm. Starting band: ±5-8%, to be tuned as more certification
cycles provide real calibration data. A gap of the size in this incident
(~20-25%) should always flag.

### Rule 3 — Public numbers come from a verified reference, not a live page
Any number leaving the platform — marketing posts, screenshots, press,
external reports — must be pulled from a small "verified stats" reference
file/table, updated only immediately after a Rule 2 check passes. Never
copy a number directly off a live page for external use. This closes the gap
between "matches what the site currently displays" and "matches ground
truth" — the exact gap that let this incident happen even with a genuine
accuracy-check request already in the review loop.

### Scope note
Real-property-only is the deliberate, documented policy for all public
county-total figures going forward (decided July 2026) — Business Personal
Property is excluded by explicit choice, matching the platform's existing
"real-property parcels only" framing everywhere else, not as an accidental
side effect of an unrelated filter.
