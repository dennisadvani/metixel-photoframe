# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web server app-level tests — index, captive portal, slideshow signal."""

from __future__ import annotations

import json
import threading


class TestIndex:
    def test_serves_dashboard(self, app, monkeypatch):
        import metixel.backend.web.server as server_mod

        monkeypatch.setattr(server_mod, "_is_ap_mode", lambda: False)
        resp = app.test_client().get("/")
        assert resp.status_code == 200
        assert b"Metixel Photoframe" in resp.data

    def test_serves_captive_when_ap_mode(self, app, monkeypatch):
        import metixel.backend.web.server as server_mod

        monkeypatch.setattr(server_mod, "_is_ap_mode", lambda: True)
        resp = app.test_client().get("/")
        assert resp.status_code == 200
        assert b"WiFi Setup" in resp.data


class TestCaptivePreview:
    def test_captive_preview_served(self, client):
        resp = client.get("/captive")
        assert resp.status_code == 200
        assert b"WiFi Setup" in resp.data


class TestSlideshowStartedSignal:
    def test_sets_daemon_event(self, app):
        event = threading.Event()

        class FakeDaemon:
            _slideshow_started = event

        app.config["METIXEL_DAEMON"] = FakeDaemon()
        resp = app.test_client().post("/api/slideshow-started")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"status": "ok"}
        assert event.is_set()

    def test_without_daemon_still_ok(self, app):
        app.config["METIXEL_DAEMON"] = None
        resp = app.test_client().post("/api/slideshow-started")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"status": "ok"}
