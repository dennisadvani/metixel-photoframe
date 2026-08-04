# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network management API endpoints for the web dashboard.

Provides Wi-Fi scanning, connection management, and access point (AP)
mode control.  All Wi-Fi operations are delegated to
:mod:`metixel.backend.network_manager`.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from metixel.backend.network_manager import (
    connect_to_network,
    forget_network,
    get_connection_status,
    is_ap_mode_active,
    is_connected,
    scan_networks,
)

logger = logging.getLogger(__name__)

network_bp = Blueprint("network", __name__)


def _get_controller() -> object | None:
    """Return the NetworkController from the daemon, or None if unavailable."""
    daemon = current_app.config.get("METIXEL_DAEMON")
    if daemon is not None:
        return getattr(daemon, "_network_controller", None)
    return None


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
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")

    if not ssid:
        return jsonify({"error": "SSID is required"}), 400

    # Tell the controller a connection is in progress so the monitor
    # thread doesn't panic when it sees the AP go down.
    controller = _get_controller()
    if controller is not None:
        controller.begin_connection()

    # Capture references BEFORE the request context ends.  The
    # background thread runs after the response is sent — Flask
    # proxies (current_app, request) are unavailable.
    ipc = current_app.config.get("METIXEL_IPC")

    # Return the response NOW — before stopping the AP.  Once hostapd
    # is killed the client's TCP connection dies, so we must flush the
    # response while the AP is still up.
    response = jsonify({
        "status": "ok",
        "message": f"Connecting to {ssid} — your device will switch networks.",
    })

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
                    ipc.send(ControlMessage(
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
                    ))
            except Exception:
                pass

    threading.Thread(target=_do_connect, name="wifi-connect", daemon=True).start()
    return response


@network_bp.route("/network/forget", methods=["POST"])
def network_forget():
    """Forget a saved Wi-Fi network.

    Accepts JSON: ``{"ssid": "MyWiFi"}``.
    """
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()

    if not ssid:
        return jsonify({"error": "SSID is required"}), 400

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
    data = request.get_json(silent=True) or {}
    candidate = data.get("pin", "").strip()

    if not candidate or len(candidate) != 4 or not candidate.isdigit():
        return jsonify({"valid": False, "message": "Enter a 4-digit PIN"}), 400

    controller = _get_controller()
    if controller is None:
        return jsonify({"valid": False, "message": "Network controller unavailable"}), 503

    valid, message = controller.validate_pin(candidate)

    if valid:
        return jsonify({"valid": True, "message": "PIN accepted"})
    else:
        return jsonify({"valid": False, "message": message}), 403
