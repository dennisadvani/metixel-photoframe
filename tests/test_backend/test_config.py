# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared config module."""


def test_default_config():
    """Verify default config can be created."""
    from metixel.shared.config import DEFAULT_CONFIG, Config

    config = Config()
    assert config.display["width"] == 0
    assert config.slideshow["image_duration_seconds"] == 30


def test_config_update():
    """Verify config section updates work."""
    from metixel.shared.config import Config

    config = Config()
    config.update("display", {"width": 1280, "height": 720})
    assert config.display["width"] == 1280
    assert config.display["height"] == 720


def test_config_save_load(tmp_path):
    """Verify atomic config save and load."""
    from metixel.shared.config import Config

    config = Config()
    config.update("slideshow", {"image_duration_seconds": 10})

    config_path = tmp_path / "config.json"
    config.save(config_path)

    loaded = Config.load(config_path)
    assert loaded.slideshow["image_duration_seconds"] == 10


def test_config_missing_file_uses_defaults(tmp_path):
    """Verify loading a non-existent file returns defaults."""
    from metixel.shared.config import Config

    path = tmp_path / "nonexistent.json"
    config = Config.load(path)
    assert config.display["width"] == 0


def test_video_playback_enabled_persists(tmp_path):
    """Verify video_playback_enabled saves and loads back correctly.

    Regression test: the web UI checkbox must survive a page refresh.
    """
    from metixel.shared.config import Config

    config = Config()
    config_path = tmp_path / "config.json"

    # Default is True
    assert config.slideshow["video_playback_enabled"] is True
    assert config.slideshow["video_max_duration_seconds"] == 120

    # Simulate user unchecking the box and saving
    config.update("slideshow", {"video_playback_enabled": False})
    config.save(config_path)

    # Simulate page refresh — reload from disk
    loaded = Config.load(config_path)
    assert loaded.slideshow["video_playback_enabled"] is False

    # Simulate user checking the box and saving
    loaded.update("slideshow", {"video_playback_enabled": True})
    loaded.save(config_path)

    # Refresh again
    loaded2 = Config.load(config_path)
    assert loaded2.slideshow["video_playback_enabled"] is True


def test_video_max_duration_persists(tmp_path):
    """Verify video_max_duration_seconds saves and loads back correctly."""
    from metixel.shared.config import Config

    config = Config()
    config_path = tmp_path / "config.json"

    config.update("slideshow", {"video_max_duration_seconds": 300})
    config.save(config_path)

    loaded = Config.load(config_path)
    assert loaded.slideshow["video_max_duration_seconds"] == 300

    # Setting to 0 (unlimited) should also persist
    loaded.update("slideshow", {"video_max_duration_seconds": 0})
    loaded.save(config_path)

    loaded2 = Config.load(config_path)
    assert loaded2.slideshow["video_max_duration_seconds"] == 0


def test_config_update_boolean_false_values(tmp_path):
    """Verify that boolean false values are correctly set, not skipped.

    The _deep_merge function must not treat False as "no value to set".
    """
    from metixel.shared.config import Config

    config = Config()
    config_path = tmp_path / "config.json"

    # Start with True (default)
    assert config.slideshow["shuffle"] is True

    # Set to False and persist
    config.update("slideshow", {"shuffle": False, "video_playback_enabled": False})
    config.save(config_path)

    loaded = Config.load(config_path)
    assert loaded.slideshow["shuffle"] is False
    assert loaded.slideshow["video_playback_enabled"] is False


def test_video_player_backend_persists(tmp_path):
    """Verify video_player_backend saves and loads back correctly."""
    from metixel.shared.config import Config

    config = Config()
    config_path = tmp_path / "config.json"

    # Default is "auto"
    assert config.slideshow["video_player_backend"] == "auto"

    # Switch to vlc
    config.update("slideshow", {"video_player_backend": "vlc"})
    config.save(config_path)

    loaded = Config.load(config_path)
    assert loaded.slideshow["video_player_backend"] == "vlc"

    # Switch to ffmpeg
    loaded.update("slideshow", {"video_player_backend": "ffmpeg"})
    loaded.save(config_path)

    loaded2 = Config.load(config_path)
    assert loaded2.slideshow["video_player_backend"] == "ffmpeg"
