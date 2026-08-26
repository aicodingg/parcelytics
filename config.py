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

# PIR-XLSX-HOTFIX-1 follow-up (real, preventative, Aug 17 2026): a real,
# live incident this session showed a bare `DATABASE_URL` env var, unset in
# a local shell, silently falls through to the local-dev defaults above with
# NO visible signal at all -- Diego ran a loader expecting to test something
# meaningful, silently hit a stale local database that predates the
# county-partitioning migration, and got a confusing, out-of-context
# "column county_code does not exist" error 80 seconds into an unrelated
# 437K-row parse, with nothing in the loader's own output pointing at "you're
# on the wrong database" as the real cause. DB_SOURCE records which path was
# taken so loaders/db.py's get_conn() (the one, canonical connection helper
# every loader/migration script uses -- NOT app.py's own separate get_db(),
# which is request-scoped and deliberately left alone here) can print an
# explicit, unmissable banner on every real connection, instead of the
# database identity being a silent, easy-to-forget implementation detail.
DB_SOURCE = "env:DATABASE_URL" if _database_url else "local-fallback-defaults"

# ── Data files (FILE-ARCH-2, Aug 21 2026) ───────────────────────────────────
# Real, structural change: the old DATA_DIR pointed at a single flat folder
# (~/Desktop/Claude Files) holding the git repo AND every raw data file
# loose, side by side -- the exact anti-pattern FILE-ARCH-1/Fable's review
# named and FILE-ARCH-2 physically corrects. The new real, approved layout
# (see that brief) is a dedicated ~/Parcelytics/ root, external to the repo
# entirely, with data organized as:
#     data/<county>/<source_slug>/{current, archive/<vintage>, canary}
# where <source_slug> must match that county's real Source Registry row
# 1:1 -- a folder without a registry row, or a row without a folder, is
# meant to be a visible, checkable anomaly, not silently reconciled.
#
# PARCELYTICS_DATA_ROOT replaces DATA_DIR as the one real env-var-
# overridable root, defaulting to the new location. Every county/source
# path below is derived from root + registry slug via the two small
# helpers below -- one real path convention, so a future move (or a
# sandbox/local split) is a config change here, not another migration.
PARCELYTICS_DATA_ROOT = os.environ.get(
    "PARCELYTICS_DATA_ROOT",
    os.path.expanduser("~/Parcelytics/data")
)

# Back-compat alias: DATA_DIR is deliberately NOT kept pointed at the old
# location -- any real caller still reading DATA_DIR directly (rather than
# the slug-based paths below) is exactly the kind of untracked reference
# FILE-ARCH-2's own Step 1 enumeration exists to catch. Kept only as an
# equality alias so a missed reference fails by pointing at the new (empty,
# at least until Diego's own manual data move finishes) location instead of
# silently resolving against the retired folder.
DATA_DIR = PARCELYTICS_DATA_ROOT


def _county_source(county, slug, *parts):
    """Real, single path-construction helper -- every county/source path in
    this file goes through this or _travis() below, never a hand-built
    os.path.join(DATA_DIR, ...) again. `slug` must match that county's own
    real Source Registry row (see FILE-ARCH-2's own binding rule)."""
    return os.path.join(PARCELYTICS_DATA_ROOT, county, slug, *parts)


def _travis(slug, *parts):
    return _county_source("travis", slug, *parts)


# ── Archive root (FILE-ARCH-3, Aug 2026, Fable-approved) ────────────────────
# Real, confirmed two-root architecture: the local PARCELYTICS_DATA_ROOT above
# holds current/ and canary/ only; archived source data (multi-GB per-vintage
# exports) lives on a separate external drive, PARCELYTICS_ARCHIVE_ROOT.
#
# Fable's own explicit reasoning for making this a second real root in code
# (Option 2) rather than a symlink pointing PARCELYTICS_DATA_ROOT/.../archive
# at the external drive: a symlink is filesystem state, not code -- invisible
# in the repo, absent from any fresh machine's mental model, and makes this
# file lie by omission about where data actually lives. The two-root reality
# (local current/, external archive/) is a real architectural fact that
# belongs here, in code, not silently in the filesystem.
PARCELYTICS_ARCHIVE_ROOT = os.environ.get(
    "PARCELYTICS_ARCHIVE_ROOT",
    "/Volumes/Expansion/parcelytics_vault"
)


class ArchiveNotMountedError(RuntimeError):
    """Raised when a real archive/<vintage> path is resolved but the
    external vault drive (PARCELYTICS_ARCHIVE_ROOT) isn't mounted. A named
    exception class, not a bare FileNotFoundError, so this fails loudly and
    unambiguously at the point of use rather than surfacing as a confusing
    raw stack trace three directories deep inside whatever loader asked for
    the path."""
    pass


def _require_archive_mounted():
    # Real, confirmed failure-mode reasoning (FILE-ARCH-3): fail-loudly is
    # correct here specifically, because normal operation (loaders reading
    # current/, canary runs against canary/) never touches this root at all
    # -- a disconnected external drive costs nothing until something
    # actually asks for archived data. That's exactly when this check runs:
    # inside the archive-side path helpers below, at the point a real
    # archive path is requested, never at config.py import time (which
    # would wrongly turn "drive unplugged" into "app won't start").
    if not os.path.isdir(PARCELYTICS_ARCHIVE_ROOT):
        raise ArchiveNotMountedError(
            f"archive root not mounted: {PARCELYTICS_ARCHIVE_ROOT} -- plug "
            f"in / mount the external vault drive before requesting an "
            f"archive-slug path. Current/canary data is unaffected; this "
            f"only fires when something specifically asked for archived "
            f"source data."
        )


def _county_source_archive(county, slug, *parts):
    """Archive-side twin of _county_source() above -- identical registry-
    slug grammar (one path grammar, two roots), rooted at
    PARCELYTICS_ARCHIVE_ROOT instead of PARCELYTICS_DATA_ROOT. Checks the
    drive is actually mounted before returning a path; see
    _require_archive_mounted()'s own docstring for why that check lives
    here (at resolution time) rather than at import time."""
    _require_archive_mounted()
    return os.path.join(PARCELYTICS_ARCHIVE_ROOT, county, slug, *parts)


def _travis_archive(slug, *parts):
    return _county_source_archive("travis", slug, *parts)


# Real, confirmed rider (not touched by this brief, per Fable's own explicit
# instruction): the legacy vault path this drive already holds
# (`Travis County (TX)/...`, with its real spaces and parentheses) is
# referenced history and stays exactly as-is -- do not rename it. Only
# archive paths this file newly constructs going forward (i.e. any real call
# to _travis_archive() from here on) use the same slug convention as
# PARCELYTICS_DATA_ROOT (travis/certified_roll/..., not
# "Travis County (TX)/..."), so the archive root converges on the one real
# grammar for new material without disturbing what's already there.
#
# Real, confirmed rider (also settled, no code change): MC-4's canary
# slices stay local-only, always derived from PARCELYTICS_DATA_ROOT's own
# canary/ folders, never PARCELYTICS_ARCHIVE_ROOT -- canaries gate loads, so
# they must be available precisely when the external drive might not be,
# and they're regenerable in one line from archived source data (see
# MULTI_COUNTY_ONBOARDING_STANDARDS.md's MC-4, point 4), so a vault copy
# would just be redundancy of a disposable artifact. No _travis_archive()
# call should ever construct a canary/ path.
#
# Real, honest disclosure: the actual physical move of Travis's bulk
# archived source data from the legacy vault layout into this new slug-
# based structure is out of scope for this brief -- a real, separate, later
# step once this config change lands and is verified. No real call site
# calls _travis_archive() yet as of this commit.


# Real Travis source_slugs, derived from Travis's own Source Registry entry
# (4 real rows: CAD certified appraisal export, CAD preliminary appraisal
# export, Tax office billing data, Adopted tax rates) -- no separate
# Registry row exists for AJR specifically, so the 2021-2024 AJR/EARS
# files (the real, historical stand-in used before the full certified
# export pipeline covered those years) nest under certified_roll, the same
# registry row, rather than inventing an unregistered "ajr" slug.
#
# Real, honest disclosure: the actual multi-GB certified/preliminary
# export folders (2022-2026) do not currently exist in this connected
# folder or anywhere this session can reach -- Diego confirmed they live
# externally and will move them into the archive/<year> folders below
# himself. The paths are written now, against the real destination
# layout, so no second config edit is needed once that move happens.
AJR_FILES = {
    2021: _travis("certified_roll", "archive", "2021", "20210925_000416_PTD.csv"),
    2022: _travis("certified_roll", "archive", "2022", "extracted", "227EARS092822.csv"),
    2023: _travis("certified_roll", "archive", "2023", "extracted", "227EARS083023.csv"),
    2024: _travis("certified_roll", "archive", "2024", "ears_extracted", "227EARS082824.csv"),
    # 2025 AJR is intentionally omitted — use Certified Export instead
}

CERT_2021_PDF = _travis("certified_roll", "archive", "2021",
                         "2021 CERTIFIED APPRAISAL ROLL as of Supp 0_GEO.pdf")

PRELIM_2026_DIR = _travis("preliminary_roll", "archive", "2026")

# ── CERT_DIR family (PX-20260824-03: real-location fix) ─────────────────────
# The 5 lines this replaced (`CERT_DIR = _travis("certified_roll", "archive",
# "2025")` etc.) never matched where these files actually are. They assumed
# a local-root `certified_roll/archive/<year>/` layout that vault_manifest.md
# never used -- PX-20260824-02's pre-flight found the real, SHA-256-verified
# location for every one of these years is on the EXTERNAL archive drive
# (PARCELYTICS_ARCHIVE_ROOT), in a date-stamped folder, not a year-named one.
# Each date below is cited straight from vault_manifest.md's own "Current
# path (as of FILE-ARCH-3, 2026-08-22)" column (all rows COPIED_VERIFIED):
#   CERT_DIR      -> vault_manifest.md row: "2025 | certified | .../PROP.TXT | ... |
#                     .../certified_roll/2025-07-20/PROP.TXT (verified 2026-08-22)"
#   CERT_DIR_2022 -> vault_manifest.md row: "2022 | certified | .../PROP.TXT | ... |
#                     .../certified_roll/2022-07-25/PROP.TXT (verified 2026-08-22)"
#   CERT_DIR_2023 -> vault_manifest.md row: "2023 | certified | .../PROP.TXT | ... |
#                     .../certified_roll/2023-07-22/PROP.TXT (verified 2026-08-22)"
#   CERT_DIR_2024 -> vault_manifest.md row: "2024 | certified | .../PROP.TXT | ... |
#                     .../certified_roll/2024-08-21/PROP.TXT (verified 2026-08-22)"
#   CERT_DIR_2026 -> vault_manifest.md row: "2026 | certified | .../PROP.TXT | ... |
#                     .../certified_roll/2026-07-19/PROP.TXT (verified 2026-08-22)"
# (PROP_ENT.TXT and LAND_DET.TXT for each year are recorded at the identical
# directory in the manifest -- one row per file, same date-stamped folder.)
#
# Real design constraint, not incidental: these must resolve through
# _travis_archive() (the canonical FILE-ARCH-3 archive-side helper, per this
# brief's own instruction), which calls _require_archive_mounted() and RAISES
# ArchiveNotMountedError if the external drive isn't attached -- correct and
# deliberate (silently resolving to a wrong/missing path is exactly the bug
# being fixed here). But _require_archive_mounted()'s own docstring is
# explicit that this check must fire "at the point of use", "never at
# config.py import time (which would wrongly turn 'drive unplugged' into
# 'app won't start')". A plain `CERT_DIR_2022 = _travis_archive(...)` module-
# level assignment would violate that -- Python evaluates it once, eagerly,
# the moment ANYTHING does `import config`, including callers (app.py, most
# loaders) that never touch this data and must keep starting cleanly with
# the drive unplugged.
#
# Module-level __getattr__ (PEP 562) defers that resolution to the actual
# point of use: `config.CERT_DIR_2022` still reads exactly like a plain
# string attribute to every existing caller (no call-site changes needed
# anywhere in the codebase), but the mount check and the real path join only
# happen the first time something actually asks for one of these 5 names --
# matching _require_archive_mounted()'s own stated intent exactly, instead
# of fighting it.
_CERT_ARCHIVE_DATES = {
    "CERT_DIR":      "2025-07-20",
    "CERT_DIR_2022": "2022-07-25",
    "CERT_DIR_2023": "2023-07-22",
    "CERT_DIR_2024": "2024-08-21",
    "CERT_DIR_2026": "2026-07-19",
}


# ── Dallas archive grammar (PX-20260826-04, DCAD relational certified-roll
# product) ────────────────────────────────────────────────────────────────
# Same real, PEP 562 lazy-resolution pattern as the CERT_DIR family above,
# now extended to a second county.
#
# REAL CORRECTION (PX-20260826-04 follow-up finding, from Diego's own
# real-dry-run check against the physical vault): the FIRST version of this
# block (committed to this session's own working tree, never pushed to
# Diego) derived a per-year dated folder (e.g. "2026-07-23") directly under
# certified_roll/ from vault_manifest.md's own "Vault path" TABLE COLUMN --
# reading that column at face value. That column is WRONG relative to the
# real, physical archive layout: the whole 5-year Dallas acquisition was
# archived as ONE event on 2026-08-26 (vault_manifest.md's own Migration 4
# prose says so explicitly: "archived to the vault 2026-08-26"), which
# means one acquisition-dated folder contains all five years' own
# "<year> Certified/" subfolders, preserving the delivered structure --
# NOT five independently-dated top-level folders keyed by each year's own
# certification date. The per-row "Vault path" column in the manifest's own
# Migration 4 table is itself stale/wrong on this point (a real, disclosed
# manifest-vs-filesystem mismatch, not a values judgment call) -- the
# correct ground truth is the acquisition date (2026-08-26, from the
# migration's own header/prose) PLUS each row's own "Delivered path (as
# received)" column, preserved verbatim as the sub-path under that one
# acquisition folder. Real, corrected shape:
#     PARCELYTICS_ARCHIVE_ROOT/dallas/certified_roll/2026-08-26/<year> Certified/DCAD<year>_CERTIFIED_<certdate>/
# test_cert_archive_paths.py pins this exact shape against vault_manifest.md's
# own Migration 4 rows (parsed from the table directly, not re-typed) so
# this specific drift class fails loud in a test, not just at runtime.
#
# Real, honest disclosure: only 2026 has been EXTRACTED (vault_manifest.md's
# own 2026 rows hash 14 individual per-table CSVs directly; every other
# year's 2022-2025 rows hash only the still-zipped .ZIP file itself) --
# 2022-2025 extraction is a separate, later, not-yet-done step (this
# brief's own text says so explicitly). DALLAS_EXTRACTED_YEARS records
# that real, current state so load_dallas_certified.py can fail loud with
# a clear, correct message ("this year's zip hasn't been extracted yet")
# distinct from a generic "directory not found".
DALLAS_ACQUISITION_DATE = "2026-08-26"  # the one real archival-event date for all 5 years (Migration 4 prose)
_DALLAS_CERT_ARCHIVE_INFO = {
    "DALLAS_CERT_DIR_2022": ("2022 Certified", "DCAD2022_CERTIFIED_07252022"),
    "DALLAS_CERT_DIR_2023": ("2023 Certified", "DCAD2023_CERTIFIED_07252023"),
    "DALLAS_CERT_DIR_2024": ("2024 Certified", "DCAD2024_CERTIFIED_07252024"),
    "DALLAS_CERT_DIR_2025": ("2025 Certified", "DCAD2025_CERTIFIED_07242025"),
    "DALLAS_CERT_DIR_2026": ("2026 Certified", "DCAD2026_CERTIFIED_07232026"),
}
DALLAS_EXTRACTED_YEARS = frozenset({2026})


def _dallas_archive(*parts):
    return _county_source_archive("dallas", "certified_roll", *parts)


def __getattr__(name):
    """PEP 562 module-level lazy attribute resolution -- see the CERT_DIR
    family comment block above for why this exists. Intercepts the 5
    Travis names in _CERT_ARCHIVE_DATES and the 5 Dallas names in
    _DALLAS_CERT_ARCHIVE_INFO; anything else is a genuine AttributeError,
    same as normal module attribute lookup (this function is only consulted
    when a plain lookup in this module's __dict__ already failed)."""
    if name in _CERT_ARCHIVE_DATES:
        return _travis_archive("certified_roll", _CERT_ARCHIVE_DATES[name])
    if name in _DALLAS_CERT_ARCHIVE_INFO:
        year_folder, dcad_folder = _DALLAS_CERT_ARCHIVE_INFO[name]
        return _dallas_archive(DALLAS_ACQUISITION_DATE, year_folder, dcad_folder)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ── Raw Vault (DATA_LIFECYCLE.md Stage 0 / Phase 1 backfill, Aug 2026) ──────
# vault/{county}/{year}/{source}/{date}/ per the lifecycle doc's own
# convention. Real, honest disclosure: FILE-ARCH-1/2's own approved
# structure does not name a location for the Vault at all -- it predates
# that brief. Placed here at data/_vault/ (a sibling of the per-county
# folders, not nested inside any one county/source_slug, since the Vault's
# own convention is already county/source-scoped one level down) as a
# reasonable extension of the same root, NOT a Fable-confirmed placement --
# flag for Diego/Fable to confirm or relocate, same as any other real,
# undecided judgment call in this file.
VAULT_DIR = os.path.join(PARCELYTICS_DATA_ROOT, "_vault")
TAX_CUR_CSV  = _travis("tax_billing", "current", "TaxCurOpenData (1).csv")
TAX_DELQ_CSV = _travis("tax_billing", "current", "TaxDelqOpenData.csv")
TAX_RATES_XL = _travis("rates", "current", "2025RatesHistory1990-2025.xlsx")

# Real, single source of truth for the 4 real Travis PIR billing xlsx
# files (2021 Revised + 2022/2023/2024) -- previously each of
# load_pir_billing_2022.py / _2023.py / _2024.py and
# check_geo_ids_in_pir_source.py independently hardcoded their own
# DATA_DIR-relative join (a real Step 1 finding, beyond config.py's own
# already-known line). All 4 real call sites now import and read this dict
# instead, so a future move touches this one dict, not 4 separate files.
PIR_BILLING_XLSX = {
    2021: _travis("tax_billing", "archive", "2021", "DiegoPIR2021 Revised.xlsx"),
    2022: _travis("tax_billing", "archive", "2022", "DiegoPIR2022.xlsx"),
    2023: _travis("tax_billing", "archive", "2023", "DiegoPIR2023.xlsx"),
    2024: _travis("tax_billing", "archive", "2024", "DiegoPIR2024.xlsx"),
}

# ── PIR / Open Records Requests ──────────────────────────────────────────────
# Populate these when files arrive, then run:
#   python3 loaders/load_pir_tcad.py --inspect   (confirm field positions first)
#   python3 loaders/load_pir_tcad.py             (load taxable_value, land, imprv)
#   python3 loaders/load_pir_billing.py          (load historical billing 2021-2024)
#   python3 loaders/compute_metrics.py           (recompute — flips Not Available → Verified)
#
# TCAD PIR Ref. R010172-062126: taxable_value, land_value, imprv_value for 2021–2024
PIR_TCAD_FILES = {
    # 2021: _travis("tax_billing", "archive", "2021", "pir_tcad_2021.csv"),
    # 2022: _travis("tax_billing", "archive", "2022", "pir_tcad_2022.csv"),
    # 2023: _travis("tax_billing", "archive", "2023", "pir_tcad_2023.csv"),
    # 2024: _travis("tax_billing", "archive", "2024", "pir_tcad_2024.csv"),
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
PIR_2021_FULL_XLSX = PIR_BILLING_XLSX[2021]

# Travis County Tax Office (sent Jun 21 2026): historical billing for 2021–2024
# Each file is expected to be TaxCurOpenData-format with TAXYEAR column present.
# If the office sends one multi-year file instead, list it once with any key (e.g. 0).
PIR_BILLING_FILES = {
    # 2021: _travis("tax_billing", "archive", "2021", "TaxCurOpenData_2021.csv"),
    # 2022: _travis("tax_billing", "archive", "2022", "TaxCurOpenData_2022.csv"),
    # 2023: _travis("tax_billing", "archive", "2023", "TaxCurOpenData_2023.csv"),
    # 2024: _travis("tax_billing", "archive", "2024", "TaxCurOpenData_2024.csv"),
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

# ── Rate limit exemption allowlist (RATE-LIMIT-EXEMPT-1, Aug 2026) ────────────
# Diego got locked out of his own production site (429 Too Many Requests on
# _LIMIT_HEAVY routes) during a live-testing session -- his real browser IP
# shares whatever rate-limit bucket a Chrome-extension-driven testing session
# consumes. Comma-separated list of IPs to fully exempt from every rate limit
# (default_limits AND every @limiter.limit(...) tier -- see app.py's
# request_filter for the reasoning on why this is applied uniformly rather
# than per-tier). No default -- an unset/empty env var means an EMPTY
# allowlist (nobody exempted), never "exempt everyone"; see
# test_rate_limit_exempt.py's own explicit test for this failure mode.
#
# Same convention as SENTRY_DSN above: read once at import time via
# os.environ.get(), no hardcoded fallback. Diego updates this the same way
# he already updates Render's database IP allowlist when his location
# changes (e.g. traveling to Dallas) -- edit the env var in Render's
# dashboard, restart the service (not a full redeploy) to pick it up.
# Parsed into a frozenset of stripped, non-empty strings so a trailing
# comma or accidental whitespace ("1.2.3.4, 5.6.7.8, ") doesn't produce a
# bogus empty-string entry that could (harmlessly, since "" would never
# equal a real client IP, but confusingly) live in the set.
RATE_LIMIT_EXEMPT_IPS = frozenset(
    ip.strip() for ip in os.environ.get("RATE_LIMIT_EXEMPT_IPS", "").split(",") if ip.strip()
)

# ── Version ───────────────────────────────────────────────────────────────────
# Cowork brief "Version Display + Single Source of Truth", July 2026. The
# VERSION file at the repo root is the ONE place this number lives -- bump it
# there and it's picked up everywhere (currently: the site footer) with no
# other edit required. Read once at import time, not per-request.
VERSION = open(os.path.join(os.path.dirname(__file__), "VERSION")).read().strip()
