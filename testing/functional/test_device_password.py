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


def _which(name: str) -> bool:
    """Return whether *name* is on PATH.

    Checked via the absolute-ish ``command -v`` in a LOGIN shell rather than
    ``sudo -n which``: several of these tools live in /usr/sbin, which is not on
    PATH in a non-login non-interactive ssh shell, so a bare `which` reports a
    perfectly present binary as missing.
    """
    result = subprocess.run(
        ["bash", "-lc", f"command -v {name}"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0


def _samba_installed() -> bool:
    """Whether the tools needed to CHANGE and VERIFY a Samba password exist.

    Deliberately checks both: the old version only checked ``smbpasswd``, so on
    a device without ``smbclient`` the change succeeded but the verification
    could not run — and reported that as a failure rather than a skip.
    """
    return _which("smbpasswd") and _which("smbclient")


def _verify_console_password(password: str) -> bool:
    """Verify the console password against /etc/shadow.

    Checked by comparing the stored hash with one produced from *password*.

    Two traps this avoids, both verified on the device:

    * ``su -c true`` reads the password from the CONTROLLING TERMINAL, not
      stdin, so the old ``printf 'pw\\n' | su ...`` check never authenticated
      at all — it could pass or fail for reasons unrelated to the password.
    * Python CANNOT do this here.  The ``crypt`` module was removed from the
      stdlib in 3.13 (PEP 594) and the device runs 3.13.5, and the hash is
      yescrypt (``$y$``) which ``openssl passwd`` does not support either.

    So we shell out to ``perl``: its ``crypt()`` calls the platform crypt(3),
    which DOES handle yescrypt, and perl ships with Raspberry Pi OS — no new
    dependency.  Runs as root because /etc/shadow is not world-readable.
    """
    script = "my ($pw, $stored) = @ARGV; exit(crypt($pw, $stored) eq $stored ? 0 : 1);"
    stored = _read_console_hash()
    if not stored:
        raise AssertionError(
            f"could not read the /etc/shadow hash for {DEVICE_USER} — "
            "cannot verify the console password"
        )
    result = _run_sudo(["perl", "-e", script, password, stored])
    return result.returncode == 0


def _read_console_hash() -> str | None:
    """Return the user's stored ``/etc/shadow`` hash, or ``None``.

    Via ``getent shadow`` rather than reading the file, so the same call works
    regardless of the shadow backend (files, LDAP, …).
    """
    result = _run_sudo(["getent", "shadow", DEVICE_USER])
    if result.returncode != 0:
        return None
    # getent shadow: name:hash:lastchange:...
    parts = result.stdout.strip().split(":")
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


def _verify_samba_password(password: str) -> bool:
    """Verify the Samba password by authenticating with ``smbclient``."""
    try:
        result = subprocess.run(
            ["smbclient", "-L", "localhost", "-U", f"{DEVICE_USER}%{password}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except FileNotFoundError:
        # Not a wrong password — a missing verifier.  The caller must have
        # checked availability first (see _samba_installed); reaching here means
        # the environment is wrong, so fail loudly rather than returning a
        # verdict we cannot actually make.
        raise AssertionError(
            "smbclient is not installed — cannot verify the Samba password "
            "(it is required by requirements-system.txt)"
        ) from None


#: Password restored after the test.  This mirrors the image default AND the
#: value ``reconcile.sh`` seeds the Samba account with on a fresh device, so
#: "restore" leaves both stores consistent.
#:
#: It is intentionally a FIXED constant rather than a value read back from
#: /etc/shadow: Samba keeps its own separate hash which can only be set from
#: PLAINTEXT, so restoring /etc/shadow's crypt hash would fix the console and
#: leave Samba on the throwaway password — a silent desync, which is the very
#: thing this module exists to detect.
RESTORE_PASSWORD = "raspberry"

RESTORE_WARNING = (
    "DEVICE PASSWORD CHANGED TO {pw!r} BY THIS TEST — restoring it requires a "
    "working chpasswd; if you see this message the restore FAILED and 'pi' is "
    "still on the test password. Recover with: sudo passwd pi"
)


def _restore_password() -> None:
    """Put the default device password back in BOTH stores.

    Best-effort by design, but it reports loudly if it did not take effect:
    silently leaving a device on a known test password is worse than a noisy
    failure.
    """
    console = _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:{RESTORE_PASSWORD}\n")
    _run_sudo(
        ["smbpasswd", "-a", "-s", DEVICE_USER],
        input=f"{RESTORE_PASSWORD}\n{RESTORE_PASSWORD}\n",
    )
    if console.returncode != 0 or not _verify_console_password(RESTORE_PASSWORD):
        print(RESTORE_WARNING.format(pw=TEST_PASSWORD))


def test_device_password_changes_both_stores(sudo_ok: bool) -> None:
    """chpasswd + smbpasswd must both succeed and stay in sync."""
    assert sudo_ok, "passwordless sudo is required"
    if not _samba_installed():
        pytest.skip("smbpasswd and smbclient are both required to change AND verify")

    # The password is a REAL credential for SSH and the SMB share, so restoring
    # it must not depend on the assertions below passing — a failure between the
    # change and the restore would otherwise leave the device holding a
    # well-known test password.  (That is exactly what happened: a smbclient
    # verification failure left `pi` set to MetixelTestPass123!.)  `finally`
    # guarantees the restore runs on failure, error, or success alike.
    try:
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
    finally:
        _restore_password()


def test_device_password_partial_failure_detected(sudo_ok: bool) -> None:
    """A failing smbpasswd after a successful chpasswd must be detectable.

    Simulates the partial-failure path by pointing smbpasswd at a non-existent
    user, which fails while chpasswd succeeds.  This mirrors the backend's
    explicit partial-state reporting.
    """
    assert sudo_ok, "passwordless sudo is required"
    if not _samba_installed():
        pytest.skip("smbpasswd and smbclient are both required to change AND verify")

    # Same leak risk as the first test: this mutates the real credential, so the
    # restore must be in `finally` rather than after the assertion.
    try:
        # chpasswd succeeds for a real user.
        console = _run_sudo(["chpasswd"], input=f"{DEVICE_USER}:{TEST_PASSWORD}\n")
        assert console.returncode == 0

        # smbpasswd fails for a non-existent user → partial state.
        samba = _run_sudo(
            ["smbpasswd", "-a", "-s", "no_such_user_xyz"],
            input=f"{TEST_PASSWORD}\n{TEST_PASSWORD}\n",
        )
        assert samba.returncode != 0, "smbpasswd should have failed for a non-existent user"
    finally:
        _restore_password()
