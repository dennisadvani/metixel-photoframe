# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""HDMI-CEC input handler.

Listens for CEC commands from the TV remote (play, pause, stop, navigation)
and translates them into Metixel control messages.
"""

from __future__ import annotations

import logging
import time

from metixel.backend.state import StateManager
from metixel.shared.ipc import ControlMessage, IPCClient
from metixel.shared.ports import CecController

logger = logging.getLogger(__name__)


class CECHandler:
    """Handles HDMI-CEC input from TV remotes.

    Requires ``python-cec`` and ``libcec`` to be installed.
    Maps common CEC user control codes to Metixel commands.
    """

    # CEC user control code → Metixel command
    CMD_MAP: dict[int, str] = {
        0x01: "next",  # Play (next slide)
        0x02: "pause",  # Pause
        0x44: "prev",  # Previous
        0x45: "next",  # Forward
        0x46: "prev",  # Backward
        0x41: "screen_on",  # Screen On
        0x42: "screen_off",  # Screen Off
        0x60: "next",  # Play (alternative)
        0x00: "resume",  # Select / OK → resume
    }

    def __init__(
        self,
        state: StateManager,
        ipc: IPCClient,
        cec: CecController | None = None,
    ) -> None:
        self._state = state
        self._ipc = ipc
        self._running = False
        self._cec = cec  # injected CecController port (None → real adapter in run())

    def run(self) -> None:
        """Initialize CEC and process incoming commands."""
        gw = self._cec
        if gw is None:
            try:
                from metixel.shared.adapters import LibCecAdapter

                gw = LibCecAdapter()
                self._cec = gw
            except ImportError:
                logger.warning("python-cec not installed — CEC disabled")
                return

        try:
            gw.set_log_callback(self._cec_log_callback)
            gw.set_keypress_callback(self._cec_key_callback)
            gw.initialize(device_name="Metixel Frame")
        except AttributeError:
            logger.warning(
                "CEC library API mismatch — CEC disabled. "
                "The installed 'cec' package does not match the expected API. "
                "Try: sudo apt install python3-libcec"
            )
            return
        except Exception:
            logger.warning("Failed to initialise CEC — CEC disabled", exc_info=True)
            return

        try:
            com_port = gw.detect_and_open()
            if com_port is None:
                logger.warning("No CEC adapters detected — CEC disabled")
                return
            logger.info("CEC handler started on %s", com_port)
        except Exception:
            logger.warning(
                "Failed to open CEC adapter — CEC disabled. "
                "Is the HDMI cable connected to a CEC-capable TV?"
            )
            return

        self._running = True
        while self._running:
            time.sleep(1)

    def stop(self) -> None:
        self._running = False
        if self._cec is not None:
            self._cec.close()

    def _cec_key_callback(self, keypress, duration) -> None:
        """Called by libcec when a remote key is pressed."""
        if keypress in self.CMD_MAP:
            cmd = self.CMD_MAP[keypress]
            logger.debug("CEC key: %d → %s", keypress, cmd)
            self._ipc.send(ControlMessage(cmd=cmd))

    @staticmethod
    def _cec_log_callback(level, time, message) -> int:
        """Route CEC library logs to Python logging."""
        if "unused" in message.lower():
            return 0
        logger.debug("libcec: %s", message)
        return 0
