# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the Metixel Photoframe display backend abstraction."""


def test_backend_abc_imports():
    """Verify the DisplayBackend ABC can be imported."""
    from metixel.display.backend import DisplayBackend
    assert DisplayBackend is not None


def test_dev_backend_imports():
    """Verify the DevBackend can be imported."""
    from metixel.display.dev_backend import DevBackend
    assert DevBackend is not None


def test_detect_backend_returns_dev():
    """On a non-Pi machine, detect_backend should return DevBackend.

    When pi3d is importable (running on a Pi), it returns Pi3dBackend instead.
    """
    from metixel.display import detect_backend
    from metixel.display.dev_backend import DevBackend

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
        assert isinstance(backend, DevBackend), (
            f"On non-Pi, expected DevBackend, got {type(backend).__name__}"
        )


def test_detect_backend_env_override():
    """Setting METIXEL_DISPLAY_BACKEND=dev should force DevBackend."""
    import os

    os.environ["METIXEL_DISPLAY_BACKEND"] = "dev"
    from metixel.display import detect_backend
    from metixel.display.dev_backend import DevBackend

    backend = detect_backend()
    assert isinstance(backend, DevBackend)
    del os.environ["METIXEL_DISPLAY_BACKEND"]
