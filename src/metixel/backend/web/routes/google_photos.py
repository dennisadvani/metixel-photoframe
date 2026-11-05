# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Google Photos sync API endpoints for the web dashboard.

Mirrors the Immich routes — status, manual sync, cancel, connection test — for
the one public shared album the frame mirrors (see
:mod:`metixel.backend.sync.google_photos`).
"""

from __future__ import annotations

import json as _json
import logging
import threading
from typing import TYPE_CHECKING, Any

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import get_body, jsonify_error

if TYPE_CHECKING:
    from metixel.backend.sync.google_photos import GooglePhotosSyncer

logger = logging.getLogger(__name__)

google_photos_bp = Blueprint("google_photos", __name__)


# ── Status ──────────────────────────────────────────────────────────────────


@google_photos_bp.route("/status", methods=["GET"])
def sync_status():
    """Return the last sync result and live progress."""
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    result = syncer.get_last_result()
    progress = _read_progress()

    payload: dict[str, Any] = {
        "status": "ok" if result is not None else "never_run",
        "configured": syncer.is_configured(),
        "last_sync": result.to_dict() if result is not None else None,
        "progress": progress,
    }
    return jsonify(payload)


# ── Sync ────────────────────────────────────────────────────────────────────


@google_photos_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """Sync the shared album in the background.

    Returns quickly; progress and the outcome are polled from
    ``GET /api/google-photos/status``.
    """
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    if not syncer.is_configured():
        return jsonify_error(
            "No Google Photos shared album link is set",
            400,
            hint="Paste the album's share link, then test the connection",
        )

    def _run():
        try:
            result = syncer.sync_once()
            logger.info("Manual Google Photos sync finished: %s", result.to_dict())
        except Exception:
            logger.exception("Manual Google Photos sync failed")

    thread = threading.Thread(target=_run, name="google-photos-manual-sync", daemon=True)
    thread.start()

    return jsonify(
        {
            "status": "started",
            "message": (
                "Sync started in background — check GET /api/google-photos/status for results"
            ),
        }
    )


@google_photos_bp.route("/cancel", methods=["POST"])
def cancel_sync():
    """Cancel the running sync (it finishes the current file, then aborts)."""
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    syncer.cancel()
    return jsonify({"status": "ok", "message": "Cancellation requested"})


@google_photos_bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Check that a shared-album link can be read.

    Body (JSON, optional — the configured link is used when omitted)::

        {"shared_album_url": "https://photos.app.goo.gl/..."}

    As with the Immich probe the HTTP status stays 200 once the body is valid:
    upstream failures come back as ``{"ok": false, "error": ...}`` so the
    dashboard never mistakes Google's errors for its own backend failing.
    """
    from metixel.backend.sync import google_photos as gp  # noqa: PLC0415

    state = current_app.config["METIXEL_STATE"]
    data = get_body()
    if "shared_album_url" in data and not isinstance(data["shared_album_url"], str):
        return jsonify_error("'shared_album_url' must be a string", 400)

    cfg = state.config.sync.get("google_photos") or {}
    url = str(data.get("shared_album_url", cfg.get("shared_album_url") or ""))
    return jsonify(gp.probe_shared_album(url))


# ── Module-level syncer cache ───────────────────────────────────────────────

_syncer_cache: GooglePhotosSyncer | None = None


def _get_or_create_syncer(state) -> GooglePhotosSyncer:
    """Return the :class:`GooglePhotosSyncer` the routes should drive.

    That is the daemon's own instance whenever there is one: a second,
    route-private instance would have its own ``_syncing`` flag and cancel
    flag, so Sync Now could overlap a scheduled cycle on the same folder and
    Cancel would never reach it.  A private cached instance is only used when
    no daemon is attached (tests, tooling).

    Config is refreshed on every call so a changed album link is picked up
    without a backend restart (the syncer skips the reload mid-sync).
    """
    from metixel.backend.sync.google_photos import GooglePhotosSyncer  # noqa: PLC0415

    daemon = current_app.config.get("METIXEL_DAEMON")
    shared = getattr(daemon, "google_photos_syncer", None)
    if isinstance(shared, GooglePhotosSyncer):
        shared._reload_config()  # noqa: SLF001 — pick up hot-reloaded config
        return shared

    global _syncer_cache  # noqa: PLW0603
    if _syncer_cache is None:
        _syncer_cache = GooglePhotosSyncer(state)
    else:
        _syncer_cache._reload_config()  # noqa: SLF001 — pick up hot-reloaded config
    return _syncer_cache


def _read_progress() -> dict | None:
    """Read the live sync progress file, if it exists."""
    from metixel.backend.sync.google_photos import _sync_progress_path  # noqa: PLC0415

    try:
        path = _sync_progress_path()
        if path.is_file():
            with open(path) as f:
                return _json.load(f)  # type: ignore[no-any-return]
    except (OSError, ValueError):
        pass
    return None
