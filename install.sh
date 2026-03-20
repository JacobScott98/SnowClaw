#!/usr/bin/env bash
# SnowClaw installer — curl -fsSL https://raw.githubusercontent.com/JacobScott98/SnowClaw/main/install.sh | bash
set -euo pipefail

REPO="https://github.com/JacobScott98/SnowClaw.git"
INSTALL_DIR="${SNOWCLAW_DIR:-$HOME/snowclaw}"
MIN_PYTHON="3.10"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Check git ---
command -v git &>/dev/null || err "git is required but not found."

# --- Check Python ---
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    err "Python 3.10+ is required but not found. Install it from https://python.org"
fi

PY_VERSION=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PY -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PY -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Python >= $MIN_PYTHON required (found $PY_VERSION). Please upgrade."
fi

info "Found Python $PY_VERSION"

# --- Install pipx if missing ---
if ! command -v pipx &>/dev/null; then
    info "Installing pipx..."
    $PY -m pip install --user pipx 2>/dev/null || $PY -m pip install --break-system-packages --user pipx
    $PY -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
fi

info "Using pipx at $(command -v pipx)"

# --- Clone or update repo ---
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing repo at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning snowclaw to $INSTALL_DIR..."
    git clone "$REPO" "$INSTALL_DIR"
fi

# --- Install CLI from local checkout ---
info "Installing snowclaw CLI..."
pipx install --force -e "$INSTALL_DIR"

# --- Verify ---
if command -v snowclaw &>/dev/null; then
    info "Installed snowclaw $(snowclaw --version 2>/dev/null || echo '(version unknown)')"
    echo ""
    echo "Get started:"
    echo "  mkdir my-openclaw && cd my-openclaw"
    echo "  snowclaw setup"
else
    err "Installation failed — snowclaw not found on PATH. Try restarting your shell."
fi
