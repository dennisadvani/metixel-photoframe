# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""System log viewer API.

Reads recent log entries from the in-memory ring buffer attached to the
``metixel`` logger. Falls back to reading the last lines of the log file
if the ring buffer is empty (e.g., on a fresh start before any records
have been emitted).
"""

import logging
import os

from flask import Blueprint, current_app, jsonify

logger = logging.getLogger(__name__)

logs_bp = Blueprint("logs", __name__)

# Default log file path used by RotatingFileHandler (see etc/logging.conf)
_DEFAULT_LOG_PATH = "/opt/metixel/logs/metixel.log"


def _read_from_ring_buffer(count: int = 200) -> list[dict]:
    """Read recent log entries from the in-memory ring buffer."""
    root = logging.getLogger("metixel")
    for handler in root.handlers:
        # Import here to avoid circular import at module level
        from metixel.shared.log_buffer import LogRingBuffer  # noqa: PLC0415

        if isinstance(handler, LogRingBuffer):
            return handler.get_recent(count)
    return []


def _tail_file(path: str, lines: int = 200) -> list[str]:
    """Read the last *lines* lines from a text file efficiently.

    Returns an empty list if the file does not exist or cannot be read.
    """
    try:
        if not os.path.isfile(path):
            return []
        # Use a simple approach: read backwards in chunks
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return []

            chunk_size = 4096
            collected: list[str] = []
            remaining = lines

            while remaining > 0 and size > 0:
                read_size = min(chunk_size, size)
                size -= read_size
                f.seek(size)
                chunk = f.read(read_size).decode("utf-8", errors="replace")
                chunk_lines = chunk.splitlines()

                if size == 0:
                    # First chunk from start of file
                    collected = chunk_lines + collected
                else:
                    # Merge partial first line from this chunk with last
                    # partial from previous chunk
                    if collected and chunk_lines:
                        chunk_lines[-1] = chunk_lines[-1] + collected[0]
                        collected = chunk_lines + collected[1:]
                    else:
                        collected = chunk_lines + collected

                # Only keep what we need
                if len(collected) > lines:
                    collected = collected[-lines:]
                    remaining = 0
                else:
                    remaining = lines - len(collected)

            return collected
    except OSError:
        return []


def _find_log_file() -> str | None:
    """Find the active log file path from the root logger's file handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            path = getattr(handler, "baseFilename", None)
            if path and os.path.isfile(path):
                return path

    # Check the metixel logger too
    metixel = logging.getLogger("metixel")
    for handler in metixel.handlers:
        if isinstance(handler, logging.FileHandler):
            path = getattr(handler, "baseFilename", None)
            if path and os.path.isfile(path):
                return path

    # Fall back to default path
    if os.path.isfile(_DEFAULT_LOG_PATH):
        return _DEFAULT_LOG_PATH

    return None


# Sentinel level — above CRITICAL (50); no log record passes this filter.
_NONE_LEVEL = 100


@logs_bp.route("/level", methods=["POST"])
def set_log_level():
    """Change the **file-handler** log level at runtime.

    Accepts JSON: ``{"level": "DEBUG|INFO|WARNING|ERROR|NONE"}``

    Only ``FileHandler`` instances (i.e. the on-disk log file) are
    affected — the in-memory ring buffer stays at ``DEBUG`` so the
    dashboard severity checkboxes can always filter the full stream.
    Console handlers are left unchanged.

    The new level is persisted to ``config.json`` so it survives a
    restart.
    """
    from flask import request

    data = request.get_json(silent=True)
    if data is None or "level" not in data:
        return jsonify({"error": "Missing 'level' in JSON body"}), 400

    level_name = data["level"].upper()
    valid_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "NONE": _NONE_LEVEL,
    }
    if level_name not in valid_levels:
        return jsonify(
            {
                "error": f"Invalid level: {data['level']}",
                "valid": sorted(valid_levels.keys()),
            }
        ), 400

    new_level = valid_levels[level_name]

    # ── 1. Update every FileHandler across all loggers ──────────────────
    #     Ring buffers and console handlers are deliberately skipped.
    updated = 0
    for _logger_name, logger_obj in logging.Logger.manager.loggerDict.items():
        if not isinstance(logger_obj, logging.Logger):
            continue
        for handler in logger_obj.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.setLevel(new_level)
                updated += 1

    # Root logger
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler):
            handler.setLevel(new_level)
            updated += 1

    # ── 2. Persist to config so it survives a restart ──────────────────
    state = current_app.config["METIXEL_STATE"]
    try:
        state.update_config("system", {"log_level": level_name})
    except Exception:
        logger.exception("Failed to persist log level to config")

    logger.warning(
        "File log level changed to %s — %d file handlers updated (ring buffer + console unchanged)",
        level_name,
        updated,
    )
    return jsonify(
        {
            "status": "ok",
            "level": level_name,
            "file_handlers_updated": updated,
        }
    )


@logs_bp.route("/recent", methods=["GET"])
def recent_logs():
    """Get the most recent log entries.

    Query params:
        count (int): Max entries to return (default 200, max 1000).

    Returns:
        JSON: ``{"logs": [...], "total": N}``
    """
    from flask import request

    count = request.args.get("count", 200, type=int)
    count = max(1, min(count, 1000))

    # Prefer the in-memory ring buffer
    entries = _read_from_ring_buffer(count)

    if entries:
        return jsonify({"logs": entries, "total": len(entries)})

    # Fall back to reading the log file
    log_path = _find_log_file()
    if log_path:
        lines = _tail_file(log_path, count)
        return jsonify({"logs": lines, "total": len(lines)})

    return jsonify({"logs": [], "total": 0})
