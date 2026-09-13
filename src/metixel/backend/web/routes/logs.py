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

from metixel.shared.paths import data_dir

logger = logging.getLogger(__name__)

logs_bp = Blueprint("logs", __name__)

# Log files written by the pi-run processes.  Each process owns its OWN file —
# they must never share one, because two RotatingFileHandlers on the same path
# race and truncate each other's output.  The Logs page merges these.
_LOG_FILES = ("metixel-backend.log", "metixel-frontend.log")

# Legacy single-file name, still surfaced so an upgraded device's existing
# history remains readable until it rotates away.
_LEGACY_LOG_NAME = "metixel.log"


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


def _log_files() -> list[str]:
    """Return the log files that currently exist, in display order.

    Includes the legacy single-file name so history written before the
    per-process split stays readable.
    """
    log_dir = data_dir() / "logs"
    found: list[str] = []
    for name in (*_LOG_FILES, _LEGACY_LOG_NAME):
        path = log_dir / name
        if os.path.isfile(path):
            found.append(str(path))
    return found


def _find_log_file() -> str | None:
    """Return the primary log file path (the backend's, when present).

    Retained for callers that want a single file; prefer :func:`_log_files`.
    """
    files = _log_files()
    if files:
        return files[0]
    return None


def _tail_files(count: int) -> list[str]:
    """Return the most recent *count* lines merged across ALL process logs.

    Each process writes its own file, so a single-file tail would hide half the
    picture — the whole point of the Logs page is to show what the backend AND
    the frontend are doing.  Lines carry a leading ``YYYY-MM-DD HH:MM:SS``
    timestamp, so the merge sorts on that prefix; anything unparseable falls
    back to its original position rather than being dropped.
    """
    per_file: list[list[str]] = []
    for path in _log_files():
        lines = _tail_file(path, count)
        if lines:
            per_file.append(lines)

    if not per_file:
        return []
    if len(per_file) == 1:
        return per_file[0][-count:]

    # Interleave: tag each line with its file order as a stable tiebreaker so
    # equal timestamps keep a deterministic, grouped output.
    tagged: list[tuple[str, int, int, str]] = []
    for file_idx, lines in enumerate(per_file):
        for line_idx, line in enumerate(lines):
            ts = line[:19]  # "YYYY-MM-DD HH:MM:SS"
            tagged.append((ts, file_idx, line_idx, line))

    tagged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in tagged][-count:]


# Sentinel level — above CRITICAL (50); no log record passes this filter.
_NONE_LEVEL = 100


def _count_file_handlers() -> int:
    """Count FileHandler instances across all loggers (for reporting only)."""
    seen: set[int] = set()
    total = 0

    def _count(logger_obj: logging.Logger) -> None:
        nonlocal total
        for handler in logger_obj.handlers:
            if isinstance(handler, logging.FileHandler) and id(handler) not in seen:
                seen.add(id(handler))
                total += 1

    for obj in logging.Logger.manager.loggerDict.values():
        if isinstance(obj, logging.Logger):
            _count(obj)
    _count(logging.getLogger())
    _count(logging.getLogger("metixel"))
    return total


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
    from metixel.backend.web.helpers import get_body, jsonify_error

    data = get_body()
    if "level" not in data:
        return jsonify_error("Missing 'level' in JSON body", 400)

    level_name = data["level"].upper()
    valid_levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "NONE": _NONE_LEVEL,
    }
    if level_name not in valid_levels:
        return jsonify_error(
            f"Invalid level: {data['level']}",
            400,
            valid=sorted(valid_levels.keys()),
        )

    new_level = valid_levels[level_name]

    # ── 1. Update every FileHandler across all loggers ──────────────────
    #     Reuses the same walk as startup so the runtime control and the
    #     persisted setting cannot apply levels differently.  Ring buffers and
    #     console handlers are deliberately skipped.
    from metixel.__main__ import _apply_file_handler_levels

    _apply_file_handler_levels(new_level)
    updated = _count_file_handlers()

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

    # Fall back to reading the log files (merged across processes)
    lines = _tail_files(count)
    return jsonify({"logs": lines, "total": len(lines)})
