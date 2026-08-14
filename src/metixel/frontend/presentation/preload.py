# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media pipeline helper — CPU decode thread plus GPU upload for the next texture."""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from metixel.frontend.presentation.base import BaseEngineState
from metixel.shared.models import MediaItem, MediaType

logger = logging.getLogger(__name__)


class TexturePreloaderMixin(BaseEngineState):
    """Media pipeline helper — CPU decode thread plus GPU upload for the next texture."""

    def _load_texture_for_slot(self, slot: int, item: MediaItem) -> None:
        """Ensure ``_tex[slot]`` has a valid texture for *item*.

        For images: loads + downscales the JPEG.
        For videos: launches VLC via the non-blocking state machine.

        Loads the new texture BEFORE unloading the old one — if the load
        fails, the slot retains its previous texture rather than going
        black.  This is critical on memory-constrained hardware where
        GPU allocations can fail under fragmentation pressure.
        """
        if item.media_type == MediaType.VIDEO:
            self._video_launch(item)
            return
        new_tex = self._load_texture_for_item(item)
        if new_tex is not None:
            self._unload_texture(self._tex[slot])
            self._tex[slot] = new_tex
            self._tex_item[slot] = item

    def _load_texture_for_item(self, item: MediaItem) -> Any:
        """Load an image as a GPU texture (videos are handled elsewhere).

        If the file is an uncached original that is excessively large
        (> 3× screen pixels), the load is skipped to avoid OOM on
        memory-constrained hardware.  The backend will eventually
        provide a cached (resized) version via playlist hot-reload.
        """
        path_to_load = item.cached_path

        max_w = int(self._backend.width * 1.2)
        max_h = int(self._backend.height * 1.2)

        # ── Guard: skip huge uncached originals ──────────────────────
        if str(item.cached_path) == str(item.original_path):
            try:
                file_size_mb = path_to_load.stat().st_size / (1024 * 1024)
            except OSError:
                file_size_mb = 0.0
            # Files > 8 MB are likely high-res originals.  Loading them
            # into PIL can consume 100+ MB of RAM per image.  Defer to
            # the backend's cached (resized) version.
            if file_size_mb > 8.0:
                logger.debug(
                    "Skipping large uncached original (%.1f MB): %s — "
                    "waiting for backend cached version",
                    file_size_mb,
                    path_to_load,
                )
                return None

        try:
            from PIL import ImageFile

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            img: Image.Image = Image.open(path_to_load)
            img = ImageOps.exif_transpose(img)

            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ("RGB", "L") or img.mode == "L":
                img = img.convert("RGB")

            if img.width > max_w or img.height > max_h:
                orig_w, orig_h = img.width, img.height
                img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                logger.debug(
                    "Downscaled [%s]: %dx%d → %dx%d",
                    path_to_load,
                    orig_w,
                    orig_h,
                    img.width,
                    img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)  # noqa: SIM115
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)
            logger.debug("Texture loaded [%s]: tex=%s", path_to_load, id(texture))
            return texture
        except Exception:
            logger.exception("Failed to load texture: %s", path_to_load)
            return None

    def _cancel_preload(self) -> None:
        """Cancel any in-progress preload and discard its result."""
        if self._preload_thread is not None and self._preload_thread.is_alive():
            logger.debug("Cancelling stale preload")
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""

    def _preload_into_inactive(self) -> None:
        """Start preloading the next queue item into the inactive slot."""
        if not self._queue or self._current_idx < 0:
            return
        next_idx = (self._current_idx + 1) % len(self._queue)
        next_item = self._queue[next_idx]

        if self._preload_thread is not None and self._preload_thread.is_alive():
            return

        self._preload_thread = threading.Thread(
            target=self._preload_worker,
            args=(next_item,),
            daemon=True,
            name="tex-preload",
        )
        self._preload_thread.start()

    def _preload_worker(self, item: MediaItem) -> None:
        """CPU work: load + downscale → numpy array.  Main thread uploads.

        For images: loads the JPEG, downscales, stores as numpy.
        For videos: extracts/caches the first frame, loads it the same way.
        """
        try:
            from PIL import ImageFile

            ImageFile.LOAD_TRUNCATED_IMAGES = True

            if item.media_type == MediaType.VIDEO:
                path_to_load = self._preload_video_first_frame(item)
                if path_to_load is None:
                    with self._preload_lock:
                        self._preload_array = None
                        self._tex[self._inactive] = None
                    return
            else:
                path_to_load = item.cached_path

            # ── Guard: skip huge uncached originals ──────────────────
            if str(item.cached_path) == str(item.original_path):
                try:
                    file_size_mb = path_to_load.stat().st_size / (1024 * 1024)
                except OSError:
                    file_size_mb = 0.0
                if file_size_mb > 8.0:
                    logger.debug(
                        "Preload skipping large uncached original "
                        "(%.1f MB): %s — waiting for backend cache",
                        file_size_mb,
                        path_to_load,
                    )
                    with self._preload_lock:
                        self._preload_array = None
                    return

            img: Image.Image = Image.open(path_to_load)
            img = ImageOps.exif_transpose(img)

            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ("RGB", "L") or img.mode == "L":
                img = img.convert("RGB")

            max_w = int(self._backend.width * 1.2)
            max_h = int(self._backend.height * 1.2)
            if img.width > max_w or img.height > max_h:
                orig_w, orig_h = img.width, img.height
                img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                logger.debug(
                    "Preload downscaled [%s]: %dx%d → %dx%d",
                    path_to_load,
                    orig_w,
                    orig_h,
                    img.width,
                    img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            with self._preload_lock:
                self._preload_array = arr
                self._preload_cache_key = str(path_to_load)
            logger.debug("Preload ready [%s]", path_to_load)
        except Exception:
            logger.exception("Preload failed: %s", getattr(item, "cached_path", item.original_path))
            with self._preload_lock:
                self._preload_array = None

    def _preload_video_first_frame(self, item: MediaItem) -> Path | None:
        """Return the path to the pre-generated first frame JPEG.

        Frame extraction is a backend (Phase 2 OPTIMISE) responsibility.
        The frontend simply loads the cache file referenced by the
        ``MediaItem`` — no ffmpeg/ffprobe here.
        """
        video_path = str(item.cached_path or item.original_path)

        # Guard: if cached path != original and the file doesn't exist
        # (e.g. transcoding not yet complete), don't attempt to play it.
        from pathlib import Path as _Path

        _cached = _Path(video_path)
        if str(item.cached_path) != str(item.original_path) and (
            not _cached.is_file() or _cached.stat().st_size < 1024
        ):
            logger.warning(
                "Video cached file not ready — skipping: %s",
                video_path,
            )
            return None

        if item.first_frame_path is not None and item.first_frame_path.exists():
            return item.first_frame_path

        logger.warning(
            "No first frame cached for %s — backend should have pre-generated this during OPTIMISE",
            video_path,
        )
        return None

    def _upload_pending_preload(self) -> None:
        """Upload a finished preload numpy array to the inactive GPU slot."""
        arr: np.ndarray | None = None
        cache_key: str = ""
        with self._preload_lock:
            if self._preload_array is not None:
                arr = self._preload_array
                cache_key = self._preload_cache_key
                self._preload_array = None
                self._preload_cache_key = ""

        if arr is None:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)  # noqa: SIM115
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)

            self._unload_texture(self._tex[self._inactive])
            self._tex[self._inactive] = texture
            # Track which item this preload belongs to.
            next_idx = (self._current_idx + 1) % len(self._queue)
            self._tex_item[self._inactive] = self._queue[next_idx]
            logger.debug("Preload GPU upload OK: %s tex=%s", cache_key, id(texture))
        except Exception:
            logger.exception("Preload GPU upload failed: %s", cache_key)
            self._tex[self._inactive] = None
            self._tex_item[self._inactive] = None

    def _unload_texture(self, texture: Any) -> None:
        if texture is not None:
            self._backend.unload_texture(texture)
