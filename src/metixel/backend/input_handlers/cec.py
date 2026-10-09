# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""HDMI-CEC input handler.

Listens for CEC commands from the TV remote (play, pause, stop, navigation)
and translates them into Metixel control messages.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from metixel.backend.state import StateManager
from metixel.shared.ipc import ControlMessage, IPCSender
from metixel.shared.ports import CecController

logger = logging.getLogger(__name__)


class CECHandler:
    """Handles HDMI-CEC input from TV remotes.

    Requires ``python-cec`` and ``libcec`` to be installed.
    Maps common CEC user control codes to Metixel commands.
    """

    #: Pseudo-command for the CEC "Power Toggle" key — resolved against the
    #: current display state in :meth:`_cec_key_callback` (never sent over IPC).
    SCREEN_TOGGLE = "screen_toggle"

    # CEC user control code → Metixel command, per the HDMI-CEC "UI command"
    # table (CEC 1.4 §13.13 / CEC-OSD): 0x00 Select, 0x01 Up, 0x02 Down,
    # 0x03 Left, 0x04 Right, 0x0D Exit, 0x40 Power, 0x41 Volume Up, 0x42
    # Volume Down, 0x43 Mute, 0x44 Play, 0x45 Stop, 0x46 Pause, 0x48 Rewind,
    # 0x49 Fast Forward, 0x4B Forward, 0x4C Backward, 0x60 Play Function,
    # 0x61 Pause-Play Function, 0x6B Power Toggle, 0x6C Power Off, 0x6D
    # Power On.  Volume/Mute keys are deliberately unmapped (the TV owns
    # audio); Up/Down/Exit/Stop have no slideshow meaning.
    CMD_MAP: dict[int, str] = {
        # Navigation → previous / next slide
        0x04: "next",  # Right
        0x4B: "next",  # Forward
        0x49: "next",  # Fast Forward
        0x03: "prev",  # Left
        0x4C: "prev",  # Backward
        0x48: "prev",  # Rewind
        # Play / pause → toggle the slideshow
        0x00: "toggle_pause",  # Select / OK
        0x44: "toggle_pause",  # Play
        0x46: "toggle_pause",  # Pause
        0x60: "toggle_pause",  # Play Function
        0x61: "toggle_pause",  # Pause-Play Function
        # Power → screen on / off / toggle
        0x6D: "screen_on",  # Power On
        0x6C: "screen_off",  # Power Off
        0x6B: SCREEN_TOGGLE,  # Power Toggle
    }

    def __init__(
        self,
        state: StateManager,
        ipc: IPCSender,
        cec: CecController | None = None,
        display_power: Callable[[bool], None] | None = None,
        display_is_on: Callable[[], bool] | None = None,
    ) -> None:
        self._state = state
        self._ipc = ipc
        self._display_power = display_power
        # Reads the daemon's current display-power flag so "Power Toggle"
        # can flip it; without it the toggle key is ignored.
        self._display_is_on = display_is_on
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
                "Ensure the PyPI 'cec' package (python-cec) is installed: "
                "pip3 install 'cec>=0.2.8'"
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
        if keypress not in self.CMD_MAP:
            logger.debug("CEC key: 0x%02X — unmapped, ignored", keypress)
            return
        cmd = self.CMD_MAP[keypress]
        logger.debug("CEC key: 0x%02X → %s", keypress, cmd)
        if cmd == self.SCREEN_TOGGLE:
            # Needs the current state; resolved to a concrete on/off below.
            if self._display_power is None or self._display_is_on is None:
                logger.debug("CEC Power Toggle ignored — display state not available")
                return
            self._display_power(not self._display_is_on())
            return
        # Screen power goes through the daemon choke-point so the flag
        # and MQTT state stay in sync with every other source.
        if cmd in ("screen_on", "screen_off") and self._display_power is not None:
            self._display_power(cmd == "screen_on")
        else:
            self._ipc.send(ControlMessage(cmd=cmd))

    @staticmethod
    def _cec_log_callback(level, time, message) -> int:
        """Route CEC library logs to Python logging."""
        if "unused" in message.lower():
            return 0
        logger.debug("libcec: %s", message)
        return 0
