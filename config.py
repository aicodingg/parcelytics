import os
import urllib.parse

# ── Database ──────────────────────────────────────────────────────────────────
# Cowork brief "Production Deployment Readiness (Render)", July 2026.
# Render (and most hosts) inject a single DATABASE_URL env var in the standard
# postgresql://user:pass@host:port/dbname form. Previously this module built
# its own DATABASE_URL string from the 5 pieces below but never read one back
# in -- so a host-provided DATABASE_URL would have been silently ignored and
# the app would have kept trying to connect to "localhost", failing in
# production. Now: if DATABASE_URL is set, it's parsed into the 5 pieces
# (host/port/dbname/user/password) that loaders/db.py's get_conn() and
# app.py's get_db() already consume via keyword args -- so neither of those
# needed to change at all. If DATABASE_URL is unset, behavior is byte-for-byte
# identical to before (same env vars, same local-dev defaults).
_database_url = os.environ.get("DATABASE_URL")

if _database_url:
    _parsed = urllib.parse.urlparse(_database_url)
    DB_HOST = _parsed.hostname or "localhost"
    DB_PORT = _parsed.port or 5432
    DB_NAME = (_parsed.path or "").lstrip("/") or "parcel_tax"
    DB_USER = _parsed.username or os.getenv("USER", "postgres")
    DB_PASS = _parsed.password or ""
else:
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = int(os.environ.get("DB_PORT", 5432))
    DB_NAME = os.environ.get("DB_NAME", "parcel_tax")
    DB_USER = os.environ.get("DB_USER", os.getenv("USER", "postgres"))
    DB_PASS = os.environ.get("DB_PASS", "")

DATABASE_URL = _database_url or (
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
DEBUG = os.environ.get("FLASK_DEBUG", "1") == "1"
PORT  = int(os.environ.get("PORT", 5000))

# FLASK_SECRET -- Cowork brief "Wire Up a Real FLASK_SECRET", July 2026.
# No hardcoded fallback string. DEBUG (above) is used instead of Flask's own
# app.debug here -- this module has no Flask app object (config.py is a plain
# settings module imported before app = Flask(__name__) exists in app.py),
# and DEBUG is already this project's actual source of truth for dev vs.
# production mode (it's what app.py passes to app.run(debug=config.DEBUG, ...)).
#
# In dev (DEBUG on), a missing FLASK_SECRET is fine -- generate a random
# per-run value so no developer has to set this just to run the app locally.
# Sessions won't persist across restarts in that case -- acceptable for dev,
# never acceptable in production. In production (DEBUG off), a missing
# FLASK_SECRET is a hard failure: never silently fall back to something
# insecure/predictable in production, so raise instead of starting.
FLASK_SECRET = os.environ.get("FLASK_SECRET")
if not FLASK_SECRET:
    if DEBUG:
        import secrets
        FLASK_SECRET = secrets.token_hex(32)
        print("  FLASK_SECRET: not set, using a random per-run value (dev only)")
    else:
        raise RuntimeError("FLASK_SECRET must be set in the environment for production")

# ── Error monitoring (Sentry) ─────────────────────────────────────────────────
# Cowork brief "Error Monitoring (Sentry) + Rate Limiting (Flask-Limiter)",
# July 2026. No default -- deliberately None, never a hardcoded/fallback DSN.
# app.py checks `if config.SENTRY_DSN:` before calling sentry_sdk.init() and
# skips initialization entirely when this is unset (e.g. local dev without
# it exported), rather than erroring or silently using a placeholder.
SENTRY_DSN = os.environ.get("SENTRY_DSN")

# ── Version ───────────────────────────────────────────────────────────────────
# Cowork brief "Version Display + Single Source of Truth", July 2026. The
# VERSION file at the repo root is the ONE place this number lives -- bump it
# there and it's picked up everywhere (currently: the site footer) with no
# other edit required. Read once at import time, not per-request.
VERSION = open(os.path.join(os.path.dirname(__file__), "VERSION")).read().strip()
