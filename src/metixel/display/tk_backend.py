# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tkinter-based Development Display Backend.

Used for local development on machines without Qt or a GPU.  Tkinter ships with
Python on all platforms, making this the most portable dev backend available and
the reason the slideshow can be exercised end-to-end on a desktop.

Renders a :class:`~metixel.framing.layout.RenderPlan` to a tkinter Canvas using
software blitting.  Video is not supported (``supports_video`` is ``False``), so
the presentation layer skips video items rather than failing once per cycle.
"""

from __future__ import annotations

import contextlib
import logging
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageTk

from metixel.display.backend import DisplayBackend
from metixel.framing.layout import RenderPlan

logger = logging.getLogger(__name__)


class TkBackend(DisplayBackend):
    """Tkinter display backend for zero-dependency desktop development.

    Renders through a tkinter Canvas using Pillow for scaling and cropping —
    no external libraries beyond Pillow, which is already a core dependency.

    tkinter is only needed on desktop dev machines.  On a headless Pi (no
    tkinter) this module can still be IMPORTED without error;
    ``display/__init__.py``'s ``detect_backend()`` only imports it when it is
    actually creating a TkBackend.
    """

    def __init__(self) -> None:
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._running: bool = False
        self._w: int = 1280
        self._h: int = 720
        self._bg_color: str = "black"
        self._fps_limit: int = 30
        self._images: dict[int, Image.Image] = {}  # handle → PIL Image
        # (handle, w, h) → tk PhotoImage.  Bounded, because each entry holds a
        # full-resolution image and a slideshow runs for weeks.
        self._photo_cache: dict[tuple[Any, int, int], ImageTk.PhotoImage] = {}
        self._texture_counter: int = 0
        self._frame_delay_ms: int = 33  # ~30 FPS

    # -- Properties ----------------------------------------------------------

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def supports_video(self) -> bool:
        """Software renderer — no video pipeline, so video items are skipped."""
        return False

    # -- Lifecycle -----------------------------------------------------------

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
        # Use a manageable window size on desktop (clamp to 1280×720 max).
        # If width/height is 0 (auto-detect requested), use the max size.
        if width <= 0:
            width = 1280
        if height <= 0:
            height = 720
        self._w = min(width, 1280)
        self._h = min(height, 720)
        self._fps_limit = fps_limit
        self._frame_delay_ms = max(1, int(1000 / fps_limit))
        # Refresh rate and rotation are not applicable to the tkinter dev
        # backend — accepted for interface compatibility and ignored.
        self._refresh_rate = refresh_rate
        self._rotation = rotation

        self._root = tk.Tk()
        self._root.title("Metixel Photoframe — Dev Mode (Tkinter)")
        self._root.geometry(f"{self._w}x{self._h}")
        self._root.configure(bg="black")

        if hide_cursor:
            self._root.config(cursor="none")

        self._canvas = tk.Canvas(
            self._root,
            width=self._w,
            height=self._h,
            bg="black",
            highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)

        # Bind keys
        self._root.bind("<Escape>", lambda e: self._stop())
        self._root.bind("<space>", lambda e: self._toggle_pause())

        # Close via window manager
        self._root.protocol("WM_DELETE_WINDOW", self._stop)

        self._running = True
        self._pending_pause: bool = False  # Flag set by spacebar
        logger.info("TkBackend created: %dx%d @ %d FPS", self._w, self._h, fps_limit)

    def destroy(self) -> None:
        self._running = False
        self._images.clear()
        self._photo_cache.clear()
        if self._root:
            with contextlib.suppress(Exception):
                self._root.destroy()
            self._root = None
        self._canvas = None
        logger.info("TkBackend destroyed")

    def loop_running(self) -> bool:
        """Process one frame of tkinter events, then return.

        The caller is responsible for calling this in a loop at the
        desired frame rate. We call ``update()`` once to process events
        without blocking.
        """
        if not self._running or self._root is None:
            return False
        try:
            self._root.update()
        except tk.TclError:
            self._running = False
            return False
        return True

    def swap_buffers(self) -> None:
        """No-op — tkinter Canvas renders immediately."""
        pass

    def schedule(self, tick: Callable[[], bool]) -> None:
        """Drive *tick* from a plain loop, pumping tkinter events each frame.

        tkinter is cooperative: the loop owns the thread and calls ``update()``
        to drain events, so the tick runs on the same thread between pumps.
        Timing is a monotonic deadline rather than ``after()``, because the
        slideshow's own clock is what decides when a frame changes.
        """
        period = 1.0 / max(1, self._fps_limit)
        next_frame = time.monotonic()
        while self._running and self.loop_running():
            if not tick():
                break
            next_frame += period
            sleep_for = next_frame - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # We are behind: resynchronise rather than accumulate debt, or a
                # slow stretch would make the loop spin to catch up.
                next_frame = time.monotonic()

    def quit(self) -> None:
        """Stop the loop; safe from another thread (just clears a flag)."""
        self._running = False

    # -- Frame presentation --------------------------------------------------

    def present(self, plan: RenderPlan, image: Any = None, alpha: float = 1.0) -> None:
        """Software-composite one frame from *plan*.

        Paints in the order the framing specification mandates — ambient fill,
        artwork, whitespace, mat, moulding — so this backend and the Qt one
        agree on layering without sharing pixel code.  Tk's Canvas has no alpha
        compositing and no z-buffer, which is exactly why the plan's ring layers
        are disjoint from the artwork: the order alone is sufficient, and no
        clipping or blending is required.

        ``alpha`` fades the artwork only.  Tk cannot blend, so a transition here
        degrades to a cut (see the alpha guard below) — the desktop dev backend
        is not the place to reproduce a crossfade, and pretending otherwise would
        mean a software blend of every frame.
        """
        if self._canvas is None:
            return

        # Everything for this frame is tagged so clearing is a single delete
        # rather than a full canvas teardown.
        self._canvas.delete("frame")

        if plan.ambient is not None:
            self._rect(plan.ambient, plan.ambient_colour)

        # A partially transparent artwork would need real alpha compositing; show
        # it opaque above the midpoint and omit it below, which reads as a cut.
        if image is not None and alpha >= 0.5:
            self._artwork(image, plan)

        for rect in plan.whitespace:
            self._rect(rect, plan.whitespace_colour)
        for rect in plan.matte:
            self._rect(rect, plan.matte_colour)
        for rect in plan.moulding:
            self._rect(rect, "#000000")

    def _rect(self, rect: tuple[float, float, float, float], colour: str) -> None:
        """Fill a plan rectangle with a ``#rrggbb`` colour."""
        if self._canvas is None:
            return
        x, y, w, h = rect
        if w <= 0 or h <= 0:
            return
        self._canvas.create_rectangle(
            x,
            y,
            x + w,
            y + h,
            fill=colour,
            outline="",
            tags="frame",
        )

    def _artwork(self, handle: Any, plan: RenderPlan) -> None:
        """Draw the artwork through the plan's source→destination mapping.

        ``artwork_src`` is the sub-rectangle of the source image to sample, which
        is how ``overflow="crop"`` discards the parts of a photo that fall
        outside the Mat Window.  Honouring it here keeps cover-cropping identical
        to the Qt backend instead of re-deriving it per backend.

        ``source_window`` crosses from the plan's media space into this image's
        pixels.  That is the identity for a photo, but a video is drawn as its
        pre-generated poster, which ffmpeg has already shrunk to fit the screen —
        cropping in video pixels samples past the poster's edge, and PIL pads the
        overhang black exactly as ``QImage.copy`` does.
        """
        pil_img = self._images.get(handle) if isinstance(handle, int) else handle
        if pil_img is None or self._canvas is None:
            return

        dx, dy, dw, dh = plan.artwork_dst
        sx, sy, sw, sh = plan.artwork_src
        if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
            return
        sx, sy, sw, sh = plan.source_window(pil_img.width, pil_img.height)

        try:
            frame = pil_img
            if (sx, sy, sw, sh) != (0.0, 0.0, float(pil_img.width), float(pil_img.height)):
                frame = pil_img.crop((round(sx), round(sy), round(sx + sw), round(sy + sh)))
            resized = frame.resize((max(1, int(dw)), max(1, int(dh))), Image.Resampling.LANCZOS)
        except Exception:
            logger.debug("Failed to render artwork for handle %s", handle, exc_info=True)
            return

        key = (handle if isinstance(handle, int) else id(handle), round(dw), round(dh))
        if key not in self._photo_cache:
            self._photo_cache[key] = ImageTk.PhotoImage(resized)
            # Bound the cache: the plan changes size between items, and each
            # entry holds a full-resolution PhotoImage.
            if len(self._photo_cache) > 4:
                self._photo_cache.pop(next(iter(self._photo_cache)))
        self._canvas.create_image(dx, dy, image=self._photo_cache[key], anchor="nw", tags="frame")

    # -- Artwork -------------------------------------------------------------

    def load_image(self, path: Path | np.ndarray | bytes) -> Any:
        """Load an image into a PIL handle.

        Returns an integer handle, or ``None`` if the file could not be read —
        a single unreadable photo must never stop the slideshow.

        ``bytes`` is the encoded payload from the preload worker; ``ndarray`` is
        used by tests and generated frames.
        """
        try:
            if isinstance(path, (bytes, bytearray)):
                import io

                opened = Image.open(io.BytesIO(path))
                opened.load()
                pil_img = opened.convert("RGB")
            elif isinstance(path, np.ndarray):
                arr = path
                if arr.ndim == 3 and arr.shape[2] == 4:
                    pil_img = Image.fromarray(arr, "RGBA")
                elif arr.ndim == 3 and arr.shape[2] == 3:
                    pil_img = Image.fromarray(arr, "RGB")
                else:
                    raise ValueError(f"Unsupported array shape: {arr.shape}")
            else:
                opened = Image.open(path)
                opened.load()
                pil_img = opened.convert("RGB")
        except Exception:
            logger.debug("Failed to load image: %s", path, exc_info=True)
            return None

        self._texture_counter += 1
        self._images[self._texture_counter] = pil_img
        return self._texture_counter

    def unload_image(self, handle: Any) -> None:
        """Release an image handle and any cached PhotoImages derived from it."""
        if not isinstance(handle, int):
            return
        self._images.pop(handle, None)
        for key in [k for k in self._photo_cache if isinstance(k, tuple) and k[0] == handle]:
            del self._photo_cache[key]

    # -- Display Control -----------------------------------------------------

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        self._bg_color = f"#{r:02x}{g:02x}{b:02x}"

    def clear(self) -> None:
        if self._canvas is None:
            return
        self._canvas.delete("frame")
        self._canvas.configure(bg=self._bg_color)
        # PhotoImage holds a reference into Tk; dropping the cache each frame
        # prevents an unbounded leak on a long-running slideshow.
        self._photo_cache.clear()

    def display_power(self, on: bool) -> None:
        logger.debug("TkBackend display_power(%s) — no-op on desktop", on)

    # -- Internal ------------------------------------------------------------

    @property
    def pending_pause(self) -> bool:
        """Check and reset the pause toggle flag."""
        if self._pending_pause:
            self._pending_pause = False
            return True
        return False

    def _stop(self) -> None:
        self._running = False

    def _toggle_pause(self) -> None:
        self._pending_pause = True
