# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Queue / playlist management for the presentation engine."""

from __future__ import annotations

import logging
import random
import time

from metixel.frontend.presentation.base import BaseEngineState
from metixel.frontend.presentation.video_state import _VIDEO_IDLE
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)


class PlaylistControllerMixin(BaseEngineState):
    """Queue / playlist management for the presentation engine."""

    def set_queue(self, items: list[MediaItem]) -> None:
        self._queue = list(items)

        # Stop any running video before replacing the queue.
        if self._video_state != _VIDEO_IDLE:
            self._video_stop()

        # ── Video guardrails ─────────────────────────────────────────
        # Read video config (new section; fall back to slideshow legacy keys)
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        skipped_playback: int = 0
        skipped_backend: int = 0
        skipped_transcode: int = 0
        skipped_duration: int = 0
        skipped_ready: int = 0

        for item in self._queue:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue

            # 0. Backend capability — software renderers (tkinter) can't
            #    play videos; skip them so they don't error every cycle.
            if not self._backend.supports_video:
                skipped_backend += 1
                continue

            # 1. Video playback master switch
            if not playback_enabled:
                skipped_playback += 1
                continue

            # 2. Max duration filter
            if max_duration > 0 and item.duration_seconds > max_duration:
                skipped_duration += 1
                continue

            # 3. Transcoding guardrails
            if transcoding_enabled:
                # Only play transcoded videos (or failed ones that
                # will be played as original)
                if not item.is_ready_to_play:
                    skipped_transcode += 1
                    continue
                # Also skip if the transcode status is FAILED but
                # transcoding is explicitly requested (user wants
                # optimised videos, not originals)
                if item.transcode_status == TranscodeStatus.FAILED:
                    logger.debug(
                        "Skipping %s — transcode failed and transcoding is required",
                        item.original_path.name,
                    )
                    skipped_transcode += 1
                    continue

            filtered.append(item)

        if skipped_playback:
            logger.info(
                "Video playback disabled — filtered %d videos",
                skipped_playback,
            )
        if skipped_backend:
            logger.info(
                "Display backend does not support video playback — filtered %d videos",
                skipped_backend,
            )
        if skipped_duration:
            logger.info(
                "Max video duration (%ds) — filtered %d videos",
                max_duration,
                skipped_duration,
            )
        if skipped_transcode:
            logger.info(
                "Videos not yet transcoded — filtered %d videos "
                "(transcoding is enabled; they will appear after processing)",
                skipped_transcode,
            )
        if skipped_ready:
            logger.info(
                "Videos not ready to play — filtered %d videos",
                skipped_ready,
            )

        self._queue = filtered

        if self._config.slideshow.get("shuffle", True):
            random.shuffle(self._queue)

        self._current_idx = 0 if self._queue else -1
        self._item_start_time = time.monotonic()

        for i in (0, 1):
            self._unload_texture(self._tex[i])
            self._tex[i] = None
            self._tex_item[i] = None
        self._active = 0
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""

        self._preload_into_inactive()
        self._write_current_media()
        self._queue_loaded = True
        logger.info("Media queue set: %d items", len(self._queue))

    def add_items(self, items: list[MediaItem]) -> int:
        """Add new items to the existing queue (deduplicating by id).

        Does NOT reset the current slideshow position — new items are
        appended to the end.  This is designed for hot-reload from the
        backend playlist without interrupting the currently displayed image.

        Also updates existing items' ``thumbnail_path`` if the backend
        provides one and the current item doesn't have one (e.g. items
        from the dev fallback scan lack thumbnails).

        Applies the same video guardrails as ``set_queue()``: respects
        ``video.playback_enabled``, ``video.transcoding_enabled``, and
        ``video.max_duration_seconds``.

        Returns the number of items actually added.
        """
        existing_ids = {item.id for item in self._queue}

        # Update existing items with richer backend data (e.g. thumbnail_path
        # and cached_path that the dev fallback scan couldn't provide).
        backend_by_id = {item.id: item for item in items}
        for existing in self._queue:
            backend_item = backend_by_id.get(existing.id)
            if backend_item is None:
                continue
            # Thumbnail from backend
            if existing.thumbnail_path is None and backend_item.thumbnail_path is not None:
                existing.thumbnail_path = backend_item.thumbnail_path
            # Optimised cache path from backend (avoids loading 4K originals)
            if str(existing.cached_path) == str(existing.original_path) and str(
                backend_item.cached_path
            ) != str(backend_item.original_path):
                existing.cached_path = backend_item.cached_path

        new_items = [item for item in items if item.id not in existing_ids]
        if not new_items:
            return 0

        # ── Video guardrails ─────────────────────────────────────────
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        for item in new_items:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue
            # Backend capability — software renderers can't play videos
            if not self._backend.supports_video:
                continue
            if not playback_enabled:
                continue
            if max_duration > 0 and item.duration_seconds > max_duration:
                continue
            if transcoding_enabled:
                if not item.is_ready_to_play:
                    continue
                if item.transcode_status == TranscodeStatus.FAILED:
                    continue
            filtered.append(item)

        if not filtered:
            return 0

        # Cancel any in-progress preload BEFORE modifying the queue.
        # New items (especially videos via shuffle) may land at the next
        # position, making the in-flight preload stale.
        self._cancel_preload()

        self._queue.extend(filtered)
        if self._config.slideshow.get("shuffle", True):
            for item in filtered:
                if self._current_idx >= 0 and len(self._queue) > self._current_idx + 1:
                    pos = random.randint(self._current_idx + 1, len(self._queue) - 1)
                else:
                    pos = len(self._queue) - 1
                self._queue.pop()
                self._queue.insert(pos, item)

        added = len(filtered)
        skipped = len(new_items) - added
        if skipped:
            logger.info(
                "Added %d new items (filtered %d by video guardrails) — total: %d, current idx: %d",
                added,
                skipped,
                len(self._queue),
                self._current_idx,
            )
        else:
            logger.info(
                "Added %d new items to queue (total: %d, current idx: %d)",
                added,
                len(self._queue),
                self._current_idx,
            )

        # Restart preload for the correct next item after queue change.
        self._preload_into_inactive()
        return added

    def remove_items(self, item_ids: set[str]) -> int:
        """Remove items from the queue by media item ID.

        Does NOT reset the slideshow position — the currently displayed
        item is preserved.  If the current item is removed, the slideshow
        advances to the next item.  Items pending in the preload thread
        are cancelled if they match a removed ID.

        Returns the number of items actually removed.
        """
        if not item_ids:
            return 0

        before = len(self._queue)
        removed_current = (
            any(self._queue[self._current_idx].id in item_ids for _ in [0])
            if 0 <= self._current_idx < len(self._queue)
            else False
        )

        self._queue = [item for item in self._queue if item.id not in item_ids]
        removed = before - len(self._queue)

        if removed == 0:
            return 0

        # If the current item was removed, advance or reset
        if removed_current:
            if self._queue:
                # Stay at the same index (which now points to the next item
                # that slid into this position) or wrap to 0.
                if self._current_idx >= len(self._queue):
                    self._current_idx = 0
            else:
                self._current_idx = -1
            # Reset the texture slots so we don't keep displaying the
            # removed item.
            for i in (0, 1):
                self._unload_texture(self._tex[i])
                self._tex[i] = None
                self._tex_item[i] = None
            self._active = 0
            self._item_start_time = time.monotonic()

        # Cancel in-flight preload if it matches a removed item
        with self._preload_lock:
            pk = self._preload_cache_key
            if pk:
                for rid in item_ids:
                    if rid in pk:
                        self._preload_array = None
                        self._preload_cache_key = ""
                        break

        # Restart preload for the correct next item
        self._preload_into_inactive()

        logger.info(
            "Removed %d items from queue (total: %d, current idx: %d)",
            removed,
            len(self._queue),
            self._current_idx,
        )
        return removed
