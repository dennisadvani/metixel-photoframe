# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Standalone thumbnail generation — usable from FolderWatcher (Phase 1).

Generates thumbnails for images and videos **regardless of whether the
media needs optimisation**.  Thumbnails are cached in
``<cache_dir>/thumbnails/`` and regenerated only when missing or corrupt.

This module is intentionally separate from ``ImageProcessor`` and
``VideoProcessor`` so that thumbnails can be generated early — during
the folder-watch metadata phase — before the optimisation queue decides
whether to resize or transcode.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from metixel.backend.processing.utils import ensure_heif_support, nice_cmd

logger = logging.getLogger(__name__)

# Register the optional HEIF decoder so HEIC originals (often mislabelled
# .jpg via Immich sync) can be thumbnailed.
ensure_heif_support()

THUMBNAIL_SIZE = 320

# ── helpers ───────────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    """Compute a short content hash (first 1 MB + last 1 KB)."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        sha.update(f.read(1024 * 1024))
        f.seek(-1024, 2)
        sha.update(f.read(1024))
    return sha.hexdigest()[:16]


def _validate_thumbnail(path: Path) -> bool:
    """Check that a cached thumbnail JPEG is readable (not corrupt/truncated)."""
    try:
        if path.stat().st_size < 512:
            return False
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _resolve_thumb_dir(cache_dir: str | Path) -> Path:
    """Resolve and create the thumbnails cache directory."""
    cd = Path(cache_dir)
    if not cd.is_absolute():
        cd = Path("/opt/metixel") / cd
    thumb_dir = cd / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    return thumb_dir


# ── public API ────────────────────────────────────────────────────────


def resolve_thumb_cache_dir(config_cache_dir: str | Path) -> Path:
    """Resolve the thumbnails directory from the configured cache dir.

    This is a convenience for callers that already have the config value
    and just need the :class:`Path`.
    """
    return _resolve_thumb_dir(config_cache_dir)


def generate_image_thumbnail(
    source_path: Path,
    cache_dir: str | Path = "cache",
) -> Path | None:
    """Generate a 320 px thumbnail for an image file.

    Args:
        source_path: Path to the source image.
        cache_dir: Cache root directory (default ``"cache"``).

    Returns:
        Path to the cached ``<hash>.jpg`` thumbnail, or ``None`` on failure.
        Skips generation when a valid thumbnail already exists.
    """
    try:
        file_hash = _hash_file(source_path)
        thumb_dir = _resolve_thumb_dir(cache_dir)
        thumb_path = thumb_dir / f"{file_hash}.jpg"

        # Reuse valid cached thumbnail
        if thumb_path.exists() and _validate_thumbnail(thumb_path):
            return thumb_path

        # Remove stale/corrupt thumbnail before regenerating
        if thumb_path.exists():
            with contextlib.suppress(OSError):
                thumb_path.unlink()

        with Image.open(source_path) as handle:
            img: Image.Image = handle
            # Composite transparent images onto black before converting
            # to RGB — otherwise transparent areas render as white.
            if img.mode in ("RGBA", "PA"):
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, img)
                img = bg
            elif img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            thumb = img.copy()
            thumb.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
            thumb.save(thumb_path, "JPEG", quality=70)

        logger.debug("Image thumbnail generated: %s → %s", source_path.name, thumb_path.name)
        return thumb_path

    except UnidentifiedImageError:
        logger.warning(
            "Corrupt/unreadable image — cannot generate thumbnail: %s",
            source_path.name,
        )
        return None
    except Exception:
        logger.warning(
            "Failed to generate image thumbnail: %s",
            source_path.name,
            exc_info=True,
        )
        return None


def generate_video_thumbnail(
    source_path: Path,
    cache_dir: str | Path = "cache",
) -> Path | None:
    """Extract a thumbnail frame at 2 seconds into a video.

    Uses fast keyframe seeking (``-ss`` before ``-i`` with
    ``-noaccurate_seek``).  A generous 300 s timeout accommodates
    heavy 4K sources on a Pi 2/3 where software decode of a single
    frame can take > 120 seconds under CPU contention.

    Args:
        source_path: Path to the source video.
        cache_dir: Cache root directory (default ``"cache"``).

    Returns:
        Path to the cached ``<hash>.jpg`` thumbnail, or ``None`` on failure.
        Skips generation when a valid thumbnail already exists.
    """
    try:
        file_hash = _hash_file(source_path)
        thumb_dir = _resolve_thumb_dir(cache_dir)
        thumb_path = thumb_dir / f"{file_hash}.jpg"

        # Reuse valid cached thumbnail
        if thumb_path.exists() and _validate_thumbnail(thumb_path):
            return thumb_path

        # Remove stale/corrupt thumbnail before regenerating
        if thumb_path.exists():
            with contextlib.suppress(OSError):
                thumb_path.unlink()

        cmd = nice_cmd(
            [
                "ffmpeg",
                "-y",
                "-noaccurate_seek",
                "-ss",
                "2",
                "-i",
                str(source_path),
                "-vframes",
                "1",
                "-q:v",
                "2",
                str(thumb_path),
            ]
        )
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )

        # Validate the generated thumbnail
        if thumb_path.exists() and _validate_thumbnail(thumb_path):
            logger.debug("Video thumbnail generated: %s → %s", source_path.name, thumb_path.name)
            return thumb_path

        # Clean up invalid output (ffmpeg created a file but it's corrupt)
        logger.warning("Generated thumbnail is invalid — discarding: %s", thumb_path.name)
        with contextlib.suppress(OSError):
            thumb_path.unlink()
        return None

    except subprocess.TimeoutExpired:
        logger.warning(
            "Thumbnail generation timed out (300 s) for: %s",
            source_path.name,
        )
        return None
    except subprocess.CalledProcessError:
        logger.warning(
            "ffmpeg failed to generate thumbnail for: %s",
            source_path.name,
        )
        return None
    except Exception:
        logger.warning(
            "Failed to generate video thumbnail: %s",
            source_path.name,
            exc_info=True,
        )
        return None
