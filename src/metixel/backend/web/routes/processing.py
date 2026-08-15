# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media processing control endpoints (processing journal retry)."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

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
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return (
            jsonify({"error": "Missing 'path'", "hint": 'Send {"path": "/abs/file.mp4"}'}),
            400,
        )
    try:
        state.journal.retry(path)
    except Exception:
        logger.exception("Retry failed for %s", path)
        return jsonify({"error": "Retry failed"}), 500
    logger.info("[PROCESSING] Retry requested for %s", path)
    return jsonify({"status": "ok"})
