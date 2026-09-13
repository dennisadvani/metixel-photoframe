# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Abstract base class for display backends.

All rendering in Metixel Photoframe goes through this interface. The presentation
layer and the overlay system never import hardware-specific libraries directly.

Geometry comes from :mod:`metixel.framing` — a backend is handed a
:class:`~metixel.framing.layout.RenderPlan` of pixel rectangles and paints it.
Backends therefore hold **no** layout knowledge, and the layout maths stays
testable without Qt, mpv or a GPU.

Consequently a backend is not a bag of drawing primitives. It is a *surface*:
it can present a whole frame, host a video stream, and control display power.
The old per-primitive surface (``draw_rect`` / ``draw_image`` /
``draw_crossfade`` / ``load_texture`` / ``update_texture`` / ``clear_depth`` …)
is gone. It existed to drive pi3d's immediate-mode texture pipeline, and it
forced every caller to reason about GL state and depth ordering that the
retained-mode canvas now owns outright.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import numpy as np

from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)


class DisplayBackend(ABC):
    """Hardware-agnostic display surface.

    Implementations:
    - :class:`~metixel.display.qt_backend.PySide6Backend` (Raspberry Pi: cage + Qt + mpv)
    - :class:`~metixel.display.tk_backend.TkBackend` (Desktop dev: tkinter)
    - :class:`~metixel.display.wayland_backend.WaylandBackend` (Phase 2: not yet implemented)
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

    @property
    def supports_video(self) -> bool:
        """Whether this backend can play video.

        ``False`` on software renderers (tkinter), which have no video pipeline.
        The presentation layer filters video items out of the playlist rather
        than attempting playback once per item per cycle.

        Declared on the ABC, not merely on the implementations: the playlist
        filter in ``presentation/queue.py`` reads it, so a backend that silently
        omitted it would raise at the first video rather than degrading.
        """
        return True

    # -- Lifecycle -----------------------------------------------------------

    @abstractmethod
    def create(
        self,
        width: int = 1920,
        height: int = 1080,
        fullscreen: bool = True,
        hide_cursor: bool = True,
        fps_limit: int = 30,
        refresh_rate: int = 0,
        rotation: int = 0,
        **kwargs: Any,
    ) -> None:
        """Initialize the display and create the rendering surface.

        Args:
            width: Desired display width in pixels.
            height: Desired display height in pixels.
            fullscreen: Whether to use fullscreen mode.
            hide_cursor: Whether to hide the mouse cursor.
            fps_limit: Maximum frames per second.
            refresh_rate: Desired refresh rate in Hz (0 = auto/native).
            rotation: Screen rotation in degrees clockwise (0, 90, 180, 270).
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
        """Present the composed frame to the screen."""

    # -- Frame presentation --------------------------------------------------

    @abstractmethod
    def present(self, plan: RenderPlan, image: Any = None, alpha: float = 1.0) -> None:
        """Paint one complete frame for *plan*.

        The single rendering entry point.  Implementations paint the plan's
        layers in the order the framing specification mandates:

            ambient fill -> artwork -> whitespace -> mat -> moulding

        Ambient fill is the only full-canvas layer; the rest are annuli that are
        disjoint from the artwork, which is why a single pass in that order
        needs no depth buffer.

        Args:
            plan: Pixel geometry from
                :meth:`metixel.framing.layout.LayoutEngine.compute`.
            image: An opaque handle from :meth:`load_image` for the artwork, or
                ``None`` to paint the frame without artwork (a pure mat preview,
                or a video whose frames arrive out of band).
            alpha: Opacity of the *artwork*, used by the crossfade.  The frame
                rings always paint opaque, so a fading photo does not reveal the
                matte behind it.  Painting twice at complementary alpha is what
                implements the transition — there is no separate blend entry
                point, which keeps transitions independent of backend blend
                capability.
        """
        ...

    # -- Overlay -------------------------------------------------------------

    def present_overlay(self, elements: list[OverlayElement]) -> None:  # noqa: B027
        """Composite overlay elements on top of the current frame.

        Elements are :class:`~metixel.display.overlay_element.OverlayElement`
        instances, already flattened and sorted by the overlay manager (largest
        ``z`` first).

        Kept separate from :meth:`present` on purpose: the slideshow frame is
        composed once per item, whereas overlay layers animate every frame
        (boot spinner, message slide-in).  Folding them together would force a
        full re-composite of the matte on every animation tick.

        Default is a no-op, so a backend may present frames without overlay
        support rather than being forced to implement it.
        """

    # -- Artwork -------------------------------------------------------------

    @abstractmethod
    def load_image(self, path: Path | np.ndarray | bytes) -> Any:
        """Load an image into a backend-native handle.

        For a video item, pass the pre-generated first-frame JPEG: the backend
        shows it as a poster until :meth:`play_video` takes over.  Frames are
        produced by the backend media pipeline during Phase 2 (OPTIMISE); the
        presentation layer never runs ffmpeg or ffprobe.

        Args:
            path: A filesystem path, an ``(H, W, 3/4)`` numpy array, or encoded
                image ``bytes``.  The bytes form exists because the preload
                worker decodes off-thread and hands over a payload rather than
                letting Qt objects be constructed on a worker thread.

        Returns:
            An opaque handle, or ``None`` if the image could not be loaded.
        """
        ...

    @abstractmethod
    def unload_image(self, handle: Any) -> None:
        """Release an image handle returned by :meth:`load_image`."""
        ...

    # -- Video ---------------------------------------------------------------

    def play_video(self, path: Path, plan: RenderPlan) -> bool:  # noqa: B027
        """Begin video playback, positioned per *plan*.

        Returns ``True`` if playback started.  Backends with no video pipeline
        return ``False`` (and report ``supports_video = False``) so the caller
        can advance instead of waiting on a stream that will never arrive.

        Video renders **under** the frame's ring layers: the presentation layer
        then calls :meth:`present` with ``image=None`` to paint the matte over
        the live video.  That is how the virtual mat composites on top of a
        playing video without a second framebuffer.
        """
        return False

    def stop_video(self) -> None:  # noqa: B027
        """Stop playback and release the video pipeline.

        Must be idempotent: it is called on item advance, on queue reset, and
        during shutdown.
        """

    def pause_video(self, paused: bool = True) -> None:  # noqa: B027
        """Pause or resume playback without tearing down the pipeline.

        Used by the slideshow pause command and, in 2.1.0, by an open on-screen
        menu.  Preferred over SIGSTOP so the decoder keeps its buffers warm and
        resume is immediate.
        """

    def video_playing(self) -> bool:  # noqa: B027
        """Whether video is currently playing (not paused and not ended)."""
        return False

    def video_finished(self) -> bool:  # noqa: B027
        """Whether the current video has reached its end.

        The presentation state machine polls this instead of guessing from
        timers, so a video that ends early advances immediately.
        """
        return False

    # -- Display Control -----------------------------------------------------

    @abstractmethod
    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Set the clear colour for the display background.

        Args:
            color: RGBA tuple with values 0.0–1.0.
        """
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clear the display to the background colour."""

    @abstractmethod
    def display_power(self, on: bool) -> None:
        """Turn the physical display on or off.

        Implementations use the tiered fallback chain in
        :mod:`metixel.display.hardware` (wlr-randr → DRM DPMS sysfs →
        ``vcgencmd``).  Must never raise: failing to sleep the panel must not
        take down the frame.
        """

    # -- Diagnostics ---------------------------------------------------------

    def connected_output(self) -> str | None:
        """Return the connected output name (e.g. ``"HDMI-A-2"``), or ``None``.

        Reported to the web UI so a user can see which port their monitor is
        on.
        """
        return None

    def list_modes(self) -> list[dict[str, Any]]:
        """Return the display modes the monitor and the host both support.

        Used to populate the resolution dropdown.  Returns an empty list when
        the information is unavailable.
        """
        return []
