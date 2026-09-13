# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Overlay layer — abstract base for rendering layers on top of the slideshow.

Layers are drawn in z-order (farthest first, closest last) after clearing
the depth buffer.  pi3d uses GL_LESS depth testing: **every draw call must
have a unique z value** — identical z-values fail the depth test.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from metixel.display.backend import DisplayBackend


class OverlayLayer(ABC):
    """A rendering layer drawn on top of the slideshow.

    Each layer has a *z_base* that determines its stacking position
    relative to other layers.  Lower z_base = closer to camera = on top.

    Subclasses implement :meth:`update` and :meth:`draw` and manage
    their own z-offsets for individual elements within the layer.

    Layer ordering (closest → farthest):
        Boot Screen   z_base = 0.0
        Messages      z_base = 0.01
        Widgets       z_base = 0.02
        Video         z_base = 0.03
    """

    # -- Reserved z-base values for each layer type -------------------------
    Z_BOOT = 0.0  # Boot screen (closest to camera)
    Z_MESSAGES = 0.01  # System pop-up messages
    Z_WIDGETS = 0.02  # Persistent widgets (clock, weather, etc.)
    Z_VIDEO = 0.03  # Video overlay (future)

    def __init__(self, name: str, z_base: float) -> None:
        self._name = name
        self._z_base = z_base
        self._visible = True
        self._z_offset = 0.0  # Incremented per-element for unique z

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable layer name (for logging)."""
        return self._name

    @property
    def z_base(self) -> float:
        """Base z-value for this layer.  Lower = closer to camera."""
        return self._z_base

    @property
    def visible(self) -> bool:
        """Whether this layer should be drawn."""
        return self._visible

    @visible.setter
    def visible(self, value: bool) -> None:
        self._visible = value

    # -- Z-value helpers ----------------------------------------------------

    def next_z(self, step: float = 0.001) -> float:
        """Return a unique z-value within this layer.

        Each call returns a slightly lower z-value (closer to camera)
        so elements drawn in sequence stack correctly with GL_LESS.

        Args:
            step: Micro-offset between consecutive elements.
        """
        z = self._z_base + self._z_offset
        self._z_offset -= step  # Next element gets lower z = closer
        return z

    def reset_z(self) -> None:
        """Reset the z-offset counter for a new frame."""
        self._z_offset = 0.0

    # -- Optional layer actions ---------------------------------------------
    # Default no-ops so the messages layer can be handled generically via
    # ``get_layer()``; subclasses that support these override them.

    def show(
        self,
        title: str = "",
        body: str = "",
        severity: str = "info",
        duration: float = 5.0,
        icon: str = "",
    ) -> str:  # noqa: B027
        """Queue a message and return its id (no-op unless supported)."""
        return ""

    def dismiss(self, msg_id: str) -> bool:  # noqa: B027
        """Dismiss a message by id (no-op unless supported)."""
        return False

    def dismiss_all(self) -> int:  # noqa: B027
        """Dismiss all messages (no-op unless supported)."""
        return 0

    # -- Interface -----------------------------------------------------------

    @abstractmethod
    def update(self, shared_state: dict[str, Any] | None = None) -> None:
        """Called every frame.  Advance animations, process state."""
        ...

    @abstractmethod
    def draw(self, backend: DisplayBackend) -> None:
        """Called every frame after :meth:`update`.  Render layer content.

        Superseded by :meth:`render`.  It is retained on the interface because
        the abstract method is what forces every layer to state its intent, but
        a layer can no longer issue draw calls: the backend exposes
        ``present(plan)`` rather than ``draw_image``/``draw_rect``, so a
        ``draw()`` that called those would fail.
        """
        ...

    def render(self) -> list[dict[str, Any]]:
        """Return this layer's elements for the current frame.

        Each element is a plain dict, so an overlay layer carries no rendering
        dependency and stays testable without a backend or Qt:

            {
                "kind": "rect",           # "rect" | "image" | "text"
                "rect": (x, y, w, h),     # pixels, screen coordinates
                "colour": "#rrggbb",      # rect only
                "image": <handle>,        # image only
                "alpha": 0.0..1.0,        # optional, default 1.0
                "rotation": degrees,      # optional, images only
                "text": "...",            # text only
                "size": 24,               # text only, points
                "z": 0.0,                 # paint order
            }

        Elements are composited in ascending ``z`` — the largest z paints first,
        the smallest last (closest to the viewer).  That matches the convention
        the pi3d backend used with GL_LESS depth testing, so existing z-offsets
        keep their meaning unchanged.

        The default returns nothing, so a layer that has not been ported yet
        simply draws nothing instead of crashing the frame.  Deliberate: a
        missing overlay must never take down the slideshow.
        """
        return []
