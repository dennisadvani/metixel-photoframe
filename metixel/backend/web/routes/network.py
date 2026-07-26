# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network management API endpoints for the web dashboard.

Provides Wi-Fi scanning, connection management, and access point (AP)
mode control.  All Wi-Fi operations are delegated to
:mod:`metixel.backend.network_manager`.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from metixel.backend.network_manager import (
    connect_to_network,
    forget_network,
    get_connection_status,
    is_ap_mode_active,
    scan_networks,
    start_ap_mode,
    stop_ap_mode,
)

logger = logging.getLogger(__name__)

network_bp = Blueprint("network", __name__)


@network_bp.route("/network/status", methods=["GET"])
def network_status():
    """Get current Wi-Fi connection status and AP mode state."""
    status = get_connection_status()
    status["ap_mode_active"] = is_ap_mode_active()
    return jsonify(status)


@network_bp.route("/network/scan", methods=["GET"])
def network_scan():
    """Scan for visible Wi-Fi networks.

    Returns a list of networks sorted by signal strength (strongest first).
    """
    networks = scan_networks()
    return jsonify({"networks": networks})


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
    """Check whether the access point (captive portal) is currently active."""
    return jsonify({"active": is_ap_mode_active()})


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
