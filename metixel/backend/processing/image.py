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

from PIL import Image, ImageOps, UnidentifiedImageError

from metixel.shared.models import MediaItem, MediaType

logger = logging.getLogger(__name__)


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

        Returns a MediaItem with paths to the cached version, or None on failure.
        Corrupt/unreadable images are automatically deleted so they don't block
        future scan cycles.
        """
        try:
            # Compute content hash for cache key
            file_hash = self._hash_file(source_path)

            # Check cache
            cached_path = self._image_cache / f"{file_hash}.jpg"
            thumb_path = self._thumb_cache / f"{file_hash}.jpg"

            if cached_path.exists():
                # Validate the cached file isn't corrupt (e.g. partial write
                # from a killed process).  Re-process if it's broken.
                if cached_path.stat().st_size < 1024 or not self._validate_cached_image(cached_path):
                    logger.warning(
                        "Cached image is corrupt — will re-process: %s",
                        cached_path.name,
                    )
                    with contextlib.suppress(OSError):
                        cached_path.unlink()
                else:
                    logger.debug("Image already cached: %s", file_hash)
                    # Regenerate thumbnail if missing (e.g. cache was cleared)
                    if not thumb_path.exists():
                        self._regenerate_thumbnail(cached_path, thumb_path)
                    exif = self._read_exif(source_path)
                    return self._build_item(source_path, cached_path, thumb_path, exif, source, file_hash)

            # Open and process
            with Image.open(source_path) as img:
                # Auto-rotate based on EXIF orientation
                img = ImageOps.exif_transpose(img)

                # Extract EXIF before converting
                exif = self._extract_exif(img)

                # Convert to RGB (handles RGBA, P, etc.)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                # Resize to screen resolution (maintain aspect ratio)
                img = self._resize_to_screen(img)

                # Save cached version
                img.save(cached_path, "JPEG", quality=self.JPEG_QUALITY)

                # Generate thumbnail
                thumb = img.copy()
                thumb.thumbnail((self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE), Image.LANCZOS)
                thumb.save(thumb_path, "JPEG", quality=70)

            logger.info("Image processed: %s → %s", source_path.name, file_hash)
            return self._build_item(source_path, cached_path, thumb_path, exif, source, file_hash)

        except UnidentifiedImageError:
            # File is corrupt, truncated, or not a valid image format.
            # Remove it so it doesn't keep failing on every scan cycle.
            logger.warning(
                "Corrupt/unreadable image — deleting: %s (%d bytes)",
                source_path.name, source_path.stat().st_size if source_path.exists() else 0,
            )
            self._safe_delete(source_path)
            return None
        except OSError as e:
            logger.warning(
                "Cannot read image file (permissions / I/O error): %s — %s",
                source_path.name, e,
            )
            return None
        except Exception:
            logger.exception("Failed to process image: %s", source_path)
            return None

    # -- Helpers -------------------------------------------------------------

    def _resize_to_screen(self, img: Image.Image) -> Image.Image:
        """Resize image to fit within screen dimensions, maintaining aspect ratio."""
        # Max dimension = screen diagonal to ensure quality for Ken Burns zoom
        max_w = int(self._screen_w * 1.2)  # 20% overscan for Ken Burns
        max_h = int(self._screen_h * 1.2)

        img.thumbnail((max_w, max_h), Image.LANCZOS)
        return img

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

    def _extract_exif(self, img: Image.Image) -> dict[str, Any]:
        """Extract relevant EXIF tags from a PIL Image."""
        exif_data: dict[str, Any] = {}
        try:
            exif = img.getexif()
            if exif:
                # Map common EXIF tags
                for tag_id, value in exif.items():
                    from PIL.ExifTags import TAGS

                    tag_name = TAGS.get(tag_id, str(tag_id))
                    # Skip binary data
                    if isinstance(value, bytes):
                        continue
                    exif_data[tag_name] = str(value)
        except Exception:
            pass
        return exif_data

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

    def _build_item(
        self,
        source: Path,
        cached: Path,
        thumb: Path,
        exif: dict[str, Any],
        source_name: str,
        file_hash: str,
    ) -> MediaItem:
        """Build a MediaItem from processed data."""
        # Get dimensions from cached file
        try:
            with Image.open(cached) as img:
                w, h = img.size
        except Exception:
            w, h = 0, 0

        return MediaItem(
            id=file_hash,
            original_path=source,
            cached_path=cached,
            media_type=MediaType.IMAGE,
            width=w,
            height=h,
            thumbnail_path=thumb,
            exif_data=exif,
            source=source_name,
        )
