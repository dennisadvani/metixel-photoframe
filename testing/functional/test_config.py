# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: configuration persistence.

These run ON the Pi against the RUNNING backend.  They verify that a
config change made through the web API is written to disk atomically and
survives a backend restart (the core "settings stick" guarantee).

The tests use the backend's HTTP API (urllib, no extra deps) and read the
config file directly from disk.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"

#: A harmless, reversible slideshow setting we can toggle to prove persistence.
#: We use the slideshow section because it does NOT trigger a pipeline reset
#: (unlike display/video/image/sync), so the running slideshow is undisturbed.
_TEST_SECTION = "slideshow"
_TEST_KEY = "image_duration_seconds"
_TEST_VALUE = 17


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def _api_put(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _config_path() -> Path:
    """Resolve the running config file path from the API."""
    data = _api_get("/api/config/path")
    return Path(data["config_path"])


def _read_disk_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_config_save_persists_to_disk() -> None:
    """A config change via the API must be written to the disk file."""
    path = _config_path()
    assert path.exists(), f"config file {path} does not exist"

    # Read the current value so we can restore it afterwards.
    before = _api_get(f"/api/config/{_TEST_SECTION}")
    original = before.get(_TEST_KEY)

    try:
        # Save a new value via the API.
        resp = _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: _TEST_VALUE})
        assert resp.get("status") == "ok", f"config save failed: {resp}"

        # The in-memory config must reflect the change immediately.
        after = _api_get(f"/api/config/{_TEST_SECTION}")
        assert after.get(_TEST_KEY) == _TEST_VALUE, (
            f"in-memory config did not update: {after.get(_TEST_KEY)}"
        )

        # The on-disk file must reflect the change (atomic write).
        disk = _read_disk_config(path)
        assert disk[_TEST_SECTION].get(_TEST_KEY) == _TEST_VALUE, (
            f"on-disk config did not persist: {disk[_TEST_SECTION].get(_TEST_KEY)}"
        )
    finally:
        # Restore the original value so we don't leave the device changed.
        if original is not None:
            _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: original})


def test_config_survives_backend_restart() -> None:
    """A saved config value must survive a backend restart.

    This is the strongest persistence guarantee: the value is written to
    disk, the backend is restarted, and the value is still present.
    """
    path = _config_path()
    assert path.exists(), f"config file {path} does not exist"

    before = _api_get(f"/api/config/{_TEST_SECTION}")
    original = before.get(_TEST_KEY)

    try:
        # Save a distinctive value.
        _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: _TEST_VALUE})

        # Restart the backend service.
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "metixel-backend.service"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"backend restart failed: {result.stderr}"

        # Wait for the backend to come back up.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                after = _api_get(f"/api/config/{_TEST_SECTION}")
                break
            except Exception:
                time.sleep(2)
        else:
            pytest.fail("backend did not come back up after restart")

        assert after.get(_TEST_KEY) == _TEST_VALUE, (
            f"config value did not survive restart: {after.get(_TEST_KEY)}"
        )
    finally:
        # Restore the original value.
        if original is not None:
            _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: original})
