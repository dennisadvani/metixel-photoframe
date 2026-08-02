# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#!/bin/bash
# =============================================================================
# Metixel Photoframe — Dev Environment Setup
#
# Installs development & testing tools on top of an existing Metixel install.
# Run this after setup_trixie.sh (or on a dev desktop) to add:
#   pytest, pytest-cov, ruff, mypy
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

# -- Python dev packages -----------------------------------------------------
echo "[1/3] Installing Python dev packages..."
cd "${METIXEL_DIR}"

PIP_IGNORE="--break-system-packages --ignore-installed"

# Install dev tools: pytest, pytest-cov, ruff, mypy
sudo pip3 install ${PIP_IGNORE} pytest pytest-cov ruff mypy 2>/dev/null || \
    pip3 install ${PIP_IGNORE} pytest pytest-cov ruff mypy

# -- Samba share (full project) ---------------------------------------------
# Adds a [metixel] share for the entire /opt/metixel tree — useful during
# development when you need to edit code, configs, and scripts over the
# network.  The production setup_trixie_metixel.sh creates a separate
# [metixel-media] share scoped to /opt/metixel/media only; the two shares
# can coexist without conflict.
echo "[2/3] Configuring Samba share (/opt/metixel as 'metixel')..."
SMB_CONF="/etc/samba/smb.conf"
if ! grep -q '\[metixel\]' "${SMB_CONF}" 2>/dev/null; then
    sudo tee -a "${SMB_CONF}" > /dev/null <<'SMBEOF'
[metixel]
   comment = Metixel Photoframe — Full Project (Dev)
   path = /opt/metixel
   browseable = yes
   read only = no
   guest ok = no
   valid users = pi
   create mask = 0664
   directory mask = 0775
   force user = pi
   force group = pi
SMBEOF
    sudo systemctl restart smbd
    echo "  Added [metixel] share → /opt/metixel"
else
    echo "  [metixel] share already present — skipping."
fi

# -- Verify ------------------------------------------------------------------
echo ""
echo "[3/3] Verifying installed tools..."
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
