# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the OTA update script builder.

These guard against regressions where an OTA upgrade applies the new code but
fails to install/update its Python dependencies — e.g. the bug where
``pillow-heif`` (needed for HEIC/HEIF media) was never installed because the
OTA pip step only ran ``pip install -e .`` and never installed
``requirements-pip.txt``.
"""

from __future__ import annotations

from metixel.backend.update_manager import UpdateManager


class TestBuildUpdateScript:
    def test_installs_runtime_pip_dependencies(self) -> None:
        """The update script must install requirements-pip.txt so new/changed
        runtime deps (e.g. pillow-heif) are applied on upgrade."""
        script = UpdateManager._build_update_script("/opt/metixel", "v1.0.0", "stable")

        assert "requirements-pip.txt" in script
        assert '-r "$REPO/requirements-pip.txt"' in script
        # The package reinstall step is still present.
        assert 'pip install --break-system-packages -e "$REPO"' in script
        # Services are still restarted.
        assert "systemctl restart metixel-backend metixel-cage" in script

    def test_quotes_external_values(self) -> None:
        """Externally-influenced values (ref/channel) are shell-quoted so they
        can't break out of the embedded bash string literals."""
        script = UpdateManager._build_update_script("/opt/metixel", "ref-with'quotes", "stable")

        # shlex.quote() wraps the apostrophe in a quoted splice, so the raw
        # contiguous value must not appear in the script.
        assert "ref-with'quotes" not in script
