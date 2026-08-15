# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media processing control endpoints (processing journal retry/delete)."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import get_body, jsonify_error

logger = logging.getLogger(__name__)

processing_bp = Blueprint("processing", __name__)


@processing_bp.route("/retry", methods=["POST"])
def retry_media():
    """Forget a failed/skipped journal entry so the next scan re-processes it.

    Body: ``{"path": "<resolved file path>"}`` — the path as returned by
    ``/api/health/processing-status`` ``issues[].path``.

    The folder watcher re-discovers the file on its next scan cycle and
    re-runs it through the optimisation pipeline.
    """
    state = current_app.config["METIXEL_STATE"]
    data = get_body()
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify_error(
            "Missing 'path'",
            400,
            hint='Send {"path": "/abs/file.mp4"}',
        )
    try:
        state.journal.retry(path)
    except Exception:
        logger.exception("Retry failed for %s", path)
        return jsonify_error("Retry failed", 500)
    logger.info("[PROCESSING] Retry requested for %s", path)
    return jsonify({"status": "ok"})


@processing_bp.route("/delete", methods=["POST"])
def delete_media():
    """Delete a failed/skipped media file and drop its journal entry.

    Body: ``{"path": "<resolved file path>"}`` — the path as returned by
    ``/api/health/processing-status`` ``issues[].path``.

    Safety: only files inside a configured watch folder can be deleted.
    The file is removed from disk, the journal entry is dropped, and any
    matching playlist item is removed so it never appears again.
    """
    state = current_app.config["METIXEL_STATE"]
    data = get_body()
    path_str = (data.get("path") or "").strip()
    if not path_str:
        return jsonify_error(
            "Missing 'path'",
            400,
            hint='Send {"path": "/abs/file.jpg"}',
        )

    target = Path(path_str)
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    # Safety: only allow deleting inside a configured watch path
    from metixel.shared.config import resolve_watch_paths

    watch_paths = resolve_watch_paths(state.config)
    if not any(resolved.is_relative_to(wp.resolve()) for wp in watch_paths):
        logger.warning("Refusing to delete file outside watch paths: %s", path_str)
        return jsonify_error("Path is not inside a watch folder", 400)

    # Remove matching playlist items (by resolved original path)
    ids = set()
    for item in state.get_playlist():
        try:
            if item.original_path.resolve() == resolved:
                ids.add(item.id)
        except OSError:
            pass
    if ids:
        state.remove_playlist_items(ids)

    # Drop the journal entry so it stops showing as an issue
    state.journal.remove(resolved)

    # Delete the source file
    deleted = False
    if target.is_file():
        try:
            target.unlink()
            deleted = True
        except OSError:
            logger.warning("Could not delete media file: %s", target)
            return jsonify({"error": "Could not delete file"}), 500

    logger.info("[PROCESSING] Deleted media file: %s", resolved)
    return jsonify({"status": "ok", "deleted": deleted})
