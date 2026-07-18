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
