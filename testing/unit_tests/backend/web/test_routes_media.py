# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media API endpoints — thumbnail serving and media listing."""

from __future__ import annotations

import json


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
