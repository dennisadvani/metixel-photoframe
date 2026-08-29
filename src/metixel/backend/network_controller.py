# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network Controller — single owner of WiFi/AP state machine.

All AP activation/deactivation and PIN management flows through this
class.  No other module may start/stop hostapd or mutate PIN state
directly — this eliminates the race conditions between the network
monitor thread and Flask web request threads.

WiFi hardware constraint (enforced by this controller):
    wlan0 can be in client mode OR master (AP) mode — never both.
    ``is_connected()`` (nmcli) is NOT called while AP_ACTIVE because
    it can interact with the radio and disrupt hostapd beacons.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

#: Ephemeral functional-test flag.  When set (``METIXEL_NETWORK_TEST_MODE=1``),
#: the controller ignores Ethernet for connectivity decisions so WiFi/AP tests
#: can run while the Pi is still controlled over Ethernet.  Ethernet stays up
#: (reachable via SSH) — it is only excluded from *connectivity* checks.  This
#: is test scaffolding: never set it in production config.
_TEST_MODE_ENV = "METIXEL_NETWORK_TEST_MODE"


def _test_mode_enabled() -> bool:
    """Return whether the functional network test mode is active."""
    return os.environ.get(_TEST_MODE_ENV) == "1"


class NetworkState(Enum):
    """Exclusive states — the WiFi radio can only be in one mode at a time."""

    CLIENT_CONNECTED = "client_connected"  # WiFi client or Ethernet up
    CLIENT_DISCONNECTED = "client_disconnected"  # No connection, grace period
    AP_ACTIVE = "ap_active"  # WiFi in master mode, PIN on screen
    AP_EXHAUSTED = "ap_exhausted"  # AP timed out, client mode, terminal


# ---------------------------------------------------------------------------
# Low-level operations from network_manager (no state, just CLI wrappers)
# ---------------------------------------------------------------------------

from metixel.backend.network_manager import (  # noqa: E402
    has_saved_wifi_networks,
    is_ap_mode_active,
    is_connected,
    is_ethernet_connected,
    is_wifi_hardware_present,
    pre_scan_for_ap,
)
from metixel.backend.network_manager import (  # noqa: E402
    start_ap_mode as _start_ap,
)
from metixel.backend.network_manager import (  # noqa: E402
    stop_ap_mode as _stop_ap,
)

MAX_PIN_ATTEMPTS = 3
PIN_COOLDOWN_SECONDS = 600  # 10 minutes


class NetworkController:
    """Single owner of WiFi/AP state.

    All state mutations go through :meth:`_transition_to`, which runs
    under an internal lock.  Side effects (start/stop AP, generate PIN,
    dismiss popups) are queued as *pending actions* and drained by the
    monitor loop.

    Thread safety:
        - :meth:`tick` — called by monitor thread (~5s)
        - :meth:`validate_pin`, :meth:`on_wifi_connected` — called by
          Flask request threads
        - All share ``self._lock``
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._config = config

        # ── Functional-test mode ───────────────────────────────────
        # When METIXEL_NETWORK_TEST_MODE=1, Ethernet is ignored for
        # connectivity decisions (see _is_ethernet_connected) so WiFi/AP
        # tests can run while the Pi stays reachable over Ethernet.
        self._test_mode = _test_mode_enabled()

        # ── State machine ──────────────────────────────────────────
        self._state: NetworkState = NetworkState.CLIENT_CONNECTED
        self._state_entered: float = time.monotonic()

        # ── PIN ────────────────────────────────────────────────────
        self._pin: str = ""
        self._pin_attempts: int = 0
        self._pin_locked_until: float = 0.0

        # ── Actions pending for the monitor thread ─────────────────
        self._pending_actions: list[NetworkState] = []

        # ── Connection-in-progress guard ───────────────────────────
        # Set by begin_connection() before stopping the AP for a WiFi
        # connection attempt.  Prevents the monitor tick from seeing
        # the AP as "unexpectedly dead" during the scan delay.
        self._connecting: bool = False

        # ── Display tracking ───────────────────────────────────────
        self._pin_displayed: bool = False

        # ── Clean up stale AP from previous boot ───────────────────
        # If the Pi was powered off while in AP mode, hostapd may still
        # be running on next boot.  The controller initialises as
        # CLIENT_CONNECTED and never calls _transition_to if the state
        # doesn't change, so the stale AP is never stopped — and the
        # captive portal blocks the dashboard.  Kill it on init.
        if is_ap_mode_active():
            logger.warning("Stale AP detected at init — stopping")
            _stop_ap()

    # -- Properties (thread-safe reads) ------------------------------------

    @property
    def state(self) -> NetworkState:
        with self._lock:
            return self._state

    @property
    def pin(self) -> str:
        with self._lock:
            return self._pin

    @property
    def pin_displayed(self) -> bool:
        with self._lock:
            return self._pin_displayed

    # -- Tick — called by monitor thread every ~5s ------------------------

    def tick(self) -> tuple[NetworkState, str, list[NetworkState]]:
        """Advance the state machine.

        Returns:
            (current_state, active_pin, list_of_actions_for_monitor)
        """
        with self._lock:
            self._pending_actions.clear()

            # ── Ethernet-only device (Pi 2, no wlan0) ──────────
            # Skip all WiFi/AP logic — the device can never create
            # an access point.  Just track Ethernet connectivity.
            if not is_wifi_hardware_present():
                if self._is_any_connected():
                    if self._state != NetworkState.CLIENT_CONNECTED:
                        self._transition_to(NetworkState.CLIENT_CONNECTED)
                elif self._state != NetworkState.CLIENT_DISCONNECTED:
                    self._transition_to(NetworkState.CLIENT_DISCONNECTED)
                return (
                    self._state,
                    self._pin,
                    list(self._pending_actions),
                )

            if self._state == NetworkState.CLIENT_CONNECTED:
                if not self._is_any_connected():
                    self._transition_to(NetworkState.CLIENT_DISCONNECTED)

            elif self._state == NetworkState.CLIENT_DISCONNECTED:
                if self._is_any_connected():
                    self._transition_to(NetworkState.CLIENT_CONNECTED)
                elif not has_saved_wifi_networks():
                    # Nothing to retry — AP immediately
                    self._transition_to(NetworkState.AP_ACTIVE)
                elif self._elapsed() >= self._config.get(
                    "ap_grace_period_seconds",
                    300,
                ):
                    self._transition_to(NetworkState.AP_ACTIVE)

            elif self._state == NetworkState.AP_ACTIVE:
                # Ethernet can be checked safely — it's a different radio
                if self._is_ethernet_connected():
                    self._transition_to(NetworkState.CLIENT_CONNECTED)
                elif self._connecting:
                    # connect_to_network() intentionally stopped the AP
                    # for a scan+connect cycle — do NOT treat this as a
                    # crash.  The connection thread will call
                    # end_connection() + on_wifi_connected() when done.
                    pass
                elif not is_ap_mode_active():
                    logger.warning("AP died unexpectedly — marking exhausted")
                    self._transition_to(NetworkState.AP_EXHAUSTED)
                elif self._elapsed() >= self._config.get(
                    "ap_max_duration_seconds",
                    600,
                ):
                    logger.warning(
                        "AP auto-stop after %ds",
                        int(self._elapsed()),
                    )
                    self._transition_to(NetworkState.AP_EXHAUSTED)

            elif self._state == NetworkState.AP_EXHAUSTED:
                # Still check connectivity — WiFi may come back
                if self._is_any_connected():
                    self._transition_to(NetworkState.CLIENT_CONNECTED)
                elif is_ap_mode_active():
                    # Guard: only run sudo commands when actually needed
                    _stop_ap()

            return (
                self._state,
                self._pin,
                list(self._pending_actions),
            )

    # -- Transition (single writer under lock) -----------------------------

    def _transition_to(self, new_state: NetworkState) -> None:
        """Move to *new_state*, performing all side effects atomically.

        Queues the new state as a pending action so the monitor thread
        can show/hide the appropriate popups regardless of which thread
        triggered the transition.
        """
        old = self._state
        if old == new_state:
            return

        self._state = new_state
        self._state_entered = time.monotonic()
        self._pending_actions.append(new_state)

        # ── Enter CLIENT_CONNECTED ─────────────────────────────────
        if new_state == NetworkState.CLIENT_CONNECTED:
            if is_ap_mode_active():
                _stop_ap()
            self._pin = ""
            self._pin_attempts = 0
            self._pin_locked_until = 0.0
            self._pin_displayed = False

        # ── Enter AP_ACTIVE ────────────────────────────────────────
        elif new_state == NetworkState.AP_ACTIVE:
            self._pin = f"{random.randint(0, 9999):04d}"
            self._pin_attempts = 0
            self._pin_locked_until = 0.0
            logger.info("AP PIN generated: %s", self._pin)
            pre_scan_for_ap()
            if not _start_ap():
                logger.error("AP start failed — will retry next tick")
                self._pin = ""
                self._state = old
                self._pending_actions.pop()
                return

        # ── Enter AP_EXHAUSTED ─────────────────────────────────────
        elif new_state == NetworkState.AP_EXHAUSTED:
            _stop_ap()
            self._pin = ""
            self._pin_attempts = 0
            self._pin_displayed = False

        # ── Enter CLIENT_DISCONNECTED (no side effects) ────────────
        # Just start the grace-period clock.  No AP yet.

        logger.info("State: %s → %s", old.value, new_state.value)

    # -- Connectivity helpers (called under lock) --------------------------

    def _is_any_connected(self) -> bool:
        """Check for any upstream connection, gated by current state.

        Ethernet is always safe to check.  WiFi nmcli queries are
        blocked while the AP is active to avoid radio disruption.
        """
        if self._is_ethernet_connected():
            return True
        if self._state != NetworkState.AP_ACTIVE:
            return is_connected()
        return False

    def _is_ethernet_connected(self) -> bool:
        """Check specifically for an active Ethernet connection.

        Uses nmcli but only queries Ethernet — safe alongside an AP.
        In functional-test mode the result is forced to ``False`` so a
        live Ethernet uplink doesn't mask a broken WiFi connection.
        """
        if self._test_mode:
            return False
        return is_ethernet_connected()

    def _elapsed(self) -> float:
        """Seconds since the current state was entered."""
        return time.monotonic() - self._state_entered

    # -- PIN validation — called from Flask request threads ----------------

    def validate_pin(self, candidate: str) -> tuple[bool, str]:
        """Validate a PIN entered on the captive portal.  Thread-safe.

        Returns:
            (valid, message) tuple.
        """
        with self._lock:
            if not self._pin:
                return False, "No PIN active — network may have reconnected"

            now = time.monotonic()

            if self._pin_locked_until > 0 and now < self._pin_locked_until:
                remaining = int(self._pin_locked_until - now)
                return False, f"Too many attempts. Try again in {remaining}s."

            if candidate == self._pin:
                self._pin_attempts = 0
                logger.info("AP PIN validated successfully")
                return True, "ok"

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
                self._pin_attempts,
                MAX_PIN_ATTEMPTS,
            )
            return False, f"Incorrect PIN. {remaining} attempt(s) remaining."

    # -- Called by web routes -------------------------------------------------

    def on_wifi_connected(self) -> None:
        """Notify that WiFi has been connected via the captive portal or API."""
        with self._lock:
            self._transition_to(NetworkState.CLIENT_CONNECTED)

    # -- Connection-in-progress guard --------------------------------------

    def begin_connection(self) -> None:
        """Tell the controller a WiFi connection attempt is about to start.

        The AP will be intentionally stopped for scanning — the monitor
        tick must NOT treat this as an unexpected AP death.
        """
        with self._lock:
            self._connecting = True

    def end_connection(self) -> None:
        """Clear the connection-in-progress flag (success or failure)."""
        with self._lock:
            self._connecting = False

    # -- Display helpers (for the monitor's use) ---------------------------

    def mark_pin_displayed(self) -> None:
        """Called by the monitor after showing the PIN on screen."""
        with self._lock:
            self._pin_displayed = True

    def mark_pin_dismissed(self) -> None:
        """Called by the monitor after dismissing the PIN from screen."""
        with self._lock:
            self._pin_displayed = False
