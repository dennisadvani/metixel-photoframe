# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media API endpoints — thumbnail serving and media listing."""

from __future__ import annotations

import io
import json

import pytest


class TestServeThumbnail:
    def test_invalid_extension_returns_403(self, client):
        resp = client.get("/api/media/thumbnail/evil.png")
        assert resp.status_code == 403

    def test_served_from_cache(self, client, tmp_path, monkeypatch):
        import metixel.backend.web.routes.media as media_mod

        thumb_dir = tmp_path / "thumbnails"
        thumb_dir.mkdir(parents=True)
        (thumb_dir / "photo.jpg").write_bytes(b"\xff\xd8fakejpeg")
        monkeypatch.setattr(media_mod, "_resolve_cache_dir", lambda state: tmp_path)

        resp = client.get("/api/media/thumbnail/photo.jpg")
        assert resp.status_code == 200
        assert resp.data == b"\xff\xd8fakejpeg"
        assert resp.mimetype == "image/jpeg"

    def test_missing_thumbnail_returns_404(self, client, tmp_path, monkeypatch):
        import metixel.backend.web.routes.media as media_mod

        monkeypatch.setattr(media_mod, "_resolve_cache_dir", lambda state: tmp_path)
        resp = client.get("/api/media/thumbnail/nope.jpg")
        assert resp.status_code == 404

    def test_video_frame_served_from_cache_videos(self, client, tmp_path, monkeypatch):
        """``<cache>/videos/<hash>.<N>.frame.jpg`` frames are served (resized)."""
        import metixel.backend.web.routes.media as media_mod

        pytest.importorskip("PIL")
        from PIL import Image

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True)
        Image.new("RGB", (1280, 720), "red").save(videos_dir / "abc123.2.frame.jpg", "JPEG")
        monkeypatch.setattr(media_mod, "_resolve_cache_dir", lambda state: tmp_path)

        resp = client.get("/api/media/thumbnail/abc123.2.frame.jpg")
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"
        with Image.open(io.BytesIO(resp.data)) as img:
            assert max(img.size) <= 320

    def test_non_frame_name_not_looked_up_in_videos(self, client, tmp_path, monkeypatch):
        """Only the strict ``<hash>.<N>.frame.jpg`` shape is served from
        cache/videos — an arbitrary jpg dropped there is not exposed."""
        import metixel.backend.web.routes.media as media_mod

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True)
        (videos_dir / "random.jpg").write_bytes(b"\xff\xd8fake")
        monkeypatch.setattr(media_mod, "_resolve_cache_dir", lambda state: tmp_path)
        assert client.get("/api/media/thumbnail/random.jpg").status_code == 404

    def test_media_tree_not_searched(self, client, tmp_path, monkeypatch):
        """The old rglob-the-media-tree fallback is gone: a jpg in the watch
        folder is not served via the thumbnail route."""
        import metixel.backend.web.routes.media as media_mod
        import metixel.shared.config as config_mod

        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "photo.jpg").write_bytes(b"\xff\xd8fake")
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [media_dir])
        monkeypatch.setattr(media_mod, "_resolve_cache_dir", lambda state: tmp_path / "cache")
        assert client.get("/api/media/thumbnail/photo.jpg").status_code == 404


class TestListMedia:
    def test_list_empty_watch_path(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [tmp_path])
        resp = client.get("/api/media/list")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 0
        assert data["items"] == []
        assert data["images"] == 0
        assert data["videos"] == 0

    def test_list_image_in_watch_path(self, client, tmp_path, monkeypatch):
        from PIL import Image

        import metixel.shared.config as config_mod

        img_path = tmp_path / "photo.png"
        Image.new("RGB", (2, 3)).save(img_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [tmp_path])

        resp = client.get("/api/media/list")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1
        assert data["images"] == 1
        item = data["items"][0]
        assert item["name"] == "photo.png"
        assert item["width"] == 2
        assert item["height"] == 3
        assert item["media_type"] == "image"


class TestListMediaFilters:
    """Server-side filtering of the media list (name / folder / type)."""

    @staticmethod
    def _make_library(tmp_path):
        """Create a small library: two images + one video across two folders."""
        from PIL import Image

        folder_a = tmp_path / "folder_a"
        folder_b = tmp_path / "folder_b"
        folder_a.mkdir()
        folder_b.mkdir()
        Image.new("RGB", (2, 3)).save(folder_a / "beach.png")
        Image.new("RGB", (2, 3)).save(folder_a / "mountain.png")
        Image.new("RGB", (2, 3)).save(folder_b / "beach.png")
        # A tiny video file (probe will fail gracefully → width/height 0)
        (folder_b / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return [folder_a, folder_b]

    def test_filter_by_name(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?name=mountain")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1
        assert data["items"][0]["name"] == "mountain.png"

    def test_filter_by_name_case_insensitive(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?name=MOUNTAIN")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1
        assert data["items"][0]["name"] == "mountain.png"

    def test_filter_by_type_video(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?type=video")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1
        assert data["videos"] == 1
        assert data["images"] == 0
        assert data["items"][0]["name"] == "clip.mp4"

    def test_filter_by_type_image(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?type=image")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 3
        assert data["images"] == 3
        assert data["videos"] == 0
        assert all(i["media_type"] == "image" for i in data["items"])

    def test_filter_by_folder(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?folder=folder_b")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 2
        names = {i["name"] for i in data["items"]}
        assert names == {"beach.png", "clip.mp4"}

    def test_combined_filters(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        # beach.png exists in both folders; folder_a + name=beach → 1 result
        resp = client.get("/api/media/list?folder=folder_a&name=beach")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1
        assert data["items"][0]["name"] == "beach.png"
        assert data["items"][0]["folder"] == "folder_a"

    def test_filter_no_match_returns_empty(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        resp = client.get("/api/media/list?name=doesnotexist")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 0
        assert data["items"] == []
        assert data["images"] == 0
        assert data["videos"] == 0

    def test_filter_respects_pagination(self, client, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        paths = self._make_library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: paths)

        # 3 images total; limit=2 → first page has 2, has_more True
        resp = client.get("/api/media/list?type=image&limit=2")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["has_more"] is True

        # Second page returns the remaining 1
        resp2 = client.get("/api/media/list?type=image&limit=2&offset=2")
        data2 = json.loads(resp2.data)
        assert len(data2["items"]) == 1
        assert data2["has_more"] is False


class TestListMediaSyncedFlag:
    """Items under the Immich sync folder are flagged ``synced`` so the UI
    can hide the per-item Delete menu for them."""

    def test_synced_flag_reflects_immich_sync_dir(self, client, mock_state, tmp_path, monkeypatch):
        from PIL import Image

        import metixel.shared.config as config_mod

        local = tmp_path / "my_media"
        synced = tmp_path / "immich"
        local.mkdir()
        synced.mkdir()
        Image.new("RGB", (2, 3)).save(local / "mine.png")
        Image.new("RGB", (2, 3)).save(synced / "theirs.png")
        mock_state.update_config("sync", {"immich": {"sync_dir": str(synced)}})
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [local, synced])

        resp = client.get("/api/media/list")
        assert resp.status_code == 200
        by_name = {i["name"]: i for i in json.loads(resp.data)["items"]}
        assert by_name["mine.png"]["synced"] is False
        assert by_name["theirs.png"]["synced"] is True


class TestDeleteLibraryMedia:
    """``POST /api/media/delete`` — delete one library item by folder + path."""

    @staticmethod
    def _library(tmp_path):
        from PIL import Image

        root = tmp_path / "my_media"
        (root / "sub").mkdir(parents=True)
        Image.new("RGB", (2, 3)).save(root / "top.png")
        Image.new("RGB", (2, 3)).save(root / "sub" / "nested.png")
        return root

    def test_deletes_file_by_folder_and_relative_path(
        self, client, mock_state, tmp_path, monkeypatch
    ):
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        resp = client.post(
            "/api/media/delete", json={"folder": "my_media", "path": "sub/nested.png"}
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["deleted"] is True
        assert body["name"] == "nested.png"
        assert not (root / "sub" / "nested.png").exists()
        assert (root / "top.png").exists()

    def test_deleted_file_disappears_from_next_listing(
        self, client, mock_state, tmp_path, monkeypatch
    ):
        """The file-list cache must be invalidated so the UI doesn't keep
        showing a file that is gone."""
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        first = json.loads(client.get("/api/media/list").data)
        assert first["total"] == 2

        client.post("/api/media/delete", json={"folder": "my_media", "path": "top.png"})

        second = json.loads(client.get("/api/media/list").data)
        assert second["total"] == 1
        assert [i["name"] for i in second["items"]] == ["nested.png"]

    def test_removes_matching_playlist_item(self, client, mock_state, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod
        from metixel.shared.models import MediaItem, MediaType

        root = self._library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])
        target = (root / "top.png").resolve()
        mock_state.add_playlist_items(
            [
                MediaItem(
                    id="top",
                    original_path=target,
                    cached_path=target,
                    media_type=MediaType.IMAGE,
                    width=2,
                    height=3,
                )
            ]
        )

        resp = client.post("/api/media/delete", json={"folder": "my_media", "path": "top.png"})
        assert resp.status_code == 200
        assert all(i.id != "top" for i in mock_state.get_playlist())

    def test_falls_back_to_any_watch_path_when_folder_name_is_wrong(
        self, client, mock_state, tmp_path, monkeypatch
    ):
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        resp = client.post("/api/media/delete", json={"folder": "", "path": "top.png"})
        assert resp.status_code == 200
        assert not (root / "top.png").exists()

    def test_requires_path(self, client):
        resp = client.post("/api/media/delete", json={"folder": "my_media"})
        assert resp.status_code == 400

    def test_unknown_file_is_404(self, client, mock_state, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        resp = client.post("/api/media/delete", json={"folder": "my_media", "path": "nope.png"})
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "bad_path",
        ["../outside.png", "sub/../../outside.png", "/etc/passwd"],
    )
    def test_refuses_paths_outside_watch_folder(
        self, client, mock_state, tmp_path, monkeypatch, bad_path
    ):
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"x")
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        resp = client.post("/api/media/delete", json={"folder": "my_media", "path": bad_path})
        assert resp.status_code == 404
        assert outside.exists()

    def test_refuses_symlink_escaping_watch_folder(self, client, mock_state, tmp_path, monkeypatch):
        import metixel.shared.config as config_mod

        root = self._library(tmp_path)
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"x")
        (root / "link.png").symlink_to(outside)
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [root])

        resp = client.post("/api/media/delete", json={"folder": "my_media", "path": "link.png"})
        assert resp.status_code == 404
        assert outside.exists()

    def test_refuses_immich_synced_file(self, client, mock_state, tmp_path, monkeypatch):
        from PIL import Image

        import metixel.shared.config as config_mod

        synced = tmp_path / "immich"
        synced.mkdir()
        Image.new("RGB", (2, 3)).save(synced / "theirs.png")
        mock_state.update_config("sync", {"immich": {"sync_dir": str(synced)}})
        monkeypatch.setattr(config_mod, "resolve_watch_paths", lambda config: [synced])

        resp = client.post("/api/media/delete", json={"folder": "immich", "path": "theirs.png"})
        assert resp.status_code == 403
        assert (synced / "theirs.png").exists()
