# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Image processor — EXIF parsing, resizing, downsampling, auto-rotation.

Processes high-resolution source images into screen-optimized cached versions.
Critical for memory-constrained Phase 1 hardware (Pi Zero 2 W: 512MB).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from metixel.shared.models import MediaItem, MediaType

logger = logging.getLogger(__name__)

# ── CPU limit detection (cached at import time) ──────────────────────────

_CPULIMIT_PATH: str | None = None


def _detect_cpulimit() -> str | None:
    """Locate the ``cpulimit`` binary.  Returns the path or None."""
    global _CPULIMIT_PATH
    if _CPULIMIT_PATH is None:
        import shutil
        _CPULIMIT_PATH = shutil.which("cpulimit")
        if _CPULIMIT_PATH:
            logger.debug("cpulimit found at %s — workers will be CPU-capped", _CPULIMIT_PATH)
        else:
            logger.debug("cpulimit not installed — workers use nice-only (no CPU cap)")
    return _CPULIMIT_PATH


def _wrap_worker_cmd(worker_cmd: list[str]) -> list[str]:
    """Wrap the worker command with cpulimit and/or nice.

    Priority order:
    1. ``cpulimit -l 50`` (hard 50 % CPU cap) if installed
    2. ``nice -n 19`` (lowest scheduling priority) always

    On a Pi 3 with 4 cores, ``cpulimit -l 50`` means the worker uses
    at most half of one core — the other 3.5 cores stay free for the
    frontend renderer and Flask web server.
    """
    cpulimit = _detect_cpulimit()
    if cpulimit:
        # cpulimit -l 50 -f -- nice -n 19 python3 ...
        # -f = foreground (wait for child to exit) — required on v3.1
        return [
            cpulimit, "-l", "50", "-f", "--",
            "nice", "-n", "19",
        ] + worker_cmd
    # cpulimit not available — nice only (scheduling hint, no hard cap)
    return ["nice", "-n", "19"] + worker_cmd


# ── ImageProcessor ───────────────────────────────────────────────────────


class ImageProcessor:
    """Processes images for display: resize, rotate, cache.

    Memory-conscious design:
    - Processes one image at a time
    - Releases PIL Image objects immediately after use
    - Saves EXIF data separately to avoid keeping it in memory
    - Target cache format: JPEG quality 85 (good balance of size/quality)

    Threshold gating:
    - Use :meth:`needs_optimisation` to check whether an image exceeds
      the optimisation threshold BEFORE calling :meth:`process`.
    - Images at or below the threshold don't need processing and can go
      directly to the slideshow playlist.
    """

    JPEG_QUALITY = 85
    THUMBNAIL_SIZE = 320

    def __init__(self, cache_dir: Path, screen_width: int = 1920, screen_height: int = 1080) -> None:
        self._cache_dir = cache_dir
        self._image_cache = cache_dir / "images"
        self._thumb_cache = cache_dir / "thumbnails"
        self._screen_w = screen_width
        self._screen_h = screen_height
        self._image_cache.mkdir(parents=True, exist_ok=True)
        self._thumb_cache.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def needs_optimisation(
        width: int, height: int,
        max_width: int = 0, max_height: int = 0,
    ) -> bool:
        """Check whether an image exceeds the optimisation threshold.

        Args:
            width: Image pixel width.
            height: Image pixel height.
            max_width: Threshold width (0 = use display width).
            max_height: Threshold height (0 = use display height).

        Returns:
            True if the image should be resized, False if it's already
            within limits.
        """
        if width <= 0 or height <= 0:
            return True  # Unknown dimensions — optimise to be safe
        if max_width > 0 and width > max_width:
            return True
        if max_height > 0 and height > max_height:
            return True
        return False

    def process(self, source_path: Path, source: str = "local") -> MediaItem | None:
        """Process a single image file.

        Heavy PIL operations (load, transpose, resize, save) are delegated
        to a subprocess via :mod:`metixel.backend.processing.worker` so that:

        * Memory is reclaimed by the OS when the subprocess exits
        * ``nice -n 19`` gives the subprocess lowest CPU priority
        * A crash/OOM in PIL kills only the worker, not the backend daemon

        Returns a MediaItem with paths to the cached version, or None on failure.
        Corrupt/unreadable images are automatically deleted.
        """
        import json
        import subprocess
        import sys

        try:
            # Compute content hash for cache key (in-process — fast)
            file_hash = self._hash_file(source_path)

            # Resolve cache paths
            cached_path = self._image_cache / f"{file_hash}.jpg"
            thumb_path = self._thumb_cache / f"{file_hash}.jpg"

            # ── Cache hit ────────────────────────────────────────────
            if cached_path.exists():
                if cached_path.stat().st_size < 1024 or not self._validate_cached_image(cached_path):
                    logger.warning(
                        "Cached image is corrupt — will re-process: %s",
                        cached_path.name,
                    )
                    with contextlib.suppress(OSError):
                        cached_path.unlink()
                else:
                    logger.debug("Image already cached: %s", file_hash)
                    if not thumb_path.exists():
                        self._regenerate_thumbnail(cached_path, thumb_path)
                    exif = self._read_exif(source_path)
                    w, h = self._get_cached_dimensions(cached_path)
                    return MediaItem(
                        id=file_hash,
                        original_path=source_path,
                        cached_path=cached_path,
                        media_type=MediaType.IMAGE,
                        width=w,
                        height=h,
                        thumbnail_path=thumb_path,
                        exif_data=exif,
                        source=source,
                    )

            # ── Cache miss — delegate to subprocess ──────────────────
            logger.debug("Spawning worker for: %s", source_path.name)
            screen = f"{self._screen_w}x{self._screen_h}"
            worker_cmd = [
                sys.executable, "-m", "metixel.backend.processing.worker",
                "--source", str(source_path),
                "--cache", str(cached_path),
                "--thumb", str(thumb_path),
                "--screen", screen,
            ]
            # Wrap with cpulimit (hard CPU cap) if installed, otherwise
            # fall back to nice-only (priority hint, no hard cap).
            cmd = _wrap_worker_cmd(worker_cmd)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )

            # Parse JSON output from the worker.
            # cpulimit prints "Process NNNN detected\n" to stdout
            # BEFORE the worker's output, so we take only the last
            # non-empty line (the worker's JSON).
            try:
                stdout_lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
                # The worker's JSON is the first line; cpulimit noise
                # ("Process NNNN detected") comes after it.
                json_line = stdout_lines[0] if stdout_lines else result.stdout
                data = json.loads(json_line)
            except (json.JSONDecodeError, IndexError):
                logger.error(
                    "Worker returned invalid JSON for %s (rc=%d, stderr=%r, stdout=%r)",
                    source_path.name, result.returncode,
                    result.stderr[:200] if result.stderr else "",
                    result.stdout[:200] if result.stdout else "",
                )
                return None

            status = data.get("status", "error")

            # ── Corrupt image — delete source ────────────────────────
            if status == "corrupt":
                logger.warning(
                    "Corrupt/unreadable image — deleting: %s",
                    source_path.name,
                )
                self._safe_delete(source_path)
                return None

            # ── Transient error ──────────────────────────────────────
            if status == "error":
                logger.warning(
                    "Worker failed for %s: %s (rc=%d)",
                    source_path.name,
                    data.get("message", "unknown"),
                    result.returncode,
                )
                return None

            # ── Success ──────────────────────────────────────────────
            logger.info("Image processed: %s → %s", source_path.name, file_hash)
            return MediaItem(
                id=file_hash,
                original_path=source_path,
                cached_path=cached_path,
                media_type=MediaType.IMAGE,
                width=data.get("width", 0),
                height=data.get("height", 0),
                thumbnail_path=thumb_path,
                exif_data=data.get("exif", {}),
                source=source,
            )

        except subprocess.TimeoutExpired:
            logger.error("Worker timed out after 120s: %s", source_path.name)
            return None
        except Exception:
            logger.exception("Failed to process image: %s", source_path)
            return None

    # -- Helpers -------------------------------------------------------------

    def _regenerate_thumbnail(self, cached_path: Path, thumb_path: Path) -> None:
        """Generate a thumbnail from an already-cached image.

        Called when the cached image exists but the thumbnail was deleted
        (e.g. after a cache clear).
        """
        try:
            with Image.open(cached_path) as img:
                thumb = img.copy()
                thumb.thumbnail((self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE), Image.LANCZOS)
                thumb.save(thumb_path, "JPEG", quality=70)
            logger.debug("Thumbnail regenerated: %s", thumb_path.name)
        except Exception:
            logger.warning(
                "Failed to regenerate thumbnail for %s", cached_path.name, exc_info=True,
            )

    @staticmethod
    def _validate_cached_image(path: Path) -> bool:
        """Check that a cached JPEG is readable (not corrupt/truncated).

        Uses PIL's ``.verify()`` which checks the file structure without
        fully decoding pixel data — fast enough for a startup check.
        """
        try:
            from PIL import Image as PILImage
            with PILImage.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False

    @staticmethod
    def _get_cached_dimensions(path: Path) -> tuple[int, int]:
        """Read image dimensions without decoding pixel data."""
        try:
            from PIL import Image as PILImage
            with PILImage.open(path) as img:
                return img.size
        except Exception:
            return (0, 0)

    @staticmethod
    def _read_exif(path: Path) -> dict[str, Any]:
        """Read EXIF from a file without loading the full image."""
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                if exif:
                    from PIL.ExifTags import TAGS

                    return {
                        TAGS.get(k, str(k)): str(v)
                        for k, v in exif.items()
                        if not isinstance(v, bytes)
                    }
        except Exception:
            pass
        return {}

    @staticmethod
    def _hash_file(path: Path) -> str:
        """Compute SHA-256 hash of a file (first 1MB for speed)."""
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            # Hash first 1MB + last 1KB for a fast-but-reliable fingerprint
            sha.update(f.read(1024 * 1024))
            f.seek(-1024, 2)  # Last 1KB
            sha.update(f.read(1024))
        return sha.hexdigest()[:16]

    @staticmethod
    def _safe_delete(path: Path) -> None:
        """Delete a file, logging any errors instead of raising.

        Used to clean up corrupt images so they don't block future scans.
        """
        try:
            if path.exists():
                path.unlink()
                logger.info("Deleted corrupt file: %s", path.name)
        except OSError as e:
            logger.warning("Could not delete corrupt file %s: %s", path.name, e)
