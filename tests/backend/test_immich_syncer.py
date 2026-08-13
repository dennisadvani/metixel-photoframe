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
    def _make_syncer(tmp_path: Path, http: FakeHttpGateway):
        from metixel.backend.state import StateManager
        from metixel.backend.sync.immich import ImmichSyncer
        from metixel.shared.config import Config

        config = Config()
        # Empty base URL so requests stay relative (no network in tests).
        config.update("sync", {"immich": {"server_url": ""}})
        config_path = tmp_path / "config.json"
        config.save(config_path)

        state = StateManager(config_path, tmp_path / "run")
        return ImmichSyncer(state, http=http)

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

        syncer._sync_dir = tmp_path
        # Skip the disk-space gate (uses os.statvfs, which is Unix-only).
        monkeypatch.setattr(syncer, "_check_disk_space", lambda _p: None)

        syncer._download_asset({"id": "asset-1", "originalPath": "/x/photo.jpg"}, "out.jpg")

        assert (tmp_path / "out.jpg").read_bytes() == b"part1part2"
