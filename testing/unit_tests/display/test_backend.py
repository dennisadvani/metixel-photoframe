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


def test_detect_backend_returns_tk(monkeypatch):
    """On a non-Pi machine, detect_backend should return TkBackend.

    When pi3d is importable (running on a Pi), it returns Pi3dBackend instead.
    The Wayland / override env vars are cleared so the result does not depend
    on the developer's session (WSL sets WAYLAND_DISPLAY, which would pick the
    Wayland backend).
    """
    pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")
    for var in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "METIXEL_DISPLAY_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    from metixel.display import detect_backend
    from metixel.display.tk_backend import TkBackend

    # Check if we're on a Pi with pi3d available
    try:
        import pi3d  # noqa: F401

        on_pi = True
    except ImportError:
        on_pi = False

    backend = detect_backend()
    if on_pi:
        from metixel.display.dispmanx_backend import Pi3dBackend

        assert isinstance(backend, Pi3dBackend), (
            f"On Pi with pi3d, expected Pi3dBackend, got {type(backend).__name__}"
        )
    else:
        assert isinstance(backend, TkBackend), (
            f"On non-Pi, expected TkBackend, got {type(backend).__name__}"
        )


def test_detect_backend_env_override(monkeypatch):
    """Setting METIXEL_DISPLAY_BACKEND=tk should force TkBackend."""
    pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")
    monkeypatch.setenv("METIXEL_DISPLAY_BACKEND", "tk")
    from metixel.display import detect_backend
    from metixel.display.tk_backend import TkBackend

    backend = detect_backend()
    assert isinstance(backend, TkBackend)
