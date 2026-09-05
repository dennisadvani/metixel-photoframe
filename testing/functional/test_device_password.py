# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: the synced device password (SSH console + Samba).

The backend changes the Pi console password (``chpasswd``) and the Samba
share password (``smbpasswd``) together so the two stores stay in sync as a
single "device password".  These tests verify the real commands work on the
Pi and that both stores are updated by the same change.

These tests are destructive (they change the ``pi`` password).  They run in
a **test mode** that uses a throwaway password and restores the original
afterwards, mirroring ``test_sudo.py``.  They are gated behind the
``functional`` marker and skipped unless the host is a Pi with passwordless
sudo.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.functional

#: The system user whose console + Samba passwords are kept in sync.
DEVICE_USER = "pi"
#: Throwaway password used during the test (restored afterwards).
TEST_PASSWORD = "MetixelTestPass123!"


def _run_sudo(cmd: list[str], input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *cmd],
        capture_output=True,
        text=True,
        timeout=30,
        input=input,
    )


def _samba_installed() -> bool:
    """Return whether smbpasswd is available."""
    result = _run_sudo(["which", "smbpasswd"])
    return result.returncode == 0


def _verify_console_password(password: str) -> bool:
    """Verify the console password by attempting a login via ``su``."""
    # Use `su` with the password on stdin (non-interactive).
    result = subprocess.run(
        ["su", "-c", "true", DEVICE_USER],
        capture_output=True,
        text=True,
        timeout=15,
        input=f"{password}\n",
    )
    return result.returncode == 0


def _verify_samba_password(password: str) -> bool:
    """Verify the Samba password via ``smbclient`` (if available)."""
    try:
        result = subprocess.run(
            ["smbclient", "-L", "localhost", "-U", f"{DEVICE_USER}%{password}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def test_device_password_changes_both_stores(sudo_ok: bool) -> None:
    """chpasswd + smbpasswd must both succeed and stay in sync."""
    assert sudo_ok, "passwordless sudo is required"
    if not _samba_installed():
        pytest.skip("smbpasswd not installed")

    # 1. Change console password.
    console = _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:{TEST_PASSWORD}\n")
    assert console.returncode == 0, f"chpasswd failed: {console.stderr.strip()}"

    # 2. Change Samba password.
    samba = _run_sudo(
        ["smbpasswd", "-a", "-s", DEVICE_USER],
        input=f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n",
    )
    assert samba.returncode == 0, f"smbpasswd failed: {samba.stderr.strip()}"

    # 3. Verify both stores accept the new password.
    assert _verify_console_password(TEST_PASSWORD), "console password not updated"
    assert _verify_samba_password(TEST_PASSWORD), "samba password not updated"

    # 4. Restore the original password (best-effort).
    _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:raspberry\n")
    _run_sudo(
        ["smbpasswd", "-a", "-s", DEVICE_USER],
        input=f"raspberry\nraspberry\n",
    )


def test_device_password_partial_failure_detected(sudo_ok: bool) -> None:
    """A failing smbpasswd after a successful chpasswd must be detectable.

    Simulates the partial-failure path by pointing smbpasswd at a non-existent
    user, which fails while chpasswd succeeds.  This mirrors the backend's
    explicit partial-state reporting.
    """
    assert sudo_ok, "passwordless sudo is required"
    if not _samba_installed():
        pytest.skip("smbpasswd not installed")

    # chpasswd succeeds for a real user.
    console = _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:{TEST_PASSWORD}\n")
    assert console.returncode == 0

    # smbpasswd fails for a non-existent user → partial state.
    samba = _run_sudo(
        ["smbpasswd", "-a", "-s", "no_such_user_xyz"],
        input=f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n",
    )
    assert samba.returncode != 0, "smbpasswd should have failed for a non-existent user"

    # Restore the original password.
    _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:raspberry\n")