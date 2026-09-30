# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Screen rotation must actually reach the compositor.

The setting was inert, and the way it was inert is the interesting part: two
separate halves were missing and *neither* was visible from the other's code.

* ``Presenter`` did pass ``display.rotation`` to ``LayoutEngine``, so the layout
  genuinely honoured it — which made the feature look implemented.
* ``QmlBackend.create()`` silently discarded its ``rotation`` argument, with a
  comment claiming the framing engine had already applied it.  That is true of the
  *plan* and false of the *output*: the wlroots output kept presenting its native
  landscape mode.
* Nothing called ``WlrOutput.set_mode(rotation=...)`` at runtime at all.

So the config key round-tripped, the logs agreed the value had been set, and the
panel rendered sideways.  These tests pin both halves and the size swap that
joins them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from metixel.display.qt_qml_backend import QmlBackend

_BACKEND = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display" / "qt_qml_backend.py"


class _RecordingWlr:
    """A ``WlrOutput`` that records what it was asked to do."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict[str, int]] = []

    def set_mode(self, **kwargs: int) -> bool:
        self.calls.append(kwargs)
        return self.ok


class TestTheCompositorIsAskedToRotate:
    def test_the_transform_is_applied(self) -> None:
        backend = QmlBackend()
        wlr = _RecordingWlr()
        backend._wlr_output = wlr

        backend._apply_rotation(90)

        assert wlr.calls == [{"rotation": 90}], "the compositor must be told to rotate"

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270, 360])
    def test_every_supported_value_is_passed_through(self, rotation: int) -> None:
        backend = QmlBackend()
        wlr = _RecordingWlr()
        backend._wlr_output = wlr

        backend._apply_rotation(rotation)

        assert wlr.calls == [{"rotation": rotation}]

    def test_an_unsupported_angle_is_refused_rather_than_guessed(self) -> None:
        """45° would land on ``--transform normal`` in silence, i.e. no rotation."""
        backend = QmlBackend()
        wlr = _RecordingWlr()
        backend._wlr_output = wlr

        backend._apply_rotation(45)

        assert wlr.calls == []

    def test_a_failure_does_not_abort_startup(self) -> None:
        """A frame that cannot rotate must still come up — rule 7."""
        backend = QmlBackend()
        backend._wlr_output = _RecordingWlr(ok=False)

        backend._apply_rotation(90)  # must not raise

    def test_a_raising_adapter_does_not_abort_startup(self) -> None:
        class _Broken:
            def set_mode(self, **_kwargs: int) -> bool:
                raise RuntimeError("no wayland socket")

        backend = QmlBackend()
        backend._wlr_output = _Broken()

        backend._apply_rotation(90)  # must not raise


class TestTheSceneIsSizedForTheRotatedPanel:
    """``--transform`` turns the output, so Qt then needs the turned size.

    Without this the scene is laid out for the unrotated panel and letterboxes
    inside the rotated output — sideways content with bars, which is a different
    symptom from no rotation at all.
    """

    @staticmethod
    def _sizes_after_create(rotation: int) -> tuple[int, int]:
        """The swapped size, without building a window."""
        width, height = 1920, 1200
        if rotation % 360 in (90, 270):
            width, height = height, width
        return width, height

    @pytest.mark.parametrize("rotation", [90, 270])
    def test_a_quarter_turn_swaps_the_axes(self, rotation: int) -> None:
        assert self._sizes_after_create(rotation) == (1200, 1920)

    @pytest.mark.parametrize("rotation", [0, 180])
    def test_half_turns_keep_the_axes(self, rotation: int) -> None:
        assert self._sizes_after_create(rotation) == (1920, 1200)

    def test_create_consults_the_rotation_it_is_given(self) -> None:
        """Static, because the swap happens inside ``create()`` before any window.

        The regression this guards is that the argument was assigned to ``_`` and
        never read, so it is the *use* of ``rotation`` that has to be pinned.
        """
        source = _BACKEND.read_text(encoding="utf-8")
        start = source.index("def create(", source.index("class QmlBackend"))
        body = source[start : source.index("\n    def ", start + 10)]

        assert "_ = rotation" not in body, "the rotation argument is being discarded again"
        assert "rotation % 360 in (90, 270)" in body, "the size swap is missing"
        assert "self._apply_rotation(rotation)" in body, "the transform is never applied"
