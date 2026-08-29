# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: passwordless sudo access on the Pi.

The backend relies on ``sudo -n`` (NOPASSWD) for privileged operations —
Wi-Fi/AP control (nmcli, iw, hostapd/dnsmasq), systemctl, timezone, reboot,
and shutdown.  These tests verify the ``pi`` user can actually run them
without a password prompt.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.functional


def _run_sudo(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_sudo_nopasswd_works(sudo_ok: bool) -> None:
    """The pi user must have passwordless sudo (sudo -n true)."""
    assert sudo_ok, "passwordless sudo is required (pi ALL=(ALL) NOPASSWD: ALL)"


@pytest.mark.parametrize(
    "cmd",
    [
        ["systemctl", "is-system-running"],
        ["nmcli", "-t", "general", "status"],
        ["ip", "link", "show"],
        ["timedatectl", "show", "-p", "Timezone"],
    ],
)
def test_sudo_privileged_commands(cmd: list[str]) -> None:
    """Common privileged commands must run under sudo -n without a prompt."""
    result = _run_sudo(cmd)
    assert result.returncode == 0, f"sudo -n {' '.join(cmd)} failed: {result.stderr.strip()}"


def test_sudo_iw_available() -> None:
    """iw (used to disable power-save for AP mode) must be present."""
    result = _run_sudo(["iw", "--version"])
    assert result.returncode == 0, "iw is not installed"


def test_sudo_hostapd_dnsmasq_units_present() -> None:
    """The AP units must exist (even if not running)."""
    for unit in ("hostapd.service", "dnsmasq.service"):
        result = _run_sudo(["systemctl", "list-unit-files", unit])
        assert result.returncode == 0
        assert unit in result.stdout, f"{unit} is not installed"
