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

import ast
import json
import random
import time
from pathlib import Path

import pytest

from metixel.framing.layout import RenderPlan
from metixel.frontend.presentation.image_cache import MAX_ENTRIES, DecodedImage, ImageCache
from metixel.frontend.presentation.presenter import STALL_TIMEOUT_S, Presenter
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

_ROOT = Path(__file__).resolve().parents[3]
_PRESENTER = _ROOT / "src" / "metixel" / "frontend" / "presentation" / "presenter.py"


def _code(path: Path, name: str) -> str:
    """A function's executable statements, docstring and comments stripped."""
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{name} not found in {path.name}")


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
        self.unloaded: list[object] = []
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

    def present(
        self,
        plan: RenderPlan,
        image: object = None,
        alpha: float = 1.0,
        backdrop_source: object = None,
    ) -> None:
        self.presented.append((plan, image, alpha))

    def load_image(self, path: object) -> object:
        # A non-None handle means "the item is displayable", which is what the
        # presenter keys off.  Never fails, so tests control readiness via
        # dimensions rather than by simulating IO errors.
        self.loaded.append(path)
        return f"image:{path}"

    def unload_image(self, handle: object) -> None:
        self.unloaded.append(handle)

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


class TestTransitionLayersAreResolvedOnce:
    """One handle per layer for the whole fade -- never one per frame.

    ``_image_for`` is not a pure lookup, which is what makes this a correctness
    property rather than a tidiness one:

    * for a video that has just ended it returns a **newly decoded** last frame
      and, with it, a newly minted backend artwork key -- that frame is
      deliberately uncached, so nothing about it is stable;
    * for an item the presenter's cache has dropped it falls back to a blocking
      re-decode, which also mints a new key.

    Asking for the outgoing layer on every frame of a fade therefore re-decoded a
    full-resolution JPEG on the render thread once per frame (the outgoing layer
    is the *previous* item -- for the 4K test clip, a 3840x2160 decode per frame),
    and handed the scene a different image URL every time.  The backend's artwork
    store can only pin a *stable* key, so one orphaned key was minted per frame
    and Qt asked for keys that had already been trimmed out.

    Measured on the frame, per crossfade: ~150-165 ``Failed to get image from
    provider`` errors -- one per frame of a 2.5 s fade at 60 fps -- of which 316
    of 318 were on ``Frame.qml:227``, the outgoing layer.
    """

    @staticmethod
    def _enter_fade(presenter: Presenter, backend: FakeBackend) -> None:
        presenter._item_start_time = time.monotonic() - 30.5
        backend.presented.clear()

    @staticmethod
    def _outgoing(backend: FakeBackend) -> list[object]:
        """The handles painted as the outgoing layer, in frame order.

        Without ``present_transition`` the fake takes the two-paint fallback, and
        the outgoing layer is painted first within each frame.
        """
        return [backend.presented[i][1] for i in range(0, len(backend.presented), 2)]

    def test_the_outgoing_handle_survives_a_cache_eviction_mid_fade(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        self._enter_fade(presenter, backend)
        presenter.render()
        first = self._outgoing(backend)
        assert first, "the fade should paint an outgoing layer"
        backend.loaded.clear()

        # The presenter's LRU is only a few entries deep, and it is cleared
        # outright when the queue changes.  Either can drop the item on screen
        # while it is still the fade's outgoing layer.
        presenter._cache.clear()
        for _ in range(4):
            presenter.render()

        assert self._outgoing(backend) == first * len(self._outgoing(backend))
        assert backend.loaded == [], "an evicted outgoing layer must not be re-decoded per frame"

    def test_an_ended_videos_last_frame_is_decoded_once_per_fade(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video), _image_item("b", tmp_path / "b.jpg")])
        backend.finish_video()
        # The poster was loaded while the slide was still being set up, so only
        # the last-frame decodes are left to count.
        backend.loaded.clear()

        for _ in range(5):
            presenter.render()

        assert backend.loaded.count(video) == 1, (
            "the ended video's last frame must be decoded once for the fade, not once per frame"
        )


class TestTheLastFrameIsPreloadedBehindTheVideo:
    """The last frame is fetched while the video still covers it.

    Loading it *at* the moment playback ends is strictly worse, and is the defect
    this replaces: the artwork layer becomes visible the instant the video surface
    is hidden, so a load started then shows whatever was underneath — the opening
    image — for however long the decode takes.  Observed on the frame as the first
    frame "appearing from behind" the video the moment it finished.

    Ported from the 1.2.6 method (``presentation/video_state.py``), which loaded the
    last frame into a texture BEFORE launching the player and swapped it into the
    active slot at 50% of playtime as a pure pointer swap with no I/O.  Here the
    fraction is :data:`Presenter._LAST_FRAME_PRELOAD_FRACTION` (20%), against 50%
    there, because this backend loads through an image provider — a decode plus a
    provider entry — so starting earlier leaves more slack for a 4K frame.
    """

    _CLIP_SECONDS = 100.0

    def _play(self, presenter: Presenter, backend: FakeBackend, tmp_path: Path) -> Path:
        """Start a long video and clear the load log, leaving playback running."""
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=self._CLIP_SECONDS)])
        assert presenter.video_active, "the video should be playing"
        backend.loaded.clear()
        return video

    def test_nothing_is_loaded_before_the_fraction(self, presenter, backend, tmp_path) -> None:
        video = self._play(presenter, backend, tmp_path)

        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.10)

        assert video not in backend.loaded, "the last frame must not load this early"
        assert presenter._preloaded_last_frame is None

    def test_the_last_frame_loads_once_past_the_fraction(
        self, presenter, backend, tmp_path
    ) -> None:
        self._play(presenter, backend, tmp_path)

        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)

        assert presenter._preloaded_last_frame is not None

    def test_the_fraction_is_a_twentieth_to_a_half_of_the_clip(self) -> None:
        """Low enough to beat the decode, high enough not to precede the video."""
        assert 0.05 <= Presenter._LAST_FRAME_PRELOAD_FRACTION <= 0.5

    def test_the_preload_runs_once_per_slide_not_once_per_tick(
        self, presenter, backend, tmp_path
    ) -> None:
        """A per-tick load would be a decode per frame for the rest of the slide."""
        video = self._play(presenter, backend, tmp_path)

        for _ in range(10):
            presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.30)

        assert backend.loaded.count(video) == 1

    def test_a_failed_preload_is_not_retried(self, presenter, backend, tmp_path) -> None:
        """A missing last frame degrades to the poster, which is the old behaviour."""
        video = self._play(presenter, backend, tmp_path)
        presenter._load_uncached = lambda _path: None  # type: ignore[method-assign]

        for _ in range(5):
            presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.30)

        assert presenter._preloaded_last_frame is None
        assert presenter._video_last_frame_ready is True, "the latch must still be set"
        assert video not in backend.loaded

    def test_the_fraction_is_of_the_clip_not_the_slide(self, presenter, backend, tmp_path) -> None:
        """A video capped by config would otherwise preload at an arbitrary point.

        The fraction is taken from ``item.duration_seconds``, so a 100 s clip given
        a 10 s slide window still preloads at 20 s of *clip* time — which is beyond
        the slide and simply never fires, rather than firing at 2 s.
        """
        video = self._play(presenter, backend, tmp_path)

        # Third of the slide's cap, but only 3% of the clip.
        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.03)

        assert video not in backend.loaded

    def test_an_unknown_duration_never_preloads(self, presenter, backend, tmp_path) -> None:
        """Without a duration there is no fraction to take, so it degrades."""
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=0.0)])
        backend.loaded.clear()

        presenter._preload_last_frame(presenter.current_item, 900.0)

        assert video not in backend.loaded
        assert presenter._preloaded_last_frame is None

    def test_an_image_item_is_ignored(self, presenter, backend, tmp_path) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        backend.loaded.clear()

        presenter._preload_last_frame(presenter.current_item, 900.0)

        assert backend.loaded == []

    def test_the_preloaded_handle_is_what_the_transition_gets(
        self, presenter, backend, tmp_path
    ) -> None:
        """The whole point: the fade's outgoing layer must be the preloaded one."""
        self._play(presenter, backend, tmp_path)
        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)
        preloaded = presenter._preloaded_last_frame
        assert preloaded is not None
        item = presenter.current_item
        assert item is not None

        backend.loaded.clear()
        presenter._end_video(item)

        assert presenter._image_for(item) is preloaded
        assert backend.loaded == [], "the end of the video must cost no decode at all"

    def test_skipping_before_the_fraction_still_falls_back_to_a_load(
        self, presenter, backend, tmp_path
    ) -> None:
        """A manual skip ends the slide before the preload point is ever reached."""
        video = self._play(presenter, backend, tmp_path)
        item = presenter.current_item
        assert item is not None
        assert presenter._preloaded_last_frame is None, "precondition: never preloaded"

        presenter._end_video(item)
        backend.loaded.clear()

        assert presenter._image_for(item) is not None
        assert backend.loaded.count(video) == 1, "the uncached fallback must cover this"

    def test_leaving_the_video_clears_the_handle_and_the_latch(
        self, presenter, backend, tmp_path
    ) -> None:
        """A stale handle would show the previous video's last frame on the next."""
        self._play(presenter, backend, tmp_path)
        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)
        assert presenter._preloaded_last_frame is not None

        presenter.next_item()

        assert presenter._preloaded_last_frame is None
        assert presenter._video_ended_id is None
        assert presenter._video_last_frame_ready is False, "the latch must not leak across slides"

    def test_render_drives_the_preload_while_playing(
        self, presenter, backend, tmp_path, monkeypatch
    ) -> None:
        """The trigger is ``render``, so a real slideshow reaches it without help."""
        self._play(presenter, backend, tmp_path)
        calls: list[float] = []
        monkeypatch.setattr(
            Presenter, "_preload_last_frame", lambda self, item, elapsed: calls.append(elapsed)
        )

        presenter.render()

        assert calls, "render must offer the playing video a chance to preload"


class TestThePreloadedFrameReplacesThePosterWhileHidden:
    """The preload must re-source the artwork layer, not just decode.

    Decoding early is only half the fix.  The remaining cost is the *source
    assignment*: Qt cannot present a new ``source`` until it has resolved and
    uploaded it, so a swap performed at the instant the video stops still leaves
    whatever was already in the slot — the poster — on screen for that gap.  That
    is the first-frame flash.

    The video surface covers the artwork layer for the whole clip, so re-sourcing
    it mid-playback is invisible.  Doing it there is what makes the reveal instant,
    and it is why the preloaded handle must ALSO win in ``_image_for`` while the
    video is still playing — otherwise the next tick re-asserts the poster and
    throws the work away.
    """

    _CLIP_SECONDS = 100.0

    def test_the_layer_is_resourced_as_soon_as_the_preload_lands(
        self, presenter, backend, tmp_path
    ) -> None:
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=self._CLIP_SECONDS)])
        backend.loaded.clear()

        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)

        preloaded = presenter._preloaded_last_frame
        assert preloaded is not None
        assert backend.presented, "the preload must be pushed to the display, not only decoded"
        assert backend.presented[-1][1] is preloaded, (
            "the last frame must be the artwork source before the video stops"
        )

    def test_the_poster_does_not_come_back_on_the_next_tick(
        self, presenter, backend, tmp_path
    ) -> None:
        """``_present_current`` runs every tick and would otherwise undo the swap."""
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=self._CLIP_SECONDS)])
        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)
        preloaded = presenter._preloaded_last_frame
        assert preloaded is not None
        item = presenter.current_item
        assert item is not None
        assert item.media_type == MediaType.VIDEO

        backend.loaded.clear()
        presenter._present_current()

        assert presenter._image_for(item) is preloaded
        assert backend.loaded == [], "the tick must not re-decode the poster"

    def test_the_same_handle_serves_the_fade_after_the_video_ends(
        self, presenter, backend, tmp_path
    ) -> None:
        """One handle, two phases: hidden under the video, then the outgoing layer."""
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=self._CLIP_SECONDS)])
        presenter._preload_last_frame(presenter.current_item, self._CLIP_SECONDS * 0.25)
        preloaded = presenter._preloaded_last_frame
        item = presenter.current_item
        assert item is not None

        presenter._end_video(item)

        assert presenter._image_for(item) is preloaded, (
            "the fade must not swap in a second handle at the moment of the reveal"
        )

    def test_an_item_that_never_preloaded_still_uses_the_poster(
        self, presenter, backend, tmp_path
    ) -> None:
        """No regression: without a preload the old path is untouched.

        A skip before the fraction leaves no handle, so ``_image_for`` must fall
        through to the poster rather than reporting nothing.  The poster is served
        from ``ImageCache``, so it is already loaded and this costs no decode.
        """
        video = tmp_path / "v.mp4"
        presenter.set_queue([_video_item("v", video, duration=self._CLIP_SECONDS)])
        item = presenter.current_item
        assert item is not None
        assert presenter._preloaded_last_frame is None
        assert backend.loaded.count(video) == 1, "precondition: the poster loaded once"

        assert presenter._image_for(item) is not None, "no preload means the poster, not None"

        presenter._present_current()
        assert backend.loaded.count(video) == 1, "the poster must be reused, not re-decoded"


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
        """The card follows the frame, which means it follows the FADE.

        A manual skip now animates, so the target is not on screen the instant
        ``next_item`` returns — publishing it then would make the dashboard name a
        file the viewer cannot see yet, for the whole duration of the crossfade.
        The republish therefore happens where the index actually moves, in
        ``_advance``, and this walks the fade out to reach it.
        """
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        presenter.next_item()

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            presenter.render()
            state = self._read(tmp_path)
            if state is not None and state["id"] == "b":
                return
            time.sleep(0.01)
        pytest.fail("current_media.json never named the item the fade landed on")

    def test_the_state_file_tracks_the_frame_not_the_target(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """Mid-fade the outgoing item is still the one on screen."""
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        presenter.next_item()
        # One tick only: the blend has started but not finished.
        presenter.render()

        state = self._read(tmp_path)
        assert state is not None
        assert state["id"] == "a", (
            "the dashboard must not claim to be showing an item that is still only "
            "part-way through fading in"
        )

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


class TestTheDecodeAheadPipeline:
    """Where a finished decode-ahead payload is adopted, and what it may not do.

    The defect behind the blur-ambient regressions on hardware: with
    ``ambient_strategy == "blur"``, every OTHER slide painted the flat ambient
    colour instead of its blurred backdrop, and every transition stalled.

    Two mistakes compounded:

    * the payload was collected from ``_preload_next``, so it landed a full slide
      after it was requested.  The next item's backdrop is built from the next
      item's decoded handle while the CURRENT slide is on screen, so a payload
      that arrives a slide late leaves the backdrop with nothing to build from
      and the transition with nothing to start against;
    * ``ImageCache.put`` replaced the handle for a key that was already cached.
      The canvas identifies a layer's backdrop by the IDENTITY of the artwork
      handle it was built for, so swapping the handle orphaned a backdrop that
      was already in memory — the photo drew, its blur existed, and the lookup
      could not match them.
    """

    def test_a_finished_decode_is_adopted_within_the_slide(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        presenter._cache._ready = DecodedImage(key="next", data=b"jpeg", width=2, height=2)

        presenter.render()

        assert presenter._cache.get("next") is not None, (
            "the frame loop must adopt a finished decode, not wait for the next advance"
        )

    def test_a_drain_does_not_swap_the_handle_of_the_item_on_screen(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """The exact swap that orphaned the backdrop and painted the flat fill."""
        presenter.set_queue([_image_item("a", tmp_path / "a.jpg")])
        on_screen = presenter._cache.get("a")
        assert on_screen is not None

        # A late decode of the SAME item, which is what the worker produces when
        # it is asked for the item that is already being shown.
        presenter._cache._ready = DecodedImage(key="a", data=b"jpeg", width=2, height=2)
        presenter.render()

        assert presenter._cache.get("a") is on_screen, (
            "a second decode of the same key must not orphan the backdrop keyed to it"
        )

    def test_starting_a_preload_does_not_collect_the_previous_one(self) -> None:
        """Collecting at the point of *starting* a decode can only be a slide late."""
        code = _code(_PRESENTER, "_preload_next")
        assert "_drain_cache" not in code

    def test_the_preload_payload_carries_the_media_id(self, tmp_path: Path) -> None:
        """The payload must be retrievable under the key the presenter looks up.

        Regression: the worker published the SOURCE PATH as the payload key, so
        the drain stored it under a key nothing ever asks for.  The decode was
        performed and thrown away, the next item was never cached ahead, and its
        ambient backdrop had nothing to be built from during the slide — which is
        what left the transition with nothing to start against.
        """
        from PIL import Image

        from metixel.frontend.presentation.image_cache import ImageCache

        source = tmp_path / "photo.jpg"
        Image.new("RGB", (40, 30), (10, 20, 30)).save(source)

        cache = ImageCache()
        cache.start_preload("media-id", source, 100, 100)

        payload = None
        deadline = time.monotonic() + 5.0
        while payload is None and time.monotonic() < deadline:
            payload = cache.take_ready()
            if payload is None:
                time.sleep(0.01)

        assert payload is not None, "the preload never produced a payload"
        assert payload.key == "media-id", (
            "the payload must carry the key the caller will look it up with"
        )

    def test_the_drain_never_lands_on_a_transition_frame(self) -> None:
        """It is a texture upload on the GUI thread; a crossfade must not absorb it."""
        code = _code(_PRESENTER, "render")
        assert "_drain_cache()" in code
        assert code.index("_in_transition()") < code.index("_drain_cache()")

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


class TestTheCurrentItemSurvivesAdoptingADecode:
    """Adopting a preload must not blank the item already on screen.

    ``_drain_cache`` runs on the tick that a background decode finishes, and
    ``ImageCache.put`` can evict to make room.  The victim is the
    least-recently-used entry, and once an item's plan is memoised nothing ever
    ``get``s its handle again — ``_preload_next`` and ``_current_plan`` are the
    only cache users.  So the LRU victim is the item *on screen*, while
    ``_current_plan`` keeps returning the memoised plan and ``_image_for`` then
    hands the artwork layer ``None`` on every tick from then on.

    Videos hid it, which is what made it look like an image-only bug: a video's
    slide is driven by ``video_finished()``/its own duration and the visible pixels
    come from the ``VideoOutput``, not from the artwork handle — so a missing
    artwork is invisible there.
    """

    @staticmethod
    def _queue(presenter: Presenter, tmp_path: Path, count: int = 3) -> None:
        presenter.set_queue([_image_item(f"i{n}", tmp_path / f"i{n}.jpg") for n in range(count)])

    def test_adopting_a_decode_does_not_leave_the_shown_item_plan_stale(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """The invariant, stated as behaviour: a dropped handle drops the plan.

        Driving the LRU to evict the shown item specifically is fiddly — ``put``
        refreshes an existing key, and ``_preload_next`` is already holding the
        item after it — so this asserts the property rather than the exact
        sequence: whenever the shown item is absent from the cache, the memoised
        plan must not survive.  The eviction itself is pinned in
        ``TestImageHandleRelease``.
        """
        self._queue(presenter, tmp_path, count=3)
        presenter.render()
        assert presenter._shown_plan is not None, "a rendered slide has a plan"

        presenter._cache.clear()
        presenter._drain_cache()

        assert presenter._shown_plan is None, (
            "the memoised plan must be dropped so the handle is re-resolved"
        )

    def test_the_shown_item_still_has_an_artwork_after_the_drain(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        self._queue(presenter, tmp_path, count=4)
        presenter.render()
        shown = presenter._shown_item
        assert shown is not None

        presenter._cache.put("filler-a", "h:a")
        presenter._cache.put("filler-b", "h:b")
        presenter._drain_cache()
        presenter._cache.put("filler-c", "h:c")
        presenter._drain_cache()

        assert presenter._image_for(shown) is not None, "the item must remain displayable"

    def test_a_frame_is_never_presented_without_its_artwork(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A blank tick is worse than holding the previous frame."""
        self._queue(presenter, tmp_path)
        presenter.render()
        backend.presented.clear()

        # Make the handle genuinely unobtainable so the re-resolve cannot succeed.
        presenter._shown_plan = None
        presenter._shown_item = presenter.current_item
        monkey_to_none = lambda _path: None  # noqa: E731
        presenter._load_uncached = monkey_to_none  # type: ignore[method-assign]
        presenter._cache.clear()

        presenter._present_current()

        painted = [img for _p, img, _a in backend.presented]
        assert backend.presented == [] or all(img is not None for img in painted), (
            "present() must not be called with a null artwork"
        )

    def test_with_artwork_false_still_presents(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """The deliberate ``present(..., None)`` call must not be suppressed."""
        self._queue(presenter, tmp_path)
        presenter.render()
        backend.presented.clear()

        presenter._present_current(with_artwork=False)

        assert backend.presented, "an explicit no-artwork present must still happen"


class TestImageHandleRelease:
    """A handle dropped from the cache must be released by the BACKEND too.

    The defect behind the frame's OOM crash loop: ``ImageCache`` bounded what the
    presenter retained, but the decoded image actually lives in the backend, keyed
    by the handle it handed out.  Dropping the handle therefore freed nothing, and
    the leak was exactly one full-resolution image per slide — ~1 GB in ten
    minutes, then ``Out of memory: Killed process <frontend>
    anon-rss:1021456kB``, then a systemd restart, forever.

    Nothing here caught it because every existing assertion is about what the
    *presenter* retains, and the leak was on the far side of that boundary.
    """

    def test_eviction_releases_the_handle_it_drops(self) -> None:
        released: list[str] = []
        cache = ImageCache(max_entries=2, release=released.append)

        cache.put("a", "h:a")
        cache.put("b", "h:b")
        cache.put("c", "h:c")

        assert released == ["h:a"], "the evicted handle must be handed back, not just dropped"

    def test_the_least_recently_used_handle_is_the_one_released(self) -> None:
        released: list[str] = []
        cache = ImageCache(max_entries=2, release=released.append)
        cache.put("a", "h:a")
        cache.put("b", "h:b")

        cache.get("a")  # "a" is now the most recent, so "b" is the eviction target
        cache.put("c", "h:c")

        assert released == ["h:b"]

    def test_a_handle_that_stays_cached_is_never_released(self) -> None:
        """A backdrop is keyed to the IDENTITY of the handle, so it must survive."""
        released: list[str] = []
        cache = ImageCache(release=released.append)
        cache.put("a", "h:a")

        cache.put("a", "h:second-decode-of-the-same-item")

        assert released == [], "re-putting a cached key must not release the live handle"
        assert cache.get("a") == "h:a"

    def test_clear_releases_every_handle(self) -> None:
        released: list[str] = []
        cache = ImageCache(release=released.append)
        cache.put("a", "h:a")
        cache.put("b", "h:b")

        cache.clear()

        assert sorted(released) == ["h:a", "h:b"]
        assert cache.size == 0

    def test_a_failing_release_does_not_break_the_slideshow(self) -> None:
        """Never raise into a slide change: a slow leak beats a stopped frame."""

        def boom(_handle: object) -> None:
            raise RuntimeError("backend went away")

        cache = ImageCache(max_entries=1, release=boom)
        cache.put("a", "h:a")
        cache.put("b", "h:b")  # must not propagate

    def test_the_presenter_releases_through_the_backend(
        self, presenter: Presenter, backend: FakeBackend
    ) -> None:
        """The wiring, not just the hook: the presenter must pass the backend's own
        ``unload_image``, or the hook has nothing to call."""
        for i in range(MAX_ENTRIES + 1):
            presenter._cache.put(f"k{i}", f"h:{i}")

        assert backend.unloaded, "an eviction must reach FakeBackend.unload_image"


def _reloaded(presenter: Presenter, **slideshow: object) -> Config:
    """A *new* Config with *slideshow* merged in, as the hot-reload path builds.

    ``Config.load()`` in the renderer constructs a fresh instance rather than
    mutating the running one, so a test that mutates ``presenter._config`` in
    place would not exercise the repointing that ``reload_config`` has to do.
    """
    cfg = Config(presenter._config.to_dict())
    cfg.update("slideshow", dict(slideshow))
    return cfg


class TestFitMode:
    """``fit_mode`` + ``smart_cover`` decide how an artwork meets the panel.

    The card's vocabulary (contain/cover) is mapped onto the framing engine's
    overflow axis (fill/crop); these assert on the resulting
    :class:`RenderPlan`, which is what the canvas actually paints.
    """

    #: A landscape panel, as the FakeBackend reports it.
    LANDSCAPE = (3000, 2000)
    PORTRAIT = (1080, 1920)

    def test_cover_crops_a_same_orientation_photo(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", *self.LANDSCAPE)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "crop"
        # Cropped, so only part of the source is sampled.
        assert plan.artwork_src[3] < self.LANDSCAPE[1]

    def test_contain_letterboxes_without_cropping(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        presenter._config.update("slideshow", {"fit_mode": "contain"})
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", *self.LANDSCAPE)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "fill"
        assert plan.ambient is not None, "the residue must be filled, not left blank"
        assert plan.artwork_src == (0.0, 0.0, 3000.0, 2000.0), "nothing may be cropped"

    def test_smart_cover_contains_an_opposite_orientation_photo(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """A portrait photo on a landscape panel would lose its top and bottom."""
        presenter.set_queue([_image_item("p", tmp_path / "p.jpg", *self.PORTRAIT)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "fill"

    def test_smart_cover_off_crops_it_instead(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter._config.update("slideshow", {"smart_cover": False})
        presenter.set_queue([_image_item("p", tmp_path / "p.jpg", *self.PORTRAIT)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "crop"

    def test_smart_cover_does_not_apply_to_contain_mode(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """Contain already keeps the whole photo; smart cover must not fight it."""
        presenter._config.update("slideshow", {"fit_mode": "contain", "smart_cover": True})
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", *self.LANDSCAPE)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "fill"

    def test_retired_fill_mode_falls_back_to_cover(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """An older config can still say 'fill' (stretch); the engine cannot."""
        presenter._config.update("slideshow", {"fit_mode": "fill"})
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", *self.LANDSCAPE)])
        plan = presenter._current_plan()
        assert plan is not None
        assert plan.overflow == "crop"


class TestShuffle:
    def test_disabled_keeps_the_backend_scan_order(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        ids = [f"i{n}" for n in range(10)]
        presenter.set_queue([_image_item(i, tmp_path / f"{i}.jpg") for i in ids])
        assert [item.id for item in presenter.queue] == ids

    def test_enabled_randomises_the_play_order(self, presenter: Presenter, tmp_path: Path) -> None:
        presenter._config.update("slideshow", {"shuffle": True})
        random.seed(1234)
        ids = [f"i{n}" for n in range(20)]
        presenter.set_queue([_image_item(i, tmp_path / f"{i}.jpg") for i in ids])
        order = [item.id for item in presenter.queue]
        assert sorted(order) == sorted(ids), "shuffling must not lose or duplicate items"
        assert order != ids, "…and must actually reorder them"

    def test_new_items_never_land_behind_the_playhead(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """Scattering new items must not re-order what has already been shown."""
        presenter._config.update("slideshow", {"shuffle": True})
        random.seed(7)
        presenter.set_queue([_image_item(f"a{n}", tmp_path / f"a{n}.jpg") for n in range(6)])
        presenter.next_item()

        played = [item.id for item in presenter.queue[:2]]
        presenter.add_items([_image_item(f"n{n}", tmp_path / f"n{n}.jpg") for n in range(4)])

        assert [item.id for item in presenter.queue[:2]] == played
        assert len(presenter.queue) == 10

    def test_new_items_never_displace_the_item_the_transition_is_heading_for(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """The immediate next item's backdrop is already built and keyed to it.

        Displacing that item with a new arrival means the transition starts
        against the wrong backdrop and the slide stalls waiting for a new one —
        which is what made arrivals (a scan finishing, an Immich sync, an upload)
        show up as a glitch at the next slide boundary.
        """
        presenter._config.update("slideshow", {"shuffle": True})
        random.seed(7)
        presenter.set_queue([_image_item(f"a{n}", tmp_path / f"a{n}.jpg") for n in range(6)])
        presenter.next_item()

        upcoming = presenter.queue[presenter.current_index + 1].id
        presenter.add_items([_image_item(f"n{n}", tmp_path / f"n{n}.jpg") for n in range(8)])

        assert presenter.queue[presenter.current_index + 1].id == upcoming


class TestSettingsHotReload:
    """Saving the Slideshow card restarts nothing, so the Presenter must reload.

    ``routes/config.py`` only bounces a service for processing-affecting
    sections, so every slideshow setting has to land through
    :meth:`Presenter.reload_config`.
    """

    def test_transition_style_applies_without_a_restart(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        presenter.set_queue(
            [_image_item("a", tmp_path / "a.jpg"), _image_item("b", tmp_path / "b.jpg")]
        )
        assert presenter._transition_seconds() > 0

        presenter.reload_config(_reloaded(presenter, transition_style="none"))
        assert presenter._transition_seconds() == 0.0

        backend.presented.clear()
        presenter._item_start_time = time.monotonic() - 30.05
        presenter.render()

        assert presenter.current_index == 1
        assert [a for _plan, _img, a in backend.presented] == [1.0], (
            "a 'none' style must cut, not blend two frames"
        )

    def test_transition_duration_applies_without_a_restart(self, presenter: Presenter) -> None:
        presenter.reload_config(_reloaded(presenter, transition_duration_ms=800))
        assert presenter._transition_seconds() == 0.8

    def test_fit_mode_change_re_lays_the_frame_on_screen(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """The plan cached for the slide on screen must be rebuilt."""
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", 3000, 2000)])
        cached = presenter._current_plan()
        assert cached is not None and cached.overflow == "crop"

        presenter.reload_config(_reloaded(presenter, fit_mode="contain"))

        replanned = presenter._current_plan()
        assert replanned is not None and replanned.overflow == "fill"

    def test_an_unrelated_save_keeps_the_frame_visible(
        self, presenter: Presenter, tmp_path: Path
    ) -> None:
        """Only a real change may invalidate the frame, or the boot fade reruns."""
        presenter.set_queue([_image_item("l", tmp_path / "l.jpg", 3000, 2000)])
        assert presenter.has_visible_frame
        presenter.reload_config(_reloaded(presenter, image_duration_seconds=45))
        assert presenter.has_visible_frame
