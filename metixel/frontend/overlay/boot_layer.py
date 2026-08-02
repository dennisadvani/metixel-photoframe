# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Boot Screen Layer — renders the Metixel logo and spinner on startup.

The boot layer is drawn at z=0.0 (closest to camera, on top of everything).
It fades out smoothly once the frontend's presentation engine has loaded
its initial queue, revealing the slideshow underneath with no black-screen gap.

For video: VLC renders on top of everything anyway, so the boot layer
unloads immediately when a video starts — no fade needed.

Architecture:
    BootLayer is a self-contained :class:`OverlayLayer`.  It has zero
    knowledge of the slideshow or presentation engine — it receives a
    ``slideshow_ready`` flag through the overlay shared-state dict from
    the renderer and uses that to decide when to dismiss.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.frontend.overlay.layer import OverlayLayer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "metixel_logo_red_white.png"
_SPINNER_PATH = _ASSETS_DIR / "spinner.png"

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
_MIN_DISPLAY = 3.0       # Minimum time logo is visible (seconds)
_FADE_DURATION = 0.8     # Fade-out animation duration (seconds)
_SPINNER_RPM = 60        # Spinner rotation speed

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
_LOGO_WIDTH_RATIO = 0.60       # Logo takes 60% of screen width
_SPINNER_SIZE_RATIO = 0.05     # Spinner size relative to screen height
_SPINNER_GAP_RATIO = 0.04      # Gap between logo bottom and spinner top


class BootLayer(OverlayLayer):
    """Boot splash rendered via the display backend.

    Shows the Metixel logo (centred, 60% screen width) with a rotating
    spinner underneath.  Fades out when the backend has processed enough
    media to begin the slideshow.
    """

    def __init__(self) -> None:
        super().__init__("boot", OverlayLayer.Z_BOOT)
        self._bg_tex: Any = None
        self._logo_tex: Any = None
        self._spinner_tex: Any = None
        self._tex_loaded: bool = False

        # State machine: "active" → "fading" → "done"
        self._state: str = "active"
        self._alpha: float = 1.0
        self._spinner_angle: float = 0.0
        self._start_time: float = 0.0  # Set on first draw (when screen actually visible)
        self._fade_start: float = 0.0

        # Layout (computed once display dimensions are known)
        self._logo_w: int = 0
        self._logo_h: int = 0
        self._logo_x: int = 0
        self._logo_y: int = 0
        self._spinner_size: int = 0
        self._spinner_x: int = 0
        self._spinner_y: int = 0
        self._layout_done: bool = False

    # -- Properties ----------------------------------------------------------

    @property
    def is_done(self) -> bool:
        """True when the boot screen has finished and textures are freed."""
        return self._state == "done"

    @property
    def is_fading(self) -> bool:
        """True during the fade-out animation."""
        return self._state == "fading"

    # -- OverlayLayer interface ----------------------------------------------

    def update(self, shared_state: dict[str, Any] | None = None) -> None:
        """Advance animations and check whether to start fading out."""
        if self._state == "done":
            return

        now = time.monotonic()

        # ── Animate spinner ────────────────────────────────────────────
        # Convert RPM to degrees per 1/30 s frame tick (assumes ~30 FPS)
        self._spinner_angle = (self._spinner_angle + _SPINNER_RPM * 0.36) % 360.0

        # ── Fade-out animation ─────────────────────────────────────────
        if self._state == "fading":
            elapsed = now - self._fade_start
            if elapsed >= _FADE_DURATION:
                self._finish()
            else:
                # Ease-out: smooth deceleration
                t = elapsed / _FADE_DURATION
                self._alpha = 1.0 - (t * t * (3.0 - 2.0 * t))
            return

        # ── Video override: VLC renders on top, dismiss immediately ───
        video_playing = (shared_state or {}).get("video_playing", False)
        if video_playing and self._state in ("active", "fading"):
            self.dismiss_immediate()
            return

        # ── Check exit conditions ──────────────────────────────────────
        # The boot layer fades out when the frontend's presentation
        # engine has actually loaded its queue (not just when the
        # backend has written playlist.json to disk).  The renderer
        # passes ``slideshow_ready`` via shared_state once the
        # background queue loader thread has finished.
        if self._state == "active" and self._start_time > 0:
            min_elapsed = (now - self._start_time) >= _MIN_DISPLAY
            slideshow_ready = (shared_state or {}).get("slideshow_ready", False)

            if slideshow_ready and min_elapsed:
                logger.info("Boot screen fading out — slideshow is ready")
                self._start_fade()

            # Safety net: if the frontend never loads its queue, dismiss
            # after a timeout rather than showing the boot screen forever.
            if self._start_time > 0 and (now - self._start_time) > 300.0:
                logger.warning(
                    "Boot screen timed out after 300s — dismissing",
                )
                self._start_fade()

    def draw(self, backend: DisplayBackend) -> None:
        """Render logo + spinner.  Lazy-loads textures on first call."""
        if self._state == "done":
            return

        # ── Lazy-init textures (first draw call only) ──────────────────
        if not self._tex_loaded:
            self._load_textures(backend)
            # Start the minimum-display clock NOW — the boot screen is
            # actually visible on screen for the first time.
            self._start_time = time.monotonic()

        # ── Lazy-init layout (needs display dimensions) ────────────────
        if not self._layout_done:
            self._compute_layout(backend)

        self.reset_z()

        # ── Full-screen black background ───────────────────────────────
        if self._bg_tex is not None:
            backend.draw_image(
                self._bg_tex, 0, 0,
                backend.width, backend.height,
                alpha=self._alpha,
                z=self.next_z(),
            )

        # ── Logo ───────────────────────────────────────────────────────
        if self._logo_tex is not None:
            backend.draw_image(
                self._logo_tex,
                self._logo_x, self._logo_y,
                self._logo_w, self._logo_h,
                alpha=self._alpha,
                z=self.next_z(),
            )

        # ── Spinner ────────────────────────────────────────────────────
        if self._spinner_tex is not None:
            backend.draw_image(
                self._spinner_tex,
                self._spinner_x, self._spinner_y,
                self._spinner_size, self._spinner_size,
                alpha=self._alpha,
                rotation=self._spinner_angle,
                z=self.next_z(),
            )

    # -- Internal ------------------------------------------------------------

    def _load_textures(self, backend: DisplayBackend) -> None:
        """Load logo, spinner, and background textures into GPU memory.

        Logo and spinner are loaded via Pillow → numpy array so the
        alpha channel is preserved.  Passing file paths directly to
        ``load_texture()`` forces GL_RGB (no alpha) in the pi3d backend.
        """
        self._backend_ref = backend  # Keep reference for unload

        import numpy as np
        from PIL import Image

        # 1×1 black pixel for full-screen background (avoids draw_rect colour issues)
        black_arr = np.zeros((1, 1, 3), dtype=np.uint8)
        self._bg_tex = backend.load_texture(black_arr)

        if _LOGO_PATH.is_file():
            try:
                logo_img = Image.open(_LOGO_PATH).convert("RGBA")
                logo_arr = np.array(logo_img, dtype=np.uint8)
                self._logo_tex = backend.load_texture(logo_arr)
                logger.debug("Boot logo loaded: %s", _LOGO_PATH.name)
            except Exception:
                logger.exception("Failed to load boot logo texture")
        else:
            logger.warning("Boot logo not found: %s", _LOGO_PATH)

        if _SPINNER_PATH.is_file():
            try:
                spinner_img = Image.open(_SPINNER_PATH).convert("RGBA")
                spinner_arr = np.array(spinner_img, dtype=np.uint8)
                self._spinner_tex = backend.load_texture(spinner_arr)
                logger.debug("Boot spinner loaded: %s", _SPINNER_PATH.name)
            except Exception:
                logger.exception("Failed to load spinner texture")
        else:
            logger.warning("Boot spinner not found: %s", _SPINNER_PATH)

        self._tex_loaded = True

    def _compute_layout(self, backend: DisplayBackend) -> None:
        """Compute logo and spinner positions relative to display size."""
        dw = backend.width
        dh = backend.height

        # Logo: 60% screen width, centred, preserving aspect ratio
        self._logo_w = int(dw * _LOGO_WIDTH_RATIO)
        try:
            from PIL import Image
            with Image.open(_LOGO_PATH) as img:
                orig_w, orig_h = img.size
            if orig_w > 0:
                self._logo_h = int(self._logo_w * orig_h / orig_w)
            else:
                self._logo_h = self._logo_w
        except Exception:
            self._logo_h = int(self._logo_w * 0.35)  # fallback aspect

        self._logo_x = (dw - self._logo_w) // 2
        # Centre vertically, but nudge up slightly so spinner has room
        self._logo_y = (dh - self._logo_h) // 2 - int(dh * 0.04)

        # Spinner: below logo, centred
        self._spinner_size = max(int(dh * _SPINNER_SIZE_RATIO), 24)
        self._spinner_x = (dw - self._spinner_size) // 2
        self._spinner_y = self._logo_y + self._logo_h + int(dh * _SPINNER_GAP_RATIO)

        self._layout_done = True
        logger.debug(
            "Boot layout: logo=%dx%d@(%d,%d) spinner=%d@(%d,%d)",
            self._logo_w, self._logo_h, self._logo_x, self._logo_y,
            self._spinner_size, self._spinner_x, self._spinner_y,
        )

    def _start_fade(self) -> None:
        """Begin the fade-out animation."""
        if self._state != "active":
            return
        self._state = "fading"
        self._fade_start = time.monotonic()
        logger.debug("Boot layer fade-out started")

    def _finish(self) -> None:
        """Complete the boot sequence — unload textures and hide."""
        self._state = "done"
        self.visible = False
        self._alpha = 0.0
        self._unload_textures()
        logger.info("Boot screen dismissed — textures freed")

    def dismiss_immediate(self) -> None:
        """Dismiss instantly (used when a video starts — VLC covers everything)."""
        if self._state == "done":
            return
        self._state = "done"
        self.visible = False
        self._alpha = 0.0
        self._unload_textures()
        logger.info("Boot screen dismissed immediately (video starting)")

    def _unload_textures(self) -> None:
        """Release GPU textures.  Safe to call multiple times."""
        for attr in ('_bg_tex', '_logo_tex', '_spinner_tex'):
            tex = getattr(self, attr, None)
            if tex is not None:
                try:
                    if hasattr(self, '_backend_ref'):
                        self._backend_ref.unload_texture(tex)
                except Exception:
                    logger.debug("Failed to unload %s texture", attr, exc_info=True)
                setattr(self, attr, None)
