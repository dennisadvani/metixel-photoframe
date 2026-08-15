# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Message management API endpoints for the web dashboard.

Provides endpoints for dismissing persistent on-screen messages (duration=0
messages that stay until manually cleared) and retrieving the current
persistent message list.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import get_body, jsonify_error
from metixel.shared.ipc import ControlMessage

logger = logging.getLogger(__name__)

messages_bp = Blueprint("messages", __name__)


@messages_bp.route("/messages/persistent", methods=["GET"])
def list_persistent():
    """Return the current list of persistent messages from config."""
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    persistent = config.messages.get("persistent", [])
    return jsonify({"persistent": persistent})


@messages_bp.route("/messages/dismiss", methods=["POST"])
def dismiss_persistent():
    """Dismiss one or all persistent messages.

    Accepts JSON body:

    - ``{"id": "welcome_wifi"}`` — dismiss a specific persistent message
    - ``{"all": true}`` — dismiss all persistent messages

    Removes the message(s) from the config's ``messages.persistent`` list
    (so they won't re-appear on next boot) and sends a ``dismiss_all_messages``
    IPC command to immediately clear them from the screen.
    """
    state = current_app.config["METIXEL_STATE"]
    ipc = current_app.config.get("METIXEL_IPC")

    data = get_body()
    dismiss_all = data.get("all", False)
    target_id = data.get("id", "")

    if not dismiss_all and not target_id:
        return jsonify_error("Must provide 'id' or 'all': true", 400)

    config = state.config
    persistent: list[dict] = config.messages.get("persistent", [])

    if dismiss_all:
        if not persistent:
            return jsonify({"status": "ok", "dismissed": 0, "persistent": []})
        count = len(persistent)
        new_persistent: list[dict] = []
        logger.info("Dismissing all %d persistent message(s)", count)
    else:
        count = 0
        new_persistent = []
        for entry in persistent:
            if entry.get("id") == target_id:
                count += 1
                logger.info("Dismissing persistent message: id=%r", target_id)
            else:
                new_persistent.append(entry)
        if count == 0:
            return jsonify_error(f"No persistent message found with id={target_id!r}", 404)

    # Atomically update config
    state.update_config("messages", {"persistent": new_persistent})

    # Send IPC dismiss to clear the screen immediately
    if ipc is not None:
        try:
            ipc.send(ControlMessage(cmd="dismiss_all_messages"))
        except Exception:
            logger.warning("Failed to send dismiss IPC — messages may still show", exc_info=True)

    return jsonify(
        {
            "status": "ok",
            "dismissed": count,
            "persistent": new_persistent,
        }
    )
