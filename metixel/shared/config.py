# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Configuration schema, validation, and defaults for Metixel Photoframe."""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "display": {
        "width": 0,
        "height": 0,
        "fullscreen": True,
        "fps_limit": 30,
        "hide_cursor": True,
    },
    "slideshow": {
        "image_duration_seconds": 15,
        "video_playback_enabled": True,  # Legacy — prefer video.playback_enabled
        "video_player_backend": "auto",  # Legacy — prefer video.player_backend
        "video_max_duration_seconds": 0,  # Legacy — prefer video.max_duration_seconds
        "transition_duration_ms": 2500,
        "transition_style": "crossfade",  # crossfade, fade_through_black, none
        "fit_mode": "cover",  # contain, cover, fill
        "smart_cover": True,  # use contain for square/opposite-orientation images in cover mode
        "matte_color": [0, 0, 0],  # RGB
        "shuffle": True,
    },
    "image": {
        "optimisation_enabled": True,
        "optimise_max_width": 0,  # 0 = use display width; images wider than this get resized
        "optimise_max_height": 0,  # 0 = use display height; images taller than this get resized
    },
    "video": {
        "playback_enabled": True,
        "player_backend": "auto",  # auto, vlc, ffmpeg
        "max_duration_seconds": 0,  # 0 = unlimited
        "transcoding_enabled": True,
        "transcode_max_width": 0,  # 0 = use display width
        "transcode_max_height": 0,  # 0 = use display height
        "transcode_quality": 23,  # CRF value (lower = better, 18-28 typical)
        "transcode_use_software_encoder": True,  # libx264 (best quality); False = try hardware first
        "transcode_timeout_seconds": 7200,  # max time per transcode (2 hours)
        "cpu_throttle_enabled": True,
        "cpu_throttle_percent": 50,  # 0-100, percentage of CPU to leave idle
    },
    "sync": {
        "immich": {
            "enabled": False,
            "server_url": "https://immich.example.com",
            "api_key": "",
            "album_name": "",
            "strict_sync": False,
            "sync_dir": "media/sync/immich/",
            "poll_interval_seconds": 3600,  # 60 minutes
        },
        "local": {
            "enabled": True,
            "watch_paths": [
                {"path": "media/sample_media/", "enabled": True},
                {"path": "media/sync/immich/", "enabled": True},
                {"path": "media/my_media/", "enabled": True},
            ],
            "poll_interval_seconds": 30,
        },
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "debug": False,
    },
    "mqtt": {
        "enabled": False,
        "broker": "localhost",
        "port": 1883,
        "topic_prefix": "metixel",
        "username": "",
        "password": "",
    },
    "input": {
        "cec_enabled": True,
        "ir_enabled": False,
        "ir_device": "/dev/lirc0",
    },
    "system": {
        "cache_dir": "cache/",
        "log_level": "INFO",
        "db_path": "cache/metixel.db",
    },
}


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------


class Config:
    """Thread-safe configuration container with atomic disk I/O."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = deepcopy(data) if data else deepcopy(DEFAULT_CONFIG)

    # -- Accessors -----------------------------------------------------------

    @property
    def display(self) -> dict[str, Any]:
        return self._data["display"]

    @property
    def slideshow(self) -> dict[str, Any]:
        return self._data["slideshow"]

    @property
    def image(self) -> dict[str, Any]:
        """Image optimisation settings with backward-compatible defaults.

        If the ``image`` section is missing from the config (e.g. an older
        config file), returns sensible defaults.
        """
        img = self._data.get("image", {})
        if not img:
            img = {
                "optimisation_enabled": True,
                "optimise_max_width": 0,
                "optimise_max_height": 0,
            }
            self._data["image"] = img
        return img

    @property
    def video(self) -> dict[str, Any]:
        """Video settings with backward-compatible defaults.

        If the ``video`` section is missing from the config (e.g. an older
        config file), falls back to legacy keys in the ``slideshow`` section
        for ``playback_enabled`` and ``max_duration_seconds``, then returns
        the full merged dict.
        """
        v = self._data.get("video", {})
        s = self._data.get("slideshow", {})

        if not v:
            # First access on an older config — synthesize from slideshow
            v = {
                "playback_enabled": s.get("video_playback_enabled", True),
                "player_backend": s.get("video_player_backend", "auto"),
                "max_duration_seconds": s.get("video_max_duration_seconds", 0),
                "transcoding_enabled": True,
                "transcode_max_width": 0,
                "transcode_max_height": 0,
                "transcode_quality": 23,
                "transcode_use_software_encoder": True,
                "transcode_timeout_seconds": 7200,
                "cpu_throttle_enabled": True,
                "cpu_throttle_percent": 50,
            }
            self._data["video"] = v

        return v

    @property
    def sync(self) -> dict[str, Any]:
        return self._data["sync"]

    @property
    def web(self) -> dict[str, Any]:
        return self._data["web"]

    @property
    def mqtt(self) -> dict[str, Any]:
        return self._data["mqtt"]

    @property
    def input(self) -> dict[str, Any]:
        return self._data["input"]

    @property
    def system(self) -> dict[str, Any]:
        return self._data["system"]

    def get(self, key: str, default: Any = None) -> Any:
        """Get a top-level config value."""
        return self._data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a deep copy of the full config dict."""
        return deepcopy(self._data)

    # -- Mutators ------------------------------------------------------------

    def update(self, section: str, values: dict[str, Any]) -> None:
        """Deep-merge values into a config section."""
        if section not in self._data:
            raise KeyError(f"Unknown config section: {section}")
        _deep_merge(self._data[section], values)
        logger.debug("Config section '%s' updated: %s", section, values)

    def replace(self, data: dict[str, Any]) -> None:
        """Replace the entire configuration atomically."""
        self._data = deepcopy(data)
        logger.debug("Config fully replaced")

    # -- Persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomically write configuration to disk.

        Writes to a temp file first, then uses ``os.replace()`` for
        atomicity — the frontend's inotify watcher will only see complete
        writes. Creates parent directories if needed.
        """
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        logger.debug("Saving config: tmp=%s, dst=%s", tmp_path, path)

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())

        file_size = tmp_path.stat().st_size
        os.replace(tmp_path, path)
        logger.info("Config saved atomically to %s (%d bytes)", path, file_size)

    @classmethod
    def load(cls, path: Path) -> Config:
        """Load configuration from disk, filling missing keys with defaults."""
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Merge loaded data over defaults so new keys are always present
            merged = deepcopy(DEFAULT_CONFIG)
            _deep_merge(merged, data)
            logger.info("Config loaded from %s", path)
            return cls(merged)
        else:
            logger.warning("Config not found at %s, using defaults", path)
            return cls()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, overlay: dict) -> None:
    """Recursively merge *overlay* into *base* in-place."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_config(path: Path) -> Config:
    """Convenience wrapper to load a Config from a path."""
    return Config.load(path)
