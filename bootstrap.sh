#!/usr/bin/env bash
# bootstrap.sh — One-command setup for investclosure
# Usage: ./bootstrap.sh [--no-docker] [--podman]
#
# What it does:
#   1. Creates .env from template (interactive if TWO_CAPTCHA_API_KEY missing)
#   2. Creates data directories
#   3. Installs Python deps (via pip or pipx)
#   4. Installs Playwright Chromium (if not using Docker/Podman)
#   5. Runs a test --list to verify setup

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}!${NC} $*"; }
fail()    { echo -e "${RED}✗${NC} $*"; exit 1; }

# Detect mode
DOCKER_MODE="docker"
if [[ "${1:-}" == "--no-docker" ]]; then
    RUN_MODE="native"
elif [[ "${1:-}" == "--podman" ]]; then
    RUN_MODE="podman"
    DOCKER_MODE="podman"
else
    RUN_MODE="docker"
fi

# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------
echo "─── Step 1/5: Environment ───"

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -n "Enter TWO_CAPTCHA_API_KEY (or press Enter to skip): "
        read -r key
        if [ -n "$key" ]; then
            sed -i "s/your_2captcha_api_key_here/$key/" .env
        fi
        info ".env created"
    else
        fail ".env.example not found"
    fi
else
    info ".env already exists, skipping"
fi

# ---------------------------------------------------------------------------
# 2. Data directories
# ---------------------------------------------------------------------------
echo "─── Step 2/5: Data directories ───"
mkdir -p data/data data/backups data/logs
# Check env vars override the default paths
DATA_DIR="${INVESTCLOSURE_DATA_DIR:-$SCRIPT_DIR/data}"
mkdir -p "$DATA_DIR" "$DATA_DIR/backups" "$DATA_DIR/logs"
info "Data dirs: $DATA_DIR"

# ---------------------------------------------------------------------------
# 3. Python dependencies
# ---------------------------------------------------------------------------
echo "─── Step 3/5: Python dependencies ───"

if [[ "$RUN_MODE" == "native" ]]; then
    # Check Python version
    python3 -V 2>&1 || fail "python3 not found"

    # Virtual env if available
    if [ -d .venv ]; then
        VIRTUALENV=".venv/bin/python"
        if [ ! -x "$VIRTUALENV" ]; then VIRTUALENV="python3"; fi
    else
        if command -v python3 -m venv &>/dev/null; then
            python3 -m venv .venv
            VIRTUALENV=".venv/bin/python"
        else
            VIRTUALENV="python3"
        fi
    fi

    # Install deps
    if command -v pipx &>/dev/null; then
        pipx ensurepath
    fi

    $VIRTUALENV -m pip install -r requirements.txt
    info "Python deps installed"
else
    info "Skipping Python deps (${DOCKER_MODE} mode — handled in Dockerfile)"
fi

# ---------------------------------------------------------------------------
# 4. Playwright Chromium
# ---------------------------------------------------------------------------
echo "─── Step 4/5: Playwright Chromium ───"

if [[ "$RUN_MODE" == "native" ]]; then
    PLAYWRIGHT_DIR="${HOME}/.cache/ms-playwright"
    if ls "$PLAYWRIGHT_DIR"/*/chrome-linux64/chrome &>/dev/null 2>&1; then
        info "Chromium already installed at $PLAYWRIGHT_DIR"
    else
        warn "Installing Playwright Chromium (this may take a few minutes)..."
        $VIRTUALENV -m playwright install chromium
        $VIRTUALENV -m playwright install-deps chromium
        info "Chromium installed"
    fi
else
    info "Skipping Chromium install (${DOCKER_MODE} handles this)"
fi

# ---------------------------------------------------------------------------
# 5. Verification
# ---------------------------------------------------------------------------
echo "─── Step 5/5: Verification ───"

if [[ "$RUN_MODE" == "native" ]]; then
    $VIRTUALENV -m scraper --list
    info "Setup complete! Run: $VIRTUALENV -m scraper"
else
    $DOCKER_MODE compose build
    info "${DOCKER_MODE^} image built. Run: $DOCKER_MODE compose up"
fi

echo ""
echo "─── Done ───"
echo "  Docker:   docker compose up"
echo "  Podman:   podman compose up"
echo "  Native:   python3 -m scraper"
