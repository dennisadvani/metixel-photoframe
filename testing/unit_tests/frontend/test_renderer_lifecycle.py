# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for FrontendRenderer shutdown and playlist hot-reload robustness."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metixel.frontend.renderer import FrontendRenderer


@pytest.fixture
def renderer(tmp_path: Path, monkeypatch) -> FrontendRenderer:
    from metixel.shared.config import Config

    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    config_path = tmp_path / "config.json"
    Config().save(config_path)
    r = FrontendRenderer(config_path=config_path)
    r._ipc_server = mock.MagicMock()
    return r


class TestShutdown:
    def test_stops_running_video_before_destroying_backend(self, renderer):
        """A VLC subprocess is not owned by the display backend — it must be
        killed explicitly or it outlives the frontend restart."""
        calls: list[str] = []
        presentation = mock.MagicMock()
        presentation._video_stop.side_effect = lambda: calls.append("video_stop")
        backend = mock.MagicMock()
        backend.destroy.side_effect = lambda: calls.append("destroy")
        renderer._presentation = presentation
        renderer._backend = backend

        renderer._shutdown()

        assert calls == ["video_stop", "destroy"]
        assert renderer._backend is None

    def test_video_stop_failure_does_not_abort_shutdown(self, renderer):
        presentation = mock.MagicMock()
        presentation._video_stop.side_effect = RuntimeError("boom")
        backend = mock.MagicMock()
        renderer._presentation = presentation
        renderer._backend = backend

        renderer._shutdown()  # must not raise

        backend.destroy.assert_called_once()

    def test_shutdown_without_presentation(self, renderer):
        renderer._presentation = None
        renderer._backend = mock.MagicMock()
        renderer._shutdown()
        assert renderer._backend is None


class TestPlaylistHotReload:
    def _write_playlist(self, renderer: FrontendRenderer, entries: list[dict]) -> None:
        renderer._playlist_path.parent.mkdir(parents=True, exist_ok=True)
        renderer._playlist_path.write_text(json.dumps(entries), encoding="utf-8")
        renderer._playlist_mtime = 0.0

    def test_malformed_entry_with_wrong_type_is_skipped(self, renderer, tmp_path):
        """A ``TypeError`` from a bad field type must not take down the loop
        (``_load_backend_playlist`` already tolerated it; the hot-reload path
        did not)."""
        good = {
            "id": "a",
            "original_path": str(tmp_path / "a.jpg"),
            "cached_path": str(tmp_path / "a.jpg"),
            "media_type": "image",
            "width": 100,
            "height": 100,
        }
        bad = dict(good, id="b", thumbnail_path=12345)  # Path(12345) → TypeError
        self._write_playlist(renderer, [bad, good])
        presentation = mock.MagicMock()
        presentation._queue = []
        renderer._presentation = presentation

        renderer._check_playlist_changed()

        presentation.set_queue.assert_called_once()
        items = presentation.set_queue.call_args[0][0]
        assert [i.id for i in items] == ["a"]

    def test_bad_enum_value_is_skipped(self, renderer, tmp_path):
        good = {
            "id": "a",
            "original_path": str(tmp_path / "a.jpg"),
            "cached_path": str(tmp_path / "a.jpg"),
            "media_type": "image",
        }
        bad = dict(good, id="b", media_type="hologram")  # ValueError
        self._write_playlist(renderer, [bad, good])
        presentation = mock.MagicMock()
        presentation._queue = []
        renderer._presentation = presentation

        renderer._check_playlist_changed()

        items = presentation.set_queue.call_args[0][0]
        assert [i.id for i in items] == ["a"]

    def test_load_backend_playlist_skips_wrong_types(self, renderer, tmp_path, monkeypatch):
        good = {
            "id": "a",
            "original_path": str(tmp_path / "a.jpg"),
            "cached_path": str(tmp_path / "a.jpg"),
            "media_type": "image",
        }
        bad = dict(good, id="b", transcode_status="nonsense")  # ValueError
        self._write_playlist(renderer, [bad, good])

        items = FrontendRenderer._load_backend_playlist()

        assert [i.id for i in items] == ["a"]
