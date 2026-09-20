# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for PresentationEngine queue bookkeeping, preload safety and controls.

Covers the shared-state invariants between the presentation mixins:

- ``remove_items`` keeps ``_current_idx`` on the item that is actually
  displayed and stops a video that was removed while playing.
- ``reload_config`` re-filters the backend playlist (``_all_items``) on a
  playback toggle instead of rescanning the media folder.
- The preload pipeline never tags a decoded result with an item it does not
  belong to, and never leaves a stale worker blocking future preloads.
- ``pause``/``resume`` reach VLC in every non-idle video state.
- The layout cache is keyed by values, not object identity.
- ``_video_stop`` survives a stuck VLC.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from metixel.frontend.presentation.engine import PresentationEngine
from metixel.frontend.presentation.video_state import (
    _VIDEO_IDLE,
    _VIDEO_PLAYING,
    _VIDEO_SWAPPED,
    _VIDEO_WAITING,
)
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus


def _jpeg(path: Path, size: tuple[int, int] = (16, 16)) -> Path:
    Image.new("RGB", size, color=(0, 128, 255)).save(path, "JPEG")
    return path


def _image(item_id: str, path: Path, width: int = 1920, height: int = 1080) -> MediaItem:
    if not path.exists():
        _jpeg(path)
    return MediaItem(
        id=item_id,
        original_path=path,
        cached_path=path,
        media_type=MediaType.IMAGE,
        width=width,
        height=height,
    )


def _video(item_id: str, tmp_path: Path) -> MediaItem:
    first = _jpeg(tmp_path / f"{item_id}.1.frame.jpg")
    last = _jpeg(tmp_path / f"{item_id}.2.frame.jpg")
    return MediaItem(
        id=item_id,
        original_path=tmp_path / f"{item_id}.mp4",
        cached_path=tmp_path / f"{item_id}.mp4",
        media_type=MediaType.VIDEO,
        width=1920,
        height=1080,
        duration_seconds=5.0,
        transcode_status=TranscodeStatus.TRANSCODED,
        first_frame_path=first,
        last_frame_path=last,
    )


def _join_preload(engine: PresentationEngine) -> None:
    thread = engine._preload_thread
    if thread is not None:
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "preload worker did not finish"


@pytest.fixture
def backend():
    b = mock.MagicMock()
    b.width = 1920
    b.height = 1080
    b.supports_video = True
    b.gpu_memory_info.return_value = None
    return b


@pytest.fixture
def config():
    cfg = Config()
    cfg.update("slideshow", {"shuffle": False})
    cfg.update("video", {"playback_enabled": True})
    return cfg


@pytest.fixture
def engine(config, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return PresentationEngine(config, backend)


# ---------------------------------------------------------------------------
# remove_items
# ---------------------------------------------------------------------------


class TestRemoveItems:
    def test_removing_item_before_current_keeps_current_item(self, engine, tmp_path):
        """Removing an earlier item shifts the index so the same item stays current."""
        a, b, c = (_image(n, tmp_path / f"{n}.jpg") for n in "abc")
        engine.set_queue([a, b, c])
        _join_preload(engine)
        engine.next_item()  # now on b (index 1)
        assert engine._queue[engine._current_idx].id == "b"

        removed = engine.remove_items({"a"})

        assert removed == 1
        assert engine._current_idx == 0
        assert engine._queue[engine._current_idx].id == "b"

    def test_removing_items_before_and_after_current(self, engine, tmp_path):
        a, b, c, d = (_image(n, tmp_path / f"{n}.jpg") for n in "abcd")
        engine.set_queue([a, b, c, d])
        _join_preload(engine)
        engine.next_item()
        engine.next_item()  # on c (index 2)

        engine.remove_items({"a", "d"})

        assert [i.id for i in engine._queue] == ["b", "c"]
        assert engine._queue[engine._current_idx].id == "c"

    def test_removing_current_item_advances_to_successor(self, engine, tmp_path):
        """Existing behaviour: the item that slid into the slot is shown."""
        a, b, c = (_image(n, tmp_path / f"{n}.jpg") for n in "abc")
        engine.set_queue([a, b, c])
        _join_preload(engine)
        engine.next_item()  # on b

        engine.remove_items({"b"})

        assert engine._queue[engine._current_idx].id == "c"
        assert engine._tex == [None, None]

    def test_removing_current_item_at_end_wraps_to_start(self, engine, tmp_path):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        engine.set_queue([a, b])
        _join_preload(engine)
        engine.next_item()  # on b (last)

        engine.remove_items({"b"})

        assert engine._current_idx == 0
        assert engine._queue[0].id == "a"

    def test_removing_playing_video_stops_vlc(self, engine, tmp_path):
        a = _image("a", tmp_path / "a.jpg")
        v = _video("v", tmp_path)
        engine.set_queue([a, v])
        _join_preload(engine)
        engine.next_item()  # on v — _video_launch is real; make it look running
        engine._video_state = _VIDEO_PLAYING
        engine._video_item = v
        engine._video_proc = mock.MagicMock()

        with mock.patch.object(engine, "_video_stop", wraps=engine._video_stop) as stop:
            engine.remove_items({"v"})

        stop.assert_called_once()
        assert engine._video_state == _VIDEO_IDLE
        assert [i.id for i in engine._queue] == ["a"]

    def test_removing_other_item_does_not_stop_video(self, engine, tmp_path):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        v = _video("v", tmp_path)
        engine.set_queue([v, a, b])
        _join_preload(engine)
        engine._video_state = _VIDEO_PLAYING
        engine._video_item = v
        engine._video_proc = mock.MagicMock()

        with mock.patch.object(engine, "_video_stop") as stop:
            engine.remove_items({"b"})

        stop.assert_not_called()

    def test_removed_item_is_dropped_from_inactive_slot(self, engine, tmp_path, backend):
        """A preloaded texture of a removed item must not be promoted later."""
        a, b, c = (_image(n, tmp_path / f"{n}.jpg") for n in "abc")
        engine.set_queue([a, b, c])
        _join_preload(engine)
        engine._upload_pending_preload()  # b now in the inactive slot
        assert engine._tex_item[engine._inactive] is b
        inactive_tex = engine._tex[engine._inactive]

        engine.remove_items({"b"})

        backend.unload_texture.assert_any_call(inactive_tex)
        assert engine._tex_item[engine._inactive] is not b

    def test_remove_prunes_unfiltered_playlist(self, engine, tmp_path):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        engine.set_queue([a, b])
        engine.remove_items({"a"})
        assert [i.id for i in engine._all_items] == ["b"]


# ---------------------------------------------------------------------------
# reload_config — playback toggle
# ---------------------------------------------------------------------------


class TestReloadConfigVideoToggle:
    def test_toggle_refilters_backend_playlist_not_folder_scan(self, engine, tmp_path):
        a = _image("a", tmp_path / "a.jpg", width=4000, height=3000)
        v = _video("v", tmp_path)
        engine.set_queue([a, v])
        assert [i.id for i in engine._queue] == ["a", "v"]

        off = Config()
        off.update("slideshow", {"shuffle": False})
        off.update("video", {"playback_enabled": False})
        with mock.patch.object(engine, "scan_folder") as scan:
            engine.reload_config(off)

        scan.assert_not_called()
        assert [i.id for i in engine._queue] == ["a"]
        # The backend item (with its real dimensions) survives — not a stub.
        assert engine._queue[0].width == 4000
        # The unfiltered playlist still remembers the video.
        assert [i.id for i in engine._all_items] == ["a", "v"]

        on = Config()
        on.update("slideshow", {"shuffle": False})
        on.update("video", {"playback_enabled": True})
        with mock.patch.object(engine, "scan_folder") as scan:
            engine.reload_config(on)

        scan.assert_not_called()
        assert [i.id for i in engine._queue] == ["a", "v"]

    def test_add_items_records_filtered_videos_for_later_toggle(self, engine, tmp_path):
        """A video filtered out by add_items reappears when playback is enabled."""
        engine.reload_config(_config_with_playback(False))
        a = _image("a", tmp_path / "a.jpg")
        v = _video("v", tmp_path)
        engine.set_queue([a])
        added = engine.add_items([v])
        assert added == 0
        assert [i.id for i in engine._all_items] == ["a", "v"]

        engine.reload_config(_config_with_playback(True))

        assert [i.id for i in engine._queue] == ["a", "v"]

    def test_no_toggle_leaves_queue_alone(self, engine, tmp_path):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        engine.set_queue([a, b])
        engine.next_item()
        with mock.patch.object(engine, "set_queue") as set_queue:
            engine.reload_config(_config_with_playback(True))
        set_queue.assert_not_called()
        assert engine._current_idx == 1


def _config_with_playback(enabled: bool) -> Config:
    cfg = Config()
    cfg.update("slideshow", {"shuffle": False})
    cfg.update("video", {"playback_enabled": enabled})
    return cfg


class TestPlaybackEnabledHelper:
    """Every gate resolves ``video.playback_enabled`` over the legacy key."""

    def test_new_key_wins_over_legacy(self, backend):
        cfg = Config()
        cfg.update("slideshow", {"video_playback_enabled": True})
        cfg.update("video", {"playback_enabled": False})
        assert PresentationEngine._video_playback_enabled(cfg) is False

        cfg2 = Config()
        cfg2.update("slideshow", {"video_playback_enabled": False})
        cfg2.update("video", {"playback_enabled": True})
        assert PresentationEngine._video_playback_enabled(cfg2) is True

    def test_video_launch_respects_new_key(self, engine, tmp_path):
        engine.reload_config(_config_with_playback(False))
        v = _video("v", tmp_path)
        with mock.patch.object(engine, "_video_launch_vlc") as launch:
            engine._video_launch(v)
        launch.assert_not_called()


# ---------------------------------------------------------------------------
# Preload safety
# ---------------------------------------------------------------------------


class TestPreloadSafety:
    def test_stale_result_is_discarded_and_correct_item_reloaded(self, engine, tmp_path, backend):
        """A result decoded for the old "next" item must not be tagged as the new one."""
        a, b, c = (_image(n, tmp_path / f"{n}.jpg") for n in "abc")
        engine.set_queue([a, b, c])
        _join_preload(engine)
        assert engine._preload_cache_key == str(b.cached_path)

        # The queue changes under the pending result: x is now next.
        x = _image("x", tmp_path / "x.jpg")
        engine._queue.insert(1, x)
        backend.load_texture.reset_mock()

        engine._upload_pending_preload()

        # Stale pixels were not uploaded, and a fresh decode for x started.
        backend.load_texture.assert_not_called()
        assert engine._tex_item[engine._inactive] is None
        assert engine._preload_target is x
        _join_preload(engine)

        engine._upload_pending_preload()

        backend.load_texture.assert_called_once()
        assert engine._tex_item[engine._inactive] is x

    def test_matching_result_is_uploaded(self, engine, tmp_path, backend):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        engine.set_queue([a, b])
        _join_preload(engine)
        backend.load_texture.reset_mock()

        engine._upload_pending_preload()

        backend.load_texture.assert_called_once()
        assert engine._tex_item[engine._inactive] is b

    def test_stale_alive_worker_is_cancelled_and_replaced(self, engine, tmp_path):
        a, b, c = (_image(n, tmp_path / f"{n}.jpg") for n in "abc")
        engine._queue = [a, b, c]
        engine._current_idx = 0

        # A worker for c is "still decoding" (blocked on an event).
        release = threading.Event()
        stale = threading.Thread(target=release.wait, daemon=True)
        stale.start()
        stale_cancel = threading.Event()
        engine._preload_thread = stale
        engine._preload_target = c
        engine._preload_cancel = stale_cancel
        try:
            engine._preload_into_inactive()  # wants b, not c

            assert stale_cancel.is_set()
            assert engine._preload_thread is not stale
            assert engine._preload_target is b
            _join_preload(engine)
            assert engine._preload_cache_key == str(b.cached_path)
        finally:
            release.set()

    def test_alive_worker_for_same_item_is_left_alone(self, engine, tmp_path):
        a, b = (_image(n, tmp_path / f"{n}.jpg") for n in "ab")
        engine._queue = [a, b]
        engine._current_idx = 0
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        cancel = threading.Event()
        engine._preload_thread = worker
        engine._preload_target = b
        engine._preload_cancel = cancel
        try:
            engine._preload_into_inactive()
            assert engine._preload_thread is worker
            assert not cancel.is_set()
        finally:
            release.set()

    def test_cancelled_worker_discards_its_result(self, engine, tmp_path):
        a = _image("a", tmp_path / "a.jpg")
        cancel = threading.Event()
        cancel.set()

        engine._preload_worker(a, cancel)

        assert engine._preload_array is None
        assert engine._preload_cache_key == ""

    def test_worker_never_touches_texture_slots(self, engine, tmp_path):
        """A failed video preload must leave ``_tex`` to the main thread."""
        v = _video("v", tmp_path)
        v.first_frame_path = None  # nothing to decode
        sentinel = object()
        engine._tex[engine._inactive] = sentinel

        engine._preload_worker(v, threading.Event())

        assert engine._tex[engine._inactive] is sentinel
        assert engine._preload_array is None

    def test_upload_with_empty_queue_is_noop(self, engine, tmp_path, backend):
        a = _image("a", tmp_path / "a.jpg")
        engine._preload_worker(a, None)
        assert engine._preload_array is not None
        engine._queue = []
        engine._current_idx = -1

        engine._upload_pending_preload()

        backend.load_texture.assert_not_called()


# ---------------------------------------------------------------------------
# pause / resume during video playback
# ---------------------------------------------------------------------------


class TestPauseResumeVideo:
    @pytest.mark.parametrize("state", [_VIDEO_WAITING, _VIDEO_PLAYING, _VIDEO_SWAPPED])
    def test_pause_stops_vlc_in_every_running_state(self, engine, state):
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.poll.return_value = None  # alive
        engine._video_state = state
        engine._video_proc = proc

        with mock.patch("metixel.frontend.presentation.scheduler.os.kill") as kill:
            engine.pause()

        assert engine._paused is True
        assert engine._video_paused is True
        kill.assert_called_once()
        assert kill.call_args[0][0] == 4321

    def test_pause_does_not_signal_idle_or_exited_vlc(self, engine):
        proc = mock.MagicMock()
        proc.poll.return_value = 0  # already exited
        engine._video_state = _VIDEO_SWAPPED
        engine._video_proc = proc

        with mock.patch("metixel.frontend.presentation.scheduler.os.kill") as kill:
            engine.pause()

        kill.assert_not_called()
        assert engine._video_paused is False

    def test_resume_continues_vlc_after_swap(self, engine):
        proc = mock.MagicMock()
        proc.pid = 4321
        proc.poll.return_value = None
        engine._video_state = _VIDEO_SWAPPED
        engine._video_proc = proc
        with mock.patch("metixel.frontend.presentation.scheduler.os.kill") as kill:
            engine.pause()
            engine.resume()

        assert kill.call_count == 2
        assert engine._video_paused is False
        assert engine._paused is False


# ---------------------------------------------------------------------------
# Layout cache
# ---------------------------------------------------------------------------


class TestLayoutCache:
    def test_in_place_dimension_update_invalidates_layout(self, engine, tmp_path, backend):
        item = _image("a", tmp_path / "a.jpg", width=1920, height=1080)
        engine._queue = [item]
        engine._current_idx = 0
        engine._tex[0] = "tex"
        engine._tex_item[0] = item

        engine._render_item(item, 1.0)
        landscape_rect = backend.draw_image.call_args[0][1:5]

        # Renderer hot-reload mutates the item in place (same id()).
        item.width, item.height = 1080, 1920
        engine._render_item(item, 1.0)
        portrait_rect = backend.draw_image.call_args[0][1:5]

        assert landscape_rect == (0, 0, 1920, 1080)
        assert portrait_rect != landscape_rect
        assert portrait_rect[2] < 1920  # pillarboxed

    def test_cache_is_bounded(self, engine, tmp_path, backend):
        engine._tex[0] = "tex"
        engine._current_idx = 0
        for i in range(40):
            item = _image(f"i{i}", tmp_path / f"{i}.jpg", width=1000 + i, height=1000)
            engine._queue = [item]
            engine._tex_item[0] = item
            engine._render_item(item, 1.0)
        assert len(engine._layout_cache) == 16


# ---------------------------------------------------------------------------
# _video_stop robustness
# ---------------------------------------------------------------------------


class TestVideoStop:
    def test_survives_vlc_that_ignores_kill(self, engine):
        proc = mock.MagicMock()
        proc.pid = 99
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="vlc", timeout=1.0)
        engine._video_state = _VIDEO_PLAYING
        engine._video_proc = proc

        engine._video_stop()  # must not raise

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert engine._video_state == _VIDEO_IDLE
        assert engine._video_proc is None
