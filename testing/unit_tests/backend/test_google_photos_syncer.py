# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""GooglePhotosSyncer — unit tests driven through the ``HttpGateway`` port.

The syncer mirrors one public shared album.  Here the album reader
(:func:`google_photos_album.fetch_album`) is stubbed with a canned listing and
a ``FakeHttpGateway`` serves the media downloads, so no network access is
required.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

ALBUM_URL = "https://photos.google.com/share/SHARE123?key=KEY456"


class FakeResponse:
    """Implements the ``HttpResponse`` port surface."""

    def __init__(
        self,
        json_data: Any = None,
        status_code: int = 200,
        iter_chunks: tuple[bytes, ...] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.json_data = json_data
        self.status_code = status_code
        self._iter_chunks = iter_chunks
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self.json_data

    def iter_content(self, chunk_size: int = 1):
        return iter(self._iter_chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class FakeHttpGateway:
    """Configurable ``HttpGateway`` fake with per-route canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.headers_seen: list[dict[str, str]] = []
        self._routes: dict[tuple[str, str], Any] = {}

    def route(self, method: str, url: str, response: Any) -> None:
        self._routes[(method, url)] = response

    def get(self, url, *, headers=None, params=None, stream: bool = False, timeout=None):
        self.calls.append(("GET", url, params))
        self.headers_seen.append(dict(headers or {}))
        return self._resolve("GET", url, params)

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return self._resolve("POST", url, json)

    def _resolve(self, method: str, url: str, body: Any) -> Any:
        response = self._routes.get((method, url))
        if response is None:
            raise AssertionError(f"No route registered for {method} {url}")
        if callable(response):
            return response(body)
        return response


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path: Path, monkeypatch):
    """Keep status/progress files out of ``/run/metixel``."""
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


@pytest.fixture(autouse=True)
def online(monkeypatch):
    """The sync cycle's connectivity gate always passes in tests."""
    try:
        import metixel.backend.network_manager as nm
    except ImportError:  # pragma: no cover — non-Linux dev box
        return
    monkeypatch.setattr(nm, "is_connected", lambda: True)


def _make_syncer(tmp_path: Path, http: FakeHttpGateway, cfg: dict | None = None):
    from metixel.backend.state import StateManager
    from metixel.backend.sync.google_photos import GooglePhotosSyncer
    from metixel.shared.config import Config

    config = Config()
    overrides: dict[str, Any] = dict(cfg or {})
    overrides.setdefault("shared_album_url", ALBUM_URL)
    overrides.setdefault("sync_dir", str(tmp_path / "gphotos"))
    config.update("sync", {"google_photos": overrides})
    config_path = tmp_path / "config.json"
    config.save(config_path)

    state = StateManager(config_path, tmp_path / "run")
    return GooglePhotosSyncer(state, http=http)


def _item(key: str, *, video: bool = False) -> dict[str, Any]:
    """One album item, as :func:`google_photos_album.fetch_album` returns it."""
    media_key = f"AF1QipItem{key}".ljust(40, "0")
    return {
        "media_key": media_key,
        "base_url": f"https://lh3.googleusercontent.com/{key}",
        "width": 4032,
        "height": 3024,
        "taken_ms": 1_700_000_000_000,
        "dedup_key": None,
        "is_video": video,
    }


def _media_url(item: dict[str, Any]) -> str:
    return str(item["base_url"]) + ("=dv" if item["is_video"] else "=d")


@pytest.fixture
def album_stub(monkeypatch):
    """Replace the album reader with a canned listing.

    Returns a ``listing`` dict the test can edit before syncing: ``items``,
    ``pages``, ``truncated``, ``complete``, or ``raise`` (an exception).
    """
    import metixel.backend.sync.google_photos as gp

    listing: dict[str, Any] = {"items": [], "pages": 1, "truncated": False}
    calls: list[str] = []

    def _fetch_album(url, **kwargs):
        calls.append(url)
        if listing.get("raise"):
            raise listing["raise"]
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(len(listing["items"]), listing["pages"])
        payload = {
            "url": url,
            "title": "Holiday",
            "album_key": "AF1QipAlbum",
            "pages": listing["pages"],
            "truncated": listing["truncated"],
            "items": list(listing["items"]),
        }
        if "complete" in listing:
            payload["complete"] = listing["complete"]
        return payload

    monkeypatch.setattr(gp.album, "fetch_album", _fetch_album)
    listing["calls"] = calls
    return listing


def _wire(tmp_path: Path, album_stub, items: list[dict], cfg: dict | None = None, **listing):
    """A syncer whose album holds *items*, each downloadable as a JPEG/MP4."""
    album_stub["items"] = items
    album_stub.update(listing)
    http = FakeHttpGateway()
    for item in items:
        http.route(
            "GET",
            _media_url(item),
            FakeResponse(
                iter_chunks=(b"bytes-", item["media_key"].encode()),
                headers={"Content-Type": "video/mp4" if item["is_video"] else "image/jpeg"},
            ),
        )
    return _make_syncer(tmp_path, http, cfg)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_configured_by_album_url_alone(self, tmp_path: Path) -> None:
        assert _make_syncer(tmp_path, FakeHttpGateway()).is_configured() is True

    def test_short_links_count_as_configured(self, tmp_path: Path) -> None:
        syncer = _make_syncer(
            tmp_path, FakeHttpGateway(), {"shared_album_url": "https://photos.app.goo.gl/abc"}
        )
        assert syncer.is_configured() is True

    @pytest.mark.parametrize("url", ["", "https://example.com/album", "not a url"])
    def test_missing_or_bad_url_is_not_configured(self, tmp_path: Path, url: str) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"shared_album_url": url})
        assert syncer.is_configured() is False

    def test_sync_dir_defaults_under_the_data_tree(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"sync_dir": ""})
        assert syncer._sync_dir.name == "google_photos"

    @pytest.mark.parametrize("value", [0, -5, "0", None, "soon"])
    def test_poll_interval_is_clamped(self, tmp_path: Path, value) -> None:
        """A zero interval would re-read the album in a tight loop."""
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"poll_interval_seconds": value})
        assert syncer._poll_interval >= 60

    def test_poll_interval_is_kept_when_sane(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"poll_interval_seconds": 43200})
        assert syncer._poll_interval == 43200

    def test_config_is_not_reloaded_mid_sync(self, tmp_path: Path) -> None:
        """A dashboard poll during a sync must not switch album or folder under it."""
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        original_dir = syncer._sync_dir
        syncer._state.update_config(
            "sync",
            {
                "google_photos": {
                    "shared_album_url": "https://photos.app.goo.gl/other",
                    "sync_dir": str(tmp_path / "elsewhere"),
                }
            },
        )

        syncer._syncing = True
        syncer._reload_config()
        assert syncer._shared_album_url == ALBUM_URL
        assert syncer._sync_dir == original_dir

        syncer._syncing = False
        syncer._reload_config()
        assert syncer._shared_album_url == "https://photos.app.goo.gl/other"


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------


class TestFilenames:
    def test_stem_is_deterministic_for_an_item(self) -> None:
        from metixel.backend.sync.google_photos import GooglePhotosSyncer

        first = GooglePhotosSyncer._stem_for(_item("abc"))
        assert first == GooglePhotosSyncer._stem_for(_item("abc"))
        assert first.startswith("gphoto_")

    def test_different_items_get_different_stems(self) -> None:
        from metixel.backend.sync.google_photos import GooglePhotosSyncer

        assert GooglePhotosSyncer._stem_for(_item("abc")) != GooglePhotosSyncer._stem_for(
            _item("xyz")
        )

    def test_stem_matches_files_already_on_frames(self) -> None:
        """Changing the naming would re-download every photo already synced."""
        from metixel.backend.sync.google_photos import GooglePhotosSyncer

        key = "AF1QipPg4kkwxVMshxETCLNTqR540cscnN03Dtr0wAEc"
        expected = "gphoto_" + hashlib.blake2s(key.encode(), digest_size=8).hexdigest()
        assert GooglePhotosSyncer._stem_for({"media_key": key}) == expected

    def test_extension_comes_from_the_content_type(self) -> None:
        from metixel.backend.sync.google_photos import _extension_for

        assert _extension_for("image/heic", False) == ".heic"
        assert _extension_for("image/jpeg; charset=binary", False) == ".jpg"
        assert _extension_for("video/quicktime", True) == ".mov"

    def test_unknown_content_type_falls_back_by_media_type(self) -> None:
        from metixel.backend.sync.google_photos import _extension_for

        assert _extension_for("", True) == ".mp4"
        assert _extension_for("", False) == ".jpg"

    def test_generic_content_types_never_become_bin(self) -> None:
        """``Accept: application/octet-stream`` invites exactly that answer."""
        from metixel.backend.sync.google_photos import _extension_for

        assert _extension_for("application/octet-stream", False) == ".jpg"
        assert _extension_for("application/octet-stream", True) == ".mp4"
        assert _extension_for("text/plain", False) == ".jpg"


# ---------------------------------------------------------------------------
# Sync cycle
# ---------------------------------------------------------------------------


class TestSyncCycle:
    def test_downloads_the_album(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a"), _item("b")])

        result = syncer.sync_once()

        assert result.success is True
        assert result.total_remote == 2
        assert result.downloaded == 2
        assert result.album_title == "Holiday"
        assert album_stub["calls"] == [ALBUM_URL]
        files = sorted(p.name for p in syncer._sync_dir.iterdir())
        assert len(files) == 2
        assert all(f.startswith("gphoto_") and f.endswith(".jpg") for f in files)
        assert (syncer._sync_dir / files[0]).read_bytes().startswith(b"bytes-")

    def test_downloads_send_no_credentials(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer.sync_once()
        assert syncer._http.headers_seen
        assert all("Authorization" not in h for h in syncer._http.headers_seen)

    def test_second_run_downloads_nothing_new(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer.sync_once()

        result = syncer.sync_once()

        assert result.downloaded == 0
        assert result.skipped == 1

    def test_new_album_photos_arrive_on_the_next_cycle(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer.sync_once()

        grown = _wire(tmp_path, album_stub, [_item("a"), _item("b"), _item("c")])
        result = grown.sync_once()

        assert result.downloaded == 2
        assert len(list(grown._sync_dir.iterdir())) == 3

    def test_videos_use_the_video_download_suffix(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("v", video=True)])

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert any(url.endswith("=dv") for _m, url, _p in syncer._http.calls)
        assert [p.suffix for p in syncer._sync_dir.iterdir()] == [".mp4"]

    def test_result_is_persisted_for_the_dashboard(
        self, tmp_path: Path, album_stub, runtime_dir: Path
    ) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer.sync_once()

        status_file = runtime_dir / "google_photos_sync_status.json"
        saved = json.loads(status_file.read_text())
        assert saved["downloaded"] == 1
        assert saved["album_title"] == "Holiday"
        # And it survives a fresh syncer instance.
        reloaded = _make_syncer(tmp_path, FakeHttpGateway())
        assert reloaded.get_last_result().downloaded == 1

    def test_sync_adds_the_folder_to_the_watch_paths(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer.sync_once()

        paths = [p["path"] for p in syncer._state.config.sync["local"]["watch_paths"]]
        assert str(tmp_path / "gphotos") in paths

    def test_download_failure_is_reported(self, tmp_path: Path, album_stub) -> None:
        item = _item("a")
        syncer = _wire(tmp_path, album_stub, [item])
        syncer._http.route("GET", _media_url(item), FakeResponse(status_code=403))

        result = syncer.sync_once()

        assert result.success is False
        assert result.downloaded == 0
        assert any("link may have been turned off" in e for e in result.errors)

    def test_html_response_is_refused(self, tmp_path: Path, album_stub) -> None:
        """A sign-in page must never be written out as a photo."""
        item = _item("a")
        syncer = _wire(tmp_path, album_stub, [item])
        syncer._http.route(
            "GET",
            _media_url(item),
            FakeResponse(iter_chunks=(b"<html>",), headers={"Content-Type": "text/html"}),
        )

        result = syncer.sync_once()

        assert result.downloaded == 0
        assert any("instead of media" in e for e in result.errors)
        assert list(syncer._sync_dir.iterdir()) == []

    def test_unconfigured_sync_does_not_call_google(self, tmp_path: Path, album_stub) -> None:
        http = FakeHttpGateway()  # every route raises if called
        syncer = _make_syncer(tmp_path, http, {"shared_album_url": ""})

        result = syncer.sync_once()

        assert result.success is False
        assert "No Google Photos shared album link" in result.errors[0]
        assert http.calls == []
        assert album_stub["calls"] == []

    def test_unreadable_album_fails_clearly(self, tmp_path: Path, album_stub) -> None:
        from metixel.backend.sync.google_photos_album import SharedAlbumError

        syncer = _wire(tmp_path, album_stub, [_item("a")])
        album_stub["raise"] = SharedAlbumError("No photos found on the album page")

        result = syncer.sync_once()

        assert result.success is False
        assert "No photos found" in result.errors[0]
        assert result.downloaded == 0

    def test_truncated_album_is_reported_but_still_downloaded(
        self, tmp_path: Path, album_stub
    ) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a"), _item("b")], pages=200, truncated=True)

        result = syncer.sync_once()

        assert result.downloaded == 2
        assert result.album_pages == 200
        assert any("could be read" in e for e in result.errors)
        assert result.success is False

    def test_cancellation_stops_the_download_loop(self, tmp_path: Path, album_stub) -> None:
        """A cancel mid-download finishes the current file, then stops."""
        items = [_item("a"), _item("b")]
        syncer = _wire(tmp_path, album_stub, items)

        def _responder(_params):
            syncer.cancel()
            return FakeResponse(iter_chunks=(b"bytes",), headers={"Content-Type": "image/jpeg"})

        for item in items:
            syncer._http.route("GET", _media_url(item), _responder)

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert any("Cancelled" in e for e in result.errors)
        # The item the cancel left unattempted is not "already here".
        assert result.skipped == 0

    def test_skipped_counts_only_files_found_on_disk(self, tmp_path: Path, album_stub) -> None:
        items = [_item("a"), _item("b"), _item("c")]
        syncer = _wire(tmp_path, album_stub, items[:1])
        syncer.sync_once()  # "a" is now on disk

        syncer = _wire(tmp_path, album_stub, items)
        syncer._http.route("GET", _media_url(items[2]), FakeResponse(status_code=500))
        result = syncer.sync_once()

        assert result.downloaded == 1  # b
        assert result.skipped == 1  # a
        assert len(result.errors) == 1  # c failed


# ---------------------------------------------------------------------------
# Strict (mirror) mode
# ---------------------------------------------------------------------------


class TestStrictSync:
    def test_strict_sync_mirrors_the_album(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")], {"strict_sync": True})
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        removed_from_album = syncer._sync_dir / "gphoto_gone.jpg"
        removed_from_album.write_bytes(b"old")

        result = syncer.sync_once()

        assert result.deleted == 1
        assert not removed_from_album.exists()
        assert len(list(syncer._sync_dir.iterdir())) == 1

    def test_strict_sync_leaves_files_it_did_not_write(self, tmp_path: Path, album_stub) -> None:
        """The folder is user-configurable — their own uploads are never ours to delete."""
        syncer = _wire(tmp_path, album_stub, [_item("a")], {"strict_sync": True})
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        own_upload = syncer._sync_dir / "IMG_4521.jpg"
        own_upload.write_bytes(b"mine")
        gone_from_album = syncer._sync_dir / "gphoto_gone.jpg"
        gone_from_album.write_bytes(b"old")

        result = syncer.sync_once()

        assert own_upload.exists()
        assert not gone_from_album.exists()
        assert result.deleted == 1

    def test_pull_only_keeps_files_removed_from_the_album(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")])
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_stale.jpg"
        stray.write_bytes(b"old")

        result = syncer.sync_once()

        assert result.deleted == 0
        assert stray.exists()

    def test_strict_sync_never_deletes_from_a_partial_listing(
        self, tmp_path: Path, album_stub
    ) -> None:
        """A partly-read album must not look like the photos beyond it were removed."""
        syncer = _wire(tmp_path, album_stub, [_item("a")], {"strict_sync": True}, complete=False)
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        beyond_page_one = syncer._sync_dir / "gphoto_on_a_later_page.jpg"
        beyond_page_one.write_bytes(b"keep")

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert result.deleted == 0
        assert beyond_page_one.exists()
        assert any("Strict sync skipped" in e for e in result.errors)

    def test_strict_sync_skips_a_truncated_album_too(self, tmp_path: Path, album_stub) -> None:
        syncer = _wire(tmp_path, album_stub, [_item("a")], {"strict_sync": True}, truncated=True)
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_later.jpg"
        stray.write_bytes(b"keep")

        result = syncer.sync_once()

        assert result.deleted == 0
        assert stray.exists()

    def test_cancelled_sync_deletes_nothing(self, tmp_path: Path, album_stub) -> None:
        item = _item("a")
        syncer = _wire(tmp_path, album_stub, [item], {"strict_sync": True})
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_gone.jpg"
        stray.write_bytes(b"old")

        def _responder(_params):
            syncer.cancel()
            return FakeResponse(iter_chunks=(b"bytes",), headers={"Content-Type": "image/jpeg"})

        syncer._http.route("GET", _media_url(item), _responder)

        result = syncer.sync_once()

        assert result.deleted == 0
        assert stray.exists()


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------


class TestProbeSharedAlbum:
    def test_empty_url(self) -> None:
        from metixel.backend.sync.google_photos import probe_shared_album

        assert probe_shared_album("")["ok"] is False

    def test_non_album_url(self) -> None:
        from metixel.backend.sync.google_photos import probe_shared_album

        result = probe_shared_album("https://example.com/album")
        assert result["ok"] is False
        assert "share link" in result["error"]

    def test_reports_the_first_page_count(self, monkeypatch) -> None:
        import metixel.backend.sync.google_photos as gp

        monkeypatch.setattr(
            gp.album,
            "fetch_album",
            lambda url, **k: {
                "title": "Holiday",
                "items": [{}, {}, {}],
                "pages": 1,
                "truncated": True,
            },
        )

        result = gp.probe_shared_album("https://photos.google.com/share/ABC?key=K")

        assert result["ok"] is True
        assert result["count"] == 3
        assert result["has_more_pages"] is True
        assert "Holiday" in result["message"]

    def test_reader_failure_is_reported(self, monkeypatch) -> None:
        import metixel.backend.sync.google_photos as gp
        from metixel.backend.sync.google_photos_album import SharedAlbumError

        def _boom(url, **k):
            raise SharedAlbumError("Album not found (HTTP 404)")

        monkeypatch.setattr(gp.album, "fetch_album", _boom)

        result = gp.probe_shared_album("https://photos.google.com/share/ABC?key=K")

        assert result["ok"] is False
        assert "404" in result["error"]
