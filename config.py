import os

# ── Database ──────────────────────────────────────────────────────────────────
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ.get("DB_NAME", "parcel_tax")
DB_USER = os.environ.get("DB_USER", os.getenv("USER", "postgres"))
DB_PASS = os.environ.get("DB_PASS", "")

DATABASE_URL = (
    f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# ── Data files ────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get(
    "DATA_DIR",
    os.path.expanduser("~/Desktop/Claude Files")
)

AJR_FILES = {
    2021: os.path.join(DATA_DIR, "2021EARS092521/20210925_000416_PTD.csv"),
    2022: os.path.join(DATA_DIR, "227EARS092822 (2)/extracted/227EARS092822.csv"),
    2023: os.path.join(DATA_DIR, "227EARS082923 (2)/extracted/227EARS083023.csv"),
    2024: os.path.join(DATA_DIR, "227EARS082824 (2)/ears_extracted/227EARS082824.csv"),
    # 2025 AJR is intentionally omitted — use Certified Export instead
}

CERT_DIR      = os.path.join(DATA_DIR, "2025 Certified Appraisal Export Supp 0_07202025")
CERT_DIR_2022 = os.path.join(DATA_DIR, "2022_Certified_Export")
CERT_DIR_2023 = os.path.join(DATA_DIR, "2023_Certified_Export")
CERT_DIR_2024 = os.path.join(DATA_DIR, "2024_Certified_Export")
PRELIM_2026_DIR = os.path.join(DATA_DIR, "2026 Preliminary Appraisal Export Supp 0_06092026 (1)")
TAX_CUR_CSV  = os.path.join(DATA_DIR, "TaxCurOpenData (1).csv")
TAX_DELQ_CSV = os.path.join(DATA_DIR, "TaxDelqOpenData.csv")
TAX_RATES_XL = os.path.join(DATA_DIR, "2025RatesHistory1990-2025.xlsx")

# ── PIR / Open Records Requests ──────────────────────────────────────────────
# Populate these when files arrive, then run:
#   python3 loaders/load_pir_tcad.py --inspect   (confirm field positions first)
#   python3 loaders/load_pir_tcad.py             (load taxable_value, land, imprv)
#   python3 loaders/load_pir_billing.py          (load historical billing 2021-2024)
#   python3 loaders/compute_metrics.py           (recompute — flips Not Available → Verified)
#
# TCAD PIR Ref. R010172-062126: taxable_value, land_value, imprv_value for 2021–2024
PIR_TCAD_FILES = {
    # 2021: os.path.join(DATA_DIR, "pir_tcad_2021.csv"),
    # 2022: os.path.join(DATA_DIR, "pir_tcad_2022.csv"),
    # 2023: os.path.join(DATA_DIR, "pir_tcad_2023.csv"),
    # 2024: os.path.join(DATA_DIR, "pir_tcad_2024.csv"),
}

# Travis County Tax Office 2021 PIR response, full per-entity export (received
# ~Jul 2026): a real, comprehensive 418,159-row bulk billing file, one row per
# taxing account, up to 10 entities per account with base/due/penalty/attorney-
# fee/collected columns each -- far richer AND far messier than the simple
# TaxCurOpenData-format PIR_BILLING_FILES below. Loaded by a dedicated script
# (loaders/load_pir_billing_2021_full.py, not load_pir_billing.py) because the
# column layout is completely different. See that script's module docstring
# for the full investigation writeup (geo_id mapping, duplicate-account
# handling, field semantics) before touching this loader.
PIR_2021_FULL_XLSX = os.path.join(DATA_DIR, "DiegoPIR2021 Revised.xlsx")

# Travis County Tax Office (sent Jun 21 2026): historical billing for 2021–2024
# Each file is expected to be TaxCurOpenData-format with TAXYEAR column present.
# If the office sends one multi-year file instead, list it once with any key (e.g. 0).
PIR_BILLING_FILES = {
    # 2021: os.path.join(DATA_DIR, "TaxCurOpenData_2021.csv"),
    # 2022: os.path.join(DATA_DIR, "TaxCurOpenData_2022.csv"),
    # 2023: os.path.join(DATA_DIR, "TaxCurOpenData_2023.csv"),
    # 2024: os.path.join(DATA_DIR, "TaxCurOpenData_2024.csv"),
}

# ── Feature flags ─────────────────────────────────────────────────────────────
# When True, the 5-Year History table shows a computed tax estimate for
# tax years 2021–2024 where no real billing data is available. The value is
# derived as:  taxable_value × combined_rate / 100
# and is clearly labelled "~$X,XXX (computed)" — NOT the actual billed amount.
#
# Enabled (Jun 23 2026): Travis County Tax Office confirmed they do not retain
# historical snapshots of TaxCurOpenData. Computed levy is the best available
# estimate for the full 430K parcel dataset. Where real billing data exists
# (portal_scrape rows or future PIR bulk data), it takes priority automatically
# — computed_total_tax is only filled when total_tax IS NULL.
#
# Priority order in the UI:
#   1. Verified billing (taxcur / pir_billing) — shown as $X,XXX
#   2. Portal payment receipt (portal_scrape)  — shown as ~$X,XXX · Partial
#   3. Computed levy (taxable_value × rate)    — shown as ~$X,XXX (computed)
#   4. No data                                 — shown as "Not available yet"
COMPUTED_HIST_TAX_ENABLED = os.environ.get("COMPUTED_HIST_TAX", "0") == "1"

# ── App ───────────────────────────────────────────────────────────────────────
FLASK_SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
DEBUG        = os.environ.get("FLASK_DEBUG", "1") == "1"
PORT         = int(os.environ.get("PORT", 5000))
