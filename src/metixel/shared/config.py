# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Configuration schema, validation, and defaults for Metixel Photoframe."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from metixel.shared.io import atomic_write_json
from metixel.shared.paths import data_dir

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
        "schedule_enabled": False,
        "schedule_on_time": "07:00",
        "schedule_off_time": "22:00",
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
        "player_backend": "auto",  # auto, vlc
        "max_duration_seconds": 0,  # 0 = unlimited
        "transcoding_enabled": True,
        "transcoding_profile": "",  # pi2, pi3, pi4, pi5, custom — empty = auto-detect
        "keep_audio": False,  # True = preserve audio track, False = strip audio
        "transcode_max_width": 0,  # 0 = use profile limit; overridden by profile/custom
        "transcode_max_height": 0,
        "transcode_quality": 23,  # CRF value (lower = better, 18-28 typical)
        "transcode_use_software_encoder": True,  # libx264; False = try hardware first
        "transcode_timeout_seconds": 7200,  # max time per transcode (2 hours)
        "cpu_throttle_enabled": True,
        "cpu_throttle_percent": 100,  # 0-100 or >100 for multi-core (100 = 1 core)
    },
    "sync": {
        "immich": {
            "enabled": False,
            "server_url": "https://immich.example.com",
            "api_key": "",
            "albums": [],  # [{"id": ..., "name": ...}] — multi-album sync
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
        # Optional web-dashboard password.  Empty string = auth disabled
        # (default, so first boot and existing installs are unaffected).
        # When set, the dashboard and all /api/* routes (except the exempt
        # set) require a login.  Stored as a salted hash, never plaintext.
        "password": "",
        # Optional on-screen UI PIN (future).  Empty string = disabled.
        # Independent of the web password and the device password.  Stored
        # as a salted hash.  Default 6 digits, accepted range 4-6.
        "screen_pin": "",
        # Random secret used to sign the Flask session cookie.  Generated
        # and persisted on first need so logins survive a backend restart.
        # Empty string = not yet generated.
        "auth_secret": "",
        # Web session idle timeout in minutes.  0 = no timeout (forever) —
        # the user's PC has its own OS/browser protections.  Default 30.
        "session_timeout_minutes": 30,
        # On-screen PIN unlock timeout in minutes.  Capped at 1440 (24h) —
        # anything higher defeats the PIN's purpose.  Default 60.
        "screen_pin_timeout_minutes": 60,
    },
    "mqtt": {
        "enabled": False,
        "broker": "localhost",
        "port": 1883,
        # Unique per frame; "" = hardware-unique id (Pi serial → MAC →
        # machine-id → hostname).  Scopes both the MQTT topics and the HA
        # device identity so multiple frames on one broker never collide.
        "device_id": "",
        "username": "",
        "password": "",
        "discovery_enabled": True,  # Home Assistant MQTT Discovery
        "discovery_prefix": "homeassistant",  # HA discovery base topic
    },
    "input": {
        # HDMI-CEC is opt-in: it needs the Debian python3-libcec bindings
        # (apt) and a CEC-capable TV.  Leave off unless you use a TV remote.
        "cec_enabled": False,
        "ir_enabled": False,
        "ir_device": "/dev/lirc0",
        "keyboard_enabled": True,
        "keyboard_map": {},  # {code: cmd} — populated by learn mode
    },
    "messages": {
        "enabled": True,
        "default_duration": 5.0,
        "max_visible": 5,
        "persistent": [],
    },
    "network": {
        "wifi_country": "",
        "ap_fallback_enabled": True,
        "ap_timeout_seconds": 60,
        "ap_grace_period_seconds": 300,
        "ap_max_duration_seconds": 600,
        "connection_check_url": "http://connectivity-check.ubuntu.com",
    },
    "system": {
        "cache_dir": "cache/",
        "log_level": "NONE",
        "quiet_boot": False,
        "first_run": True,
        "timezone": "",
        "ntp_enabled": True,
        "ntp_servers": [""],
        "db_path": "cache/metixel.db",
    },
    "update": {
        "channel": "stable",
        "auto_check": True,
        "auto_update": True,
        "auto_update_day": 0,
        "auto_update_time": "04:30",
        "check_interval_hours": 6,
        "github_repo": "dennisadvani/metixel-photoframe",
        "last_check": None,
        "last_update": None,
        "last_auto_update": None,
    },
    "timeouts": {
        # ── ffprobe / metadata ──────────────────────────────────────
        "ffprobe_probe": 120,  # ffprobe metadata probe (video.py _probe)
        "ffprobe_validate": 60,  # ffprobe cached-video validation
        "folder_watcher_probe": 120,  # folder watcher ffprobe metadata scan
        "hw_codec_detect": 30,  # ffmpeg HW codec detection
        # ── Extraction / processing ──────────────────────────────────
        "thumbnail_extract": 300,  # thumbnail frame extraction (ffmpeg)
        "frame_extract_first": 180,  # first-frame JPEG extraction
        "frame_extract_last": 120,  # last-frame JPEG extraction (decodes final 1s)
        "image_process": 120,  # image optimisation subprocess
        "transcode": 7200,  # video transcode (2 hours)
        # ── Playback ─────────────────────────────────────────────────
        "vlc_start": 30,  # VLC playback confirmation
    },
}


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------


class Config:
    """Thread-safe configuration container with atomic disk I/O."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = deepcopy(data) if data else deepcopy(DEFAULT_CONFIG)

    def _section(self, key: str) -> dict[str, Any]:
        """Return a top-level config section as a typed dict."""
        return cast(dict[str, Any], self._data[key])

    # -- Accessors -----------------------------------------------------------

    @property
    def display(self) -> dict[str, Any]:
        return self._section("display")

    @property
    def slideshow(self) -> dict[str, Any]:
        return self._section("slideshow")

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
        return cast(dict[str, Any], img)

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
            v = {
                "playback_enabled": s.get("video_playback_enabled", True),
                "player_backend": s.get("video_player_backend", "auto"),
                "max_duration_seconds": s.get("video_max_duration_seconds", 0),
                "transcoding_enabled": True,
                "transcoding_profile": "",
                "keep_audio": False,
                "transcode_max_width": 0,
                "transcode_max_height": 0,
                "transcode_quality": 23,
                "transcode_use_software_encoder": True,
                "transcode_timeout_seconds": 7200,
                "cpu_throttle_enabled": True,
                "cpu_throttle_percent": 100,
            }
            self._data["video"] = v

        return cast(dict[str, Any], v)

    @property
    def timeouts(self) -> dict[str, Any]:
        """Centralised timeout settings with defaults for every value.

        Returns the ``timeouts`` dict, filling in any missing keys from
        the global defaults so callers never get KeyError.
        """
        t = self._data.setdefault("timeouts", {})
        defaults = DEFAULT_CONFIG.get("timeouts", {})
        for key, val in defaults.items():
            t.setdefault(key, val)
        return cast(dict[str, Any], t)

    def timeout(self, key: str, fallback: int) -> int:
        """Read a single timeout value, falling back to *fallback* if
        the key is missing or the value is non-positive.
        """
        val = self.timeouts.get(key, fallback)
        try:
            ival = int(val)
            return ival if ival > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    def get_resolved_transcoding_profile(self) -> dict[str, Any]:
        """Return the effective transcoding profile with all values resolved.

        If ``transcoding_profile`` is set to ``custom``, returns the
        raw config values.  If set to a Pi model (or empty = auto-detect),
        merges the profile defaults with any overrides the user has set.
        """
        from metixel.backend.processing.video import _detect_pi_model

        v = self._data.get("video", {})
        profile_key = v.get("transcoding_profile", "") or None

        # Auto-detect on first run
        if profile_key is None or profile_key == "":
            profile_key = _detect_pi_model() or "pi3"
            # Persist the detected profile so the user can see it
            v["transcoding_profile"] = profile_key
            self._data["video"] = v

        if profile_key == "custom":
            # Return raw config values directly
            return {
                "profile": "custom",
                "label": "Custom",
                "codec": v.get("transcode_codec", "h264"),
                "encoder": "libx264" if v.get("transcode_codec", "h264") == "h264" else "libx265",
                "max_width": v.get("transcode_max_width", 1920),
                "max_height": v.get("transcode_max_height", 1080),
                "max_fps": v.get("transcode_max_fps", 30),
                "max_bitrate": v.get("transcode_max_bitrate", 20),
                "h264_profile": v.get("transcode_h264_profile", "high"),
                "h264_level": str(v.get("transcode_h264_level", "4.2")),
                "color_depth": v.get("transcode_color_depth", 8),
                "hdr_support": v.get("transcode_hdr_support", False),
            }

        # Use predefined profile from VideoProcessor
        from metixel.backend.processing.video import VideoProcessor

        prof = dict(VideoProcessor.PROFILES.get(profile_key, VideoProcessor.PROFILES["pi3"]))
        prof["profile"] = profile_key
        return prof

    @property
    def sync(self) -> dict[str, Any]:
        return self._section("sync")

    @property
    def web(self) -> dict[str, Any]:
        """Web server + auth settings with backward-compatible defaults.

        Fills in any missing keys (e.g. the optional ``password``,
        ``screen_pin``, ``auth_secret`` and timeout keys on an older config
        file) from the global defaults so callers never get KeyError.
        """
        w = self._data.setdefault("web", {})
        defaults = DEFAULT_CONFIG.get("web", {})
        for key, val in defaults.items():
            w.setdefault(key, val)
        return cast(dict[str, Any], w)

    @property
    def mqtt(self) -> dict[str, Any]:
        return self._section("mqtt")

    @property
    def input(self) -> dict[str, Any]:
        return self._section("input")

    @property
    def messages(self) -> dict[str, Any]:
        return self._section("messages")

    @property
    def network(self) -> dict[str, Any]:
        return self._section("network")

    @property
    def system(self) -> dict[str, Any]:
        return self._section("system")

    @property
    def updates(self) -> dict[str, Any]:
        """Update channel and auto-check settings with backward-compatible defaults.

        If the ``update`` section is missing from an older config file,
        synthesizes sensible defaults.
        """
        u = self._data.get("update", {})
        if not u:
            u = {
                "channel": "stable",
                "auto_check": True,
                "auto_update": True,
                "auto_update_day": 0,
                "auto_update_time": "04:30",
                "check_interval_hours": 6,
                "github_repo": "dennisadvani/metixel-photoframe",
                "last_check": None,
                "last_update": None,
                "last_auto_update": None,
            }
            self._data["update"] = u
        return cast(dict[str, Any], u)

    def _randomise_auto_update_schedule(self) -> None:
        """Pick a randomised weekly auto-update schedule on first boot.

        Chooses a random day of the week (0=Monday … 6=Sunday) and a random
        time within the 03:00–06:00 local window.  Called only when a fresh
        config is created so every device updates at a different moment,
        avoiding thundering-herd load on the GitHub API / release mirrors.

        Note: the 03:00–06:00 restriction applies ONLY to the first-boot
        randomisation.  Users may later pick any time/day in the Web UI.
        """
        import random

        day = random.randint(0, 6)
        # Random minute within 03:00–05:59 (the 3a–6a window).
        minute = random.randint(0, 179)
        hour, minute_of_hour = divmod(minute, 60)
        time_str = f"{3 + hour:02d}:{minute_of_hour:02d}"
        u = self._data.setdefault("update", {})
        u["auto_update_day"] = day
        u["auto_update_time"] = time_str
        logger.info("Randomised auto-update schedule: day=%d time=%s", day, time_str)

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

        Uses :func:`metixel.shared.io.atomic_write_json` (temp file +
        ``os.replace``) so the frontend's inotify watcher only sees
        complete writes. Creates parent directories if needed.
        """
        atomic_write_json(path, self._data, indent=2)
        file_size = Path(path).stat().st_size
        logger.info("Config saved atomically to %s (%d bytes)", path, file_size)

    @classmethod
    def load(cls, path: Path) -> Config:
        """Load configuration from disk, filling missing keys with defaults.

        If the config file does not exist, a default configuration is
        created AND immediately saved to *path* so that other subsystems
        (e.g. logging setup) can read ``system.log_level`` from it on
        the very first start.
        """
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Merge loaded data over defaults so new keys are always present
            merged = deepcopy(DEFAULT_CONFIG)
            _deep_merge(merged, data)
            logger.info("Config loaded from %s", path)
            return cls(merged)
        else:
            logger.info(
                "Config not found at %s — creating with defaults",
                path,
            )
            config = cls()
            config._randomise_auto_update_schedule()
            config.save(path)
            return config


# ---------------------------------------------------------------------------
# Shared utility: resolve watch_paths to a list of Path objects
# ---------------------------------------------------------------------------


def resolve_watch_paths(
    config: Config,
    base_dir: Path | str | None = None,
) -> list[Path]:
    """Resolve ``sync.local.watch_paths`` to a list of enabled :class:`Path` objects.

    Handles both the new object format (``[{"path": "...", "enabled": true}]``)
    and the legacy flat-list format (``["media/", ...]``).

    Args:
        config: The application :class:`Config`.
        base_dir: Directory that relative paths are resolved against.
                  Defaults to the persistent data directory (``/opt/metixel/data``
                  on Linux, ``Path.cwd()`` otherwise).
    """
    base_dir = data_dir() if base_dir is None else Path(base_dir)

    raw = config.sync.get("local", {}).get("watch_paths", [])
    paths: list[Path] = []
    for entry in raw:
        if isinstance(entry, dict):
            if entry.get("enabled", True):
                p = Path(entry["path"])
                if not p.is_absolute():
                    p = base_dir / p
                paths.append(p)
        elif isinstance(entry, str):
            # Legacy flat-list format — treat as enabled
            p = Path(entry)
            if not p.is_absolute():
                p = base_dir / p
            paths.append(p)
    return paths


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
