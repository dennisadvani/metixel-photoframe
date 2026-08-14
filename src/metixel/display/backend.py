# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Abstract base class for display backends.

All rendering in Metixel Photoframe goes through this interface. The presentation
engine and widget layer never import hardware-specific libraries directly.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class DisplayBackend(ABC):
    """Hardware-agnostic interface for 2D rendering.

    Implementations:
    - :class:`~metixel.display.dispmanx_backend.Pi3dBackend` (Phase 1: pi3d)
    - :class:`~metixel.display.wayland_backend.WaylandBackend` (Phase 2: PyOpenGL)
    - :class:`~metixel.display.tk_backend.TkBackend` (Desktop dev: tkinter)
    """

    # -- Properties ----------------------------------------------------------

    @property
    @abstractmethod
    def width(self) -> int:
        """Display width in pixels."""
        ...

    @property
    @abstractmethod
    def height(self) -> int:
        """Display height in pixels."""
        ...

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Whether the display loop is active."""
        ...

    # -- Lifecycle -----------------------------------------------------------

    @abstractmethod
    def create(
        self,
        width: int = 1920,
        height: int = 1080,
        fullscreen: bool = True,
        hide_cursor: bool = True,
        fps_limit: int = 30,
        **kwargs: Any,
    ) -> None:
        """Initialize the display and create the rendering surface.

        Args:
            width: Desired display width in pixels.
            height: Desired display height in pixels.
            fullscreen: Whether to use fullscreen mode.
            hide_cursor: Whether to hide the mouse cursor.
            fps_limit: Maximum frames per second.
        """
        ...

    @abstractmethod
    def destroy(self) -> None:
        """Tear down the display and release GPU resources."""
        ...

    @abstractmethod
    def loop_running(self) -> bool:
        """Check if the main render loop should continue.

        Returns False on window close, escape key, or shutdown signal.
        """
        ...

    @abstractmethod
    def swap_buffers(self) -> None:
        """Present the rendered frame to the screen."""
        ...

    # -- 2D Rendering Primitives ---------------------------------------------

    @abstractmethod
    def draw_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        color: tuple[float, float, float, float] = (0, 0, 0, 1),
        z: float = 0.0,
    ) -> None:
        """Draw a filled rectangle at pixel coordinates.

        Args:
            x, y: Top-left corner in pixels.
            w, h: Width and height in pixels.
            color: RGBA tuple with values 0.0–1.0.
            z: Z-order (higher = in front).
        """
        ...

    @abstractmethod
    def draw_image(
        self,
        texture: Any,
        x: float,
        y: float,
        w: float,
        h: float,
        alpha: float = 1.0,
        rotation: float = 0.0,
        z: float = 0.0,
        uv_offset: tuple[float, float] = (0.0, 0.0),
        uv_scale: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        """Draw a textured rectangle (sprite).

        Args:
            texture: A backend-specific texture handle.
            x, y: Position in pixels.
            w, h: Size in pixels.
            alpha: Opacity (0.0 = transparent, 1.0 = opaque).
            rotation: Rotation in degrees around center.
            z: Z-order.
            uv_offset: Texture coordinate offset (for Ken Burns pan).
            uv_scale: Texture coordinate scale (for Ken Burns zoom).
        """
        ...

    def draw_crossfade(  # noqa: B027
        self,
        tex_current: Any,
        tex_next: Any,
        blend: float,
        current_rect: tuple[float, float, float, float] | None = None,
        next_rect: tuple[float, float, float, float] | None = None,
        slide_offset_current: float = 0.0,
        slide_offset_next: float = 0.0,
    ) -> None:
        """Draw a crossfade or slide between two textures in a single GPU pass.

        Uses a custom blend shader that mixes two textures per-pixel.
        Default implementation is a no-op — backends that support GPU
        blending (Pi3dBackend) override this.

        Args:
            tex_current: Texture for the outgoing image.
            tex_next: Texture for the incoming image.
            blend: 0.0 = fully current, 1.0 = fully next.
            current_rect: (x, y, w, h) of the current image on screen.
            next_rect: (x, y, w, h) of the next image on screen.
            slide_offset_current: Horizontal pixel shift for current texture.
            slide_offset_next: Horizontal pixel shift for next texture.
        """

    # -- Texture Management --------------------------------------------------

    @abstractmethod
    def load_texture(self, path: Path | np.ndarray, **kwargs: Any) -> Any:
        """Load an image into a GPU texture.

        Args:
            path: Path to an image file, or a numpy array (H, W, 3/4).
            **kwargs: Backend-specific options (e.g., mipmap, format).

        Returns:
            An opaque texture handle.
        """
        ...

    @abstractmethod
    def unload_texture(self, texture: Any) -> None:
        """Release a GPU texture from memory.

        Args:
            texture: A texture handle previously returned by :meth:`load_texture`.
        """
        ...

    def update_texture(self, texture: Any, data: np.ndarray) -> None:
        """Update an existing texture's pixel data in-place.

        Used by the video player to push new frames to the GPU without
        creating/destroying texture objects every frame. The *data* array
        must have the same dimensions and format as the original texture.

        Default implementation falls back to unload + reload — backends
        that support in-place updates (pi3d, PyOpenGL) should override.

        Args:
            texture: A texture handle previously returned by :meth:`load_texture`.
            data: New pixel data as a numpy array (H, W, 3) or (H, W, 4).
        """
        # Default: unload old, load new (works everywhere but is slow)
        self.unload_texture(texture)
        return self.load_texture(data)

    def gpu_memory_info(self) -> dict[str, Any] | None:
        """Return GPU memory usage statistics, or ``None`` if unavailable.

        Subclasses on Raspberry Pi hardware should override to read from
        ``vcgencmd`` and ``/sys/kernel/debug/dri/0/bo_stats``.

        Returns:
            Dict with keys like ``gpu_total_mb``, ``reloc_used_mb``,
            ``v3d_bo_count``, ``v3d_bo_kb``, or ``None`` on non-Pi
            platforms.
        """
        return None

    def flush_gpu(self) -> None:  # noqa: B027
        """Block until all pending GPU operations complete.

        Subclasses should call ``glFinish()`` or equivalent to ensure
        texture uploads, shader dispatches, and buffer writes are
        fully committed before the CPU proceeds.  Critical on
        memory-constrained hardware where ``free_after_load`` may
        release source buffers before DMA transfers finish.

        Default is a no-op — safe for dev backends without a GPU.
        """
        pass

    def clear_depth(self) -> None:  # noqa: B027
        """Clear the depth buffer so overlay draws appear on top.

        The slideshow's rendering may write depth values that would occlude
        overlay content (widgets, pop-up messages).  Call this after the
        slideshow renders and before drawing overlay elements.

        Default is a no-op — safe for software renderers without a depth
        buffer (tkinter); GPU backends should clear their depth buffer.
        """
        pass

    # -- Text Rendering ------------------------------------------------------

    @abstractmethod
    def draw_text(
        self,
        text: str,
        x: float,
        y: float,
        font_size: int = 24,
        color: tuple[float, float, float, float] = (1, 1, 1, 1),
        z: float = 10.0,
    ) -> None:
        """Render a text string at the given position.

        Args:
            text: The string to render.
            x, y: Position in pixels.
            font_size: Font size in points.
            color: RGBA color.
            z: Z-order.
        """
        ...

    # -- Display Control -----------------------------------------------------

    @abstractmethod
    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Set the clear color for the display background.

        Args:
            color: RGBA tuple (0.0–1.0).
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear the display to the background color."""
        ...

    # -- Power Management (Phase 1: vcgencmd; Phase 2: DPMS) -----------------

    @abstractmethod
    def display_power(self, on: bool) -> None:
        """Turn the physical display on or off.

        Phase 1 (dispmanx): Uses ``vcgencmd display_power``.
        Phase 2 (DRM/KMS): Uses DPMS or compositor protocol.
        """
        ...
