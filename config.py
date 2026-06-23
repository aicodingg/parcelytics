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

# Travis County Tax Office (sent Jun 21 2026): historical billing for 2021–2024
# Each file is expected to be TaxCurOpenData-format with TAXYEAR column present.
# If the office sends one multi-year file instead, list it once with any key (e.g. 0).
PIR_BILLING_FILES = {
    # 2021: os.path.join(DATA_DIR, "TaxCurOpenData_2021.csv"),
    # 2022: os.path.join(DATA_DIR, "TaxCurOpenData_2022.csv"),
    # 2023: os.path.join(DATA_DIR, "TaxCurOpenData_2023.csv"),
    # 2024: os.path.join(DATA_DIR, "TaxCurOpenData_2024.csv"),
}

# ── App ───────────────────────────────────────────────────────────────────────
FLASK_SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
DEBUG        = os.environ.get("FLASK_DEBUG", "1") == "1"
PORT         = int(os.environ.get("PORT", 5000))
