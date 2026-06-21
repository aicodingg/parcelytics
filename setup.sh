#!/usr/bin/env bash
# ============================================================
# Travis County Property Tax Platform — macOS Setup Script
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_NAME="${DB_NAME:-parcel_tax}"
DB_USER="${DB_USER:-$(whoami)}"
PORT="${PORT:-5432}"

echo "============================================================"
echo " Travis County Property Tax — Setup"
echo "============================================================"
echo " App dir : $APP_DIR"
echo " DB      : $DB_NAME (user: $DB_USER)"
echo ""

# ── 1. Homebrew & PostgreSQL ──────────────────────────────────
echo "[1/5] Checking PostgreSQL…"
if ! command -v psql &>/dev/null; then
  echo "  Installing postgresql via Homebrew…"
  if ! command -v brew &>/dev/null; then
    echo "ERROR: Homebrew not found. Install from https://brew.sh then re-run."
    exit 1
  fi
  brew install postgresql@15
  brew services start postgresql@15
  echo "  Waiting 5s for Postgres to start…"
  sleep 5
else
  echo "  psql found: $(psql --version)"
  # Start if not running
  pg_isready -q || brew services start postgresql@15 && sleep 2 || true
fi

# ── 2. Create database ────────────────────────────────────────
echo "[2/5] Creating database '$DB_NAME'…"
createdb "$DB_NAME" 2>/dev/null && echo "  Created." || echo "  Already exists."

# ── 3. Python deps ────────────────────────────────────────────
echo "[3/5] Installing Python packages…"
pip3 install -r "$APP_DIR/requirements.txt" --break-system-packages -q
echo "  Done."

# ── 4. Apply schema ───────────────────────────────────────────
echo "[4/5] Applying database schema…"
cd "$APP_DIR"
DB_NAME="$DB_NAME" DB_USER="$DB_USER" python3 loaders/run_all.py --schema-only
echo "  Schema applied."

# ── 5. Instructions ───────────────────────────────────────────
echo ""
echo "============================================================"
echo " Setup complete!"
echo ""
echo " Next: load data (takes 60–90 min for all 5 years):"
echo ""
echo "   cd $APP_DIR"
echo "   DB_NAME=$DB_NAME DB_USER=$DB_USER python3 loaders/run_all.py"
echo ""
echo " To skip AJR years and load only 2025 + rates:"
echo "   python3 loaders/run_all.py --skip-ajr"
echo ""
echo " Then start the app:"
echo "   DB_NAME=$DB_NAME DB_USER=$DB_USER python3 app.py"
echo "   open http://localhost:5000"
echo "============================================================"
