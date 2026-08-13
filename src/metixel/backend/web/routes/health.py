# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Read-only status and diagnostics endpoints."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Blueprint, current_app, jsonify

logger = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health_check():
    """System health endpoint."""
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    # Include display power state so the Web UI button reflects
    # the actual state (e.g. when the schedule turns it off).
    daemon = current_app.config.get("METIXEL_DAEMON")
    health["display_on"] = getattr(daemon, "_display_on", True) if daemon else True
    return jsonify(health)


@health_bp.route("/display/info", methods=["GET"])
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


@health_bp.route("/processing", methods=["GET"])
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

            with open(path) as f:
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

            with open(path) as f:
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
            with open(info_path) as f:
                return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


@health_bp.route("/processing-status", methods=["GET"])
def processing_status():
    """Return per-phase processing progress for the dashboard.

    Each phase (``scanning``, ``optimising_images``, ``transcoding``)
    tracks its own ``total``/``processed`` independently.  The web UI
    renders a separate progress bar for each phase so the user can see
    all queue states at once, without flickering between them.
    """
    try:
        path = Path("/run/metixel/processing_status.json")
        if not path.exists():
            return jsonify({"active": None, "phases": {}})
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"active": None, "phases": {}})
