# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""BaseEngineState — shared state contract for the presentation engine mixins."""

from __future__ import annotations

import subprocess
import threading
from typing import Any

import numpy as np

from metixel.display.backend import DisplayBackend
from metixel.frontend.presentation.layout import LayoutEngine
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType
from metixel.shared.paths import resolve_install_path


class BaseEngineState:
    """Declares every instance attribute the PresentationEngine sets
    in ``__init__`` so each mixin can be type-checked in isolation."""

    _config: Config
    _backend: DisplayBackend
    _layout: LayoutEngine
    _transitions: TransitionEngine
    _tex: list[Any | None]
    _tex_item: list[MediaItem | None]
    _active: int
    _queue: list[MediaItem]
    #: Unfiltered copy of the last backend playlist.  ``_queue`` is derived
    #: from this by the video guardrails, so a config toggle can re-apply the
    #: filter without rescanning the media folder.
    _all_items: list[MediaItem]
    _current_idx: int
    _paused: bool
    _item_start_time: float
    _queue_loaded: bool
    _preload_thread: threading.Thread | None
    #: Item the live preload thread is decoding (None when idle).
    _preload_target: MediaItem | None
    #: Cancellation token for the live preload thread — set to make the
    #: worker discard its result instead of storing it.
    _preload_cancel: threading.Event | None
    _preload_lock: threading.Lock
    _preload_array: np.ndarray | None
    _preload_cache_key: str
    _layout_cache: dict[tuple[str, int, int, str], dict]
    _fit_mode_cache: str
    _screen_ratio: float
    _transition_stall_logged: bool
    _video_state: int
    _video_proc: subprocess.Popen[bytes] | None
    _video_player: Any
    _video_launch_at: float
    _video_swap_at: float
    _video_item: MediaItem | None
    _video_path: str
    _video_vw: int
    _video_vh: int
    _video_duration: float
    _video_paused: bool
    _video_last_frame_loaded: bool
    _video_last_frame_tex: Any | None

    @property
    def _inactive(self) -> int:
        """Index of the texture slot NOT currently displayed."""
        return 1 - self._active

    @property
    def _cache_base(self) -> str:
        """Resolved cache directory from config (always absolute)."""
        cache_dir = self._config.system.get("cache_dir", "cache/")
        return str(resolve_install_path(cache_dir))

    @staticmethod
    def _video_playback_enabled(config: Config) -> bool:
        """Resolve the video playback master switch from *config*.

        The ``video.playback_enabled`` key takes precedence; the legacy
        ``slideshow.video_playback_enabled`` key is only consulted when
        the new key is absent.  Every code path that gates video playback
        (queue filtering, VLC launch, config reload) must use this helper
        so they can never disagree with each other.
        """
        video_cfg = config.video if hasattr(config, "video") else {}
        legacy = config.slideshow.get("video_playback_enabled", True)
        return bool(video_cfg.get("playback_enabled", legacy))

    @staticmethod
    def _preload_key_for(item: MediaItem) -> str:
        """The ``_preload_cache_key`` a finished preload of *item* carries.

        Images decode their cached file; videos decode the backend-generated
        first-frame JPEG.  The upload path compares this against the key the
        worker stored to make sure the decoded pixels belong to the item
        that is about to be tagged as "next".
        """
        if item.media_type == MediaType.VIDEO and item.first_frame_path is not None:
            return str(item.first_frame_path)
        return str(item.cached_path)

    # ------------------------------------------------------------------
    # Cross-mixin interface stubs — implemented by the concrete mixins.
    # Declared here so each mixin can be type-checked in isolation.
    # ------------------------------------------------------------------
    def _advance(self) -> None: ...
    def _cancel_preload(self) -> None: ...
    def _draw_frame_to_buffer(self, texture: Any, layout: dict) -> None: ...
    def _get_item_duration(self, item: MediaItem) -> float:
        raise NotImplementedError

    def _load_texture_for_item(self, item: MediaItem) -> Any: ...
    def _load_texture_for_slot(self, slot: int, item: MediaItem) -> None: ...
    def _preload_into_inactive(self) -> None: ...
    def _render_item(
        self,
        item: MediaItem,
        alpha: float,
        with_matte: bool = True,
        texture: Any = None,
        layout: dict | None = None,
    ) -> None: ...
    def _render_transition(
        self,
        current_item: MediaItem,
        progress: float,
        next_tex: Any,
    ) -> None: ...
    def _resolve_fit_mode(self, item: MediaItem) -> str:
        raise NotImplementedError

    def _unload_texture(self, texture: Any) -> None: ...
    def _upload_pending_preload(self) -> None: ...
    def _video_launch(self, item: MediaItem) -> None: ...
    def _video_stop(self) -> None: ...
    def _video_tick(self) -> None: ...
    def _write_current_media(self) -> None: ...
