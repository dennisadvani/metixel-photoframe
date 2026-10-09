# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the boot and message overlay layers."""

from __future__ import annotations

from unittest import mock

from metixel.frontend.overlay.boot_layer import BootLayer
from metixel.frontend.overlay.message_layer import SLIDE_IN_MS, MessageLayer


class TestBootLayerReactivate:
    def _loaded_layer(self, state: str) -> tuple[BootLayer, mock.MagicMock]:
        layer = BootLayer()
        backend = mock.MagicMock()
        layer._backend_ref = backend
        layer._tex_loaded = True
        layer._bg_tex = "bg"
        layer._logo_tex = "logo"
        layer._spinner_tex = "spinner"
        layer._progress_bg_tex = "pbg"
        layer._progress_fill_tex = "pfill"
        layer._state = state
        return layer, backend

    def test_reactivate_while_fading_releases_textures(self):
        """Textures are live GPU objects mid-fade — they must be unloaded,
        not just dereferenced (which leaked them on every pipeline reset)."""
        layer, backend = self._loaded_layer("fading")

        layer.reactivate()

        unloaded = {c.args[0] for c in backend.unload_texture.call_args_list}
        assert unloaded == {"bg", "logo", "spinner", "pbg", "pfill"}
        assert layer._bg_tex is None
        assert layer._logo_tex is None
        assert layer._spinner_tex is None
        assert layer._progress_bg_tex is None
        assert layer._progress_fill_tex is None
        assert layer._tex_loaded is False
        assert layer._state == "active"
        assert layer.visible is True

    def test_reactivate_after_done_is_safe(self):
        layer, backend = self._loaded_layer("done")
        layer._finish()  # frees textures the normal way
        backend.unload_texture.reset_mock()

        layer.reactivate()

        backend.unload_texture.assert_not_called()
        assert layer._state == "active"
        assert layer._reset_mode is True

    def test_reactivate_while_active_is_noop(self):
        layer, backend = self._loaded_layer("active")
        layer.reactivate()
        backend.unload_texture.assert_not_called()
        assert layer._bg_tex == "bg"


class TestMessageLayerSlideIn:
    def test_alpha_follows_ease_out_cubic(self):
        layer = MessageLayer()
        layer.show(title="t", body="b")
        (m,) = layer._msgs
        start = 1000.0
        layer._tick(m, start)  # hidden → sliding_in
        assert m.state == "sliding_in"

        half = start + (SLIDE_IN_MS / 1000.0) * 0.5
        layer._tick(m, half)

        # ease_out_cubic(0.5) = 1 - 0.5^3 = 0.875 (the old linear ramp hit 1.0 here)
        assert abs(m._alpha - 0.875) < 1e-6

    def test_alpha_reaches_one_when_slide_in_completes(self):
        layer = MessageLayer()
        layer.show(title="t", body="b")
        (m,) = layer._msgs
        layer._tick(m, 1000.0)
        layer._tick(m, 1000.0 + SLIDE_IN_MS / 1000.0 + 0.01)
        assert m.state == "visible"
        assert m._alpha == 1.0
