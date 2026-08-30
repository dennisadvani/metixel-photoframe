# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: Immich sync.

These run ON the Pi against the RUNNING backend.  They verify the Immich
integration end-to-end: the server connection + API key work, the two
configured test albums are found on the server, and a sync cycle downloads
them into the local sync directory.

The tests require Immich credentials in ``functional/.env``:

    METIXEL_TEST_IMMICH_URL=http://192.168.222.228:2283
    METIXEL_TEST_IMMICH_API_KEY=your-api-key
    METIXEL_TEST_IMMICH_ALBUM_1=AlbumNameOne
    METIXEL_TEST_IMMICH_ALBUM_2=AlbumNameTwo

If any credential is missing the suite skips (it cannot run without a real
Immich server).

The ``/api/immich/albums`` and ``/api/immich/sync`` endpoints use the RUNNING
backend's config (not the ``.env`` creds), so the tests configure the backend
via ``PUT /api/config/sync`` first and restore the original settings after.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import urllib.request
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"

#: How long to wait for a sync cycle to finish.  The test albums can contain
#: many large videos (hundreds of MB each), so a full sync can take several
#: minutes — allow up to 10 minutes.
_SYNC_WAIT = 600


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def _api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _api_put(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _sync_dir() -> Path:
    """Resolve the Immich sync directory from the running config."""
    config = _api_get("/api/config")
    sync_dir = config.get("sync", {}).get("immich", {}).get("sync_dir", "media/sync/immich/")
    p = Path(sync_dir)
    if not p.is_absolute():
        p = Path("/opt/metixel/data") / p
    return p


def _wait_for_sync_done(timeout: int = _SYNC_WAIT) -> dict:
    """Poll the sync status until a sync cycle completes (or timeout).

    Waits for a ``last_sync`` result whose ``started_at`` is newer than the
    trigger time, so a stale result from a previous run is not mistaken for
    the sync we just triggered.
    """
    trigger_time = time.time()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _api_get("/api/immich/status")
        last = status.get("last_sync")
        if last is not None and last.get("started_at", 0) >= trigger_time:
            return last
        time.sleep(3)
    pytest.fail("Immich sync did not complete within timeout")


@pytest.fixture(scope="module")
def immich_configured(immich_creds: dict[str, str]) -> dict[str, str]:
    """Configure the running backend's Immich settings from ``.env``.

    Saves the original ``sync.immich`` config, applies the test server URL +
    API key, and restores the original on teardown.  Also deletes any album
    folders the sync test downloaded so the pipeline isn't left processing a
    large backlog of test media.  Yields the creds so tests can look up album
    IDs by name.
    """
    if not immich_creds["url"] or not immich_creds["api_key"]:
        pytest.skip("Immich credentials not set in functional/.env")
    if not immich_creds["album_1"] or not immich_creds["album_2"]:
        pytest.skip("Immich test album names not set in functional/.env")

    # Save the original Immich config so we can restore it.
    original = _api_get("/api/config/sync").get("immich", {})

    try:
        # Apply the test server URL + API key (keep existing albums).
        _api_put(
            "/api/config/sync",
            {
                "immich": {
                    "server_url": immich_creds["url"],
                    "api_key": immich_creds["api_key"],
                }
            },
        )
        yield immich_creds
    finally:
        # Restore the original Immich config.
        _api_put("/api/config/sync", {"immich": original})

        # Delete any album folders the sync test downloaded, so the pipeline
        # isn't left processing a large backlog of test media.  The sync dir
        # is a watch path, so the folder watcher will pick up the deletions
        # and remove them from the playlist on its next scan.
        sync_dir = _sync_dir()
        if sync_dir.exists():
            for album_dir in sync_dir.glob("album_*"):
                if album_dir.is_dir():
                    shutil.rmtree(album_dir, ignore_errors=True)
                    logger.info("Cleaned up Immich test album folder: %s", album_dir)


def _album_ids_by_name(creds: dict[str, str]) -> dict[str, str]:
    """Return {album_name: album_id} for the two configured test albums."""
    albums = _api_get("/api/immich/albums")
    by_name = {a.get("name", ""): a.get("id", "") for a in albums}
    missing = [n for n in (creds["album_1"], creds["album_2"]) if n not in by_name]
    assert not missing, f"albums not found on server: {missing}; got: {sorted(by_name)}"
    return {
        creds["album_1"]: by_name[creds["album_1"]],
        creds["album_2"]: by_name[creds["album_2"]],
    }


def test_immich_connection_works(immich_creds: dict[str, str]) -> None:
    """The Immich server URL + API key must authenticate successfully."""
    if not immich_creds["url"] or not immich_creds["api_key"]:
        pytest.skip("Immich credentials not set in functional/.env")

    resp = _api_post(
        "/api/immich/test-connection",
        {"server_url": immich_creds["url"], "api_key": immich_creds["api_key"]},
    )
    assert resp.get("ok") is True, f"Immich connection failed: {resp}"


def test_immich_albums_found(immich_configured: dict[str, str]) -> None:
    """The two configured test albums must exist on the Immich server."""
    ids = _album_ids_by_name(immich_configured)
    assert len(ids) == 2, f"expected 2 albums, got: {ids}"


def test_immich_sync_downloads_albums(immich_configured: dict[str, str]) -> None:
    """A sync cycle must download the two configured albums locally."""
    creds = immich_configured

    # Resolve the two test albums to IDs and configure them for sync.
    ids = _album_ids_by_name(creds)
    albums = [{"id": aid, "name": name} for name, aid in ids.items()]
    _api_put("/api/config/sync", {"immich": {"albums": albums, "enabled": True}})

    # Trigger a manual sync cycle.
    _api_post("/api/immich/sync", {})

    result = _wait_for_sync_done()

    # The sync result must report success and cover both albums.
    assert result.get("success") is True, f"Immich sync reported failure: {result}"
    synced_albums = result.get("albums", [])
    assert len(synced_albums) >= 2, (
        f"expected at least 2 albums synced, got {len(synced_albums)}: {result}"
    )

    # The local sync directory must now contain the downloaded media.
    sync_dir = _sync_dir()
    assert sync_dir.exists(), f"sync dir {sync_dir} does not exist"
    downloaded = list(sync_dir.rglob("*"))
    media_files = [f for f in downloaded if f.is_file() and not f.name.startswith(".")]
    assert media_files, f"no media downloaded into {sync_dir}"
