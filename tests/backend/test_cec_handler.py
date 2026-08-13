# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""CECHandler — HDMI-CEC key mapping via a ``FakeCecController``.

The real handler drives the TV remote through ``libcec``; the fake implements
the ``CecController`` port so the key-to-command mapping logic is tested
without the hardware.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FakeIPC:
    def __init__(self) -> None:
        self.sent = []

    def send(self, msg) -> None:
        self.sent.append(msg)


class FakeCecController:
    """Implements the full ``CecController`` port surface."""

    def __init__(self, port: str | None = None) -> None:
        self.port = port
        self.initialized = False
        self.closed = False
        self.log_callback: Any = None
        self.key_callback: Any = None

    def set_log_callback(self, fn: Any) -> None:
        self.log_callback = fn

    def set_keypress_callback(self, fn: Any) -> None:
        self.key_callback = fn

    def initialize(self, device_name: str = "Metixel Frame") -> None:
        self.initialized = True

    def detect_and_open(self) -> str | None:
        return self.port

    def close(self) -> None:
        self.closed = True


class TestCECHandler:
    @staticmethod
    def _make(tmp_path: Path, cec: FakeCecController):
        from metixel.backend.input_handlers.cec import CECHandler
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        return CECHandler(state, ipc, cec=cec), ipc

    def test_key_play_maps_to_next(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(0x01, 0)  # Play → next slide
        assert ipc.sent[-1].cmd == "next"

    def test_key_screen_off_maps_to_screen_off(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(0x42, 0)  # Screen Off
        assert ipc.sent[-1].cmd == "screen_off"

    def test_key_unknown_ignored(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(0x99, 0)
        assert ipc.sent == []

    def test_run_wires_callbacks_and_degrades_without_adapter(self, tmp_path: Path) -> None:
        from metixel.shared.ports import CecController

        cec = FakeCecController(port=None)
        handler, _ipc = self._make(tmp_path, cec)
        assert isinstance(cec, CecController)

        handler.run()

        assert cec.initialized is True
        assert cec.key_callback is not None
        assert cec.log_callback is not None
        # No adapter detected → handler exits cleanly without blocking.
        assert handler._running is False

    def test_stop_closes_controller(self, tmp_path: Path) -> None:
        cec = FakeCecController(port="hdmi0")
        handler, _ipc = self._make(tmp_path, cec)
        handler.stop()
        assert cec.closed is True
