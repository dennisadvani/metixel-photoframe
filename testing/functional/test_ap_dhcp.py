# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: the AP actually serves DHCP (not just beacons).

WHY THIS FILE EXISTS
--------------------
The 1.2.4/1.2.5 DHCP bug shipped because every existing test asked "is the AP
UP?" and none asked "can a client USE it?".  ``test_ap.py`` asserted hostapd was
active, wlan0 was in AP mode, and 192.168.42.1 was assigned — and **all three
passed on a frame that handed out no addresses at all**, because the AP settings
in ``/etc/dnsmasq.d/`` were never read (Debian's stock ``/etc/dnsmasq.conf``
ends with its own ``conf-dir=`` line, so the sidecar was sourced last and its
values lost to the defaults above it).

A running dnsmasq proves nothing: with no effective ``dhcp-range`` it still
starts as a plain DNS forwarder and still exits 0.  These tests therefore assert
the three things that distinguish a *working* AP from a *broadcasting* one:

  1. dnsmasq parsed a DHCP range for the AP subnet (from its own journal — the
     effective config, not what a file happens to say);
  2. it is bound to UDP/67, so a client's DISCOVER can actually be answered;
  3. an actual DHCP exchange completes and yields a 192.168.42.x lease.

(3) is the real regression test.  It is the only one that fails for *every*
possible cause — wrong ordering, missing sidecar, `interface=` bound to a
nonexistent device, dnsmasq-base-but-no-conffile — instead of just the one we
happened to find.

These run ON the Pi.  Like ``test_ap.py`` they start the AP, which takes wlan0
out of client mode, so they run in the AP-style invocation (see
``scripts/run_functional_tests.sh``) and Ethernet control is unaffected.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from metixel.backend import network_manager as nm

#: Directories that hold the tools this suite needs, in probe order.
#:
#: ``shutil.which`` alone is NOT enough here.  The functional suite runs over a
#: non-login ssh shell whose PATH is ``/usr/local/bin:/usr/bin:/bin:/usr/games``
#: — no ``/usr/sbin`` or ``/sbin``.  Both dnsmasq and dhcpcd live there, so a
#: bare ``which`` reports them MISSING and this suite skipped its single most
#: important test (``test_client_obtains_a_lease``) on a perfectly healthy
#: device.  That is the same ``/usr/sbin``-not-on-PATH trap that caused the
#: original dnsmasq AP bug, in its third appearance.
_SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin", "/usr/local/bin", "/usr/bin", "/bin")


def _resolve(tool: str) -> str | None:
    """Absolute path to *tool*, searching sbin dirs that PATH omits.

    Returns the bare name if it is on PATH, else the first absolute hit, else
    ``None``.  Preferring PATH keeps the usual semantics when PATH is complete.
    """
    found = shutil.which(tool)
    if found:
        return found
    for d in _SBIN_DIRS:
        candidate = f"{d}/{tool}"
        try:
            if (
                subprocess.run(["test", "-x", candidate], capture_output=True, timeout=5).returncode
                == 0
            ):
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return None


pytestmark = pytest.mark.functional

#: How long to wait for hostapd/dnsmasq to come up after start_ap_mode().
_AP_WAIT = 30

#: Interface used for the test client.  A veth pair gives a real "client" on the
#: AP subnet without needing a second Wi-Fi card or leaving the Pi.
# Interface names are capped at IFNAMSIZ (16 bytes INCLUDING the NUL), so 15
# usable characters.  The previous names ("metixel-dhcp-test0", 18 chars) were
# rejected by the kernel with `"name" not a valid ifname`, which made the veth
# fixture fail at setup — this test could never have run, let alone passed.
_CLIENT_IF = "mx-dhcp-cli"
_SERVER_IF = "mx-dhcp-srv"
assert max(len(_CLIENT_IF), len(_SERVER_IF)) <= 15, "ifname exceeds IFNAMSIZ"


def _run(cmd: list[str], sudo: bool = False) -> subprocess.CompletedProcess[str]:
    full = ["sudo", "-n", *cmd] if sudo else cmd
    return subprocess.run(full, capture_output=True, text=True, timeout=60)


def _dnsmasq_journal() -> str:
    """The current dnsmasq boot's journal, as the daemon itself reported it."""
    return _run(
        ["journalctl", "-u", nm.DNSMASQ_UNIT, "-n", "60", "--no-pager", "-o", "cat"],
        sudo=True,
    ).stdout


def _hostapd_ssid() -> str:
    """The SSID hostapd is actually broadcasting."""
    result = _run(["/usr/sbin/iw", "dev", "wlan0", "info"])
    match = re.search(r"^\s*ssid\s+(.+)$", result.stdout, re.MULTILINE)
    return match.group(1).strip() if match else ""


@pytest.fixture(scope="module")
def ap_up() -> str:
    """Start the AP once for this module and return an error string ("" = ok).

    ``start_ap_mode()`` is now itself the first line of defence: it refuses to
    start when it cannot see a usable dhcp-range, so a failure here is already
    meaningful rather than a silent half-start.
    """
    if not nm.start_ap_mode():
        pytest.fail(
            "start_ap_mode() returned False — either the AP is genuinely broken "
            "or dnsmasq is missing its dhcp-range (both are the bug this file "
            "guards). Check: sudo journalctl -u dnsmasq -n 30"
        )
    deadline = time.monotonic() + _AP_WAIT
    while time.monotonic() < deadline:
        if nm.is_ap_mode_active():
            break
        time.sleep(2)
    else:
        pytest.fail("hostapd did not become active in time")
    # dnsmasq is started after wlan0 has its AP address.
    time.sleep(2)
    return ""


class TestDnsmasqConfigurationIsEffective:
    """The config must be *parsed*, not merely present somewhere on disk.

    These are the cheap checks that would have failed loudly on the broken
    frame, and they name the cause instead of only the symptom.
    """

    def test_dhcp_range_is_effective(self, ap_up: str) -> None:
        """dnsmasq must report a DHCP range for the AP subnet.

        Read from the daemon's own journal on purpose.  Asserting on
        /etc/dnsmasq.conf or /etc/dnsmasq.d/metixel-ap.conf would pass on a
        file that dnsmasq never reads — which is exactly what happened.
        """
        journal = _dnsmasq_journal()
        assert "DHCP, IP range" in journal, (
            "dnsmasq started without an effective DHCP range — the AP "
            f"broadcasts but cannot hand out addresses.\njournal:\n{journal}"
        )
        assert nm.AP_SUBNET_PREFIX in journal, (
            f"dnsmasq's DHCP range is not on the AP subnet "
            f"({nm.AP_SUBNET_PREFIX}*).\njournal:\n{journal}"
        )

    def test_conf_dir_is_read_before_the_stock_defaults(self) -> None:
        """The AP settings must win over Debian's stock dnsmasq.conf.

        Debian's file ends with its own ``conf-dir=`` line, so a sidecar is
        sourced LAST and loses to the defaults above it — including an
        ``interface=`` line.  Our line must therefore come before any
        *uncommented* directive that the AP settings need to override.

        Asserting "near the top" alone is not enough: the stock file has a
        COMMENTED ``#conf-dir=`` near its own end, so a naive scan can pass on a
        file whose only effective conf-dir is still in the wrong place.  This
        compares our line's position against the first active directive instead.

        KNOWN LIMIT: a stock file whose conf-dir is last but which has no active
        directive above it is indistinguishable from a correct file by ordering
        alone — and is genuinely fine, because nothing precedes it.  Ordering is
        therefore necessary but NOT sufficient; the effective-config assertion
        (``test_dhcp_range_is_effective``, reading dnsmasq's journal) and the
        end-to-end lease test are what actually prove DHCP works.  This test's
        job is to name the cause when ordering IS the problem.
        """
        with open(nm.DNSMASQ_CONF, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()

        # 1-based line number of our (uncommented) conf-dir, if present.
        ours = next(
            (
                i
                for i, ln in enumerate(lines, start=1)
                if ln.strip().startswith("conf-dir=") and "/etc/dnsmasq.d/" in ln
            ),
            None,
        )
        assert ours is not None, (
            f"{nm.DNSMASQ_CONF} never sources /etc/dnsmasq.d/ — the AP settings "
            "are present but will never be read"
        )

        # 2. The first ACTIVE (uncommented, non-blank) directive, excluding ours.
        #    Anything above us is read BEFORE us, so it wins — which is the bug.
        first_active = next(
            (
                i
                for i, ln in enumerate(lines, start=1)
                if ln.strip() and not ln.lstrip().startswith("#") and i != ours
            ),
            None,
        )
        if first_active is not None:
            assert ours < first_active, (
                f"the /etc/dnsmasq.d/ conf-dir is on line {ours} of "
                f"{nm.DNSMASQ_CONF}, after the active directive on line "
                f"{first_active} ({lines[first_active - 1].strip()!r}). The AP "
                "settings will be read last and lose to it — this is the "
                "ordering defect behind the 1.2.4/1.2.5 DHCP bug."
            )

    def test_bound_to_the_dhcp_port(self, ap_up: str) -> None:
        """dnsmasq must be listening on UDP/67 to answer a DISCOVER.

        A daemon that started with no dhcp-range listens on 53 only.
        """
        result = _run(["ss", "-lunp"], sudo=True)
        assert re.search(r":67\s", result.stdout), (
            "nothing is listening on UDP/67 — clients' DHCP requests cannot be "
            f"answered.\nss output:\n{result.stdout}"
        )

    def test_bind_is_not_pinned_to_a_nonexistent_interface(self) -> None:
        """dnsmasq must pass its own config check.

        ``interface=wlan0`` used to make this fail with "unknown interface
        wlan0", because hostapd creates the AP interface AFTER dnsmasq starts.
        ``dnsmasq --test`` is dnsmasq's own parser, so it is the authority here.
        """
        result = _run(["/usr/sbin/dnsmasq", "--test"], sudo=True)
        combined = f"{result.stdout}\n{result.stderr}"
        assert "syntax check OK" in combined, f"dnsmasq rejected its own configuration:\n{combined}"
        assert "unknown interface" not in combined, (
            f"dnsmasq is bound to an interface that does not exist yet:\n{combined}"
        )


class TestApIdentity:
    """The broadcast SSID must be one the AP is entitled to broadcast.

    The name is ``Metixel-Setup-<6 hex>`` (see ``nm.ap_ssid_for_mac``), but the
    bare ``Metixel-Setup`` is legitimate too: it is what
    :func:`network_manager.ap_ssid_for_mac` falls back to when the MAC cannot be
    read, and what an identical-but-unreconciled ``hostapd.conf`` still says.
    ``reconcile.sh`` renames it once per device, so "no suffix yet" is a
    not-yet-converged state rather than a defect.

    What must NOT appear is a *malformed* suffix — ``Metixel-Setup-`` with
    nothing after it, or a truncation that looks deliberate but collides across
    MACs.  That is what these assertions pin down.
    """

    @staticmethod
    def _suffix(ssid: str) -> str | None:
        """The MAC suffix of *ssid*, or ``None`` for the bare base name."""
        if ssid == nm.AP_SSID_BASE:
            return None
        assert ssid.startswith(f"{nm.AP_SSID_BASE}-"), (
            f"SSID {ssid!r} is neither the base name {nm.AP_SSID_BASE!r} nor a "
            f"{nm.AP_SSID_BASE}-<suffix> name, so it is not a name this app "
            "can have produced"
        )
        return ssid[len(nm.AP_SSID_BASE) + 1 :]

    def test_broadcasts_a_valid_ap_name(self, ap_up: str) -> None:
        """Whatever hostapd broadcasts must be a well-formed AP name.

        Deliberately does NOT require the suffix to equal ``nm.ap_ssid()``.
        That assertion was wrong: ``nm.ap_ssid()`` is derived from the live
        wlan0 MAC, whereas ``hostapd.conf`` is only rewritten when
        ``reconcile.sh`` runs.  A card moved between boards — or a device that
        has simply not been reconciled yet — legitimately broadcasts
        ``Metixel-Setup`` while the app derives a suffixed name, and the module
        docstring for ``ap_ssid()`` documents exactly that caveat as a known,
        self-correcting mismatch.  Requiring equality failed such a device for
        being in a state the code says is fine.
        """
        actual = _hostapd_ssid()
        assert actual in (nm.AP_SSID_BASE, nm.ap_ssid()), (
            f"hostapd broadcasts {actual!r}, which is neither the base name "
            f"{nm.AP_SSID_BASE!r} nor the name derived from this device's MAC "
            f"({nm.ap_ssid()!r}) — the user is being told to join an access "
            "point that does not exist under that name."
        )

    def test_ssid_suffix_is_well_formed_when_present(self, ap_up: str) -> None:
        """A suffix, if present, must be exactly six hex digits.

        Both forms are accepted — ``Metixel-Setup`` (no suffix yet) and
        ``Metixel-Setup-A1B2C3`` (reconciled) — but never ``Metixel-Setup-`` or
        a short suffix, which is anonymous-looking and collides across devices.
        """
        suffix = self._suffix(_hostapd_ssid())
        if suffix is None:
            pytest.skip(
                f"hostapd broadcasts the bare base name {nm.AP_SSID_BASE!r} — "
                "valid, but it means reconcile.sh has not renamed this device yet"
            )
        assert re.fullmatch(r"[0-9A-F]{6}", suffix), (
            f"SSID {_hostapd_ssid()!r} has a malformed MAC suffix {suffix!r} — "
            "it must be exactly six uppercase hex digits to be unique per device"
        )

    def test_the_app_and_hostapd_agree_when_a_suffix_is_configured(self, ap_up: str) -> None:
        """If a suffix IS configured, it must match the app's derived name.

        This keeps the drift check that matters — ``reconcile.sh``'s ``sed``
        pipeline and ``ap_ssid_for_mac()`` must render the same MAC the same
        way — without inheriting the false requirement that a suffix must exist.
        """
        actual = _hostapd_ssid()
        if actual == nm.AP_SSID_BASE:
            pytest.skip(
                f"hostapd broadcasts the bare base name {actual!r}, so there is nothing to compare"
            )
        assert actual == nm.ap_ssid(), (
            f"hostapd broadcasts {actual!r} but the app derives {nm.ap_ssid()!r} — "
            "reconcile.sh and network_manager.py have drifted."
        )


@pytest.mark.skipif(
    _resolve("dnsmasq") is None or _resolve("ip") is None,
    reason="needs dnsmasq + ip to build a test client",
)
class TestAClientActuallyGetsALease:
    """End-to-end: a real DHCP exchange must yield an address on the AP subnet.

    This is the regression test proper.  It is the only assertion that fails for
    *every* cause of this class of bug, rather than only the ordering defect we
    found — a client that cannot get a lease is the user-visible symptom, so it
    is what gets asserted.

    A veth pair provides the client: it is a real Ethernet interface to the
    kernel, so dnsmasq's DHCP server treats it like any other link.  Verified
    the same way a phone would be — by asking for a lease.
    """

    @pytest.fixture()
    def test_link(self) -> Iterator[tuple[str, str]]:
        """A veth pair with the server end on the AP subnet.  Cleans up after."""
        teardown = [
            ["ip", "link", "del", _SERVER_IF],
            ["ip", "link", "del", _CLIENT_IF],
        ]
        for cmd in teardown:
            _run(cmd, sudo=True)

        assert (
            _run(
                ["ip", "link", "add", _SERVER_IF, "type", "veth", "peer", "name", _CLIENT_IF],
                sudo=True,
            ).returncode
            == 0
        ), "could not create the veth pair"
        # The server end carries the AP address, so the client's DISCOVER is on
        # the subnet dnsmasq serves.  .1 is the Pi's own AP address; the test
        # uses a spare so both can coexist.
        _run(["ip", "addr", "add", f"{nm.AP_SUBNET_PREFIX}254/24", "dev", _SERVER_IF], sudo=True)
        for iface in (_SERVER_IF, _CLIENT_IF):
            _run(["ip", "link", "set", iface, "up"], sudo=True)

        # dnsmasq binds dynamically, so a new interface appearing is picked up
        # without a restart.
        time.sleep(2)
        try:
            yield _CLIENT_IF, _SERVER_IF
        finally:
            for cmd in teardown:
                _run(cmd, sudo=True)

    def test_client_obtains_a_lease(self, ap_up: str, test_link: tuple[str, str]) -> None:
        client_if, _ = test_link

        # udhcpc is the lightest DHCP client and ships with the AP stack as a
        # BUSYBOX APPLET — there is no /usr/bin/udhcpc on Raspberry Pi OS, so it
        # must be invoked as `busybox udhcpc`.  Falling back to dhcpcd (also
        # outside PATH, in /sbin) keeps this runnable if busybox is absent.
        #
        # Resolved by absolute path because a bare `which` finds none of these
        # here (see _resolve: PATH lacks /usr/sbin and /sbin).
        busybox = _resolve("busybox")
        dhcpcd = _resolve("dhcpcd")

        if busybox is not None:
            # `-n` fails instead of forking a background daemon, `-t 5 -T 3`
            # bounds the attempt to ~15s.
            #
            # Deliberately NOT relying on the interface being configured:
            # Raspberry Pi OS ships NO /etc/udhcpc/default.script, so udhcpc
            # completes the DHCP exchange and then has no script to APPLY the
            # lease.  Asserting on `ip addr` therefore failed even though the
            # server answered correctly (`lease of 192.168.42.50 obtained`).
            # The assertion below reads udhcpc's own output instead — that is
            # the signal that dnsmasq actually served the client, which is what
            # this test exists to prove.
            cmd = [busybox, "udhcpc", "-i", client_if, "-n", "-t", "5", "-T", "3"]
        elif dhcpcd is not None:
            cmd = [dhcpcd, "-1", "-T", "15", client_if]
        else:
            pytest.fail(
                "no DHCP client found (tried busybox udhcpc, dhcpcd in "
                f"{', '.join(_SBIN_DIRS)}) — cannot test that a client gets a lease"
            )

        result = _run(cmd, sudo=True)
        combined = f"{result.stdout}\n{result.stderr}"

        # Two acceptable proofs that the SERVER answered, in preference order:
        #   1. udhcpc obtained a lease (works without default.script).
        #   2. the address is configured on the interface (what dhcpcd does).
        leased = _run(["ip", "-4", "addr", "show", client_if]).stdout
        lease_obtained = "lease of" in combined and nm.AP_SUBNET_PREFIX in combined
        assert lease_obtained or nm.AP_SUBNET_PREFIX in leased, (
            "a client on the AP subnet could not obtain a lease — this is the "
            "exact symptom of the 1.2.4/1.2.5 DHCP bug.\n"
            f"lease attempt output:\n{combined}\n"
            f"interface state:\n{leased}\n"
            f"dnsmasq journal:\n{_dnsmasq_journal()}"
        )
