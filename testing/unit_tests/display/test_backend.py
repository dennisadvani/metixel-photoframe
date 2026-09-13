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


def test_retired_backend_modules_stay_deleted():
    """The pi3d-era backend module must not come back.

    ``dispmanx_backend.py`` spent a release as a raising retirement stub, and
    that stub was then deleted.  It has twice been restored to disk as a side
    effect of editor/sync tooling, and the second time it was committed.  A
    resurrected stub is worse than useless: its docstring promises it will
    "fail loudly", but the factory no longer references it, so it is simply dead
    code that misleads whoever reads ``display/`` next.

    Asserting absence of the FILE (not just the symbol) is the point — importing
    it would succeed, since nothing stops a module existing.
    """
    from pathlib import Path

    display_dir = Path(__file__).resolve().parents[3] / "src" / "metixel" / "display"

    for retired in ("dispmanx_backend.py", "shaders"):
        assert not (display_dir / retired).exists(), (
            f"{retired} was retired in 2.0.0 and must not be reinstated"
        )

    # The factory must not reference the retired backend by any spelling.
    factory = (display_dir / "__init__.py").read_text(encoding="utf-8")
    assert "dispmanx_backend" not in factory
    assert "Pi3dBackend" not in factory


def _on_raspberry_pi() -> bool:
    """Whether we are running on a Pi (which selects the Qt backend)."""
    from metixel.shared.platform import is_raspberry_pi

    return bool(is_raspberry_pi())
