# tax_billing / tax_billing_entity — Promoted Collision-Resolution + Partitioning Brief

**Status:** DESIGN-ONLY, NOT IMPLEMENTED. No schema change, migration script, or
live database action results from this document. Promotes and expands an
already-pending, previously-flagged brief (see "Origin" below) per
`DALLAS-GATE-5` Part 2, itself implementing Fable's `HARRIS-SCALE-1`
architectural review's "one migration, not three" sequencing recommendation.
Actually implementing native partitioning — if that ends up being the real,
chosen path once this document's own open questions are answered — remains
its own, separate, future migration brief, per `DALLAS-GATE-5`'s explicit
scope boundary. Nothing here executes against production.

## 0. Origin — what this promotes, and why now

`SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §3.7 defined Migration Step M0: a pure,
deterministic file scan (no DB access) to measure whether `tax_billing`
(keyed `geo_id = PARCEL[:10]`, `ON CONFLICT (geo_id, tax_year) DO UPDATE`, at
the time that spec was written) suffers the same last-write-wins collision
loss the unit-model review found in the appraisal data. §3.7's own decision
rule: zero collision groups means billing is genuinely account-grained,
nothing to do; nonzero means real, measurable loss, and **"that becomes its
own brief... do not bundle into this implementation."** §7 confirms:
"tax_billing re-keying, if and only if M0 finds collisions" is explicitly out
of scope for the unit-model build.

M0 ran. Per `KNOWN_LIMITATIONS.md`'s own recorded result:

| Scope | Rows read | Distinct 14-digit `PARCEL` | Distinct 10-char prefixes | Collision groups | Largest group | Combined `TOTAL_TAX` across collision-group prefixes |
|---|---:|---:|---:|---:|---:|---:|
| **2025 only** | 493,521 (35,981 skipped, other years) | 449,548 | 426,491 | **3,384** | 1,210 accounts on prefix `0259410216` | **$5,794,968.90** |
| All tax years in file | 493,521 | 452,049 | 427,037 | 3,694 | 1,278 accounts on prefix `0259410216` | $11,722,987.90 |

**Result: CONFIRMED — nonzero.** Per §3.7's own decision rule, this became a
real, pending future brief. It has sat pending, unactioned, since the
original unit-model work. This document is that brief — promoted now (not
re-derived; the numbers above are the same real M0 result, unchanged) and
expanded per Fable's own sequencing recommendation to also carry the native-
partitioning decision and the transitional-index history for this same
table, since all three would otherwise touch this large, still-growing table
in three separate future migrations instead of one.

**Current-state check performed before writing this document** (per
`DALLAS-GATE-5`'s own instruction to investigate before designing): confirmed
via direct inspection of `loaders/load_tax_current.py`'s real, current
`billing_sql` (as of this session's own `DALLAS-GATE-4` fix) that the keying
grain is unchanged since M0 was measured — still `ON CONFLICT (county_code,
geo_id, tax_year) DO UPDATE`. `DALLAS-GATE-4` correctly scoped this by
`county_code` and fixed the `ON CONFLICT` target to match the live, migrated
schema, but did **not** touch the underlying keying grain — that was
explicitly out of scope for that brief, same as it was for the original
unit-model build. **The M0 collision finding is fully live and unresolved
today**, not stale.

## 1. A real naming precision this document corrects before designing anything

`DALLAS-GATE-5`'s own brief text (and this session's Task Log entries)
describe the pending item as "the tax_billing_entity collision-resolution
brief." Per the real record above, **the confirmed, measured M0 finding is
on `tax_billing`, not `tax_billing_entity`.** `tax_billing_entity` was never
separately scanned by M0 — §3.7's own text and the M0 script
(`investigate_taxcur_m0_collision.py`) both operate on `TaxCurOpenData`'s
`PARCEL`/`TOTAL_TAX` columns, which is `tax_billing`'s own source, not
`tax_billing_entity`'s per-entity breakdown.

This is not a reason to assume `tax_billing_entity` is safe. It shares the
identical mechanism structurally: `load_tax_current.py`'s `entity_sql` truncates
the same 14-digit `PARCEL` to the same 10-char `geo_id` prefix, keyed
`ON CONFLICT (county_code, geo_id, tax_year, entity_code)`. Any 14-digit
account collision that loses a `tax_billing` row the same way loses that
account's `tax_billing_entity` rows too, unless a colliding account happens
to carry different `entity_code`s than the account that overwrote it (in
which case the entity rows would coexist under the same geo_id/tax_year,
partially masking the loss at the entity grain while `tax_billing`'s single
aggregate row is still last-write-wins lossy). This is a real, mechanistic
argument for treating both tables as sharing the risk — not a confirmed
dollar figure for `tax_billing_entity` specifically.

**Recommendation carried into this brief's scope — now DONE (M0-EXTENSION-1,
2026-08-16), real numbers recorded, not re-derived here:** the recommended
extension of `investigate_taxcur_m0_collision.py` has been built and run —
`investigate_taxcur_m0_entity_collision.py` (repo root, gitignored the same
way) — against the real, current `TaxCurOpenData (1).csv`. The
`tax_billing_entity` numbers do NOT mirror `tax_billing`'s 3,384/$5.79M, as
this section originally cautioned they might not: they are real, larger, and
meaningfully different in kind, not just magnitude. Full results, the
concentration analysis, and the two-figure dollar-exposure methodology (a
"mirrored M0" figure vs. the decision-relevant real last-write-wins loss
figure, kept deliberately separate) are recorded in
`KNOWN_LIMITATIONS.md`'s own new entry, "`tax_billing_entity` has its own,
larger real collision loss" (M0-EXTENSION-1), immediately following the
original `tax_billing` M0 entry. Headline figures, for reference without
re-reading that entry: 14,989 real entity-grain collision pairs (2025-only,
3,148 of the 3,384 prefix groups carry a real entity-level collision, 236
are pure coexist), real last-write-wins loss $170,061,400.28 (106,297
overwrite events) — roughly 29x `tax_billing`'s own $5,794,968.90 figure,
confirmed driven by shared taxing entities compounding across large
multi-unit collision groups, not a measurement discrepancy. Whoever picks up
§2's actual re-key/partitioning design work below has these real numbers in
hand and does not need to re-run or re-derive this measurement.

## 2. Combined scope, per Fable's "one migration, not three"

Three real, separately-flagged pieces of pending work all touch the same
physical table(s) (`tax_billing`, `tax_billing_entity`) and, left separate,
would migrate this large and still-growing table three times as it keeps
growing (Dallas onboarding, then Harris, then whenever partitioning was
eventually revisited). Resolved together, once, before Dallas rows exist:

### 2a. The collision fix itself (M0's own recommendation)

Per `KNOWN_LIMITATIONS.md`'s own recorded decision text: "likely re-keying
`tax_billing` by the full 14-digit account with a `geo_id` column, and
summing to account level for display, mirroring the unit-layer approach
chosen for the appraisal side." This is the real precedent already proven in
this codebase — `prop_unit`/`prop_unit_tax_year` were built exactly this way
when the same class of collision was found in appraisal data
(`SPEC_UNIT_MODEL_AND_INGEST_GATE.md` §3.2 onward). The design question this
brief raises but does not resolve: does `tax_billing_entity` need the same
treatment (pending the §1 measurement above), and does the account-level
"sum to geo_id for display" step live in a view, a materialized summary, or
application-layer aggregation — each has real, different implications for
every read site currently querying `tax_billing`/`tax_billing_entity`
directly (this session's own `DALLAS-GATE-3`/`-4` work touched several of
them). Not decided here.

### 2b. The native-partitioning decision for `tax_billing_entity`

Per this document's own `SPEC_COUNTY_PARTITIONING.md` §1.3 amendment (written
alongside this brief, same session): `tax_billing_entity`'s 20-30M-row
native-partitioning revisit trigger now projects to fire at Dallas onboarding
itself (~27-28M rows), not "sometime before Harris." Real, current sizing:
10,770,184 rows, 1494 MB total (701 MB table + 793 MB index), ~145
bytes/row — projecting to roughly 3.9-4GB for this one table alone at the
~27-28M-row mark. If `tax_billing_entity` is going to be re-keyed anyway per
2a (a real schema change, not a cosmetic one), this is the natural, and per
Fable's own recommendation, correct point to also make the lightweight-vs-
native-partitioning decision for this specific table — not necessarily
reaching a different conclusion than `SPEC_COUNTY_PARTITIONING.md` §4.1's
schema-wide lightweight recommendation, but making that decision **for this
table specifically**, with this table's own real, now-corrected projected
scale in hand, rather than inheriting the schema-wide default by omission.
**Not decided here** — this document identifies the decision point and its
real inputs, not the answer. See §3 below for the honest uncertainty this
decision needs to account for.

### 2c. The transitional geo_id-leading / entity_code-leading index history

`schema.sql`'s own record (the "Real, urgent hotfix, Aug 8, 2026" and "Real,
second wave of transitional indexes, Aug 15, 2026" comment blocks) shows this
table's neighbors already went through one real, live incident from the
`county_code`-leading PK migration: `idx_billing_geo` (on `tax_billing`) and
`idx_tbe_entity_code` (on `tax_billing_entity`) both exist today specifically
because a bare `geo_id`/`entity_code`-only query lost its fast lookup path
once `county_code` became the leading PK column — both are explicitly marked
**transitional, meant to be dropped** once the resolver seam is fully wired
and a coverage audit reports zero county-unscoped queries (per
`SPEC_COUNTY_PARTITIONING.md`'s own hard policy, restated in its §10.4 Dallas
gate). A re-keying migration on these same two tables is exactly the moment
to also retire or redesign these transitional indexes deliberately, rather
than let them survive by inertia into a newly re-keyed table's index set, or
forget them entirely during the re-key and silently reintroduce the same
gap. **Not decided here** — flagged as a real, concrete checklist item for
whoever designs the actual migration.

## 3. Real, honest uncertainty this document preserves, not resolves

The ~39.5M-row Harris-alone extrapolation (from `HARRIS-ONBOARD-1`) and the
~27-28M-row Dallas-onboarding projection (from this session's
`SPEC_COUNTY_PARTITIONING.md` §1.3 amendment) are both **ratio
extrapolations from Travis's own real, measured entities-per-parcel density**,
not real measurements of Dallas's or Harris's own data — neither county has a
single real row loaded yet. Fable's own review specifically flagged that
Harris's real, known MUD/utility-district density likely makes its true
entities-per-parcel ratio **higher** than Travis's, meaning the ~39.5M figure
is more plausibly a floor than a ceiling. This document does not round either
number into false precision, and any migration design built from this
document should carry the same explicit uncertainty forward rather than
treating ~27-28M or ~39.5M as confirmed targets to engineer exactly against.

## 4. Real, separate housekeeping note, not part of this brief's scope

Not part of this document's own scope, but surfaced during this session's
sizing work and worth a name so it isn't lost: the `*_old_pre_partition`
backup tables from the original `county_code` migration are still present in
production, consuming 3GB+ combined, never cleaned up. Real, not urgent —
tracked separately, not resolved by this document.

## 5. What this document does NOT do

- Does not choose a re-keying schema for `tax_billing`/`tax_billing_entity`.
- Does not choose native partitioning vs. continuing the lightweight design
  for `tax_billing_entity` specifically.
- Does not design or run the `tax_billing_entity` collision measurement §1
  recommends.
- Does not touch any code, schema, or live database.
- Does not set a date or sequence this ahead of or behind other open Dallas
  prerequisites (`DALLAS-ONBOARD-1`'s three documents, the still-open
  `BILLING-DIAG-5` Sentry mystery, Dallas's own sample-export acquisition) —
  that prioritization call is Diego's.

**Real, recommended next step:** a dedicated future brief (not this one) that
(a) runs the `tax_billing_entity` collision measurement per §1, (b) then
designs the combined re-key + partitioning-decision + transitional-index
resolution per §2, informed by that real measurement rather than the
mechanistic-but-unmeasured argument this document makes. This document's own
job was narrower and is complete: promote the pending M0 brief, state its
real current status, and combine its scope with the two other real findings
this session surfaced, per Fable's own instruction — not to design or
execute the fix itself.
