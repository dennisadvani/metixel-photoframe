# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""BackendDaemon display-schedule parsing must never crash startup.

``__init__`` evaluates the schedule to seed the display-power flag.  A saved
empty/malformed ``schedule_on_time`` (the UI can post ``""``) used to raise
``ValueError``/``IndexError`` from ``int(parts[1])`` and crash-loop the
service on every boot.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest


class FakeIPC:
    """IPCClient stand-in (avoids the Pi-only Unix socket)."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        pass


def _write_config(tmp_path: Path, display: dict) -> Path:
    from metixel.shared.config import Config

    data = Config().to_dict()
    data["display"].update(display)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def make_daemon(tmp_path: Path, monkeypatch):
    import metixel.backend.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))

    def _make(display: dict):
        return daemon_mod.BackendDaemon(_write_config(tmp_path, display))

    return _make


class TestParseTime:
    def test_valid(self):
        from metixel.backend.daemon import BackendDaemon

        assert BackendDaemon._parse_time("07:30", "22:00") == 450
        assert BackendDaemon._parse_time("7:30", "22:00") == 450

    @pytest.mark.parametrize("bad", ["", "7", "25:00", "07:60", None, 5, "x:y"])
    def test_invalid_falls_back_to_default_and_warns(self, bad, caplog):
        from metixel.backend.daemon import BackendDaemon

        with caplog.at_level(logging.WARNING, logger="metixel.backend.daemon"):
            assert BackendDaemon._parse_time(bad, "22:00") == 22 * 60
        assert "Invalid display schedule time" in caplog.text


class TestStartupWithBadSchedule:
    def test_empty_schedule_time_does_not_crash_init(self, make_daemon):
        # Bypasses Config.update() sanitising by writing the file directly —
        # exactly what an older release may have left on disk.
        daemon = make_daemon(
            {"schedule_enabled": True, "schedule_on_time": "", "schedule_off_time": ""}
        )
        assert isinstance(daemon._display_on, bool)

    def test_malformed_schedule_uses_defaults(self, make_daemon, monkeypatch):
        import time

        # 12:00 local → inside the default 07:00–22:00 window.
        fake_now = time.struct_time((2026, 1, 1, 12, 0, 0, 3, 1, -1))
        monkeypatch.setattr("metixel.backend.daemon.time.localtime", lambda *a: fake_now)
        daemon = make_daemon(
            {"schedule_enabled": True, "schedule_on_time": "junk", "schedule_off_time": "junk"}
        )
        assert daemon._display_on is True

    def test_exception_in_schedule_evaluation_is_contained(self, make_daemon, monkeypatch):
        import metixel.backend.daemon as daemon_mod

        def boom(self):
            raise RuntimeError("config exploded")

        monkeypatch.setattr(daemon_mod.BackendDaemon, "_display_should_be_on", boom)
        daemon = make_daemon({"schedule_enabled": True})
        assert daemon._display_on is True

    def test_schedule_disabled_is_on(self, make_daemon):
        daemon = make_daemon({"schedule_enabled": False})
        assert daemon._display_on is True
