# Parcelytics

Property Tax Intelligence Platform for Travis County, Texas.

Parcelytics loads 5 years of TCAD appraisal data and Travis County tax billing records into a local PostgreSQL database and serves them through a Flask web application. The goal is to give investors and property researchers a fast, structured view of parcel value history, tax burden, and entity-level rate trends — data that is technically public but hard to work with in its raw form.

## Features

- **Parcel search** — search by 10-digit TCAD account (geo_id), 14-digit tax office account, or short integer prop_id
- **5-year value history** — market value, assessed value, taxable value (2025 only), land/improvement breakdown (2025 only), and homestead cap loss (2021–2024) per parcel
- **Investor Insight Report** — per-parcel summary of appreciation (CAGR), tax burden, homestead cap buyer risk, property class, and delinquency status
- **Entity-level tax breakdown** — 2025 billing detail by taxing entity with year-over-year rate changes
- **Tax rate trend explorer** — interactive chart of combined and per-entity rates from 1990–2025
- **5-year tax projection** — forward estimate of market value, assessed value, and tax burden based on historical trends *(in development — see note below)*

## Startup

```bash
# 1. Start PostgreSQL (one time per boot)
brew services start postgresql@15

# 2. Start the web app
cd ~/Desktop/Claude\ Files/parcel_app
python3 app.py

# 3. Open in browser
open http://127.0.0.1:5000
```

If port 5000 is in use (macOS AirPlay Receiver), use:
```bash
PORT=5001 python3 app.py
```

## Loading / reloading data

```bash
# Full reload (takes 20–30 min; resets all parcel data)
python3 loaders/run_all.py --reset

# Reload only the 2025 Certified Export (fastest, ~10 min)
python3 loaders/run_all.py --skip-ajr --skip-tax

# Reload only tax billing and rates (fast)
python3 loaders/run_all.py --skip-ajr --skip-cert
```

## Data sources and coverage

| Source | Years | Fields |
|---|---|---|
| TCAD Certified Appraisal Export (EARS) | 2025 | market, assessed, taxable, land, improvement values; exemptions; owner |
| Texas Comptroller AJR (EARS/CSV) | 2021–2024 | market value, assessed value, homestead cap loss |
| Travis County Tax Office — TaxCurOpenData | 2025 | total tax, total due/paid per entity |
| Travis County Tax Office — TaxDelqOpenData | All years (delinquent only) | delinquent balance, first delinquent year |
| TCAD Tax Rate History XLSX | 1990–2025 | per-entity tax rates |

**Coverage gaps:** taxable value, land/improvement breakdown, and full billing detail are only available for 2025. Historical Certified Export files for 2021–2024 are not publicly available and would require a Public Information Request to TCAD. Historical billing files for 2021–2024 do not exist in the Travis County Tax Office public data portal. See [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) for full detail and resolution paths.

## Database

PostgreSQL 15, database name `parcel_tax`. Six tables:

- `parcel` — one row per unique parcel (geo_id, owner, address, property type)
- `parcel_tax_year` — one row per parcel × year (values, exemptions, data source)
- `tax_billing` — 2025 billing totals per parcel; pre-2025 delinquent-only
- `tax_billing_entity` — 2025 billing detail broken out by taxing entity
- `tax_delinquent` — delinquent summary per parcel
- `county_tax_rate` — per-entity tax rates 1990–2025

Schema: [`schema.sql`](schema.sql)

## Project layout

```
parcel_app/
├── app.py                  Flask application + insight/projection logic
├── config.py               DB and file path configuration
├── schema.sql              PostgreSQL schema with migration guards
├── loaders/
│   ├── run_all.py          Master load script (runs all loaders in order)
│   ├── load_certified_2025.py  2025 EARS Certified Export (PROP.TXT etc.)
│   ├── load_ajr.py         AJR CSV loader for 2021–2024
│   ├── load_tax_current.py Tax Office billing + delinquent loader
│   ├── load_tax_rates.py   Rate history XLSX loader
│   └── db.py               DB helpers (get_conn, execute_schema, batch_upsert)
├── templates/
│   ├── base.html
│   ├── index.html          Search page
│   ├── property.html       Parcel detail page
│   └── rates.html          Tax rate trend explorer
├── ui_test.py              Automated UI/smoke test (run with app started)
├── review_check.py         DB sanity check script
├── KNOWN_LIMITATIONS.md    Documented data gaps and resolution paths
└── push_to_github.sh       Initial GitHub push script
```

## Notes on the 5-year tax projection

The projection feature (`build_projections()` in `app.py`) is included but **not yet validated** for accuracy across property types. It uses historical CAGR for value trend and average annual rate change for the rate trend. It correctly applies the Texas 10%/yr homestead assessment cap for capped properties. It is labeled "ESTIMATES ONLY" in the UI. Do not use it for investment underwriting without further validation — it will be revisited in a later phase.

## Requirements

```
flask
psycopg2-binary
openpyxl
```

Install: `pip3 install flask psycopg2-binary openpyxl`
