# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Immich sync API endpoints for the web dashboard.

Provides manual sync triggering, album listing (for the album picker UI),
and sync status retrieval.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

immich_bp = Blueprint("immich", __name__)


@immich_bp.route("/albums", methods=["GET"])
def list_albums():
    """List all albums from the configured Immich server.

    Uses the configured server URL and API key to fetch the album list.
    Returns a simplified list of ``{id, name, assetCount}`` objects.
    """

    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    try:
        albums = syncer._list_albums()  # noqa: SLF001 (internal access)
    except Exception as e:
        logger.exception("Failed to list Immich albums")
        return jsonify({
            "error": str(e),
            "hint": "Check server URL, API key, and network connectivity",
        }), 502

    # Simplify for the frontend — only return what the picker needs
    result = [
        {
            "id": a["id"],
            "name": a.get("albumName", "Untitled"),
            "assetCount": a.get("assetCount", 0),
        }
        for a in albums
    ]
    # Sort alphabetically
    result.sort(key=lambda a: a["name"].lower())
    return jsonify(result)


@immich_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """Trigger a manual Immich sync cycle.

    Runs synchronously in a background thread so the HTTP request returns
    quickly. The result can be polled via ``GET /api/immich/status``.

    Body (optional JSON):
        ``{"album_name": "My Album"}`` — override the configured album for
        this one-time sync.
    """

    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    data = request.get_json(silent=True) or {}

    # Optionally override album name for this sync
    if "album_name" in data:
        syncer._album_name = data["album_name"]  # noqa: SLF001

    # Run sync in a background thread so the request returns immediately
    def _run():
        try:
            result = syncer.sync_once()
            logger.info("Manual Immich sync finished: %s", result.to_dict())
        except Exception:
            logger.exception("Manual Immich sync failed")

    thread = threading.Thread(target=_run, name="immich-manual-sync", daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Sync started in background — check GET /api/immich/status for results",
    })


@immich_bp.route("/status", methods=["GET"])
def sync_status():
    """Return the most recent Immich sync result plus live progress.

    Returns ``null`` if no sync has ever been performed.
    """

    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    result = syncer.get_last_result()

    # Also read live progress file
    progress = _read_progress()

    if result is None:
        return jsonify({
            "status": "never_run",
            "last_sync": None,
            "progress": progress,
        })

    return jsonify({
        "status": "ok",
        "last_sync": result.to_dict(),
        "progress": progress,
    })


@immich_bp.route("/cancel", methods=["POST"])
def cancel_sync():
    """Cancel the currently running Immich sync cycle.

    The sync will finish the current file download, then abort.
    """

    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)
    syncer.cancel()
    return jsonify({"status": "ok", "message": "Cancellation requested"})


@immich_bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Test the Immich server connection and API key.

    Body (JSON):
        ``{"server_url": "...", "api_key": "..."}``

    Returns 200 if the connection works, or an error message.
    """
    import requests as req_lib

    data = request.get_json(silent=True) or {}
    server_url = data.get("server_url", "").rstrip("/")
    api_key = data.get("api_key", "")

    if not server_url or not api_key:
        return jsonify({"error": "Missing server_url or api_key"}), 400

    headers = {"Accept": "application/json", "x-api-key": api_key}

    try:
        # Try the /api/albums endpoint as a connectivity check
        resp = req_lib.get(
            f"{server_url}/api/albums",
            headers=headers,
            timeout=(10, 15),
        )
        if resp.status_code == 401 or resp.status_code == 403:
            return jsonify({
                "ok": False,
                "error": f"Authentication failed (HTTP {resp.status_code}). Check your API key.",
            }), 401
        resp.raise_for_status()
        album_count = len(resp.json())
        return jsonify({
            "ok": True,
            "message": f"Connected successfully — found {album_count} album(s)",
            "album_count": album_count,
        })
    except req_lib.exceptions.ConnectionError:
        return jsonify({
            "ok": False,
            "error": "Could not connect to the server. Check the URL and network.",
        }), 502
    except req_lib.exceptions.Timeout:
        return jsonify({
            "ok": False,
            "error": "Connection timed out. Check the server URL and network.",
        }), 504
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 502


# ── Module-level syncer cache ───────────────────────────────────────────────

_syncer_cache: Any = None


def _get_or_create_syncer(state) -> ImmichSyncer:
    """Return a cached ``ImmichSyncer`` for the current state manager.

    The syncer is lightweight — it just wraps API calls. We reuse it
    so album-listing and sync-trigger share the same instance.
    Config is refreshed on every call so hot-reloaded settings are picked up.
    """
    from metixel.backend.sync.immich import ImmichSyncer  # noqa: PLC0415

    global _syncer_cache  # noqa: PLW0603
    if _syncer_cache is None:
        _syncer_cache = ImmichSyncer(state)
    else:
        _syncer_cache._reload_config()  # noqa: SLF001  — pick up hot-reloaded config
    return _syncer_cache


def _read_progress() -> dict | None:
    """Read the live sync progress file, if it exists."""
    import json as _json
    import os as _os

    try:
        path = "/run/metixel/immich_sync_progress.json"
        if _os.path.isfile(path):
            with open(path) as f:
                return _json.load(f)  # type: ignore[no-any-return]
    except (OSError, ValueError):
        pass
    return None
