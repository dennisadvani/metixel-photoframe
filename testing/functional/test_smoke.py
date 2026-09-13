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
import urllib.error
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


def test_strict_health_gate_passes_when_frontend_runs() -> None:
    """The OTA gate's strict probe must PASS on a healthy, running stack.

    ``scripts/update.sh`` gates on ``/api/health?require=render``, which answers
    503 unless the frontend is demonstrably alive (heartbeat present, fresh,
    and from a process whose identity has been stable for a few seconds).

    This is the "can it pass?" half of that contract; the "can it fail?" half is
    covered by unit tests with a fake heartbeat.  Without this check a subtle
    bug in the liveness signal would make every future update roll back.

    It POLLS rather than probing once, mirroring ``update.sh``.  The liveness
    check deliberately requires ~8s of observed stability, and both
    ``metixel-backend`` and ``metixel-cage`` are restarted together on an OTA —
    so immediately after a restart the honest verdict IS "not yet alive".  A
    single-shot probe would flake on any freshly-restarted stack.
    """
    deadline = time.monotonic() + _BOOT_WAIT
    last: object = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{BACKEND_PORT}/api/health?require=render", timeout=10
            ) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    frontend = data["liveness"]["frontend"]
                    assert data["healthy"] is True, data
                    assert data["status"] == "healthy", data
                    assert frontend["alive"] is True, frontend
                    assert frontend["pid"], "liveness verdict should name the frontend pid"
                    return
        except urllib.error.HTTPError as exc:
            # 503 carries the reason — keep it for the failure message.
            try:
                last = json.loads(exc.read().decode()).get("liveness", {}).get("frontend")
            except Exception:  # noqa: BLE001 - diagnostics must not mask the real failure
                last = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - backend may be mid-restart
            last = str(exc)
        time.sleep(2)

    pytest.fail(
        f"strict health gate did not pass within {_BOOT_WAIT}s — the OTA updater "
        f"would roll back every release. Last verdict: {last}"
    )


def test_heartbeat_file_is_published() -> None:
    """The frontend must publish its heartbeat into the runtime directory.

    The heartbeat is the only thing that lets the OTA gate distinguish a
    rendering frontend from a crash-looping one, so its absence would silently
    disable the protection (the gate would fail closed and roll everything
    back instead).
    """
    from pathlib import Path

    heartbeat = Path("/run/metixel/frontend_heartbeat.json")
    assert heartbeat.is_file(), (
        "frontend heartbeat missing — the OTA health gate has no signal to read"
    )
    data = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert data.get("pid"), f"heartbeat has no pid: {data}"
    # Must be recent: the render loop rewrites it every ~10s.
    age = time.time() - heartbeat.stat().st_mtime
    assert age < 60, f"heartbeat is {age:.0f}s old — the render loop may be stalled"


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
