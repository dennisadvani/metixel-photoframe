# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clock / timezone / NTP endpoints — system-zone detection and forced sync.

The basic ``/api/time`` contract tests live in ``test_routes_config.py``;
these cover the behaviour added so the dashboard shows the *real* system
timezone and can force an NTP sync on demand.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import metixel.backend.web.routes.time as time_mod


def _ok() -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stderr="", stdout="")


class TestSystemTimezoneName:
    def test_reads_localtime_symlink(self, monkeypatch):
        monkeypatch.setattr(
            time_mod.os, "readlink", lambda p: "/usr/share/zoneinfo/Australia/Sydney"
        )
        assert time_mod.system_timezone_name() == "Australia/Sydney"

    def test_relative_symlink_target(self, monkeypatch):
        monkeypatch.setattr(
            time_mod.os, "readlink", lambda p: "../usr/share/zoneinfo/Europe/London"
        )
        assert time_mod.system_timezone_name() == "Europe/London"

    def test_falls_back_to_etc_timezone(self, monkeypatch, tmp_path):
        def _no_link(p):
            raise OSError("not a symlink")

        monkeypatch.setattr(time_mod.os, "readlink", _no_link)
        tzfile = tmp_path / "timezone"
        tzfile.write_text("America/Chicago\n", encoding="utf-8")
        real_open = open

        def _open(path, *a, **kw):
            if path == "/etc/timezone":
                return real_open(tzfile, *a, **kw)
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", _open)
        assert time_mod.system_timezone_name() == "America/Chicago"

    def test_unknown_when_nothing_available(self, monkeypatch):
        def _fail(p, *a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(time_mod.os, "readlink", _fail)
        monkeypatch.setattr("builtins.open", _fail)
        assert time_mod.system_timezone_name() == ""


class TestServerTimeUsesSystemZone:
    def test_payload_reflects_system_zone_not_process_zone(self, client, monkeypatch):
        """Even if the backend process started in UTC, /api/time must report
        the zone the system is now set to."""
        monkeypatch.setattr(time_mod, "system_timezone_name", lambda: "Asia/Tokyo")
        resp = client.get("/api/time")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["timezone_name"] == "Asia/Tokyo"
        assert data["utc_offset"] == "+0900"
        assert data["timezone"] == "JST"

    def test_unknown_zone_falls_back_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(time_mod, "system_timezone_name", lambda: "Not/AZone")
        resp = client.get("/api/time")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["time"]
        assert data["timezone_name"] == "Not/AZone"


class TestTimezoneList:
    def test_sorted_alphabetically(self, client):
        resp = client.get("/api/time/timezones")
        zones = json.loads(resp.data)["timezones"]
        assert zones == sorted(zones)
        assert len(zones) == len(set(zones))


class TestSetTimezoneAppliesToProcess:
    def test_process_zone_updated_after_set(self, client, monkeypatch):
        monkeypatch.setattr(time_mod.subprocess, "run", mock.MagicMock(return_value=_ok()))
        applied = mock.MagicMock()
        monkeypatch.setattr(time_mod, "_apply_process_timezone", applied)
        resp = client.post("/api/time/timezone", json={"timezone": "Europe/Paris"})
        assert resp.status_code == 200
        applied.assert_called_once_with("Europe/Paris")

    def test_process_zone_not_touched_on_failure(self, client, monkeypatch):
        monkeypatch.setattr(
            time_mod.subprocess,
            "run",
            mock.MagicMock(return_value=SimpleNamespace(returncode=1, stderr="bad", stdout="")),
        )
        applied = mock.MagicMock()
        monkeypatch.setattr(time_mod, "_apply_process_timezone", applied)
        resp = client.post("/api/time/timezone", json={"timezone": "Europe/Paris"})
        assert resp.status_code == 500
        applied.assert_not_called()


class TestSyncTimeNow:
    def test_restarts_timesyncd_and_reports_synchronized(self, client, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["sudo", "-n"]:
                return _ok()
            return SimpleNamespace(returncode=0, stderr="", stdout="yes\n")

        monkeypatch.setattr(time_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(time_mod.time, "sleep", lambda s: None)

        resp = client.post("/api/time/sync", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["synchronized"] is True
        # Fresh time fields ride along so the UI can repaint the clock.
        assert {"time", "date", "timezone_name", "utc_offset"} <= set(data)
        assert calls[0] == ["sudo", "-n", "systemctl", "restart", "systemd-timesyncd"]
        assert calls[1][:3] == ["timedatectl", "show", "-p"]

    def test_reports_unsynchronized_after_wait(self, client, monkeypatch):
        def fake_run(cmd, **kw):
            if cmd[:2] == ["sudo", "-n"]:
                return _ok()
            return SimpleNamespace(returncode=0, stderr="", stdout="no\n")

        monkeypatch.setattr(time_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(time_mod.time, "sleep", lambda s: None)
        # Make the wait window collapse so the test doesn't spin for 8 s.
        monkeypatch.setattr(time_mod, "_SYNC_WAIT_SECONDS", 0.0)

        resp = client.post("/api/time/sync", json={})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["synchronized"] is False

    def test_restart_failure_is_500(self, client, monkeypatch):
        monkeypatch.setattr(
            time_mod.subprocess,
            "run",
            mock.MagicMock(return_value=SimpleNamespace(returncode=1, stderr="denied", stdout="")),
        )
        resp = client.post("/api/time/sync", json={})
        assert resp.status_code == 500
        assert json.loads(resp.data)["status"] == "error"

    def test_missing_tools_is_500(self, client, monkeypatch):
        def _missing(cmd, **kw):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(time_mod.subprocess, "run", _missing)
        resp = client.post("/api/time/sync", json={})
        assert resp.status_code == 500
