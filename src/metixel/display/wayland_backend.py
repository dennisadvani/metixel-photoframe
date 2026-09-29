# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Phase 2 Display Backend: Mesa/DRM/Wayland via PyOpenGL.

Targets Raspberry Pi 4/5 and other non-Pi SBCs (e.g., Radxa Zero 3W)
running a modern Linux kernel with Mesa drivers and Wayland compositor.

This is a STUB for future Phase 2 implementation. Phase 1 renders through
:class:`~metixel.display.qt_qml_backend.QmlBackend` (Qt Quick under cage); this
backend will be implemented when Phase 2 hardware becomes the primary target.
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
            "Use QmlBackend (Pi under cage) or TkBackend (desktop) for now."
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
            "Set METIXEL_DISPLAY_BACKEND=dev for desktop development."
        )

    def destroy(self):
        self._running = False

    def loop_running(self):
        return self._running

    def swap_buffers(self):
        """No-op — the surface ABC presents on the compositor's own clock."""

    # -- Surface ABC (2.0.0) -------------------------------------------------
    #
    # These four are declared by the surface ABC and this stub predated it, so the
    # class was left ABSTRACT and `detect_backend()` raised "Can't instantiate
    # abstract class WaylandBackend" on any Linux + Wayland machine that is not a
    # Pi — including a developer's desktop. Importing the module succeeds, so this
    # was only visible by INSTANTIATING the backend; a module-level smoke test does
    # not catch it.
    #
    # They raise rather than silently no-op: `create()` already raises, so a silent
    # no-op here is unreachable code that would turn "Phase 2 is not implemented"
    # into "the frame renders nothing, for no stated reason".
    def present(self, plan, image=None, alpha=1.0, backdrop_source=None):
        raise NotImplementedError("WaylandBackend stub — see create()")

    def load_image(self, path):
        raise NotImplementedError("WaylandBackend stub — see create()")

    def unload_image(self, handle):
        raise NotImplementedError("WaylandBackend stub — see create()")

    def schedule(self, tick):
        raise NotImplementedError("WaylandBackend stub — see create()")

    # The primitive surface this class used to stub as well (``draw_rect`` /
    # ``draw_image`` / ``load_texture`` / ``unload_texture`` / ``draw_text``) was
    # retired with the rest of the per-primitive API in 2.0.0.  Stubbing a method
    # the ABC no longer declares is worse than dead code: it advertises an
    # interface the frame must not be built against.  Everything now arrives
    # through ``present()``, so those were removed rather than left raising.
    def set_background(self, color):
        self._bg_color = color

    def clear(self):
        pass

    def display_power(self, on):
        raise NotImplementedError("WaylandBackend stub")
