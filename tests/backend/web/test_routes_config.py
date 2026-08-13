# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the config REST API endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metixel.shared.config import Config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_state(tmp_path: Path):
    """Create a StateManager with a temp config path."""
    from metixel.backend.state import StateManager

    config_path = tmp_path / "config.json"
    return StateManager(config_path, run_dir=tmp_path / "run")


@pytest.fixture
def mock_ipc():
    """Create a mock IPC client."""
    return mock.MagicMock()


@pytest.fixture
def app(mock_state, mock_ipc):
    """Create a Flask test app with the config blueprint only."""
    from flask import Flask

    test_app = Flask(__name__)
    test_app.config["METIXEL_STATE"] = mock_state
    test_app.config["METIXEL_IPC"] = mock_ipc
    test_app.config["METIXEL_OPT_QUEUE"] = None
    test_app.config["METIXEL_UPDATE_MGR"] = None
    test_app.config["METIXEL_DAEMON"] = None

    from metixel.backend.web.routes.config import config_bp
    test_app.register_blueprint(config_bp, url_prefix="/api/config")

    return test_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /api/config — full config
# ---------------------------------------------------------------------------

class TestGetFullConfig:
    """Tests for GET /api/config."""

    def test_returns_200_and_full_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "display" in data
        assert "slideshow" in data
        assert "video" in data
        assert "sync" in data
        assert "web" in data
        assert "timeouts" in data

    def test_contains_slideshow_defaults(self, client):
        resp = client.get("/api/config")
        data = json.loads(resp.data)
        assert data["slideshow"]["image_duration_seconds"] == 15
        assert data["slideshow"]["shuffle"] is True
        assert data["slideshow"]["transition_style"] == "crossfade"


# ---------------------------------------------------------------------------
# GET /api/config/<section> — section access
# ---------------------------------------------------------------------------

class TestGetConfigSection:
    """Tests for GET /api/config/<section>."""

    def test_get_display_section(self, client):
        resp = client.get("/api/config/display")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["width"] == 0
        assert data["height"] == 0
        assert data["fullscreen"] is True

    def test_get_slideshow_section(self, client):
        resp = client.get("/api/config/slideshow")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["image_duration_seconds"] == 15

    def test_get_video_section(self, client):
        resp = client.get("/api/config/video")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["playback_enabled"] is True
        assert data["transcoding_enabled"] is True

    def test_get_sync_section(self, client):
        resp = client.get("/api/config/sync")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "immich" in data
        assert "local" in data

    def test_get_web_section(self, client):
        resp = client.get("/api/config/web")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["host"] == "0.0.0.0"
        assert data["port"] == 8080

    def test_get_mqtt_section(self, client):
        resp = client.get("/api/config/mqtt")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is False
        assert data["broker"] == "localhost"

    def test_get_input_section(self, client):
        resp = client.get("/api/config/input")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["cec_enabled"] is True
        assert data["keyboard_enabled"] is True

    def test_get_messages_section(self, client):
        resp = client.get("/api/config/messages")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is True
        assert data["default_duration"] == 5.0

    def test_get_network_section(self, client):
        resp = client.get("/api/config/network")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ap_fallback_enabled"] is True

    def test_get_system_section(self, client):
        resp = client.get("/api/config/system")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["cache_dir"] == "cache/"
        assert data["log_level"] == "NONE"
        assert data["first_run"] is True

    def test_get_update_section(self, client):
        resp = client.get("/api/config/update")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["channel"] == "stable"
        assert data["auto_check"] is True

    def test_get_timeouts_section(self, client):
        resp = client.get("/api/config/timeouts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["vlc_start"] == 30
        assert data["transcode"] == 7200
        assert data["ffprobe_probe"] == 120

    def test_unknown_section_returns_404(self, client):
        resp = client.get("/api/config/nonexistent")
        assert resp.status_code == 404
        data = json.loads(resp.data)
        assert "error" in data


# ---------------------------------------------------------------------------
# GET /api/config/video/profiles
# ---------------------------------------------------------------------------

class TestVideoProfiles:
    """Tests for GET /api/config/video/profiles."""

    def test_returns_profiles_list(self, client):
        resp = client.get("/api/config/video/profiles")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "profiles" in data
        assert "current" in data
        assert "detected_model" in data
        assert isinstance(data["profiles"], list)
        assert len(data["profiles"]) > 0

    def test_contains_custom_option(self, client):
        resp = client.get("/api/config/video/profiles")
        data = json.loads(resp.data)
        keys = [p["key"] for p in data["profiles"]]
        assert "custom" in keys

    def test_current_is_empty_by_default(self, client):
        resp = client.get("/api/config/video/profiles")
        data = json.loads(resp.data)
        assert data["current"] == ""
