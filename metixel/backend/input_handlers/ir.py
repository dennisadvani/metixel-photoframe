# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""LIRC-based infrared remote control handler.

Reads IR events via the LIRC socket and maps them to Metixel control
commands.
"""

from __future__ import annotations

import logging

from metixel.backend.state import StateManager
from metixel.shared.ipc import ControlMessage, IPCClient

logger = logging.getLogger(__name__)


class IRHandler:
    """Handles infrared remote input via LIRC.

    Reads from the LIRC socket (typically ``/var/run/lirc/lircd``) and
    maps recognized button names to Metixel commands.
    """

    # LIRC button name → Metixel command (configurable via config.json)
    DEFAULT_BUTTON_MAP: dict[str, str] = {
        "KEY_PLAY": "resume",
        "KEY_PAUSE": "pause",
        "KEY_NEXT": "next",
        "KEY_PREVIOUS": "prev",
        "KEY_POWER": "power_on",  # Toggle handled at app level
        "KEY_OK": "resume",
        "KEY_UP": "next",
        "KEY_DOWN": "prev",
        "KEY_RIGHT": "next",
        "KEY_LEFT": "prev",
    }

    def __init__(self, state: StateManager, ipc: IPCClient) -> None:
        self._state = state
        self._ipc = ipc
        self._running = False
        self._device: str = state.config.input.get("ir_device", "/dev/lirc0")

    def run(self) -> None:
        """Open LIRC socket and process incoming IR commands."""
        import socket

        lirc_socket = "/var/run/lirc/lircd"
        self._running = True
        logger.info("IR handler starting (socket: %s)", lirc_socket)

        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(lirc_socket)
            sock.settimeout(1.0)
            logger.info("IR handler connected to LIRC socket")
        except (FileNotFoundError, ConnectionRefusedError):
            logger.warning("LIRC socket not found at %s — IR disabled", lirc_socket)
            self._running = False
            return
        except Exception:
            logger.exception("Failed to connect to LIRC")
            self._running = False
            return

        buffer = ""
        while self._running:
            try:
                data = sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    self._process_line(line.strip())
            except socket.timeout:
                continue
            except Exception:
                logger.exception("IR handler error")
                break

        if sock:
            sock.close()
        logger.info("IR handler stopped")

    def stop(self) -> None:
        self._running = False

    def _process_line(self, line: str) -> None:
        """Parse a LIRC output line and dispatch the command."""
        # LIRC output format: "0000000000f40bf0 00 KEY_PLAY lircd.conf"
        parts = line.split()
        if len(parts) < 3:
            return
        button = parts[2]
        cmd = self.DEFAULT_BUTTON_MAP.get(button)
        if cmd:
            logger.debug("IR button: %s → %s", button, cmd)
            self._ipc.send(ControlMessage(cmd=cmd))
