# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for :mod:`metixel.display.overlay_element` — the compositing contract.

The point of the dataclass is that a malformed element fails *before* it reaches
the Pi.  These tests pin the validation that makes that true, plus the one piece
of behaviour the contract encodes: paint order runs from the largest ``z`` down.
"""

from __future__ import annotations

import pytest

from metixel.display.overlay_element import OverlayElement


class TestRectElement:
    def test_constructor_sets_kind_and_colour(self) -> None:
        element = OverlayElement.rect_element((1, 2, 3, 4), "#ff0000", z=5, alpha=0.5)
        assert element.kind == "rect"
        assert element.rect == (1, 2, 3, 4)
        assert element.colour == "#ff0000"
        assert element.z == 5
        assert element.alpha == 0.5

    def test_alpha_defaults_to_opaque(self) -> None:
        assert OverlayElement.rect_element((0, 0, 1, 1), "#000000").alpha == 1.0


class TestTextElement:
    def test_constructor_anchors_at_position(self) -> None:
        element = OverlayElement.text_element("hi", (10, 20), size=14)
        assert element.kind == "text"
        assert element.text == "hi"
        assert element.size == 14
        # Width/height are unused for text, so they are zero rather than a guess.
        assert element.rect == (10.0, 20.0, 0.0, 0.0)


class TestImageElement:
    def test_constructor_carries_handle_and_rotation(self) -> None:
        handle = object()
        element = OverlayElement.image_element(handle, (0, 0, 8, 8), rotation=90.0)
        assert element.kind == "image"
        assert element.image is handle
        assert element.rotation == 90.0


class TestValidation:
    """Each case here is a mistake that would otherwise fail silently on a device.

    A dict-based element with a mistyped key simply never drew — on the frame,
    in a log nobody reads.  These assertions convert that class of bug into a
    startup error.
    """

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown kind"):
            OverlayElement(kind="sprite", rect=(0, 0, 1, 1))  # type: ignore[arg-type]

    def test_image_without_a_handle_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs an image handle"):
            OverlayElement(kind="image", rect=(0, 0, 1, 1))

    def test_text_without_text_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs text"):
            OverlayElement(kind="text", rect=(0, 0, 0, 0))

    def test_text_without_a_font_size_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="font size"):
            OverlayElement(kind="text", rect=(0, 0, 0, 0), text="hi", size=0)

    def test_malformed_rect_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"rect must be"):
            OverlayElement(kind="rect", rect=(0, 0, 1))  # type: ignore[arg-type]

    @pytest.mark.parametrize("alpha", [-0.1, 1.1, 2.0])
    def test_out_of_range_alpha_is_rejected(self, alpha: float) -> None:
        with pytest.raises(ValueError, match="alpha must be"):
            OverlayElement(kind="rect", rect=(0, 0, 1, 1), alpha=alpha)

    def test_elements_are_immutable(self) -> None:
        """Frozen so a layer cannot mutate an element the canvas is painting."""
        from dataclasses import FrozenInstanceError

        element = OverlayElement.rect_element((0, 0, 1, 1), "#ffffff")
        with pytest.raises(FrozenInstanceError):
            element.alpha = 0.5  # type: ignore[misc]


class TestPaintOrder:
    def test_largest_z_paints_first(self) -> None:
        """Sorting descending by z keeps the old GL_LESS convention intact.

        This is the property the overlay manager relies on, and the reason
        existing z-offsets in the layers did not need adjusting when the drawing
        model changed.
        """
        elements = [
            OverlayElement.rect_element((0, 0, 1, 1), "#000000", z=0.0),
            OverlayElement.rect_element((0, 0, 1, 1), "#000000", z=0.02),
            OverlayElement.rect_element((0, 0, 1, 1), "#000000", z=0.01),
        ]
        ordered = sorted(elements, key=lambda e: e.z, reverse=True)
        assert [e.z for e in ordered] == [0.02, 0.01, 0.0]
