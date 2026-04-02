#!/usr/bin/env bash
# Dev setup for SnowClaw contributors — run from repo root after cloning.
set -euo pipefail

MIN_PYTHON="3.10"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Verify we're in the repo root ---
[ -f "pyproject.toml" ] || err "Run this script from the snowclaw repo root."

# --- Check Python ---
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    err "Python 3.10+ is required but not found."
fi

PY_MAJOR=$($PY -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PY -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Python >= $MIN_PYTHON required (found $PY_MAJOR.$PY_MINOR)."
fi

info "Found Python $PY_MAJOR.$PY_MINOR"

# --- Install pipx if missing ---
if ! command -v pipx &>/dev/null; then
    info "Installing pipx..."
    $PY -m pip install --user pipx 2>/dev/null || $PY -m pip install --break-system-packages --user pipx
    $PY -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
fi

# --- Editable install with dev extras ---
info "Installing snowclaw (editable + dev deps)..."
$PY -m pipx install --force -e ".[dev]"

# --- Verify ---
if ! command -v snowclaw &>/dev/null; then
    err "Installation failed — snowclaw not found on PATH. Try restarting your shell."
fi

info "Installed snowclaw $(snowclaw --version 2>/dev/null || echo '(version unknown)')"

# Check pytest is available via pipx runpip
if $PY -m pipx runpip snowclaw show pytest &>/dev/null; then
    info "pytest available — run tests with: $PY -m pipx run --spec . pytest"
else
    info "Warning: pytest not found in the snowclaw venv"
fi

echo ""
echo "Dev setup complete. Changes to snowclaw/ take effect immediately."
echo ""
echo "  Run the CLI:   snowclaw --version"
echo "  Run tests:     pipx run --spec . pytest"
