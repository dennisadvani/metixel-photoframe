# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network management API endpoints for the web dashboard.

Provides Wi-Fi scanning, connection management, and access point (AP)
mode control.  All Wi-Fi operations are delegated to
:mod:`metixel.backend.network_manager`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from flask import Blueprint, current_app, jsonify, session

from metixel.backend.network_manager import (
    connect_to_network,
    forget_network,
    get_connection_status,
    is_ap_mode_active,
    is_connected,
    scan_networks,
)
from metixel.backend.web.helpers import get_body, get_daemon_component, jsonify_error

if TYPE_CHECKING:
    from metixel.backend.network_controller import NetworkController

logger = logging.getLogger(__name__)

network_bp = Blueprint("network", __name__)

#: Session flag set by a successful ``POST /network/validate-pin``.  While
#: the controller has an active PIN, ``POST /network/connect`` refuses any
#: session without it — the PIN check must be enforced server-side, not
#: just by the captive portal hiding the form.
_SESSION_PORTAL_PIN_OK = "portal_pin_ok"


def _get_controller() -> NetworkController | None:
    """Return the NetworkController from the daemon, or None if unavailable."""
    return cast("NetworkController | None", get_daemon_component("_network_controller"))


def _get_str(data: dict, key: str) -> str | None:
    """Return ``data[key]`` stripped, ``""`` if absent, ``None`` if not a str."""
    value = data.get(key, "")
    if not isinstance(value, str):
        return None
    return value.strip()


@network_bp.route("/network/status", methods=["GET"])
def network_status():
    """Get current Wi-Fi connection status and AP mode state.

    AP mode is only reported as active when the AP is running AND there
    is no real network connection — matching the captive-portal logic
    in the web server.
    """
    status = get_connection_status()
    controller = _get_controller()
    ap_active = is_ap_mode_active() or bool(controller and controller.pin)
    status["ap_mode_active"] = ap_active and not is_connected()
    return jsonify(status)


@network_bp.route("/network/scan", methods=["GET"])
def network_scan():
    """Scan for visible Wi-Fi networks.

    When the AP is active, returns cached pre-scan results (the Pi's
    WiFi chip can't scan while in AP mode).  When the AP is not active,
    performs a live scan.
    """
    networks = scan_networks()
    return jsonify({"networks": networks, "cached": is_ap_mode_active()})


@network_bp.route("/network/connect", methods=["POST"])
def network_connect():
    """Connect to a Wi-Fi network.

    Accepts JSON: ``{"ssid": "MyWiFi", "password": "passphrase"}``.
    Empty password is allowed for open networks.

    Returns immediately so the response reaches the client BEFORE the
    AP is torn down.  The actual connection happens in a background
    thread — the phone will lose its AP connection when hostapd stops,
    but by then it has already received the HTTP response.
    """
    data = get_body()
    ssid = _get_str(data, "ssid")
    password = data.get("password", "")

    if ssid is None or not isinstance(password, str):
        return jsonify_error("'ssid' and 'password' must be strings", 400)
    if not ssid:
        return jsonify_error("SSID is required", 400)

    controller = _get_controller()

    # Server-side PIN gate: while the captive portal PIN is active, only a
    # session that has passed /network/validate-pin may connect.
    if controller is not None and controller.pin and not session.get(_SESSION_PORTAL_PIN_OK):
        return jsonify_error("PIN validation required", 403)

    # Tell the controller a connection is in progress so the monitor
    # thread doesn't panic when it sees the AP go down.
    if controller is not None:
        controller.begin_connection()
    # The PIN grant is single-use: clear it once a connect has been kicked off.
    session.pop(_SESSION_PORTAL_PIN_OK, None)

    # Capture references BEFORE the request context ends.  The
    # background thread runs after the response is sent — Flask
    # proxies (current_app, request) are unavailable.
    ipc = current_app.config.get("METIXEL_IPC")

    # Return the response NOW — before stopping the AP.  Once hostapd
    # is killed the client's TCP connection dies, so we must flush the
    # response while the AP is still up.
    response = jsonify(
        {
            "status": "ok",
            "message": f"Connecting to {ssid} — your device will switch networks.",
        }
    )

    # Spawn a background thread to do the actual work
    import threading

    def _do_connect() -> None:
        success, msg = connect_to_network(ssid, password)
        if controller is not None:
            controller.end_connection()
            if success:
                controller.on_wifi_connected()
        if ipc is not None and success:
            try:
                from metixel.shared.ipc import ControlMessage

                # Dismiss any PIN/welcome messages
                ipc.send(ControlMessage(cmd="dismiss_all_messages"))
                # Show a WiFi-connected popup with the IP address
                from metixel.backend.network_manager import get_connection_status

                status = get_connection_status()
                ip_addr = status.get("ip", "")
                if ip_addr and not ip_addr.startswith("192.168.42."):
                    ipc.send(
                        ControlMessage(
                            cmd="show_message",
                            args={
                                "title": f"Connected to {ssid}",
                                "body": (
                                    f"WiFi connected. Access Metixel at "
                                    f"http://metixel.local or http://{ip_addr}"
                                ),
                                "severity": "success",
                                "duration": 60,
                            },
                        )
                    )
            except Exception:
                pass
        elif ipc is not None and not success:
            try:
                from metixel.shared.ipc import ControlMessage

                ipc.send(
                    ControlMessage(
                        cmd="show_message",
                        args={
                            "title": "WiFi Connection Failed",
                            "body": (
                                f'Could not connect to "{ssid}". '
                                "Check the password and try again or the WiFi "
                                "may use an unsupported configuration such as "
                                "WPA3-only."
                            ),
                            "severity": "error",
                            "duration": 30,
                        },
                    )
                )
            except Exception:
                pass

    threading.Thread(target=_do_connect, name="wifi-connect", daemon=True).start()
    return response


@network_bp.route("/network/forget", methods=["POST"])
def network_forget():
    """Forget a saved Wi-Fi network.

    Accepts JSON: ``{"ssid": "MyWiFi"}``.
    """
    data = get_body()
    ssid = _get_str(data, "ssid")

    if ssid is None:
        return jsonify_error("'ssid' must be a string", 400)
    if not ssid:
        return jsonify_error("SSID is required", 400)

    ok = forget_network(ssid)
    if ok:
        return jsonify({"status": "ok", "message": f"Forgot {ssid}"})
    else:
        return jsonify({"status": "ok", "message": f"No saved connection for {ssid}"})


@network_bp.route("/network/ap-status", methods=["GET"])
def ap_status():
    """Check whether the access point (captive portal) is currently active."""
    controller = _get_controller()
    ap_or_pin = is_ap_mode_active() or bool(controller and controller.pin)
    return jsonify({"active": ap_or_pin and not is_connected()})


@network_bp.route("/network/radio", methods=["POST"])
def network_radio():
    """Enable or disable the WiFi radio at the OS level.

    Accepts JSON: ``{"enabled": true|false}``.

    This is the ONLY user-facing way to change the radio, and it is a
    deliberate move of that control out of host provisioning: the radio is
    user-owned state, so nothing converges it on boot or on an OTA.  See the
    note in ``scripts/reconcile.sh`` §8 and
    :meth:`BackendDaemon._ensure_first_run_wifi_radio`.

    Disabling is BLOCKED while the AP is active.  Turning the radio off kills
    hostapd, which would strand a user who is mid-setup in the captive portal —
    the very situation the AP exists to resolve.  A ``409`` is returned with
    ``reason: "ap_active"`` so the UI can explain rather than just fail.

    Disabling is allowed while connected over Wi-Fi, but that can drop the
    caller's own connection, so the response is flushed before the radio
    actually goes down (same pattern as ``/network/connect``).
    """
    from metixel.backend.network_manager import (
        is_wifi_hardware_present,
        set_wifi_radio,
    )

    data = get_body()
    if "enabled" not in data:
        return jsonify_error("Missing 'enabled' (true or false)", 400)
    # Require a real boolean.  `bool("false")` is True, so accepting a string
    # here would silently do the OPPOSITE of what the caller asked for.
    if not isinstance(data["enabled"], bool):
        return jsonify_error("'enabled' must be true or false", 400)
    enabled = data["enabled"]

    if not is_wifi_hardware_present():
        return jsonify_error("No WiFi hardware present on this device", 400)

    controller = _get_controller()

    if not enabled:
        # Block during AP mode: stopping the radio tears down hostapd and would
        # strand anyone using the captive portal.  Checked via both the live AP
        # state and the controller's PIN, matching the /network/status logic.
        ap_active = is_ap_mode_active() or bool(controller and controller.pin)
        if ap_active and not is_connected():
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "Access Point is active",
                        "reason": "ap_active",
                        "message": (
                            "WiFi cannot be turned off while the setup hotspot is "
                            "running. Connect the frame to a network first."
                        ),
                    }
                ),
                409,
            )

        # Flush this response BEFORE the radio drops, otherwise the caller's own
        # socket dies first and they see a network error instead of a result.
        response = jsonify({"status": "ok", "message": "WiFi radio is turning off"})

        import threading

        def _do_disable() -> None:
            if set_wifi_radio(False):
                logger.info("WiFi radio disabled via web UI")
            else:
                logger.warning("WiFi radio disable via web UI failed")

        threading.Thread(target=_do_disable, name="wifi-radio-off", daemon=True).start()
        return response

    # Enabling is synchronous — it completes in well under a second and the
    # caller needs a definite answer (there is no connection to lose).
    if set_wifi_radio(True):
        return jsonify({"status": "ok", "message": "WiFi radio enabled"})
    return jsonify_error("Failed to enable the WiFi radio — check the backend log", 500)


@network_bp.route("/network/ap-start", methods=["POST"])
def ap_start():
    """Manually start the access point (captive portal).

    Note: Manual AP start is discouraged — the NetworkController manages
    AP lifecycle automatically.  This endpoint exists for debugging.
    """
    from metixel.backend.network_manager import start_ap_mode

    ok = start_ap_mode()
    if ok:
        return jsonify({"status": "ok", "message": "AP mode started"})
    else:
        return jsonify({"status": "error", "message": "Failed to start AP mode"}), 500


@network_bp.route("/network/ap-stop", methods=["POST"])
def ap_stop():
    """Manually stop the access point.

    Note: Manual AP stop is discouraged — the NetworkController manages
    AP lifecycle automatically.  This endpoint exists for debugging.
    """
    from metixel.backend.network_manager import stop_ap_mode

    ok = stop_ap_mode()
    if ok:
        return jsonify({"status": "ok", "message": "AP mode stopped"})
    else:
        return jsonify({"status": "error", "message": "Failed to stop AP mode"}), 500


@network_bp.route("/network/validate-pin", methods=["POST"])
def validate_pin():
    """Validate the AP security PIN shown on the frame display.

    Accepts JSON: ``{"pin": "1234"}``.
    Returns ``{"valid": true}`` on success, or an error message with
    remaining attempts on failure.  After 3 wrong attempts the PIN
    is locked for 10 minutes.
    """
    data = get_body()
    candidate = _get_str(data, "pin")

    if not candidate or len(candidate) != 4 or not candidate.isdigit():
        return jsonify({"valid": False, "message": "Enter a 4-digit PIN"}), 400

    controller = _get_controller()
    if controller is None:
        return jsonify({"valid": False, "message": "Network controller unavailable"}), 503

    valid, message = controller.validate_pin(candidate)

    if valid:
        # Grant this session the right to call /network/connect.
        session[_SESSION_PORTAL_PIN_OK] = True
        return jsonify({"valid": True, "message": "PIN accepted"})
    else:
        session.pop(_SESSION_PORTAL_PIN_OK, None)
        return jsonify({"valid": False, "message": message}), 403
