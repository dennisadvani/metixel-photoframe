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
# PUT /api/config/display — refresh rate + rotation
# ---------------------------------------------------------------------------


class TestPutDisplayConfig:
    """PUT /api/config/display persists refresh_rate and rotation."""

    def test_put_display_persists_new_keys(self, client, mock_state):
        resp = client.put(
            "/api/config/display",
            json={
                "width": 1920,
                "height": 1080,
                "fps_limit": 30,
                "refresh_rate": 60,
                "rotation": 90,
            },
        )
        assert resp.status_code == 200
        d = mock_state.config.display
        assert d["refresh_rate"] == 60
        assert d["rotation"] == 90

    def test_put_display_defaults_when_omitted(self, client, mock_state):
        resp = client.put(
            "/api/config/display",
            json={"fps_limit": 30},
        )
        assert resp.status_code == 200
        d = mock_state.config.display
        # Keys not sent keep their defaults
        assert d["refresh_rate"] == 0
        assert d["rotation"] == 0

    def test_get_display_includes_new_keys(self, client):
        resp = client.get("/api/config/display")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["refresh_rate"] == 0
        assert data["rotation"] == 0

    def test_display_mode_change_schedules_frontend_restart(self, client, monkeypatch):
        """Changing rotation/refresh schedules a metixel-cage restart."""
        import metixel.backend.web.routes.config as config_mod

        scheduled: list[list[str]] = []

        def fake_schedule_sudo(cmd, **kwargs):
            scheduled.append(cmd)

        monkeypatch.setattr(config_mod, "schedule_sudo", fake_schedule_sudo)
        resp = client.put(
            "/api/config/display",
            json={"rotation": 180, "refresh_rate": 60},
        )
        assert resp.status_code == 200
        assert scheduled == [["systemctl", "restart", "metixel-cage"]]

    def test_display_non_mode_change_no_restart(self, client, monkeypatch):
        """Changing only fps_limit/schedule does NOT restart the frontend."""
        import metixel.backend.web.routes.config as config_mod

        scheduled: list[list[str]] = []

        def fake_schedule_sudo(cmd, **kwargs):
            scheduled.append(cmd)

        monkeypatch.setattr(config_mod, "schedule_sudo", fake_schedule_sudo)
        resp = client.put(
            "/api/config/display",
            json={"fps_limit": 24},
        )
        assert resp.status_code == 200
        assert scheduled == []


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

        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=1, stderr="boom", stdout=""))
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post("/api/time/timezone", json={"timezone": "UTC"})
        assert resp.status_code == 500

    def test_ntp_enable(self, client, monkeypatch):
        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post("/api/time/ntp", json={"enabled": True, "servers": ["0.pool.ntp.org"]})
        assert resp.status_code == 200
        assert json.loads(resp.data)["ntp"] == "enabled"

    def test_ntp_enable_defaults_when_empty(self, client, monkeypatch):
        """Empty/blank server list falls back to the Debian pool defaults."""
        import tempfile

        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(time_mod.subprocess, "run", fake)

        # Capture the timesyncd.conf written to the temp file.
        written_conf = {}

        class _FakeTempFile:
            def __init__(self, mode="w", suffix=".conf", delete=False):
                self.name = "fake_timesyncd.conf"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, s):
                written_conf["conf"] = s

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", _FakeTempFile)
        monkeypatch.setattr(time_mod.os, "unlink", mock.MagicMock())

        resp = client.post("/api/time/ntp", json={"enabled": True, "servers": []})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ntp"] == "enabled"
        assert data["servers"] == time_mod.DEFAULT_NTP_SERVERS

        # The timesyncd.conf written must contain the default NTP= lines.
        conf = written_conf.get("conf", "")
        assert "NTP=0.debian.pool.ntp.org" in conf
        assert "NTP=1.debian.pool.ntp.org" in conf
        assert "NTP=2.debian.pool.ntp.org" in conf

    def test_ntp_enable_defaults_when_blank(self, client, monkeypatch):
        """A list of blank strings also falls back to the defaults."""
        import metixel.backend.web.routes.time as time_mod

        fake = mock.MagicMock(return_value=self._ok_result())
        monkeypatch.setattr(time_mod.subprocess, "run", fake)
        resp = client.post("/api/time/ntp", json={"enabled": True, "servers": ["", "  ", ""]})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["servers"] == time_mod.DEFAULT_NTP_SERVERS

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
