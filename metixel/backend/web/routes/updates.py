# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""OTA Update API endpoints.

Provides REST endpoints for the Web UI's "Updates" card under Advanced:
checking for updates, switching channels, applying updates, and
retrieving the current update status.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

updates_bp = Blueprint("updates", __name__)


def _get_update_manager():
    """Return the UpdateManager instance from the Flask app config."""
    mgr = current_app.config.get("METIXEL_UPDATE_MGR")
    if mgr is None:
        raise RuntimeError("UpdateManager not available — is the backend running?")
    return mgr


@updates_bp.route("/status", methods=["GET"])
def get_update_status():
    """Get the full update status.

    Returns current version, channel, available updates per channel,
    last check time, and whether checks/updates are in progress.
    """
    try:
        mgr = _get_update_manager()
        return jsonify(mgr.get_status())
    except Exception as exc:
        logger.exception("Failed to get update status")
        return jsonify({"error": str(exc)}), 500


@updates_bp.route("/check", methods=["POST"])
def trigger_update_check():
    """Trigger an immediate check for available updates.

    The check runs in a background thread so the HTTP response returns
    immediately.  Poll ``GET /api/updates/status`` to see results.
    """
    import threading

    try:
        mgr = _get_update_manager()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    # Run the check in a background thread so the HTTP request doesn't block
    thread = threading.Thread(
        target=mgr.check_for_updates,
        name="update-check-on-demand",
        daemon=True,
    )
    thread.start()

    return jsonify({
        "status": "ok",
        "message": "Update check started — poll /api/updates/status for results",
    })


@updates_bp.route("/apply", methods=["POST"])
def apply_update():
    """Apply an update now.

    Accepts JSON body:
        channel (str, optional): Which channel to update from.
            Defaults to the current configured channel.
        version (str, optional): Specific version tag/SHA to install.
            Defaults to the latest on the channel.

    The update runs synchronously — the HTTP response is sent AFTER
    the update completes and services have been restarted.

    Returns immediately with an error if an update is already in
    progress.
    """
    try:
        mgr = _get_update_manager()
        data = request.get_json(silent=True) or {}
        channel = data.get("channel")
        version = data.get("version")

        result = mgr.apply_update(channel=channel, version=version)
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Update apply failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@updates_bp.route("/channel", methods=["PUT"])
def set_channel():
    """Switch the update channel.

    Accepts JSON body:
        channel (str): One of ``stable``, ``beta``, ``dev``.

    Triggers an automatic check on the new channel after saving.
    """
    try:
        mgr = _get_update_manager()
        data = request.get_json(silent=True)
        if not data or "channel" not in data:
            return jsonify({
                "status": "error",
                "message": "Missing 'channel' in request body. Valid: stable, beta, dev",
            }), 400

        result = mgr.set_channel(data["channel"])
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Channel switch failed")
        return jsonify({"status": "error", "message": str(exc)}), 500
