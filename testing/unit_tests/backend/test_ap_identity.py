# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The AP SSID is rendered by two implementations that must not drift.

``network_manager.ap_ssid_for_mac`` builds the name the backend logs, shows on
screen and serves to the web UI.  ``scripts/reconcile.sh`` builds the SAME
string with ``sed``/``tr``, because hostapd reads a static file and the shell
cannot call Python.

That duplication is unavoidable, but silent drift is not: if the two ever
disagree, the frame broadcasts one name and tells the user to look for another —
which is worse than having no suffix at all.  The shell pipeline is therefore
executed for real here and compared against the Python function.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from metixel.backend import network_manager as nm

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE = REPO_ROOT / "scripts" / "reconcile.sh"

#: The MAC cases the two implementations must agree on.  Includes the formats a
#: reader may hit (colon-separated, bare, mixed case) and the degenerate inputs
#: that must degrade to the bare base name rather than a truncated one.
MAC_CASES = [
    "d8:3a:dd:12:ab:c3",
    "D8:3A:DD:12:AB:C3",
    "d83add12abc3",
    "d8-3a-dd-12-ab-c3",
    "00:00:00:00:00:00",
    "dc:a6:32:ff:ff:ff",
]


class TestSsidForMac:
    def test_appends_last_six_hex_digits(self) -> None:
        assert nm.ap_ssid_for_mac("d8:3a:dd:12:ab:c3") == "Metixel-Setup-12ABC3"

    def test_is_case_insensitive(self) -> None:
        """A MAC read as lower case must produce the same SSID as upper case.

        SSIDs are case-sensitive, so an inconsistent case would make the name
        the frame broadcasts depend on how sysfs happened to be read.
        """
        assert nm.ap_ssid_for_mac("D8:3A:DD:12:AB:C3") == nm.ap_ssid_for_mac("d8:3a:dd:12:ab:c3")

    def test_accepts_separatorless_mac(self) -> None:
        assert nm.ap_ssid_for_mac("d83add12abc3") == "Metixel-Setup-12ABC3"

    def test_uses_the_last_six_not_the_first(self) -> None:
        """The suffix must come from the END of the MAC.

        Taking the first six would make every Pi on the same OUI broadcast an
        identical name — defeating the point of the suffix.
        """
        assert nm.ap_ssid_for_mac("b8:27:eb:00:00:01") == "Metixel-Setup-000001"

    @pytest.mark.parametrize("mac", ["", "aa:bb", "not-a-mac", ":::"])
    def test_short_or_invalid_mac_falls_back_to_base(self, mac: str) -> None:
        """An unreadable MAC degrades to the old name, never a partial one.

        The AP must still come up on a device whose MAC cannot be read; what it
        must not do is advertise ``Metixel-Setup-`` or a truncated suffix that
        looks deliberate but is not unique.
        """
        assert nm.ap_ssid_for_mac(mac) == nm.AP_SSID_BASE

    def test_suffix_is_always_six_digits(self) -> None:
        """Guard the fixed width — a shorter suffix would collide across MACs."""
        suffix = nm.ap_ssid_for_mac("aa:bb:cc:dd:ee:ff").removeprefix(f"{nm.AP_SSID_BASE}-")
        assert len(suffix) == 6


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
class TestShellImplementationMatchesPython:
    """reconcile.sh and network_manager.py must render the identical SSID.

    This runs the shell helper extracted from reconcile.sh and compares it to
    the Python function for every case.  A change to one side that is not made
    to the other fails here with both values shown.
    """

    @staticmethod
    def _shell_ssid(mac: str, tmp_path: Path) -> str:
        """Run reconcile.sh's _ap_ssid_from_mac and return its output."""
        # Source the function rather than re-typing it: a copy in the test would
        # pass while the real script drifted.
        script = tmp_path / "ssid.sh"
        script.write_text(
            "set -euo pipefail\n"
            # Pull in the helper straight from reconcile.sh so the test cannot
            # disagree with the shipped implementation.
            'eval "$(sed -n \'/^_ap_ssid_from_mac()/,/^}/p\' "$1")"\n'
            '_ap_ssid_from_mac "$2"\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(script), str(RECONCILE), mac],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"shell helper failed: {result.stderr}"
        return result.stdout

    @pytest.mark.parametrize("mac", MAC_CASES)
    def test_agrees_with_python(self, mac: str, tmp_path: Path) -> None:
        assert self._shell_ssid(mac, tmp_path) == nm.ap_ssid_for_mac(mac)

    def test_helper_is_actually_present(self, tmp_path: Path) -> None:
        """Fail loudly if the helper is renamed or removed from reconcile.sh.

        Without this, a missing function would make `eval` a no-op and every
        comparison above would pass vacuously.
        """
        assert self._shell_ssid("d8:3a:dd:12:ab:c3", tmp_path) == "Metixel-Setup-12ABC3"


class TestReconcileWritesTheSsid:
    """reconcile.sh must actually use the helper for hostapd.conf."""

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_hostapd_ssid_is_rendered_from_the_mac(self) -> None:
        text = RECONCILE.read_text(encoding="utf-8")
        # The conf must be built with the computed variable, not a literal —
        # otherwise every frame broadcasts the same base name again.
        assert "ssid=${AP_SSID}" in text, "hostapd.conf is not rendered from AP_SSID"
        assert 'AP_SSID="$(_ap_ssid_from_mac' in text

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_existing_ssid_is_migrated(self) -> None:
        """An existing hostapd.conf must have its ssid line corrected."""
        text = RECONCILE.read_text(encoding="utf-8")
        assert 'sed -i "s|^ssid=.*|ssid=${AP_SSID}|" /etc/hostapd/hostapd.conf' in text


class TestReconcileFixesDnsmasqOrdering:
    """The DHCP bug's defining detail: conf-dir ordering, not mere presence."""

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_sidecar_is_written(self) -> None:
        text = RECONCILE.read_text(encoding="utf-8")
        assert 'DNSMASQ_SIDECAR="/etc/dnsmasq.d/metixel-ap.conf"' in text

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_conf_dir_is_hoisted_to_the_top(self) -> None:
        """Reading the sidecar is not enough — it must be read BEFORE the rest.

        Debian's stock /etc/dnsmasq.conf already ends with its own conf-dir=
        line, so a sidecar is sourced last and its values lose to the defaults
        above it.  That was the whole bug: `grep conf-dir` passed while DHCP
        was still dead.
        """
        text = RECONCILE.read_text(encoding="utf-8")
        assert "head -5 /etc/dnsmasq.conf" in text, "no check that conf-dir comes first"
        assert "hoisted to the top" in text

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_no_interface_bind_that_may_not_exist(self) -> None:
        """The sidecar must not bind to wlan0 by name.

        hostapd creates the interface AFTER dnsmasq starts, so a name bind
        refers to something that does not exist yet and dnsmasq --test rejects
        it.  bind-dynamic + except-interface is the correct form.
        """
        text = RECONCILE.read_text(encoding="utf-8")
        assert "bind-dynamic" in text
        assert "except-interface=lo" in text
        # The AP range must specify a netmask, since no interface= line tells
        # dnsmasq which subnet the range belongs to.
        assert "dhcp-range=192.168.42.10,192.168.42.100,255.255.255.0,12h" in text

    @pytest.mark.skipif(not RECONCILE.exists(), reason="reconcile.sh not present")
    def test_dnsmasq_package_is_checked_by_file_and_unit(self) -> None:
        """`command -v` cannot be used here: /usr/sbin is not on PATH.

        That is precisely how dnsmasq-base (binary, no conffile) went
        undetected and the AP ended up with no DHCP at all.
        """
        text = RECONCILE.read_text(encoding="utf-8")
        assert "/usr/lib/systemd/system/dnsmasq.service" in text
        # Guard the regression directly: no `command -v dnsmasq` in the AP block.
        # Only EXECUTABLE lines count — the block explains at length why this
        # check is wrong, and that prose must be allowed to name the command.
        executable = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        assert not any("command -v dnsmasq" in line for line in executable), (
            "/usr/sbin is not on PATH here — detect dnsmasq by conffile + unit"
        )


class TestDnsmasqConfDirOrdering:
    """The ordering rule that made a "configured" frame serve no DHCP.

    Debian's stock /etc/dnsmasq.conf ends with its own ``conf-dir=`` line, so a
    sidecar under /etc/dnsmasq.d/ is sourced LAST and its values lose to every
    default above it — including ``interface=``.  A sidecar that is read is not
    the same as a sidecar that WINS.

    The predicate below mirrors the assertion in
    ``testing/functional/test_ap_dhcp.py`` so the ordering rule is checked in
    CI, without a Pi, rather than only on hardware.
    """

    @staticmethod
    def _conf_dir_wins(lines: list[str]) -> bool:
        """True when our conf-dir is sourced before the first active directive."""
        ours = next(
            (
                i
                for i, ln in enumerate(lines, start=1)
                if ln.strip().startswith("conf-dir=") and "/etc/dnsmasq.d/" in ln
            ),
            None,
        )
        if ours is None:
            return False
        first_active = next(
            (
                i
                for i, ln in enumerate(lines, start=1)
                if ln.strip() and not ln.lstrip().startswith("#") and i != ours
            ),
            None,
        )
        return first_active is None or ours < first_active

    def test_hoisted_line_wins(self) -> None:
        assert self._conf_dir_wins(
            [
                "conf-dir=/etc/dnsmasq.d/,*.conf",
                "# Configuration file for dnsmasq.",
                "interface=lo",
                "port=53",
            ]
        )

    def test_appended_after_defaults_loses(self) -> None:
        """The exact 1.2.5 shape: sidecar sourced after the stock directives."""
        assert not self._conf_dir_wins(
            ["interface=lo", "port=53", "conf-dir=/etc/dnsmasq.d/,*.conf"]
        )

    def test_never_uncommented_is_not_configured(self) -> None:
        assert not self._conf_dir_wins(["#conf-dir=/etc/dnsmasq.d/,*.conf", "# x"])

    def test_commented_copy_does_not_count(self) -> None:
        """Debian's file has a COMMENTED conf-dir near its own end.

        A naive scan for the string would match that one and wrongly conclude
        the sidecar is read — the false positive that hid this bug.
        """
        stock = [
            "conf-dir=/etc/dnsmasq.d/,*.conf",
            "interface=lo",
            "#conf-dir=/etc/dnsmasq.d,.bak",
            "#conf-dir=/etc/dnsmasq.d/,*.conf",
        ]
        # Our (line 1) is before the active `interface=lo` (line 2) => wins.
        assert self._conf_dir_wins(stock)
        # Remove our hoisted line: only commented ones remain => not configured.
        assert not self._conf_dir_wins(stock[1:])

    def test_stock_file_alone_is_not_configured(self) -> None:
        """No uncommented conf-dir anywhere => the sidecar is never read."""
        assert not self._conf_dir_wins(["# comment"] * 50)

    def test_reconcile_hoists_rather_than_appends(self) -> None:
        """reconcile.sh must place the line FIRST, not just add it.

        Appending is the intuitive fix and the wrong one: it leaves the stock
        defaults above it winning.  The script must both detect the late line
        and move it.
        """
        text = RECONCILE.read_text(encoding="utf-8")
        assert "head -5 /etc/dnsmasq.conf" in text, "no check that conf-dir comes first"
        assert "hoisted to the top" in text, "no in-place hoist path"
        # The hoist must rewrite the file with our line ahead of the rest.
        assert "printf '%s\\n' \"${DNSMASQ_CONF_MARKER}\"" in text


class TestStartApRefusesWithoutDhcp:
    """start_ap_mode must not advertise an AP that cannot serve addresses.

    A running dnsmasq proves nothing: with no effective ``dhcp-range`` it still
    starts as a plain DNS forwarder and still exits 0, so ``start_ap_mode()``
    used to return True on a frame no client could join.  These tests cover the
    helper AND — critically — that the start path actually consults it.  A
    helper that is correct but never called is the same bug.
    """

    @staticmethod
    def _fake_run(calls: list[list[str]]) -> object:
        """A command-aware subprocess.run stand-in for the start path.

        It must answer each command the way the real system would, or an
        earlier guard (the wlan0 poll, the unit-existence check) short-circuits
        and the test passes without ever reaching the DHCP guard — which is
        precisely what a naive always-OK fake does.
        """

        def run(cmd: list[str], **_kw: object) -> object:
            calls.append(list(cmd))
            import subprocess as _sp

            joined = " ".join(cmd)
            stdout = ""
            if "link show wlan0" in joined:
                stdout = "2: wlan0: <BROADCAST,MULTICAST,UP> mtu 1500"
            elif "list-unit-files" in joined:
                stdout = "hostapd.service enabled\ndnsmasq.service enabled\n"
            return _sp.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        return run

    def test_fails_when_no_dhcp_range_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        conf = tmp_path / "dnsmasq.conf"
        conf.write_text("# stock Debian: comments only\n", encoding="utf-8")
        monkeypatch.setattr(nm, "DNSMASQ_CONF", str(conf))
        monkeypatch.setattr("glob.glob", lambda *_a, **_k: [])

        assert nm._dnsmasq_dhcp_configured() is False

    def test_start_ap_mode_refuses_when_guard_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must actually gate start_ap_mode, not just exist.

        Without this, deleting the call (or a typo'd condition) silently
        restores the original bug: the AP broadcasts and hands out nothing.
        """
        monkeypatch.setattr(nm, "_dnsmasq_dhcp_configured", lambda: False)
        monkeypatch.setattr(nm.time, "sleep", lambda *_a: None)
        calls: list[list[str]] = []
        monkeypatch.setattr(nm.subprocess, "run", self._fake_run(calls))

        started = nm.start_ap_mode()

        # Assert the guard was REACHED, not merely that the result is False:
        # a False from an earlier failure would make this test vacuous.
        assert self._reached_guard(calls), (
            f"start_ap_mode() never reached the DHCP guard, so this test proves "
            f"nothing. Calls made: {calls}"
        )
        assert started is False, (
            "start_ap_mode() began the AP with no usable dhcp-range — clients "
            "will connect and never get an address (the 1.2.4/1.2.5 bug)"
        )

    @staticmethod
    def _reached_guard(calls: list[list[str]]) -> bool:
        """Whether the run got past the unit checks to the DHCP decision.

        The guard sits immediately after the `list-unit-files` loop, so seeing
        that loop complete for BOTH units is the observable proof the guard was
        evaluated rather than skipped.
        """
        checked = [c for c in calls if "list-unit-files" in " ".join(c)]
        return len(checked) >= 2

    def test_start_ap_mode_consults_the_guard_before_hostapd(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal must happen BEFORE hostapd is asked to start.

        Ordering matters: starting hostapd first broadcasts an SSID that users
        can join but not use, which is far worse than not broadcasting at all.

        Only the START is asserted on — ``systemctl list-unit-files`` is an
        install check that legitimately runs earlier and is not a start.
        """
        monkeypatch.setattr(nm, "_dnsmasq_dhcp_configured", lambda: False)
        monkeypatch.setattr(nm.time, "sleep", lambda *_a: None)
        calls: list[list[str]] = []
        monkeypatch.setattr(nm.subprocess, "run", self._fake_run(calls))

        nm.start_ap_mode()

        assert self._reached_guard(calls), f"guard was never reached: {calls}"

        def _is_hostapd_start(c: list[str]) -> bool:
            return "start" in c and nm.HOSTAPD_UNIT in c

        started = [c for c in calls if _is_hostapd_start(c)]
        assert not started, (
            f"hostapd was STARTED despite having no DHCP range: {started} — the "
            "AP would broadcast an SSID that cannot be joined usefully"
        )

    def test_true_when_range_matches_ap_subnet(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        conf = tmp_path / "dnsmasq.conf"
        conf.write_text(
            "# comment\ndhcp-range=192.168.42.10,192.168.42.100,12h\n", encoding="utf-8"
        )
        monkeypatch.setattr(nm, "DNSMASQ_CONF", str(conf))
        monkeypatch.setattr("glob.glob", lambda *_a, **_k: [])

        assert nm._dnsmasq_dhcp_configured() is True

    def test_ignores_an_unrelated_dhcp_range(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A range on another subnet is not a working AP.

        A machine can legitimately run dnsmasq for something else; that must
        not be mistaken for the captive portal being configured.
        """
        conf = tmp_path / "dnsmasq.conf"
        conf.write_text("dhcp-range=10.0.0.10,10.0.0.100,12h\n", encoding="utf-8")
        monkeypatch.setattr(nm, "DNSMASQ_CONF", str(conf))
        monkeypatch.setattr("glob.glob", lambda *_a, **_k: [])

        assert nm._dnsmasq_dhcp_configured() is False

    def test_reads_the_sidecar_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The AP settings live in /etc/dnsmasq.d/, so that must be searched."""
        conf = tmp_path / "dnsmasq.conf"
        conf.write_text("# stock\n", encoding="utf-8")
        sidecar = tmp_path / "metixel-ap.conf"
        sidecar.write_text(
            "dhcp-range=192.168.42.10,192.168.42.100,255.255.255.0,12h\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(nm, "DNSMASQ_CONF", str(conf))
        monkeypatch.setattr("glob.glob", lambda *_a, **_k: [str(sidecar)])

        assert nm._dnsmasq_dhcp_configured() is True

    def test_missing_files_report_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(nm, "DNSMASQ_CONF", str(tmp_path / "nope.conf"))
        monkeypatch.setattr("glob.glob", lambda *_a, **_k: [])
        assert nm._dnsmasq_dhcp_configured() is False
