# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Development Display Backend: pygame-based software renderer.

Used for local development and testing on desktop/laptop machines without
Raspberry Pi hardware. Renders to a pygame window with software blitting.

This backend is for DEVELOPMENT ONLY and should never be deployed to a
production photo frame.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from metixel.display.backend import DisplayBackend

logger = logging.getLogger(__name__)


class DevBackend(DisplayBackend):
    """Pygame-based display backend for desktop development.

    Provides a reasonable approximation of the rendering pipeline for testing
    the presentation engine, widget system, and transitions on a dev machine.
    """

    def __init__(self) -> None:
        self._screen: Any = None
        self._pygame: Any = None
        self._clock: Any = None
        self._running: bool = False
        self._w: int = 1920
        self._h: int = 1080
        self._bg_color: tuple[int, int, int] = (0, 0, 0)
        self._fps_limit: int = 30
        self._textures: dict[int, Any] = {}  # id → pygame Surface
        self._texture_counter: int = 0
        self._font: Any = None
        self._font_cache: dict[int, Any] = {}  # font_size → pygame Font

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
        import pygame

        self._pygame = pygame
        self._w = width
        self._h = height
        self._fps_limit = fps_limit

        pygame.init()

        flags = 0
        if fullscreen:
            flags |= pygame.FULLSCREEN | pygame.SCALED
        else:
            flags |= pygame.RESIZABLE

        self._screen = pygame.display.set_mode((width, height), flags)
        pygame.display.set_caption("Metixel Photoframe — Dev Mode")

        # Read back actual resolution (passing 0,0 with FULLSCREEN uses desktop res)
        self._w = self._screen.get_width()
        self._h = self._screen.get_height()

        if hide_cursor:
            pygame.mouse.set_visible(False)

        self._clock = pygame.time.Clock()
        self._bg_color = (0, 0, 0)
        self._running = True

        logger.info("DevBackend created: %dx%d @ %d FPS", self._w, self._h, fps_limit)

    def destroy(self) -> None:
        self._running = False
        if self._pygame:
            self._pygame.quit()
        self._screen = None
        self._textures.clear()
        logger.info("DevBackend destroyed")

    def loop_running(self) -> bool:
        if not self._running:
            return False
        for event in self._pygame.event.get():
            if event.type == self._pygame.QUIT:
                self._running = False
                return False
            if event.type == self._pygame.KEYDOWN:
                if event.key == self._pygame.K_ESCAPE:
                    self._running = False
                    return False
        self._clock.tick(self._fps_limit)
        return True

    def swap_buffers(self) -> None:
        if self._screen:
            self._pygame.display.flip()

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
        if self._screen is None:
            return
        # Convert 0-1 float to 0-255 int
        c = tuple(int(v * 255) for v in color[:3])
        # Create a temporary surface with alpha
        alpha = int(color[3] * 255) if len(color) > 3 else 255
        surf = self._pygame.Surface((int(w), int(h)), self._pygame.SRCALPHA)
        surf.fill((*c, alpha))
        self._screen.blit(surf, (int(x), int(y)))

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
        if self._screen is None or texture is None:
            return

        if isinstance(texture, int):
            surf = self._textures.get(texture)
            if surf is None:
                return
        else:
            surf = texture

        # Scale to target size
        scaled = self._pygame.transform.smoothscale(surf, (int(w), int(h)))

        # Apply alpha
        if alpha < 1.0:
            scaled.set_alpha(int(alpha * 255))

        # Apply rotation
        if rotation != 0.0:
            scaled = self._pygame.transform.rotate(scaled, rotation)

        self._screen.blit(scaled, (int(x), int(y)))

    # -- Texture Management --------------------------------------------------

    def load_texture(self, path: Path | np.ndarray, **kwargs: Any) -> Any:
        """Load an image as a pygame Surface.

        Returns an integer handle that can be passed to draw_image.
        """
        if self._pygame is None:
            raise RuntimeError("Display not initialized")

        if isinstance(path, np.ndarray):
            # numpy array → pygame surface
            array = path
            if array.ndim == 3 and array.shape[2] == 4:
                # RGBA
                surf = self._pygame.image.frombuffer(
                    array.tobytes(), (array.shape[1], array.shape[0]), "RGBA"
                )
            elif array.ndim == 3 and array.shape[2] == 3:
                surf = self._pygame.image.frombuffer(
                    array.tobytes(), (array.shape[1], array.shape[0]), "RGB"
                )
            else:
                raise ValueError(f"Unsupported numpy array shape: {array.shape}")
        else:
            surf = self._pygame.image.load(str(path))

        # Convert for faster blitting
        surf = surf.convert_alpha() if surf.get_flags() & self._pygame.SRCALPHA else surf.convert()

        self._texture_counter += 1
        self._textures[self._texture_counter] = surf
        return self._texture_counter

    def unload_texture(self, texture: Any) -> None:
        if isinstance(texture, int):
            self._textures.pop(texture, None)

    def update_texture(self, texture: Any, data: np.ndarray) -> None:
        """Update a pygame texture surface with new pixel data in-place.

        Converts the numpy array to a pygame Surface and swaps it in
        the texture cache under the same handle, so subsequent
        ``draw_image`` calls use the updated frame.
        """
        if not isinstance(texture, int) or texture not in self._textures:
            super().update_texture(texture, data)
            return

        if self._pygame is None:
            return

        if data.ndim == 3 and data.shape[2] == 4:
            surf = self._pygame.image.frombuffer(
                data.tobytes(), (data.shape[1], data.shape[0]), "RGBA"
            )
        elif data.ndim == 3 and data.shape[2] == 3:
            surf = self._pygame.image.frombuffer(
                data.tobytes(), (data.shape[1], data.shape[0]), "RGB"
            )
        else:
            super().update_texture(texture, data)
            return

        surf = surf.convert_alpha() if surf.get_flags() & self._pygame.SRCALPHA else surf.convert()
        self._textures[texture] = surf

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
        if self._screen is None or self._pygame is None:
            return

        # Cache fonts by size
        if font_size not in self._font_cache:
            self._font_cache[font_size] = self._pygame.font.SysFont("sans-serif", font_size)

        font = self._font_cache[font_size]
        c = tuple(int(v * 255) for v in color[:3])
        text_surf = font.render(text, True, c)
        self._screen.blit(text_surf, (int(x), int(y)))

    # -- Display Control -----------------------------------------------------

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        self._bg_color = tuple(int(v * 255) for v in color[:3])

    def clear(self) -> None:
        if self._screen:
            self._screen.fill(self._bg_color)

    def display_power(self, on: bool) -> None:
        logger.info("DevBackend display_power(%s) — no-op on desktop", on)
