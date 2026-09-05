# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Keyboard and input configuration endpoints."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from metixel.backend.input_handlers.keyboard import DEFAULT_KEY_MAP
from metixel.backend.web.helpers import get_body, get_daemon_component, jsonify_error

logger = logging.getLogger(__name__)

input_bp = Blueprint("input", __name__)


@input_bp.route("/keyboard/map", methods=["GET"])
def keyboard_map():
    """Get the current keyboard key mapping.

    The live ``KeyboardHandler`` holds the *effective* map (code defaults
    overlaid with any stored ``config.input.keyboard_map`` overrides), so the
    UI here always reflects exactly what the handler uses.  If the keyboard
    handler has not been started, fall back to merging ``DEFAULT_KEY_MAP`` with
    the stored config so the table is still populated.
    """
    handler = get_daemon_component("_keyboard_handler")
    if handler is not None and hasattr(handler, "key_map"):
        # {cmd: [codes]} — already merges defaults + stored overrides.
        cmd_map = handler.key_map
    else:
        cmd_map = _merge_key_map(
            DEFAULT_KEY_MAP,
            current_app.config["METIXEL_STATE"].config.input.get("keyboard_map", {}) or {},
        )

    # Include default key names for display
    result: dict[str, list[dict]] = {}
    for cmd, codes in cmd_map.items():
        result[cmd] = []
        for code in codes:
            name = _key_name(code)
            result[cmd].append({"code": code, "name": name})
    return jsonify({"map": result})


def _merge_key_map(defaults: dict[int, str], stored: dict) -> dict[str, list[int]]:
    """Return {cmd: [codes]} = ``defaults`` overlaid with ``stored`` overrides.

    Mirrors ``KeyboardHandler``: an empty list for a command means the user
    explicitly cleared it (removing the defaults too).
    """
    effective: dict[str, list[int]] = {}
    for code, cmd in defaults.items():
        effective.setdefault(cmd, []).append(code)
    for cmd, codes in stored.items():
        if codes:
            effective[cmd] = list(codes)
        else:
            # Explicit clear → blank this command (defaults included).
            effective[cmd] = []
    return effective


@input_bp.route("/keyboard/learn", methods=["POST"])
def keyboard_learn():
    """Start or check keyboard learn mode.

    POST {"cmd": "start", "target": "pause"} — begin learning.
    POST {"cmd": "check"} — poll for result.
    POST {"cmd": "cancel"} — cancel learning.
    """
    data = get_body()
    action = data.get("cmd", "")

    handler = get_daemon_component("_keyboard_handler")
    if not handler:
        return jsonify_error("Keyboard handler not running", 503)

    if action == "start":
        target = data.get("target", "")
        try:
            handler.start_learn(target)
            return jsonify({"status": "learning", "target": target})
        except ValueError as e:
            return jsonify_error(str(e), 400)

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
            return jsonify_error("Missing target command", 400)
        state = current_app.config["METIXEL_STATE"]
        stored = dict(state.config.input.get("keyboard_map", {}) or {})
        # Remove the command from config and set it to empty list
        # so the handler knows to clear all keys for this command
        # (including any defaults that were overridden).
        stored[target] = []
        state.update_config("input", {"keyboard_map": stored})
        handler.set_key_map(stored)
        return jsonify({"status": "cleared", "command": target})

    return jsonify_error("Unknown action", 400)


def _key_name(code: int) -> str:
    """Get a human-readable name for a Linux key code."""
    try:
        import evdev

        return str(evdev.ecodes.KEY.get(code, f"Key {code}"))
    except ImportError:
        return f"Key {code}"
