# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional (hardware) test harness for the Metixel network stack.

These tests run ON a Raspberry Pi as the ``pi`` user and exercise the real
Wi-Fi/AP stack (nmcli, hostapd, dnsmasq) plus passwordless sudo.  They are
deliberately excluded from the default ``tests/`` run (``testpaths`` points at
``tests/``) and are gated behind the ``functional`` pytest marker.

Prerequisites (see CONTRIBUTING.md):
    * A Pi with a Wi-Fi radio (wlan0) and an Ethernet uplink for control.
    * Passwordless sudo for the ``pi`` user (``pi ALL=(ALL) NOPASSWD: ALL``).
    * A ``functional/.env`` file with the test network credentials.

The harness loads ``functional/.env`` (dependency-free) and skips the whole
suite if the credentials are missing or the host is not a Pi.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: Marker used to gate all functional tests.
pytestmark = pytest.mark.functional

#: Path to the gitignored credentials file, relative to this conftest.
_ENV_FILE = Path(__file__).resolve().parent / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` .env file without any dependencies.

    Supports blank lines, ``#`` comments, and optional surrounding quotes.
    Values are returned as strings (empty string for a blank value).
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def _is_raspberry_pi() -> bool:
    """Return whether the host looks like a Raspberry Pi."""
    try:
        with open("/proc/device-tree/model", encoding="utf-8", errors="ignore") as f:
            return "raspberry pi" in f.read().lower()
    except OSError:
        return False


def _has_wlan0() -> bool:
    """Return whether a wlan0 interface exists."""
    try:
        result = subprocess.run(
            ["ip", "link", "show", "wlan0"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "wlan0:" in result.stdout
    except Exception:
        return False


def _sudo_ok() -> bool:
    """Return whether passwordless sudo works (``sudo -n true``)."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="session")
def wifi_creds() -> dict[str, str]:
    """The test-network credentials from ``functional/.env``."""
    env = _load_env_file(_ENV_FILE)
    return {
        "ssid": env.get("METIXEL_TEST_WIFI_SSID", ""),
        "password": env.get("METIXEL_TEST_WIFI_PASSWORD", ""),
    }


@pytest.fixture(scope="session")
def sudo_ok() -> bool:
    """Whether passwordless sudo is available on this host."""
    return _sudo_ok()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the whole functional suite when prerequisites are missing.

    The suite needs a Pi with wlan0, passwordless sudo, and a configured
    ``functional/.env``.  If any is missing we skip rather than fail so the
    suite can be collected on a dev machine without erroring.
    """
    env = _load_env_file(_ENV_FILE)
    missing = not env.get("METIXEL_TEST_WIFI_SSID")
    reasons: list[str] = []
    if not _is_raspberry_pi():
        reasons.append("not a Raspberry Pi")
    if not _has_wlan0():
        reasons.append("no wlan0 interface")
    if not _sudo_ok():
        reasons.append("passwordless sudo unavailable")
    if missing:
        reasons.append("METIXEL_TEST_WIFI_SSID not set in functional/.env")

    if reasons:
        skip = pytest.mark.skip(reason="functional suite skipped: " + "; ".join(reasons))
        for item in items:
            item.add_marker(skip)
