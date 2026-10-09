# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the health display endpoints (info + supported modes) and the
current-media thumbnail URL resolution used by ``GET /api/health``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestCurrentMediaThumbnail:
    """``/api/health`` must never publish a thumbnail URL that 404s.

    The frontend writes ``current_media.json`` naming a thumbnail path; the
    dashboard then renders ``<img src=thumbnail_url>``.  A stale path (e.g. the
    cache was cleared but the state file still names the old hash) would 404 and
    surface as a console/network error on the dashboard.
    """

    @pytest.fixture(autouse=True)
    def _run_dir(self, monkeypatch, tmp_path):
        """Point the run-dir resolution at the test's temp directory.

        ``conftest`` builds a real ``StateManager`` with ``run_dir=tmp_path/"run"``,
        but the route reads the frontend's state file through
        ``metixel.shared.paths.run_path``, which honours ``METIXEL_RUN_DIR``.
        """
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))

    def _write_current_media(self, tmp_path, payload: dict) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "current_media.json").write_text(json.dumps(payload), encoding="utf-8")

    def _thumbs_dir(self, tmp_path) -> Path:
        """The cache/thumbnails dir, resolved exactly as the route sees it."""
        from metixel.backend.web.routes.media import _resolve_cache_dir

        fake_state = type(
            "S",
            (),
            {"config": type("C", (), {"system": {"cache_dir": str(tmp_path / "cache")}})()},
        )()
        cache_dir = _resolve_cache_dir(fake_state)
        return cache_dir / "thumbnails"

    def _current_media(self, client) -> dict:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        media = json.loads(resp.data)["current_media"]
        assert isinstance(media, dict)
        return media

    def test_publishes_url_when_thumbnail_exists(self, client, tmp_path):
        thumb_dir = self._thumbs_dir(tmp_path)
        thumb_dir.mkdir(parents=True)
        (thumb_dir / "abc123.jpg").write_bytes(b"\xff\xd8\xff")

        self._write_current_media(
            tmp_path,
            {"file": "a.jpg", "thumbnail_path": str(thumb_dir / "abc123.jpg")},
        )

        assert self._current_media(client)["thumbnail_url"] == ("/api/media/thumbnail/abc123.jpg")

    def test_omits_url_when_thumbnail_missing(self, client, tmp_path):
        # State file names a hash that no longer exists in the cache.
        self._write_current_media(
            tmp_path,
            {"file": "a.jpg", "thumbnail_path": str(self._thumbs_dir(tmp_path) / "gone.jpg")},
        )

        assert self._current_media(client)["thumbnail_url"] is None

    def test_omits_url_when_no_thumbnail_path(self, client, tmp_path):
        self._write_current_media(tmp_path, {"file": "a.jpg", "thumbnail_path": None})

        assert self._current_media(client)["thumbnail_url"] is None

    def test_publishes_url_for_video_frame_cache(self, client, tmp_path):
        """Video frames live in ``<cache>/videos/<hash>.<N>.frame.jpg``
        (processing/frames.py) — the URL must point at what
        ``/api/media/thumbnail`` can actually serve."""
        videos_dir = self._thumbs_dir(tmp_path).parent / "videos"
        videos_dir.mkdir(parents=True)
        frame = videos_dir / "0123456789abcdef.1.frame.jpg"
        frame.write_bytes(b"\xff\xd8\xff")

        self._write_current_media(tmp_path, {"file": "clip.mp4", "thumbnail_path": str(frame)})

        url = self._current_media(client)["thumbnail_url"]
        assert url == "/api/media/thumbnail/0123456789abcdef.1.frame.jpg"
        # And the media route agrees — the URL does not 404.
        assert client.get(url).status_code == 200

    def test_omits_url_for_frame_outside_cache(self, client, tmp_path):
        """A frame file that exists but is NOT under the cache is not served,
        so no URL is published for it (the old code handed out 404s here)."""
        frame = tmp_path / "clip.mp4.1.frame"
        frame.write_bytes(b"\xff\xd8\xff")
        self._write_current_media(tmp_path, {"file": "clip.mp4", "thumbnail_path": str(frame)})
        assert self._current_media(client)["thumbnail_url"] is None


class TestDisplayModes:
    """GET /api/health/display/modes returns mutually-supported modes."""

    def test_returns_modes_list(self, client):
        resp = client.get("/api/health/display/modes")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "modes" in data
        assert isinstance(data["modes"], list)
        assert len(data["modes"]) > 0

    def test_modes_have_width_height_refresh(self, client):
        resp = client.get("/api/health/display/modes")
        data = json.loads(resp.data)
        for m in data["modes"]:
            assert "width" in m
            assert "height" in m
            assert "refresh" in m

    def test_uses_modes_from_display_info(self, client, monkeypatch):
        """Modes written by the frontend to display_info.json are used."""
        import metixel.backend.web.routes.health as health_mod

        real_modes = [
            {"width": 1920, "height": 1200, "refresh": 59.95, "preferred": True, "current": True},
            {"width": 1920, "height": 1080, "refresh": 60.0, "preferred": False, "current": False},
            {"width": 1280, "height": 1024, "refresh": 60.0, "preferred": False, "current": False},
        ]
        monkeypatch.setattr(
            health_mod,
            "_read_display_info",
            lambda: {"modes": real_modes},
        )
        resp = client.get("/api/health/display/modes")
        data = json.loads(resp.data)
        assert data["source"] == "monitor"

        # Deduplicated by resolution, highest refresh kept.
        def _key(m):
            return (m["width"], m["height"], m["refresh"])

        keys = [_key(m) for m in data["modes"]]
        assert (1920, 1200, 60) in keys
        assert (1920, 1080, 60) in keys
        assert (1280, 1024, 60) in keys

    def test_falls_back_to_wlr_randr(self, client, monkeypatch):
        """Without display_info, queries wlr-randr directly."""
        import metixel.backend.web.routes.health as health_mod
        import metixel.display.hardware as hw

        real_modes = [
            {"width": 1920, "height": 1080, "refresh": 60.0, "preferred": True, "current": True},
        ]
        monkeypatch.setattr(health_mod, "_read_display_info", lambda: None)
        monkeypatch.setattr(hw.WlrOutput, "list_modes", lambda self: real_modes)
        resp = client.get("/api/health/display/modes")
        data = json.loads(resp.data)
        assert data["source"] == "monitor"
        assert data["modes"][0]["width"] == 1920

    def test_falls_back_when_no_modes(self, client, monkeypatch):
        """When no modes are available, falls back to a static list."""
        import metixel.backend.web.routes.health as health_mod
        import metixel.display.hardware as hw

        monkeypatch.setattr(health_mod, "_read_display_info", lambda: None)
        monkeypatch.setattr(hw.WlrOutput, "list_modes", lambda self: [])
        resp = client.get("/api/health/display/modes")
        data = json.loads(resp.data)
        assert data["source"] == "fallback"
        assert len(data["modes"]) > 0


class TestDisplayInfo:
    """GET /api/health/display/info returns detected display info."""

    def test_returns_display_info(self, client):
        resp = client.get("/api/health/display/info")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Falls back to config values when the frontend status file is absent.
        assert "width" in data
        assert "height" in data
