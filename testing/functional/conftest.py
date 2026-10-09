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

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

#: Marker used to gate all functional tests.
pytestmark = pytest.mark.functional


def _run_dir() -> Path:
    """Directory the suite is actually being run from.

    ``run_functional_tests.sh`` copies the tests to a temp dir on the Pi and
    runs pytest there, so the credentials it pushes land next to the tests
    that *run*, not next to this file in the installed release tree.  Anchoring
    ``.env`` to ``__file__`` looked in the release tree and never found it,
    which silently disabled every credential-gated test.
    """
    return Path.cwd()


def _env_candidates() -> list[Path]:
    """Every place a ``.env`` is legitimately found, most specific first.

    Order matters: the run directory wins (that is the copy the harness
    pushes), then ``$METIXEL_FUNCTIONAL_ENV`` if set, then this file's own
    directory for the in-repo case (``pytest testing/functional``).
    """
    here = Path(__file__).resolve().parent
    candidates = [_run_dir() / ".env", here / ".env"]
    override = os.environ.get("METIXEL_FUNCTIONAL_SSH_ENV") or os.environ.get(
        "METIXEL_FUNCTIONAL_ENV"
    )
    if override:
        candidates.insert(0, Path(override).expanduser())
    # De-duplicate while preserving order.
    seen: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.append(path)
    return seen


#: Backwards-compatible alias — the first place a ``.env`` would live.
_ENV_FILE = _env_candidates()[0]

#: Backend HTTP port (Flask serves here; nginx proxies :80 → :8080).
BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"


def parse_json_object(raw: bytes | str, *, context: str = "") -> dict:
    """Parse a JSON **object** from an API response body.

    ``json.loads()`` returns ``Any``, so returning it directly from a function
    annotated ``-> dict`` trips mypy's ``no-any-return`` on every API helper in
    the functional suite.  Parsing through this helper keeps the annotation
    honest AND asserts the shape we actually expect, so a response that is a
    list or a bare scalar fails loudly here rather than confusingly further
    down a test.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        where = f" for {context}" if context else ""
        raise AssertionError(f"expected a JSON object{where}, got {type(data).__name__}")
    return data


def parse_json_array(raw: bytes | str, *, context: str = "") -> list:
    """Parse a JSON **array** from an API response body.

    Companion to :func:`parse_json_object` for the handful of endpoints whose
    top level is a list (e.g. ``GET /api/immich/albums`` returns an array of
    ``{id, name, assetCount}``).  Keeping the two helpers separate — rather
    than one helper returning ``dict | list`` — means each call site states
    which shape it expects, so a route changing from one to the other is a
    loud, localised failure instead of a confusing ``TypeError`` further down.
    """
    data = json.loads(raw)
    if not isinstance(data, list):
        where = f" for {context}" if context else ""
        raise AssertionError(f"expected a JSON array{where}, got {type(data).__name__}")
    return data


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``functional`` marker.

    The functional suite is copied to a tmp dir on the Pi and run from there,
    so there is no ``pyproject.toml`` to register the marker.  Registering it
    here (instead of relying on the project config) silences the
    ``PytestUnknownMarkWarning`` and enables ``-m functional`` selection.
    """
    config.addinivalue_line("markers", "functional: on-Pi hardware/integration tests")


def wait_for_pipeline_idle(timeout: int = 300) -> bool:
    """Wait until the media pipeline is idle (no active processing).

    Polls ``/api/health/processing-status`` until ``active`` is ``"complete"``
    (or the file reports no active phase).  This is the "boot screen is off"
    check: the frontend keeps the boot screen up while the pipeline is
    rebuilding, so tests that drop media or read the playlist should wait for
    the pipeline to settle first (e.g. after a backend restart or a heavy
    Immich sync).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health/processing-status", timeout=5) as resp:
                data = json.loads(resp.read().decode())
            active = data.get("active", "")
            if active == "complete" or not active:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


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


def _load_env() -> dict[str, str]:
    """Merge every candidate ``.env``, most specific first.

    Earlier files win, so a ``.env`` in the run directory (pushed by the
    harness) overrides the in-repo one.  Returns ``{}`` when none exist.
    """
    merged: dict[str, str] = {}
    for path in _env_candidates():
        for key, value in _load_env_file(path).items():
            merged.setdefault(key, value)
    return merged


def _env_file_used() -> Path | None:
    """The first candidate ``.env`` that actually exists, for diagnostics."""
    for path in _env_candidates():
        if path.is_file():
            return path
    return None


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
    env = _load_env()
    return {
        "ssid": env.get("METIXEL_TEST_WIFI_SSID", ""),
        "password": env.get("METIXEL_TEST_WIFI_PASSWORD", ""),
    }


@pytest.fixture(scope="session")
def immich_creds() -> dict[str, str]:
    """The Immich sync-test credentials + album names from ``functional/.env``."""
    env = _load_env()
    return {
        "url": env.get("METIXEL_TEST_IMMICH_URL", "").rstrip("/"),
        "api_key": env.get("METIXEL_TEST_IMMICH_API_KEY", ""),
        "album_1": env.get("METIXEL_TEST_IMMICH_ALBUM_1", ""),
        "album_2": env.get("METIXEL_TEST_IMMICH_ALBUM_2", ""),
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

    Two classes of prerequisite are treated differently, because conflating
    them hid a real device fault:

    * **Cannot run here at all** (not a Pi, no wlan0) — skip.  Running on a
      workstation is expected and must stay quiet.
    * **Correct host, broken prerequisite** (a Pi whose passwordless sudo is
      missing, or whose ``.env`` was not pushed) — skip everything EXCEPT
      :mod:`test_sudo`, which is left to run so it can *fail* with a clear
      message.

    That last point matters: ``test_sudo_nopasswd_works`` used to be gated
    behind the very ``sudo -n`` check it exists to verify, so on a device with
    no NOPASSWD entry it skipped — the one test that should have shouted was
    silenced, and 50 tests reported green-ish "skipped" instead of a failure.
    Never gate a test behind the condition it asserts.
    """
    env = _load_env()
    env_file = _env_file_used()
    reasons: list[str] = []
    if not _is_raspberry_pi():
        reasons.append("not a Raspberry Pi")
    if not _has_wlan0():
        reasons.append("no wlan0 interface")

    # Prerequisites that mean "this host is not a Metixel device" — skip all.
    if reasons:
        skip = pytest.mark.skip(reason="functional suite skipped: " + "; ".join(reasons))
        for item in items:
            item.add_marker(skip)
        return

    # The host is a Pi.  From here, a missing prerequisite is a *device* fault
    # and must be visible, so we only skip the tests that genuinely cannot run.
    device_faults: list[str] = []
    if not _sudo_ok():
        device_faults.append("passwordless sudo unavailable (need `pi ALL=(ALL) NOPASSWD: ALL`)")
    if not env.get("METIXEL_TEST_WIFI_SSID"):
        where = (
            str(env_file)
            if env_file
            else "no .env found (looked in: " + ", ".join(str(p) for p in _env_candidates()) + ")"
        )
        device_faults.append(f"METIXEL_TEST_WIFI_SSID not set — {where}")

    if not device_faults:
        return

    detail = "; ".join(device_faults)
    # A banner the operator cannot miss, before the skip markers hide the
    # detail in the default output.
    terminal = config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line("")
        terminal.write_line("!" * 78)
        terminal.write_line("METIXEL FUNCTIONAL SUITE — DEVICE PREREQUISITE FAILED")
        for fault in device_faults:
            terminal.write_line(f"  * {fault}")
        terminal.write_line(
            "The suite cannot exercise the device until this is fixed. "
            "Tests that verify it are left running so they FAIL rather than skip."
        )
        terminal.write_line("!" * 78)
        terminal.write_line("")

    skip = pytest.mark.skip(reason="functional suite skipped (device fault): " + detail)
    for item in items:
        # test_sudo exists precisely to assert the sudo prerequisite — leaving
        # it unskipped turns a silent skip into a loud, actionable failure.
        if item.fspath.basename == "test_sudo.py":
            continue
        item.add_marker(skip)
