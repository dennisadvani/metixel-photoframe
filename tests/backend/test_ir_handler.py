# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""IRHandler — LIRC line parsing via a ``FakeIrSocket``.

The real handler reads from the LIRC Unix socket; the fake implements the
``IrSocket`` port so line parsing and dispatch are tested without the device.
"""

from __future__ import annotations

from pathlib import Path


class FakeIPC:
    def __init__(self) -> None:
        self.sent = []

    def send(self, msg) -> None:
        self.sent.append(msg)


class FakeIrSocket:
    """Implements the full ``IrSocket`` port surface."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self.closed = False
        self.connected_to: str | None = None

    def connect(self, path: str) -> None:
        self.connected_to = path

    def settimeout(self, timeout: float) -> None:
        pass

    def recv(self, bufsize: int) -> bytes:
        if self._data:
            data, self._data = self._data, b""
            return data
        # Simulate connection close so the read loop terminates.
        raise OSError("connection closed")

    def close(self) -> None:
        self.closed = True


class TestIRHandler:
    @staticmethod
    def _make(tmp_path: Path, ir: FakeIrSocket):
        from metixel.backend.input_handlers.ir import IRHandler
        from metixel.backend.state import StateManager
        from metixel.shared.config import Config

        config_path = tmp_path / "config.json"
        Config().save(config_path)
        state = StateManager(config_path, tmp_path / "run")
        ipc = FakeIPC()
        return IRHandler(state, ipc, ir=ir), ipc

    def test_process_line_key_play(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeIrSocket())
        handler._process_line("0000000000f40bf0 00 KEY_PLAY lircd.conf")
        assert ipc.sent[-1].cmd == "resume"

    def test_process_line_key_next(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeIrSocket())
        handler._process_line("0000000000f40bf0 00 KEY_NEXT lircd.conf")
        assert ipc.sent[-1].cmd == "next"

    def test_process_line_unknown_button_ignored(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeIrSocket())
        handler._process_line("0000000000f40bf0 00 KEY_UNKNOWN lircd.conf")
        assert ipc.sent == []

    def test_process_line_malformed_ignored(self, tmp_path: Path) -> None:
        handler, ipc = self._make(tmp_path, FakeIrSocket())
        handler._process_line("too-short")
        assert ipc.sent == []

    def test_run_reads_and_dispatches(self, tmp_path: Path) -> None:
        from metixel.shared.ports import IrSocket

        ir = FakeIrSocket(data=b"0000000000f40bf0 00 KEY_NEXT lircd.conf\n")
        handler, ipc = self._make(tmp_path, ir)
        assert isinstance(ir, IrSocket)

        handler.run()

        assert ipc.sent[-1].cmd == "next"
        assert ir.connected_to == "/var/run/lirc/lircd"
        assert ir.closed is True
