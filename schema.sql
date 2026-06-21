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

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_parcel_prop_id     ON parcel(prop_id);
CREATE INDEX IF NOT EXISTS idx_parcel_owner       ON parcel(owner_name);
CREATE INDEX IF NOT EXISTS idx_pty_year           ON parcel_tax_year(tax_year);
CREATE INDEX IF NOT EXISTS idx_billing_geo        ON tax_billing(geo_id);
CREATE INDEX IF NOT EXISTS idx_rate_year          ON county_tax_rate(tax_year);
CREATE INDEX IF NOT EXISTS idx_rate_entity        ON county_tax_rate(entity_code);
