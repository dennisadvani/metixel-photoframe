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
    is_pin_required,
    scan_networks,
    start_ap_mode,
    stop_ap_mode,
    validate_ap_pin,
)

logger = logging.getLogger(__name__)

network_bp = Blueprint("network", __name__)


@network_bp.route("/network/status", methods=["GET"])
def network_status():
    """Get current Wi-Fi connection status and AP mode state.

    AP mode is only reported as active when the AP (or PIN) is actually
    running AND there is no real network connection — matching the
    captive-portal logic in the web server.
    """
    status = get_connection_status()
    ap_or_pin = is_ap_mode_active() or is_pin_required()
    status["ap_mode_active"] = ap_or_pin and not is_connected()
    return jsonify(status)


@network_bp.route("/network/scan", methods=["GET"])
def network_scan():
    """Scan for visible Wi-Fi networks.

    By default serves cached results from a pre-AP scan (the Pi's WiFi
    chip can't scan while in AP mode).  Pass ``?force=1`` to perform a
    live scan — this will briefly drop the AP, disconnecting captive
    portal clients.  Use with a warning to the user.
    """
    force = request.args.get("force", "0") == "1"
    networks = scan_networks(force_live=force)
    return jsonify({"networks": networks, "cached": not force and is_ap_mode_active()})


@network_bp.route("/network/connect", methods=["POST"])
def network_connect():
    """Connect to a Wi-Fi network.

    Accepts JSON: ``{"ssid": "MyWiFi", "password": "passphrase"}``.
    Empty password is allowed for open networks.
    """
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")

    if not ssid:
        return jsonify({"error": "SSID is required"}), 400

    success, message = connect_to_network(ssid, password)
    if success:
        # Immediately clear the PIN gate so the dashboard is accessible
        from metixel.backend.network_manager import clear_ap_pin
        clear_ap_pin()
        # Tell the frontend to dismiss the PIN message
        ipc = current_app.config.get("METIXEL_IPC")
        if ipc is not None:
            try:
                from metixel.shared.ipc import ControlMessage
                ipc.send(ControlMessage(cmd="dismiss_all_messages"))
            except Exception:
                pass
        return jsonify({"status": "ok", "message": message})
    else:
        return jsonify({"status": "error", "message": message}), 400


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
    """Check whether the access point (captive portal) is currently active.

    Only reports active when the AP (or PIN) is running AND there is no
    real network connection — matching the captive-portal logic.
    """
    ap_or_pin = is_ap_mode_active() or is_pin_required()
    return jsonify({"active": ap_or_pin and not is_connected()})


@network_bp.route("/network/ap-start", methods=["POST"])
def ap_start():
    """Manually start the access point (captive portal)."""
    ok = start_ap_mode()
    if ok:
        return jsonify({"status": "ok", "message": "AP mode started"})
    else:
        return jsonify({"status": "error", "message": "Failed to start AP mode"}), 500


@network_bp.route("/network/ap-stop", methods=["POST"])
def ap_stop():
    """Manually stop the access point."""
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

    valid, message = validate_ap_pin(candidate)
    if valid:
        return jsonify({"valid": True, "message": "PIN accepted"})
    else:
        return jsonify({"valid": False, "message": message}), 403
