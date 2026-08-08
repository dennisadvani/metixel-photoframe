# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""USB keyboard / wireless remote input handler.

Listens for key events from keyboard-emulating devices (wireless remotes,
mini keyboards, etc.) and translates them into Metixel control commands.

Supports a learn mode that captures the next keypress and maps it to
a user-selected function.  Mappings are persisted in ``config.json``
under ``input.keyboard_map``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from metixel.shared.ipc import ControlMessage, IPCClient

logger = logging.getLogger(__name__)

# -- Default key map (Linux key codes) ---------------------------------------

DEFAULT_KEY_MAP: dict[int, str] = {
    103: "next",     # KEY_UP
    108: "next",     # KEY_DOWN
    105: "prev",     # KEY_LEFT
    106: "prev",     # KEY_RIGHT
    28:  "resume",   # KEY_ENTER / OK
    57:  "pause",    # KEY_SPACE
    116: "power_off",  # KEY_POWER (only if NOT handled by systemd)
}

# -- Valid commands that can be mapped ---------------------------------------

VALID_COMMANDS = {"next", "prev", "pause", "resume",
                  "power_on", "power_off", "switch_album"}


class KeyboardHandler:
    """Handles input from USB keyboard-emulating remotes.

    Two modes:
    - **normal**: key events are looked up in the key map and the
      corresponding IPC command is sent to the frontend.
    - **learn**: the next keypress is captured and the caller
      (typically a web route) retrieves the key code to persist
      the new mapping.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        ipc: IPCClient | None = None,
    ) -> None:
        self._ipc = ipc
        self._config = config or {}
        self._running = False
        self._thread: threading.Thread | None = None

        # Load key map from config, merging with defaults
        stored = self._config.get("keyboard_map", {})
        # stored maps str → str, invert and merge with defaults
        self._key_map: dict[int, str] = dict(DEFAULT_KEY_MAP)
        for cmd, codes in self._invert_map(stored).items():
            for code in codes:
                self._key_map[code] = cmd

        # Learn mode state (accessed across threads — simple flag is fine)
        self._learn_mode: bool = False
        self._learn_target: str = ""
        self._learn_result: tuple[int, str] | None = None  # (key_code, key_name)
        self._learn_lock = threading.Lock()

    # -- Public API ----------------------------------------------------------

    @property
    def key_map(self) -> dict[str, list[int]]:
        """Return the current mapping as {command: [key_codes]}."""
        return self._invert_map(self._key_map)

    def start_learn(self, target_command: str) -> None:
        """Enter learn mode — next keypress maps to *target_command*."""
        if target_command not in VALID_COMMANDS:
            raise ValueError(f"Unknown command: {target_command}")
        with self._learn_lock:
            self._learn_mode = True
            self._learn_target = target_command
            self._learn_result = None
        logger.info("Learn mode: waiting for key to map → %s", target_command)

    def get_learn_result(self) -> tuple[int, str] | None:
        """Return (key_code, key_name) if a key was learned, or None."""
        with self._learn_lock:
            result = self._learn_result
            self._learn_result = None
            return result

    def cancel_learn(self) -> None:
        """Cancel learn mode without saving."""
        with self._learn_lock:
            self._learn_mode = False
            self._learn_target = ""
            self._learn_result = None

    def set_key_map(self, cmd_map: dict[str, list[int]]) -> None:
        """Replace the entire key mapping."""
        self._key_map.clear()
        for cmd, codes in cmd_map.items():
            if cmd in VALID_COMMANDS:
                for code in codes:
                    self._key_map[code] = cmd

    def run(self) -> None:
        """Find keyboard devices and process key events.  Blocks."""
        try:
            import evdev  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("python3-evdev not installed — keyboard input disabled")
            return

        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        keyboards = [d for d in devices if
                     evdev.ecodes.EV_KEY in d.capabilities()]

        if not keyboards:
            logger.debug("No keyboard input devices found")
            return

        names = [k.name for k in keyboards]
        logger.info("Keyboard handler listening on: %s", names)

        self._running = True
        while self._running:
            try:
                for kbd in keyboards:
                    try:
                        event = kbd.read_one()
                        if event is None:
                            continue
                    except OSError:
                        continue

                    if event.type != evdev.ecodes.EV_KEY:
                        continue
                    if event.value != 1:  # key-down only
                        continue

                    key_name = evdev.ecodes.KEY.get(event.code, f"code={event.code}")

                    # Learn mode?
                    with self._learn_lock:
                        if self._learn_mode:
                            self._learn_result = (event.code, str(key_name))
                            self._learn_mode = False
                            logger.info(
                                "Learned: key %s (%s) → %s",
                                event.code, key_name, self._learn_target,
                            )
                            continue

                    # Normal mode — lookup and execute
                    cmd = self._key_map.get(event.code)
                    if cmd and self._ipc:
                        logger.debug("Key %s (%s) → %s", event.code, key_name, cmd)
                        self._ipc.send(ControlMessage(cmd=cmd))

            except Exception:
                time.sleep(0.1)

    def stop(self) -> None:
        """Signal the handler thread to stop."""
        self._running = False

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _invert_map(
        src: dict[int, str] | dict[str, list[int]],
    ) -> dict[str, list[int]]:
        """Convert between {code: cmd} and {cmd: [codes]} formats."""
        result: dict[str, list[int]] = {}
        for k, v in src.items():
            if isinstance(k, int):
                code, cmd = k, str(v)
            else:
                cmd, codes = str(k), v
                result[cmd] = list(codes) if isinstance(codes, list) else [int(codes)]
                continue
            if cmd not in result:
                result[cmd] = []
            if code not in result[cmd]:
                result[cmd].append(code)
        return result
