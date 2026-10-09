# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for network_manager Wi-Fi passphrase handling.

Covers the ``--passwd-file`` capability probe and the inline-argv fallback for
nmcli builds (e.g. 1.52.x) that reject ``--passwd-file`` — which previously
made every captive-portal reconnect fail with ``Option '--passwd-file' is
unknown``.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from metixel.backend import network_manager as nm


@pytest.fixture(autouse=True)
def _reset_probe_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nm, "_PASSWD_FILE_SUPPORT", None)


def _fake_run(stdout: str = "", stderr: str = "", returncode: int = 0) -> tuple:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run, calls


class TestInlinePasswordArgs:
    def test_wifi_connect_appends_password(self) -> None:
        args = ["-w", "30", "device", "wifi", "connect", "MyNet"]
        assert nm._inline_password_args(args, "secret") == [*args, "password", "secret"]

    def test_connection_add_appends_psk(self) -> None:
        args = [
            "connection",
            "add",
            "type",
            "wifi",
            "con-name",
            "Metixel-MyNet",
            "ssid",
            "MyNet",
            "wifi-sec.key-mgmt",
            "wpa-psk",
        ]
        assert nm._inline_password_args(args, "secret")[-2:] == ["wifi-sec.psk", "secret"]

    def test_connection_up_unchanged(self) -> None:
        args = ["connection", "up", "Metixel-MyNet"]
        assert nm._inline_password_args(args, "secret") == args

    def test_unknown_command_appends_password(self) -> None:
        args = ["connection", "modify", "x"]
        assert nm._inline_password_args(args, "secret")[-2:] == ["password", "secret"]


class TestPasswdFileProbe:
    def test_unsupported_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stderr="Error: Option '--passwd-file' is unknown, try 'nmcli -help'.")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm._nmcli_supports_passwd_file() is False

    def test_supported_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stdout="nmcli tool, version 1.52.1")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm._nmcli_supports_passwd_file() is True

    def test_probe_failure_defaults_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("nmcli not found")

        monkeypatch.setattr(nm.subprocess, "run", boom)
        assert nm._nmcli_supports_passwd_file() is False

    def test_result_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, calls = _fake_run(stderr="Error: Option '--passwd-file' is unknown")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm._nmcli_supports_passwd_file() is False
        assert nm._nmcli_supports_passwd_file() is False
        assert len(calls) == 1  # probed once


class TestNmcliWithPassword:
    def test_fallback_uses_inline_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nm, "_nmcli_supports_passwd_file", lambda: False)
        run, calls = _fake_run()
        monkeypatch.setattr(nm.subprocess, "run", run)
        args = ["-w", "30", "device", "wifi", "connect", "MyNet"]
        nm._nmcli_with_password(args, "secret", 40)
        assert calls[0] == ["sudo", "nmcli", *args, "password", "secret"]
        assert "--passwd-file" not in calls[0]

    def test_supported_uses_passwd_file_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(nm, "_nmcli_supports_passwd_file", lambda: True)
        run, calls = _fake_run()
        monkeypatch.setattr(nm.subprocess, "run", run)
        nm._nmcli_with_password(["connection", "up", "Metixel-MyNet"], "secret", 40)
        assert "--passwd-file" in calls[0]
        # The temp secret file must be cleaned up on every exit path.
        leftovers = [p for p in Path(tempfile.gettempdir()).glob("metixel-wifi-*") if p.is_file()]
        assert leftovers == []

    def test_no_password_skips_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nm, "_nmcli_supports_passwd_file", lambda: True)
        run, calls = _fake_run()
        monkeypatch.setattr(nm.subprocess, "run", run)
        nm._nmcli_with_password(["device", "wifi", "connect", "OpenNet"], "", 40)
        assert calls[0] == ["sudo", "nmcli", "device", "wifi", "connect", "OpenNet"]


class TestIsEthernetConnected:
    def test_connected_when_ethernet_device_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "eth0:ethernet:connected\nwlan0:wifi:connected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_ethernet_connected() is True

    def test_disconnected_when_only_wifi_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "wlan0:wifi:connected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_ethernet_connected() is False

    def test_disconnected_when_ethernet_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "eth0:ethernet:disconnected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_ethernet_connected() is False

    def test_false_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: object, **__: object) -> None:
            raise OSError("nmcli missing")

        monkeypatch.setattr(nm.subprocess, "run", boom)
        assert nm.is_ethernet_connected() is False


class TestIsWifiConnected:
    def test_connected_when_wifi_device_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "eth0:ethernet:connected\nwlan0:wifi:connected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_wifi_connected() is True

    def test_disconnected_when_only_ethernet_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "eth0:ethernet:connected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_wifi_connected() is False

    def test_disconnected_when_wifi_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = "wlan0:wifi:disconnected\n"
        run, _ = _fake_run(stdout=stdout)
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_wifi_connected() is False

    def test_false_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: object, **__: object) -> None:
            raise OSError("nmcli missing")

        monkeypatch.setattr(nm.subprocess, "run", boom)
        assert nm.is_wifi_connected() is False


class TestFindRfkillBinary:
    """The rfkill binary lives in /usr/sbin, which is NOT on the PATH of a
    non-login shell — the original cause of a silent WiFi-enablement failure.
    Well-known absolute paths must be probed before falling back to PATH."""

    def test_prefers_usr_sbin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nm.os, "access", lambda path, mode: path == "/usr/sbin/rfkill")
        assert nm._find_rfkill_binary() == "/usr/sbin/rfkill"

    def test_falls_back_to_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nm.os, "access", lambda _p, _m: False)
        monkeypatch.setattr(nm.shutil, "which", lambda _n: "/opt/bin/rfkill")
        assert nm._find_rfkill_binary() == "/opt/bin/rfkill"

    def test_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nm.os, "access", lambda _p, _m: False)
        monkeypatch.setattr(nm.shutil, "which", lambda _n: None)
        assert nm._find_rfkill_binary() is None


class TestSetWifiRadio:
    """set_wifi_radio must report real failures (unlike is_wifi_radio_enabled,
    which fails open) because callers persist a one-shot marker on success."""

    def test_enable_runs_rfkill_then_radio_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, calls = _fake_run()
        monkeypatch.setattr(nm, "_find_rfkill_binary", lambda: "/usr/sbin/rfkill")
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", run)

        assert nm.set_wifi_radio(True) is True

        assert calls[0] == ["sudo", "-n", "/usr/sbin/rfkill", "unblock", "wifi"]
        assert calls[1] == ["sudo", "-n", "/usr/sbin/rfkill", "unblock", "wlan"]
        # The nmcli step is what actually matters and must come after rfkill.
        assert calls[2] == ["sudo", "-n", "nmcli", "radio", "wifi", "on"]
        assert calls[3] == ["sudo", "-n", "nmcli", "device", "set", "wlan0", "managed", "yes"]

    def test_enable_survives_missing_rfkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, calls = _fake_run()
        monkeypatch.setattr(nm, "_find_rfkill_binary", lambda: None)
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", run)

        assert nm.set_wifi_radio(True) is True
        # No rfkill calls, but nmcli still ran.
        assert calls[0] == ["sudo", "-n", "nmcli", "radio", "wifi", "on"]

    def test_enable_reports_nmcli_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(returncode=1, stderr="Error: not authorised")
        monkeypatch.setattr(nm, "_find_rfkill_binary", lambda: None)
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", run)

        assert nm.set_wifi_radio(True) is False

    def test_enable_false_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("nmcli missing")

        monkeypatch.setattr(nm, "_find_rfkill_binary", lambda: None)
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", boom)
        assert nm.set_wifi_radio(True) is False

    def test_disable_is_single_nmcli_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, calls = _fake_run()
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", run)

        assert nm.set_wifi_radio(False) is True
        # One call only — no rfkill, no managed re-adopt.
        assert calls == [["sudo", "-n", "nmcli", "radio", "wifi", "off"]]

    def test_disable_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(returncode=1, stderr="Error: not authorised")
        monkeypatch.setattr("metixel.shared.subprocess.run_cmd", run)
        assert nm.set_wifi_radio(False) is False


class TestIsWifiRadioEnabled:
    def test_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stdout="enabled\n")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_wifi_radio_enabled() is True

    def test_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stdout="disabled\n")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.is_wifi_radio_enabled() is False

    def test_fails_open_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise OSError("nmcli missing")

        monkeypatch.setattr(nm.subprocess, "run", boom)
        # Documented behaviour: a status check must not block the user.
        assert nm.is_wifi_radio_enabled() is True


class TestTerseSplit:
    """``nmcli -t`` escapes ``:`` inside values as ``\\:`` — an SSID such as
    ``Home:Net`` must survive parsing."""

    def test_split_unescapes_colon_and_backslash(self) -> None:
        assert nm._split_terse("Home\\:Net:70:WPA2:2412") == ["Home:Net", "70", "WPA2", "2412"]
        assert nm._split_terse("a\\\\b:c") == ["a\\b", "c"]
        assert nm._split_terse("plain") == ["plain"]
        assert nm._split_terse("") == [""]
        assert nm._split_terse("a::b") == ["a", "", "b"]

    def test_maxsplit_like_str_split(self) -> None:
        assert nm._split_terse("IP4.ADDRESS[1]:192.168.1.5/24", 1) == [
            "IP4.ADDRESS[1]",
            "192.168.1.5/24",
        ]
        assert nm._split_terse("GENERAL.CONNECTION:Wired\\:Home", 1)[-1] == "Wired:Home"

    def test_parse_scan_results_keeps_colon_in_ssid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stdout="Home\\:Net:70:WPA2:2412\nCafe:40::5180\n")
        monkeypatch.setattr(nm.subprocess, "run", run)
        nets = nm._parse_scan_results()
        assert [n["ssid"] for n in nets] == ["Home:Net", "Cafe"]
        assert nets[0]["signal"] == 70 and nets[0]["security"] == "WPA2"
        assert nets[0]["freq"] == 2412
        assert nets[1]["freq"] == 5180

    def test_fill_wifi_details_keeps_colon_in_ssid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, _ = _fake_run(stdout="yes:Home\\:Net:70:WPA2\n")
        monkeypatch.setattr(nm.subprocess, "run", run)
        status: dict = {}
        nm._fill_wifi_details(status, "wlan0")
        assert status["ssid"] == "Home:Net"
        assert status["signal"] == 70
        assert status["security"] == "WPA2"

    def test_forget_network_matches_escaped_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run, calls = _fake_run(stdout="Home\\:Net:1111-2222\nOther:3333\n")
        monkeypatch.setattr(nm.subprocess, "run", run)
        assert nm.forget_network("Home:Net") is True
        assert ["sudo", "nmcli", "connection", "delete", "1111-2222"] in calls
