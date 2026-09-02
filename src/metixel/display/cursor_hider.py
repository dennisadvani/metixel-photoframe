# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Cursor hider for the cage/Wayland frontend.

Hides the compositor cursor by creating a persistent virtual absolute mouse
(via ``/dev/uinput`` / evdev) and parking it off-screen.  This is the pure
Python equivalent of the ydotool off-screen move, but needs no daemon/socket.

The service is **trigger-based**: it creates the persistent device at startup,
then listens on a Unix socket.  When the frontend starts (a client sends a
``hide`` trigger), it fires the off-screen move every 0.1s for a short window
(2s), then stops.  This avoids continuously injecting input events.

Why this works (three requirements, all verified on-device):
1. **Persistent device** — the ``UInput`` device is created WITHOUT a ``with``
   block and kept open for the process lifetime.  A fire-and-exit device is
   torn down before libinput attaches it, so the cursor never moves.
2. **Random changing values** — each fire writes a RANDOM coordinate.  Writing
   the same value repeatedly is a kernel no-op (no event emitted); random
   values force a change, so an event is always produced.
3. **Fixed ABS range** — the ABS range is set to a small fixed value (0..4096)
   and we write coordinates BEYOND it (5000..6000).  wlroots/cage maps the ABS
   range linearly onto the output, so ``pointer = value / ABS_max * width``.
   Since ``value > ABS_max`` always, ``pointer > width`` for ANY resolution
   (720p through 4K and beyond) — no resolution detection needed.

The daemon runs as root (for ``/dev/uinput`` access) via a dedicated systemd
unit (``metixel-cursor-hider.service``), started before cage.  The frontend
sends a ``hide`` trigger via :class:`CursorHiderClient` when it starts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import socket
import time
from typing import Protocol

logger = logging.getLogger(__name__)

# Fixed ABS range.  wlroots maps this onto the output; writing values beyond
# it always lands off-screen regardless of resolution.
_ABS_MAX = 4096
# Random off-screen coordinate range (always > _ABS_MAX).
_LO = 5000
_HI = 6000

# Unix socket the service listens on for triggers.
DEFAULT_SOCKET_PATH = "/run/metixel/cursor-hider.sock"
# How long to keep firing after a trigger (seconds).
_FIRE_DURATION = 2.0


class _UInputLike(Protocol):
    """Minimal interface for the evdev.UInput object we use."""

    def write(self, etype: int, code: int, value: int) -> None: ...

    def syn(self) -> None: ...

    def close(self) -> None: ...


class CursorHider:
    """Persistent virtual absolute mouse that parks the cursor off-screen.

    Listens on a Unix socket for a ``hide`` trigger; when received, fires the
    off-screen move every ``interval`` seconds for ``fire_duration`` seconds.
    """

    def __init__(
        self,
        interval: float = 0.1,
        lo: int = _LO,
        hi: int = _HI,
        fire_duration: float = _FIRE_DURATION,
        socket_path: str = DEFAULT_SOCKET_PATH,
    ):
        self._interval = interval
        self._lo = lo
        self._hi = hi
        self._fire_duration = fire_duration
        self._socket_path = socket_path
        self._ui: _UInputLike | None = None  # evdev.UInput, imported lazily
        self._count = 0
        self._running = False
        # Axis codes (set in start()); kept so park() doesn't re-import evdev.
        self._abs_x = 0
        self._abs_y = 0

    def start(self) -> None:
        """Create the persistent device (kept open for the process lifetime)."""
        try:
            from evdev import UInput
            from evdev import ecodes as e

            self._abs_x = e.ABS_X
            self._abs_y = e.ABS_Y
            cap = {
                e.EV_ABS: (
                    (e.ABS_X, (0, 0, _ABS_MAX)),
                    (e.ABS_Y, (0, 0, _ABS_MAX)),
                ),
                e.EV_KEY: (e.BTN_LEFT, e.BTN_RIGHT),
            }
            # Persistent device — do NOT use a context manager.
            self._ui = UInput(cap, name="metixel-cursor-hider")
            # Initialise axes to 0 so the first park() write is a change.
            self._ui.write(e.EV_ABS, e.ABS_X, 0)
            self._ui.write(e.EV_ABS, e.ABS_Y, 0)
            self._ui.syn()
            logger.info("Created persistent uinput device metixel-cursor-hider")
        except PermissionError:
            logger.error(
                "Insufficient permissions to access /dev/uinput. "
                "Run as root or add the user to the 'input' group."
            )
            raise
        except Exception as ex:  # noqa: BLE001 - never crash the frontend
            logger.error("Failed to create uinput device: %s", ex)
            raise

    def park(self) -> None:
        """Send one absolute move to a random off-screen coordinate."""
        if not self._ui:
            return
        try:
            rx = random.randint(self._lo, self._hi)
            ry = random.randint(self._lo, self._hi)
            self._ui.write(3, self._abs_x, rx)  # 3 = EV_ABS
            self._ui.write(3, self._abs_y, ry)
            self._ui.syn()
            self._count += 1
        except Exception as ex:  # noqa: BLE001 - never crash the frontend
            logger.warning("Failed to send park events: %s", ex)

    def _fire_window(self) -> None:
        """Fire the off-screen move every ``interval`` for ``fire_duration``."""
        logger.info("Cursor-hide trigger received — firing for %.1fs", self._fire_duration)
        end = time.monotonic() + self._fire_duration
        while time.monotonic() < end:
            self.park()
            time.sleep(self._interval)
        logger.info("Cursor-hide window complete (%d events)", self._count)

    def _listen(self) -> None:
        """Listen on the Unix socket for a ``hide`` trigger.

        The socket file can be removed by other processes sharing
        ``/run/metixel`` (e.g. the frontend's IPC cleanup).  We detect a
        missing socket and rebind it so triggers keep working.
        """
        if not hasattr(socket, "AF_UNIX"):
            logger.debug("AF_UNIX not available — cursor-hider trigger disabled")
            return
        sock_dir = os.path.dirname(self._socket_path)
        os.makedirs(sock_dir, exist_ok=True)

        def _bind() -> socket.socket:
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)  # type: ignore[attr-defined]
            s.bind(self._socket_path)
            # The frontend runs as 'pi' (not root) — make the socket writable
            # by all so it can send triggers.
            with contextlib.suppress(OSError):
                os.chmod(self._socket_path, 0o666)
            s.settimeout(0.5)
            return s

        sock = _bind()
        logger.info("Cursor-hider listening on %s", self._socket_path)
        try:
            while self._running:
                # Recreate the socket if it was removed by another process.
                if not os.path.exists(self._socket_path):
                    logger.warning("Cursor-hider socket missing — rebinding")
                    with contextlib.suppress(Exception):
                        sock.close()
                    sock = _bind()
                try:
                    data = sock.recv(4096)
                    if data:
                        try:
                            msg = json.loads(data.decode("utf-8"))
                            if msg.get("cmd") == "hide":
                                self._fire_window()
                        except Exception:  # noqa: BLE001
                            logger.warning("Bad trigger message", exc_info=True)
                except TimeoutError:
                    continue
                except BlockingIOError:
                    continue
        finally:
            sock.close()
            with contextlib.suppress(OSError):
                os.unlink(self._socket_path)

    def run(self) -> None:
        """Create the device, fire once immediately, then listen for triggers.

        The service starts BEFORE cage (``Before=metixel-cage.service``), so
        firing the hide window on startup parks the cursor off-screen before
        cage draws it.  It then keeps listening so the frontend can re-trigger
        (e.g. on hot-plug or restart) as a safety net.
        """
        self.start()
        self._running = True
        # Fire immediately on startup — before cage boots — so the cursor is
        # parked off-screen before it is ever drawn.
        self._fire_window()
        self._listen()

    def close(self) -> None:
        self._running = False
        if self._ui is not None:
            with contextlib.suppress(Exception):
                self._ui.close()
            self._ui = None


class CursorHiderClient:
    """Sends a ``hide`` trigger to the cursor-hider service.

    The frontend calls :meth:`trigger` when it starts so the service parks the
    cursor off-screen.  Best-effort — never raises if the service is absent.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH):
        self._socket_path = socket_path

    def trigger(self) -> bool:
        """Send a ``hide`` trigger. Returns True on success."""
        if not hasattr(socket, "AF_UNIX"):
            logger.debug("AF_UNIX not available — cursor-hider trigger skipped")
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.sendto(json.dumps({"cmd": "hide"}).encode("utf-8"), self._socket_path)
                logger.info("Sent cursor-hide trigger to %s", self._socket_path)
                return True
            finally:
                sock.close()
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug("Cursor-hider trigger failed (service not running?)")
            return False


def build_cursor_hider(interval: float = 0.1) -> CursorHider:
    """Composition-root factory for the cursor hider."""
    return CursorHider(interval=interval)
