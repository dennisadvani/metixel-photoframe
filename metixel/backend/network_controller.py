# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network Controller — single owner of WiFi/AP state machine.

All AP activation/deactivation and PIN management flows through this
class.  No other module may start/stop hostapd or mutate PIN state
directly — this eliminates the race conditions between the network
monitor thread and Flask web request threads.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class NetworkPhase(Enum):
    """Current phase of the network state machine."""
    MONITORING = "monitoring"        # Normal — WiFi connected, nothing to do
    GRACE_PERIOD = "grace_period"    # WiFi lost — retrying before activating AP
    AP_ACTIVE = "ap_active"          # AP broadcasting, PIN on screen
    AP_EXHAUSTED = "ap_exhausted"    # AP timed out — never again until reboot


# ---------------------------------------------------------------------------
# Imports from network_manager for low-level operations
# ---------------------------------------------------------------------------

from metixel.backend.network_manager import (  # noqa: E402
    connect_to_network,
    forget_network,
    get_connection_status,
    has_saved_wifi_networks,
    is_ap_mode_active,
    is_connected,
    is_wifi_radio_enabled,
    pre_scan_for_ap,
    scan_networks,
    start_ap_mode as _start_ap,
    stop_ap_mode as _stop_ap,
)

MAX_PIN_ATTEMPTS = 3
PIN_COOLDOWN_SECONDS = 600  # 10 minutes


class NetworkController:
    """Single owner of WiFi/AP state.

    All state mutations happen under an internal lock.  The monitor
    thread calls :meth:`tick` every ~10 seconds; Flask routes call
    :meth:`validate_pin` and :meth:`on_wifi_connected` from request
    threads.

    The state machine::

        MONITORING ──(WiFi lost)──► GRACE_PERIOD ──(5 min)──► AP_ACTIVE
            ▲                          │                          │
            │                          │ (WiFi back)              │ (10 min timeout,
            │                          ▼                          │  no user connected)
            └──────────────────────────┘                          ▼
                                                          AP_EXHAUSTED
                                                          (terminal until reboot)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._config = config

        # ── PIN state ──────────────────────────────────────────────
        self._pin: str = ""
        self._pin_attempts: int = 0
        self._pin_locked_until: float = 0.0

        # ── Phase state ────────────────────────────────────────────
        self._phase: NetworkPhase = NetworkPhase.MONITORING
        self._grace_start: float = 0.0       # monotonic; 0 = not in grace
        self._ap_start: float = 0.0           # monotonic; 0 = AP not active
        self._exhausted: bool = False         # AP timed out — never again

        # ── Display tracking (for the monitor's use) ───────────────
        self._pin_displayed: bool = False     # PIN message is on screen

    # -- Properties (thread-safe reads) ------------------------------------

    @property
    def phase(self) -> NetworkPhase:
        with self._lock:
            return self._phase

    @property
    def pin(self) -> str:
        with self._lock:
            return self._pin

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._exhausted

    # -- Tick — called by the monitor thread every ~10s --------------------

    def tick(self) -> tuple[NetworkPhase, str]:
        """Advance the state machine.  Call from the monitor thread.

        Returns:
            (current_phase, active_pin_or_empty_string)
        """
        with self._lock:
            if is_connected():
                self._on_connected()
            else:
                self._on_disconnected()
            return (self._phase, self._pin)

    def _on_connected(self) -> None:
        """Called when a real upstream connection is detected."""
        # If AP is active, tear it down
        if is_ap_mode_active():
            _stop_ap()

        # Reset all AP-related state (but NOT exhausted — only reboot clears that)
        self._pin = ""
        self._pin_attempts = 0
        self._pin_locked_until = 0.0
        self._grace_start = 0.0
        self._ap_start = 0.0
        self._phase = NetworkPhase.MONITORING
        self._pin_displayed = False

    def _on_disconnected(self) -> None:
        """Called when no upstream connection is found."""
        now = time.monotonic()

        # ── Terminal state: AP already exhausted ──────────────────
        if self._exhausted:
            # Ensure wlan0 is in managed mode so NetworkManager can
            # auto-connect to saved WiFi (if any).  _stop_ap is
            # idempotent — safe to call even if AP is already down.
            _stop_ap()
            return

        # ── AP is currently active — check for timeout ────────────
        if self._ap_start > 0:
            # AP crashed or was killed externally (OOM, driver error,
            # someone ran `systemctl stop hostapd`).  Treat the same
            # as a timeout — stop the AP (returns wlan0 to managed
            # mode) and never reactivate.
            if not is_ap_mode_active():
                logger.warning(
                    "AP died unexpectedly — stopping and marking exhausted"
                )
                _stop_ap()  # Ensure wlan0 returns to managed mode
                self._pin = ""
                self._pin_attempts = 0
                self._exhausted = True
                self._ap_start = 0.0
                self._grace_start = 0.0
                self._phase = NetworkPhase.AP_EXHAUSTED
                self._pin_displayed = False
                return

            elapsed = now - self._ap_start
            max_duration = self._config.get("ap_max_duration_seconds", 600)
            if elapsed >= max_duration:
                logger.warning(
                    "AP auto-stop after %ds — no user connected; "
                    "AP will not reactivate until next reboot",
                    int(elapsed),
                )
                _stop_ap()
                self._pin = ""
                self._pin_attempts = 0
                self._exhausted = True
                self._ap_start = 0.0
                self._grace_start = 0.0
                self._phase = NetworkPhase.AP_EXHAUSTED
                self._pin_displayed = False
                # wlan0 is now back in managed mode (stop_ap_mode did it).
                # NetworkManager will auto-connect to any saved network.
            return

        # ── No saved networks?  Skip grace, go straight to AP ─────
        if not has_saved_wifi_networks():
            self._activate_ap()
            return

        # ── Start or continue grace period ─────────────────────────
        if self._grace_start == 0.0:
            self._grace_start = now
            self._phase = NetworkPhase.GRACE_PERIOD
            logger.info(
                "Network lost — entering grace period (%ds)",
                self._config.get("ap_grace_period_seconds", 300),
            )
            return

        grace_elapsed = now - self._grace_start
        grace_limit = self._config.get("ap_grace_period_seconds", 300)
        if grace_elapsed >= grace_limit:
            logger.warning(
                "Grace period expired after %ds — activating AP fallback",
                int(grace_elapsed),
            )
            self._activate_ap()

    def _activate_ap(self) -> None:
        """Activate the AP fallback with a fresh PIN."""
        if is_ap_mode_active():
            return  # Already active

        self._pin = f"{random.randint(0, 9999):04d}"
        self._pin_attempts = 0
        self._pin_locked_until = 0.0
        logger.info("AP PIN generated: %s", self._pin)

        pre_scan_for_ap()
        if _start_ap():
            self._ap_start = time.monotonic()
            self._phase = NetworkPhase.AP_ACTIVE
            self._grace_start = 0.0
            logger.info("AP fallback activated (PIN: %s)", self._pin)
        else:
            logger.error("AP fallback activation failed — will retry on next tick")
            self._pin = ""

    # -- PIN validation — called from Flask request threads ----------------

    def validate_pin(self, candidate: str) -> tuple[bool, str]:
        """Validate a PIN entered on the captive portal.  Thread-safe.

        Returns:
            (valid, message) tuple.
        """
        with self._lock:
            # Never pass when no PIN is active
            if not self._pin:
                return False, "No PIN active — network may have reconnected"

            now = time.monotonic()

            # Cooldown check
            if self._pin_locked_until > 0 and now < self._pin_locked_until:
                remaining = int(self._pin_locked_until - now)
                return False, f"Too many attempts. Try again in {remaining}s."

            # Validate
            if candidate == self._pin:
                self._pin_attempts = 0
                logger.info("AP PIN validated successfully")
                return True, "ok"

            # Wrong PIN
            self._pin_attempts += 1
            remaining = MAX_PIN_ATTEMPTS - self._pin_attempts

            if self._pin_attempts >= MAX_PIN_ATTEMPTS:
                self._pin_locked_until = now + PIN_COOLDOWN_SECONDS
                self._pin_attempts = 0
                logger.warning(
                    "AP PIN locked after %d failed attempts",
                    MAX_PIN_ATTEMPTS,
                )
                return False, f"Locked. Try again in {PIN_COOLDOWN_SECONDS}s."

            logger.warning(
                "AP PIN validation failed (%d/%d attempts)",
                self._pin_attempts, MAX_PIN_ATTEMPTS,
            )
            return False, f"Incorrect PIN. {remaining} attempt(s) remaining."

    def clear_pin(self) -> None:
        """Clear the active PIN (called when WiFi connects)."""
        with self._lock:
            self._pin = ""
            self._pin_attempts = 0
            self._pin_locked_until = 0.0

    # -- Called by web routes after a successful WiFi connection ------------

    def on_wifi_connected(self) -> None:
        """Notify the controller that WiFi has been connected via the API."""
        with self._lock:
            if is_ap_mode_active():
                _stop_ap()
            self._pin = ""
            self._pin_attempts = 0
            self._pin_locked_until = 0.0
            self._grace_start = 0.0
            self._ap_start = 0.0
            self._phase = NetworkPhase.MONITORING
            self._pin_displayed = False

    # -- Display helpers (for the monitor's use) ---------------------------

    def mark_pin_displayed(self) -> None:
        """Called by the monitor after showing the PIN on screen."""
        with self._lock:
            self._pin_displayed = True

    def mark_pin_dismissed(self) -> None:
        """Called by the monitor after dismissing the PIN from screen."""
        with self._lock:
            self._pin_displayed = False

    @property
    def pin_displayed(self) -> bool:
        with self._lock:
            return self._pin_displayed
