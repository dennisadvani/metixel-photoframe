# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Google Photos sync API endpoints (Picker API flow)."""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest


@pytest.fixture
def fake_syncer():
    """MagicMock stand-in for the GooglePhotosSyncer used by the routes."""
    syncer = mock.MagicMock()
    syncer.mode = "picker"
    syncer.is_configured.return_value = True
    syncer.get_last_result.return_value = None
    syncer.read_session.return_value = None
    syncer.refresh_session.return_value = None
    return syncer


@pytest.fixture(autouse=True)
def patch_syncer(monkeypatch, fake_syncer):
    """Redirect the route's syncer factory to the fake."""
    import metixel.backend.web.routes.google_photos as gp_routes

    monkeypatch.setattr(gp_routes, "_get_or_create_syncer", lambda state: fake_syncer)


def _session(**overrides):
    session = {
        "id": "sess-1",
        "picker_uri": "https://photos.google.com/pick?x=1",
        "expire_time": "2999-01-01T00:00:00Z",
        "media_items_set": False,
        "picked_count": 0,
        "imported_at": 0.0,
        "poll_interval_seconds": 3,
        "created_at": time.time(),
    }
    session.update(overrides)
    return session


# ---------------------------------------------------------------------------
# GET /api/google-photos/status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_never_run(self, client, fake_syncer) -> None:
        resp = client.get("/api/google-photos/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "never_run"
        assert data["last_sync"] is None
        assert data["session"] is None
        assert data["configured"] is True

    def test_reports_the_active_mode(self, client, fake_syncer) -> None:
        assert json.loads(client.get("/api/google-photos/status").data)["mode"] == "picker"
        fake_syncer.mode = "shared_url"
        assert json.loads(client.get("/api/google-photos/status").data)["mode"] == "shared_url"

    def test_reports_unconfigured(self, client, fake_syncer) -> None:
        fake_syncer.is_configured.return_value = False
        data = json.loads(client.get("/api/google-photos/status").data)
        assert data["configured"] is False

    def test_last_result_and_session(self, client, fake_syncer) -> None:
        result = mock.MagicMock()
        result.to_dict.return_value = {"downloaded": 3, "success": True}
        fake_syncer.get_last_result.return_value = result
        fake_syncer.read_session.return_value = _session(media_items_set=True, picked_count=3)

        data = json.loads(client.get("/api/google-photos/status").data)

        assert data["status"] == "ok"
        assert data["last_sync"] == {"downloaded": 3, "success": True}
        assert data["session"]["media_items_set"] is True
        assert data["session"]["picked_count"] == 3
        # A live session reports a countdown rather than a raw timestamp.
        assert data["session"]["expires_in_seconds"] > 0
        assert data["session"]["expired"] is False

    def test_expired_session_is_flagged(self, client, fake_syncer) -> None:
        fake_syncer.read_session.return_value = _session(
            expire_time="1970-01-01T00:00:00Z", media_items_set=True
        )
        data = json.loads(client.get("/api/google-photos/status").data)
        assert data["session"]["expired"] is True
        assert data["session"]["expires_in_seconds"] == 0.0


# ---------------------------------------------------------------------------
# POST/GET /api/google-photos/session
# ---------------------------------------------------------------------------


class TestPickingSession:
    def test_start_returns_the_picker_uri(self, client, fake_syncer) -> None:
        fake_syncer.create_session.return_value = _session()

        resp = client.post("/api/google-photos/session", json={})

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["session"]["picker_uri"] == "https://photos.google.com/pick?x=1"
        assert data["session"]["poll_interval_seconds"] == 3

    def test_start_requires_credentials(self, client, fake_syncer) -> None:
        fake_syncer.is_configured.return_value = False

        resp = client.post("/api/google-photos/session", json={})

        assert resp.status_code == 400
        assert "not connected" in json.loads(resp.data)["error"]
        fake_syncer.create_session.assert_not_called()

    def test_start_upstream_failure_is_502(self, client, fake_syncer) -> None:
        fake_syncer.create_session.side_effect = RuntimeError("boom")

        resp = client.post("/api/google-photos/session", json={})

        assert resp.status_code == 502
        assert "boom" in json.loads(resp.data)["error"]

    def test_get_refreshes_from_google(self, client, fake_syncer) -> None:
        fake_syncer.refresh_session.return_value = _session(media_items_set=True)

        data = json.loads(client.get("/api/google-photos/session").data)

        fake_syncer.refresh_session.assert_called_once()
        assert data["session"]["media_items_set"] is True

    def test_get_falls_back_to_the_stored_session(self, client, fake_syncer) -> None:
        """A failed refresh must still answer with what we know."""
        fake_syncer.refresh_session.side_effect = RuntimeError("offline")
        fake_syncer.read_session.return_value = _session()

        resp = client.get("/api/google-photos/session")

        assert resp.status_code == 200
        assert json.loads(resp.data)["session"]["id"] == "sess-1"

    def test_get_without_a_session(self, client, fake_syncer) -> None:
        data = json.loads(client.get("/api/google-photos/session").data)
        assert data["session"] is None

    def test_start_is_refused_in_shared_url_mode(self, client, fake_syncer) -> None:
        fake_syncer.mode = "shared_url"

        resp = client.post("/api/google-photos/session", json={})

        assert resp.status_code == 409
        fake_syncer.create_session.assert_not_called()

    def test_clear(self, client, fake_syncer) -> None:
        fake_syncer.clear_session.return_value = True

        data = json.loads(client.post("/api/google-photos/session/clear", json={}).data)

        assert data == {"status": "ok", "cleared": True}
        fake_syncer.clear_session.assert_called_once()


# ---------------------------------------------------------------------------
# POST /api/google-photos/sync  and  /cancel
# ---------------------------------------------------------------------------


class TestSync:
    def test_starts_in_the_background(self, client, fake_syncer) -> None:
        fake_syncer.read_session.return_value = _session(media_items_set=True)
        fake_syncer.sync_once.return_value = mock.MagicMock(to_dict=lambda: {})

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "started"

    def test_requires_credentials(self, client, fake_syncer) -> None:
        fake_syncer.is_configured.return_value = False

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 400
        fake_syncer.sync_once.assert_not_called()

    def test_requires_a_picked_session(self, client, fake_syncer) -> None:
        fake_syncer.read_session.return_value = None

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 409
        assert "picked" in json.loads(resp.data)["error"]
        fake_syncer.sync_once.assert_not_called()

    def test_shared_url_mode_needs_no_session(self, client, fake_syncer) -> None:
        fake_syncer.mode = "shared_url"
        fake_syncer.read_session.return_value = None
        fake_syncer.sync_once.return_value = mock.MagicMock(to_dict=lambda: {})

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "started"

    def test_cancel(self, client, fake_syncer) -> None:
        resp = client.post("/api/google-photos/cancel", json={})

        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        fake_syncer.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# POST /api/google-photos/test-connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    @pytest.fixture
    def probe(self, monkeypatch):
        import metixel.backend.sync.google_photos as gp

        calls: list[tuple] = []

        def _probe(client_id, client_secret, refresh_token, http=None):
            calls.append((client_id, client_secret, refresh_token))
            return {"ok": True, "message": "Connected to Google Photos"}

        monkeypatch.setattr(gp, "probe_credentials", _probe)
        return calls

    def test_uses_the_posted_credentials(self, client, probe) -> None:
        resp = client.post(
            "/api/google-photos/test-connection",
            json={"client_id": "cid", "client_secret": "sec", "refresh_token": "ref"},
        )

        assert resp.status_code == 200
        assert json.loads(resp.data)["ok"] is True
        assert probe == [("cid", "sec", "ref")]

    def test_falls_back_to_the_saved_credentials(self, client, mock_state, probe) -> None:
        mock_state.update_config(
            "sync",
            {
                "google_photos": {
                    "client_id": "saved-id",
                    "client_secret": "saved-secret",
                    "refresh_token": "saved-token",
                }
            },
        )

        client.post("/api/google-photos/test-connection", json={})

        assert probe == [("saved-id", "saved-secret", "saved-token")]

    def test_shared_url_mode_probes_the_album(self, client, monkeypatch) -> None:
        import metixel.backend.sync.google_photos as gp

        seen: list[str] = []

        def _probe(url, **_kwargs):
            seen.append(url)
            return {"ok": True, "message": "Read “Holiday” — 42 photo(s)", "count": 42}

        monkeypatch.setattr(gp, "probe_shared_album", _probe)

        resp = client.post(
            "/api/google-photos/test-connection",
            json={"mode": "shared_url", "shared_album_url": "https://photos.app.goo.gl/abc"},
        )

        assert resp.status_code == 200
        assert json.loads(resp.data)["count"] == 42
        assert seen == ["https://photos.app.goo.gl/abc"]

    def test_unknown_mode_is_rejected(self, client, probe) -> None:
        resp = client.post("/api/google-photos/test-connection", json={"mode": "telepathy"})

        assert resp.status_code == 400
        assert probe == []

    def test_rejects_non_string_fields(self, client, probe) -> None:
        resp = client.post("/api/google-photos/test-connection", json={"client_id": 7})

        assert resp.status_code == 400
        assert probe == []

    def test_upstream_failure_is_reported_in_the_body(self, client, monkeypatch) -> None:
        """Google's 401 must not surface as our own 401."""
        import metixel.backend.sync.google_photos as gp

        monkeypatch.setattr(
            gp,
            "probe_credentials",
            lambda *a, **k: {"ok": False, "status": 401, "error": "Google rejected it"},
        )

        resp = client.post(
            "/api/google-photos/test-connection",
            json={"client_id": "c", "client_secret": "s", "refresh_token": "r"},
        )

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["ok"] is False
        assert body["status"] == 401
