# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for IPC (Inter-Process Communication) control messages."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from metixel.shared.ipc import DEFAULT_SOCKET_PATH, ControlMessage, IPCServer


# ---------------------------------------------------------------------------
# ControlMessage serialization
# ---------------------------------------------------------------------------

class TestControlMessage:
    """Tests for ControlMessage JSON serialization."""

    def test_to_json_basic(self) -> None:
        msg = ControlMessage(cmd="next")
        data = json.loads(msg.to_json())
        assert data["cmd"] == "next"
        assert data["args"] == {}

    def test_to_json_with_args(self) -> None:
        msg = ControlMessage(cmd="switch_album", args={"album_id": "abc123"})
        data = json.loads(msg.to_json())
        assert data["cmd"] == "switch_album"
        assert data["args"] == {"album_id": "abc123"}

    def test_from_json_basic(self) -> None:
        msg = ControlMessage.from_json('{"cmd": "pause"}')
        assert msg.cmd == "pause"
        assert msg.args == {}

    def test_from_json_with_args(self) -> None:
        msg = ControlMessage.from_json(
            '{"cmd": "switch_album", "args": {"album_id": "xyz"}}'
        )
        assert msg.cmd == "switch_album"
        assert msg.args == {"album_id": "xyz"}

    def test_roundtrip(self) -> None:
        original = ControlMessage(cmd="toggle_pause", args={"reason": "ir"})
        restored = ControlMessage.from_json(original.to_json())
        assert restored.cmd == original.cmd
        assert restored.args == original.args

    @pytest.mark.parametrize("cmd", [
        "next", "prev", "pause", "resume", "toggle_pause",
        "switch_album", "screen_off", "screen_on",
    ])
    def test_all_known_commands_roundtrip(self, cmd: str) -> None:
        msg = ControlMessage(cmd=cmd, args={"key": "val"})
        restored = ControlMessage.from_json(msg.to_json())
        assert restored.cmd == cmd
        assert restored.args == {"key": "val"}


# ---------------------------------------------------------------------------
# IPCServer (Windows-safe — AF_UNIX not available)
# ---------------------------------------------------------------------------

class TestIPCServer:
    """Tests for IPCServer behaviour on all platforms."""

    def test_default_socket_path(self) -> None:
        server = IPCServer()
        assert server._socket_path == DEFAULT_SOCKET_PATH
        assert "/run/metixel/control.sock" in DEFAULT_SOCKET_PATH

    def test_custom_socket_path(self) -> None:
        server = IPCServer(socket_path="/tmp/test.sock")
        assert server._socket_path == "/tmp/test.sock"

    def test_start_noop_on_windows(self) -> None:
        """start() should silently succeed even without AF_UNIX."""
        server = IPCServer(socket_path="/nonexistent/path.sock")
        # Should not raise — on Windows it's a no-op, on Linux it may
        # fail if the directory doesn't exist, but we just want no crash.
        try:
            server.start()
        except OSError:
            # Expected on Linux if /nonexistent doesn't exist
            pass
        finally:
            server.stop()

    def test_poll_returns_none_when_not_started(self) -> None:
        server = IPCServer()
        assert server.poll() is None

    def test_poll_returns_none_when_stopped(self) -> None:
        server = IPCServer()
        server.stop()
        assert server.poll() is None

    def test_stop_idempotent(self) -> None:
        """Stopping twice should not crash."""
        server = IPCServer()
        server.stop()
        server.stop()  # Should not raise

    def test_start_stop_lifecycle(self) -> None:
        """Full lifecycle on a temp socket (Unix only)."""
        import socket as sock_mod
        if not hasattr(sock_mod, "AF_UNIX"):
            pytest.skip("AF_UNIX not available on this platform")

        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"
            server = IPCServer(socket_path=str(sock_path))
            server.start()
            assert sock_path.exists()
            server.stop()
            assert not sock_path.exists()
