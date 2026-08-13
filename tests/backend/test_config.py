# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared config module."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_default_config():
    """Verify default config can be created."""
    from metixel.shared.config import Config

    config = Config()
    assert config.display["width"] == 0
    assert config.slideshow["image_duration_seconds"] == 15


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
    assert config.slideshow["video_max_duration_seconds"] == 0

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


# ── New video config section tests ──────────────────────────────────────


def test_video_section_defaults():
    """Verify the new video section has sensible defaults."""
    from metixel.shared.config import Config

    config = Config()
    v = config.video
    assert v["playback_enabled"] is True
    assert v["player_backend"] == "auto"
    assert v["max_duration_seconds"] == 0  # unlimited
    assert v["transcoding_enabled"] is True
    assert v["transcode_max_width"] == 0  # use display width
    assert v["transcode_max_height"] == 0  # use display height
    assert v["transcode_quality"] == 23
    assert v["cpu_throttle_enabled"] is True
    assert v["cpu_throttle_percent"] == 100


def test_video_section_save_load(tmp_path):
    """Verify the video section persists atomically."""
    from metixel.shared.config import Config

    config = Config()
    config_path = tmp_path / "config.json"

    config.update("video", {
        "playback_enabled": False,
        "max_duration_seconds": 300,
        "transcoding_enabled": False,
        "transcode_quality": 18,
        "cpu_throttle_percent": 30,
    })
    config.save(config_path)

    loaded = Config.load(config_path)
    v = loaded.video
    assert v["playback_enabled"] is False
    assert v["max_duration_seconds"] == 300
    assert v["transcoding_enabled"] is False
    assert v["transcode_quality"] == 18
    assert v["cpu_throttle_percent"] == 30


def test_video_section_legacy_fallback():
    """Verify the video section synthesises values from legacy slideshow keys."""
    import copy

    from metixel.shared.config import DEFAULT_CONFIG, Config
    old_data = copy.deepcopy(DEFAULT_CONFIG)
    old_data["slideshow"]["video_playback_enabled"] = False
    old_data["slideshow"]["video_max_duration_seconds"] = 60
    del old_data["video"]  # Remove the new section entirely

    config = Config(old_data)
    v = config.video
    # Should have picked up legacy values
    assert v["playback_enabled"] is False
    assert v["player_backend"] == "auto"
    assert v["max_duration_seconds"] == 60
    # New keys should have defaults
    assert v["transcoding_enabled"] is True
    assert v["transcode_quality"] == 23


# ── Parametrized default-value verification (all 79 keys) ──────────────

# Every key in DEFAULT_CONFIG with its expected default value.
# Flat keys use dotted-path notation: "section.key" or "section.sub.key".
ALL_DEFAULTS: list[tuple[str, object]] = [
    # display
    ("display.width", 0),
    ("display.height", 0),
    ("display.fullscreen", True),
    ("display.fps_limit", 30),
    ("display.hide_cursor", True),
    ("display.schedule_enabled", False),
    ("display.schedule_on_time", "07:00"),
    ("display.schedule_off_time", "22:00"),
    # slideshow
    ("slideshow.image_duration_seconds", 15),
    ("slideshow.video_playback_enabled", True),
    ("slideshow.video_player_backend", "auto"),
    ("slideshow.video_max_duration_seconds", 0),
    ("slideshow.transition_duration_ms", 2500),
    ("slideshow.transition_style", "crossfade"),
    ("slideshow.fit_mode", "cover"),
    ("slideshow.smart_cover", True),
    ("slideshow.matte_color", [0, 0, 0]),
    ("slideshow.shuffle", True),
    # image
    ("image.optimisation_enabled", True),
    ("image.optimise_max_width", 0),
    ("image.optimise_max_height", 0),
    # video (subset — remainder in video section defaults test)
    ("video.playback_enabled", True),
    ("video.player_backend", "auto"),
    ("video.max_duration_seconds", 0),
    ("video.transcoding_enabled", True),
    ("video.transcoding_profile", ""),
    ("video.keep_audio", False),
    ("video.transcode_max_width", 0),
    ("video.transcode_max_height", 0),
    ("video.transcode_quality", 23),
    ("video.transcode_use_software_encoder", True),
    ("video.transcode_timeout_seconds", 7200),
    ("video.cpu_throttle_enabled", True),
    ("video.cpu_throttle_percent", 100),
    # sync.immich
    ("sync.immich.enabled", False),
    ("sync.immich.server_url", "https://immich.example.com"),
    ("sync.immich.api_key", ""),
    ("sync.immich.albums", []),
    ("sync.immich.strict_sync", False),
    ("sync.immich.sync_dir", "media/sync/immich/"),
    ("sync.immich.poll_interval_seconds", 3600),
    # sync.local
    ("sync.local.enabled", True),
    ("sync.local.poll_interval_seconds", 30),
    # web
    ("web.host", "0.0.0.0"),
    ("web.port", 8080),
    ("web.debug", False),
    # mqtt
    ("mqtt.enabled", False),
    ("mqtt.broker", "localhost"),
    ("mqtt.port", 1883),
    ("mqtt.topic_prefix", "metixel"),
    ("mqtt.username", ""),
    ("mqtt.password", ""),
    # input
    ("input.cec_enabled", True),
    ("input.ir_enabled", False),
    ("input.ir_device", "/dev/lirc0"),
    ("input.keyboard_enabled", True),
    ("input.keyboard_map", {}),
    # messages
    ("messages.enabled", True),
    ("messages.default_duration", 5.0),
    ("messages.max_visible", 5),
    ("messages.persistent", []),
    # network
    ("network.wifi_country", ""),
    ("network.ap_fallback_enabled", True),
    ("network.ap_timeout_seconds", 60),
    ("network.ap_grace_period_seconds", 300),
    ("network.ap_max_duration_seconds", 600),
    ("network.connection_check_url", "http://connectivity-check.ubuntu.com"),
    # system
    ("system.cache_dir", "cache/"),
    ("system.log_level", "NONE"),
    ("system.quiet_boot", False),
    ("system.first_run", True),
    ("system.timezone", ""),
    ("system.ntp_enabled", True),
    ("system.ntp_servers", [""]),
    ("system.db_path", "cache/metixel.db"),
    # update
    ("update.channel", "stable"),
    ("update.auto_check", True),
    ("update.check_interval_hours", 6),
    ("update.github_repo", "dennisadvani/metixel-photoframe"),
    ("update.last_check", None),
    ("update.last_update", None),
    # timeouts
    ("timeouts.ffprobe_probe", 120),
    ("timeouts.ffprobe_validate", 60),
    ("timeouts.folder_watcher_probe", 120),
    ("timeouts.hw_codec_detect", 30),
    ("timeouts.thumbnail_extract", 300),
    ("timeouts.frame_extract_first", 180),
    ("timeouts.frame_extract_last", 120),
    ("timeouts.image_process", 120),
    ("timeouts.transcode", 7200),
    ("timeouts.vlc_start", 30),
]


def _get_nested(d: dict, dotted_key: str) -> object:
    """Resolve 'section.sub.key' → d['section']['sub']['key']."""
    parts = dotted_key.split(".")
    current: object = d
    for part in parts:
        assert isinstance(current, dict), f"Expected dict at {part!r} in {dotted_key}"
        current = current[part]
    return current


@pytest.mark.parametrize("dotted_key,expected", ALL_DEFAULTS)
def test_all_config_defaults(dotted_key: str, expected: object) -> None:
    """Every key in DEFAULT_CONFIG has its documented default value."""
    from metixel.shared.config import DEFAULT_CONFIG

    actual = _get_nested(DEFAULT_CONFIG, dotted_key)
    assert actual == expected, (
        f"DEFAULT_CONFIG key {dotted_key!r}: expected {expected!r}, got {actual!r}"
    )


# ── Config.timeout() tests ─────────────────────────────────────────────

class TestConfigTimeout:
    """Tests for the Config.timeout() helper."""

    def test_known_key_returns_value(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        assert cfg.timeout("vlc_start", 999) == 30

    def test_missing_key_returns_fallback(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        assert cfg.timeout("nonexistent_key", 42) == 42

    def test_zero_value_returns_fallback(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        cfg.update("timeouts", {"vlc_start": 0})
        assert cfg.timeout("vlc_start", 30) == 30

    def test_negative_value_returns_fallback(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        cfg.update("timeouts", {"vlc_start": -5})
        assert cfg.timeout("vlc_start", 30) == 30

    def test_string_value_returns_fallback(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        cfg.update("timeouts", {"vlc_start": "not_a_number"})  # type: ignore[dict-item]
        assert cfg.timeout("vlc_start", 30) == 30

    def test_float_value_truncated_to_int(self) -> None:
        from metixel.shared.config import Config

        cfg = Config()
        cfg.update("timeouts", {"vlc_start": 30.7})
        # float 30.7 → int(30.7) = 30
        assert cfg.timeout("vlc_start", 999) == 30

    def test_timeouts_section_fills_missing_keys(self) -> None:
        """timeouts property fills in keys missing from config."""
        from metixel.shared.config import Config

        cfg = Config()
        # Remove a key from timeouts
        cfg.update("timeouts", {"vlc_start": 60})
        # The rest should still be filled by defaults
        tos = cfg.timeouts
        assert tos["ffprobe_probe"] == 120
        assert tos["vlc_start"] == 60  # overridden

    def test_timeouts_property_does_not_mutate_defaults(self) -> None:
        """Accessing timeouts should not modify DEFAULT_CONFIG."""
        from metixel.shared.config import DEFAULT_CONFIG, Config

        original_transcode = DEFAULT_CONFIG["timeouts"]["transcode"]
        cfg = Config()
        _ = cfg.timeouts  # Access to trigger fill
        assert DEFAULT_CONFIG["timeouts"]["transcode"] == original_transcode

    def test_timeout_section_missing_entirely(self) -> None:
        """timeout() still returns defaults when timeouts section is absent.

        The ``timeouts`` property auto-creates the section and fills in
        all defaults, so even a missing section behaves correctly.
        """
        from metixel.shared.config import Config

        cfg = Config()
        cfg._data.pop("timeouts", None)
        # The timeouts property will re-create and backfill defaults
        assert cfg.timeout("vlc_start", 999) == 30


# ── resolve_watch_paths tests ──────────────────────────────────────────

class TestResolveWatchPaths:
    """Tests for resolve_watch_paths() utility."""

    def test_object_format_enabled(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": [
                    {"path": "/test/path", "enabled": True},
                ],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/opt/metixel")
        assert len(paths) == 1
        assert paths[0] == Path("/test/path")

    def test_object_format_disabled_filtered(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": [
                    {"path": "/test/path", "enabled": False},
                ],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/opt/metixel")
        assert len(paths) == 0

    def test_relative_path_resolved(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": [
                    {"path": "media/photos/", "enabled": True},
                ],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/opt/metixel")
        assert paths[0] == Path("/opt/metixel/media/photos/")

    def test_legacy_flat_list_format(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": ["/legacy/path/"],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/opt/metixel")
        assert len(paths) == 1
        assert paths[0] == Path("/legacy/path/")

    def test_legacy_relative_path_resolved(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": ["legacy/relative/"],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/home/pi")
        assert paths[0] == Path("/home/pi/legacy/relative/")

    def test_mixed_formats(self) -> None:
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        cfg.update("sync", {
            "local": {
                "watch_paths": [
                    {"path": "/enabled/path", "enabled": True},
                    {"path": "/disabled/path", "enabled": False},
                    "/legacy/path/",
                ],
            },
        })
        paths = resolve_watch_paths(cfg, base_dir="/opt/metixel")
        assert len(paths) == 2
        assert Path("/enabled/path") in paths
        assert Path("/legacy/path/") in paths

    def test_default_watch_paths(self) -> None:
        """Default config has 3 enabled watch paths, resolved to the base dir."""
        from metixel.shared.config import Config, resolve_watch_paths

        cfg = Config()
        base = Path("/opt/metixel")
        paths = resolve_watch_paths(cfg, base_dir=str(base))
        assert len(paths) == 3
        # All default paths are relative → resolved under the base dir
        assert all(str(p).startswith(str(base)) for p in paths)
