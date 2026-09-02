# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the health display endpoints (info + supported modes)."""

from __future__ import annotations

import json


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
