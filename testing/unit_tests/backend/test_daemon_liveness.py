# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Integration test: the daemon exposes the frontend-liveness tracker.

The OTA health gate probes ``/api/health?require=render`` and fails closed
when the tracker reports "unknown".  Since ``BackendDaemon.__init__`` must
construct the tracker *before* the web server starts, a wiring regression
(renaming/removing the attribute) would make every update report 503 — a
device bricked by a typo, not by a bad release.

``web/conftest.py`` passes ``daemon=None``, so the route tests cannot catch
that; this one builds a real daemon and asserts the route sees it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metixel.backend.frontend_liveness import FrontendLiveness


class FakeIPC:
    """IPCClient stand-in (avoids the Pi-only Unix socket)."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        pass


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch):
    """A real BackendDaemon with a fake IPC client."""
    import metixel.backend.daemon as daemon_mod
    from metixel.shared.config import Config

    config_path = tmp_path / "config.json"
    Config().save(config_path)
    monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return daemon_mod.BackendDaemon(config_path)


class TestDaemonExposesLiveness:
    def test_tracker_is_constructed(self, daemon) -> None:
        """The attribute the route reads must exist on a real daemon."""
        assert isinstance(daemon.frontend_liveness, FrontendLiveness)

    def test_route_reports_missing_without_a_heartbeat(self, daemon) -> None:
        """A frontend that never started → ``?require=render`` must 503.

        This is the black-screen case the gate exists to catch.
        """
        from metixel.backend.web.server import create_app

        app = create_app(daemon._state, daemon._ipc, opt_queue=None, update_mgr=None, daemon=daemon)

        resp = app.test_client().get("/api/health?require=render")

        assert resp.status_code == 503
        body = json.loads(resp.data)
        assert body["liveness"]["frontend"]["state"] == "missing"
        assert body["unhealthy_checks"] == ["render"]

    def test_route_reports_alive_with_a_stable_heartbeat(self, daemon, tmp_path: Path) -> None:
        """A stable frontend process must pass the strict gate.

        Otherwise the fix would simply break every future update.
        """
        from metixel.backend.web.server import create_app

        heartbeat = tmp_path / "run" / "frontend_heartbeat.json"
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(
            json.dumps({"pid": 4242, "boot_id": "", "uptime": 300.0, "queue_len": 5}),
            encoding="utf-8",
        )
        # Stable window satisfied: the same identity has been beating a while.
        tracker = FrontendLiveness(heartbeat, stale_after=300.0, stable_after=0.0)
        tracker.snapshot()
        daemon.frontend_liveness = tracker

        app = create_app(daemon._state, daemon._ipc, opt_queue=None, update_mgr=None, daemon=daemon)

        resp = app.test_client().get("/api/health?require=render")

        assert resp.status_code == 200
        assert json.loads(resp.data)["liveness"]["frontend"]["alive"] is True
