#!/usr/bin/env bash
# SnowClaw installer — curl -fsSL https://raw.githubusercontent.com/JacobScott98/SnowClaw/main/install.sh | bash
set -euo pipefail

REPO="https://github.com/JacobScott98/SnowClaw.git"
INSTALL_DIR="${SNOWCLAW_DIR:-$HOME/.snowclaw}"
MIN_PYTHON="3.10"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- Check git ---
command -v git &>/dev/null || err "git is required but not found."

# --- Check Docker container runtime ---
# SnowClaw uses `docker compose` for `snowclaw dev` and image builds for deploy,
# so a working Docker daemon is required up front. On macOS we recommend OrbStack;
# Linux users typically have Docker Engine installed directly.
if ! command -v docker &>/dev/null; then
    OS="$(uname -s)"
    echo "" >&2
    printf '\033[1;31merror:\033[0m Docker is required but no `docker` CLI was found.\n' >&2
    echo "" >&2
    echo "SnowClaw needs a running Docker container runtime to build and run images." >&2
    echo "Install one of the following, then re-run this installer:" >&2
    echo "" >&2
    if [ "$OS" = "Darwin" ]; then
        echo "  OrbStack (recommended on macOS):" >&2
        echo "    brew install --cask orbstack" >&2
        echo "" >&2
        echo "  Alternatives:" >&2
        echo "    brew install --cask docker        # Docker Desktop" >&2
        echo "    brew install colima docker        # Colima + docker CLI" >&2
    else
        echo "  Docker Engine (Linux):" >&2
        echo "    https://docs.docker.com/engine/install/" >&2
        echo "" >&2
        echo "  Or Docker Desktop for Linux:" >&2
        echo "    https://docs.docker.com/desktop/install/linux-install/" >&2
    fi
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "" >&2
    printf '\033[1;31merror:\033[0m The `docker` CLI is installed but the Docker daemon is not responding.\n' >&2
    echo "" >&2
    echo "Start your container runtime (e.g. OrbStack, Docker Desktop, or Colima) and re-run this installer." >&2
    echo "  OrbStack:        open -a OrbStack" >&2
    echo "  Docker Desktop:  open -a Docker" >&2
    echo "  Colima:          colima start" >&2
    exit 1
fi

info "Found Docker runtime ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'version unknown'))"

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
if ! $PY -m pipx --version &>/dev/null; then
    info "Installing pipx..."
    $PY -m pip install --user pipx 2>/dev/null || $PY -m pip install --break-system-packages --user pipx
    $PY -m pipx ensurepath
    export PATH="$HOME/.local/bin:$PATH"
    $PY -m pipx --version &>/dev/null || err "pipx installed but not importable by $PY. Try: $PY -m pip install pipx"
fi

info "Using pipx via: $PY -m pipx"

# --- Migrate old install location ---
OLD_DIR="$HOME/snowclaw"
if [ -z "${SNOWCLAW_DIR:-}" ] && [ -d "$OLD_DIR/.git" ] && [ ! -d "$INSTALL_DIR" ]; then
    info "Migrating install directory from ~/snowclaw to ~/.snowclaw..."
    mv "$OLD_DIR" "$INSTALL_DIR"
fi

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
$PY -m pipx install --force -e "$INSTALL_DIR"

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
