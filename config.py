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

CERT_DIR     = os.path.join(DATA_DIR, "2025 Certified Appraisal Export Supp 0_07202025")
TAX_CUR_CSV  = os.path.join(DATA_DIR, "TaxCurOpenData (1).csv")
TAX_DELQ_CSV = os.path.join(DATA_DIR, "TaxDelqOpenData.csv")
TAX_RATES_XL = os.path.join(DATA_DIR, "2025RatesHistory1990-2025.xlsx")

# ── App ───────────────────────────────────────────────────────────────────────
FLASK_SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
DEBUG        = os.environ.get("FLASK_DEBUG", "1") == "1"
PORT         = int(os.environ.get("PORT", 5000))
