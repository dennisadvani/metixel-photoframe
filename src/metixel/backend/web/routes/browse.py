# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Filesystem browsing endpoint for folder selection."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

from metixel.shared.paths import install_root

logger = logging.getLogger(__name__)

browse_bp = Blueprint("browse", __name__)


@browse_bp.route("", methods=["GET"])
def browse_folder():
    """Browse the filesystem for folder selection in the web UI.

    Query params:
        path (str): The directory to browse.  Defaults to the media folder
            (``<install root>/media``) so the folder browser opens where the
            user's photos/videos live.  Relative paths are resolved against
            the install root.

    Returns:
        JSON with ``current_path``, ``parent_path``, and ``entries`` —
        a list of subdirectory names (no files, no hidden dirs).
    """

    base = install_root()
    # Default the browser to the media folder so users start where their
    # photos/videos live, not at the install root.
    default_path = str(base / "media")
    requested = request.args.get("path") or default_path
    requested_path = Path(requested)

    if not requested_path.is_absolute():
        requested_path = base / requested_path

    # Resolve and security-check: don't allow escaping the base path
    try:
        resolved = requested_path.resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "Invalid path"}), 400

    # Allow browsing anywhere readable — the user is configuring their
    # own system via the dashboard.  Just ensure the path exists.
    if not resolved.exists():
        return jsonify({"error": f"Path not found: {resolved}"}), 404
    if not resolved.is_dir():
        return jsonify({"error": f"Not a directory: {resolved}"}), 400

    # List subdirectories (no files, no hidden dirs)
    entries = []  # type: list
    try:
        for entry in sorted(resolved.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            entries.append(
                {
                    "name": entry.name + "/",
                    "path": str(entry),
                }
            )
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": str(resolved)}), 403
    except OSError as e:
        return jsonify({"error": str(e), "path": str(resolved)}), 500

    parent = str(resolved.parent) if resolved != resolved.anchor else None
    return jsonify(
        {
            "current_path": str(resolved),
            "parent_path": parent,
            "entries": entries,
        }
    )
