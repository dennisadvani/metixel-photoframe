# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Realtime frontend control endpoints (IPC)."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

control_bp = Blueprint("control", __name__)


@control_bp.route("", methods=["POST"])
def send_control():
    """Send a real-time control command to the frontend via IPC.

    Accepts JSON: {"cmd": "next|prev|pause|resume|switch_album|screen_off|screen_on"}
    """
    ipc = current_app.config.get("METIXEL_IPC")
    data = request.get_json(silent=True)
    if data is None or "cmd" not in data:
        return jsonify({"error": "Missing 'cmd' in JSON body"}), 400

    cmd = data["cmd"]
    valid_cmds = {
        "next",
        "prev",
        "pause",
        "resume",
        "toggle_pause",
        "switch_album",
        "screen_off",
        "screen_on",
        "show_message",
        "dismiss_message",
        "dismiss_all_messages",
    }
    if cmd not in valid_cmds:
        return jsonify({"error": f"Unknown command: {cmd}. Valid: {sorted(valid_cmds)}"}), 400

    if ipc is not None:
        from metixel.shared.ipc import ControlMessage

        ipc.send(ControlMessage(cmd=cmd, args=data.get("args", {})))
        logger.info("Control command '%s' sent via IPC", cmd)
    else:
        logger.warning("IPC not available — control command '%s' ignored", cmd)

    # Update daemon's display state so the Web UI button reflects reality
    if cmd in ("screen_on", "screen_off"):
        daemon = current_app.config.get("METIXEL_DAEMON")
        if daemon is not None:
            daemon._display_on = cmd == "screen_on"

    return jsonify({"status": "ok", "cmd": cmd})
