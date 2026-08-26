# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Immich sync API endpoints for the web dashboard.

Provides manual sync triggering, album listing (for the album picker UI),
and sync status retrieval.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import get_body, jsonify_error
from metixel.shared.paths import resolve_install_path

if TYPE_CHECKING:
    from metixel.backend.sync.immich import ImmichSyncer

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
        return jsonify_error(
            str(e),
            502,
            hint="Check server URL, API key, and network connectivity",
        )

    # Simplify for the frontend — only return what the picker needs
    result = [
        {
            "id": a["id"],
            "name": a.get("albumName", "Untitled"),
            "assetCount": a.get("assetCount", 0),
        }
        for a in albums
    ]
    # Sort alphabetically, cap the list (the UI filters client-side).
    result.sort(key=lambda a: a["name"].lower())
    result = result[:5000]
    return jsonify(result)


@immich_bp.route("/albums/add", methods=["POST"])
def add_album():
    """Add an album to the configured sync group (deduplicated by id)."""

    data = get_body()
    album_id = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    if not album_id or not name:
        return jsonify_error("Missing album id or name", 400)

    state = current_app.config["METIXEL_STATE"]
    config = state.config
    albums = list(config.sync["immich"].get("albums") or [])
    if not any(a.get("id") == album_id for a in albums):
        albums.append({"id": album_id, "name": name})
        state.update_config("sync", {"immich": {"albums": albums}})
        logger.info("Added Immich album to sync group: %s (%s)", name, album_id)

    return jsonify({"status": "ok", "albums": albums})


@immich_bp.route("/albums/remove", methods=["POST"])
def remove_album():
    """Remove an album from the sync group and delete its local folder.

    The UI confirms before calling this — the ``album_<id>`` folder and
    all downloaded files are deleted.
    """

    data = get_body()
    album_id = (data.get("id") or "").strip()
    if not album_id:
        return jsonify_error("Missing album id", 400)

    state = current_app.config["METIXEL_STATE"]
    config = state.config
    albums = [a for a in (config.sync["immich"].get("albums") or []) if a.get("id") != album_id]
    state.update_config("sync", {"immich": {"albums": albums}})

    # Delete the local album folder (best-effort).
    import shutil

    sync_dir = config.sync["immich"].get("sync_dir", "media/sync/immich/")
    sync_dir_path = resolve_install_path(sync_dir)
    album_dir = sync_dir_path / f"album_{album_id}"
    deleted = False
    if album_dir.is_dir():
        shutil.rmtree(album_dir, ignore_errors=True)
        deleted = True
        logger.info("Removed Immich album %s and deleted local folder %s", album_id, album_dir)

    return jsonify({"status": "ok", "albums": albums, "deleted_folder": deleted})


@immich_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """Trigger a manual Immich sync cycle (all configured albums).

    Runs synchronously in a background thread so the HTTP request returns
    quickly. The result can be polled via ``GET /api/immich/status``.
    """

    state = current_app.config["METIXEL_STATE"]
    syncer = _get_or_create_syncer(state)

    # Run sync in a background thread so the request returns immediately
    def _run():
        try:
            result = syncer.sync_once()
            logger.info("Manual Immich sync finished: %s", result.to_dict())
        except Exception:
            logger.exception("Manual Immich sync failed")

    thread = threading.Thread(target=_run, name="immich-manual-sync", daemon=True)
    thread.start()

    return jsonify(
        {
            "status": "started",
            "message": "Sync started in background — check GET /api/immich/status for results",
        }
    )


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
        return jsonify(
            {
                "status": "never_run",
                "last_sync": None,
                "progress": progress,
            }
        )

    return jsonify(
        {
            "status": "ok",
            "last_sync": result.to_dict(),
            "progress": progress,
        }
    )


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

    data = get_body()
    server_url = data.get("server_url", "").rstrip("/")
    api_key = data.get("api_key", "")

    if not server_url or not api_key:
        return jsonify_error("Missing server_url or api_key", 400)

    headers = {"Accept": "application/json", "x-api-key": api_key}

    try:
        # Try the /api/albums endpoint as a connectivity check
        resp = req_lib.get(
            f"{server_url}/api/albums",
            headers=headers,
            timeout=(10, 15),
        )
        if resp.status_code == 401 or resp.status_code == 403:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"Authentication failed (HTTP {resp.status_code}). Check your API key."
                    ),
                }
            ), 401
        resp.raise_for_status()
        album_count = len(resp.json())
        return jsonify(
            {
                "ok": True,
                "message": f"Connected successfully — found {album_count} album(s)",
                "album_count": album_count,
            }
        )
    except req_lib.exceptions.ConnectionError:
        return jsonify(
            {
                "ok": False,
                "error": "Could not connect to the server. Check the URL and network.",
            }
        ), 502
    except req_lib.exceptions.Timeout:
        return jsonify(
            {
                "ok": False,
                "error": "Connection timed out. Check the server URL and network.",
            }
        ), 504
    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
            }
        ), 502


# ── Module-level syncer cache ───────────────────────────────────────────────

_syncer_cache: ImmichSyncer | None = None


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
