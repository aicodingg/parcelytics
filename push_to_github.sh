#!/usr/bin/env bash
# Push parcelytics to GitHub
# Run once from the parcel_app directory: bash push_to_github.sh
set -euo pipefail

REPO_NAME="parcelytics"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "============================================================"
echo " Parcelytics — GitHub Push"
echo "============================================================"

# ── 1. Install gh CLI if needed ───────────────────────────────────
if ! command -v gh &>/dev/null; then
  echo "[1/5] Installing GitHub CLI via Homebrew…"
  brew install gh
else
  echo "[1/5] GitHub CLI already installed."
fi

# ── 2. Authenticate with GitHub (browser flow) ───────────────────
echo "[2/5] Checking GitHub authentication…"
if ! gh auth status &>/dev/null; then
  echo "  Opening browser for GitHub login…"
  gh auth login --web --git-protocol https
else
  echo "  Already authenticated."
  gh auth status
fi

# ── 3. Initialize git repo ────────────────────────────────────────
echo "[3/5] Initializing git repository…"
if [ ! -d ".git" ]; then
  git init
  git branch -M main
  echo "  Git initialized."
else
  echo "  Git already initialized."
fi

# ── 4. Create GitHub repo and add remote ─────────────────────────
echo "[4/5] Creating GitHub repository '$REPO_NAME'…"
if gh repo view "$REPO_NAME" &>/dev/null 2>&1; then
  echo "  Repo already exists on GitHub."
else
  gh repo create "$REPO_NAME" --public --description "Travis County Property Tax Intelligence Platform" --source=. --remote=origin
  echo "  Repo created."
fi

# Ensure remote is set
if ! git remote get-url origin &>/dev/null 2>&1; then
  GH_USER=$(gh api user --jq .login)
  git remote add origin "https://github.com/$GH_USER/$REPO_NAME.git"
fi

# ── 5. Commit and push ────────────────────────────────────────────
echo "[5/5] Committing and pushing…"
git add .
git status --short

git commit -m "Initial commit — Parcelytics Property Tax Platform

- PostgreSQL schema (6 tables: parcel, parcel_tax_year, tax_billing,
  tax_billing_entity, tax_delinquent, county_tax_rate)
- ETL loaders for TCAD AJR (2021-2024), 2025 Certified Export,
  Travis County Tax Office billing/delinquency data, and tax rates
- Flask web app with parcel search, 5-year history, investor insight
  report, 5-year tax projection, and tax rate trend explorer
- Bootstrap 5 + Chart.js frontend" || echo "  Nothing new to commit."

git push -u origin main

echo ""
echo "============================================================"
GH_USER=$(gh api user --jq .login)
echo " Done! Repo live at: https://github.com/$GH_USER/$REPO_NAME"
echo "============================================================"
