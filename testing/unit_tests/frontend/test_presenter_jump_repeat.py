# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Repeated Next/Prev presses must each step one item further.

The defect this guards: a skip parks ``_current_idx`` one BEFORE its destination
so that ``_present_transition`` (which only blends ``shown -> current + 1``) runs
forwards.  That makes ``current_item`` name the item being LEFT for the whole
fade.  A second press that advanced from there recomputed the destination that was
already arriving, so it re-faded to the same photo and the button appeared dead
until the first fade finished.

The fix records the in-flight destination on ``_jump_target``, so each press steps
from where the previous one was *going* rather than from the parked index.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from metixel.frontend.presentation.presenter import Presenter
from metixel.shared.config import Config

from .test_presenter import FakeBackend, _image_item, _video_item


@pytest.fixture
def fade_config() -> Config:
    cfg = Config()
    cfg.update("slideshow", {"shuffle": False, "image_duration_seconds": 30})
    return cfg


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def presenter(
    fade_config: Config, backend: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Presenter:
    # A long fade, so a second press lands comfortably inside the first one.
    fade_config.update(
        "slideshow", {"transition_style": "crossfade", "transition_duration_ms": 400}
    )
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return Presenter(fade_config, backend)  # type: ignore[arg-type]


def _six(presenter: Presenter, tmp_path: Path) -> None:
    """A six-item queue, so three fast presses cannot wrap back onto themselves."""
    presenter.set_queue(
        [_image_item(name, tmp_path / f"{name}.jpg") for name in ("a", "b", "c", "d", "e", "f")]
    )


def _settle(presenter: Presenter, item_id: str, *, seconds: float = 12.0) -> None:
    """Drive ``render`` until *item_id* is the settled current item."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        presenter.render()
        current = presenter.current_item
        if current is not None and current.id == item_id and presenter._jump_target is None:
            return
        time.sleep(0.005)
    current = presenter.current_item
    pytest.fail(f"never settled on {item_id!r} (ended on {current.id if current else None!r})")


class TestASecondPressDuringAFadeAdvancesFurther:
    def test_two_fast_next_presses_move_two_items(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """The reported bug: the second press did nothing but re-fade to the same photo."""
        _six(presenter, tmp_path)
        assert presenter.current_item is not None and presenter.current_item.id == "a"

        presenter.next_item()
        # Deliberately no render() between them: the second press lands while the
        # first fade is still in flight, which is the reported sequence.
        presenter.next_item()

        _settle(presenter, "c")

    def test_three_fast_presses_move_three_items(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        _six(presenter, tmp_path)

        presenter.next_item()
        presenter.next_item()
        presenter.next_item()

        _settle(presenter, "d")

    def test_the_in_flight_target_is_what_the_next_press_steps_from(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """Stated as the invariant, so a regression names the cause."""
        _six(presenter, tmp_path)

        presenter.next_item()
        assert presenter._jump_target is not None, "the first press must record a destination"
        assert presenter._jump_target.id == "b"

        presenter.next_item()
        assert presenter._jump_target is not None
        assert presenter._jump_target.id == "c", (
            "the second press must step past b, not re-aim at it"
        )

    def test_two_fast_prev_presses_move_back_two_items(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        _six(presenter, tmp_path)
        presenter.next_item()
        presenter.next_item()
        _settle(presenter, "c")

        presenter.prev_item()
        presenter.prev_item()

        _settle(presenter, "a")

    def test_a_reversed_pair_does_what_it_says(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """Next then Prev while the first fade runs must land back where it started.

        The two presses are independent steps, so their net effect is zero — the
        frame ends on the item it began on.  Getting this wrong in either
        direction would show up as a stuck or doubled jump.
        """
        _six(presenter, tmp_path)

        presenter.next_item()
        presenter.prev_item()

        _settle(presenter, "a")

    def test_presses_across_a_photo_video_boundary_still_step(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A video in the queue must not break the origin lookup.

        The origin is found by identity in the queue; a video that stops and is
        re-presented as its last frame is still the same item, so the lookup must
        hold.
        """
        presenter.set_queue(
            [
                _image_item("a", tmp_path / "a.jpg"),
                _video_item("v", tmp_path / "v.mp4", duration=30.0),
                _image_item("c", tmp_path / "c.jpg"),
                _image_item("d", tmp_path / "d.jpg"),
            ]
        )

        presenter.next_item()
        presenter.next_item()

        _settle(presenter, "c")


class TestTheDestinationDoesNotOutliveTheFade:
    def test_it_is_cleared_once_the_fade_lands(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """A stale destination would mis-direct the NEXT skip one slide later."""
        _six(presenter, tmp_path)
        presenter.next_item()
        assert presenter._jump_target is not None

        _settle(presenter, "b")

        assert presenter._jump_target is None

    def test_a_later_single_press_still_moves_exactly_one(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """The double-press fix must not make a normal press skip two."""
        _six(presenter, tmp_path)
        presenter.next_item()
        presenter.next_item()
        _settle(presenter, "c")
        assert presenter._jump_target is None

        presenter.next_item()

        _settle(presenter, "d")

    def test_a_hard_cut_leaves_no_destination_behind(
        self,
        fade_config: Config,
        backend: FakeBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no fade there is no park, so nothing may be left set."""
        fade_config.update("slideshow", {"transition_style": "none"})
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
        presenter = Presenter(fade_config, backend)  # type: ignore[arg-type]
        _six(presenter, tmp_path)

        presenter.next_item()

        assert presenter._jump_target is None
        assert presenter.current_item is not None
        assert presenter.current_item.id == "b"

    def test_two_fast_presses_with_no_transition_still_step_two(
        self,
        fade_config: Config,
        backend: FakeBackend,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cut has no park to confuse, so repeated presses must be exact."""
        fade_config.update("slideshow", {"transition_style": "none"})
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
        presenter = Presenter(fade_config, backend)  # type: ignore[arg-type]
        _six(presenter, tmp_path)

        presenter.next_item()
        presenter.next_item()

        assert presenter.current_item is not None
        assert presenter.current_item.id == "c"

    def test_a_skip_off_the_end_wraps_once_not_twice(
        self, presenter: Presenter, backend: FakeBackend, tmp_path: Path
    ) -> None:
        """Wrapping is the case most likely to hide a doubled step."""
        presenter.set_queue(
            [
                _image_item("a", tmp_path / "a.jpg"),
                _image_item("b", tmp_path / "b.jpg"),
                _image_item("c", tmp_path / "c.jpg"),
            ]
        )
        presenter.next_item()
        presenter.next_item()
        _settle(presenter, "c")

        presenter.next_item()
        presenter.next_item()

        _settle(presenter, "b")
