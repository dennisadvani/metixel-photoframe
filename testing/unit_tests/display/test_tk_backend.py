# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the tkinter dev backend's per-frame canvas handling.

No display is needed: the tests inject mock ``Tk``/``Canvas`` objects
instead of calling ``create()``.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest

tk = pytest.importorskip("tkinter", reason="tkinter not installed (headless Pi)")

from metixel.display.tk_backend import TkBackend  # noqa: E402


@pytest.fixture
def backend() -> TkBackend:
    b = TkBackend()
    b._root = mock.MagicMock()
    b._canvas = mock.MagicMock()
    b._running = True
    return b


class TestPerFrameClear:
    def test_loop_running_clears_canvas_after_processing_events(self, backend):
        """The renderer never calls clear() between frames — the backend must
        drop last frame's items itself or they pile up forever."""
        assert backend.loop_running() is True

        backend._root.update.assert_called_once()
        backend._canvas.delete.assert_called_once_with("all")

    def test_swap_buffers_paints_before_the_next_clear(self, backend):
        backend.swap_buffers()
        backend._root.update_idletasks.assert_called_once()

    def test_loop_running_false_when_window_closed(self, backend):
        backend._root.update.side_effect = tk.TclError("gone")
        assert backend.loop_running() is False
        assert backend.is_running is False
        backend._canvas.delete.assert_not_called()

    def test_photo_cache_survives_per_frame_clear(self, backend):
        """The current slide's PhotoImage is reused frame to frame; only an
        explicit clear() drops it."""
        backend._photo_cache[(1, 1.0)] = mock.MagicMock()
        backend.loop_running()
        assert (1, 1.0) in backend._photo_cache

        backend.clear()
        assert backend._photo_cache == {}
        backend._canvas.delete.assert_called_with("all")

    def test_swap_and_loop_without_root_are_safe(self):
        b = TkBackend()
        b.swap_buffers()
        assert b.loop_running() is False


class TestUpdateTextureContract:
    def test_in_place_update_returns_same_handle(self, backend):
        arr = np.zeros((4, 4, 3), dtype=np.uint8)
        handle = backend.load_texture(arr)
        backend._photo_cache[(handle, 1.0)] = mock.MagicMock()

        new_arr = np.full((4, 4, 3), 200, dtype=np.uint8)
        returned = backend.update_texture(handle, new_arr)

        assert returned == handle
        assert (handle, 1.0) not in backend._photo_cache  # stale render dropped
        assert backend._textures[handle].getpixel((0, 0)) == (200, 200, 200)

    def test_fallback_returns_the_new_handle(self, backend):
        """The ABC default unloads and reloads — the old handle is dead, so the
        caller must be handed the replacement."""
        arr = np.zeros((4, 4, 3), dtype=np.uint8)
        stale_handle = 999  # not a known texture → default path

        returned = backend.update_texture(stale_handle, arr)

        assert returned != stale_handle
        assert returned in backend._textures
