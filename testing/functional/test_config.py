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

import contextlib
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


def _api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
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


def _log_dir() -> Path:
    """Resolve the on-disk log directory from the running backend.

    Logs live at ``<data dir>/logs/``, and the config file reported by the API
    lives directly in the data dir (``<data dir>/config.json``).  Deriving the
    path from the reported config path keeps the test correct on any install
    layout (``/opt/metixel``, dev).
    """
    return _config_path().parent / "logs"


def _backend_log_path() -> Path:
    """The backend's OWN log file.

    Each process writes a SEPARATE file (``metixel-backend.log`` /
    ``metixel-frontend.log``).  This test used to look for a shared
    ``metixel.log``, but NO process writes that name any more — only the
    ``None``-mode fallback in ``_log_file_for_mode`` would, and neither daemon
    runs that way.  So the old assertion could only ever time out.
    (``routes/logs.py`` still lists ``_LEGACY_LOG_NAME``, but purely so a
    pre-split device's history stays readable in the dashboard.)
    """
    return _log_dir() / "metixel-backend.log"


def _frontend_log_path() -> Path:
    """The frontend's OWN log file — reported for diagnostics on failure."""
    return _log_dir() / "metixel-frontend.log"


def _log_dir_contents(backend_log: Path) -> str:
    """List what is actually on disk, to make a failure actionable.

    A bare "file not created" assertion cannot distinguish between "the config
    round-trip produced no log records" and "the handler is writing a
    different filename" — which is exactly the drift this test must catch.
    """
    log_dir = backend_log.parent
    try:
        present = sorted(p.name for p in log_dir.iterdir())
    except OSError as exc:
        present = [f"<unreadable: {exc}>"]
    return f"contents of {log_dir}: {present}"


def test_log_file_owned_by_pi_and_writes_logs() -> None:
    """The backend's OWN log must be owned by pi:pi and receive writes.

    The backend runs as the ``pi`` user, so a log file created by root (e.g.
    a one-off root invocation during install) would make the pi-run service
    fail to open it.  We verify ownership, then toggle the file log level to
    INFO (from the current value, normally NONE), trigger an INFO log write
    via a harmless config round-trip, and confirm the file grows.
    """
    backend_log = _backend_log_path()

    # Read the current level so we can restore it afterwards (normally NONE).
    current_level = "NONE"
    with contextlib.suppress(Exception):
        current_level = _api_get("/api/config/system").get("log_level", "NONE")

    try:
        # Enable INFO disk logging so the FileHandler actually writes.
        resp = _api_post("/api/logs/level", {"level": "INFO"})
        assert resp.get("status") == "ok", f"could not set log level to INFO: {resp}"

        # Record the file size (creating the file if needed) so we can detect
        # growth.  Wait a moment for the handler to open the file.
        size_before = backend_log.stat().st_size if backend_log.exists() else 0

        # Trigger INFO-level log lines with a harmless slideshow round-trip
        # (slideshow does NOT trigger a pipeline restart).
        section = _api_get(f"/api/config/{_TEST_SECTION}")
        original = section.get(_TEST_KEY)
        _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: _TEST_VALUE})
        if original is not None:
            _api_put(f"/api/config/{_TEST_SECTION}", {_TEST_KEY: original})

        # Give the RotatingFileHandler a moment to flush.
        deadline = time.monotonic() + 10
        size_after = size_before
        while time.monotonic() < deadline:
            if backend_log.exists():
                size_after = backend_log.stat().st_size
                if size_after > size_before:
                    break
            time.sleep(0.5)

        assert backend_log.exists(), (
            f"metixel-backend.log was not created at {backend_log} "
            f"(frontend log exists: {_frontend_log_path().exists()}) — "
            f"{_log_dir_contents(backend_log)}"
        )
        assert size_after > size_before, (
            "metixel-backend.log did not grow after enabling INFO logging + a "
            f"config round-trip (size {size_before} → {size_after})"
        )

        # The file must still be owned by pi after being (re)opened by the
        # pi-run backend — this is the real ownership regression guard.
        st = backend_log.stat()
        assert st.st_uid == 1000, f"metixel-backend.log not owned by pi (uid {st.st_uid})"
        assert st.st_gid == 1000, f"metixel-backend.log not owned by group pi (gid {st.st_gid})"

        # Sanity check the content actually looks like a log line.
        tail = backend_log.read_text(encoding="utf-8", errors="replace").splitlines()[-1]
        assert "[" in tail and "]" in tail, (
            f"last log line does not look like a log entry: {tail!r}"
        )
    finally:
        # Restore the original file log level (normally NONE) so the device
        # is left exactly as we found it.
        with contextlib.suppress(Exception):
            _api_post("/api/logs/level", {"level": current_level or "NONE"})
