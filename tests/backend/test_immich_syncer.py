# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""ImmichSyncer — unit tests driven through the ``HttpGateway`` port.

The real client talks to the Immich REST API via ``requests``; here a
``FakeHttpGateway`` (a duck-typed ``HttpGateway`` implementation) serves
canned responses, so no network access is required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FakeResponse:
    """Implements the ``HttpResponse`` port surface."""

    def __init__(
        self,
        json_data: Any = None,
        status_code: int = 200,
        text: str = "",
        iter_chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.json_data = json_data
        self.status_code = status_code
        self.text = text
        self._iter_chunks = iter_chunks

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

    def __exit__(self, *args: Any) -> bool:
        return False


class FakeHttpGateway:
    """Configurable ``HttpGateway`` fake with per-route canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._routes: dict[tuple[str, str], Any] = {}

    def route(self, method: str, url: str, response: Any) -> None:
        self._routes[(method, url)] = response

    def get(
        self,
        url,
        *,
        headers=None,
        params=None,
        stream: bool = False,
        timeout=None,
    ):
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


class TestImmichSyncer:
    @staticmethod
    def _make_syncer(tmp_path: Path, http: FakeHttpGateway, immich: dict | None = None):
        from metixel.backend.state import StateManager
        from metixel.backend.sync.immich import ImmichSyncer
        from metixel.shared.config import Config

        config = Config()
        # Empty base URL so requests stay relative (no network in tests).
        # sync_dir points at tmp_path so nothing touches /opt/metixel.
        overrides: dict[str, Any] = dict(immich or {})
        overrides.setdefault("server_url", "")
        overrides.setdefault("api_key", "test-key")
        overrides.setdefault("sync_dir", str(tmp_path))
        config.update("sync", {"immich": overrides})
        config_path = tmp_path / "config.json"
        config.save(config_path)

        state = StateManager(config_path, tmp_path / "run")
        return ImmichSyncer(state, http=http)

    @staticmethod
    def _route_assets(http: FakeHttpGateway, assets: list[dict[str, Any]]) -> None:
        """Route POST /api/search/metadata to return a single page of assets."""
        http.route(
            "POST",
            "/api/search/metadata",
            FakeResponse(json_data={"assets": {"items": assets, "nextPage": None}}),
        )

    # -- Album resolution ----------------------------------------------------

    def test_resolve_album_id(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route(
            "GET", "/api/albums", FakeResponse(json_data=[{"id": "abc", "albumName": "Holiday"}])
        )
        syncer = self._make_syncer(tmp_path, http)
        assert syncer._resolve_album_id("Holiday") == "abc"

    def test_resolve_album_id_case_insensitive(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route(
            "GET", "/api/albums", FakeResponse(json_data=[{"id": "abc", "albumName": "Holiday"}])
        )
        syncer = self._make_syncer(tmp_path, http)
        assert syncer._resolve_album_id("holiday") == "abc"

    def test_resolve_album_id_not_found(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route(
            "GET", "/api/albums", FakeResponse(json_data=[{"id": "abc", "albumName": "Other"}])
        )
        syncer = self._make_syncer(tmp_path, http)
        assert syncer._resolve_album_id("Holiday") is None

    def test_list_albums(self, tmp_path: Path) -> None:
        http = FakeHttpGateway()
        http.route("GET", "/api/albums", FakeResponse(json_data=[{"id": "abc"}]))
        syncer = self._make_syncer(tmp_path, http)
        assert syncer._list_albums() == [{"id": "abc"}]

    # -- Asset pagination ----------------------------------------------------

    def test_fetch_album_assets_paginated(self, tmp_path: Path) -> None:
        pages = [
            FakeResponse(json_data={"assets": {"items": [{"id": "1"}], "nextPage": 1}}),
            FakeResponse(json_data={"assets": {"items": [{"id": "2"}], "nextPage": None}}),
        ]

        def responder(_body: Any) -> FakeResponse:
            return pages.pop(0)

        http = FakeHttpGateway()
        http.route("POST", "/api/search/metadata", responder)
        syncer = self._make_syncer(tmp_path, http)

        assets = syncer._fetch_album_assets("album-1")

        assert [a["id"] for a in assets] == ["1", "2"]
        # First request has no page; the second carries page=1.
        assert http.calls[0][2] == {"albumIds": ["album-1"]}
        assert http.calls[1][2] == {"albumIds": ["album-1"], "page": 1}

    # -- Streaming download --------------------------------------------------

    def test_download_asset_streams_to_disk(self, tmp_path: Path, monkeypatch) -> None:
        from metixel.shared.ports import HttpGateway

        http = FakeHttpGateway()
        http.route(
            "GET",
            "/api/assets/asset-1/original",
            FakeResponse(status_code=200, iter_chunks=(b"part1", b"part2")),
        )
        syncer = self._make_syncer(tmp_path, http)
        assert isinstance(http, HttpGateway)

        # Skip the disk-space gate (uses os.statvfs, which is Unix-only).
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        syncer._download_asset({"id": "asset-1", "originalPath": "/x/photo.jpg"}, "out.jpg")

        assert (tmp_path / "out.jpg").read_bytes() == b"part1part2"

    # -- Multi-album sync ---------------------------------------------------

    def test_sync_one_album_downloads_into_album_folder(self, tmp_path: Path, monkeypatch) -> None:
        """Assets land in <sync_dir>/album_<id>/ not the sync root."""
        http = FakeHttpGateway()
        self._route_assets(http, [{"id": "a1", "originalPath": "/x/1.jpg"}])
        http.route(
            "GET", "/api/assets/a1/original",
            FakeResponse(status_code=200, iter_chunks=(b"DATA",)),
        )
        syncer = self._make_syncer(
            tmp_path, http, immich={"albums": [{"id": "abc", "name": "Holiday"}]}
        )
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        result = syncer._do_sync()

        assert result.success
        assert len(result.albums) == 1
        album_dir = tmp_path / "album_abc"
        assert (album_dir / "immich_a1.jpg").read_bytes() == b"DATA"
        # Nothing in the sync root itself
        assert not (tmp_path / "immich_a1.jpg").exists()

    def test_sync_multiple_albums_aggregated(self, tmp_path: Path, monkeypatch) -> None:
        """Two albums sync into separate folders; results are aggregated."""
        http = FakeHttpGateway()
        self._route_assets(http, [{"id": "a1", "originalPath": "/x/1.jpg"}])
        for asset_id in ("a1", "b1"):
            http.route(
                "GET", f"/api/assets/{asset_id}/original",
                FakeResponse(status_code=200, iter_chunks=(b"X",)),
            )
        syncer = self._make_syncer(
            tmp_path,
            http,
            immich={
                "albums": [
                    {"id": "A", "name": "Alpha"},
                    {"id": "B", "name": "Beta"},
                ]
            },
        )
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        result = syncer._do_sync()

        assert result.success
        assert len(result.albums) == 2
        assert (tmp_path / "album_A" / "immich_a1.jpg").exists()
        assert (tmp_path / "album_B" / "immich_a1.jpg").exists()
        assert result.downloaded == 2
        assert result.total_remote == 2
        assert all(a["success"] for a in result.albums)

    def test_sync_album_not_found_keeps_local_files(self, tmp_path: Path) -> None:
        """A deleted/unresolvable album errors but never deletes local files."""
        http = FakeHttpGateway()
        http.route("GET", "/api/albums", FakeResponse(json_data=[]))
        syncer = self._make_syncer(
            tmp_path, http, immich={"albums": [{"id": "", "name": "Missing"}]}
        )

        # Pre-seed a local file that must survive.
        (tmp_path / "album_missing").mkdir(parents=True)
        (tmp_path / "album_missing" / "keep.jpg").write_bytes(b"KEEP")

        result = syncer._do_sync()

        assert not result.success
        assert len(result.errors) >= 1
        assert (tmp_path / "album_missing" / "keep.jpg").exists()

    def test_strict_sync_deletes_only_album_folder(self, tmp_path: Path, monkeypatch) -> None:
        """Strict mode only touches files inside the album folder, never the root."""
        http = FakeHttpGateway()
        self._route_assets(http, [{"id": "a1", "originalPath": "/x/1.jpg"}])
        http.route(
            "GET", "/api/assets/a1/original",
            FakeResponse(status_code=200, iter_chunks=(b"NEW",)),
        )
        syncer = self._make_syncer(
            tmp_path,
            http,
            immich={"albums": [{"id": "abc", "name": "Holiday"}], "strict_sync": True},
        )
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        album_dir = tmp_path / "album_abc"
        album_dir.mkdir(parents=True)
        (album_dir / "immich_stale.jpg").write_bytes(b"STALE")
        (tmp_path / "root_stray.jpg").write_bytes(b"ROOT")

        result = syncer._do_sync()

        assert result.success
        assert not (album_dir / "immich_stale.jpg").exists()  # deleted by strict
        assert (tmp_path / "root_stray.jpg").exists()          # root untouched
        assert (album_dir / "immich_a1.jpg").exists()

    # -- Legacy single-album migration --------------------------------------

    def test_migrate_legacy_layout_moves_root_files(self, tmp_path: Path, monkeypatch) -> None:
        """Old flat files move into album_<id> and config is rewritten."""
        http = FakeHttpGateway()
        http.route(
            "GET", "/api/albums",
            FakeResponse(json_data=[{"id": "abc", "albumName": "Holiday"}]),
        )
        self._route_assets(http, [{"id": "a1", "originalPath": "/x/1.jpg"}])
        http.route(
            "GET", "/api/assets/a1/original",
            FakeResponse(status_code=200, iter_chunks=(b"NEW",)),
        )
        syncer = self._make_syncer(tmp_path, http, immich={"album_name": "Holiday"})
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        # Pre-seed the old flat layout: files directly in the sync root.
        (tmp_path / "immich_old.jpg").write_bytes(b"OLD")

        result = syncer._do_sync()

        assert result.success
        # Old file moved into the per-album folder.
        album_dir = tmp_path / "album_abc"
        assert (album_dir / "immich_old.jpg").exists()
        # Config rewritten: albums list set, legacy key removed.
        cfg = syncer._state.config
        assert "album_name" not in cfg.sync["immich"]
        assert {"id": "abc", "name": "Holiday"} in cfg.sync["immich"]["albums"]

    def test_migrate_legacy_layout_empty_name_drops_key(self, tmp_path: Path) -> None:
        """An empty legacy album_name is dropped without touching files."""
        http = FakeHttpGateway()
        syncer = self._make_syncer(tmp_path, http, immich={"album_name": ""})

        (tmp_path / "stray.jpg").write_bytes(b"STAY")

        syncer._do_sync()

        cfg = syncer._state.config
        assert "album_name" not in cfg.sync["immich"]
        assert (tmp_path / "stray.jpg").exists()
