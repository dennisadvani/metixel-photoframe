# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Phase 2 Display Backend: Mesa/DRM/Wayland via PyOpenGL.

Targets non-Pi SBCs (e.g., Radxa Zero 3W) running a modern Linux kernel with
Mesa drivers and a Wayland compositor.

This is a STUB for future Phase 2 implementation. It will be built when Phase 2
hardware becomes the primary target; until then the Raspberry Pi path and the
TkBackend desktop path are the supported ones.
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
            "Use TkBackend (desktop) or the Raspberry Pi backend for now."
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
        self,
        width=1920,
        height=1080,
        fullscreen=True,
        hide_cursor=True,
        fps_limit=30,
        refresh_rate=0,
        rotation=0,
        **kwargs,
    ):
        raise NotImplementedError(
            "WaylandBackend is not yet implemented. "
            "Run on a Raspberry Pi, or use METIXEL_DISPLAY_BACKEND=tk on desktop."
        )

    def destroy(self):
        self._running = False

    def loop_running(self):
        return self._running

    def swap_buffers(self):
        pass

    def present(
        self,
        plan,
        image=None,
        alpha=1.0,
        backdrop_source=None,
    ):
        raise NotImplementedError("WaylandBackend stub")

    def schedule(self, tick):
        raise NotImplementedError("WaylandBackend stub")

    def quit(self):
        self._running = False

    def load_image(self, path):
        raise NotImplementedError("WaylandBackend stub")

    def unload_image(self, handle):
        raise NotImplementedError("WaylandBackend stub")

    def set_background(self, color):
        self._bg_color = color

    def clear(self):
        pass

    def display_power(self, on):
        raise NotImplementedError("WaylandBackend stub")
