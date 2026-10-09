# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Read-only status and diagnostics endpoints."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from metixel.shared.paths import run_path

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
    """System health endpoint.

    Returns the system metrics the dashboard renders, plus a ``liveness``
    block and an overall ``healthy`` flag.

    By DEFAULT this answers 200 whenever the backend can serve the request,
    because the dashboard (``apiGet``) treats any non-2xx as a hard failure
    and would blank itself, and because a device deliberately run without a
    frontend must not look permanently broken.

    Pass ``?require=render`` to make the endpoint FAIL (503) when the
    frontend is not demonstrably alive.  That is the form the OTA health gate
    uses — a release whose frontend crash-loops must not pass the gate.  The
    query form is used (rather than a separate endpoint or a config flag) so
    the strict contract is opt-in, testable, and extensible
    (``?require=render,network`` later) without breaking existing callers.

    Note the backend cannot be checked here for liveness by construction: a
    200 response already proves the backend answered.  This check is
    specifically about the half the old gate could never see — the frontend.
    """
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    # Include display power state so the Web UI button reflects
    # the actual state (e.g. when the schedule turns it off).
    daemon = current_app.config.get("METIXEL_DAEMON")
    health["display_on"] = getattr(daemon, "_display_on", True) if daemon else True

    # Frontend liveness.  Not an error to be absent: the /api/health contract
    # predates this signal, so a daemon that does not expose a tracker (or a
    # test app built without one) reports "unknown" rather than "unhealthy".
    liveness = _frontend_liveness(daemon)
    health["liveness"] = {"frontend": liveness}

    required = _required_checks()
    # Fail-closed on the strict form: "require render" means a live renderer is
    # required, so anything other than a demonstrated `alive is True` fails —
    # including `None` ("cannot tell").  Do not relax this to `is False`: that
    # would let an unknown state through, which is the exact hole this closes.
    # In production the tracker is built in BackendDaemon.__init__ before the
    # web server starts, so `None` there means a wiring bug, not a healthy
    # device.  The LENIENT default (no query param) still returns 200.
    unhealthy = [name for name in required if name == "render" and liveness["alive"] is not True]
    health["required"] = required
    health["healthy"] = not unhealthy
    health["status"] = "healthy" if not unhealthy else "unhealthy"
    if unhealthy:
        # The reason is surfaced in the body (the OTA script logs the body on
        # failure) so a failed upgrade says WHY, not just that it failed.
        health["unhealthy_checks"] = unhealthy
        return jsonify(health), 503
    return jsonify(health)


def _required_checks() -> list[str]:
    """Return the checks the caller demanded via ``?require=``.

    ``?require=render`` (or ``?require=any``) enables the frontend check;
    anything unrecognised is ignored so a typo cannot silently disable the
    gate it was meant to enable.
    """
    raw = request.args.get("require", "")
    wanted = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if "render" in wanted or "any" in wanted or "all" in wanted:
        return ["render"]
    return []


def _frontend_liveness(daemon: object | None) -> dict[str, Any]:
    """Return the frontend liveness verdict, or a safe "unknown".

    Never raises: liveness is diagnostics on a health endpoint, so a broken
    tracker must degrade to "unknown" rather than take the endpoint down.
    """
    tracker = getattr(daemon, "frontend_liveness", None)
    snapshot = getattr(tracker, "snapshot", None)
    if snapshot is None:
        return {
            "alive": None,
            "state": "unknown",
            "reason": "frontend liveness tracking unavailable",
        }
    try:
        verdict = snapshot()
    except Exception:  # pragma: no cover - defensive, never break /health
        logger.debug("Could not determine frontend liveness", exc_info=True)
        return {
            "alive": None,
            "state": "unknown",
            "reason": "frontend liveness check failed",
        }
    return (
        verdict
        if isinstance(verdict, dict)
        else {
            "alive": None,
            "state": "unknown",
            "reason": "frontend liveness returned an unexpected value",
        }
    )


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
    progress bar (``<run_dir>/processing_status.json``).
    """
    data = _read_json(str(run_path("processing_status.json")))
    if data is None:
        return jsonify({"phase": "unknown", "total": 0, "processed": 0, "current_file": ""})
    return jsonify(data)


def _read_current_media() -> dict | None:
    """Read the current media state file written by the frontend.

    Resolves any thumbnail path into a URL the dashboard can fetch.  The URL
    is only published when the file it points at genuinely exists in one of
    the locations ``/api/media/thumbnail`` serves from — otherwise the
    dashboard renders an ``<img>`` that 404s (and the SPA's error collector
    treats that as a failure).  The frontend clears ``cache/thumbnails/``
    when the cache is reset while ``current_media.json`` still names the old
    hash, so a stale path is a normal, expected state — not an error.
    """
    data = _read_json(str(run_path("current_media.json")))
    if data is None:
        return None

    # Convert thumbnail_path → thumbnail_url (only if the file is servable)
    data["thumbnail_url"] = _resolve_thumbnail_url(data.get("thumbnail_path"))

    return data


def _resolve_thumbnail_url(thumb_path: str | None) -> str | None:
    """Map a ``thumbnail_path`` to a servable URL, or ``None`` if absent.

    ``thumbnail_path`` is either a hash-based thumbnail
    (``<cache>/thumbnails/<hash>.jpg``) or a video frame cache
    (``<cache>/videos/<hash>.<N>.frame.jpg``).  Uses the exact lookup
    ``/api/media/thumbnail`` performs (``find_thumbnail``) so the URL is
    only handed out when the request would succeed.
    """
    if not thumb_path:
        return None

    name = os.path.basename(thumb_path)
    if not name:
        return None

    state = current_app.config.get("METIXEL_STATE")
    if state is not None:
        try:
            from metixel.backend.web.routes.media import find_thumbnail

            if find_thumbnail(state, name) is not None:
                return f"/api/media/thumbnail/{name}"
        except Exception:  # pragma: no cover - defensive, never break /health
            logger.debug("Could not resolve thumbnail location", exc_info=True)

    logger.debug("Current-media thumbnail not found, omitting URL: %s", thumb_path)
    return None


def _read_display_info() -> dict | None:
    """Read the display info status file written by the frontend."""
    return _read_json(str(run_path("display_info.json")))


@health_bp.route("/processing-status", methods=["GET"])
def processing_status():
    """Return per-phase processing progress + journal issues for the dashboard.

    Each phase (``scanning``, ``optimising_images``, ``inspecting_videos``,
    ``transcoding``) tracks its own ``total``/``processed`` independently.
    ``issues`` lists failed/skipped media from the processing journal so the
    UI can show why items are missing from the slideshow.
    """
    data = _read_json(str(run_path("processing_status.json")))
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
