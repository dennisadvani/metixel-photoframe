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
