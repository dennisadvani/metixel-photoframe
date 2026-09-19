# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: passwordless sudo access on the Pi.

The backend relies on ``sudo -n`` (NOPASSWD) for privileged operations —
Wi-Fi/AP control (nmcli, iw, hostapd/dnsmasq), systemctl, timezone, reboot,
and shutdown.  These tests verify the ``pi`` user can actually run them
without a password prompt.

This is the ONE module the suite deliberately does not skip when the host is
a Pi whose sudo prerequisite is unmet (see ``pytest_collection_modifyitems``).
Gating these tests behind the condition they assert is what let a
passwordless-sudo-less device report 50 green "skipped" tests instead of a
single red failure.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.functional

#: The exact remediation shown on failure — a bare assertion message left the
#: operator guessing at scope, and the wrong scope (blanket NOPASSWD) is a
#: security decision that should not be inferred from a test failure.
_SUDO_HELP = (
    "passwordless sudo is required for the 'pi' user, e.g.\n"
    "    echo 'pi ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/010_pi-nopasswd\n"
    "    sudo chmod 440 /etc/sudoers.d/010_pi-nopasswd\n"
    "Verify with: sudo -n true && echo OK"
)


def _run_sudo(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_sudo_nopasswd_works() -> None:
    """The pi user must have passwordless sudo (``sudo -n true``).

    Asserts directly rather than taking the ``sudo_ok`` fixture: the fixture
    is truthful, but a failing assertion here names the cause, whereas a
    fixture-consumed boolean hides which command actually failed.
    """
    result = _run_sudo(["true"])
    assert result.returncode == 0, (
        f"sudo -n true failed (rc={result.returncode}): {result.stderr.strip()}\n{_SUDO_HELP}"
    )


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
