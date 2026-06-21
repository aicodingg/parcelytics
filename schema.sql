-- Travis County Property Tax Platform — Phase 1 Schema
-- PostgreSQL 14+

CREATE TABLE IF NOT EXISTS parcel (
    geo_id          VARCHAR(20)  PRIMARY KEY,   -- TCAD 10-char long account (e.g. 0100030105)
    prop_id         BIGINT,                      -- TCAD short integer ID
    prop_type_cd    VARCHAR(5),                  -- R=Real, P=Personal, MH=Mobile Home, MN=Mineral
    situs_address   TEXT,
    legal_desc      TEXT,
    neighborhood_cd VARCHAR(20),
    state_cd1       VARCHAR(10),                 -- PTD state property code (e.g. A, F1, B)
    state_cd2       VARCHAR(10),
    owner_id        BIGINT,
    owner_name      TEXT,                        -- current owner (from 2025 Certified or TaxCur)
    zip_code        VARCHAR(10),
    latitude        NUMERIC(12,9),
    longitude       NUMERIC(12,9)
);

CREATE TABLE IF NOT EXISTS parcel_tax_year (
    geo_id          VARCHAR(20)  NOT NULL,
    tax_year        SMALLINT     NOT NULL,
    market_value    BIGINT,
    assessed_value  BIGINT,                      -- market minus HS cap loss
    taxable_value   BIGINT,                      -- assessed minus entity exemptions (TCO entity used)
    hs_cap_loss     BIGINT,
    land_value      BIGINT,                      -- 2025 Certified only
    imprv_value     BIGINT,                      -- 2025 Certified only
    exemption_codes TEXT,                        -- comma-separated codes (HS, OV65, DP, DV, etc.)
    data_source     VARCHAR(20),                 -- 'ajr' or 'certified'
    PRIMARY KEY (geo_id, tax_year)
);

-- Current-year tax office billing (TaxCurOpenData — 2025 only in supplied data)
CREATE TABLE IF NOT EXISTS tax_billing (
    geo_id              VARCHAR(20)  NOT NULL,
    tax_year            SMALLINT     NOT NULL,
    billing_num         VARCHAR(30),
    owner_name          TEXT,
    total_tax           NUMERIC(14,2),
    total_paid          NUMERIC(14,2),
    total_due           NUMERIC(14,2),
    is_delinquent       BOOLEAN      DEFAULT FALSE,
    first_delinquent_yr SMALLINT,
    cause_number        VARCHAR(50),
    exemption_codes     VARCHAR(50),
    PRIMARY KEY (geo_id, tax_year)
);

-- Per-entity billing detail (extracted from TaxCurOpenData entity columns)
CREATE TABLE IF NOT EXISTS tax_billing_entity (
    geo_id       VARCHAR(20) NOT NULL,
    tax_year     SMALLINT    NOT NULL,
    entity_code  VARCHAR(10) NOT NULL,
    amount_due   NUMERIC(14,2),
    amount_paid  NUMERIC(14,2),
    PRIMARY KEY (geo_id, tax_year, entity_code)
);

-- Delinquent accounts (TaxDelqOpenData)
CREATE TABLE IF NOT EXISTS tax_delinquent (
    geo_id              VARCHAR(20)  PRIMARY KEY,
    tax_year            SMALLINT,
    delinquent_total    NUMERIC(14,2),
    current_year_total  NUMERIC(14,2),
    total_due           NUMERIC(14,2),
    first_delinquent_yr SMALLINT,
    cause_number        VARCHAR(50),
    judgement_date      DATE,
    bankruptcy_number   VARCHAR(50)
);

-- Tax rates by entity and year (2025RatesHistory1990-2025.xlsx)
CREATE TABLE IF NOT EXISTS county_tax_rate (
    entity_code  VARCHAR(10) NOT NULL,
    entity_name  VARCHAR(100),
    tax_year     SMALLINT    NOT NULL,
    rate         NUMERIC(8,6),
    PRIMARY KEY (entity_code, tax_year)
);

-- Migrate column types if tables were created with old definitions
DO $$ BEGIN
  ALTER TABLE parcel ALTER COLUMN prop_id TYPE BIGINT;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
DO $$ BEGIN
  ALTER TABLE parcel ALTER COLUMN geo_id TYPE VARCHAR(20);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
DO $$ BEGIN
  ALTER TABLE parcel_tax_year ALTER COLUMN geo_id TYPE VARCHAR(20);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
DO $$ BEGIN
  ALTER TABLE tax_billing ALTER COLUMN geo_id TYPE VARCHAR(20);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
DO $$ BEGIN
  ALTER TABLE tax_billing_entity ALTER COLUMN geo_id TYPE VARCHAR(20);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
DO $$ BEGIN
  ALTER TABLE tax_delinquent ALTER COLUMN geo_id TYPE VARCHAR(20);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

-- ============================================================
-- PHASE 2 — Computed insight layer
-- ============================================================

-- parcel_metrics: one row per parcel × year, computed values only.
-- Source data (parcel_tax_year) is never modified — this table is fully derived.
-- Refreshed by compute_metrics.py after each data load.
--
-- Confidence levels (per Part 2 Data Integrity Standard):
--   coverage_level = 'full'        → 2025: market + assessed + real billing data (Verified)
--   coverage_level = 'value_only'  → 2021–2024: market + assessed only; tax fields are NOT AVAILABLE
--
-- Fields that are NULL on a 'value_only' row are NOT AVAILABLE, never zero.
-- has_tax_data mirrors coverage_level as a boolean for easy querying.
CREATE TABLE IF NOT EXISTS parcel_metrics (
    geo_id                       VARCHAR(20)  NOT NULL REFERENCES parcel(geo_id),
    tax_year                     SMALLINT     NOT NULL,

    -- Coverage / confidence
    coverage_level               VARCHAR(20)  NOT NULL,   -- 'full' | 'value_only'
    has_tax_data                 BOOLEAN      NOT NULL,   -- TRUE only for 2025

    -- Year-over-year changes (NULL when prior year missing or zero)
    -- NUMERIC(15,4): AJR source data contains extreme outliers (e.g. 751,858,200% YoY)
    -- that overflow NUMERIC(9,4); NUMERIC(15,4) handles up to ~10^11 safely.
    yoy_market_value_pct         NUMERIC(15,4),
    yoy_assessed_value_pct       NUMERIC(15,4),
    yoy_tax_amount_pct           NUMERIC(15,4),  -- NULL for 2021–2024 (not available)

    -- Ratios
    -- NUMERIC(10,4): assessment_ratio can exceed 999 in AJR bad-data rows
    -- (e.g. market_value=1, assessed_value=normal), overflowing NUMERIC(7,4).
    assessment_ratio             NUMERIC(10,4),  -- assessed_value / market_value; NULL if market = 0 or ratio > 100
    effective_tax_rate           NUMERIC(10,4),  -- total_tax / market_value; NULL for 2021–2024

    -- Cumulative (only set on the most-recent-year row per parcel)
    cumulative_value_growth_pct  NUMERIC(15,4),  -- earliest valid year → 2025
    cumulative_tax_growth_pct    NUMERIC(15,4),  -- NULL until full billing history exists

    -- Risk flags
    risk_large_value_jump        BOOLEAN      DEFAULT FALSE,  -- |yoy_market_value_pct| > threshold
    risk_large_value_jump_pct    NUMERIC(15,4),
    risk_homestead_cap_expiry    BOOLEAN      DEFAULT FALSE,  -- residential, hs_cap present, mkt >> assessed
    risk_delinquent              BOOLEAN      DEFAULT FALSE,
    risk_data_incomplete         BOOLEAN      DEFAULT FALSE,  -- market_value = 0 or known gap

    -- Provenance
    computed_at                  TIMESTAMPTZ  DEFAULT NOW(),
    computation_version          VARCHAR(20),

    PRIMARY KEY (geo_id, tax_year)
);

CREATE INDEX IF NOT EXISTS idx_metrics_year        ON parcel_metrics (tax_year);
CREATE INDEX IF NOT EXISTS idx_metrics_risk_jump   ON parcel_metrics (risk_large_value_jump) WHERE risk_large_value_jump = TRUE;
CREATE INDEX IF NOT EXISTS idx_metrics_cap_expiry  ON parcel_metrics (risk_homestead_cap_expiry) WHERE risk_homestead_cap_expiry = TRUE;
CREATE INDEX IF NOT EXISTS idx_metrics_delinquent  ON parcel_metrics (risk_delinquent) WHERE risk_delinquent = TRUE;


-- county_benchmark: one row per property type per year, county-wide aggregates.
-- property_type_label matches the display mapping used in the UI
-- (A→'Residential', B→'Multi-Family', C→'Land/Vacant', D/E→'Agricultural', F→'Commercial').
CREATE TABLE IF NOT EXISTS county_benchmark (
    county_code              VARCHAR(20)  NOT NULL DEFAULT 'TRAVIS',
    tax_year                 SMALLINT     NOT NULL,
    property_type_label      VARCHAR(50)  NOT NULL,
    state_cd1_prefix         VARCHAR(5),           -- the state_cd1 first-char that defines this group

    parcel_count             INTEGER,
    median_market_value      BIGINT,
    p25_market_value         BIGINT,
    p75_market_value         BIGINT,
    median_assessed_value    BIGINT,
    median_assessment_ratio  NUMERIC(7,4),
    median_yoy_value_change_pct NUMERIC(15,4),    -- NULL for 2021 (no prior year)

    computed_at              TIMESTAMPTZ  DEFAULT NOW(),

    PRIMARY KEY (county_code, tax_year, property_type_label)
);

CREATE INDEX IF NOT EXISTS idx_benchmark_year_type ON county_benchmark (tax_year, property_type_label);

-- ── Migration: widen pct columns to NUMERIC(15,4) ──────────────────────────────
-- AJR source data contains extreme outliers (max observed: 751,858,200% YoY) that
-- overflow NUMERIC(9,4). These DO blocks are safe to re-run; they no-op if the
-- column is already the right type.
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN yoy_market_value_pct      TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN yoy_assessed_value_pct    TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN yoy_tax_amount_pct        TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN cumulative_value_growth_pct TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN cumulative_tax_growth_pct TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN risk_large_value_jump_pct TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE county_benchmark ALTER COLUMN median_yoy_value_change_pct TYPE NUMERIC(15,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN assessment_ratio  TYPE NUMERIC(10,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;
DO $$ BEGIN ALTER TABLE parcel_metrics ALTER COLUMN effective_tax_rate TYPE NUMERIC(10,4); EXCEPTION WHEN OTHERS THEN NULL; END $$;


-- rate_trend: VIEW on county_tax_rate adding YoY delta/pct.
-- No new table — just makes rate history easier to query with trends.
CREATE OR REPLACE VIEW rate_trend AS
SELECT
    entity_code,
    entity_name,
    tax_year,
    rate,
    rate - LAG(rate) OVER (PARTITION BY entity_code ORDER BY tax_year)   AS yoy_rate_change,
    ROUND(
        100.0 * (rate - LAG(rate) OVER (PARTITION BY entity_code ORDER BY tax_year))
        / NULLIF(LAG(rate) OVER (PARTITION BY entity_code ORDER BY tax_year), 0),
        4
    )                                                                      AS yoy_rate_change_pct
FROM county_tax_rate
ORDER BY entity_code, tax_year;

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_parcel_prop_id     ON parcel(prop_id);
CREATE INDEX IF NOT EXISTS idx_parcel_owner       ON parcel(owner_name);
CREATE INDEX IF NOT EXISTS idx_pty_year           ON parcel_tax_year(tax_year);
CREATE INDEX IF NOT EXISTS idx_billing_geo        ON tax_billing(geo_id);
CREATE INDEX IF NOT EXISTS idx_rate_year          ON county_tax_rate(tax_year);
CREATE INDEX IF NOT EXISTS idx_rate_entity        ON county_tax_rate(entity_code);
