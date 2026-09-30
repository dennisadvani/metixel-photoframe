# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The overlay must actually reach the screen — all three kinds of it.

``OverlayElement`` describes three kinds of thing to paint: ``rect``, ``image``
and ``text``.  The QML backend mapped every element to a bare string and the scene
drew every one of them as a ``Text``, which meant:

* the boot screen — a black curtain, a logo, a rotating spinner and a progress
  bar, and **no text at all** — painted nothing whatsoever, and
* a notification lost its panel, and (having no position) stacked its strings in
  the top-left corner of the frame.

Nothing raised and nothing was logged, because a blank overlay is a perfectly
valid frame, and neither half was wrong on its own terms — the mapping invented
keys (``x``, ``opacity``) the element type does not have, and the delegate read
exactly those.  So the seam is what these tests pin: everything an element carries
has to arrive at the scene, and the scene has to draw it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from metixel.display.overlay_element import OverlayElement
from metixel.display.qt_qml_backend import _overlay_entry
from metixel.frontend.overlay.boot_layer import BootLayer

_QML_DIR = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display" / "qml"
_FRAME_QML = _QML_DIR / "Frame.qml"


class _FakeDisplay:
    """Enough of a backend for the boot layer to lay itself out.

    The layer only needs a size and somewhere to send its logo/spinner payloads,
    so this keeps the boot screen testable without a window.
    """

    def __init__(self, width: int = 1920, height: int = 1200) -> None:
        self.width = width
        self.height = height
        self.loaded: list[Any] = []

    def load_image(self, payload: Any) -> str:
        self.loaded.append(payload)
        return f"image://metixel/handle{len(self.loaded)}"


class TestEveryKindSurvivesTheMapping:
    """The mapping is the whole feature: an element it drops is an unpainted one."""

    def test_a_rect_keeps_its_geometry_and_colour(self) -> None:
        element = OverlayElement.rect_element((10, 20, 30, 40), "#112233", alpha=0.5)

        entry = _overlay_entry(element)

        assert entry["kind"] == "rect"
        assert (entry["x"], entry["y"], entry["w"], entry["h"]) == (10.0, 20.0, 30.0, 40.0)
        assert entry["colour"] == "#112233"
        assert entry["alpha"] == 0.5

    def test_the_boot_spinner_keeps_its_handle_and_angle(self) -> None:
        element = OverlayElement.image_element("image://metixel/abc", (5, 6, 7, 8), rotation=42.5)

        entry = _overlay_entry(element)

        assert entry["kind"] == "image"
        assert entry["source"] == "image://metixel/abc"
        assert entry["rotation"] == 42.5
        assert (entry["x"], entry["y"], entry["w"], entry["h"]) == (5.0, 6.0, 7.0, 8.0)

    def test_text_keeps_its_anchor_and_size(self) -> None:
        element = OverlayElement.text_element("hello", (3, 4), size=18, colour="#abcdef")

        entry = _overlay_entry(element)

        assert entry["kind"] == "text"
        assert (entry["x"], entry["y"]) == (3.0, 4.0)
        assert entry["text"] == "hello"
        assert entry["size"] == 18
        assert entry["colour"] == "#abcdef"

    def test_a_fully_transparent_element_stays_transparent(self) -> None:
        """``alpha`` must not be defaulted with ``or``, which turns 0.0 into 1.0.

        A fade-in starts at zero, so an element that should be invisible for its
        first frames would instead appear at full strength.
        """
        element = OverlayElement.rect_element((0, 0, 1, 1), "#ffffff", alpha=0.0)

        assert _overlay_entry(element)["alpha"] == 0.0

    def test_an_unusable_image_handle_blanks_only_its_own_element(self) -> None:
        """Another backend hands back an object, which QML's ``source`` cannot use."""
        element = OverlayElement.image_element(object(), (0, 0, 4, 4))

        entry = _overlay_entry(element)

        assert entry["kind"] == "image"
        assert entry["source"] == ""


class TestTheBootScreenReachesTheScene:
    """The real boot layer, mapped for real.

    Deliberately not a hand-written element list: the boot layer is the thing that
    went missing, and it emits no text at all — so a mapping that only understands
    text produces nothing for it, which is exactly the bug.
    """

    @staticmethod
    def _entries() -> list[dict[str, Any]]:
        display = _FakeDisplay()
        layer = BootLayer()
        layer.ensure_ready(display)  # type: ignore[arg-type]
        entries = [_overlay_entry(element) for element in layer.render()]
        assert entries, "the boot layer should paint something while it is up"
        return entries

    def test_the_curtain_covers_the_whole_screen(self) -> None:
        """Without this the slideshow shows through for the whole boot."""
        curtain = self._entries()[0]

        assert curtain["kind"] == "rect"
        assert (curtain["w"], curtain["h"]) == (1920.0, 1200.0)

    def test_every_element_is_something_the_scene_can_draw(self) -> None:
        for entry in self._entries():
            assert entry["kind"] in ("rect", "image", "text"), entry
            if entry["kind"] == "text":
                assert entry["text"], entry
            else:
                assert entry["w"] > 0 and entry["h"] > 0, entry


class TestTheSceneIsWiredForEveryKind:
    """The scene half of the seam, checked structurally.

    A rendering assertion would be stronger, and it was attempted: the delegates a
    ``Repeater`` builds are not reachable offscreen.  ``count`` reflects the model
    immediately, but ``itemAt()`` stays null because the item is never incubated
    without a real render, so there is nothing to inspect.  This half is therefore
    covered twice over — structurally here, and at runtime by
    ``scripts/dev/_smoke_qml_scene.py``, which loads the scene and fails on any QML
    warning.  The dispatch being asserted is exactly what was missing: the delegate
    used to be a bare ``Text`` that never looked at ``kind`` at all.
    """

    @staticmethod
    def _delegate_source() -> str:
        """The overlay Repeater and its delegate — the last block in the scene."""
        source = _FRAME_QML.read_text(encoding="utf-8")
        start = source.index('objectName: "overlay"')
        delegate = source[start:]
        assert "delegate:" in delegate, "the overlay Repeater has no delegate"
        return delegate

    def test_the_delegate_dispatches_on_every_kind(self) -> None:
        delegate = self._delegate_source()

        for kind in ("rect", "image", "text"):
            assert f'modelData.kind === "{kind}"' in delegate, f"no branch for {kind}"

    def test_each_kind_has_its_own_named_child(self) -> None:
        delegate = self._delegate_source()

        for name in ("overlayRect", "overlayImage", "overlayText"):
            assert f'objectName: "{name}"' in delegate, name

    def test_the_delegate_reads_the_fields_the_mapping_sends(self) -> None:
        """The two halves have to agree on names — this is the bug that was shipped.

        The mapping sent ``x``/``opacity`` while the element carried ``rect`` and
        ``alpha``, so both halves were self-consistent and nothing was drawn.
        """
        delegate = self._delegate_source()

        for field in (
            "modelData.x",
            "modelData.y",
            "modelData.w",
            "modelData.h",
            "modelData.alpha",
            "modelData.colour",
            "modelData.source",
            "modelData.rotation",
        ):
            assert field in delegate, f"the scene never reads {field}"


class TestTheSceneInstantiates:
    """The scene must still load, and the overlay must still paint last."""

    @staticmethod
    def _load_scene() -> Any:
        pytest.importorskip("PySide6", reason="PySide6 not installed (no Qt on this host)")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine

        _ = QGuiApplication.instance() or QGuiApplication([])
        engine = QQmlApplicationEngine()
        warnings: list[str] = []
        engine.warnings.connect(lambda items: warnings.extend(i.toString() for i in items))
        engine.load(_FRAME_QML.as_uri())
        roots = engine.rootObjects()
        assert roots, "the scene did not instantiate"
        # The engine must outlive the window, or the scene is torn down.
        return engine, roots[0], warnings

    def test_the_scene_loads_without_warnings(self) -> None:
        _engine, _window, warnings = self._load_scene()

        assert warnings == []

    def test_the_overlay_is_the_last_layer_so_it_paints_on_top(self) -> None:
        """The boot screen and notifications are only visible if this holds.

        QML stacks by declaration order, and the overlay is the only layer whose
        contents are opaque to the rest of the scene — a boot curtain underneath
        the artwork would look exactly like a boot screen that never drew.
        """
        _engine, window, _warnings = self._load_scene()

        names = [child.objectName() for child in window.children() if child.objectName()]

        assert names[-1] == "overlay"
        assert "media" in names and "matte" in names, names

    def test_the_backdrop_is_grouped_with_its_own_media(self) -> None:
        """A backdrop must fade as part of its media, not as a loose layer.

        Declaring the six layers loose instead produces, bottom to top:
        ``prevAmbient, ambient, prevArtwork, artwork`` — which paints the INCOMING
        backdrop UNDER the OUTGOING artwork.  At the ambient band's edges, where no
        artwork covers, the incoming item's blur then shows through the outgoing
        item for the whole crossfade.  Each slot therefore has to be one ``Item``
        with its backdrop and artwork as children, so the item-level opacity makes
        them fade together and cannot drift apart.
        """
        _engine, window, _warnings = self._load_scene()

        # Found by walking the root's direct children, not with findChild: a bare
        # ``Item`` exposes no QML type to ``findChild``'s name filter reliably, and
        # these two are the layer containers themselves.
        by_name = {c.objectName(): c for c in window.children() if c.objectName()}
        slots = {
            "prevMedia": (by_name["prevMedia"], "prevAmbient", "prevArtwork"),
            "media": (by_name["media"], "ambient", "artwork"),
        }

        for slot_name, (slot, backdrop, artwork) in slots.items():
            child_names = {c.objectName() for c in slot.children()}
            assert backdrop in child_names, f"{slot_name} must own its backdrop: {child_names}"
            assert artwork in child_names, f"{slot_name} must own its artwork: {child_names}"
            assert len(child_names) == 2, f"{slot_name} should hold exactly a pair: {child_names}"

    def test_the_rings_composite_over_the_media(self) -> None:
        """Media farthest, rings above it, overlay closest — the whole point.

        This is also what makes video need no special case: the video is media like
        any other, so the mat composites over it by declaration order rather than
        the video painting its own matte.
        """
        _engine, window, _warnings = self._load_scene()

        names = [child.objectName() for child in window.children() if child.objectName()]

        # Declaration order IS paint order, bottom-to-top.
        expected = [
            "background",
            "prevMedia",
            "media",
            "videoOut",
            "whitespace",
            "matte",
            "moulding",
            "overlay",
        ]
        assert names == expected, names

        # The rings composite over the media, and the overlay over everything.
        assert names.index("media") < names.index("matte") < names.index("overlay"), names
        # The video supersedes the poster, so it is above the media group and
        # still below the rings that composite over it.
        assert names.index("media") < names.index("videoOut") < names.index("matte"), names
