# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Inter-Process Communication (IPC) for Metixel Photoframe.

Uses a Unix domain socket for real-time control messages between the backend
daemon and the frontend renderer.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default socket path
DEFAULT_SOCKET_PATH = "/run/metixel/control.sock"


@dataclass
class ControlMessage:
    """A JSON-serializable control message."""

    # "next", "prev", "pause", "resume", "toggle_pause",
    # "switch_album", "screen_off", "screen_on"
    cmd: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"cmd": self.cmd, "args": self.args})

    @classmethod
    def from_json(cls, data: str) -> ControlMessage:
        obj = json.loads(data)
        return cls(cmd=obj["cmd"], args=obj.get("args", {}))


class IPCServer:
    """Unix domain socket server for receiving control commands.

    The frontend renderer runs this to receive real-time commands from the
    backend daemon.
    """

    BUFFER_SIZE = 4096

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
        self._socket_path = socket_path
        self._socket: socket.socket | None = None

    def start(self) -> None:
        """Create and bind the Unix domain socket.

        On Windows (no AF_UNIX), silently becomes a no-op.
        """
        if not hasattr(socket, "AF_UNIX"):
            logger.debug("AF_UNIX not available (Windows) — IPC disabled")
            return

        # Ensure the directory exists
        sock_dir = os.path.dirname(self._socket_path)
        os.makedirs(sock_dir, exist_ok=True)

        # Remove stale socket file
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._socket.bind(self._socket_path)
        self._socket.setblocking(False)
        logger.info("IPC server listening on %s", self._socket_path)

    def poll(self) -> ControlMessage | None:
        """Non-blocking check for incoming messages. Returns None if empty."""
        if self._socket is None:
            return None
        try:
            data = self._socket.recv(self.BUFFER_SIZE)
            if data:
                return ControlMessage.from_json(data.decode("utf-8"))
        except BlockingIOError:
            pass
        except Exception:
            logger.exception("Error reading from IPC socket")
        return None

    def stop(self) -> None:
        """Close the socket and clean up."""
        if self._socket:
            self._socket.close()
            self._socket = None
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        logger.info("IPC server stopped")


class IPCClient:
    """Unix domain socket client for sending control commands.

    The backend daemon uses this to send commands to the frontend renderer.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH) -> None:
        self._socket_path = socket_path
        self._socket: socket.SocketType | None = None
        # On Windows (no AF_UNIX), IPC is disabled — mirror the server's
        # behaviour so the backend daemon can still start on a desktop.
        if not hasattr(socket, "AF_UNIX"):
            logger.debug("AF_UNIX not available (Windows) — IPC disabled")
            return
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def send(self, message: ControlMessage) -> bool:
        """Send a control message to the frontend. Returns True on success."""
        if self._socket is None:
            logger.debug("IPC disabled — dropping %s", message.cmd)
            return False
        try:
            data = message.to_json().encode("utf-8")
            self._socket.sendto(data, self._socket_path)
            logger.debug("IPC sent: %s", message.cmd)
            return True
        except Exception:
            logger.exception("Failed to send IPC message: %s", message.cmd)
            return False

    def close(self) -> None:
        if self._socket:
            self._socket.close()
            self._socket = None
