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

Verification deliberately avoids the network.  Both stores are checked by
reading back what was stored and comparing a locally-recomputed hash: the
console against ``/etc/shadow`` (via ``perl``'s ``crypt``) and Samba against
its passdb (via ``pdbedit -w`` + MD4).  An earlier version authenticated over
SMB with ``smbclient -L``, which required a TCP connection and ended in an
``srvsvc`` RPC bind that is unrelated to the credential and intermittently
hangs — see :func:`_verify_samba_password_in_passdb`.
"""

from __future__ import annotations

import struct
import subprocess
import time

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
    """Whether the tools needed to CHANGE a Samba password exist.

    ``smbpasswd`` is the only hard requirement: it is what *changes* the
    password, and ``pdbedit`` (the passdb reader used by
    :func:`_verify_samba_password`) ships in ``samba-common-bin`` alongside it.

    ``smbclient`` is deliberately NOT required here.  It used to be, but the
    live-connect check it backs is now a *secondary*, best-effort signal — see
    the note on ``_verify_samba_password``.  Gating the whole test on it turned
    a missing optional client into a hard failure.
    """
    return _which("smbpasswd")


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


#: Attempts for the secondary live-connect check, and the pause between them.
#:
#: ``smbclient -L`` does more than read the passdb: as its FINAL step it binds
#: the ``srvsvc`` RPC pipe to list servers in the workgroup.  That bind is
#: irrelevant to Metixel's share and is intermittently slow/absent, and when it
#: stalls smbclient emits ``cli_rpc_pipe_open_noauth: rpc_pipe_bind for pipe
#: srvsvc failed … NT_STATUS_IO_TIMEOUT`` and hangs.  Measured on a Pi 5 running
#: Trixie with Samba 4.22.11: the cold ``srvsvc``/``rpcd_classic`` spawn costs
#: only ~470 ms, so the stall is a rare environmental hiccup, not a device fault
#: — hence a retry rather than a long single wait.
_SMBCLIENT_ATTEMPTS = 3
_SMBCLIENT_RETRY_PAUSE_S = 2.0
#: Per-attempt timeout.  Comfortably above the ~0.5 s healthy case so a retry is
#: only spent on a genuine stall, not on a merely slow machine.
_SMBCLIENT_TIMEOUT_S = 20


def _md4(data: bytes) -> bytes:
    """Compute MD4, the primitive behind Samba's NT password hash.

    Implemented here rather than imported because **neither OpenSSL 3 nor
    Python 3.13 provides it any more**: ``hashlib.new("md4")`` raises
    ``UnsupportedDigestmodError`` (verified on the Pi, which runs 3.13.5), and
    ``hashlib.algorithms_available`` does not list it.  Adding a PyPI MD4
    dependency for a functional test would be worse than 30 lines of RFC 1320.

    Self-tested against the RFC's published vectors in
    :func:`_verify_samba_password_in_passdb`'s caller path — see
    ``test_md4_matches_published_vectors``.
    """

    def rol(x: int, n: int) -> int:
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    msg = bytearray(data)
    bit_len = len(data) * 8
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", bit_len)

    for offset in range(0, len(msg), 64):
        x = list(struct.unpack("<16I", msg[offset : offset + 64]))
        a, b, c, d = h
        # Round 1: F(x,y,z) = (x AND y) OR (NOT x AND z)
        for i in range(16):
            k = i
            shift = [3, 7, 11, 19][i % 4]
            a = rol((a + ((b & c) | (~b & d)) + x[k]) & 0xFFFFFFFF, shift)
            a, b, c, d = d, a, b, c
        # Round 2: G(x,y,z) = (x AND y) OR (x AND z) OR (y AND z)
        for i in range(16):
            k = (i % 4) * 4 + (i // 4)
            shift = [3, 5, 9, 13][i % 4]
            a = rol((a + ((b & c) | (b & d) | (c & d)) + x[k] + 0x5A827999) & 0xFFFFFFFF, shift)
            a, b, c, d = d, a, b, c
        # Round 3: H(x,y,z) = x XOR y XOR z
        for i in range(16):
            k = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15][i]
            shift = [3, 9, 11, 15][i % 4]
            a = rol((a + (b ^ c ^ d) + x[k] + 0x6ED9EBA1) & 0xFFFFFFFF, shift)
            a, b, c, d = d, a, b, c
        for index, value in enumerate((a, b, c, d)):
            h[index] = (h[index] + value) & 0xFFFFFFFF
    return struct.pack("<4I", *h)


def _nt_hash(password: str) -> str:
    """The stored NT hash for *password*: unsalted ``MD4(UTF-16LE(pw))``.

    Samba stores this (it is the ``LMHASH:NT HASH:…`` second field) and it is
    unsalted, so it can be recomputed locally from the plaintext and compared
    with what the passdb holds.
    """
    return _md4(password.encode("utf-16-le")).hex().upper()


def _read_samba_nt_hash(password: str | None = None) -> str | None:
    """Return the stored NT hash for :data:`DEVICE_USER`, or ``None``.

    ``pdbedit -w`` prints the passdb entry in the classic ``smbpasswd`` style::

        pi:1000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:CECADC1061009AEDACC80A2DE584A5F5:[U]:…

    i.e. ``name:uid:lmhash:nthash:[flags]:…``.  The ``X``s are LM hashes
    deliberately disabled — the NT hash is the field we want.
    """
    if not _which("pdbedit"):
        return None
    result = _run_sudo(["pdbedit", "-w", "-u", DEVICE_USER])
    if result.returncode != 0:
        return None
    # name:uid:lmhash:nthash:…
    parts = result.stdout.strip().split(":")
    if len(parts) < 4 or not parts[3]:
        return None
    return parts[3].strip().upper()


def _verify_samba_password_in_passdb(password: str) -> bool | None:
    """Verify the password against Samba's own passdb, with no network at all.

    This is the PRIMARY check, because it is deterministic.  It reads the stored
    NT hash via ``pdbedit -w`` and compares it with the hash recomputed from the
    plaintext — no TCP connection, no SMB session, no RPC, and nothing that can
    be slow or unavailable on a loaded device.

    That matters here because ``smbclient -L`` (the old check) finishes by
    binding the ``srvsvc`` RPC pipe to browse the workgroup, which has nothing to
    do with Metixel's share and was observed hanging on a fresh Trixie install::

        cli_rpc_pipe_open_noauth: rpc_pipe_bind for pipe srvsvc failed
        with error NT_STATUS_IO_TIMEOUT

    Returns ``None`` (not ``False``) when the check cannot run at all — no
    ``pdbedit``, or no readable passdb entry — so the caller can fall back to a
    live authentication instead of reporting a wrong password it never tested.
    """
    stored = _read_samba_nt_hash()
    if stored is None:
        return None
    return _nt_hash(password) == stored


def _verify_samba_password_live(password: str) -> bool:
    """Verify by actually authenticating over SMB with ``smbclient``.

    SECONDARY / best-effort only.  It proves the credential works end-to-end
    through the real SMB stack, which reading the passdb cannot — but
    ``smbclient -L`` also does an unrelated ``srvsvc`` workgroup browse that can
    stall (see ``_SMBCLIENT_ATTEMPTS``), so this is retried and its failure never
    decides the test on its own.

    Returns ``False`` if ``smbclient`` is simply not installed — the caller must
    not treat an absent optional client as a wrong password.
    """
    if not _which("smbclient"):
        return False
    for attempt in range(1, _SMBCLIENT_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                ["smbclient", "-L", "localhost", "-U", f"{DEVICE_USER}%{password}"],
                capture_output=True,
                text=True,
                timeout=_SMBCLIENT_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        except subprocess.TimeoutExpired:
            # A stalled srvsvc bind, not a verdict on the password.  Retry.
            if attempt == _SMBCLIENT_ATTEMPTS:
                return False
            time.sleep(_SMBCLIENT_RETRY_PAUSE_S)
            continue
        # Judge ONLY on the exit status.  smbclient writes
        # "SMB1 disabled -- no workgroup available" to stderr even on a
        # perfectly successful listing, so stderr content is never a failure
        # signal here.
        return result.returncode == 0
    return False


def _verify_samba_password(password: str) -> bool:
    """Verify the Samba password, preferring the deterministic passdb check.

    The passdb check answers the question the test actually asks — "did the
    stored Samba credential become this password?" — with no network or RPC
    involvement.  A live SMB authentication is then used only as a fallback,
    because its final workgroup-browse step is unrelated to the credential and
    can hang on a fresh install.
    """
    in_passdb = _verify_samba_password_in_passdb(password)
    if in_passdb is not None:
        return in_passdb
    # Could not read the passdb — fall back to a real authentication.
    return _verify_samba_password_live(password)


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


def test_md4_matches_published_vectors() -> None:
    """The hand-rolled MD4 must match RFC 1320's published test vectors.

    :func:`_md4` backs the Samba verification, and ``hashlib`` cannot supply MD4
    on Python 3.13 (OpenSSL 3 dropped it), so the implementation is local and
    needs its own guard.  A broken round, rotation, or padding rule changes
    these values.

    Pure computation — no Pi, no sudo, no network — so it runs everywhere.
    """
    assert _md4(b"").hex() == "31d6cfe0d16ae931b73c59d7e0c089c0"
    assert _md4(b"abc").hex() == "a448017aaf21d8525fc10ae87aa6729d"
    assert _md4(b"message digest").hex() == "d9130a8164549fe818874806e1c7014b"
    # Multi-block input, to exercise the padding + length path.
    assert _md4(b"1234567890" * 8).hex() == "e33b4ddc9c38f2199c3e7b164fcc0536"


def test_nt_hash_matches_the_value_samba_stores() -> None:
    """``_nt_hash`` must produce exactly what Samba stores for a password.

    The expected value was read from a real passdb on the device with
    ``pdbedit -w -u pi``, which prints
    ``pi:1000:XXXX…:CECADC1061009AEDACC80A2DE584A5F5:…`` for the default
    ``raspberry``.  This pins the ``UTF-16LE`` encoding and the upper-casing —
    each easy to get wrong, and either would silently make every Samba
    verification fail.

    Pure computation, so it runs everywhere.
    """
    assert _nt_hash(RESTORE_PASSWORD) == "CECADC1061009AEDACC80A2DE584A5F5"
    assert _nt_hash(TEST_PASSWORD) != _nt_hash(RESTORE_PASSWORD)


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
