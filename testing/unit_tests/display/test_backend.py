# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Metixel Photoframe display backend abstraction."""

import pytest


def test_backend_abc_imports():
    """Verify the DisplayBackend ABC can be imported."""
    from metixel.display.backend import DisplayBackend

    assert DisplayBackend is not None


def test_tk_backend_imports():
    """Verify the TkBackend can be imported."""
    pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")
    from metixel.display.tk_backend import TkBackend

    assert TkBackend is not None


def test_detect_backend_returns_tk():
    """On a non-Pi machine, detect_backend should return TkBackend.

    On a Raspberry Pi it returns the PySide6 backend instead, so the assertion
    is conditional on the hardware rather than hard-coded.
    """
    pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")
    from metixel.display import detect_backend

    backend = detect_backend()
    if _on_raspberry_pi():
        from metixel.display.qt_backend import PySide6Backend

        assert isinstance(backend, PySide6Backend), (
            f"On a Pi, expected PySide6Backend, got {type(backend).__name__}"
        )
    else:
        from metixel.display.tk_backend import TkBackend

        assert isinstance(backend, TkBackend), (
            f"On non-Pi, expected TkBackend, got {type(backend).__name__}"
        )


def test_detect_backend_env_override():
    """Setting METIXEL_DISPLAY_BACKEND=tk should force TkBackend."""
    pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")
    import os

    os.environ["METIXEL_DISPLAY_BACKEND"] = "tk"
    try:
        from metixel.display import detect_backend
        from metixel.display.tk_backend import TkBackend

        assert isinstance(detect_backend(), TkBackend)
    finally:
        del os.environ["METIXEL_DISPLAY_BACKEND"]


def test_retired_pi3d_override_fails_loudly():
    """A stale ``dispmanx`` override must be diagnosed, not silently ignored.

    pi3d was removed in 2.0.0 along with the backend that implemented it. A device
    carrying the old override should say so plainly, because the alternative —
    quietly selecting a different renderer — leaves an operator believing they are
    running the pi3d path when they are not.

    The error is a ``ValueError`` listing the valid values, not a silent fallback:
    the override is now simply not a recognised name.
    """
    import os

    for stale in ("dispmanx", "pi3d"):
        os.environ["METIXEL_DISPLAY_BACKEND"] = stale
        try:
            from metixel.display import detect_backend

            with pytest.raises(ValueError, match="retired in 2.0.0"):
                detect_backend()
        finally:
            del os.environ["METIXEL_DISPLAY_BACKEND"]


def test_unknown_backend_override_is_rejected():
    """A typo in the override must not silently fall through to a default."""
    import os

    os.environ["METIXEL_DISPLAY_BACKEND"] = "qt6-wayland-please"
    try:
        from metixel.display import detect_backend

        with pytest.raises(ValueError, match="Unknown METIXEL_DISPLAY_BACKEND"):
            detect_backend()
    finally:
        del os.environ["METIXEL_DISPLAY_BACKEND"]


def _on_raspberry_pi() -> bool:
    """Whether we are running on a Pi (which selects the Qt backend)."""
    from metixel.shared.platform import is_raspberry_pi

    return bool(is_raspberry_pi())
