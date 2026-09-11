# PX Mission 4 — Live Measurement Commands (for Diego)

Per the Mission 4 preamble item 2: Cowork has no database, network, or credential access. Every command below is prepared, code-reviewed-ready SQL/shell text — nothing here has been executed. Run these yourself, in order, and report the results back so they can be folded into `PX_CAPABILITY_CONTRACT_M4_REPORT.md`'s `[PENDING — live execution]` sections.

## Step 0 — create the table (one-time, production schema change)

This is a new table addition (`schema.sql`'s `county_field_coverage`, appended this mission) — a production schema migration, which per `parcelytics.md` §14/Mission 4 §14 requires you to run it, not Cowork.

```bash
psql "$DATABASE_URL" -c "$(sed -n '/CREATE TABLE IF NOT EXISTS county_field_coverage/,/^);/p' schema.sql)"
```

Or simply re-run `schema.sql`'s `execute_schema()` bootstrap path if that's your normal convention — `CREATE TABLE IF NOT EXISTS` makes this idempotent either way.

## Step 1 — dry-run measurement (read-only, safe to run any time)

```bash
python3 loaders/field_coverage_gate.py --county TRAVIS --tax-year 2026
python3 loaders/field_coverage_gate.py --county DALLAS --tax-year 2026
```

This prints every registered field's measured coverage for each county. Nothing is written (`--dry-run` is the default). Please paste the full output back — that becomes D6/D10's real, live-measured Travis/Dallas results, replacing this mission's fixture-based demonstration (`loaders/demonstrate_capability_m4.py`).

## Step 2 — persist the measurement (production write)

Only after reviewing Step 1's output. This writes to `county_field_coverage` and requires `assert_production_db()` to pass (refuses to write if `DATABASE_URL` isn't actually pointed at production):

```bash
python3 loaders/field_coverage_gate.py --county TRAVIS --tax-year 2026 --live
python3 loaders/field_coverage_gate.py --county DALLAS --tax-year 2026 --live
```

## Step 3 — spot-check a few fields directly (optional, for your own sanity-check before trusting the gate's own output)

```sql
-- Dallas classi_cd (the brief's own proof case — expect 0 populated)
SELECT COUNT(*) AS population,
       COUNT(*) FILTER (WHERE NULLIF(TRIM(classi_cd::text), '') IS NOT NULL) AS populated
FROM parcel WHERE county_code = 'DALLAS' AND prop_type_cd = 'R';

-- Travis state_cd1 (expect roughly 500,439 / 517,614 per KNOWN_LIMITATIONS.md)
SELECT COUNT(*) AS population,
       COUNT(*) FILTER (WHERE NULLIF(TRIM(state_cd1::text), '') IS NOT NULL) AS populated
FROM parcel WHERE county_code = 'TRAVIS';

-- Confirm the table wrote what you expect
SELECT * FROM county_field_coverage ORDER BY county_code, field;
```

## Step 4 — report back

Please send back (or paste into chat): Step 1's full console output for both counties, and Step 3's three query results. That's everything needed to complete `PX_CAPABILITY_CONTRACT_M4_REPORT.md`'s Travis/Dallas results sections with real numbers instead of `[PENDING — live execution]`.

No other production action (index changes, ANALYZE, autovacuum, Render config, anything outside this table and these two scripts) is requested or expected as part of this — see the Mission 4 brief §15/§16's explicit scope fence.
