# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Dev Environment Setup
#
# Installs development & testing tools on top of an existing Metixel install.
# Run this after setup_trixie.sh (or on a dev desktop) to add:
#   pytest, pytest-cov, ruff, mypy, pygame
#
# Usage:
#   sudo bash /opt/metixel/scripts/setup_dev_env.sh
# =============================================================================

set -euo pipefail

# -- Detect project root -----------------------------------------------------
if [ -f "/opt/metixel/scripts/setup_dev_env.sh" ]; then
    METIXEL_DIR="/opt/metixel"
else
    METIXEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

echo "=== Metixel Photoframe — Dev Environment Setup ==="
echo "Project root: ${METIXEL_DIR}"
echo ""

# -- System packages (dev tools) ---------------------------------------------
echo "[1/2] Installing system packages for dev tools..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-pygame

# -- Python dev packages -----------------------------------------------------
echo "[2/2] Installing Python dev packages..."
cd "${METIXEL_DIR}"

PIP_IGNORE="--break-system-packages --ignore-installed"

# Install dev tools: pytest, pytest-cov, ruff, mypy
# pygame is provided by apt above; pip install as fallback
sudo pip3 install ${PIP_IGNORE} pytest pytest-cov ruff mypy 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pytest pytest-cov ruff mypy

# -- Verify ------------------------------------------------------------------
echo ""
echo "=== Dev environment setup complete ==="
echo ""
echo "Installed versions:"
echo -n "  pytest  : "; python3 -m pytest --version 2>/dev/null || echo "(not found)"
echo -n "  ruff    : "; ruff --version 2>/dev/null || echo "(not found)"
echo -n "  mypy    : "; mypy --version 2>/dev/null || echo "(not found)"
echo ""
echo "You can now run:"
echo "  cd ${METIXEL_DIR} && python -m pytest tests/ -v"
echo "  ruff check metixel/"
echo "  mypy metixel/"
