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
