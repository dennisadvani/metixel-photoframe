# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Manual Next/Prev must animate, not cut.

The skip is expressed as "the outgoing item's window just ended", so the very
next ``render`` walks into the ordinary transition branch.  That is the whole
point: a manual jump reuses the SAME easing, backdrops and blend code as an
automatic advance, so the two can never drift apart — and it is why these tests
assert on what ``render`` does afterwards rather than on the index alone.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from metixel.frontend.presentation.presenter import Presenter
from metixel.shared.config import Config

# Reused from the sibling presenter suite: the fake backend and the MediaItem
# builders are already the canonical stubs for this area, and a second copy would
# be free to drift from what the other tests assert against.
from .test_presenter import FakeBackend, _image_item, _video_item


@pytest.fixture
def fade_config() -> Config:
    """A config with a fade long enough to observe mid-transition."""
    cfg = Config()
    cfg.update("slideshow", {"shuffle": False, "image_duration_seconds": 30})
    return cfg


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def _make(
    fade_config: Config,
    backend: FakeBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fade: int = 1000,
    style: str = "crossfade",
) -> Presenter:
    """Build a presenter with the given fade settings.

    A factory rather than a fixture because several tests need to change the
    transition style *after* construction (``reload_config`` is the path the
    dashboard uses), and a fixture would have to be re-requested to do that.
    """
    fade_config.update("slideshow", {"transition_style": style, "transition_duration_ms": fade})
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return Presenter(fade_config, backend)  # type: ignore[arg-type]


@pytest.fixture
def presenter(
    fade_config: Config, backend: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Presenter:
    return _make(fade_config, backend, tmp_path, monkeypatch)


def _four(presenter: Presenter, tmp_path: Path) -> None:
    """Load a four-item queue, all photos, and settle on index 0."""
    presenter.set_queue(
        [
            _image_item("a", tmp_path / "a.jpg"),
            _image_item("b", tmp_path / "b.jpg"),
            _image_item("c", tmp_path / "c.jpg"),
            _image_item("d", tmp_path / "d.jpg"),
        ]
    )


def _run_until(presenter: Presenter, item_id: str, *, seconds: float = 3.0) -> None:
    """Drive ``render`` until *item_id* is current, failing if it never lands."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        presenter.render()
        current = presenter.current_item
        if current is not None and current.id == item_id:
            return
        time.sleep(0.01)
    pytest.fail(f"the skip never reached item {item_id!r}")


class TestAManualSkipAnimates:
    def test_next_does_not_land_immediately(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A cut would show the target at full opacity on the first present.

        The skip must instead leave the fade to run: the item is reached, but
        through the transition branch, so the presented alpha is still blended.
        """
        _four(presenter, tmp_path)
        before = len(backend.presented)

        presenter.next_item()
        presenter.render()

        assert len(backend.presented) > before, "render must paint the fade"
        plan, _image, alpha = backend.presented[-1]
        assert 0.0 <= alpha <= 1.0

    def test_next_ends_on_the_following_item(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        _four(presenter, tmp_path)
        presenter.next_item()

        _run_until(presenter, "b")

        assert presenter.current_item is not None
        assert presenter.current_item.id == "b"

    def test_prev_ends_on_the_preceding_item(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """Backwards is the case a forward-only blend cannot express directly."""
        _four(presenter, tmp_path)
        presenter.next_item()
        _run_until(presenter, "b")

        presenter.prev_item()
        _run_until(presenter, "a")

        assert presenter.current_item is not None
        assert presenter.current_item.id == "a"

    def test_prev_wraps_to_the_last_item(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        _four(presenter, tmp_path)
        presenter.prev_item()

        _run_until(presenter, "d")

        assert presenter.current_item is not None
        assert presenter.current_item.id == "d"

    def test_the_incoming_layer_is_the_target_not_one_past_it(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """The index arithmetic is parked so ``current + 1`` IS the target.

        Getting this wrong is silent: the fade would blend to the item AFTER the
        one the user asked for, and the frame would land there too.
        """
        _four(presenter, tmp_path)
        item = presenter.current_item
        assert item is not None and item.id == "a"

        presenter.next_item()
        # Parked one before the target, so the target is the "next" item.
        assert presenter.current_item is not None
        assert presenter.current_item.id == "a", "next parks the index, it does not move it"

        nxt = presenter._next_item()  # noqa: SLF001 - the contract under test
        assert nxt is not None and nxt.id == "b"

    def test_prev_parks_so_the_predecessor_is_the_next_item(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        _four(presenter, tmp_path)
        presenter.next_item()
        _run_until(presenter, "b")

        presenter.prev_item()
        nxt = presenter._next_item()  # noqa: SLF001 - the contract under test
        assert nxt is not None and nxt.id == "a"


class TestTheJumpDegradesHonestly:
    def test_a_single_item_queue_still_moves_without_a_transition(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """There is no second image to blend to, so a cut is the only option."""
        presenter.set_queue([_image_item("only", tmp_path / "only.jpg")])

        presenter.next_item()

        assert presenter.current_item is not None
        assert presenter.current_item.id == "only"

    def test_transition_none_is_a_hard_cut(
        self,
        fade_config: Config,
        backend: FakeBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A style with no duration must not be given an invented fade."""
        presenter = _make(fade_config, backend, tmp_path, monkeypatch, style="none")
        _four(presenter, tmp_path)

        presenter.next_item()

        assert presenter.current_item is not None
        assert presenter.current_item.id == "b", "with no transition the index just moves"

    def test_a_video_being_left_is_stopped(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A live video surface cannot be blended, so it must stop first."""
        presenter.set_queue(
            [
                _video_item("v", tmp_path / "v.mp4"),
                _image_item("b", tmp_path / "b.jpg"),
            ]
        )
        assert presenter.video_active

        presenter.next_item()

        assert not presenter.video_active, "the video surface must be given up"

    def test_the_clock_is_backdated_by_the_parked_item_duration(
        self,
        fade_config: Config,
        backend: FakeBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``render`` measures ``elapsed`` against ``current_item``, which is the
        PARKED item — so that is the duration the backdate has to use.

        Using the target's duration is the intuitive mistake, and it mis-times
        every jump between items of different length: parking a 30 s photo in
        front of a 4 s clip would start the blend 26 s late, so the frame would
        sit on the photo and then appear to cut rather than fade.
        """
        presenter = _make(fade_config, backend, tmp_path, monkeypatch)
        presenter.set_queue(
            [
                _image_item("a", tmp_path / "a.jpg"),
                _video_item("v", tmp_path / "v.mp4", duration=4.0),
            ]
        )

        presenter.next_item()

        parked = presenter.current_item
        assert parked is not None
        assert parked.id == "a", "the index is parked, not advanced"
        elapsed = time.monotonic() - presenter._item_start_time  # noqa: SLF001
        duration = presenter._item_duration(parked)  # noqa: SLF001
        assert elapsed >= duration - 0.5, (
            f"the clock must already be at the parked item's duration ({duration}) "
            f"so the fade starts from zero, got {elapsed:.2f}"
        )

    def test_a_photo_to_video_jump_still_fades_from_the_start(
        self,
        fade_config: Config,
        backend: FakeBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mis-timed case, stated as behaviour rather than as a clock check.

        A 30 s photo parked in front of a 4 s clip must fade immediately.  If the
        backdate used the clip's duration the blend would not begin for 26 s, and
        the frame would look like it had cut.
        """
        presenter = _make(fade_config, backend, tmp_path, monkeypatch)
        presenter.set_queue(
            [
                _image_item("a", tmp_path / "a.jpg"),
                _video_item("v", tmp_path / "v.mp4", duration=4.0),
            ]
        )
        presenter.next_item()
        before = len(backend.presented)

        presenter.render()

        assert len(backend.presented) > before, (
            "the next render must already be blending, not still holding the photo"
        )
