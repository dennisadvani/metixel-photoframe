# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""GooglePhotosSyncer — unit tests driven through the ``HttpGateway`` port.

The real client talks to the Google Photos Picker API over ``requests``; here a
``FakeHttpGateway`` serves canned responses and the OAuth token endpoint is
monkeypatched, so no network access is required.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

SESSIONS_URL = "https://photospicker.googleapis.com/v1/sessions"
MEDIA_ITEMS_URL = "https://photospicker.googleapis.com/v1/mediaItems"


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
        self._routes: dict[tuple[str, str], Any] = {}

    def route(self, method: str, url: str, response: Any) -> None:
        self._routes[(method, url)] = response

    def get(self, url, *, headers=None, params=None, stream: bool = False, timeout=None):
        self.calls.append(("GET", url, params))
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
    """Keep status/progress/session files out of ``/run/metixel``."""
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    """Serve a canned OAuth token response instead of calling Google."""
    import metixel.backend.sync.google_photos as gp

    calls: list[dict[str, str]] = []

    def _post_form(url: str, data: dict[str, str]):
        calls.append(data)
        return FakeResponse(json_data={"access_token": "tok-123", "expires_in": 3600})

    monkeypatch.setattr(gp, "post_form", _post_form)
    return calls


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
    overrides.setdefault("client_id", "cid")
    overrides.setdefault("client_secret", "secret")
    overrides.setdefault("refresh_token", "refresh")
    overrides.setdefault("sync_dir", str(tmp_path / "gphotos"))
    config.update("sync", {"google_photos": overrides})
    config_path = tmp_path / "config.json"
    config.save(config_path)

    state = StateManager(config_path, tmp_path / "run")
    return GooglePhotosSyncer(state, http=http)


def _item(
    item_id: str,
    filename: str = "IMG_1.JPG",
    mime: str = "image/jpeg",
    type_: str = "PHOTO",
):
    return {
        "id": item_id,
        "type": type_,
        "mediaFile": {
            "baseUrl": f"https://lh3.googleusercontent.com/{item_id}",
            "filename": filename,
            "mimeType": mime,
        },
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_is_configured_requires_all_three_credentials(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        assert syncer.is_configured() is True

        partial = _make_syncer(tmp_path, FakeHttpGateway(), {"refresh_token": ""})
        assert partial.is_configured() is False

    def test_sync_dir_defaults_under_the_data_tree(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"sync_dir": ""})
        assert syncer._sync_dir.name == "google_photos"


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------


class TestFilenames:
    def test_filename_is_deterministic_for_an_item(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        first = syncer._filename_for(_item("abc"))
        second = syncer._filename_for(_item("abc"))
        assert first == second
        assert first.startswith("gphoto_")
        assert first.endswith(".jpg")

    def test_different_items_get_different_names(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        assert syncer._filename_for(_item("abc")) != syncer._filename_for(_item("xyz"))

    def test_unsafe_characters_are_stripped_from_the_stem(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        name = syncer._filename_for(_item("abc", filename="../../etc/pa sswd.jpg"))
        assert "/" not in name
        assert ".." not in name
        assert name.endswith(".jpg")

    def test_extension_comes_from_the_mime_type(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        heic = syncer._filename_for(_item("a", filename="x", mime="image/heic"))
        video = syncer._filename_for(_item("b", filename="clip", mime="video/mp4", type_="VIDEO"))
        assert heic.endswith(".heic")
        assert video.endswith(".mp4")

    def test_video_without_mime_falls_back_to_mp4(self, tmp_path: Path) -> None:
        from metixel.backend.sync.google_photos import _extension_for

        assert _extension_for({}, "VIDEO") == ".mp4"
        assert _extension_for({}, "PHOTO") == ".jpg"
        assert _extension_for({"filename": "x.png"}, "PHOTO") == ".png"


# ---------------------------------------------------------------------------
# Timestamp / polling helpers
# ---------------------------------------------------------------------------


class TestTimestampHelpers:
    def test_parse_rfc3339_accepts_z_suffix(self) -> None:
        from metixel.backend.sync.google_photos import parse_rfc3339

        assert parse_rfc3339("1970-01-01T00:00:10Z") == 10.0

    def test_parse_rfc3339_tolerates_nanoseconds(self) -> None:
        from metixel.backend.sync.google_photos import parse_rfc3339

        assert parse_rfc3339("1970-01-01T00:00:10.123456789Z") == pytest.approx(10.123456)

    def test_parse_rfc3339_returns_none_for_junk(self) -> None:
        from metixel.backend.sync.google_photos import parse_rfc3339

        assert parse_rfc3339("not a time") is None
        assert parse_rfc3339(None) is None

    def test_expiry_detection(self) -> None:
        from metixel.backend.sync.google_photos import _is_expired

        assert _is_expired({"expire_time": "1970-01-01T00:00:00Z"}) is True
        assert _is_expired({"expire_time": "2999-01-01T00:00:00Z"}) is False
        # No expiry known — treat the session as live rather than dead.
        assert _is_expired({}) is False

    def test_polling_interval_parsing(self) -> None:
        from metixel.backend.sync.google_photos import _polling_interval_seconds

        assert _polling_interval_seconds({"pollingConfig": {"pollInterval": "4s"}}) == 4
        assert _polling_interval_seconds({"pollingConfig": {"pollInterval": "0.2s"}}) == 1
        assert _polling_interval_seconds({}) == 5


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class TestAccessToken:
    def test_token_is_cached_between_calls(self, tmp_path: Path, fake_token) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        assert syncer._access_token() == "tok-123"
        assert syncer._access_token() == "tok-123"
        assert len(fake_token) == 1

    def test_token_is_refetched_when_credentials_change(self, tmp_path: Path, fake_token) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        syncer._access_token()
        syncer._state.update_config("sync", {"google_photos": {"refresh_token": "other"}})
        syncer._reload_config()
        syncer._access_token()
        assert len(fake_token) == 2

    def test_missing_credentials_raise(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"client_secret": ""})
        with pytest.raises(RuntimeError, match="not connected"):
            syncer._access_token()

    def test_rejected_refresh_token_is_explained(self, tmp_path: Path, monkeypatch) -> None:
        import metixel.backend.sync.google_photos as gp

        monkeypatch.setattr(
            gp,
            "post_form",
            lambda url, data: FakeResponse(
                json_data={"error": "invalid_grant", "error_description": "Token expired"},
                status_code=400,
            ),
        )
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        with pytest.raises(RuntimeError, match="google_photos_auth.py"):
            syncer._access_token()


class TestProbeCredentials:
    def test_missing_credentials_reported_without_a_call(self) -> None:
        from metixel.backend.sync.google_photos import probe_credentials

        result = probe_credentials("", "", "")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_successful_probe(self, monkeypatch) -> None:
        from metixel.backend.sync.google_photos import probe_credentials

        http = FakeHttpGateway()
        http.route("POST", SESSIONS_URL, FakeResponse(json_data={"id": "probe"}))
        # The probe deletes its throwaway session directly via requests.
        import metixel.backend.sync.google_photos as gp

        monkeypatch.setattr(gp.requests, "delete", lambda *a, **k: FakeResponse())

        result = probe_credentials("cid", "secret", "refresh", http=http)
        assert result["ok"] is True

    def test_forbidden_probe_explains_the_scope(self) -> None:
        from metixel.backend.sync.google_photos import probe_credentials

        http = FakeHttpGateway()
        http.route("POST", SESSIONS_URL, FakeResponse(status_code=403))
        result = probe_credentials("cid", "secret", "refresh", http=http)
        assert result["ok"] is False
        assert "photospicker.mediaitems.readonly" in result["error"]


# ---------------------------------------------------------------------------
# Picking sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_create_session_persists_the_picker_uri(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route(
            "POST",
            SESSIONS_URL,
            FakeResponse(
                json_data={
                    "id": "sess-1",
                    "pickerUri": "https://photos.google.com/pick?x=1",
                    "expireTime": "2999-01-01T00:00:00Z",
                    "pollingConfig": {"pollInterval": "3s"},
                }
            ),
        )
        syncer = _make_syncer(tmp_path, http)

        session = syncer.create_session()

        assert session["id"] == "sess-1"
        assert session["picker_uri"] == "https://photos.google.com/pick?x=1"
        assert session["poll_interval_seconds"] == 3
        assert syncer.read_session() == session

    def test_create_session_error_raises(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route("POST", SESSIONS_URL, FakeResponse(status_code=500))
        syncer = _make_syncer(tmp_path, http)
        with pytest.raises(RuntimeError, match="picking session"):
            syncer.create_session()

    def test_refresh_session_picks_up_the_pick(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route(
            "GET",
            f"{SESSIONS_URL}/sess-1",
            FakeResponse(
                json_data={
                    "id": "sess-1",
                    "mediaItemsSet": True,
                    "expireTime": "2999-01-01T00:00:00Z",
                }
            ),
        )
        syncer = _make_syncer(tmp_path, http)
        syncer._write_session({"id": "sess-1", "media_items_set": False})

        session = syncer.refresh_session()

        assert session is not None
        assert session["media_items_set"] is True
        assert syncer.read_session()["media_items_set"] is True

    def test_refresh_session_marks_a_forgotten_session_expired(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route("GET", f"{SESSIONS_URL}/sess-1", FakeResponse(status_code=404))
        syncer = _make_syncer(tmp_path, http)
        syncer._write_session({"id": "sess-1"})

        session = syncer.refresh_session()

        assert session is not None
        assert session["expired"] is True

    def test_refresh_session_without_one_returns_none(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        assert syncer.refresh_session() is None

    def test_clear_session_removes_the_file(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        syncer._write_session({"id": "sess-1"})
        assert syncer.clear_session(delete_remote=False) is True
        assert syncer.read_session() is None
        assert syncer.clear_session(delete_remote=False) is False

    def test_pending_session_ignores_imported_and_expired(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())

        syncer._write_session({"id": "s", "media_items_set": False})
        assert syncer._pending_session() is None

        syncer._write_session({"id": "s", "media_items_set": True, "imported_at": time.time()})
        assert syncer._pending_session() is None

        syncer._write_session(
            {"id": "s", "media_items_set": True, "expire_time": "1970-01-01T00:00:00Z"}
        )
        assert syncer._pending_session() is None

        syncer._write_session({"id": "s", "media_items_set": True})
        assert syncer._pending_session() is not None

    def test_pending_session_refresh_notices_a_pick_made_elsewhere(self, tmp_path: Path) -> None:
        """The scheduled loop must spot a pick made with the dashboard closed."""
        http = FakeHttpGateway()
        http.route(
            "GET",
            f"{SESSIONS_URL}/sess-1",
            FakeResponse(json_data={"id": "sess-1", "mediaItemsSet": True}),
        )
        syncer = _make_syncer(tmp_path, http)
        syncer._write_session({"id": "sess-1", "media_items_set": False})

        # Nothing local knows about the pick yet...
        assert syncer._pending_session() is None
        # ...until the session is re-read from Google.
        assert syncer._pending_session(refresh=True) is not None

    def test_pending_session_refresh_tolerates_a_failed_poll(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())  # no routes → raises
        syncer._write_session({"id": "sess-1", "media_items_set": False})

        assert syncer._pending_session(refresh=True) is None


# ---------------------------------------------------------------------------
# Listing picked items
# ---------------------------------------------------------------------------


class TestListPickedItems:
    def test_pagination_is_followed(self, tmp_path: Path) -> None:
        pages = [
            FakeResponse(json_data={"mediaItems": [_item("1")], "nextPageToken": "p2"}),
            FakeResponse(json_data={"mediaItems": [_item("2")]}),
        ]
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, lambda _params: pages.pop(0))
        syncer = _make_syncer(tmp_path, http)

        items = syncer._list_picked_items("sess-1")

        assert [i["id"] for i in items] == ["1", "2"]
        assert http.calls[0][2] == {"sessionId": "sess-1", "pageSize": 100}
        assert http.calls[1][2]["pageToken"] == "p2"

    def test_unfinished_pick_is_reported(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, FakeResponse(status_code=400))
        syncer = _make_syncer(tmp_path, http)
        with pytest.raises(RuntimeError, match="waiting for photos"):
            syncer._list_picked_items("sess-1")

    def test_expired_session_is_reported(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, FakeResponse(status_code=403))
        syncer = _make_syncer(tmp_path, http)
        with pytest.raises(RuntimeError, match="expired"):
            syncer._list_picked_items("sess-1")


# ---------------------------------------------------------------------------
# Sync cycle
# ---------------------------------------------------------------------------


class TestSyncCycle:
    @staticmethod
    def _wire(tmp_path: Path, items: list[dict], cfg: dict | None = None):
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, FakeResponse(json_data={"mediaItems": items}))
        for item in items:
            http.route(
                "GET",
                item["mediaFile"]["baseUrl"] + ("=dv" if item["type"] == "VIDEO" else "=d"),
                FakeResponse(iter_chunks=(b"bytes-", item["id"].encode())),
            )
        syncer = _make_syncer(tmp_path, http, cfg)
        syncer._write_session({"id": "sess-1", "media_items_set": True})
        return syncer

    def test_downloads_picked_items(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a"), _item("b")])

        result = syncer.sync_once()

        assert result.success is True
        assert result.total_remote == 2
        assert result.downloaded == 2
        files = sorted(p.name for p in syncer._sync_dir.iterdir())
        assert len(files) == 2
        assert all(f.startswith("gphoto_") for f in files)
        assert (syncer._sync_dir / files[0]).read_bytes().startswith(b"bytes-")

    def test_already_downloaded_items_are_skipped(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")])
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        (syncer._sync_dir / syncer._filename_for(_item("a"))).write_bytes(b"old")

        result = syncer.sync_once()

        assert result.downloaded == 0
        assert result.skipped == 1
        # The existing file is left exactly as it was.
        assert (syncer._sync_dir / syncer._filename_for(_item("a"))).read_bytes() == b"old"

    def test_completed_import_marks_the_session(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")])
        syncer.sync_once()
        assert syncer.read_session()["imported_at"] > 0
        # Nothing left for the scheduled loop to do.
        assert syncer._pending_session() is None

    def test_result_is_persisted_for_the_dashboard(self, tmp_path: Path, runtime_dir: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")])
        syncer.sync_once()

        status_file = runtime_dir / "google_photos_sync_status.json"
        assert status_file.is_file()
        saved = json.loads(status_file.read_text())
        assert saved["downloaded"] == 1
        assert saved["session_id"] == "sess-1"
        # And it survives a fresh syncer instance.
        reloaded = _make_syncer(tmp_path, FakeHttpGateway())
        assert reloaded.get_last_result().downloaded == 1

    def test_strict_sync_removes_unpicked_files(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")], {"strict_sync": True})
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_stale.jpg"
        stray.write_bytes(b"old")

        result = syncer.sync_once()

        assert result.deleted == 1
        assert not stray.exists()

    def test_pull_only_keeps_unpicked_files(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")])
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_stale.jpg"
        stray.write_bytes(b"old")

        result = syncer.sync_once()

        assert result.deleted == 0
        assert stray.exists()

    def test_sync_adds_the_folder_to_the_watch_paths(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, [_item("a")])
        syncer.sync_once()

        paths = [p["path"] for p in syncer._state.config.sync["local"]["watch_paths"]]
        assert str(tmp_path / "gphotos") in paths

    def test_download_failure_is_reported_and_session_stays_pending(self, tmp_path: Path) -> None:
        item = _item("a")
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, FakeResponse(json_data={"mediaItems": [item]}))
        http.route("GET", item["mediaFile"]["baseUrl"] + "=d", FakeResponse(status_code=401))
        syncer = _make_syncer(tmp_path, http)
        syncer._write_session({"id": "sess-1", "media_items_set": True})

        result = syncer.sync_once()

        assert result.success is False
        assert result.downloaded == 0
        assert any("Download failed" in e for e in result.errors)
        # Not marked imported, so the scheduled loop will retry.
        assert syncer._pending_session() is not None

    def test_no_session_is_a_clear_error(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())

        result = syncer.sync_once()

        assert result.success is False
        assert "No picking session" in result.errors[0]

    def test_expired_session_is_a_clear_error(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway())
        syncer._write_session(
            {"id": "s", "media_items_set": True, "expire_time": "1970-01-01T00:00:00Z"}
        )

        result = syncer.sync_once()

        assert result.success is False
        assert "expired" in result.errors[0]

    def test_unconfigured_sync_does_not_call_google(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()  # every route raises if called
        syncer = _make_syncer(tmp_path, http, {"refresh_token": ""})

        result = syncer.sync_once()

        assert result.success is False
        assert "not connected" in result.errors[0]
        assert http.calls == []

    def test_cancellation_stops_the_download_loop(self, tmp_path: Path) -> None:
        """A cancel mid-download finishes the current file, then stops."""
        items = [_item("a"), _item("b")]
        http = FakeHttpGateway()
        http.route("GET", MEDIA_ITEMS_URL, FakeResponse(json_data={"mediaItems": items}))
        syncer = _make_syncer(tmp_path, http)
        syncer._write_session({"id": "sess-1", "media_items_set": True})

        def _responder(_params):
            syncer.cancel()
            return FakeResponse(iter_chunks=(b"bytes",))

        for item in items:
            http.route("GET", item["mediaFile"]["baseUrl"] + "=d", _responder)

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert any("Cancelled" in e for e in result.errors)
        # Not marked imported — the rest can still be fetched later.
        assert not syncer.read_session().get("imported_at")

    def test_video_items_use_the_video_download_suffix(self, tmp_path: Path) -> None:
        video = _item("v", filename="clip.mp4", mime="video/mp4", type_="VIDEO")
        syncer = self._wire(tmp_path, [video])

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert any(url.endswith("=dv") for _m, url, _p in syncer._http.calls)


# ---------------------------------------------------------------------------
# Shared-album mode
# ---------------------------------------------------------------------------


class TestSharedUrlMode:
    """``mode="shared_url"`` reads a public album page instead of a picking
    session — a live album, so every cycle re-reads it."""

    ALBUM_URL = "https://photos.google.com/share/SHARE123?key=KEY456"

    @staticmethod
    def _album_items(count: int = 2) -> list[dict]:
        return [
            {
                "id": f"AF1QipItem{i}".ljust(40, "0"),
                "type": "PHOTO",
                "mediaFile": {
                    "baseUrl": f"https://lh3.googleusercontent.com/item{i}",
                    "filename": "",
                    "mimeType": "",
                },
            }
            for i in range(count)
        ]

    def _wire(self, tmp_path: Path, items: list[dict], cfg: dict | None = None, **listing):
        """A syncer in shared-URL mode whose album reader is stubbed out."""
        import metixel.backend.sync.google_photos as gp

        http = FakeHttpGateway()
        for item in items:
            http.route(
                "GET",
                item["mediaFile"]["baseUrl"] + "=d",
                FakeResponse(iter_chunks=(b"bytes",), headers={"Content-Type": "image/jpeg"}),
            )
        overrides = {"mode": "shared_url", "shared_album_url": self.ALBUM_URL}
        overrides.update(cfg or {})
        syncer = _make_syncer(tmp_path, http, overrides)

        payload = {
            "url": self.ALBUM_URL,
            "title": "Holiday",
            "album_key": "AF1QipAlbum",
            "pages": listing.get("pages", 1),
            "truncated": listing.get("truncated", False),
            # fetch_album returns raw items; as_media_items adapts them, so the
            # stub returns already-adapted ones and the adapter is a no-op.
            "items": items,
        }

        def _fetch_album(url, **kwargs):
            assert url == self.ALBUM_URL
            on_progress = kwargs.get("on_progress")
            if on_progress:
                on_progress(len(items), payload["pages"])
            if listing.get("raise"):
                raise listing["raise"]
            return payload

        monkey = pytest.MonkeyPatch()
        monkey.setattr(gp.album, "fetch_album", _fetch_album)
        monkey.setattr(gp.album, "as_media_items", lambda raw: list(raw))
        self._monkey = monkey
        return syncer

    def teardown_method(self) -> None:
        monkey = getattr(self, "_monkey", None)
        if monkey is not None:
            monkey.undo()

    def test_configured_by_url_alone(self, tmp_path: Path) -> None:
        syncer = _make_syncer(
            tmp_path,
            FakeHttpGateway(),
            {
                "mode": "shared_url",
                "shared_album_url": self.ALBUM_URL,
                # No OAuth credentials at all.
                "client_id": "",
                "client_secret": "",
                "refresh_token": "",
            },
        )
        assert syncer.mode == "shared_url"
        assert syncer.is_configured() is True

    def test_bad_url_is_not_configured(self, tmp_path: Path) -> None:
        syncer = _make_syncer(
            tmp_path,
            FakeHttpGateway(),
            {"mode": "shared_url", "shared_album_url": "https://example.com/x"},
        )
        assert syncer.is_configured() is False

    def test_unknown_mode_falls_back_to_picker(self, tmp_path: Path) -> None:
        syncer = _make_syncer(tmp_path, FakeHttpGateway(), {"mode": "telepathy"})
        assert syncer.mode == "picker"

    def test_downloads_the_album(self, tmp_path: Path) -> None:
        items = self._album_items(2)
        syncer = self._wire(tmp_path, items)

        result = syncer.sync_once()

        assert result.success is True
        assert result.total_remote == 2
        assert result.downloaded == 2
        assert result.album_title == "Holiday"
        files = sorted(p.name for p in syncer._sync_dir.iterdir())
        assert len(files) == 2
        # The extension came from the download's Content-Type.
        assert all(f.endswith(".jpg") for f in files)

    def test_no_oauth_token_is_requested(self, tmp_path: Path, fake_token) -> None:
        """A public album needs no credentials, so none are minted."""
        syncer = self._wire(
            tmp_path,
            self._album_items(1),
            {"client_id": "", "client_secret": "", "refresh_token": ""},
        )

        result = syncer.sync_once()

        assert result.downloaded == 1
        assert fake_token == []

    def test_second_run_downloads_nothing_new(self, tmp_path: Path) -> None:
        items = self._album_items(2)
        syncer = self._wire(tmp_path, items)
        syncer.sync_once()

        again = syncer.sync_once()

        assert again.downloaded == 0
        assert again.skipped == 2

    def test_new_album_photos_arrive_on_the_next_cycle(self, tmp_path: Path) -> None:
        """The live-album behaviour the picker mode cannot offer."""
        syncer = self._wire(tmp_path, self._album_items(1))
        syncer.sync_once()
        self.teardown_method()

        grown = self._wire(tmp_path, self._album_items(3))
        result = grown.sync_once()

        assert result.downloaded == 2
        assert len(list(grown._sync_dir.iterdir())) == 3

    def test_truncated_album_is_reported_but_still_downloaded(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, self._album_items(2), pages=200, truncated=True)

        result = syncer.sync_once()

        assert result.downloaded == 2
        assert result.album_pages == 200
        assert any("could be read" in e for e in result.errors)
        assert result.success is False

    def test_unreadable_album_fails_clearly(self, tmp_path: Path) -> None:
        from metixel.backend.sync.google_photos_album import SharedAlbumError

        syncer = self._wire(
            tmp_path,
            self._album_items(1),
            **{"raise": SharedAlbumError("No photos found on the album page")},
        )

        result = syncer.sync_once()

        assert result.success is False
        assert "No photos found" in result.errors[0]
        assert result.downloaded == 0

    def test_strict_sync_mirrors_the_album(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, self._album_items(1), {"strict_sync": True})
        syncer._sync_dir.mkdir(parents=True, exist_ok=True)
        stray = syncer._sync_dir / "gphoto_gone.jpg"
        stray.write_bytes(b"old")

        result = syncer.sync_once()

        assert result.deleted == 1
        assert not stray.exists()

    def test_no_picking_session_is_touched(self, tmp_path: Path) -> None:
        syncer = self._wire(tmp_path, self._album_items(1))
        syncer.sync_once()
        assert syncer.read_session() is None

    def test_html_response_is_refused(self, tmp_path: Path) -> None:
        """A sign-in page must never be written out as a photo."""
        items = self._album_items(1)
        syncer = self._wire(tmp_path, items)
        syncer._http.route(
            "GET",
            items[0]["mediaFile"]["baseUrl"] + "=d",
            FakeResponse(iter_chunks=(b"<html>",), headers={"Content-Type": "text/html"}),
        )

        result = syncer.sync_once()

        assert result.downloaded == 0
        assert any("instead of media" in e for e in result.errors)
        assert list(syncer._sync_dir.iterdir()) == []


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
