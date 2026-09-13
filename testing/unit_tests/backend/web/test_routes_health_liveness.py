# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for ``GET /api/health``'s ability to FAIL.

The OTA gate (``scripts/update.sh``) probes ``/api/health?require=render``.
Before this existed the endpoint always answered 200, so a release whose
frontend crash-looped passed the gate and was declared a success — black
screen, no rollback.  These tests pin both halves of the contract:

  * the DEFAULT stays 200, because the dashboard's ``apiGet`` treats a non-2xx
    as a hard failure and would blank itself, and a deliberately headless
    device must not look permanently broken;
  * the ``?require=render`` form returns 503 when the frontend is not alive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from metixel.backend.frontend_liveness import FrontendLiveness
from metixel.backend.web.server import create_app


class FakeDaemon:
    """Minimal daemon stub exposing only what the health route reads."""

    def __init__(self, tracker: FrontendLiveness | None) -> None:
        self._display_on = True
        if tracker is not None:
            self.frontend_liveness = tracker


@pytest.fixture
def heartbeat(tmp_path: Path) -> Path:
    return tmp_path / "run" / "frontend_heartbeat.json"


@pytest.fixture
def make_client(mock_state, mock_ipc, mock_update_manager, heartbeat, monkeypatch):
    """Build a real app whose daemon reports the given heartbeat state.

    A short stale window keeps the tests fast; the tracker's own behaviour is
    covered in ``test_frontend_liveness.py``.
    """

    def _factory(*, alive: bool, tracker: bool = True) -> Any:
        if tracker:
            if alive:
                # A heartbeat from a process that has been running a while.
                heartbeat.parent.mkdir(parents=True, exist_ok=True)
                heartbeat.write_text(
                    json.dumps({"pid": 4321, "boot_id": "", "uptime": 90.0, "queue_len": 2}),
                    encoding="utf-8",
                )
                ln = FrontendLiveness(heartbeat, stale_after=300.0, stable_after=0.0)
                ln.snapshot()  # prime the identity clock
            else:
                # No heartbeat at all — the frontend never came up.
                ln = FrontendLiveness(heartbeat, stale_after=5.0, stable_after=2.0)
            daemon = FakeDaemon(ln)
        else:
            daemon = FakeDaemon(None)
        app = create_app(
            mock_state,
            mock_ipc,
            opt_queue=None,
            update_mgr=mock_update_manager,
            daemon=daemon,
        )
        return app.test_client()

    return _factory


class TestDefaultIsLenient:
    def test_returns_200_when_frontend_is_dead(self, make_client) -> None:
        """The dashboard contract must not change: 200 even with no frontend.

        A non-2xx here would make ``apiGet`` return null and blank the whole
        dashboard — and would make a deliberately headless device look broken.
        """
        client = make_client(alive=False)

        resp = client.get("/api/health")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["healthy"] is True
        assert body["required"] == []
        # The verdict is still published, just not enforced.
        assert body["liveness"]["frontend"]["alive"] is False

    def test_default_reports_alive_frontend(self, make_client) -> None:
        client = make_client(alive=True)

        body = json.loads(client.get("/api/health").data)

        assert body["liveness"]["frontend"]["alive"] is True


class TestRequireRender:
    def test_503_when_frontend_is_dead(self, make_client) -> None:
        """THE regression: a crash-looping frontend must fail the gate."""
        client = make_client(alive=False)

        resp = client.get("/api/health?require=render")

        assert resp.status_code == 503
        body = json.loads(resp.data)
        assert body["healthy"] is False
        assert body["status"] == "unhealthy"
        assert body["required"] == ["render"]
        assert "render" in body["unhealthy_checks"]
        # The reason is in the body so the OTA log explains the failure.
        assert body["liveness"]["frontend"]["reason"]

    def test_200_when_frontend_is_alive(self, make_client) -> None:
        client = make_client(alive=True)

        resp = client.get("/api/health?require=render")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["healthy"] is True
        assert body["status"] == "healthy"

    def test_any_and_all_are_accepted_aliases(self, make_client) -> None:
        client = make_client(alive=False)

        for value in ("any", "all", "render"):
            assert client.get(f"/api/health?require={value}").status_code == 503

    def test_unknown_requirement_does_not_enable_the_gate(self, make_client) -> None:
        """A typo must not silently *disable* a gate the caller asked for.

        Unknown values are ignored, so the request behaves like the default —
        importantly, ``?require=rendr`` must not be read as "check nothing and
        pass", which is what a truthiness check on the raw string would do.
        """
        client = make_client(alive=False)

        resp = client.get("/api/health?require=rendr")

        assert resp.status_code == 200


class TestMissingTracker:
    def test_unknown_liveness_fails_the_strict_gate(self, make_client) -> None:
        """With no tracker, ``?require=render`` cannot be satisfied → 503.

        Fail-closed is deliberate: the strict form asserts "a live renderer is
        required", and ``None`` means that cannot be demonstrated.  In
        production the tracker is constructed in ``BackendDaemon.__init__``
        before the web server starts, so this only arises from a wiring bug —
        which is precisely when the gate should refuse, not wave through.
        """
        client = make_client(alive=False, tracker=False)

        resp = client.get("/api/health?require=render")

        assert resp.status_code == 503
        body = json.loads(resp.data)
        assert body["liveness"]["frontend"]["state"] == "unknown"
        assert body["liveness"]["frontend"]["alive"] is None

    def test_unknown_liveness_still_returns_200_by_default(self, make_client) -> None:
        """Without the query param, an unknown state is not an error.

        The dashboard (and any monitoring that predates the liveness signal)
        must keep working against a daemon that does not report it.
        """
        client = make_client(alive=False, tracker=False)

        resp = client.get("/api/health")

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["healthy"] is True
        assert body["liveness"]["frontend"]["state"] == "unknown"
