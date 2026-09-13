# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Overlay element contract — the one description of "something to be painted".

An :class:`OverlayElement` is what an overlay layer or widget *wants drawn*, with
no knowledge of how.  It is deliberately a frozen dataclass rather than a dict:

* a typo in a dict key (``"colour"`` vs ``"color"``) fails silently at runtime by
  simply not drawing, on a device, in a log nobody reads — a dataclass makes that
  a type error before the code ever reaches the Pi;
* the field set is the documentation, instead of a docstring that drifts;
* it mirrors :class:`~metixel.framing.layout.RenderPlan`, which is already a
  frozen dataclass for the same reason, so the frontend has one compositing
  vocabulary rather than a typed one for geometry and an untyped one for overlays.

Elements are painted in ascending ``z`` — largest first, smallest last (closest to
the viewer).  That preserves the convention the pi3d backend used with GL_LESS
depth testing, so every existing z-offset keeps its meaning unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: What kind of thing to paint.
ElementKind = Literal["rect", "image", "text"]

#: A pixel rectangle as ``(x, y, width, height)``.
PxRect = tuple[float, float, float, float]


@dataclass(frozen=True)
class OverlayElement:
    """One thing to composite over the current frame.

    Only the fields relevant to :attr:`kind` are read; the rest keep their
    defaults.  Construction is validated in :meth:`__post_init__` so a malformed
    element fails at the call site rather than mid-frame.
    """

    kind: ElementKind
    rect: PxRect
    """Position and size, in pixels, in screen coordinates.

    For ``text`` the width and height are ignored — text is anchored at
    ``rect[0], rect[1]`` — so they may be left as ``0``.
    """

    z: float = 0.0
    """Paint order.  Larger paints first, so a smaller z lands on top."""

    alpha: float = 1.0
    """Opacity, 0.0 to 1.0."""

    colour: str = "#ffffff"
    """``#rrggbb``, for ``rect`` and ``text``."""

    image: Any = None
    """Opaque backend handle from ``DisplayBackend.load_image``, for ``image``."""

    text: str = ""
    """The string to draw, for ``text``."""

    size: int = 24
    """Font size in points, for ``text``."""

    rotation: float = 0.0
    """Clockwise rotation in degrees, for ``image`` (the boot spinner)."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Escape hatch for backend-specific options.

    Deliberately explicit and empty by default: it exists so a future backend can
    carry something this contract does not yet model, without tempting a caller
    back into passing loose dicts.
    """

    def __post_init__(self) -> None:
        _require(self.kind in ("rect", "image", "text"), f"unknown kind {self.kind!r}")
        _require(len(self.rect) == 4, f"rect must be (x, y, w, h), got {self.rect!r}")
        _require(0.0 <= self.alpha <= 1.0, f"alpha must be 0..1, got {self.alpha}")
        if self.kind == "image":
            _require(self.image is not None, "an image element needs an image handle")
        if self.kind == "text":
            _require(bool(self.text), "a text element needs text")
            _require(self.size > 0, f"font size must be > 0, got {self.size}")

    # -- Convenience constructors -------------------------------------------

    @classmethod
    def rect_element(
        cls,
        rect: PxRect,
        colour: str,
        *,
        z: float = 0.0,
        alpha: float = 1.0,
    ) -> OverlayElement:
        """A filled rectangle."""
        return cls(kind="rect", rect=rect, colour=colour, z=z, alpha=alpha)

    @classmethod
    def image_element(
        cls,
        image: Any,
        rect: PxRect,
        *,
        z: float = 0.0,
        alpha: float = 1.0,
        rotation: float = 0.0,
    ) -> OverlayElement:
        """A scaled image."""
        return cls(
            kind="image",
            rect=rect,
            image=image,
            z=z,
            alpha=alpha,
            rotation=rotation,
        )

    @classmethod
    def text_element(
        cls,
        text: str,
        position: tuple[float, float],
        *,
        size: int = 24,
        colour: str = "#ffffff",
        z: float = 0.0,
        alpha: float = 1.0,
    ) -> OverlayElement:
        """A text string anchored at ``position``."""
        return cls(
            kind="text",
            rect=(position[0], position[1], 0.0, 0.0),
            text=text,
            size=size,
            colour=colour,
            z=z,
            alpha=alpha,
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
