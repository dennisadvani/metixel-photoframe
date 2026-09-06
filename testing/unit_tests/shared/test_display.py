# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the rotation-aware effective screen-size helper."""

from __future__ import annotations

from metixel.shared.display import effective_screen_size


class TestEffectiveScreenSize:
    def test_explicit_config_dims_apply_rotation_90(self):
        """90° rotation swaps explicit native dims (portrait)."""
        sw, sh = effective_screen_size({"width": 1920, "height": 1200, "rotation": 90})
        assert (sw, sh) == (1200, 1920)

    def test_explicit_config_dims_apply_rotation_270(self):
        sw, sh = effective_screen_size({"width": 1920, "height": 1200, "rotation": 270})
        assert (sw, sh) == (1200, 1920)

    def test_no_rotation_keeps_dims(self):
        sw, sh = effective_screen_size({"width": 1920, "height": 1200, "rotation": 0})
        assert (sw, sh) == (1920, 1200)

    def test_rotation_180_keeps_dims(self):
        sw, sh = effective_screen_size({"width": 1920, "height": 1200, "rotation": 180})
        assert (sw, sh) == (1920, 1200)

    def test_missing_config_uses_display_info(self, monkeypatch, tmp_path):
        """Uses the frontend-detected (already-rotated) size when config dims are 0."""
        monkeypatch.delenv("METIXEL_RUN_DIR", raising=False)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "display_info.json").write_text(
            '{"width":1200,"height":1920,"rotation":90}', encoding="utf-8"
        )
        monkeypatch.setenv("METIXEL_RUN_DIR", str(run_dir))
        sw, sh = effective_screen_size({"width": 0, "height": 0, "rotation": 90})
        assert (sw, sh) == (1200, 1920)

    def test_fallback_when_nothing_known(self, monkeypatch, tmp_path):
        monkeypatch.delenv("METIXEL_RUN_DIR", raising=False)
        run_dir = tmp_path / "empty"
        run_dir.mkdir()
        monkeypatch.setenv("METIXEL_RUN_DIR", str(run_dir))
        sw, sh = effective_screen_size({})
        assert (sw, sh) == (1920, 1080)

    def test_missing_display_info_file_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.delenv("METIXEL_RUN_DIR", raising=False)
        run_dir = tmp_path / "nodata"
        run_dir.mkdir()
        monkeypatch.setenv("METIXEL_RUN_DIR", str(run_dir))
        sw, sh = effective_screen_size({"width": 0, "height": 0, "rotation": 90})
        assert (sw, sh) == (1920, 1080)
