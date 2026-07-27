# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Phase 2 Display Backend: Mesa/DRM/Wayland via PyOpenGL.

Targets Raspberry Pi 4/5 and other non-Pi SBCs (e.g., Radxa Zero 3W)
running a modern Linux kernel with Mesa drivers and Wayland compositor.

This is a STUB for future Phase 2 implementation. Phase 1 code runs against
the Pi3dBackend; this backend will be implemented when Phase 2 hardware
becomes the primary target.
"""

from __future__ import annotations

import logging

from metixel.display.backend import DisplayBackend

logger = logging.getLogger(__name__)


class WaylandBackend(DisplayBackend):
    """Phase 2 STUB — PyOpenGL + EGL on Wayland/DRM.

    This backend will be implemented during Phase 2 development. It will:
    - Use PyOpenGL for OpenGL ES 3.0+ rendering
    - Create an EGL context on a Wayland surface (wl_egl_window)
    - Or use DRM/KMS directly via GBM for headless operation
    - Target Mesa drivers (vc4, v3d, panfrost, lima)
    """

    def __init__(self) -> None:
        logger.warning(
            "WaylandBackend is a STUB — Phase 2 rendering is not yet implemented. "
            "Use DispmanxBackend (Phase 1) or TkBackend (desktop) for now."
        )
        self._running: bool = False
        self._w: int = 1920
        self._h: int = 1080
        self._bg_color: tuple[float, float, float, float] = (0, 0, 0, 1)

    @property
    def width(self) -> int:
        return self._w

    @property
    def height(self) -> int:
        return self._h

    @property
    def is_running(self) -> bool:
        return self._running

    def create(
        self, width=1920, height=1080, fullscreen=True,
        hide_cursor=True, fps_limit=30, **kwargs,
    ):
        raise NotImplementedError(
            "WaylandBackend is not yet implemented. "
            "Set METIXEL_DISPLAY_BACKEND=dev for desktop development."
        )

    def destroy(self):
        self._running = False

    def loop_running(self):
        return self._running

    def swap_buffers(self):
        pass

    def draw_rect(self, x, y, w, h, color=(0, 0, 0, 1), z=0.0):
        raise NotImplementedError("WaylandBackend stub")

    def draw_image(self, texture, x, y, w, h, alpha=1.0, rotation=0.0, z=0.0,
                   uv_offset=(0.0, 0.0), uv_scale=(1.0, 1.0)):
        raise NotImplementedError("WaylandBackend stub")

    def load_texture(self, path, **kwargs):
        raise NotImplementedError("WaylandBackend stub")

    def unload_texture(self, texture):
        raise NotImplementedError("WaylandBackend stub")

    def draw_text(self, text, x, y, font_size=24, color=(1, 1, 1, 1), z=10.0):
        raise NotImplementedError("WaylandBackend stub")

    def set_background(self, color):
        self._bg_color = color

    def clear(self):
        pass

    def display_power(self, on):
        raise NotImplementedError("WaylandBackend stub")
