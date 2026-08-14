# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""BaseEngineState — shared state contract for the presentation engine mixins."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import numpy as np

from metixel.display.backend import DisplayBackend
from metixel.frontend.presentation.layout import LayoutEngine
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config
from metixel.shared.models import MediaItem


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
    _current_idx: int
    _paused: bool
    _item_start_time: float
    _queue_loaded: bool
    _preload_thread: threading.Thread | None
    _preload_lock: threading.Lock
    _preload_array: np.ndarray | None
    _preload_cache_key: str
    _layout_cache: dict[tuple[int, str], dict]
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
        path = Path(cache_dir)
        if not path.is_absolute():
            path = Path("/opt/metixel") / path
        return str(path)

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
