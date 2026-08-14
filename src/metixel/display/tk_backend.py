# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tkinter-based Development Display Backend.

Used for local development on machines without pi3d.
Tkinter is bundled with Python on all platforms, making this the
most portable dev backend available.

Renders to a tkinter Canvas with software blitting.
"""

from __future__ import annotations

import contextlib
import logging
import tkinter as tk
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageTk

from metixel.display.backend import DisplayBackend

logger = logging.getLogger(__name__)


class TkBackend(DisplayBackend):
    """Tkinter-based display backend for zero-dependency desktop development.

    Uses a tkinter Canvas for rendering — no external libraries needed
    beyond Pillow (which is already a core dependency).

    tkinter is only needed on desktop dev machines. On headless Pis (no
    tkinter installed) this module can still be imported without error
    because `display/__init__.py`'s detect_backend() only imports
    .tk_backend when actually creating a TkBackend.
    """

    def __init__(self) -> None:
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._running: bool = False
        self._w: int = 1280
        self._h: int = 720
        self._bg_color: str = "black"
        self._fps_limit: int = 30
        self._textures: dict[int, Any] = {}  # id → PIL Image
        # (texture id, alpha) → tk PhotoImage
        self._photo_cache: dict[tuple[int, float], ImageTk.PhotoImage] = {}
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

    # -- Lifecycle -----------------------------------------------------------

    def create(
        self,
        width: int = 1920,
        height: int = 1080,
        fullscreen: bool = True,
        hide_cursor: bool = True,
        fps_limit: int = 30,
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
        self._textures.clear()
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

    # -- 2D Rendering --------------------------------------------------------

    def draw_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        color: tuple[float, float, float, float] = (0, 0, 0, 1),
        z: float = 0.0,
    ) -> None:
        if self._canvas is None:
            return
        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        a = int(color[3] * 255) if len(color) > 3 else 255
        fill_color = f"#{r:02x}{g:02x}{b:02x}"
        # Tkinter doesn't support per-item alpha on Canvas — skip transparent rects
        if a < 20:
            return
        self._canvas.create_rectangle(
            x,
            y,
            x + w,
            y + h,
            fill=fill_color,
            outline="",
            tags="rect",
        )

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
        if self._canvas is None or texture is None:
            return

        if isinstance(texture, int):
            pil_img = self._textures.get(texture)
            if pil_img is None:
                return
        else:
            pil_img = texture

        # Resize
        try:
            resized = pil_img.resize((int(w), int(h)), Image.Resampling.LANCZOS)
        except Exception:
            return

        # Apply alpha by blending with black background
        if alpha < 1.0:
            bg = Image.new("RGBA", resized.size, (0, 0, 0, 0))
            resized = Image.blend(bg, resized.convert("RGBA"), alpha)

        # Convert to PhotoImage (cached by texture id + alpha combo)
        cache_key = (texture if isinstance(texture, int) else id(texture), alpha)
        if cache_key not in self._photo_cache:
            self._photo_cache[cache_key] = ImageTk.PhotoImage(resized)
            # Limit cache size
            if len(self._photo_cache) > 6:
                oldest_key = next(iter(self._photo_cache))
                del self._photo_cache[oldest_key]

        photo = self._photo_cache[cache_key]
        self._canvas.create_image(x, y, image=photo, anchor="nw", tags="img")

    # -- Texture Management --------------------------------------------------

    def load_texture(self, path: Path | np.ndarray, **kwargs: Any) -> Any:
        """Load an image as a PIL Image.

        Returns an integer handle usable with draw_image.
        """
        if isinstance(path, np.ndarray):
            arr = path
            if arr.ndim == 3 and arr.shape[2] == 4:
                pil_img = Image.fromarray(arr, "RGBA")
            elif arr.ndim == 3 and arr.shape[2] == 3:
                pil_img = Image.fromarray(arr, "RGB")
            else:
                raise ValueError(f"Unsupported array shape: {arr.shape}")
        else:
            pil_img = Image.open(path).convert("RGB")

        self._texture_counter += 1
        self._textures[self._texture_counter] = pil_img
        return self._texture_counter

    def unload_texture(self, texture: Any) -> None:
        if isinstance(texture, int):
            self._textures.pop(texture, None)
            # Also clean up cached PhotoImages for this texture
            keys_to_remove = [
                k for k in self._photo_cache if isinstance(k, tuple) and k[0] == texture
            ]
            for k in keys_to_remove:
                del self._photo_cache[k]

    def update_texture(self, texture: Any, data: np.ndarray) -> None:
        """Update a PIL Image texture with new pixel data in-place.

        Creates a new PIL Image from the numpy array and swaps it in
        the texture cache under the same handle.
        """
        if not isinstance(texture, int) or texture not in self._textures:
            super().update_texture(texture, data)
            return

        if data.ndim == 3 and data.shape[2] == 4:
            pil_img = Image.fromarray(data, "RGBA")
        elif data.ndim == 3 and data.shape[2] == 3:
            pil_img = Image.fromarray(data, "RGB")
        else:
            super().update_texture(texture, data)
            return

        self._textures[texture] = pil_img
        # Clear PhotoImage cache for this texture so next draw_image re-renders
        keys_to_remove = [k for k in self._photo_cache if isinstance(k, tuple) and k[0] == texture]
        for k in keys_to_remove:
            del self._photo_cache[k]

    # -- Text Rendering ------------------------------------------------------

    def draw_text(
        self,
        text: str,
        x: float,
        y: float,
        font_size: int = 24,
        color: tuple[float, float, float, float] = (1, 1, 1, 1),
        z: float = 10.0,
    ) -> None:
        if self._canvas is None:
            return
        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        fill_color = f"#{r:02x}{g:02x}{b:02x}"
        self._canvas.create_text(
            x,
            y,
            text=text,
            fill=fill_color,
            font=("TkDefaultFont", font_size),
            anchor="nw",
            tags="text",
        )

    # -- Display Control -----------------------------------------------------

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        self._bg_color = f"#{r:02x}{g:02x}{b:02x}"

    def clear(self) -> None:
        if self._canvas:
            self._canvas.delete("all")
            self._canvas.configure(bg=self._bg_color)
            # Clear PhotoImage cache each frame to prevent memory leak
            # (tkinter PhotoImage must be kept alive by Python ref)
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
