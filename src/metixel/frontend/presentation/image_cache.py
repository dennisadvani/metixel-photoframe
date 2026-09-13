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
from dataclasses import dataclass
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

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max_entries = max_entries
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

        Eviction returns nothing to the caller: the old handle is simply dropped,
        and since every backend reference-counts (Qt implicitly, PIL by refcount)
        it is freed as soon as the canvas is finished with it.
        """
        if handle is None:
            return
        with self._lock:
            self._images[key] = handle
            self._images.move_to_end(key)
            while len(self._images) > self._max_entries:
                evicted, _ = self._images.popitem(last=False)
                logger.debug("Image cache evicted %s (limit %d)", evicted, self._max_entries)

    def clear(self) -> None:
        """Drop every cached handle (used on queue reset)."""
        with self._lock:
            self._images.clear()

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
            self._ready = payload

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
