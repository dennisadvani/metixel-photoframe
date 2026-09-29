# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Image cache — bounded decode-ahead for the presenter.

Replaces the old two-GPU-slot ping-pong.  That design existed to hide decode
latency behind a *GPU texture* upload while the active slot was displayed; with a
retained-mode canvas the meaningful unit of memory is a **decoded image**, and the
useful thing to pre-load is the *next* item rather than a second GPU texture.

Two properties matter for a frame that runs for weeks on a Pi 3:

* **Bounded.** At most :data:`MAX_ENTRIES` decoded images are retained (current,
  next, and one in flight).  Qt caches pixmaps aggressively, so an unbounded
  cache is a slow OOM rather than an immediate failure — the failure mode is a
  frame that dies after three days, which is the worst kind to debug remotely.

Bounding this cache is only half the job, and the missing half is not obvious: the
handle is fetched from the *backend* by URL, so dropping it here frees nothing
unless the backend is told.  A backend that keeps the decoded image for the scene
to pull (the QML one stores it in an image provider) holds a strong reference this
cache cannot see.  On the frame, that combination leaked exactly one image per
slide — ~1 GB in ten minutes, then ``Out of memory: Killed process <frontend>``
and a systemd restart, over and over.  Hence :attr:`ImageCache.release`, with each
backend capping its own store as the backstop for handles nothing can release.
* **Thread-safe.** Decoding happens on a worker thread because a large JPEG can
  take hundreds of milliseconds, and doing it on the GUI thread would stall the
  event loop — which, with the OTA health gate, now has consequences beyond a
  stutter (a long enough stall reads as a dead frontend).

The worker produces a plain ``bytes`` payload; the caller converts it to a
backend-native handle on the GUI thread.  That split is deliberate: Qt objects
must not be constructed off the GUI thread, whereas Pillow is happy there.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Maximum decoded images retained.  Current + next + one in flight, with a
#: little slack for a resize in progress.  Not a tuning knob: raising it trades a
#: small latency win for a large, unbounded memory risk on a 1GB Pi.
MAX_ENTRIES = 3

#: An uncached original larger than this is skipped rather than decoded, so a
#: 60MP photo cannot exhaust RAM before the backend has produced a cached copy.
#: The backend resizes during Phase 2 (OPTIMISE) and the playlist hot-reload
#: picks the cached version up on the next poll.
MAX_UNCACHED_MB = 8.0


@dataclass(frozen=True)
class DecodedImage:
    """A decoded image payload awaiting upload on the GUI thread."""

    key: str
    """Cache key — the media id, so an item is never decoded twice."""

    data: bytes
    """Encoded payload (the on-disk file, or a re-encoded downscale)."""

    width: int
    height: int


class ImageCache:
    """Decode-ahead cache with a single background worker.

    One worker, not a pool: decoding is serialised by the presenter anyway (it
    only ever needs the *next* item), and a pool on a Pi 3 competes with the
    video decoder for the same cores.
    """

    def __init__(
        self,
        max_entries: int = MAX_ENTRIES,
        release: Callable[[Any], None] | None = None,
    ) -> None:
        self._max_entries = max_entries
        self._release = release
        self._images: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: tuple[str, Path, int, int] | None = None
        self._ready: DecodedImage | None = None

    # -- Cache ---------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """Return a cached handle for *key*, marking it most-recently-used."""
        with self._lock:
            if key in self._images:
                self._images.move_to_end(key)
                return self._images[key]
            return None

    def put(self, key: str, handle: Any) -> None:
        """Store *handle*, evicting the least-recently-used entry if needed.

        A key that is ALREADY cached keeps the handle it has.  That is not a
        micro-optimisation: the canvas identifies a layer's ambient backdrop by
        the IDENTITY of the artwork handle it was built for, so silently swapping
        in a second decoded copy of the same picture orphans that backdrop and
        the slide paints the flat ambient fill instead.  Both payloads are the
        same image, so there is nothing to gain by preferring the newer one.

        Evicted handles are handed to :attr:`release` rather than merely dropped.
        Dropping is not enough: the backend owns the decoded image, and a backend
        that serves it to the scene by URL keeps it until it is told otherwise.
        """
        if handle is None:
            return
        evicted: list[Any] = []
        with self._lock:
            if key in self._images:
                # Refresh its place in the LRU order, then leave it in place.
                self._images.move_to_end(key)
                return
            self._images[key] = handle
            self._images.move_to_end(key)
            while len(self._images) > self._max_entries:
                dropped_key, dropped_handle = self._images.popitem(last=False)
                evicted.append(dropped_handle)
                logger.debug("Image cache evicted %s (limit %d)", dropped_key, self._max_entries)
        self._release_all(evicted)

    def clear(self) -> None:
        """Drop every cached handle (used on queue reset)."""
        with self._lock:
            handles = list(self._images.values())
            self._images.clear()
        self._release_all(handles)

    def _release_all(self, handles: list[Any]) -> None:
        """Hand *handles* back to the backend, with the cache lock released.

        Called after the lock is dropped because ``release`` is the backend's own
        method, and evicting while holding the lock would let a slow backend stall
        the decode worker for no reason.

        A failing release is logged and swallowed.  The worst case of losing one is
        a leaked image; the certainty of raising here is an aborted slide change,
        and a slideshow that stops is worse than one that leaks slowly.
        """
        if self._release is None:
            return
        for handle in handles:
            try:
                self._release(handle)
            except Exception:
                logger.debug("Failed to release an image handle", exc_info=True)

    @property
    def size(self) -> int:
        """Number of retained images — asserted in tests to catch unbounded growth."""
        with self._lock:
            return len(self._images)

    # -- Preload worker ------------------------------------------------------

    def start_preload(self, key: str, path: Path, max_w: int, max_h: int) -> None:
        """Begin decoding *path* in the background.

        A request for the same key as the in-flight job is a no-op, so the
        presenter can call this every frame without spawning a thread per frame.
        """
        with self._lock:
            if self._ready is not None and self._ready.key == key:
                return
            if self._pending is not None and self._pending[0] == key:
                return
            if self._thread is not None and self._thread.is_alive():
                # One job at a time: a newer request supersedes it, and the
                # stale result is discarded on collection because the key no
                # longer matches.
                pass
            self._pending = (key, path, max_w, max_h)

        self._thread = threading.Thread(
            target=self._decode_worker,
            args=(key, path, max_w, max_h),
            name="image-preload",
            daemon=True,
        )
        self._thread.start()

    def take_ready(self) -> DecodedImage | None:
        """Return a finished decode, if any, and clear it.

        Called from the GUI thread; the payload is raw bytes so no Qt object
        crosses a thread boundary.
        """
        with self._lock:
            ready = self._ready
            self._ready = None
            return ready

    def _decode_worker(self, key: str, path: Path, max_w: int, max_h: int) -> None:
        """Decode and downscale *path*, then publish the result.

        Never raises: a failed decode means the presenter advances, and a
        slideshow must not stop because one photo is corrupt.
        """
        try:
            payload = self._decode(path, max_w, max_h)
        except Exception:
            logger.debug("Preload decode failed for %s", path, exc_info=True)
            payload = None

        if payload is None:
            return

        with self._lock:
            # Discard if superseded while we were decoding.
            if self._pending is None or self._pending[0] != key:
                logger.debug("Discarding superseded preload for %s", key)
                return
            self._pending = None
            # Re-key to the CACHE key the caller asked for (the media id), NOT the
            # source path that ``_decode`` puts on the payload.  Everything
            # downstream looks an image up by id — ``ImageCache.get`` in the
            # presenter, the ambient backdrop's handle lookup — so a payload keyed
            # by path is unretrievable and the whole decode-ahead is wasted.
            self._ready = replace(payload, key=key)

    @staticmethod
    def _decode(path: Path, max_w: int, max_h: int) -> DecodedImage | None:
        """Decode, EXIF-rotate, flatten alpha and downscale to the screen size."""
        import io

        from PIL import Image, ImageFile, ImageOps

        # A truncated download should still display rather than abort the show.
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        try:
            file_size_mb = path.stat().st_size / (1024 * 1024)
        except OSError:
            file_size_mb = 0.0
        if file_size_mb > MAX_UNCACHED_MB and path.parent.name != "cache":
            logger.debug(
                "Skipping large uncached original (%.1f MB): %s — awaiting the "
                "backend's resized copy",
                file_size_mb,
                path,
            )
            return None

        with Image.open(path) as opened:
            img = ImageOps.exif_transpose(opened)
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            if max_w > 0 and max_h > 0 and (img.width > max_w or img.height > max_h):
                img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            return DecodedImage(
                key=str(path),
                data=buf.getvalue(),
                width=img.width,
                height=img.height,
            )
