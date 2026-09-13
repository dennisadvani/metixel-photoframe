# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for :class:`metixel.frontend.presentation.presenter.Presenter`.

Replaces the old ``test_engine_video.py``, which tested a mixin engine and a VLC
subprocess that no longer exist.  What is worth keeping from it and is covered
here:

* video items must survive a queue set and reach the backend's player;
* a video's item duration honours the configured cap;
* ``current_media.json`` is republished on queue changes, because the dashboard's
  "Now Playing" card reads it via ``/api/health``;
* **stall-and-hold**: the slide is held rather than cut to an empty frame when the
  next item is not ready.

The backend is a fake, so these run on a machine with no display, no Qt and no mpv.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from metixel.framing.layout import RenderPlan
from metixel.frontend.presentation.presenter import STALL_TIMEOUT_S, Presenter
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus


class FakeBackend:
    """Minimal stand-in for a DisplayBackend.

    Records what it was asked to present so tests can assert on the frame rather
    than on internal state, which is the property that actually matters.
    """

    def __init__(self, *, width: int = 1920, height: int = 1200, video: bool = True) -> None:
        self.width = width
        self.height = height
        self._supports_video = video
        self.presented: list[tuple[RenderPlan, object, float]] = []
        self.played: list[Path] = []
        self.stopped = 0
        self.paused: list[bool] = []
        self.loaded: list[object] = []
        self._running = True
        self._video_playing = False
        self._video_finished = False

    # -- Properties ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def supports_video(self) -> bool:
        return self._supports_video

    # -- Frame ---------------------------------------------------------------

    def present(self, plan: RenderPlan, image: object = None, alpha: float = 1.0) -> None:
        self.presented.append((plan, image, alpha))

    def load_image(self, path: object) -> object:
        # A non-None handle means "the item is displayable", which is what the
        # presenter keys off.  Never fails, so tests control readiness via
        # dimensions rather than by simulating IO errors.
        self.loaded.append(path)
        return f"image:{path}"

    def unload_image(self, handle: object) -> None:
        pass

    # -- Video ---------------------------------------------------------------

    def play_video(self, path: Path, plan: RenderPlan) -> bool:
        if not self._supports_video:
            return False
        self.played.append(path)
        self._video_playing = True
        self._video_finished = False
        return True

    def stop_video(self) -> None:
        self.stopped += 1
        self._video_playing = False

    def pause_video(self, paused: bool = True) -> None:
        self.paused.append(paused)

    def video_playing(self) -> bool:
        return self._video_playing

    def video_finished(self) -> bool:
        return self._video_finished

    # -- Test helpers --------------------------------------------------------

    def finish_video(self) -> None:
        """Simulate the stream reaching its end."""
        self._video_playing = False
        self._video_finished = True

    def other_methods(self) -> None:
        """Placeholder so the fake reads as intentionally minimal."""

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        pass

    def clear(self) -> None:
        pass

    def display_power(self, on: bool) -> None:
        pass


def _image_item(item_id: str, path: Path, w: int = 3000, h: int = 2000) -> MediaItem:
    return MediaItem(
        id=item_id,
        original_path=path,
        cached_path=path,
        media_type=MediaType.IMAGE,
        width=w,
        height=h,
    )


def _video_item(item_id: str, path: Path, *, duration: float = 10.0) -> MediaItem:
    return MediaItem(
        id=item_id,
        original_path=path,
        cached_path=path,
        media_type=MediaType.VIDEO,
        width=1920,
        height=1080,
        duration_seconds=duration,
        first_frame_path=path,
        last_frame_path=path,
        transcode_status=TranscodeStatus.TRANSCODED,
    )


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.update("slideshow", {"shuffle": False, "image_duration_seconds": 30})
    return cfg


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def presenter(config: Config, backend: FakeBackend, tmp_path: Path, monkeypatch) -> Presenter:
    # current_media.json goes to run_dir(), which honours METIXEL_RUN_DIR.
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return Presenter(config, backend)  # type: ignore[arg-type]


class TestQueueHandling:
    def test_set_queue_presents_the_first_item(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        assert presenter.queue_loaded
        assert presenter.current_index == 0
        assert len(backend.presented) == 1

    def test_videos_survive_a_queue_set(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A video must reach the player, not be filtered out of the queue."""
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])
        assert len(presenter.queue) == 1
        assert backend.played == [tmp_path / "v.mp4"]

    def test_video_is_handed_to_the_player_and_marked_active(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])
        assert presenter.video_active

    def test_add_items_appends_without_restarting(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        added = presenter.add_items(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        assert added == 1, "the duplicate id must not be added twice"
        assert len(presenter.queue) == 2

    def test_remove_items_reports_the_count(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        assert presenter.remove_items({"b"}) == 1
        assert len(presenter.queue) == 1

    def test_removing_a_background_item_keeps_the_current_slide(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """Deleting something not on screen must not disturb the slideshow."""
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        assert presenter.current_item is not None
        assert presenter.current_item.id == "a"
        presenter.remove_items({"b"})
        assert presenter.current_item is not None
        assert presenter.current_item.id == "a"


class TestVideoLifecycle:
    def test_unsupported_backend_keeps_the_poster_and_does_not_crash(
        self, config: Config, tmp_path: Path, monkeypatch
    ) -> None:
        """A software backend must degrade to a still, not raise per item."""
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
        backend = FakeBackend(video=False)
        presenter = Presenter(config, backend)  # type: ignore[arg-type]
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])

        assert backend.played == []
        assert not presenter.video_active
        assert backend.presented, "the poster should still be painted"

    def test_finished_video_falls_back_to_the_poster(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])
        backend.finish_video()
        presenter.render()
        assert not presenter.video_active
        assert backend.stopped >= 1

    def test_next_item_stops_a_playing_video(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue(
            [_video_item("v", tmp_path / "v.mp4"), _image_item("a", tmp_path / "a.jpg")]
        )
        presenter.next_item()
        assert backend.stopped >= 1
        assert not presenter.video_active

    def test_pause_pauses_the_video_rather_than_stopping_it(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """Pausing must keep the decoder alive so resume is immediate."""
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])
        presenter.pause()
        assert backend.paused == [True]
        assert backend.stopped == 0, "pause must not tear down the pipeline"

    def test_resume_unpauses(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue([_video_item("v", tmp_path / "v.mp4")])
        presenter.pause()
        presenter.resume()
        assert backend.paused == [True, False]


class TestItemDuration:
    def test_image_uses_the_configured_duration(self, presenter: Presenter, tmp_path: Path) -> None:
        item = _image_item("a", tmp_path / "a.jpg")
        assert presenter._item_duration(item) == 30.0

    def test_video_uses_its_own_duration(self, presenter: Presenter, tmp_path: Path) -> None:
        item = _video_item("v", tmp_path / "v.mp4", duration=42.0)
        assert presenter._item_duration(item) == 42.0

    def test_video_duration_is_capped_by_config(self, presenter: Presenter, tmp_path: Path) -> None:
        """A long video must not overstay the configured maximum."""
        presenter._config.update("video", {"max_duration_seconds": 15})
        item = _video_item("v", tmp_path / "v.mp4", duration=600.0)
        assert presenter._item_duration(item) == 15.0


class TestStallAndHold:
    """The behaviour that stops a slow decode producing a blank frame.

    A held slide is preferable to an empty one on a wall-mounted frame, and this
    is the single behaviour most likely to be lost in a refactor — hence the
    explicit coverage.
    """

    @staticmethod
    def _unready(item_id: str, path: Path) -> MediaItem:
        """An item with no dimensions yet — the backend has not probed it."""
        return MediaItem(
            id=item_id,
            original_path=path,
            cached_path=path,
            media_type=MediaType.IMAGE,
            width=0,
            height=0,
        )

    def test_next_item_without_dimensions_is_not_layoutable(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), self._unready("b", tmp_path / "b.jpg")]
        )
        assert presenter._next_plan() is None

    def test_slide_is_held_when_the_next_item_is_not_ready(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), self._unready("b", tmp_path / "b.jpg")]
        )
        before = presenter.current_item
        # Past the slide duration but INSIDE the stall budget, so the presenter
        # holds rather than giving up.  (Beyond STALL_TIMEOUT_S it deliberately
        # advances — see test_stall_gives_up_after_the_timeout.)
        presenter._item_start_time = time.monotonic() - 35.0
        presenter.render()

        assert presenter.current_item is before, "the slide must not advance"
        assert presenter.current_index == 0
        assert backend.presented, "the held frame must still be painted"

    def test_stall_is_logged_once_not_per_frame(
        self, presenter: Presenter, tmp_path: Path, caplog
    ) -> None:
        """A slow decode must not flood the log at frame rate."""
        import logging

        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), self._unready("b", tmp_path / "b.jpg")]
        )
        presenter._item_start_time = time.monotonic() - 35.0
        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                presenter.render()
        stalls = [r for r in caplog.records if "stalled" in r.message.lower()]
        assert len(stalls) == 1, f"expected one stall log, got {len(stalls)}"

    def test_stall_gives_up_after_the_timeout(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """One unreadable file must not freeze the frame forever."""
        presenter.set_queue(
            [
                _image_item("a", tmp_path / "a.jpg"),
                self._unready("b", tmp_path / "b.jpg"),
                _image_item("c", tmp_path / "c.jpg"),
            ]
        )
        # Beyond the stall budget: the presenter must advance regardless.
        presenter._item_start_time = time.monotonic() - (STALL_TIMEOUT_S + 100.0)
        presenter.render()
        assert presenter.current_index != 0, "expected the presenter to advance"

    def test_stall_flag_clears_on_advance(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        presenter._item_start_time = time.monotonic() - 100.0
        presenter._transition_stall_logged = True
        presenter.render()
        assert not presenter._transition_stall_logged


class TestTransition:
    def test_crossfade_blends_two_frames(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A transition paints the outgoing and incoming frames at complementary alpha."""
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        backend.presented.clear()
        presenter._item_start_time = time.monotonic() - 30.5
        presenter.render()

        alphas = [a for _plan, _img, a in backend.presented]
        assert len(backend.presented) >= 2, "expected two paints for a blend"
        assert any(a < 1.0 for a in alphas), "one frame should be partially transparent"

    def test_style_none_cuts_without_blending(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter._config.update("slideshow", {"transition_style": "none"})
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        assert presenter._transition_seconds() == 0.0

    def test_transition_duration_comes_from_config(self, presenter: Presenter) -> None:
        presenter._config.update("slideshow", {"transition_duration_ms": 2500})
        assert presenter._transition_seconds() == 2.5


class TestCurrentMediaStateFile:
    """``current_media.json`` drives the dashboard's "Now Playing" card.

    The dashboard polls ``/api/health``, which reports ``current_media`` from this
    file.  A queue change that does not republish it leaves the UI showing a stale
    item (or "No media playing") indefinitely.
    """

    @staticmethod
    def _read(tmp_path: Path) -> dict | None:
        path = tmp_path / "run" / "current_media.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def test_set_queue_publishes_the_current_item(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        state = self._read(tmp_path)
        assert state is not None
        assert state["id"] == "a"

    def test_advancing_republishes(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        presenter.next_item()
        state = self._read(tmp_path)
        assert state is not None
        assert state["id"] == "b"

    def test_removing_everything_clears_the_state(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """The card must not keep naming a file the user just deleted."""
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        presenter.remove_items({"a"})
        assert self._read(tmp_path) is None

    def test_empty_queue_publishes_nothing(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter.set_queue([])
        assert self._read(tmp_path) is None


class TestReadiness:
    def test_not_ready_before_a_frame_is_painted(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        assert not presenter.has_visible_frame

    def test_ready_once_a_frame_is_painted(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        assert presenter.has_visible_frame

    def test_unprobed_first_item_is_not_ready(self, presenter: Presenter, tmp_path: Path) -> None:
        """The boot screen must not fade out onto an un-layoutable item."""
        presenter.set_queue([self._unready("a", tmp_path / "a.jpg")])
        assert not presenter.has_visible_frame

    @staticmethod
    def _unready(item_id: str, path: Path) -> MediaItem:
        return MediaItem(
            id=item_id,
            original_path=path,
            cached_path=path,
            media_type=MediaType.IMAGE,
            width=0,
            height=0,
        )
