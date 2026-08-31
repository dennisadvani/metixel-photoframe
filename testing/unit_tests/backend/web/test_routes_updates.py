# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""OTA update API endpoints — status and on-demand checks."""

from __future__ import annotations

import json


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
        mock_update_manager.check_for_updates_async.assert_called_once_with(force=True)


class TestUpdateApply:
    def test_apply_passes_keep_existing(self, client, mock_update_manager):
        mock_update_manager.apply_update.return_value = {"status": "ok"}
        resp = client.post(
            "/api/updates/apply",
            json={"version": "2.0.0", "keep_existing": True},
        )
        assert resp.status_code == 200
        mock_update_manager.apply_update.assert_called_once_with(
            channel=None, version="2.0.0", keep_existing=True
        )

    def test_apply_defaults_keep_existing_false(self, client, mock_update_manager):
        mock_update_manager.apply_update.return_value = {"status": "ok"}
        resp = client.post("/api/updates/apply", json={"version": "2.0.0"})
        assert resp.status_code == 200
        mock_update_manager.apply_update.assert_called_once_with(
            channel=None, version="2.0.0", keep_existing=False
        )


class TestUpdateReleases:
    def test_list_releases(self, client, mock_update_manager):
        mock_update_manager.list_releases.return_value = [
            {"version": "2.0.0", "prerelease": False}
        ]
        resp = client.get("/api/updates/releases")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["releases"][0]["version"] == "2.0.0"


class TestUpdateRollback:
    def test_rollback_requires_version(self, client):
        resp = client.post("/api/updates/rollback", json={})
        assert resp.status_code == 400

    def test_rollback_calls_manager(self, client, mock_update_manager):
        mock_update_manager.rollback.return_value = {"status": "ok"}
        resp = client.post("/api/updates/rollback", json={"version": "1.2.3"})
        assert resp.status_code == 200
        mock_update_manager.rollback.assert_called_once_with("1.2.3")


class TestUpdateAptUpgrade:
    def test_apt_upgrade_calls_manager(self, client, mock_update_manager):
        mock_update_manager.apt_upgrade.return_value = {"status": "ok"}
        resp = client.post("/api/updates/apt-upgrade")
        assert resp.status_code == 200
        mock_update_manager.apt_upgrade.assert_called_once()


class TestUpdateAutoUpdate:
    def test_auto_update_calls_manager(self, client, mock_update_manager):
        mock_update_manager.set_auto_update.return_value = {"status": "ok"}
        resp = client.put(
            "/api/updates/auto-update",
            json={"enabled": False, "day": 3, "time": "04:30"},
        )
        assert resp.status_code == 200
        mock_update_manager.set_auto_update.assert_called_once_with(
            enabled=False, day=3, time_str="04:30"
        )
