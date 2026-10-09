# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the folder-delete endpoint (``POST /api/browse/delete``).

The route backs the "Delete" button in the folder-browser modal.  It is
recursive and restricted to ``<data dir>/media``, so these tests cover the
safety rails as carefully as the happy path: the media-root boundary, the
watch-path config rewrite, the Immich refusal, and the non-empty guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def media_root(tmp_path: Path, monkeypatch):
    """A ``<data dir>/media`` tree with ``data_dir()`` pointed at it."""
    import metixel.backend.web.routes.browse as browse_mod

    root = tmp_path / "data"
    media = root / "media"
    media.mkdir(parents=True)
    monkeypatch.setattr(browse_mod, "data_dir", lambda: root)
    return root


def _make_media(folder: Path, count: int = 1, ext: str = ".jpg") -> None:
    """Write *count* fake media files into *folder*."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"photo{i}{ext}").write_bytes(b"\xff\xd8\xff\xe0fake")


class TestDeleteFolderHappyPath:
    """Deleting an empty or confirmed folder."""

    def test_deletes_empty_folder(self, client, media_root: Path):
        target = media_root / "media" / "empty"
        target.mkdir(parents=True)

        resp = client.post("/api/browse/delete", json={"path": str(target)})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["deleted"] is True
        assert data["media_removed"] == 0
        assert not target.exists()

    def test_deletes_folder_tree_recursively(self, client, media_root: Path):
        target = media_root / "media" / "holiday"
        _make_media(target / "2025", 1)
        _make_media(target / "2026" / "summer", 2)

        resp = client.post("/api/browse/delete", json={"path": str(target), "force": True})

        assert resp.status_code == 200
        assert resp.get_json()["media_removed"] == 3
        assert not target.exists()

    def test_relative_path_resolved_against_data_dir(self, client, media_root: Path):
        (media_root / "media" / "gone").mkdir(parents=True)

        resp = client.post("/api/browse/delete", json={"path": "media/gone/"})

        assert resp.status_code == 200
        assert not (media_root / "media" / "gone").exists()


class TestDeleteFolderNonEmptyGuard:
    """A non-empty folder requires an explicit ``force``."""

    def test_refuses_non_empty_without_force(self, client, media_root: Path):
        target = media_root / "media" / "photos"
        _make_media(target, 3)

        resp = client.post("/api/browse/delete", json={"path": str(target)})

        assert resp.status_code == 409
        data = resp.get_json()
        assert data["status"] == "error"
        assert data["media_count"] == 3
        # The folder must survive an unconfirmed request.
        assert target.is_dir()
        assert len(list(target.iterdir())) == 3

    def test_force_deletes_non_empty_folder(self, client, media_root: Path):
        target = media_root / "media" / "photos"
        _make_media(target, 3)

        resp = client.post("/api/browse/delete", json={"path": str(target), "force": True})

        assert resp.status_code == 200
        assert resp.get_json()["media_removed"] == 3
        assert not target.exists()

    def test_non_media_files_do_not_require_force(self, client, media_root: Path):
        """Only *media* counts — a folder of stray text files is "empty"."""
        target = media_root / "media" / "notes"
        target.mkdir(parents=True)
        (target / "readme.txt").write_text("hi", encoding="utf-8")

        resp = client.post("/api/browse/delete", json={"path": str(target)})

        assert resp.status_code == 200
        assert not target.exists()


class TestDeleteFolderDryRun:
    """``dry_run`` reports consequences without deleting anything."""

    def test_dry_run_reports_count_and_bytes(self, client, media_root: Path):
        target = media_root / "media" / "photos"
        _make_media(target, 2)

        resp = client.post("/api/browse/delete", json={"path": str(target), "dry_run": True})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["media_count"] == 2
        assert data["media_bytes"] > 0
        assert data["name"] == "photos"
        # Nothing may be removed by a dry run.
        assert target.is_dir()
        assert len(list(target.iterdir())) == 2

    def test_dry_run_on_empty_folder_reports_zero(self, client, media_root: Path):
        target = media_root / "media" / "empty"
        target.mkdir(parents=True)

        resp = client.post("/api/browse/delete", json={"path": str(target), "dry_run": True})

        assert resp.status_code == 200
        assert resp.get_json()["media_count"] == 0

    def test_dry_run_reports_watch_path_flag(self, client, media_root: Path, mock_state):
        target = media_root / "media" / "watched"
        target.mkdir(parents=True)
        mock_state.update_config("sync", {"local": {"watch_paths": ["media/watched/"]}})

        resp = client.post("/api/browse/delete", json={"path": str(target), "dry_run": True})

        assert resp.status_code == 200
        assert resp.get_json()["is_watch_path"] is True


class TestDeleteFolderBoundary:
    """The delete is fenced to ``<data dir>/media`` and never the root."""

    def test_refuses_media_root_itself(self, client, media_root: Path):
        media = media_root / "media"

        resp = client.post("/api/browse/delete", json={"path": str(media)})

        assert resp.status_code == 403
        assert media.is_dir()

    def test_refuses_outside_media_tree(self, client, media_root: Path, tmp_path: Path):
        """``cache/`` sits in the data tree but must never be deletable."""
        cache = media_root / "cache"
        cache.mkdir()
        (cache / "thing.dat").write_text("x", encoding="utf-8")

        resp = client.post("/api/browse/delete", json={"path": str(cache)})

        assert resp.status_code == 403
        assert cache.is_dir()

    def test_refuses_traversal_out_of_media_tree(self, client, media_root: Path):
        (media_root / "media" / "sub").mkdir(parents=True)
        outside = media_root / "outside"
        outside.mkdir()
        traversal = media_root / "media" / "sub" / ".." / ".." / "outside"

        resp = client.post("/api/browse/delete", json={"path": str(traversal)})

        assert resp.status_code == 403
        assert outside.is_dir()

    def test_refuses_symlink_escape(self, client, media_root: Path, tmp_path: Path):
        """A symlink pointing outside the media tree must not be followed."""
        elsewhere = tmp_path / "elsewhere"
        _make_media(elsewhere, 1)
        link = media_root / "media" / "link"
        link.symlink_to(elsewhere)

        resp = client.post("/api/browse/delete", json={"path": str(link), "force": True})

        assert resp.status_code == 403
        assert elsewhere.is_dir()

    def test_refuses_missing_folder(self, client, media_root: Path):
        resp = client.post("/api/browse/delete", json={"path": str(media_root / "media" / "nope")})
        assert resp.status_code == 404

    def test_requires_path(self, client):
        resp = client.post("/api/browse/delete", json={})
        assert resp.status_code == 400


class TestDeleteFolderWatchPath:
    """A deleted watch-path root is removed from config."""

    def test_removes_watch_path_entry(self, client, media_root: Path, mock_state):
        target = media_root / "media" / "watched"
        target.mkdir(parents=True)
        mock_state.update_config(
            "sync",
            {"local": {"watch_paths": [{"path": "media/watched/", "enabled": True}]}},
        )

        resp = client.post("/api/browse/delete", json={"path": str(target), "force": True})

        assert resp.status_code == 200
        assert resp.get_json()["removed_watch_path"] == "media/watched/"
        assert mock_state.config.sync["local"]["watch_paths"] == []
        assert not target.exists()

    def test_leaves_other_watch_paths_intact(self, client, media_root: Path, mock_state):
        (media_root / "media" / "a").mkdir(parents=True)
        (media_root / "media" / "b").mkdir(parents=True)
        mock_state.update_config(
            "sync",
            {"local": {"watch_paths": ["media/a/", "media/b/"]}},
        )

        resp = client.post(
            "/api/browse/delete", json={"path": str(media_root / "media" / "a"), "force": True}
        )

        assert resp.status_code == 200
        assert mock_state.config.sync["local"]["watch_paths"] == ["media/b/"]

    def test_plain_subfolder_does_not_touch_config(self, client, media_root: Path, mock_state):
        target = media_root / "media" / "watched" / "sub"
        target.mkdir(parents=True)
        mock_state.update_config("sync", {"local": {"watch_paths": ["media/watched/"]}})

        resp = client.post("/api/browse/delete", json={"path": str(target), "force": True})

        assert resp.status_code == 200
        assert resp.get_json()["removed_watch_path"] is None
        # The watch path itself is untouched — only the subfolder went.
        assert mock_state.config.sync["local"]["watch_paths"] == ["media/watched/"]


class TestDeleteFolderImmich:
    """The Immich sync folder is owned by the syncer and must be refused."""

    def test_refuses_sync_folder(self, client, media_root: Path, mock_state):
        sync_dir = media_root / "media" / "sync" / "immich"
        (sync_dir / "album").mkdir(parents=True)
        _make_media(sync_dir / "album", 1)
        mock_state.update_config("sync", {"immich": {"sync_dir": "media/sync/immich/"}})

        resp = client.post("/api/browse/delete", json={"path": str(sync_dir), "force": True})

        assert resp.status_code == 403
        assert "Immich" in resp.get_json()["error"]
        assert sync_dir.is_dir()

    def test_refuses_child_of_sync_folder(self, client, media_root: Path, mock_state):
        child = media_root / "media" / "sync" / "immich" / "album"
        child.mkdir(parents=True)
        mock_state.update_config("sync", {"immich": {"sync_dir": "media/sync/immich/"}})

        resp = client.post("/api/browse/delete", json={"path": str(child), "force": True})

        assert resp.status_code == 403
        assert child.is_dir()

    def test_refusal_wins_over_force(self, client, media_root: Path, mock_state):
        """A forced request must still not delete the sync folder."""
        sync_dir = media_root / "media" / "sync" / "immich"
        sync_dir.mkdir(parents=True)
        mock_state.update_config("sync", {"immich": {"sync_dir": "media/sync/immich/"}})

        resp = client.post(
            "/api/browse/delete",
            json={"path": str(sync_dir), "force": True, "dry_run": True},
        )

        assert resp.status_code == 403
        assert sync_dir.is_dir()
