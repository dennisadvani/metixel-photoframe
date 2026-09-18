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
from metixel.frontend.presentation.image_cache import DecodedImage
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
