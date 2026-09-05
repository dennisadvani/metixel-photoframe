# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Immich sync API endpoints (multi-album)."""

from __future__ import annotations

import json
from unittest import mock

import pytest


@pytest.fixture
def fake_syncer():
    """MagicMock stand-in for the ImmichSyncer used by the routes."""
    return mock.MagicMock()


@pytest.fixture(autouse=True)
def patch_syncer(monkeypatch, fake_syncer):
    """Redirect the route's syncer factory to the fake."""
    import metixel.backend.web.routes.immich as immich_routes

    monkeypatch.setattr(immich_routes, "_get_or_create_syncer", lambda state: fake_syncer)


# ---------------------------------------------------------------------------
# GET /api/immich/albums
# ---------------------------------------------------------------------------


class TestListAlbums:
    def test_returns_sorted_simplified_list(self, client, fake_syncer) -> None:
        fake_syncer._list_albums.return_value = [
            {"id": "b", "albumName": "Zoo", "assetCount": 2},
            {"id": "a", "albumName": "Alpha", "assetCount": 10},
        ]
        resp = client.get("/api/immich/albums")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == [
            {"id": "a", "name": "Alpha", "assetCount": 10},
            {"id": "b", "name": "Zoo", "assetCount": 2},
        ]

    def test_capped_at_5000(self, client, fake_syncer) -> None:
        albums = [{"id": str(i), "albumName": f"A{i:04d}", "assetCount": 1} for i in range(6000)]
        fake_syncer._list_albums.return_value = albums
        resp = client.get("/api/immich/albums")
        data = json.loads(resp.data)
        assert len(data) == 5000

    def test_error_returns_502(self, client, fake_syncer) -> None:
        fake_syncer._list_albums.side_effect = RuntimeError("boom")
        resp = client.get("/api/immich/albums")
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /api/immich/albums/add
# ---------------------------------------------------------------------------


class TestAddAlbum:
    def test_adds_to_config(self, client, mock_state) -> None:
        resp = client.post("/api/immich/albums/add", json={"id": "abc", "name": "Family"})
        assert resp.status_code == 200
        albums = mock_state.config.sync["immich"]["albums"]
        assert {"id": "abc", "name": "Family"} in albums

    def test_duplicate_id_not_added(self, client, mock_state) -> None:
        mock_state.update_config("sync", {"immich": {"albums": [{"id": "abc", "name": "Family"}]}})
        resp = client.post("/api/immich/albums/add", json={"id": "abc", "name": "Family"})
        assert resp.status_code == 200
        albums = mock_state.config.sync["immich"]["albums"]
        assert len(albums) == 1

    def test_missing_fields_400(self, client) -> None:
        resp = client.post("/api/immich/albums/add", json={"id": "abc"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/immich/albums/remove
# ---------------------------------------------------------------------------


class TestRemoveAlbum:
    def test_removes_from_config_and_deletes_folder(self, client, mock_state, tmp_path) -> None:
        album_dir = tmp_path / "album_abc"
        album_dir.mkdir(parents=True)
        (album_dir / "photo.jpg").write_bytes(b"X")

        mock_state.update_config(
            "sync",
            {
                "immich": {
                    "albums": [{"id": "abc", "name": "Family"}],
                    "sync_dir": str(tmp_path),
                }
            },
        )

        resp = client.post("/api/immich/albums/remove", json={"id": "abc"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["deleted_folder"] is True
        # Removed from config and folder gone
        assert mock_state.config.sync["immich"]["albums"] == []
        assert not album_dir.exists()

    def test_missing_id_400(self, client) -> None:
        resp = client.post("/api/immich/albums/remove", json={})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/immich/sync  +  GET /api/immich/status  +  POST /api/immich/cancel
# ---------------------------------------------------------------------------


class TestSyncTrigger:
    def test_sync_starts_in_background(self, client, fake_syncer) -> None:
        resp = client.post("/api/immich/sync", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "started"
        # sync_once should be invoked (in a background thread).
        # Give the thread a moment to call it.
        import time

        for _ in range(50):
            if fake_syncer.sync_once.called:
                break
            time.sleep(0.01)
        assert fake_syncer.sync_once.called

    def test_status_returns_never_run(self, client, fake_syncer) -> None:
        fake_syncer.get_last_result.return_value = None
        resp = client.get("/api/immich/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "never_run"

    def test_cancel(self, client, fake_syncer) -> None:
        resp = client.post("/api/immich/cancel")
        assert resp.status_code == 200
        fake_syncer.cancel.assert_called_once()
