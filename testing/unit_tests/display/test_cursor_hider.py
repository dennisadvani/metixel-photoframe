# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the cursor hider (cage/Wayland cursor hiding).

These tests exercise the logic without real hardware.  The evdev library is
only imported lazily inside ``CursorHider``, so we can test the factory and
the park()/close() behaviour with a fake device.  Hardware-dependent tests
use ``pytest.importorskip``.
"""

import pytest

from metixel.display.cursor_hider import (
    CursorHider,
    CursorHiderClient,
    build_cursor_hider,
)


class FakeUInput:
    """Minimal stand-in for evdev.UInput that records writes."""

    def __init__(self, cap, name):
        self.cap = cap
        self.name = name
        self.writes = []
        self.syn_count = 0
        self.closed = False

    def write(self, etype, code, value):
        self.writes.append((etype, code, value))

    def syn(self):
        self.syn_count += 1

    def close(self):
        self.closed = True


def test_build_cursor_hider_returns_instance():
    hider = build_cursor_hider()
    assert isinstance(hider, CursorHider)


def test_park_writes_off_screen_coords(monkeypatch):
    """park() must write EV_ABS coords beyond the fixed ABS max (4096)."""
    fake = FakeUInput({}, "test")
    hider = CursorHider(lo=5000, hi=6000)
    hider._ui = fake
    hider._abs_x = 0  # ABS_X
    hider._abs_y = 1  # ABS_Y

    monkeypatch.setattr("metixel.display.cursor_hider.random.randint", lambda a, b: 5500)

    hider.park()

    # Two ABS writes (X and Y) + a syn.
    assert len(fake.writes) == 2
    x_code, y_code = fake.writes[0][1], fake.writes[1][1]
    # Both values must be beyond the ABS max so the pointer is off-screen.
    assert all(v > 4096 for _, _, v in fake.writes)
    assert fake.syn_count == 1
    # X and Y use distinct axis codes.
    assert x_code != y_code


def test_park_noop_without_device():
    """park() must be a safe no-op when no device has been created."""
    hider = CursorHider()
    hider._ui = None
    hider.park()  # should not raise


def test_close_cleans_up_device():
    fake = FakeUInput({}, "test")
    hider = CursorHider()
    hider._ui = fake
    hider.close()
    assert fake.closed is True
    assert hider._ui is None


def test_start_requires_evdev(monkeypatch):
    """start() must raise cleanly if evdev is unavailable (graceful)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "evdev":
            raise ImportError("No module named 'evdev'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    hider = CursorHider()
    with pytest.raises(ImportError):
        hider.start()


def test_evdev_import_skips_if_missing():
    """Hardware test — skipped when evdev isn't installed."""
    evdev = pytest.importorskip("evdev")
    assert hasattr(evdev, "UInput")


def test_fire_window_fires_for_duration(monkeypatch):
    """_fire_window() must fire every interval for the configured duration."""
    fake = FakeUInput({}, "test")
    hider = CursorHider(interval=0.01, fire_duration=0.05)
    hider._ui = fake
    hider._abs_x = 0
    hider._abs_y = 1

    monkeypatch.setattr("metixel.display.cursor_hider.random.randint", lambda a, b: 5500)

    hider._fire_window()

    # ~5 fires over 0.05s at 0.01s interval (allow some slack).
    assert 3 <= fake.syn_count <= 10
    assert hider._count == fake.syn_count


def test_client_trigger_returns_false_without_service():
    """Client trigger must be a safe no-op (False) when no service is listening."""
    client = CursorHiderClient(socket_path="/nonexistent/cursor-hider.sock")
    assert client.trigger() is False
