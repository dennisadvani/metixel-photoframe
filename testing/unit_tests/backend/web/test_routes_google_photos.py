# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Google Photos sync API endpoints (shared-album sync)."""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

import metixel.backend.web.routes.google_photos as _gp_routes

#: The real factory, captured before the autouse fixture swaps in the fake.
_REAL_FACTORY = _gp_routes._get_or_create_syncer


@pytest.fixture
def fake_syncer():
    """MagicMock stand-in for the GooglePhotosSyncer used by the routes."""
    syncer = mock.MagicMock()
    syncer.is_configured.return_value = True
    syncer.get_last_result.return_value = None
    return syncer


@pytest.fixture(autouse=True)
def patch_syncer(monkeypatch, fake_syncer):
    """Redirect the route's syncer factory to the fake."""
    monkeypatch.setattr(_gp_routes, "_get_or_create_syncer", lambda state: fake_syncer)


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
        assert data["configured"] is True

    def test_reports_unconfigured(self, client, fake_syncer) -> None:
        fake_syncer.is_configured.return_value = False
        data = json.loads(client.get("/api/google-photos/status").data)
        assert data["configured"] is False

    def test_last_result(self, client, fake_syncer) -> None:
        fake_syncer.get_last_result.return_value = mock.MagicMock(
            to_dict=lambda: {"downloaded": 3, "album_title": "Holiday"}
        )
        data = json.loads(client.get("/api/google-photos/status").data)
        assert data["status"] == "ok"
        assert data["last_sync"]["downloaded"] == 3

    def test_no_picker_fields_remain(self, client) -> None:
        data = json.loads(client.get("/api/google-photos/status").data)
        assert "session" not in data
        assert "mode" not in data


class TestRetiredPickerEndpoints:
    @pytest.mark.parametrize(
        "path", ["/api/google-photos/session", "/api/google-photos/session/clear"]
    )
    def test_session_endpoints_are_gone(self, client, path) -> None:
        assert client.post(path, json={}).status_code in (404, 405)

    def test_session_get_no_longer_returns_a_session(self, client) -> None:
        # Unknown GETs fall through to the SPA catch-all (index.html), so the
        # check is that no session JSON comes back, not the status code.
        resp = client.get("/api/google-photos/session")
        assert not resp.is_json


# ---------------------------------------------------------------------------
# POST /api/google-photos/sync  and  /cancel
# ---------------------------------------------------------------------------


class TestSync:
    def test_starts_in_the_background(self, client, fake_syncer) -> None:
        fake_syncer.sync_once.return_value = mock.MagicMock(to_dict=lambda: {})

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "started"

    def test_requires_an_album_link(self, client, fake_syncer) -> None:
        fake_syncer.is_configured.return_value = False

        resp = client.post("/api/google-photos/sync", json={})

        assert resp.status_code == 400
        assert "shared album link" in json.loads(resp.data)["error"]
        fake_syncer.sync_once.assert_not_called()

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

        seen: list[str] = []

        def _probe(url, **_kwargs):
            seen.append(url)
            return {"ok": True, "message": "Read “Holiday” — 42 photo(s)", "count": 42}

        monkeypatch.setattr(gp, "probe_shared_album", _probe)
        return seen

    def test_probes_the_posted_album(self, client, probe) -> None:
        resp = client.post(
            "/api/google-photos/test-connection",
            json={"shared_album_url": "https://photos.app.goo.gl/abc"},
        )

        assert resp.status_code == 200
        assert json.loads(resp.data)["count"] == 42
        assert probe == ["https://photos.app.goo.gl/abc"]

    def test_falls_back_to_the_saved_album(self, client, mock_state, probe) -> None:
        mock_state.update_config(
            "sync", {"google_photos": {"shared_album_url": "https://photos.app.goo.gl/saved"}}
        )

        client.post("/api/google-photos/test-connection", json={})

        assert probe == ["https://photos.app.goo.gl/saved"]

    def test_rejects_a_non_string_url(self, client, probe) -> None:
        resp = client.post("/api/google-photos/test-connection", json={"shared_album_url": 7})

        assert resp.status_code == 400
        assert probe == []

    def test_upstream_failure_is_reported_in_the_body(self, client, monkeypatch) -> None:
        """A Google-side failure must not surface as our own HTTP error."""
        import metixel.backend.sync.google_photos as gp

        monkeypatch.setattr(
            gp,
            "probe_shared_album",
            lambda *a, **k: {"ok": False, "error": "Album not found (HTTP 404)"},
        )

        resp = client.post(
            "/api/google-photos/test-connection",
            json={"shared_album_url": "https://photos.app.goo.gl/abc"},
        )

        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["ok"] is False
        assert "404" in body["error"]


# ---------------------------------------------------------------------------
# Syncer instance sharing
# ---------------------------------------------------------------------------


class TestSyncerSharing:
    """The routes must drive the daemon's syncer, not a private copy of it."""

    def test_routes_use_the_daemons_syncer(self, app, mock_state, tmp_path) -> None:
        from metixel.backend.sync.google_photos import GooglePhotosSyncer

        daemon_syncer = mock.create_autospec(GooglePhotosSyncer, instance=True)
        # Taken before the ``is`` assert below narrows ``daemon_syncer`` to the
        # real class, where mypy would not know this method is a mock.
        reload_config: Any = daemon_syncer._reload_config
        app.config["METIXEL_DAEMON"] = mock.Mock(google_photos_syncer=daemon_syncer)

        with app.app_context():
            assert _REAL_FACTORY(mock_state) is daemon_syncer
        reload_config.assert_called_once()

    def test_falls_back_to_a_private_syncer_without_a_daemon(
        self, app, mock_state, monkeypatch
    ) -> None:
        from metixel.backend.sync.google_photos import GooglePhotosSyncer

        app.config["METIXEL_DAEMON"] = None
        monkeypatch.setattr(_gp_routes, "_syncer_cache", None)

        with app.app_context():
            first = _REAL_FACTORY(mock_state)
            assert isinstance(first, GooglePhotosSyncer)
            assert _REAL_FACTORY(mock_state) is first
