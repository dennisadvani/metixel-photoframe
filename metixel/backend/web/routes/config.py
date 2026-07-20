# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Configuration API endpoints."""

import json
import logging
import os

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

config_bp = Blueprint("config", __name__)


@config_bp.route("", methods=["GET"])
def get_config():
    """Get the full current configuration."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify(state.config.to_dict())


@config_bp.route("/<section>", methods=["GET"])
def get_config_section(section: str):
    """Get a specific config section."""
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    if section not in config.to_dict():
        return jsonify({"error": f"Unknown config section: {section}"}), 404
    return jsonify(config.to_dict()[section])


@config_bp.route("/<section>", methods=["PUT"])
def update_config_section(section: str):
    """Update a config section. Triggers hot reload in the frontend."""
    state = current_app.config["METIXEL_STATE"]
    data = request.get_json(silent=True)
    if data is None:
        logger.warning("PUT /%s: invalid or missing JSON body (Content-Type: %s)",
                       section, request.content_type)
        return jsonify({"error": "Invalid JSON body", "hint": "Send JSON with Content-Type: application/json"}), 400

    try:
        logger.info("PUT /%s: updating with keys=%s", section, list(data.keys()))
        state.update_config(section, data)
        logger.info("Config section '%s' updated via API — saved to %s", section, state.config_path)
        return jsonify({
            "status": "ok",
            "section": section,
            "config_path": str(state.config_path),
        })
    except KeyError:
        return jsonify({
            "error": f"Unknown config section: {section}",
            "valid_sections": list(state.config.to_dict().keys()),
        }), 404
    except Exception as e:
        logger.exception("Failed to update config section '%s'", section)
        return jsonify({
            "error": str(e),
            "hint": "Check server logs for details",
        }), 500


@config_bp.route("/reload", methods=["POST"])
def reload_config():
    """Reload configuration from disk."""
    state = current_app.config["METIXEL_STATE"]
    state.reload_config()
    return jsonify({"status": "ok"})


@config_bp.route("/control", methods=["POST"])
def send_control():
    """Send a real-time control command to the frontend via IPC.

    Accepts JSON: {"cmd": "next|prev|pause|resume|switch_album|power_off|power_on"}
    """
    ipc = current_app.config.get("METIXEL_IPC")
    data = request.get_json(silent=True)
    if data is None or "cmd" not in data:
        return jsonify({"error": "Missing 'cmd' in JSON body"}), 400

    cmd = data["cmd"]
    valid_cmds = {"next", "prev", "pause", "resume", "switch_album", "power_off", "power_on"}
    if cmd not in valid_cmds:
        return jsonify({"error": f"Unknown command: {cmd}. Valid: {sorted(valid_cmds)}"}), 400

    if ipc is not None:
        from metixel.shared.ipc import ControlMessage
        ipc.send(ControlMessage(cmd=cmd, args=data.get("args", {})))
        logger.info("Control command '%s' sent via IPC", cmd)
    else:
        logger.warning("IPC not available — control command '%s' ignored", cmd)

    return jsonify({"status": "ok", "cmd": cmd})


@config_bp.route("/health", methods=["GET"])
def health_check():
    """System health endpoint."""
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    return jsonify(health)


@config_bp.route("/path", methods=["GET"])
def get_config_path():
    """Return the config file path (for debugging)."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify({
        "config_path": str(state.config_path),
        "exists": state.config_path.exists(),
    })


@config_bp.route("/display/info", methods=["GET"])
def get_display_info():
    """Return the current display resolution detected by the frontend."""
    info = _read_display_info()
    if info is None:
        state = current_app.config["METIXEL_STATE"]
        dc = state.config.display
        info = {
            "width": dc.get("width", 0),
            "height": dc.get("height", 0),
            "backend": "unknown",
            "stale": True,
        }
    return jsonify(info)


@config_bp.route("/processing", methods=["GET"])
def get_processing_status():
    """Return the current background processing status.

    Reads from the same file the frontend uses for its splash screen
    progress bar (``/run/metixel/processing_status.json``).
    """
    import os

    path = "/run/metixel/processing_status.json"
    try:
        if os.path.isfile(path):
            import json as _json
            with open(path, "r") as f:
                data = _json.load(f)
            return jsonify(data)
    except (OSError, ValueError):
        pass
    return jsonify({"phase": "unknown", "total": 0, "processed": 0, "current_file": ""})


def _read_current_media() -> dict | None:
    """Read the current media state file written by the frontend.

    Resolves any thumbnail path into a URL the dashboard can fetch.
    """
    import os

    path = "/run/metixel/current_media.json"
    try:
        if os.path.isfile(path):
            import json

            with open(path, "r") as f:
                data = json.load(f)

            # Convert thumbnail_path → thumbnail_url
            thumb_path = data.get("thumbnail_path")
            if thumb_path:
                # The path could be a thumbnail hash (cache/thumbnails/<hash>.jpg)
                # or a video frame cache (<video>.<N>.frame).
                fname = os.path.basename(thumb_path)
                data["thumbnail_url"] = f"/api/media/thumbnail/{fname}"
            else:
                data["thumbnail_url"] = None

            return data
    except (OSError, ValueError):
        pass
    return None


def _read_display_info() -> dict | None:
    """Read the display info status file written by the frontend."""
    try:
        info_path = "/run/metixel/display_info.json"
        if os.path.isfile(info_path):
            with open(info_path, "r") as f:
                return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None
