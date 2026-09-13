# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Display backend abstraction layer.

Provides a hardware-agnostic interface for frame rendering.  The factory
function :func:`detect_backend` auto-selects the correct implementation based on
the runtime environment.

On a Raspberry Pi the backend is PySide6 + mpv (under cage); on a desktop the
TkBackend (tkinter) is used for development, so the whole slideshow can be
exercised without hardware.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from metixel.display.backend import DisplayBackend

from metixel.shared.platform import is_raspberry_pi, read_device_tree_model

logger = logging.getLogger(__name__)


def detect_backend() -> DisplayBackend:
    """Auto-detect the correct display backend for the current hardware.

    Detection order:
    1. ``METIXEL_DISPLAY_BACKEND`` environment variable override (dev / debug)
    2. On a Raspberry Pi: ``PySide6Backend`` (cage + Qt + mpv)
    3. On Linux with a Wayland session but no Qt: ``WaylandBackend`` (Phase 2 stub)
    4. Otherwise: ``TkBackend`` (tkinter) for desktop development

    Preferring the Qt backend whenever it is *importable* rather than
    additionally probing for a compositor is deliberate: on the Pi, cage always
    provides Wayland, and a missing compositor should surface as a Qt startup
    failure that the OTA health gate can catch — not as a silent fall back to a
    dev renderer that shows a window nobody is looking at.
    """
    logger.info(
        "Detecting display backend: platform=%s, python=%s", sys.platform, sys.version.split()[0]
    )

    forced = os.environ.get("METIXEL_DISPLAY_BACKEND", "").strip().lower()
    if forced:
        return _backend_for_override(forced)

    # -- Raspberry Pi → PySide6 + mpv ----------------------------------------
    if _is_raspberry_pi():
        if _module_available("PySide6"):
            logger.info("Detected Raspberry Pi → PySide6Backend (cage + Qt + mpv)")
            from metixel.display.qt_backend import PySide6Backend

            return PySide6Backend()
        logger.error(
            "Raspberry Pi detected but PySide6 is not installed — falling back to "
            "the Tk dev backend, which cannot render on a KMS-only console. "
            "Install the Qt packages (see requirements-system.txt)."
        )

    # -- Linux with a Wayland session (non-Pi SBCs, Phase 2) -----------------
    if sys.platform == "linux" and (
        os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland"
    ):
        logger.info("Detected Linux + Wayland → WaylandBackend (Phase 2 stub)")
        from metixel.display.wayland_backend import WaylandBackend

        return WaylandBackend()

    # -- Fallback: dev backend -----------------------------------------------
    logger.info("Using TkBackend (tkinter) for desktop development")
    from metixel.display.tk_backend import TkBackend

    return TkBackend()


def _backend_for_override(forced: str) -> DisplayBackend:
    """Return the backend named by ``METIXEL_DISPLAY_BACKEND``.

    This exists for development and diagnosis.  The Pi's systemd units no longer
    set it — an ``auto`` value that selected a renderer is exactly the kind of
    indirection that hides which backend is really running.
    """
    logger.info("Display backend forced via env: %s", forced)

    if forced in ("qt", "pyside6", "auto"):
        from metixel.display.qt_backend import PySide6Backend

        return PySide6Backend()
    if forced in ("tk", "dev"):
        from metixel.display.tk_backend import TkBackend

        return TkBackend()
    if forced == "wayland":
        from metixel.display.wayland_backend import WaylandBackend

        return WaylandBackend()
    if forced in ("dispmanx", "pi3d"):
        # Retired in 2.0.0. Constructing it raises with an explanation rather
        # than silently selecting something else, so a stale override on a
        # device is diagnosed instead of ignored.
        from metixel.display.dispmanx_backend import Pi3dBackend

        return Pi3dBackend()

    raise ValueError(
        f"Unknown METIXEL_DISPLAY_BACKEND value: {forced!r}. "
        "Valid values: qt, tk, wayland, dispmanx (retired)."
    )


def _is_raspberry_pi() -> bool:
    """Check if running on a Raspberry Pi (model file or legacy libs)."""
    is_pi = is_raspberry_pi()
    model = read_device_tree_model()
    if is_pi and model:
        logger.info("Pi model: %s", model)
    return is_pi


def _module_available(name: str) -> bool:
    """Return whether *name* can be imported, without importing it permanently."""
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
