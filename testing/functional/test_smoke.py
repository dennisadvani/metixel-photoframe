# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional smoke test: verify the running Metixel stack boots and serves.

This is the "did I ship a broken app?" check.  Unlike the other functional
tests (which exercise components in isolation), this verifies the INTEGRATED
stack on the Pi:

    * backend + frontend services are active (not crash-looping)
    * the backend HTTP API is serving on :8080
    * /api/health returns real system data
    * the frontend is rendering (process alive, no fatal errors)

It runs against the RUNNING services — the backend and cage frontend must be
up (e.g. after a sync + restart, or a fresh boot).  Run it after deploying a
change to catch boot/serve regressions before the slower functional/E2E
suites.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request

import pytest

pytestmark = pytest.mark.functional

#: Backend HTTP port (Flask serves here; nginx proxies :80 → :8080).
BACKEND_PORT = 8080
#: How long to wait for the backend to come up after a restart.
_BOOT_WAIT = 60


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _service_active(unit: str) -> bool:
    result = _run(["systemctl", "is-active", unit])
    return result.stdout.strip() == "active"


def _restart_count(unit: str) -> int:
    """Return the number of times the unit has restarted (crash-loop check)."""
    result = _run(["systemctl", "show", "-p", "NRestarts", "--value", unit])
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _wait_for_health(timeout: int = _BOOT_WAIT) -> bool:
    """Poll the health endpoint until the backend responds or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{BACKEND_PORT}/api/health", timeout=5
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def test_backend_service_active() -> None:
    """The backend daemon must be running (not crash-looping)."""
    assert _service_active("metixel-backend"), "metixel-backend is not active"
    # A high restart count indicates a crash-loop — the app is broken.
    assert _restart_count("metixel-backend") < 5, (
        f"metixel-backend has restarted {_restart_count('metixel-backend')} times (crash-loop?)"
    )


def test_frontend_service_active() -> None:
    """The frontend (cage) must be running."""
    assert _service_active("metixel-cage"), "metixel-cage is not active"


def test_health_endpoint_serves() -> None:
    """The backend HTTP API must be serving on :8080."""
    assert _wait_for_health(), (
        f"backend did not respond on http://127.0.0.1:{BACKEND_PORT}/api/health "
        f"within {_BOOT_WAIT}s"
    )


def test_health_returns_real_data() -> None:
    """The health endpoint must return real system metrics."""
    with urllib.request.urlopen(f"http://127.0.0.1:{BACKEND_PORT}/api/health", timeout=10) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())

    # Real system data must be present (not an empty/error response).
    assert "uptime_seconds" in data, "health response missing uptime_seconds"
    assert "cpu_percent" in data, "health response missing cpu_percent"
    assert "memory_percent" in data, "health response missing memory_percent"
    assert "disk_used_percent" in data, "health response missing disk_used_percent"
    assert data["uptime_seconds"] >= 0
    assert 0 <= data["cpu_percent"] <= 100
    assert 0 <= data["memory_percent"] <= 100


def test_frontend_rendering() -> None:
    """The frontend must be rendering (no fatal errors in the journal)."""
    # Check the cage/frontend journal for fatal errors in the last 5 minutes.
    result = _run(
        [
            "journalctl",
            "-u",
            "metixel-cage",
            "--since",
            "5 min ago",
            "-p",
            "err",
            "--no-pager",
        ]
    )
    # A non-zero exit means no error-level messages — good.
    # If there are errors, they must not be fatal (traceback / crash).
    if result.returncode == 0 and result.stdout.strip():
        fatal = [
            line for line in result.stdout.splitlines() if "Traceback" in line or "Fatal" in line
        ]
        assert not fatal, f"frontend journal has fatal errors:\n{result.stdout}"
