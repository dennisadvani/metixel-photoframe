# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: Wi-Fi scan/connect/forget + the network state-machine
transitions that drive the welcome / no-network / connected messages.

These run ON the Pi with ``METIXEL_NETWORK_TEST_MODE=1`` so the controller
ignores Ethernet for connectivity decisions — the Pi stays reachable over
Ethernet (SSH) while the Wi-Fi radio is exercised.

The tests run in a LOGICAL ORDER (pytest runs them in file order) so each
test's precondition is set by the previous one:

    1. scan            — radio enabled + test SSID visible
    2. no-network→AP   — no WiFi connected → controller enters AP_ACTIVE
                         (drives the "No network connection detected" + PIN
                         welcome message)
    3. AP stays up     — while still disconnected, the AP must remain up
    4. connect→welcome — connect to the test WiFi → controller enters
                         CLIENT_CONNECTED (drives the "Connected via WiFi" +
                         first-run welcome message)
    5. forget          — drop the test network, restoring prior state
    6. AP exhausted    — with no network again, the AP comes up and then
                         expires after its max duration (drives the "WiFi
                         Offline" warning)

A single module-scoped controller is shared so state carries across tests.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from metixel.backend import network_manager as nm
from metixel.backend.network_controller import NetworkController, NetworkState

pytestmark = pytest.mark.functional

#: How long to wait for a Wi-Fi connection to come up.
_CONNECT_WAIT = 45
#: How long to wait for the AP to come up on hardware.
_AP_WAIT = 30
#: Real-ish grace period before the AP activates (seconds).
_GRACE = 30
#: Shortened AP max duration so the exhausted test stays quick (seconds).
_AP_MAX = 60


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _wait_for_connected(timeout: int = _CONNECT_WAIT) -> bool:
    """Poll nm.is_connected() until the Wi-Fi link is up or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nm.is_connected():
            return True
        time.sleep(2)
    return False


def _wait_for_ap(timeout: int = _AP_WAIT) -> bool:
    """Poll is_ap_mode_active() until hostapd is up or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if nm.is_ap_mode_active():
            return True
        time.sleep(2)
    return False


def _ensure_no_wifi_connected() -> None:
    """Disconnect any active WiFi and forget all saved WiFi connections.

    Establishes the "no network" precondition the message tests need.  A
    previous run (or a real frame) may have left WiFi connected or saved —
    the no-network → AP test must start from a clean slate.
    """
    # Disconnect wlan0 (no-op if already disconnected).
    _run(["sudo", "nmcli", "device", "disconnect", "wlan0"])
    # Forget every saved WiFi connection so the controller has nothing to
    # auto-connect and falls through to AP immediately.
    result = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    for line in result.stdout.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            _run(["sudo", "nmcli", "connection", "delete", parts[0]])
    time.sleep(2)


@pytest.fixture(scope="module")
def controller() -> NetworkController:
    """A module-scoped controller in functional-test mode.

    Shared across the ordered tests so state carries from one to the next.
    """
    assert os.environ.get("METIXEL_NETWORK_TEST_MODE") == "1", (
        "Wi-Fi functional tests must run with METIXEL_NETWORK_TEST_MODE=1 "
        "so Ethernet is ignored for connectivity."
    )
    return NetworkController(
        {
            "ap_grace_period_seconds": _GRACE,
            "ap_max_duration_seconds": _AP_MAX,
        }
    )


def test_wifi_scan_finds_test_network(wifi_creds: dict[str, str]) -> None:
    """The configured test SSID must be visible in a live scan."""
    # A disabled radio (e.g. pi-gen not enabling WiFi) makes every scan
    # return empty — fail with a clear message rather than a confusing
    # "SSID not found".  This is an OS-setup issue, not something the test
    # should silently fix.
    assert nm.is_wifi_radio_enabled(), (
        "Wi-Fi radio is disabled at the OS level (nmcli radio wifi). "
        "Enable it via `sudo nmcli radio wifi on` or raspi-config."
    )
    networks = nm.scan_networks()
    ssids = {n["ssid"] for n in networks}
    assert wifi_creds["ssid"] in ssids, (
        f"test SSID {wifi_creds['ssid']!r} not found in scan; found: {sorted(ssids)}"
    )


def test_no_network_triggers_ap(controller: NetworkController) -> None:
    """With no WiFi connected (and no saved networks), the controller must
    transition to AP_ACTIVE after the grace period — which drives the
    "No network connection detected" + PIN welcome message on screen."""
    # Establish the no-network precondition (a prior run may have left WiFi
    # connected or saved).
    _ensure_no_wifi_connected()

    # First tick: CLIENT_CONNECTED → CLIENT_DISCONNECTED (no WiFi, ethernet ignored).
    state, _, _ = controller.tick()
    assert state == NetworkState.CLIENT_DISCONNECTED

    # Wait the grace period, then tick → AP_ACTIVE.
    time.sleep(_GRACE + 2)
    state, pin, actions = controller.tick()
    assert state == NetworkState.AP_ACTIVE, f"expected AP_ACTIVE after grace period, got {state}"
    assert pin, "AP_ACTIVE must generate a PIN for the welcome message"
    assert NetworkState.AP_ACTIVE in actions, (
        "AP_ACTIVE must be queued as a pending action so the daemon shows "
        "the no-network welcome message"
    )

    # The AP must actually come up on hardware.
    assert _wait_for_ap(), "hostapd did not become active in time"


def test_ap_stays_up_while_no_network(controller: NetworkController) -> None:
    """While disconnected, the AP must stay up (the "no network" welcome
    stays on screen)."""
    assert controller.state == NetworkState.AP_ACTIVE, (
        "AP must already be active from the previous test"
    )
    assert _wait_for_ap(), "AP is not up"

    # A few ticks later, still disconnected → AP must remain active.
    for _ in range(3):
        time.sleep(2)
        state, _, _ = controller.tick()
        assert state == NetworkState.AP_ACTIVE, (
            f"AP dropped while still disconnected (state={state})"
        )
        assert nm.is_ap_mode_active(), "hostapd stopped while still disconnected"


def test_connect_transitions_to_connected_and_generates_welcome(
    controller: NetworkController, wifi_creds: dict[str, str]
) -> None:
    """Connecting to the test WiFi must transition the controller to
    CLIENT_CONNECTED — which drives the "Connected via WiFi" message and the
    first-run welcome on screen."""
    assert controller.state == NetworkState.AP_ACTIVE, "AP must be active before connecting"

    # Connect to the test network (stops the AP, connects WiFi).
    ok, message = nm.connect_to_network(wifi_creds["ssid"], wifi_creds["password"])
    assert ok, f"connect_to_network failed: {message}"
    assert _wait_for_connected(), "Wi-Fi did not reach a connected state in time"

    # Notify the controller that WiFi is connected.
    controller.on_wifi_connected()

    # The controller must now be CLIENT_CONNECTED — the daemon's
    # _drain_actions shows the "Connected via WiFi" + welcome messages.
    # (on_wifi_connected() already queued the CLIENT_CONNECTED action; a
    # subsequent tick() clears the queue, so we assert the state directly.)
    assert controller.state == NetworkState.CLIENT_CONNECTED, (
        "controller must be CLIENT_CONNECTED after on_wifi_connected()"
    )

    # The AP must be torn down once connected.
    assert not nm.is_ap_mode_active(), "AP still active after connecting"


def test_wifi_forget_restores_state(
    controller: NetworkController, wifi_creds: dict[str, str]
) -> None:
    """Forgetting the test network must succeed and drop the Wi-Fi link.

    Ethernet stays up for control, so we assert the Wi-Fi interface is no
    longer connected — not that nothing is connected at all.
    """
    assert nm.forget_network(wifi_creds["ssid"]), "forget_network failed"

    # Give NetworkManager a moment to drop the link.
    time.sleep(3)
    status = nm.get_connection_status()
    # The primary interface may be ethernet (control link) — that's fine.
    # What matters is that Wi-Fi is no longer the connected interface.
    assert status["interface_type"] != "wifi", (
        "still connected via Wi-Fi after forgetting the network"
    )


def test_ap_exhausted_after_max_duration(controller: NetworkController) -> None:
    """After forgetting the network, the AP comes back up; once it stays up
    past its max duration with no connection, the controller must transition
    to AP_EXHAUSTED — which drives the "WiFi Offline" warning message."""
    # After forget, no saved networks → the controller returns to AP_ACTIVE.
    state, _, _ = controller.tick()
    if state != NetworkState.AP_ACTIVE:
        # May need a second tick to leave CLIENT_CONNECTED → DISCONNECTED → AP.
        state, _, _ = controller.tick()
    assert state == NetworkState.AP_ACTIVE, f"expected AP_ACTIVE after forgetting, got {state}"
    assert _wait_for_ap(), "AP did not come up after forgetting"

    # Wait past the max duration, then tick → AP_EXHAUSTED.
    time.sleep(_AP_MAX + 2)
    state, _, actions = controller.tick()
    assert state == NetworkState.AP_EXHAUSTED, (
        f"expected AP_EXHAUSTED after max duration, got {state}"
    )
    assert NetworkState.AP_EXHAUSTED in actions, (
        "AP_EXHAUSTED must be queued so the daemon shows the WiFi Offline warning"
    )
    # The AP is stopped on exhaustion.
    assert not nm.is_ap_mode_active(), "AP still active after exhaustion"
