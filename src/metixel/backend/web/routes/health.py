# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Read-only status and diagnostics endpoints."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from flask import Blueprint, current_app, jsonify

logger = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


def _read_json(path: str) -> dict | None:
    """Read a JSON status file written by the frontend.

    Returns ``None`` if the file is missing, unreadable, or malformed.
    """
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        pass
    return None


@health_bp.route("", methods=["GET"])
def health_check():
    """System health endpoint."""
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    # Include display power state so the Web UI button reflects
    # the actual state (e.g. when the schedule turns it off).
    daemon = current_app.config.get("METIXEL_DAEMON")
    health["display_on"] = getattr(daemon, "_display_on", True) if daemon else True
    return jsonify(health)


@health_bp.route("/display/info", methods=["GET"])
def get_display_info():
    """Return the current display resolution detected by the frontend."""
    info = _read_display_info()
    if info is None:
        state = current_app.config["METIXEL_STATE"]
        dc = state.config.display
        info = {
            "width": dc.get("width", 0),
            "height": dc.get("height", 0),
            "backend": "unknown",
            "stale": True,
        }
    return jsonify(info)


@health_bp.route("/display/modes", methods=["GET"])
def get_display_modes():
    """Return the display modes the monitor and Pi mutually support.

    The frontend (which has Wayland access) enumerates the real monitor's
    modes via wlr-randr and writes them to ``display_info.json``.  This
    endpoint reads that file first.  If it's absent (e.g. frontend not yet
    started), falls back to querying wlr-randr directly, then to a static
    list of common HDMI resolutions.  The web UI uses this to populate the
    resolution dropdown when auto-detect is disabled.
    """
    info = _read_display_info()
    modes = (info or {}).get("modes") or []
    if modes:
        return jsonify({"modes": _dedupe_modes(modes), "source": "monitor"})

    # Fallback: query wlr-randr directly (works when the backend is not
    # sandboxed away from the Wayland socket).
    from metixel.display.hardware import WlrOutput

    modes = WlrOutput().list_modes()
    if modes:
        return jsonify({"modes": _dedupe_modes(modes), "source": "monitor"})

    # Final fallback: common HDMI resolutions supported by RPi 2–5.
    fallback = [
        {"width": 1920, "height": 1080, "refresh": 60, "label": "1920 × 1080 (1080p)"},
        {"width": 1280, "height": 720, "refresh": 60, "label": "1280 × 720 (720p)"},
        {"width": 1024, "height": 768, "refresh": 60, "label": "1024 × 768 (XGA)"},
        {"width": 800, "height": 600, "refresh": 60, "label": "800 × 600 (SVGA)"},
        {"width": 640, "height": 480, "refresh": 60, "label": "640 × 480 (VGA)"},
    ]
    return jsonify({"modes": fallback, "source": "fallback"})


def _dedupe_modes(modes: list) -> list[dict]:
    """Deduplicate modes by (width, height), keeping the highest refresh."""
    by_res: dict[tuple[int, int], dict] = {}
    for m in modes:
        key = (int(m.get("width", 0)), int(m.get("height", 0)))
        if key[0] <= 0 or key[1] <= 0:
            continue
        refresh = int(round(float(m.get("refresh", 0))))
        existing = by_res.get(key)
        if existing is None or refresh > existing.get("refresh", 0):
            by_res[key] = {
                "width": key[0],
                "height": key[1],
                "refresh": refresh,
                "preferred": bool(m.get("preferred")),
                "current": bool(m.get("current")),
            }
    return sorted(by_res.values(), key=lambda r: (-r["width"], -r["height"]))


@health_bp.route("/processing", methods=["GET"])
def get_processing_status():
    """Return the current background processing status.

    Reads from the same file the frontend uses for its splash screen
    progress bar (``/run/metixel/processing_status.json``).
    """
    data = _read_json("/run/metixel/processing_status.json")
    if data is None:
        return jsonify({"phase": "unknown", "total": 0, "processed": 0, "current_file": ""})
    return jsonify(data)


def _read_current_media() -> dict | None:
    """Read the current media state file written by the frontend.

    Resolves any thumbnail path into a URL the dashboard can fetch.
    """
    data = _read_json("/run/metixel/current_media.json")
    if data is None:
        return None

    # Convert thumbnail_path → thumbnail_url
    thumb_path = data.get("thumbnail_path")
    if thumb_path:
        # The path could be a thumbnail hash (cache/thumbnails/<hash>.jpg)
        # or a video frame cache (<video>.<N>.frame).
        fname = os.path.basename(thumb_path)
        data["thumbnail_url"] = f"/api/media/thumbnail/{fname}"
    else:
        data["thumbnail_url"] = None

    return data


def _read_display_info() -> dict | None:
    """Read the display info status file written by the frontend."""
    return _read_json("/run/metixel/display_info.json")


@health_bp.route("/processing-status", methods=["GET"])
def processing_status():
    """Return per-phase processing progress + journal issues for the dashboard.

    Each phase (``scanning``, ``optimising_images``, ``inspecting_videos``,
    ``transcoding``) tracks its own ``total``/``processed`` independently.
    ``issues`` lists failed/skipped media from the processing journal so the
    UI can show why items are missing from the slideshow.
    """
    data = _read_json("/run/metixel/processing_status.json")
    if data is None:
        data = {}

    state = current_app.config["METIXEL_STATE"]
    try:
        journal = state.journal
        issues = journal.issues()
        # Convert epoch seconds → ISO 8601 so the dashboard's timeAgo()
        # (Date.parse) can render a relative timestamp.
        for issue in issues:
            ts = issue.get("updated_at")
            if ts:
                issue["updated_at"] = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        data["issues"] = issues
        data["journal_stats"] = journal.stats()
    except Exception:
        logger.debug("Could not read processing journal issues", exc_info=True)
        data["issues"] = []
        data["journal_stats"] = {}

    data.setdefault("active", None)
    data.setdefault("phases", {})
    return jsonify(data)
