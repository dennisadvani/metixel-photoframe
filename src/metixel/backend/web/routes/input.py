# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Keyboard and input configuration endpoints."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

input_bp = Blueprint("input", __name__)


@input_bp.route("/input/keyboard/map", methods=["GET"])
def keyboard_map():
    """Get the current keyboard key mapping."""
    state = current_app.config["METIXEL_STATE"]
    input_cfg = state.config.input
    stored = input_cfg.get("keyboard_map", {}) or {}

    # Include default key names for display
    result: dict[str, list[dict]] = {}
    for cmd, codes in stored.items():
        result[cmd] = []
        for code in codes:
            name = _key_name(code)
            result[cmd].append({"code": code, "name": name})
    return jsonify({"map": result})


@input_bp.route("/input/keyboard/learn", methods=["POST"])
def keyboard_learn():
    """Start or check keyboard learn mode.

    POST {"cmd": "start", "target": "pause"} — begin learning.
    POST {"cmd": "check"} — poll for result.
    POST {"cmd": "cancel"} — cancel learning.
    """
    data = request.get_json(silent=True) or {}
    action = data.get("cmd", "")

    daemon = current_app.config.get("METIXEL_DAEMON")
    if not daemon:
        return jsonify({"error": "Daemon not available"}), 503

    handler = getattr(daemon, "_keyboard_handler", None)
    if not handler:
        return jsonify({"error": "Keyboard handler not running"}), 503

    if action == "start":
        target = data.get("target", "")
        try:
            handler.start_learn(target)
            return jsonify({"status": "learning", "target": target})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    elif action == "check":
        result = handler.get_learn_result()
        if result:
            code, name = result
            # Persist the mapping
            state = current_app.config["METIXEL_STATE"]
            stored = dict(state.config.input.get("keyboard_map", {}) or {})
            target = handler._learn_target  # The command being mapped
            codes = list(stored[target]) if target and target in stored else []
            if code not in codes:
                codes.append(code)
            if target:
                stored[target] = codes
            state.update_config("input", {"keyboard_map": stored})
            handler.set_key_map(stored)
            return jsonify({"status": "learned", "code": code, "name": name, "command": target})
        return jsonify({"status": "waiting"})

    elif action == "cancel":
        handler.cancel_learn()
        return jsonify({"status": "cancelled"})

    elif action == "clear":
        target = data.get("target", "")
        if not target:
            return jsonify({"error": "Missing target command"}), 400
        state = current_app.config["METIXEL_STATE"]
        stored = dict(state.config.input.get("keyboard_map", {}) or {})
        # Remove the command from config and set it to empty list
        # so the handler knows to clear all keys for this command
        # (including any defaults that were overridden).
        stored[target] = []
        state.update_config("input", {"keyboard_map": stored})
        handler.set_key_map(stored)
        return jsonify({"status": "cleared", "command": target})

    return jsonify({"error": "Unknown action"}), 400


def _key_name(code: int) -> str:
    """Get a human-readable name for a Linux key code."""
    try:
        import evdev

        return str(evdev.ecodes.KEY.get(code, f"Key {code}"))
    except ImportError:
        return f"Key {code}"
