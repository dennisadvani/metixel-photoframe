# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""OTA update API endpoints — status and on-demand checks."""

from __future__ import annotations

import json
import time


def _wait_for_call(callable_mock, timeout: float = 2.0) -> None:
    """Busy-wait until the background thread has invoked the mock."""
    deadline = time.time() + timeout
    while time.time() < deadline and callable_mock.call_count == 0:
        time.sleep(0.01)


class TestUpdateStatus:
    def test_returns_manager_status(self, client, mock_update_manager):
        mock_update_manager.get_status.return_value = {"current": "1.0.0", "channel": "stable"}
        resp = client.get("/api/updates/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["current"] == "1.0.0"
        assert data["channel"] == "stable"
        mock_update_manager.get_status.assert_called_once()

    def test_status_without_manager_returns_500(self, app):
        app.config["METIXEL_UPDATE_MGR"] = None
        resp = app.test_client().get("/api/updates/status")
        assert resp.status_code == 500
        assert "error" in json.loads(resp.data)


class TestUpdateCheck:
    def test_check_without_manager_returns_503(self, app):
        app.config["METIXEL_UPDATE_MGR"] = None
        resp = app.test_client().post("/api/updates/check")
        assert resp.status_code == 503

    def test_check_launches_background_check(self, client, mock_update_manager):
        resp = client.post("/api/updates/check", query_string={"force": "true"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        _wait_for_call(mock_update_manager.check_for_updates)
        mock_update_manager.check_for_updates.assert_called_once_with(force=True)
