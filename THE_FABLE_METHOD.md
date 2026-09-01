# THE FABLE METHOD — Standing Brief for the PM (PX-FABLE-METHOD-1)

**From:** Fable **To:** PM **Purpose:** Run first-pass reviews at Fable's
standard so only the genuinely hard 20% routes to Fable. This is not a style
guide — it is the actual procedure. Most of what makes a Fable review is
checklist discipline executed without exception; the rest is judgment, and
§7 tells you exactly when to escalate instead of exercising it.

---

## 1. The three laws (everything else derives from these)

1. **Verify, then approve — in that order, every time.** Never review from a
   summary, a screenshot description, or memory of an artifact. Fetch the
   page. Open the file. Recompute the number. A review of someone's account
   of the work is a review of the account, not the work.
2. **Evidence over narrative.** A plausible explanation is a hypothesis, not
   a finding. If a claim can be checked in under five minutes (a query, a
   grep, a fetch, a recomputation), checking it is mandatory — "it should be
   fine" is the phrase that precedes every incident this project has had.
3. **Conventions fail; structure doesn't.** Any rule that requires every
   future writer/author/session to independently remember it will eventually
   be forgotten. When you find a fix, ask: can this be enforced by schema,
   test, grep-check, or checklist instead of memory? If yes, the fix isn't
   done until the enforcement exists.

## 2. The review loop (run in this order, no skipping)

1. **Identify what kind of artifact this is** (marketing asset, spec, brief,
   Notion doc, code) — each has its own checklist below.
2. **Fetch the real thing** and any live surface it makes claims about.
3. **Run the mechanical checks** (§3–§5). Write findings as you go.
4. **Classify every finding:** MUST-FIX (blocks shipping) / VERIFY (needs a
   named person to confirm a named fact) / RECOMMENDED (improves, doesn't
   block). Never blur these — a soft "maybe fix" is a decision avoided.
5. **Single pass:** surface EVERYTHING in one response, including items
   beyond what was asked. Mark genuinely-open items as open. A second-round
   discovery that was findable in round one is a review failure.
6. **End with a verdict** ("approved," "approved with conditions 1–3,"
   "returned, blocked on X") and **specific credit** for what was done well.
   Credit is not politeness — it teaches what to repeat.

## 3. The numbers checklist (marketing and any figure-bearing artifact)

- **Recompute every displayed figure from every other displayed figure.**
  Subtractions close at displayed precision (A − B must equal the shown C).
  Percentages recompute from the shown numerator/denominator. Multi-part
  sums hit the shown total. Component percentages sum to ~100%.
- **When correctly-rounded figures genuinely can't close:** disclosure
  footnote ("computed from unrounded figures; displayed values rounded
  independently"), never a fudged number. When they CAN close by choosing
  display values differently, choose differently.
- **Conservation:** subsets never exceed their published total. Any group
  sum gets checked against the sealed county figure. If a sum exceeds
  canon → STOP THE LINE, escalate.
- **Vintage on everything:** every figure carries its year and source
  ("2025 billing," "2026 certified"). Tax rates and values have different
  latest-vintages — never assume they match.
- **Canon discipline:** published figures come only from sealed
  Published-Metrics-Log vintages. Dead figure families stay dead. A new
  number = full sealing process + one-week clock; a reused sealed number =
  byte-identical match check.
- **Confidence labels:** carried forward wherever the source figure bears
  one. Exact names, exact colors (Verified green / Preliminary blue /
  Partial amber / Estimated purple / Not Available slate). Check every chip.

## 4. The claims checklist (the error taxonomy — memorize this)

Every claim in an artifact is one of these types; each has a specific trap:

- **Endpoint vs. path.** "Up 5% since 2021" is an endpoint claim; "raised
  every year" is a path claim needing every intermediate data point. A
  chart of endpoints can NEVER support a path claim. (This one has failed
  in public-checkable ways — treat "every/always/never + time range" as an
  automatic verify.)
- **Instance vs. population.** One parcel's 45% is not "a typical bill."
  n=1 generalized to a class is a must-fix; scope the claim to the instance
  or get population data.
- **Average vs. universal.** "Averages ~10%" is not "sits within 10%." Any
  claim of the form "all X are Y" fails on one counterexample — find the
  counterexample class (ag parcels, MUDs, de minimis units) before approving.
- **Attribution vs. arithmetic.** "The gap" (market − taxable) is cap PLUS
  exemptions. Check that every causal label matches what was actually
  computed. When a headline attributes an effect, ask: what else is inside
  this number?
- **Coverage scope.** "Near you" / "any parcel" / "typical county X" to a
  general audience = overclaim unless the live market is named. The
  qualifier goes in the caption text, not only in slide fine print.
- **Claim vs. product.** Never approve marketing a detail the live product
  doesn't render. Fetch the module and look.
- **Statutory claims.** Cite-check every section number (the trigger, the
  procedure, and the election live in DIFFERENT sections). Deadlines are
  usually "the later of" two dates — flat dates overclaim. Weekend/holiday
  rollovers get a calendar check. De minimis and special-case carve-outs
  turn "must" into "most must."
- **Cross-surface consistency.** Post ↔ site ↔ prior posts must agree. Two
  Parcelytics surfaces disagreeing in public is the one unforgivable error.
  Also check the artifact against ITSELF (headline vs. its own slide 3).

## 5. The engineering checklist (specs, briefs, migrations, code)

- **Identity scope:** for any key column — who issues this identifier?
  County-issued → county-scoped by default; burden of proof is on
  globality. Text-typed keys, sized for the multi-county reality.
- **Single writer per table.** Tag-based cohabitation of two writers is a
  convention (see Law 3). Enumerate ALL writers (whole repo, including
  routes and shared helpers) before approving any write-path change.
- **No gate, no load.** New data type = unit grain + one canonical rollup +
  conservation gate + corruption-fixture alarm tests, shipped together.
  A safeguard that has never fired is a hope.
- **Pre-commit expectations.** Expected counts/sums/outcomes stated BEFORE
  the run, so surprising numbers can't be narrated into acceptability.
- **Diagnose from the symptom backward.** "Data missing" → first establish
  the last place the data verifiably existed, starting at rest and walking
  upstream. Don't chase the most interesting theory first.
- **Fix the class, not the instance.** Every bug fix ships with its
  recurrence-prevention (test, grep-check, constraint) or names why not.
- **One migration in flight per table family.** Fold co-located decisions
  (partitioning + re-keying) into one rebuild, never two.
- **Sequencing:** fixtures → full-file scan → canary → live. Each step is
  cheaper than discovering its failure class one step later.
- **Scanner invocation convention:** `verify_index_coverage.py
  --index-source live` is the one authoritative pre-commit command for any
  index- or tenant-scope-affecting change (PX-20260830-05 Task 5).
  `--index-source schema-sql` is offline-only (fixtures, demo runs) and
  UNDERSTATES coverage for the 15 tables schema.sql's own CREATE TABLE
  PRIMARY KEY text is confirmed stale for (see KNOWN_LIMITATIONS.md); a
  schema-sql-mode "no gap found" is never sufficient grounds to commit.
- **Shadow-swap county-derivation lint (PX-20260831-02 Task 2):**
  `verify_shadow_swap_county_derivation.py` is a required pre-commit check
  for any change touching a shadow-table-then-atomic-swap writer
  (structurally detected via the `DROP TABLE IF EXISTS X_shadow` +
  `RENAME TO` shape, not a filename list). Third real instance of the same
  bug class (compute_county_benchmarks, refresh_group_stats.py,
  refresh_snapshot_summary.py): a per-county aggregate whose county_code is
  externally stamped from a caller parameter instead of derived per-row
  from the aggregation query's own GROUP BY. Run it alongside
  `verify_index_coverage.py --index-source live` and
  `verify_county_scoping.py` -- all three are complementary, not
  redundant: index coverage checks WHERE-clause scoping, MC-2 checks
  INSERT/ON-CONFLICT/UPDATE-predicate scoping for plain writers, and this
  one checks the shadow-swap architecture's own distinct failure shape
  (a write-path function signature plus its feeding query's grouping
  clause).

## 6. Epistemics and voice (how findings are stated)

- **Tag every claim you make with its evidence class:** verified-live /
  recomputed / sourced (cite it) / reasoned / assumed. Never let an
  "assumed" wear a "verified" costume.
- **Name gaps; never smooth them.** "I could not check X because Y" beats a
  confident answer built around the gap. An honest disclosure may only be
  retired by direct evidence — and the retirement cites the evidence.
- **Concede fully when refuted.** No third hypothesis after two are dead.
  When your own earlier ruling caused a problem, say so with the causality
  named — the loop corrects in both directions or it doesn't correct.
- **Two claims about the same state cannot coexist** in one artifact
  ("draft — not reviewed" above a completed table). Stale self-description
  is a must-fix everywhere you find it.
- **Precision in prose:** "nearly 9x" when it's 9.12 is wrong direction;
  "roughly" is the honest word. Small, but this brand's thesis is that its
  behavior matches its claims — the prose is part of the data.
- **Deliverables are paste-able.** Exact replacement text, not "consider
  rewording." The person applying your fix should never have to interpret.

## 7. ESCALATE TO FABLE — the actual routing rule

You own: "does this artifact meet the standard?" Fable owns: "what is the
standard?" Escalate when the question is the second kind:

1. **New architecture or schema decisions** (new tables, key choices, write
   paths, partitioning, anything MC-1/MC-3-shaped) — always.
2. **Anything that changes or contradicts published canon figures**, or
   supersedes a published claim — always, before it ships.
3. **First instance of any new pattern** (new pillar, new claim type, new
   data source class, new county's first artifact of a kind). Second and
   later instances are yours, using the first as precedent.
4. **Legal-adjacent judgment** (license/terms interpretation, §25.025,
   anything where "we're not lawyers" applies) — Fable frames it
   conservatively; neither of us concludes it.
5. **Cross-county semantics** — any claim comparing counties, or any
   decision where Travis and another county could silently diverge.
6. **Your checks pass but something feels off.** A passing checklist with
   residual unease is precisely the 20% the escalation path exists for.
   Say what feels off, even if you can't name why.
7. **Anything where the fix would delete an honest disclosure, weaken a
   gate, or trade rigor for speed** — always.

Everything else — arithmetic closure, claim taxonomy, label discipline,
statutory cite-checks against known rulings, format compliance, fixture
verification, Notion hygiene — is yours, and this document is the standard
you hold it to. When you approve, say "reviewed per PX-FABLE-METHOD-1" so
Diego knows which gate it passed.

## 8. Anti-patterns (the complete list of what causes Fable must-fixes)

Reviewing from a summary · trusting a chart to prove a path claim ·
generalizing n=1 · confusing average with universal · attributing a
composite to one cause · shipping a figure without vintage · a subset
exceeding its total · forcing closure by mis-rounding · two contradictory
state claims on one page · fixing the wrong location (body vs. field) ·
fixing the instance without the class · a safeguard with no test proving it
fires · a scoped audit sold as comprehensive · retiring a disclosure
without citing the closing evidence · "should be fine."

---

*Amendment protocol: this brief changes the way the standards docs change —
reviewed revision, dated changelog, incident named. When a PM-passed
artifact later fails a Fable spot-check, the miss gets added to §8 by name.*
