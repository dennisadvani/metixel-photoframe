# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the config REST API endpoints.

Uses the shared fixtures from ``conftest.py``, which build the *real* Flask
app via ``create_app()`` with mocked outbound dependencies (IPC, update
manager), so the config blueprint is exercised through the production wiring.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

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
        assert data["cec_enabled"] is False
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

    def test_get_system_section(self, client, mock_state):
        resp = client.get("/api/config/system")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # The conftest fixture overrides cache_dir to a temp path — the
        # endpoint must return the configured value, not a hard-coded default.
        assert data["cache_dir"] == mock_state.config.system["cache_dir"]
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


# ---------------------------------------------------------------------------
# System commands — restart/reboot/shutdown/time/ntp/quiet-boot
# ---------------------------------------------------------------------------


def _wait_for_call(callable_mock, timeout: float = 3.0) -> None:
    """Busy-wait until a background thread has invoked the mock."""
    deadline = time.time() + timeout
    while time.time() < deadline and callable_mock.call_count == 0:
        time.sleep(0.01)


class TestSystemCommands:
    """System-command endpoints with ``subprocess.run`` mocked."""

    @staticmethod
    def _ok_result() -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    def test_server_time(self, client):
        resp = client.get("/api/time")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert {"iso", "unix", "time", "date", "timezone", "utc_offset"} <= set(data)

    def test_timezones_list(self, client):
        resp = client.get("/api/time/timezones")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Present in both the curated shortlist (non-Linux) and the real
        # /usr/share/zoneinfo/zone.tab on Linux — deterministic everywhere.
        assert data["timezones"]
        assert "America/New_York" in data["timezones"]

    def test_set_timezone(self, client, monkeypatch):
        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post("/api/time/timezone", json={"timezone": "Australia/Sydney"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        fake.assert_called_once()
        cmd = fake.call_args[0][0]
        assert cmd[:4] == ["sudo", "-n", "timedatectl", "set-timezone"]
        assert cmd[-1] == "Australia/Sydney"

    def test_set_timezone_missing(self, client):
        resp = client.post("/api/time/timezone", json={})
        assert resp.status_code == 400

    def test_set_timezone_failure(self, client, monkeypatch):
        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(
            return_value=SimpleNamespace(returncode=1, stderr="boom", stdout="")
        )
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post("/api/time/timezone", json={"timezone": "UTC"})
        assert resp.status_code == 500

    def test_ntp_enable(self, client, monkeypatch):
        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post(
            "/api/time/ntp", json={"enabled": True, "servers": ["0.pool.ntp.org"]}
        )
        assert resp.status_code == 200
        assert json.loads(resp.data)["ntp"] == "enabled"

    def test_ntp_missing_enabled(self, client):
        resp = client.post("/api/time/ntp", json={})
        assert resp.status_code == 400

    def test_quiet_boot_enable(self, client, monkeypatch):
        import metixel.backend.web.routes.system as system_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(system_mod.subprocess, "run", fake)
        resp = client.post("/api/system/quiet-boot", json={"enabled": True})
        assert resp.status_code == 200
        assert json.loads(resp.data)["quiet_boot"] is True

    def test_quiet_boot_missing(self, client):
        resp = client.post("/api/system/quiet-boot", json={})
        assert resp.status_code == 400

    def test_reload(self, client):
        resp = client.post("/api/config/reload")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"status": "ok"}

    def test_restart(self, client, monkeypatch):
        import metixel.backend.web.routes.system as system_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(system_mod.subprocess, "run", fake)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        resp = client.post("/api/system/restart")
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        _wait_for_call(fake)
        assert fake.call_args[0][0][:3] == ["sudo", "-n", "systemctl"]

    def test_reboot(self, client, monkeypatch):
        import metixel.backend.web.routes.system as system_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(system_mod.subprocess, "run", fake)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        resp = client.post("/api/system/reboot")
        assert resp.status_code == 200
        _wait_for_call(fake)
        assert fake.call_args[0][0][:3] == ["sudo", "-n", "reboot"]

    def test_shutdown(self, client, monkeypatch):
        import metixel.backend.web.routes.system as system_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(system_mod.subprocess, "run", fake)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        resp = client.post("/api/system/shutdown")
        assert resp.status_code == 200
        _wait_for_call(fake)
        assert fake.call_args[0][0][:3] == ["sudo", "-n", "shutdown"]
