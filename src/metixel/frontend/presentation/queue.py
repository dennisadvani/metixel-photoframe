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
        # Keep the unfiltered playlist so a later config change (e.g. the
        # video playback toggle) can re-derive the queue from it instead of
        # rescanning the media folder.  ``list()`` matters: ``items`` may be
        # ``self._all_items`` itself (see ``reload_config``).
        self._all_items = list(items)
        self._queue = list(items)

        # Stop any running video before replacing the queue.
        if self._video_state != _VIDEO_IDLE:
            self._video_stop()

        # ── Video guardrails ─────────────────────────────────────────
        # Read video config (new section; fall back to slideshow legacy keys)
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = self._video_playback_enabled(self._config)
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )
        # Portrait (90/270°) — the current VLC subprocess player cannot
        # display videos on a rotated output (its X11 window pops blank
        # over the poster before the first frame).  This is a backend
        # guard so portrait mode can never have video playback available
        # regardless of how `playback_enabled` was set (config, API, file).
        rotation = int(self._config.display.get("rotation", 0) or 0) % 360
        rotation_blocks_video = rotation in (90, 270)

        filtered: list[MediaItem] = []
        skipped_playback: int = 0
        skipped_backend: int = 0
        skipped_transcode: int = 0
        skipped_duration: int = 0
        skipped_ready: int = 0
        skipped_rotation: int = 0

        for item in self._queue:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue

            # 0. Portrait rotation — videos cannot play at 90/270°.
            if rotation_blocks_video:
                skipped_rotation += 1
                continue

            # 1. Backend capability — software renderers (tkinter) can't
            #    play videos; skip them so they don't error every cycle.
            if not self._backend.supports_video:
                skipped_backend += 1
                continue

            # 2. Video playback master switch
            if not playback_enabled:
                skipped_playback += 1
                continue

            # 3. Max duration filter
            if max_duration > 0 and item.duration_seconds > max_duration:
                skipped_duration += 1
                continue

            # 4. Transcoding guardrails
            if transcoding_enabled:
                # Only play videos the backend has marked ready (status
                # set, playable status, first/last frame caches present).
                if not item.is_ready_to_play:
                    skipped_ready += 1
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

        if skipped_rotation:
            logger.info(
                "Video playback unavailable at %d° rotation — filtered %d videos "
                "(portrait: current player cannot display rotated video)",
                rotation,
                skipped_rotation,
            )
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
        if skipped_ready:
            logger.info(
                "Videos not ready to play — filtered %d videos "
                "(transcoding is enabled; they will appear after processing)",
                skipped_ready,
            )
        if skipped_transcode:
            logger.info(
                "Videos whose transcode failed — filtered %d videos "
                "(transcoding is required, originals are not played)",
                skipped_transcode,
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

        # Record every new backend item in the unfiltered playlist, even
        # those the guardrails below reject — a later playback toggle
        # re-filters from this list (see ``reload_config``).
        known_ids = {item.id for item in self._all_items}
        self._all_items.extend(item for item in new_items if item.id not in known_ids)

        # ── Video guardrails ─────────────────────────────────────────
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = self._video_playback_enabled(self._config)
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )
        # Portrait (90/270°) — block videos (see set_queue for rationale).
        rotation = int(self._config.display.get("rotation", 0) or 0) % 360
        rotation_blocks_video = rotation in (90, 270)

        filtered: list[MediaItem] = []
        for item in new_items:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue
            # Portrait rotation — block videos entirely
            if rotation_blocks_video:
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

        # The unfiltered playlist mirrors the backend's — prune it too so a
        # later re-filter (playback toggle) cannot resurrect deleted items.
        self._all_items = [item for item in self._all_items if item.id not in item_ids]

        before = len(self._queue)
        current_id: str | None = None
        if 0 <= self._current_idx < len(self._queue):
            current_id = self._queue[self._current_idx].id
        removed_current = current_id is not None and current_id in item_ids

        self._queue = [item for item in self._queue if item.id not in item_ids]
        removed = before - len(self._queue)

        if removed == 0:
            return 0

        # If the video being played right now was removed, kill VLC before
        # the texture slots are torn down — otherwise the render loop keeps
        # ticking the old state machine while a new item is loaded under
        # it, and a second VLC can be launched on top of the first.
        if (
            self._video_state != _VIDEO_IDLE
            and self._video_item is not None
            and self._video_item.id in item_ids
        ):
            logger.info("remove_items: stopping video that was removed from the playlist")
            self._video_stop()

        if removed_current:
            # The current item was removed: advance or reset.
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
        else:
            # The current item survived — re-point ``_current_idx`` at its
            # new position.  Removing items that sat BEFORE it shifts it
            # left; without this the index silently lands on a later item,
            # skipping a slide and reporting the wrong file.
            for idx, item in enumerate(self._queue):
                if item.id == current_id:
                    self._current_idx = idx
                    break
            # Drop a preloaded texture that belongs to a removed item so
            # the next advance cannot promote it under another item's name.
            inactive_item = self._tex_item[self._inactive]
            if inactive_item is not None and inactive_item.id in item_ids:
                self._unload_texture(self._tex[self._inactive])
                self._tex[self._inactive] = None
                self._tex_item[self._inactive] = None

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

        # Republish the current-media state file.  Without this the file kept
        # the pre-removal contents (including a stale index), so the dashboard
        # showed the wrong item — or "No media playing" — indefinitely, since
        # nothing else rewrites it until the next advance.
        self._write_current_media()

        logger.info(
            "Removed %d items from queue (total: %d, current idx: %d)",
            removed,
            len(self._queue),
            self._current_idx,
        )
        return removed
