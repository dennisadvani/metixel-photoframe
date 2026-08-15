# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for StateManager — atomic config persistence and playlist management."""

from __future__ import annotations

import json
from pathlib import Path

from metixel.shared.models import MediaItem, MediaType


class TestStateManagerInit:
    """StateManager initialisation tests."""

    def test_creates_run_dir(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        run_dir = tmp_path / "run"
        StateManager(config_path, run_dir=run_dir)
        assert run_dir.is_dir()

    def test_initial_config_has_defaults(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")
        cfg = sm.config
        assert cfg.display["fullscreen"] is True
        assert cfg.slideshow["image_duration_seconds"] == 15

    def test_config_property_is_deep_copy(self, tmp_path: Path) -> None:
        """Mutating the returned config should not affect StateManager."""
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")
        cfg = sm.config
        cfg.update("slideshow", {"image_duration_seconds": 999})
        # Original should be unchanged
        assert sm.config.slideshow["image_duration_seconds"] == 15

    def test_get_config_value_nested(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")
        assert sm.get_config_value("slideshow", "image_duration_seconds") == 15
        assert sm.get_config_value("display", "width") == 0

    def test_get_config_value_missing_key_returns_default(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")
        assert sm.get_config_value("nonexistent", "key", default=42) == 42


class TestStateManagerConfigMutation:
    """Config update and persistence tests."""

    def test_update_config_persists_atomically(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        sm.update_config("slideshow", {"image_duration_seconds": 42})
        assert sm.config.slideshow["image_duration_seconds"] == 42

        # Reload from disk
        sm.reload_config()
        assert sm.config.slideshow["image_duration_seconds"] == 42

    def test_update_config_writes_flag_file(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        run_dir = tmp_path / "run"
        sm = StateManager(config_path, run_dir=run_dir)

        flag_file = run_dir / "config.updated"
        # Remove if already created during init
        if flag_file.exists():
            flag_file.unlink()

        sm.update_config("slideshow", {"image_duration_seconds": 99})
        assert flag_file.exists()

    def test_replace_config(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        new_data = sm.config.to_dict()
        new_data["slideshow"]["image_duration_seconds"] = 77
        sm.replace_config(new_data)
        assert sm.config.slideshow["image_duration_seconds"] == 77


class TestStateManagerPlaylist:
    """Playlist (MediaItem collection) tests."""

    def test_initial_playlist_empty(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")
        assert sm._playlist == []

    def test_add_playlist_items(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        item = MediaItem(
            id="abc123",
            original_path=Path("/tmp/photo.jpg"),
            cached_path=Path("/tmp/photo.jpg"),
            media_type=MediaType.IMAGE,
        )
        sm.add_playlist_items([item])
        assert len(sm._playlist) == 1
        assert sm._playlist[0].id == "abc123"

    def test_add_playlist_items_deduplicates(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        item = MediaItem(
            id="dup",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
        )
        sm.add_playlist_items([item])
        sm.add_playlist_items([item])
        assert len(sm._playlist) == 1

    def test_add_playlist_items_noop_on_duplicate_id(self, tmp_path: Path) -> None:
        """Adding an item with an existing id is a no-op (dedup)."""
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        item1 = MediaItem(
            id="m1",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            width=100,
        )
        sm.add_playlist_items([item1])

        # Same id, different width — should be a no-op
        item2 = MediaItem(
            id="m1",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            width=200,
        )
        sm.add_playlist_items([item2])
        assert len(sm._playlist) == 1
        # Original item untouched
        assert sm._playlist[0].width == 100

    def test_remove_playlist_items_by_id(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        item1 = MediaItem(
            id="keep",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
        )
        item2 = MediaItem(
            id="remove",
            original_path=Path("/tmp/b.jpg"),
            cached_path=Path("/tmp/b.jpg"),
            media_type=MediaType.IMAGE,
        )
        sm.add_playlist_items([item1, item2])
        assert len(sm._playlist) == 2

        sm.remove_playlist_items({"remove"})
        assert len(sm._playlist) == 1
        assert sm._playlist[0].id == "keep"

    def test_get_playlist_returns_copy(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        sm = StateManager(config_path, run_dir=tmp_path / "run")

        item = MediaItem(
            id="x",
            original_path=Path("/tmp/z.jpg"),
            cached_path=Path("/tmp/z.jpg"),
            media_type=MediaType.IMAGE,
        )
        sm.add_playlist_items([item])

        playlist = sm.get_playlist()
        playlist.clear()
        # Original should be unaffected
        assert len(sm._playlist) == 1


class TestStateManagerConfigEdgeCases:
    """Edge-case behaviour."""

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        deep_config = tmp_path / "deep" / "nested" / "config.json"
        sm = StateManager(deep_config, run_dir=tmp_path / "run")
        sm.update_config("slideshow", {"shuffle": False})
        # Should have created deep/nested/ and written config
        assert deep_config.exists()
        with open(deep_config) as f:
            data = json.load(f)
        assert data["slideshow"]["shuffle"] is False

    def test_load_existing_config(self, tmp_path: Path) -> None:
        from metixel.backend.state import StateManager

        config_path = tmp_path / "existing.json"
        config_path.write_text(
            json.dumps(
                {
                    "display": {"width": 800, "height": 600},
                    "slideshow": {"image_duration_seconds": 10},
                }
            )
        )

        sm = StateManager(config_path, run_dir=tmp_path / "run")
        assert sm.config.display["width"] == 800
        assert sm.config.display["height"] == 600
        # Missing keys filled from defaults
        assert sm.config.slideshow["shuffle"] is True
