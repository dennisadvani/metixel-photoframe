# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Phase 1 Display Backend: pi3d via Mesa EGL on Trixie (cage/XWayland).

Targets Raspberry Pi 2/3/Zero 2 W running Trixie Lite with the mainline
Mesa + vc4 KMS/DRM driver.

On Trixie, pi3d needs an X11 surface for its EGL context. We provide this
via cage (minimal Wayland compositor) + XWayland — no full Xorg needed.
pi3d uses Mesa EGL, which talks to the vc4 KMS/DRM kernel driver.

This approach adds ~40MB RAM overhead (cage + XWayland) vs direct
framebuffer access, but is forward-compatible and works on all Pi models.
"""
from __future__ import annotations

import gc
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from metixel.display.backend import DisplayBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path to shader files (relative to this module)
# ---------------------------------------------------------------------------
_SHADER_DIR = Path(__file__).resolve().parent / "shaders"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# pi3d texture format constants — resolved at backend init time.
# pi3d 2.50+ moved GL constants to pi3d.constants and dropped GL_RGB565
# from FORMAT_MODES. We use GL_RGB (3 bytes/pixel) for compatibility.
GPU_TEXTURE_FORMAT_GL_RGB: int = 0


class Pi3dBackend(DisplayBackend):
    """Display backend using pi3d with Mesa EGL.

    This backend leverages pi3d to render via Mesa EGL through a Wayland
    compositor (cage) + XWayland surface. pi3d auto-detects the correct
    EGL platform at runtime — Mesa EGL on Trixie, or dispmanx on legacy
    Bullseye systems.

    Memory optimization notes for Pi Zero 2 W (512MB):
    - Uses ``GL_RGB`` (3 bytes/pixel) for GPU textures
    - Calls ``free_after_load=True`` to release CPU-side numpy arrays
    - Keeps at most 3 GPU textures loaded at once
    - Calls ``gc.collect()`` after texture unloads
    """

    def __init__(self) -> None:
        self._display: Any = None
        self._camera: Any = None
        self._shader: Any = None
        self._crossfade_shader: Any = None
        self._crossfade_sprite: Any = None
        self._pi3d: Any = None
        self._running: bool = False
        self._bg_color: tuple[float, float, float, float] = (0, 0, 0, 1)
        self._fps_limit: int = 30
        self._texture_count: int = 0
        self._max_textures: int = 8

        # Second orthographic camera for the overlay layer (widgets, pop-ups).
        # Identical projection to the main camera — separation is architectural.
        self._overlay_camera: Any = None

        # Manual frame timing (bypasses pi3d's inaccurate clock.tick)
        self._frame_period: float = 1.0 / 30.0
        self._last_frame_time: float = 0.0

        # -- Sprite pools --
        # Rect sprites: keyed by (int(w), int(h), r, g, b) — pooled because
        # matte bars are drawn at the same size/color every frame.
        self._rect_sprites: dict[tuple[int, int, int, int, int], Any] = {}
        # Pre-allocated 1x1 white texture for colored rects
        self._white_tex: Any = None
        self._max_rect_sprites: int = 6

        # Text cache — avoids recreating FixedString every frame
        self._text_cache: dict[tuple, Any] = {}

    # -- Properties ----------------------------------------------------------

    @property
    def width(self) -> int:
        if self._display:
            return self._display.width
        return 0

    @property
    def height(self) -> int:
        if self._display:
            return self._display.height
        return 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def overlay_camera(self) -> Any:
        """Secondary orthographic camera for widgets and pop-up messages.

        Identical projection to the main camera (1 unit = 1 pixel, origin
        at screen center).  The separation is architectural — the overlay
        manager uses this camera so overlay rendering is fully independent
        of the slideshow layer.
        """
        return self._overlay_camera

    # -- Lifecycle -----------------------------------------------------------

    def create(
        self,
        width: int = 0,
        height: int = 0,
        fullscreen: bool = True,
        hide_cursor: bool = True,
        fps_limit: int = 30,
        **kwargs: Any,
    ) -> None:
        """Initialize pi3d and create the rendering surface.

        On Trixie, pi3d creates an EGL context via Mesa, surfaced through
        XWayland (provided by cage). On legacy Bullseye, it uses dispmanx.

        If *width* or *height* is 0 (the default), pi3d auto-detects the
        native display resolution via DRM/KMS.
        """
        import pi3d

        self._pi3d = pi3d
        self._fps_limit = fps_limit
        self._frame_period = 1.0 / max(fps_limit, 1)
        self._last_frame_time = 0.0

        # Resolve GL_RGB constant — pi3d 2.50+ moved it to pi3d.constants
        global GPU_TEXTURE_FORMAT_GL_RGB
        try:
            from pi3d.constants import GL_RGB  # pi3d >= 2.50
            GPU_TEXTURE_FORMAT_GL_RGB = GL_RGB
        except ImportError:
            try:
                GPU_TEXTURE_FORMAT_GL_RGB = pi3d.GL_RGB  # pi3d < 2.50
            except AttributeError:
                GPU_TEXTURE_FORMAT_GL_RGB = 0x1907  # raw OpenGL GL_RGB

        # Resolve DISPLAY_CONFIG_HIDE_CURSOR — also moved in newer pi3d
        try:
            display_config_hide_cursor = pi3d.DISPLAY_CONFIG_HIDE_CURSOR
        except AttributeError:
            display_config_hide_cursor = 2  # fallback value

        display_config = 0
        if hide_cursor:
            display_config = display_config_hide_cursor

        # Hide X11 cursor immediately — pi3d's DISPLAY_CONFIG_HIDE_CURSOR
        # only takes effect after the display surface is fully initialised,
        # leaving a visible cursor for 1-2 seconds at startup.
        if hide_cursor:
            self._hide_x11_cursor()

        # Auto-detect display resolution if not explicitly specified.
        # pi3d (via SDL2 or DRM) will query the native mode when w=0 or h=0.
        # frames_per_second=0: unlimited — we handle frame timing ourselves.
        self._display = pi3d.Display.create(
            x=0,
            y=0,
            w=width,
            h=height,
            background=self._bg_color,
            display_config=display_config,
            frames_per_second=0,
        )

        # When no monitor is connected, pi3d/DRM may report 1×1 (or 0×0).
        # This causes pi3d Textures to fail with "height and width must be > 0"
        # during resize.  Fall back to a safe 1080p default and warn loudly.
        DISPLAY_FALLBACK_W = 1920
        DISPLAY_FALLBACK_H = 1080
        if self._display.width <= 1 or self._display.height <= 1:
            logger.warning(
                "Detected display resolution %dx%d — likely no monitor connected. "
                "Falling back to %dx%d.",
                self._display.width, self._display.height,
                DISPLAY_FALLBACK_W, DISPLAY_FALLBACK_H,
            )
            # Destroy the bogus 1×1 display and recreate at 1080p.
            try:
                self._display.destroy()
            except Exception:
                pass
            self._display = pi3d.Display.create(
                x=0,
                y=0,
                w=DISPLAY_FALLBACK_W,
                h=DISPLAY_FALLBACK_H,
                background=self._bg_color,
                display_config=display_config,
                frames_per_second=0,
            )

        # 2D orthographic camera — 1 unit = 1 pixel
        self._camera = pi3d.Camera(is_3d=False)
        # Second orthographic camera for the overlay layer (widgets, pop-ups).
        # Identical projection — separation is purely architectural so the
        # overlay manager can own its camera independently of the slideshow.
        self._overlay_camera = pi3d.Camera(is_3d=False)
        # Disable fog on both cameras — pi3d's default fog washes out colours
        # on 2D overlay elements (makes reds look pink, etc.)
        self._camera.fog = False
        self._overlay_camera.fog = False
        # Simple flat shader for 2D texture rendering
        self._shader = pi3d.Shader("uv_flat")
        # Crossfade shader — blends two textures in a single GPU pass.
        # Supports images with alpha channels (no dark artefacts).
        self._crossfade_shader = pi3d.Shader(
            str(_SHADER_DIR / "crossfade"),
        )

        # Pre-create white 1×1 texture for colored rects (reused every frame)
        white_arr = np.ones((1, 1, 3), dtype=np.uint8) * 255
        self._white_tex = pi3d.Texture(
            white_arr, free_after_load=True, i_format=GPU_TEXTURE_FORMAT_GL_RGB,
        )

        self._running = True
        logger.info(
            "Pi3dBackend created: %dx%d @ %d FPS | "
            "config requested %dx%d | "
            "GPU mem: %s | DRM driver: %s | pi3d version: %s",
            self._display.width, self._display.height,
            fps_limit,
            width, height,
            self._get_gpu_mem(),
            self._get_drm_driver(),
            getattr(self._pi3d, "__version__", "unknown"),
        )

    def destroy(self) -> None:
        """Release all pi3d/GPU resources and sprite pools."""
        self._running = False
        self._rect_sprites.clear()
        self._text_cache.clear()
        self._white_tex = None
        if self._display:
            try:
                self._display.destroy()
            except Exception:
                pass
            self._display = None
        self._camera = None
        self._overlay_camera = None
        self._shader = None
        gc.collect()

    @staticmethod
    def _hide_x11_cursor() -> None:
        """Hide the X11 cursor before pi3d initialises the display surface.

        On Trixie with cage + XWayland, the default X11 cursor is visible
        for 1–2 seconds between XWayland startup and pi3d's first frame.
        We hide it immediately by creating a transparent 1×1 cursor and
        applying it via xsetroot (if available).
        """
        try:
            import tempfile
            # Minimal XBM cursor: 1×1 fully transparent
            xbm = (
                "#define empty_width 1\n"
                "#define empty_height 1\n"
                "#define empty_x_hot 0\n"
                "#define empty_y_hot 0\n"
                "static unsigned char empty_bits[] = {\n"
                "   0x00\n"
                "};\n"
            )
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".xbm", delete=False, prefix="empty_cursor_",
            )
            try:
                tmp.write(xbm)
                tmp.close()
                subprocess.run(
                    ["xsetroot", "-cursor", tmp.name, tmp.name],
                    timeout=2, capture_output=True,
                )
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        except Exception:
            pass  # xsetroot not available — pi3d's hide_cursor will handle it
        logger.info("Pi3dBackend destroyed")

    def loop_running(self) -> bool:
        """Check if the display loop should continue and enforce frame timing.

        Bypasses pi3d's internal ``clock.tick()`` (which can misbehave on
        Raspberry Pi due to coarse scheduler granularity or display vsync
        at non-standard refresh rates).  Uses a simple spin-wait with
        ``time.monotonic()`` for accurate frame pacing.

        Returns:
            True while the display is active and no quit event has occurred.
        """
        if self._display is None:
            return False

        # Process events (quit, keyboard, etc.) — still needed
        if not self._display.loop_running():
            return False

        # Manual frame pacing: sleep until next frame deadline
        now = time.monotonic()
        elapsed = now - self._last_frame_time
        remaining = self._frame_period - elapsed
        if remaining > 0.001:
            time.sleep(remaining)
        self._last_frame_time = time.monotonic()
        return True

    def swap_buffers(self) -> None:
        """No explicit swap needed — pi3d swaps at end of each draw call."""
        pass

    def clear_depth(self) -> None:
        """Clear the depth buffer, allowing subsequent draws to appear on top.

        The slideshow's rendering may write depth values that would occlude
        overlay content (widgets, pop-up messages).  Call this after the
        slideshow renders and before drawing overlay elements.
        """
        if self._pi3d is not None:
            try:
                self._pi3d.opengles.glClear(0x00000100)  # GL_DEPTH_BUFFER_BIT
            except Exception:
                pass

    def set_depth_test(self, enabled: bool) -> None:
        """Enable or disable OpenGL depth testing.

        Disabling depth testing allows the overlay layer to use simple
        draw-order (painter's algorithm) for layering, avoiding z-fighting
        with whatever the slideshow left in the depth buffer.

        Args:
            enabled: ``True`` to enable, ``False`` to disable.
        """
        if self._pi3d is not None:
            try:
                if enabled:
                    self._pi3d.opengles.glEnable(0x0B71)  # GL_DEPTH_TEST
                else:
                    self._pi3d.opengles.glDisable(0x0B71)  # GL_DEPTH_TEST
            except Exception:
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
        """Draw a filled rectangle using a **pooled** sprite.

        Coordinates use top-left origin (matching the layout engine).
        Converted to pi3d's center-origin coordinate system internally.

        Sprites are cached by (w, h, r, g, b). The same matte-bar rect is
        reused every frame — zero per-frame GPU allocations.
        """
        if self._display is None or self._camera is None or self._pi3d is None:
            return

        r, g, b = int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)
        cache_key = (int(w), int(h), r, g, b)

        if cache_key not in self._rect_sprites:
            # Evict oldest if pool full
            if len(self._rect_sprites) >= self._max_rect_sprites:
                oldest = next(iter(self._rect_sprites))
                del self._rect_sprites[oldest]

            sprite = self._pi3d.Sprite(
                w=w, h=h, x=0, y=0, z=z, camera=self._camera,
            )
            sprite.set_draw_details(self._shader, [self._white_tex])
            # Tint the white texture via material color
            sprite.set_material((color[0], color[1], color[2]))
            # Disable fog — pi3d's default fog washes out overlay colours
            sprite.set_fog((0, 0, 0, 0), 100000)
            self._rect_sprites[cache_key] = sprite

        sprite = self._rect_sprites[cache_key]
        # Convert top-left origin to pi3d center-origin coordinate system.
        # pi3d's default 2D camera: (0,0) = center, +X = right, +Y = up.
        px = (x + w / 2) - self._display.width / 2
        py = self._display.height / 2 - (y + h / 2)
        sprite.position(px, py, z)
        sprite.set_alpha(color[3])
        sprite.set_fog((0, 0, 0, 0), 100000)  # disable fog every frame
        sprite.draw()

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
        """Draw a textured sprite — fresh sprite every frame.

        Matching picframe's approach: create a Sprite, set shader + textures,
        position, draw.  No sprite pool — avoids stale-GPU-state bugs when
        texture IDs are reused across slides.
        """
        if self._display is None or self._camera is None or self._pi3d is None:
            return
        if texture is None:
            return

        # Convert top-left origin to pi3d center-origin coordinate system.
        # pi3d's default 2D camera: (0,0) = center, +X = right, +Y = up.
        px = (x + w / 2) - self._display.width / 2
        py = self._display.height / 2 - (y + h / 2)

        sprite = self._pi3d.Sprite(
            w=w, h=h, x=px, y=py, z=z, camera=self._camera,
        )
        sprite.set_shader(self._shader)
        sprite.set_textures([texture])
        sprite.set_alpha(alpha)
        sprite.set_fog((0, 0, 0, 0), 100000)

        if rotation != 0.0:
            sprite.rotateToZ(rotation)

        # Texture coordinate adjustment (Ken Burns / pan-zoom)
        if uv_offset != (0.0, 0.0) or uv_scale != (1.0, 1.0):
            for buf in sprite.buf:
                buf.unib[6] = uv_scale[0]
                buf.unib[7] = uv_scale[1]
                buf.unib[9] = uv_offset[0]
                buf.unib[10] = uv_offset[1]

        sprite.draw()

    def draw_crossfade(
        self,
        tex_current: Any,
        tex_next: Any,
        blend: float,
        current_rect: tuple[float, float, float, float] | None = None,
        next_rect: tuple[float, float, float, float] | None = None,
        slide_offset_current: float = 0.0,
        slide_offset_next: float = 0.0,
    ) -> None:
        """Draw a crossfade between two textures in a **single GPU pass**.

        Uses a custom blend shader that mixes two textures per-pixel.
        Each texture can be independently scaled/positioned via UV uniforms,
        so images maintain their aspect ratio within the full-screen quad.

        When *slide_offset_current* or *slide_offset_next* are non-zero,
        the texture UVs are shifted horizontally by the given pixel amount.
        This enables smooth slide transitions with zero overdraw — both
        images are rendered in a single shader pass.

        Args:
            tex_current: Texture for the outgoing image.
            tex_next: Texture for the incoming image.
            blend: 0.0 = fully current, 1.0 = fully next.
            current_rect: (x, y, w, h) of the current image on screen.
            next_rect: (x, y, w, h) of the next image on screen.
            slide_offset_current: Horizontal pixel shift for current texture.
            slide_offset_next: Horizontal pixel shift for next texture.
        """
        if self._display is None or self._pi3d is None:
            return
        if self._crossfade_shader is None:
            return
        if tex_current is None or tex_next is None:
            return

        sw = float(self._display.width)
        sh = float(self._display.height)

        # Create or reuse the crossfade sprite (full-screen quad).
        if self._crossfade_sprite is None:
            self._crossfade_sprite = self._pi3d.Sprite(
                w=sw, h=sh, x=0, y=0, z=0.0, camera=self._camera,
            )
            self._crossfade_sprite.set_draw_details(
                self._crossfade_shader,
                [tex_current, tex_next],
            )

        # Update textures (may change each transition)
        self._crossfade_sprite.set_textures([tex_current, tex_next])

        # Compute UV scale/offset for each texture to maintain aspect ratio.
        # The shader maps screen UV → texture UV using:
        #   texcoordout = texcoord * scale - offset
        # UVs outside [0,1] are clamped to transparent in the fragment shader.
        u = self._crossfade_sprite.unif

        def _set_uv(base_idx: int, rect: tuple[float, float, float, float] | None,
                     slide_off: float = 0.0) -> None:
            if rect is None:
                u[base_idx] = 1.0       # scale X
                u[base_idx + 1] = 1.0   # scale Y
                u[base_idx + 6] = 0.0   # offset X
                u[base_idx + 7] = 0.0   # offset Y
            else:
                rx, ry, rw, rh = rect
                u[base_idx] = sw / rw if rw > 0 else 1.0       # scale X
                u[base_idx + 1] = sh / rh if rh > 0 else 1.0   # scale Y
                # Slide offset in pixels → UV offset: dx / rw
                u[base_idx + 6] = (rx - slide_off) / rw if rw > 0 else 0.0   # offset X
                u[base_idx + 7] = ry / rh if rh > 0 else 0.0   # offset Y

        # Front texture UV: unif[42,43] = scale, unif[48,49] = offset
        _set_uv(42, current_rect, slide_offset_current)
        # Back  texture UV: unif[45,46] = scale, unif[51,52] = offset
        _set_uv(45, next_rect, slide_offset_next)

        # Blend factor: unif[44] = unif[14][2] in GLSL
        u[44] = float(blend)

        self._crossfade_sprite.draw()

    # -- Texture Management --------------------------------------------------

    def load_texture(self, path: Path | np.ndarray, **kwargs: Any) -> Any:
        """Load an image into a GPU texture via pi3d.

        Uses ``blend=True, m_repeat=True`` matching picframe's proven
        approach.  No ``free_after_load`` for numpy arrays to avoid
        async-upload race on slower Pi GPUs.  No ``i_format`` override
        — let pi3d auto-detect from the numpy array shape.
        """
        if self._pi3d is None:
            raise RuntimeError("Display not initialized — call create() first")

        i_format = GPU_TEXTURE_FORMAT_GL_RGB if GPU_TEXTURE_FORMAT_GL_RGB else None

        if isinstance(path, np.ndarray):
            # Let pi3d detect format from array shape — forcing i_format
            # can conflict with pi3d 2.55+ internal format detection.
            texture = self._pi3d.Texture(
                path, blend=True, m_repeat=True,
            )
        else:
            texture = self._pi3d.Texture(
                str(path), blend=True, m_repeat=True, free_after_load=True,
                i_format=i_format, **kwargs
            )

        logger.debug(
            "load_texture: tex=%s shape=%s",
            id(texture),
            path.shape if isinstance(path, np.ndarray) else str(path),
        )

        self._texture_count += 1

        if self._texture_count > self._max_textures:
            logger.warning(
                "GPU texture count (%d) exceeds recommended max (%d) — "
                "risk of memory exhaustion on low-RAM Pi",
                self._texture_count,
                self._max_textures,
            )

        return texture

    def unload_texture(self, texture: Any) -> None:
        """Release a GPU texture.  pi3d's Texture.__del__ handles the
        OpenGL cleanup when Python GC collects the object."""
        if texture is not None:
            del texture
        self._texture_count = max(0, self._texture_count - 1)
        gc.collect()

    def update_texture(self, texture: Any, data: np.ndarray) -> None:
        """Update an existing pi3d Texture with new pixel data in-place.

        Uses pi3d's ``Texture.update_ndarray()`` to upload new frames
        without destroying/recreating the GPU texture object. Critical
        for smooth video playback — avoids per-frame GPU allocation.
        """
        if texture is not None and hasattr(texture, "update_ndarray"):
            texture.update_ndarray(data)
        else:
            # Fallback for textures that don't support in-place update
            super().update_texture(texture, data)

    # -- Font resolution -----------------------------------------------------

    # Paths to try when locating a TrueType font for text rendering.
    # Ordered by preference — DejaVu Sans is the default on Raspberry Pi OS.
    _FONT_CANDIDATES: list[str] = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]

    @classmethod
    def _find_font_path(cls) -> str:
        """Return the first available TrueType font path on the system."""
        for candidate in cls._FONT_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
        # Fallback: let pi3d try its own default
        return ""

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
        """Render text using pi3d's FixedString.

        ``FixedString`` takes a font **file path** (passed straight to
        ``PIL.ImageFont.truetype()``) and renders the text to a GPU
        texture sprite once — the same text at the same size is only
        recreated when the content changes.

        Coordinates use top-left origin (matching the widget API).
        The sprite's center is offset so the text's top-left lands at
        (x, y).
        """
        if self._pi3d is None or self._camera is None:
            return

        font_path = self._find_font_path()
        if not font_path:
            return

        # pi3d color is 0-255 RGBA
        pi3d_color = (
            int(color[0] * 255),
            int(color[1] * 255),
            int(color[2] * 255),
            int(color[3] * 255),
        )

        # Cache key: only recreate the FixedString when text/font/size
        # actually changes — avoids one GPU texture allocation per frame.
        cache_key = (text, font_path, font_size, pi3d_color)
        fixed_str = self._text_cache.get(cache_key)
        if fixed_str is None:
            fixed_str = self._pi3d.FixedString(
                font_path,
                text,
                font_size=font_size,
                color=pi3d_color,
                camera=self._camera,
                shader=self._shader,
            )
            self._text_cache[cache_key] = fixed_str
            # Disable fog — pi3d's default fog washes out colours
            fixed_str.sprite.set_fog((0, 0, 0, 0), 100000)
            # Prune cache if it grows too large (unlikely for a clock)
            if len(self._text_cache) > 16:
                oldest = next(iter(self._text_cache))
                del self._text_cache[oldest]

        # pi3d sprite position is the sprite's CENTER.
        # To place top-left at (x, y), offset by half the text dimensions.
        # pi3d's 2D camera: +Y = up, so flip Y from top-left convention.
        tw = float(getattr(fixed_str.sprite, "width", font_size * len(text) * 0.55))
        th = float(getattr(fixed_str.sprite, "height", font_size))
        px = (x + tw / 2.0) - self._display.width / 2.0
        py = self._display.height / 2.0 - (y + th / 2.0)
        fixed_str.sprite.position(px, py, z)
        fixed_str.sprite.draw()

    # -- Display Control -----------------------------------------------------

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        """Store the background color.

        Note: pi3d's background is set at ``Display.create()`` time and
        cannot be changed at runtime. This method stores the value for
        reference only.
        """
        self._bg_color = color

    def clear(self) -> None:
        """No-op — pi3d auto-clears the framebuffer each frame.

        The background color set at ``Display.create()`` is used.
        All sprites and matte bars render on top of the cleared background.
        """

    # -- Power Management ----------------------------------------------------

    # Default Wayland output name for wlr-randr (configurable via env)
    _WLR_OUTPUT = os.environ.get("METIXEL_WLR_OUTPUT", "HDMI-A-1")

    def display_power(self, on: bool) -> None:
        """Control HDMI display power.

        Tries, in order:
        1. ``wlr-randr`` — works with cage/wlroots (no root needed)
        2. DRM DPMS sysfs — KMS/Direct Render Manager
        3. ``vcgencmd`` — legacy Broadcom firmware (Bullseye)
        """
        state = "on" if on else "off"
        on_off_flag = on  # True → --on, False → --off

        # 1. wlr-randr (Wayland/wlroots — primary for cage on Trixie)
        if self._wlr_randr(on_off_flag):
            logger.info("Display power (wlr-randr): %s", state.upper())
            return

        # 2. DRM DPMS sysfs (KMS fallback)
        if self._drm_dpms(state):
            logger.info("Display power (DRM DPMS): %s", state.upper())
            return

        # 3. vcgencmd (legacy Broadcom firmware / Bullseye)
        if not self._is_pi():
            logger.warning("display_power: not on a Raspberry Pi — no-op")
            return

        cmd = ["vcgencmd", "display_power", "1" if on else "0"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
            logger.info("Display power (vcgencmd): %s", state.upper())
        except subprocess.CalledProcessError as e:
            logger.error("Failed to set display power: %s", e)
        except FileNotFoundError:
            logger.warning("vcgencmd not found — display power control unavailable")

    @staticmethod
    def _wlr_randr(on: bool) -> bool:
        """Toggle display via wlr-randr (wlroots/Wayland). Returns True on success.

        Passes a minimal environment to the subprocess to avoid any interference
        from the parent process's display-related environment variables.
        """
        try:
            wlr_bin = "/usr/bin/wlr-randr"
            if not os.path.exists(wlr_bin):
                logger.debug("wlr-randr not installed at %s", wlr_bin)
                return False

            # Use a minimal environment — only pass what wlr-randr needs.
            # Inheriting the full os.environ can cause conflicts (e.g. DISPLAY=:0
            # from XWayland may confuse some Wayland clients).
            env = {
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/home/pi"),
            }

            cmd = [
                wlr_bin,
                "--output", Pi3dBackend._WLR_OUTPUT,
                "--on" if on else "--off",
            ]
            result = subprocess.run(
                cmd, capture_output=True, timeout=5, env=env,
            )
            if result.returncode != 0:
                logger.warning(
                    "wlr-randr exited %d: %s",
                    result.returncode,
                    result.stderr.decode(errors="replace").strip(),
                )
            return result.returncode == 0
        except FileNotFoundError:
            logger.debug("wlr-randr not installed — cannot control display via Wayland")
            return False
        except Exception:
            logger.warning("wlr-randr failed", exc_info=True)
            return False

    @staticmethod
    def _drm_dpms(state: str) -> bool:
        """Set display DPMS state via KMS sysfs. Returns True on success.

        Tries writing to ``/sys/class/drm/card*-*/dpms``.  On modern kernels
        these nodes may be read-only; falls back to ``sudo tee`` to
        ``.../status`` (picframe mode 3).
        """
        import glob

        # Try dpms node first (may be read-only on newer kernels)
        try:
            for card in glob.glob("/sys/class/drm/card*-*"):
                dpms_path = os.path.join(card, "dpms")
                if os.path.exists(dpms_path):
                    with open(dpms_path, "w") as f:
                        f.write(state)
                    return True
        except OSError:
            pass

        # Fallback: write to .../status via sudo tee (picframe mode 3)
        try:
            for card in glob.glob("/sys/class/drm/card*-*"):
                status_path = os.path.join(card, "status")
                if os.path.exists(status_path):
                    on_off = "on" if state == "on" else "off"
                    result = subprocess.run(
                        ["sudo", "tee", status_path],
                        input=on_off, capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        return True
        except Exception:
            pass

        return False

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _is_pi() -> bool:
        """Check if running on a Raspberry Pi."""
        try:
            with open("/proc/device-tree/model") as f:
                return "Raspberry Pi" in f.read()
        except (OSError, FileNotFoundError):
            return os.path.exists("/opt/vc/lib/libEGL.so")

    @staticmethod
    def _get_gpu_mem() -> str:
        """Get GPU memory allocation from vcgencmd."""
        try:
            result = subprocess.run(
                ["vcgencmd", "get_mem", "gpu"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return "unknown"

    @staticmethod
    def _get_drm_driver() -> str:
        """Detect the active DRM/KMS driver (vc4, vc4-fkms-v3d, etc.)."""
        try:
            for entry in os.listdir("/sys/class/drm"):
                if entry.startswith("card"):
                    card_path = f"/sys/class/drm/{entry}/device/driver"
                    if os.path.islink(card_path):
                        return os.path.basename(os.readlink(card_path))
            return "none"
        except Exception:
            return "unknown"
