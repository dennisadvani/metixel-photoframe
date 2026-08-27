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

import json
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
_MIN_DISPLAY = 3.0  # Minimum time logo is visible (seconds)
_FADE_DURATION = 0.8  # Fade-out animation duration (seconds)
_SPINNER_RPM = 60  # Spinner rotation speed

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
_LOGO_WIDTH_RATIO = 0.60  # Logo takes 60% of screen width
_SPINNER_SIZE_RATIO = 0.05  # Spinner size relative to screen height
_SPINNER_GAP_RATIO = 0.04  # Gap between logo bottom and spinner top
_PROGRESS_GAP_RATIO = 0.02  # Gap between spinner bottom and progress bar
_PROGRESS_HEIGHT_RATIO = 0.008  # Progress bar height relative to screen height
_PROGRESS_WIDTH_RATIO = 0.30  # Progress bar is 30% screen width (half of logo)

# Processing status file written by the optimisation queue
_PROCESSING_STATUS_PATH = Path("/run/metixel/processing_status.json")


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

        # Progress bar (below spinner, shows optimisation progress)
        self._progress_pct: float = 0.0
        self._progress_w: int = 0
        self._progress_h: int = 0
        self._progress_x: int = 0
        self._progress_y: int = 0
        # 1×1 pixel textures for the progress bar (avoids draw_rect colour bugs)
        self._progress_bg_tex: Any = None  # dark gray track
        self._progress_fill_tex: Any = None  # red fill
        # Suppress the progress bar once the slideshow queue has enough
        # items to begin — avoids the bar cycling to 100% multiple times
        # on cached restarts where items process instantly.
        self._progress_hidden: bool = False

        # Reset mode — skips the 3s minimum display and progress bar.
        # Set by reactivate() when the pipeline resets (config change)
        # so the boot screen fades out as soon as new items are ready.
        self._reset_mode: bool = False

    # -- Properties ----------------------------------------------------------

    @property
    def is_done(self) -> bool:
        """True when the boot screen has finished and textures are freed."""
        return self._state == "done"

    @property
    def is_fading(self) -> bool:
        """True during the fade-out animation."""
        return self._state == "fading"

    def reactivate(self) -> None:
        """Re-show the boot screen after a pipeline reset.

        Resets all state so the logo + spinner + progress bar render
        again.  Called when the slideshow queue is cleared by a config
        change (watch folders, video toggle, etc.).
        """
        if self._state == "active":
            return  # Already showing
        self._state = "active"
        self.visible = True
        self._alpha = 1.0
        self._spinner_angle = 0.0
        self._start_time = 0.0  # Will be set on first draw
        self._fade_start = 0.0
        self._progress_pct = 0.0
        self._progress_hidden = False
        # Textures were freed by _finish() — force a reload on next draw
        self._tex_loaded = False
        self._bg_tex = None
        self._logo_tex = None
        self._spinner_tex = None
        self._progress_bg_tex = None
        self._progress_fill_tex = None
        self._reset_mode = True
        logger.info("Boot layer reactivated — pipeline is rebuilding")

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
            min_elapsed = (
                self._reset_mode  # Skip 3s wait during pipeline resets
                or (now - self._start_time) >= _MIN_DISPLAY
            )
            slideshow_ready = (shared_state or {}).get("slideshow_ready", False)

            # ── Update progress bar from backend status file ──────────
            # Skip during pipeline resets — progress bar is for cold boot
            # where the user expects a multi-minute wait.
            if not self._reset_mode:
                self._progress_pct = self._read_progress_pct()

            # Hide the progress bar once enough items are queued for a
            # meaningful slideshow.  On cached restarts the optimisation
            # queue processes items instantly, making the bar jump to
            # 100% repeatedly — confusing and unnecessary.
            # Always hidden during pipeline resets (reset_mode).
            queue_size = (shared_state or {}).get("queue_size", 0)
            if queue_size >= 6 or self._reset_mode:
                self._progress_hidden = True

            if slideshow_ready and min_elapsed:
                logger.info("Boot screen fading out — slideshow is ready")
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
                self._bg_tex,
                0,
                0,
                backend.width,
                backend.height,
                alpha=self._alpha,
                z=self.next_z(),
            )

        # ── Logo ───────────────────────────────────────────────────────
        if self._logo_tex is not None:
            backend.draw_image(
                self._logo_tex,
                self._logo_x,
                self._logo_y,
                self._logo_w,
                self._logo_h,
                alpha=self._alpha,
                z=self.next_z(),
            )

        # ── Spinner ────────────────────────────────────────────────────
        if self._spinner_tex is not None:
            backend.draw_image(
                self._spinner_tex,
                self._spinner_x,
                self._spinner_y,
                self._spinner_size,
                self._spinner_size,
                alpha=self._alpha,
                rotation=self._spinner_angle,
                z=self.next_z(),
            )

        # ── Progress bar ───────────────────────────────────────────────
        # Uses draw_image with 1×1 pixel textures instead of draw_rect
        # to avoid colour-space issues (draw_rect may render wrong
        # colours on some pi3d/OpenGL configurations).
        # The bar only draws when we have a meaningful percentage
        # and haven't been suppressed (queue already has enough items).
        if self._progress_pct > 0.0 and self._progress_pct <= 100.0 and not self._progress_hidden:
            # Lazy-init progress bar textures
            if self._progress_bg_tex is None:
                self._progress_bg_tex = self._make_pixel_tex(backend, (0.2, 0.2, 0.2))
            if self._progress_fill_tex is None:
                self._progress_fill_tex = self._make_pixel_tex(backend, (0.85, 0.15, 0.15))

            pct = self._progress_pct / 100.0
            # Track (dark gray, full width)
            if self._progress_bg_tex is not None:
                backend.draw_image(
                    self._progress_bg_tex,
                    self._progress_x,
                    self._progress_y,
                    self._progress_w,
                    self._progress_h,
                    alpha=self._alpha,
                    z=self.next_z(),
                )
            # Fill (red, clipped to percentage)
            if self._progress_fill_tex is not None and pct > 0.0:
                fill_w = max(1, int(self._progress_w * pct))
                backend.draw_image(
                    self._progress_fill_tex,
                    self._progress_x,
                    self._progress_y,
                    fill_w,
                    self._progress_h,
                    alpha=self._alpha,
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

        # Progress bar: half the logo width, centred, in the gap
        # between the spinner and the bottom of the screen.
        self._progress_h = max(int(dh * _PROGRESS_HEIGHT_RATIO), 4)
        self._progress_w = int(dw * _PROGRESS_WIDTH_RATIO)
        self._progress_x = (dw - self._progress_w) // 2
        # Place in the middle of the remaining space below the spinner
        _space_below = dh - (self._spinner_y + self._spinner_size)
        self._progress_y = (
            self._spinner_y + self._spinner_size + (_space_below - self._progress_h) // 2
        )

        self._layout_done = True
        logger.debug(
            "Boot layout: logo=%dx%d@(%d,%d) spinner=%d@(%d,%d) progress=%dx%d@(%d,%d)",
            self._logo_w,
            self._logo_h,
            self._logo_x,
            self._logo_y,
            self._spinner_size,
            self._spinner_x,
            self._spinner_y,
            self._progress_w,
            self._progress_h,
            self._progress_x,
            self._progress_y,
        )

    def _read_progress_pct(self) -> float:
        """Read optimisation progress from the per-phase status file.

        Returns percentage (0–100) from the ``optimising_images`` phase
        or retains the last-known value when that phase is absent.
        """
        try:
            if not _PROCESSING_STATUS_PATH.exists():
                return self._progress_pct
            with open(_PROCESSING_STATUS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            phases = data.get("phases", {})
            opt = phases.get("optimising_images", {})
            total = opt.get("total", 0)
            processed = opt.get("processed", 0)
            if total > 0 and processed > 0:
                return float(min(100.0, (processed / total) * 100.0))
            return self._progress_pct
        except (json.JSONDecodeError, OSError, ValueError):
            return self._progress_pct

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
        for attr in (
            "_bg_tex",
            "_logo_tex",
            "_spinner_tex",
            "_progress_bg_tex",
            "_progress_fill_tex",
        ):
            tex = getattr(self, attr, None)
            if tex is not None:
                try:
                    if hasattr(self, "_backend_ref"):
                        self._backend_ref.unload_texture(tex)
                except Exception:
                    logger.debug("Failed to unload %s texture", attr, exc_info=True)
                setattr(self, attr, None)

    @staticmethod
    def _make_pixel_tex(backend: DisplayBackend, color: tuple[float, float, float]) -> Any:
        """Create a 1×1 pixel texture of the given RGB color.

        Used for the progress bar since ``draw_rect`` can produce
        incorrect colours on some pi3d / OpenGL configurations.
        """
        import numpy as np

        arr = np.zeros((1, 1, 3), dtype=np.uint8)
        arr[0, 0] = [int(c * 255) for c in color]
        return backend.load_texture(arr)
