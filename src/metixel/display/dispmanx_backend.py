# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""RETIRED: Phase 1 Display Backend (pi3d via Mesa EGL + cage/XWayland).

Metixel 2.0.0 replaced pi3d with **PySide6 + mpv** running under cage.  The pi3d
renderer is gone; what remains here is:

* :class:`Pi3dBackend`, which raises on construction so a device that still
  selects it gets a clear diagnosis rather than a half-implemented interface;
* the hardware helpers it used to delegate to
  (:class:`~metixel.display.hardware.WlrOutput`,
  :class:`~metixel.display.hardware.DisplayPower`), which are still live and
  are re-exported for their tests.

**Why keep a stub instead of deleting the file?**  The renderer was removed in
the same change that reduced :class:`~metixel.display.backend.DisplayBackend` to
a ``present(plan)`` surface, so this class can no longer satisfy its own ABC.
Failing loudly is the correct degradation for the transition; the file itself is
deleted once the PySide6 backend is proven on hardware.
"""

from __future__ import annotations

import logging

from metixel.display.backend import DisplayBackend
from metixel.display.hardware import DisplayPower, GpuInfo, WlrOutput

logger = logging.getLogger(__name__)


class Pi3dBackend(DisplayBackend):
    """RETIRED — the pi3d renderer is superseded by the PySide6 backend.

    Metixel 2.0.0 replaced pi3d with PySide6 + mpv.  This class is kept only so
    that a device mid-upgrade, or a stale ``METIXEL_DISPLAY_BACKEND=dispmanx``
    override, produces a clear diagnosis instead of an ``AttributeError`` from a
    half-implemented interface.

    It deliberately does NOT implement the drawing surface.  The renderer it was
    written for no longer exists, and re-implementing pi3d drawing against the
    reduced :class:`~metixel.display.backend.DisplayBackend` would mean carrying
    a second renderer that nothing exercises.

    ``__init__`` raises, and the ABC's abstract methods are **stubbed rather
    than left abstract** on purpose: an abstract class fails with Python's
    generic "Can't instantiate abstract class" message naming thirteen missing
    methods, which tells an operator nothing about what to do.  Stubbing them
    lets the error explain the actual situation and the one-line remedy.

    The *hardware* concerns it used to own — GPU memory introspection, wlr-randr
    output detection, and the tiered display-power chain — live on in
    :mod:`metixel.display.hardware` and are still used, so they are re-exported
    here for the tests and any remaining caller that reaches for them.
    """

    def __init__(self) -> None:
        raise RuntimeError(
            "Pi3dBackend has been removed in Metixel 2.0.0. The display stack is "
            "now PySide6 + mpv under cage. This device should be running the "
            "PySide6 backend; if METIXEL_DISPLAY_BACKEND is set to 'dispmanx' or "
            "'pi3d', unset it (it is a retired pi3d factory switch)."
        )

    # -- Abstract members stubbed so __init__'s message is what the user sees --

    @property
    def width(self) -> int:
        return 0

    @property
    def height(self) -> int:
        return 0

    @property
    def is_running(self) -> bool:
        return False

    def create(self, *args: object, **kwargs: object) -> None:
        pass

    def destroy(self) -> None:
        pass

    def loop_running(self) -> bool:
        return False

    def swap_buffers(self) -> None:
        pass

    def present(self, plan: object, image: object = None, alpha: float = 1.0) -> None:
        pass

    def load_image(self, path: object) -> object:
        return None

    def unload_image(self, handle: object) -> None:
        pass

    def set_background(self, color: tuple[float, float, float, float]) -> None:
        pass

    def clear(self) -> None:
        pass

    def display_power(self, on: bool) -> None:
        pass

    # -- Hardware helpers retained (used by tests and the health endpoint) ---

    def _detect_wlr_output(self) -> str | None:
        """Return the connected output name (delegates to :class:`WlrOutput`)."""
        return WlrOutput.detect()

    def _disable_empty_outputs(self) -> None:
        """Disable outputs reporting no EDID (delegates to :class:`WlrOutput`)."""
        WlrOutput().disable_empty_outputs()

    @staticmethod
    def _drm_dpms(state: str) -> bool:
        """Set display DPMS state via KMS sysfs (delegates to :class:`DisplayPower`)."""
        return DisplayPower._drm_dpms(state)


# Backwards-compatible module-level aliases used by callers/tests.
GpuInfoProvider = GpuInfo
WlrOutputManager = WlrOutput
DisplayPowerController = DisplayPower
