# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Realtime frontend control endpoints (IPC)."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import (
    get_body,
    get_daemon_component,
    jsonify_error,
    require_fields,
)

logger = logging.getLogger(__name__)

control_bp = Blueprint("control", __name__)


@control_bp.route("", methods=["POST"])
def send_control():
    """Send a real-time control command to the frontend via IPC.

    Accepts JSON: {"cmd": "next|prev|pause|resume|switch_album|screen_off|screen_on"}
    """
    ipc = current_app.config.get("METIXEL_IPC")
    data = get_body()
    missing = require_fields(data, "cmd")
    if missing:
        return missing

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
        return jsonify_error(
            f"Unknown command: {cmd}. Valid: {sorted(valid_cmds)}",
            400,
        )

    if cmd in ("screen_on", "screen_off"):
        # Screen-power goes through the daemon choke-point so the flag, the
        # frontend IPC, and the immediate MQTT publish all stay in sync with
        # every other source (scheduler, keyboard/CEC/IR, MQTT commands).
        daemon = get_daemon_component("set_display_power")
        if daemon is not None:
            daemon(cmd == "screen_on", source="web")
        elif ipc is not None:
            from metixel.shared.ipc import ControlMessage

            ipc.send(ControlMessage(cmd=cmd, args=data.get("args", {})))
        else:
            logger.warning("IPC not available — control command '%s' ignored", cmd)
        return jsonify({"status": "ok", "cmd": cmd})

    if ipc is not None:
        from metixel.shared.ipc import ControlMessage

        ipc.send(ControlMessage(cmd=cmd, args=data.get("args", {})))
        logger.info("Control command '%s' sent via IPC", cmd)
    else:
        logger.warning("IPC not available — control command '%s' ignored", cmd)

    return jsonify({"status": "ok", "cmd": cmd})
