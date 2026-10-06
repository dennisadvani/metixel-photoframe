# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Google Photos sync API endpoints for the web dashboard.

Mirrors the Immich routes — status, manual sync, cancel, connection test —
with the album picker replaced by the picking-session endpoints the Photos
Picker API requires (Google retired library-wide read access in March 2025,
so the user picks in Google's own UI and the frame downloads the result).
"""

from __future__ import annotations

import json as _json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from flask import Blueprint, current_app, jsonify

from metixel.backend.sync.google_photos import MODE_PICKER, MODE_SHARED_URL
from metixel.backend.web.helpers import get_body, jsonify_error
from metixel.shared.paths import run_path

if TYPE_CHECKING:
    from metixel.backend.sync.google_photos import GooglePhotosSyncer

logger = logging.getLogger(__name__)

google_photos_bp = Blueprint("google_photos", __name__)


# ── Status ──────────────────────────────────────────────────────────────────


@google_photos_bp.route("/status", methods=["GET"])
def sync_status():
    """Return the last sync result, live progress and the picking session."""
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    result = syncer.get_last_result()
    progress = _read_progress()
    session = _session_payload(syncer.read_session())

    payload: dict[str, Any] = {
        "status": "ok" if result is not None else "never_run",
        "mode": syncer.mode,
        "configured": syncer.is_configured(),
        "last_sync": result.to_dict() if result is not None else None,
        "progress": progress,
        "session": session,
    }
    return jsonify(payload)


# ── Picking sessions ────────────────────────────────────────────────────────


@google_photos_bp.route("/session", methods=["POST"])
def start_session():
    """Open a Google Photos picking session.

    The response carries the ``picker_uri`` the user must open (on any device
    signed in to their Google account) to choose photos or albums.
    """
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    if syncer.mode == MODE_SHARED_URL:
        return jsonify_error(
            "Picking is only used in Picker mode",
            409,
            hint="Switch the mode to Picker, or just Sync Now to read the shared album",
        )
    if not syncer.is_configured():
        return jsonify_error(
            "Google Photos is not connected",
            400,
            hint="Add a client ID, client secret and refresh token, then test the connection",
        )

    try:
        session = syncer.create_session()
    except Exception as e:
        logger.exception("Could not start a Google Photos picking session")
        return jsonify_error(
            str(e),
            502,
            hint="Check the credentials, the network, and that the Photos Picker API is enabled",
        )

    return jsonify({"status": "ok", "session": _session_payload(session)})


@google_photos_bp.route("/session", methods=["GET"])
def get_session():
    """Return the current picking session, refreshed from Google.

    ``session`` is ``null`` when there is no session; otherwise
    ``media_items_set`` tells the dashboard the user has finished picking and
    an import can run.
    """
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    try:
        session = syncer.refresh_session()
    except Exception as e:  # noqa: BLE001 — a poll must not 500 the dashboard
        logger.warning("Could not refresh the Google Photos picking session: %s", e)
        session = syncer.read_session()

    return jsonify({"status": "ok", "session": _session_payload(session)})


@google_photos_bp.route("/session/clear", methods=["POST"])
def clear_session():
    """Forget the current picking session (and delete it at Google)."""
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    cleared = syncer.clear_session()
    return jsonify({"status": "ok", "cleared": cleared})


# ── Sync ────────────────────────────────────────────────────────────────────


@google_photos_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """Download the picked media in the background.

    Returns quickly; progress and the outcome are polled from
    ``GET /api/google-photos/status``.
    """
    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    if not syncer.is_configured():
        return jsonify_error(
            "Google Photos is not connected",
            400,
            hint="Add a client ID, client secret and refresh token, then test the connection",
        )

    # Shared-album mode reads the album itself, so there is no session to
    # require — only the picker flow needs something picked first.
    if syncer.mode != MODE_SHARED_URL and syncer.read_session() is None:
        return jsonify_error(
            "No photos have been picked yet",
            409,
            hint="Click Pick Photos, choose media in Google Photos, then sync",
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
    """Test a Google Photos credential set.

    Body (JSON, all optional — configured values are used when omitted)::

        {"client_id": "...", "client_secret": "...", "refresh_token": "..."}

    As with the Immich probe the HTTP status stays 200 once the body is valid:
    upstream failures come back as ``{"ok": false, "error": ...}`` so the
    dashboard never mistakes Google's 401 for its own session expiring.
    """
    from metixel.backend.sync import google_photos as gp  # noqa: PLC0415

    state = current_app.config["METIXEL_STATE"]
    data = get_body()
    for key in ("mode", "client_id", "client_secret", "refresh_token", "shared_album_url"):
        if key in data and not isinstance(data[key], str):
            return jsonify_error(f"'{key}' must be a string", 400)

    cfg = state.config.sync.get("google_photos") or {}
    mode = str(data.get("mode", cfg.get("mode") or MODE_PICKER)).strip().lower()
    if mode not in gp.MODES:
        return jsonify_error(
            f"Unknown mode '{mode}'", 400, hint=f"Expected one of {', '.join(gp.MODES)}"
        )

    if mode == MODE_SHARED_URL:
        url = str(data.get("shared_album_url", cfg.get("shared_album_url") or ""))
        return jsonify(gp.probe_shared_album(url))

    client_id = str(data.get("client_id", cfg.get("client_id") or ""))
    client_secret = str(data.get("client_secret", cfg.get("client_secret") or ""))
    refresh_token = str(data.get("refresh_token", cfg.get("refresh_token") or ""))
    return jsonify(gp.probe_credentials(client_id, client_secret, refresh_token))


# ── Helpers ─────────────────────────────────────────────────────────────────


def _session_payload(session: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shape a stored session for the dashboard (no secrets, with a countdown)."""
    if not session:
        return None
    expires_in: float | None = None
    from metixel.backend.sync.google_photos import parse_rfc3339  # noqa: PLC0415

    expires_at = parse_rfc3339(session.get("expire_time"))
    if expires_at is not None:
        expires_in = max(0.0, round(expires_at - time.time(), 1))

    return {
        "id": session.get("id", ""),
        "picker_uri": session.get("picker_uri", ""),
        "expire_time": session.get("expire_time", ""),
        "expires_in_seconds": expires_in,
        "expired": bool(session.get("expired")) or (expires_in == 0.0 and expires_in is not None),
        "media_items_set": bool(session.get("media_items_set")),
        "picked_count": session.get("picked_count", 0),
        "imported_at": session.get("imported_at", 0.0),
        "poll_interval_seconds": session.get("poll_interval_seconds", 5),
        "created_at": session.get("created_at", 0.0),
    }


# ── Module-level syncer cache ───────────────────────────────────────────────

_syncer_cache: GooglePhotosSyncer | None = None


def _get_or_create_syncer(state) -> GooglePhotosSyncer:
    """Return a cached :class:`GooglePhotosSyncer` for the current state.

    Config is refreshed on every call so hot-reloaded credentials are picked
    up without a backend restart.
    """
    from metixel.backend.sync.google_photos import GooglePhotosSyncer  # noqa: PLC0415

    global _syncer_cache  # noqa: PLW0603
    if _syncer_cache is None:
        _syncer_cache = GooglePhotosSyncer(state)
    else:
        _syncer_cache._reload_config()  # noqa: SLF001 — pick up hot-reloaded config
    return _syncer_cache


def _read_progress() -> dict | None:
    """Read the live sync progress file, if it exists."""
    try:
        path = run_path("google_photos_sync_progress.json")
        if path.is_file():
            with open(path) as f:
                return _json.load(f)  # type: ignore[no-any-return]
    except (OSError, ValueError):
        pass
    return None
