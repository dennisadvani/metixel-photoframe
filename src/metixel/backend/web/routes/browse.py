# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Filesystem browsing endpoint for folder selection."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from flask import Blueprint, jsonify, request

from metixel.shared.paths import data_dir

logger = logging.getLogger(__name__)

browse_bp = Blueprint("browse", __name__)


def _resolve_browse_path(requested: str) -> Path | None:
    """Resolve and security-check a ``path`` param against the data dir.

    Absolute paths are used as-is (the dashboard configures the user's own
    system, so any readable location is acceptable).  Relative paths resolve
    against the persistent data directory.  Returns ``None`` when the path
    is not a readable directory (the caller decides the error response).
    """
    base = data_dir()
    requested_path = Path(requested or "")
    if not requested_path.is_absolute():
        requested_path = base / requested_path
    try:
        return requested_path.resolve()
    except (OSError, RuntimeError):
        return None


def _can_create_under(path: Path) -> bool:
    """True if *path* is inside (or is) ``<data dir>/media``.

    Folder creation in the browser modal is restricted to the media tree so
    the UI can never create directories outside the frame's media area.
    """
    media_root = (data_dir() / "media").resolve()
    try:
        path.resolve().relative_to(media_root)
        return True
    except ValueError:
        return False


@browse_bp.route("", methods=["GET"])
def browse_folder():
    """Browse the filesystem for folder selection in the web UI.

    Query params:
        path (str): The directory to browse.  Defaults to the media folder
            (``<data dir>/media``) so the folder browser opens where the
            user's photos/videos live.  Relative paths are resolved against
            the persistent data directory.

    Returns:
        JSON with ``current_path``, ``parent_path``, and ``entries`` —
        a list of subdirectory names (no files, no hidden dirs).
    """

    base = data_dir()
    # Default the browser to the media folder so users start where their
    # photos/videos live, not at the data root.
    default_path = str(base / "media")
    requested = request.args.get("path") or default_path
    resolved = _resolve_browse_path(requested)
    if resolved is None:
        return jsonify({"error": "Invalid path"}), 400

    # Allow browsing anywhere readable — the user is configuring their
    # own system via the dashboard.  Just ensure the path exists.
    if not resolved.exists():
        # The requested path (e.g. a watch folder set in config that isn't on
        # disk yet) doesn't exist.  Fall back to a safe, existing directory so
        # the folder browser still opens instead of erroring out.
        fallback = _safe_fallback(resolved, base)
        if fallback is None:
            return jsonify({"error": f"Path not found: {resolved}"}), 404
        logger.warning("Browse path %s does not exist — falling back to %s", resolved, fallback)
        resolved = fallback
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
            # Persistent data dir — lets the UI display folders relative to it
            # (config values are stored relative to the data dir).
            "base_path": str(base),
            # Whether the browser can create new folders in this directory.
            # Creation is restricted to the media tree (see _can_create_under).
            "can_create": _can_create_under(resolved),
        }
    )


@browse_bp.route("/create", methods=["POST"])
def create_folder():
    """Create a new subdirectory inside the currently browsed folder.

    Request body:
        path (str): The parent directory (absolute, or relative to the
            persistent data dir).
        name (str): The new folder name.  Must be a plain directory name
            (no path separators, ``..``, or hidden-dot prefix); it is
            sanitised before use.

    Creation is restricted to the media tree (``<data dir>/media``) so the
    UI can never create directories elsewhere on the filesystem.

    Returns:
        JSON ``{status: "ok", path: <abs path>, name: <name>}``.
    """
    body = request.get_json(silent=True) or {}
    parent_raw = str(body.get("path") or "").strip()
    name_raw = str(body.get("name") or "").strip()

    if not parent_raw or not name_raw:
        return jsonify({"error": "Both 'path' and 'name' are required"}), 400

    # Validate the name: plain directory name, no separators / traversal /
    # hidden-dot prefix, no reserved "." / "..".
    if (
        name_raw in (".", "..")
        or "/" in name_raw
        or "\\" in name_raw
        or name_raw.startswith(".")
        or name_raw != re.sub(r"[^A-Za-z0-9._ -]", "_", name_raw)
    ):
        return jsonify({"error": f"Invalid folder name: {name_raw}"}), 400

    parent = _resolve_browse_path(parent_raw)
    if parent is None or not parent.exists() or not parent.is_dir():
        return jsonify({"error": "Parent folder not found"}), 404
    if not _can_create_under(parent):
        return jsonify({"error": "New folders can only be created inside the media folder"}), 403

    target = parent / name_raw
    if target.exists():
        return jsonify({"error": f"Folder already exists: {name_raw}"}), 409
    try:
        target.mkdir()
    except OSError as e:
        logger.warning("Failed to create folder %s: %s", target, e)
        return jsonify({"error": f"Cannot create folder: {e}"}), 500

    logger.info("Folder created via web UI: %s", target)
    return jsonify({"status": "ok", "path": str(target), "name": name_raw})


def _safe_fallback(missing: Path, base: Path) -> Path | None:
    """Return a safe, existing directory to browse when *missing* doesn't exist.

    Walks up from the missing path toward the data dir, then the filesystem
    root, returning the first existing directory.  Returns ``None`` only if
    nothing up to the root exists (effectively impossible on a real system).
    """
    # Walk up from the missing path to the data dir, then to the root.
    # Stop when the parent is the same as the current dir (the filesystem
    # root) — on Windows ``Path("C:/").parent`` is ``C:/``, so comparing
    # against ``anchor`` alone would loop forever.
    candidates = [missing]
    current = missing
    while current != current.parent:
        current = current.parent
        candidates.append(current)
    # Prefer the data dir if it exists, then any ancestor, then the root.
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    # Last resort: the filesystem root.
    root = Path(missing.anchor)
    if root.exists() and root.is_dir():
        return root
    return None
