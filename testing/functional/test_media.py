# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: media pipeline (scan → playlist) and slideshow advance.

These run ON the Pi against the RUNNING backend.  They verify the core
experience: a media file dropped into a watch folder is picked up by the
FolderWatcher, processed, and added to the slideshow playlist; and the
slideshow actually advances to the next item over time.

The tests use the backend's HTTP API (urllib, no extra deps) and read the
playlist/current-media state files directly.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import wait_for_pipeline_idle

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"

#: How long to wait for a media file to be scanned + processed into the playlist.
#: The folder watcher polls every poll_interval_seconds (default 30s), so a
#: newly-dropped file can take up to ~30s to be picked up, plus processing time.
#: Allow generous margin (3 minutes).
_SCAN_WAIT = 180
#: How long to wait for the slideshow to advance after a 'next' command.
_ADVANCE_WAIT = 40

#: A tiny 1x1 red PNG (base64) — no Pillow dependency needed.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


def _read_json(path: str) -> dict | list | None:
    """Read a JSON file, returning the parsed dict/list, or None on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, (dict, list)) else None
    except (OSError, ValueError):
        return None


def _playlist() -> list[dict]:
    data = _read_json("/run/metixel/playlist.json")
    return data if isinstance(data, list) else []


def _current_media() -> dict | None:
    return _read_json("/run/metixel/current_media.json")


def _wait_for_in_playlist(substring: str, timeout: int = _SCAN_WAIT) -> bool:
    """Poll the playlist until an item whose path contains *substring* appears.

    Unlike :func:`_wait_for_playlist`, this waits for a SPECIFIC file rather
    than just any item — so it works even when the playlist already has
    pre-existing media.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(substring in str(item.get("original_path", "")) for item in _playlist()):
            return True
        time.sleep(3)
    return False


def _find_watch_dir() -> Path:
    """Resolve the first enabled watch path from the running config."""
    config = _api_get("/api/config")
    watch_paths = config.get("sync", {}).get("local", {}).get("watch_paths", [])
    for entry in watch_paths:
        if isinstance(entry, dict) and entry.get("enabled", True):
            p = Path(entry["path"])
            if not p.is_absolute():
                # Relative to the data dir (default /opt/metixel/data).
                p = Path("/opt/metixel/data") / p
            return p
    return Path("/opt/metixel/data/media")


def test_media_scan_adds_file_to_playlist() -> None:
    """A media file dropped into a watch folder must appear in the playlist."""
    watch_dir = _find_watch_dir()
    assert watch_dir.exists(), f"watch dir {watch_dir} does not exist"

    # Wait for the pipeline to be idle first — if a heavy Immich sync or a
    # backend restart is still processing, the folder watcher is throttled
    # and the test file won't be picked up promptly.
    assert wait_for_pipeline_idle(), "media pipeline did not become idle within timeout"

    # Create a small test image (PNG) in the watch folder.
    test_file = watch_dir / "functional-test-image.png"
    test_file.write_bytes(_TINY_PNG)

    try:
        # The folder watcher polls every poll_interval_seconds (default 30s),
        # so no explicit rescan is needed — just wait for the file to be
        # picked up and processed into the playlist.
        assert _wait_for_in_playlist("functional-test-image"), (
            "test image did not appear in playlist within timeout"
        )
    finally:
        test_file.unlink(missing_ok=True)


def test_slideshow_advances() -> None:
    """The slideshow must advance to the next item over time."""
    # Ensure there's at least one item so the slideshow has something to show.
    if not _playlist():
        pytest.skip("no media in playlist — cannot verify slideshow advance")

    first = _current_media()
    # Force an advance via the control API, then confirm the index changes.
    _api_post("/api/control", {"cmd": "next"})

    deadline = time.monotonic() + _ADVANCE_WAIT
    while time.monotonic() < deadline:
        current = _current_media()
        if current and current.get("index") != (first or {}).get("index"):
            return
        time.sleep(2)
    pytest.fail("slideshow did not advance after 'next' command")
