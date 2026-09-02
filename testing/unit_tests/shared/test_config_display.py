# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the display config schema (refresh rate + rotation)."""

from __future__ import annotations

from metixel.shared.config import DEFAULT_CONFIG, Config


class TestDisplayDefaults:
    """The display section exposes refresh_rate and rotation defaults."""

    def test_defaults_present(self):
        cfg = Config()
        d = cfg.display
        assert d["refresh_rate"] == 0
        assert d["rotation"] == 0

    def test_defaults_in_default_config(self):
        assert DEFAULT_CONFIG["display"]["refresh_rate"] == 0
        assert DEFAULT_CONFIG["display"]["rotation"] == 0

    def test_old_config_without_new_keys_loads_with_defaults(self, tmp_path):
        """A config file predating refresh_rate/rotation still loads."""
        import json

        old = {
            "display": {
                "width": 1920,
                "height": 1080,
                "fullscreen": True,
                "fps_limit": 30,
                "hide_cursor": True,
            }
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(old), encoding="utf-8")
        cfg = Config.load(p)
        d = cfg.display
        assert d["width"] == 1920
        assert d["height"] == 1080
        # New keys merged in with defaults
        assert d["refresh_rate"] == 0
        assert d["rotation"] == 0

    def test_update_persists_new_keys(self, tmp_path):
        import json

        p = tmp_path / "config.json"
        p.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        cfg = Config.load(p)
        cfg.update("display", {"refresh_rate": 60, "rotation": 90})
        cfg.save(p)
        reloaded = Config.load(p)
        assert reloaded.display["refresh_rate"] == 60
        assert reloaded.display["rotation"] == 90
