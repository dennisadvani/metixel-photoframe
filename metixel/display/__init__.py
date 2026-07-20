# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Display backend abstraction layer.

Provides a hardware-agnostic interface for 2D rendering. The factory function
:func:`detect_backend` auto-selects the correct implementation based on the
runtime environment (Raspberry Pi model, available drivers, etc.).

On Trixie (Pi 2/3/Zero 2 W), pi3d uses Mesa EGL via cage/XWayland.
On desktop, the dev backend (pygame or tkinter) is used for testing.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from metixel.display.backend import DisplayBackend

logger = logging.getLogger(__name__)


def detect_backend() -> DisplayBackend:
    """Auto-detect the correct display backend for the current hardware.

    Detection order:
    1. Check for ``METIXEL_DISPLAY_BACKEND`` environment variable override
    2. On Raspberry Pi with pi3d installed: use Pi3dBackend (Mesa EGL)
    3. On other Linux: use Wayland/DRM backend
    4. On desktop / unknown: use dev backend (pygame or tkinter)
    """
    logger.info("Detecting display backend: platform=%s, python=%s", sys.platform, sys.version.split()[0])

    # -- Environment variable override ---------------------------------------
    env_backend = os.environ.get("METIXEL_DISPLAY_BACKEND", "").lower()
    if env_backend:
        logger.info("Display backend forced via env: %s", env_backend)
        if env_backend in ("dispmanx", "pi3d"):
            from metixel.display.dispmanx_backend import Pi3dBackend
            return Pi3dBackend()
        elif env_backend == "wayland":
            from metixel.display.wayland_backend import WaylandBackend
            return WaylandBackend()
        elif env_backend == "dev":
            from metixel.display.dev_backend import DevBackend
            return DevBackend()
        elif env_backend == "tk":
            from metixel.display.tk_backend import TkBackend
            return TkBackend()

    # -- Check if running on a Raspberry Pi ----------------------------------
    is_pi = _is_raspberry_pi()
    has_pi3d = _pi3d_available()
    logger.info("Hardware check: is_raspberry_pi=%s, pi3d_available=%s", is_pi, has_pi3d)

    if is_pi and has_pi3d:
        logger.info("Detected Raspberry Pi → Pi3dBackend (Mesa EGL via cage/XWayland)")
        from metixel.display.dispmanx_backend import Pi3dBackend
        return Pi3dBackend()

    # -- Linux (non-Pi, or Pi without pi3d) ----------------------------------
    if sys.platform == "linux":
        logger.info("Detected Linux → WaylandBackend")
        from metixel.display.wayland_backend import WaylandBackend
        return WaylandBackend()

    # -- Fallback: dev backend -----------------------------------------------
    # Try pygame first, fall back to tkinter (bundled with Python)
    try:
        import pygame  # noqa: F401
        logger.info("pygame available → DevBackend")
        from metixel.display.dev_backend import DevBackend
        return DevBackend()
    except ImportError:
        logger.info("pygame not available → TkBackend (tkinter)")
        from metixel.display.tk_backend import TkBackend
        return TkBackend()


def _is_raspberry_pi() -> bool:
    """Check if running on a Raspberry Pi by reading /proc/device-tree/model."""
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip()
            is_pi = "Raspberry Pi" in model
            if is_pi:
                logger.info("Pi model: %s", model)
            return is_pi
    except (OSError, FileNotFoundError):
        pass

    # Fallback: check for /opt/vc/lib (legacy Broadcom path)
    if os.path.exists("/opt/vc/lib/libEGL.so"):
        logger.info("Pi detected via legacy Broadcom libs at /opt/vc/lib")
        return True

    return False


def _pi3d_available() -> bool:
    """Check if the pi3d library can be imported.

    pi3d auto-detects the correct EGL platform at runtime (Mesa EGL on
    Trixie, or dispmanx on legacy Bullseye). We just need to know if
    the library itself is installed.
    """
    try:
        import pi3d  # noqa: F401
        return True
    except ImportError:
        logger.debug("pi3d not installed — Pi3dBackend unavailable")
        return False


def _has_legacy_broadcom() -> bool:
    """Check if the legacy Broadcom dispmanx driver is available.

    This is only relevant on Bullseye (Debian 11) systems with the legacy
    Broadcom graphics stack. On Trixie, the vc4 KMS driver is always used.
    """
    # The legacy driver provides EGL/GLES libs at /opt/vc/lib
    if os.path.exists("/opt/vc/lib/libEGL.so") and os.path.exists("/opt/vc/lib/libGLESv2.so"):
        # On RPi 4+, these may exist but KMS is active — check the driver in use
        # A simple heuristic: if /dev/dri/card0 exists with vc4, it's KMS
        if os.path.exists("/dev/dri/card0"):
            # KMS is available — but on Bullseye with legacy, vc4 may coexist
            # Check if dispmanx is actually available via vc_dispmanx helper
            try:
                with open("/proc/device-tree/soc/firmwarekms@7e000000/status") as f:
                    status = f.read().strip()
                    if status == "okay":
                        logger.debug("firmwarekms is active — KMS in use")
                        return False
            except (OSError, FileNotFoundError):
                pass
            # On Bullseye with legacy, dispmanx should still be accessible
            return True
        return True
    return False
