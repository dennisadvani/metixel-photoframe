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

import pytest

from metixel.shared.ipc import ControlMessage


class FakeIPC:
    """Satisfies the ``IPCSender`` port (see metixel.shared.ipc)."""

    def __init__(self) -> None:
        self.sent: list[ControlMessage] = []

    def send(self, msg: ControlMessage) -> bool:
        self.sent.append(msg)
        return True

    def close(self) -> None:
        pass


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

    # -- Key map (HDMI-CEC UI command table) -------------------------------

    @pytest.mark.parametrize("code", [0x04, 0x4B, 0x49])  # Right, Forward, Fast Forward
    def test_forward_keys_map_to_next(self, tmp_path: Path, code: int) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(code, 0)
        assert [m.cmd for m in ipc.sent] == ["next"]

    @pytest.mark.parametrize("code", [0x03, 0x4C, 0x48])  # Left, Backward, Rewind
    def test_backward_keys_map_to_prev(self, tmp_path: Path, code: int) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(code, 0)
        assert [m.cmd for m in ipc.sent] == ["prev"]

    @pytest.mark.parametrize("code", [0x00, 0x44, 0x46, 0x60, 0x61])
    def test_play_pause_select_keys_toggle_pause(self, tmp_path: Path, code: int) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(code, 0)
        assert [m.cmd for m in ipc.sent] == ["toggle_pause"]

    def test_power_off_and_on_without_daemon_go_over_ipc(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(0x6C, 0)  # Power Off
        handler._cec_key_callback(0x6D, 0)  # Power On
        assert [m.cmd for m in ipc.sent] == ["screen_off", "screen_on"]

    def test_power_keys_route_through_display_power(self, tmp_path: Path) -> None:
        from metixel.backend.input_handlers.cec import CECHandler
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        calls: list[bool] = []
        display_on = {"on": True}

        def display_power(on: bool) -> None:
            calls.append(on)
            display_on["on"] = on

        handler = CECHandler(
            state,
            ipc,
            cec=FakeCecController(),
            display_power=display_power,
            display_is_on=lambda: display_on["on"],
        )
        handler._cec_key_callback(0x6C, 0)  # Power Off
        handler._cec_key_callback(0x6D, 0)  # Power On
        handler._cec_key_callback(0x6B, 0)  # Power Toggle (on → off)
        handler._cec_key_callback(0x6B, 0)  # Power Toggle (off → on)
        assert calls == [False, True, False, True]
        assert ipc.sent == []  # never over IPC when the daemon choke-point is wired

    def test_power_toggle_ignored_without_display_state(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(0x6B, 0)
        assert ipc.sent == []

    @pytest.mark.parametrize("code", [0x41, 0x42, 0x43, 0x01, 0x02, 0x0D, 0x40, 0x45, 0x99])
    def test_volume_and_other_keys_ignored(self, tmp_path: Path, code: int) -> None:
        handler, ipc = self._make(tmp_path, FakeCecController())
        handler._cec_key_callback(code, 0)
        assert ipc.sent == []

    def test_map_values_are_known_commands(self) -> None:
        from metixel.backend.input_handlers.cec import CECHandler
        from metixel.backend.input_handlers.keyboard import VALID_COMMANDS

        allowed = VALID_COMMANDS | {CECHandler.SCREEN_TOGGLE}
        assert set(CECHandler.CMD_MAP.values()) <= allowed

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
