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

    Query params:
        force (bool): If ``true``, bypass the 5-minute cache and
            re-fetch from GitHub.  Use for manual checks.

    The check runs in a background thread so the HTTP response returns
    immediately.  Poll ``GET /api/updates/status`` to see results.
    """
    try:
        mgr = _get_update_manager()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    force = request.args.get("force", "false").lower() in ("true", "1", "yes")

    # Bounded background check — the manager coalesces rapid triggers so we
    # never spawn an unbounded number of check threads.
    mgr.check_for_updates_async(force=force)

    return jsonify(
        {
            "status": "ok",
            "message": "Update check started — poll /api/updates/status for results",
        }
    )


@updates_bp.route("/apply", methods=["POST"])
def apply_update():
    """Apply an update now.

    Accepts JSON body:
        channel (str, optional): Which channel to update from.
            Defaults to the current configured channel.
        version (str, optional): Specific version tag/SHA to install.
            Defaults to the latest on the channel.
        keep_existing (bool, optional): If the target release already exists
            locally, keep it (skip the delete-before-reinstall step).  The
            caller should have confirmed with the user.

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
        keep_existing = bool(data.get("keep_existing", False))

        result = mgr.apply_update(channel=channel, version=version, keep_existing=keep_existing)
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Update apply failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@updates_bp.route("/releases", methods=["GET"])
def list_releases():
    """List GitHub releases available for manual install.

    Returns the cached release list (atomic-era releases >= 1.2.3).  If the
    list isn't cached yet, triggers a background check and returns an empty
    list — the UI should poll ``GET /api/updates/status`` to refresh.
    """
    try:
        mgr = _get_update_manager()
        return jsonify({"status": "ok", "releases": mgr.list_releases()})
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Failed to list releases")
        return jsonify({"status": "error", "message": str(exc)}), 500


@updates_bp.route("/rollback", methods=["POST"])
def rollback():
    """Roll back the live symlink to a previously installed release.

    Accepts JSON body:
        version (str): The release version to roll back to (must already
            exist locally under ``releases/``).

    Flips the live symlink and restarts services.  No download/install.
    """
    try:
        mgr = _get_update_manager()
        data = request.get_json(silent=True) or {}
        version = data.get("version")
        if not version:
            return jsonify({"status": "error", "message": "Missing 'version' in request body"}), 400

        result = mgr.rollback(version)
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Rollback failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@updates_bp.route("/apt-upgrade", methods=["POST"])
def apt_upgrade():
    """Run a full OS ``apt update && apt upgrade`` and reboot afterwards.

    Runs in a detached background thread (the reboot kills this process).
    Returns immediately with ``{"status": "ok"}``.
    """
    try:
        mgr = _get_update_manager()
        result = mgr.apt_upgrade()
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("apt upgrade failed")
        return jsonify({"status": "error", "message": str(exc)}), 500


@updates_bp.route("/auto-update", methods=["PUT"])
def set_auto_update():
    """Configure the weekly auto-update schedule.

    Accepts JSON body (any subset):
        enabled (bool): Turn auto-update on/off.
        day (int): Day of week (0=Monday … 6=Sunday).
        time (str): ``HH:MM`` (any time of day).
    """
    try:
        mgr = _get_update_manager()
        data = request.get_json(silent=True) or {}
        result = mgr.set_auto_update(
            enabled=data.get("enabled"),
            day=data.get("day"),
            time_str=data.get("time"),
        )
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Auto-update config failed")
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
            return jsonify(
                {
                    "status": "error",
                    "message": "Missing 'channel' in request body. Valid: stable, beta, dev",
                }
            ), 400

        result = mgr.set_channel(data["channel"])
        status_code = 200 if result.get("status") == "ok" else 400
        return jsonify(result), status_code
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    except Exception as exc:
        logger.exception("Channel switch failed")
        return jsonify({"status": "error", "message": str(exc)}), 500
