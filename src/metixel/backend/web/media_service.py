# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media service — filesystem logic behind the media API routes.

Extracts the pure (Flask-free) media helpers out of ``routes/media.py``:
path/cache resolution, image/video probing, thumbnail lookup, upload
sanitisation/dedup, and cache-clearing.  Keeps the route module thin and
makes this logic independently testable.

The module-level :data:`file_list_cache` is the pagination cache shared
with ``routes/media.py`` — invalidated by :func:`clear_cache`.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import threading
from pathlib import Path

from metixel.shared.media import content_hash
from metixel.shared.paths import resolve_install_path

logger = logging.getLogger(__name__)

# Upload target subfolder under media_dir (an enabled watch path).
UPLOAD_SUBDIR = "my_media"
# Reject uploads that would leave less than this fraction of disk free.
FREE_SPACE_BUFFER_FRACTION = 0.05
# Hard safety cap — multipart data is spooled before the view runs, so a
# pathological request must not be allowed to fill tmpfs (RAM) unbounded.
MAX_UPLOAD_BYTES = 2 * 1024**3  # 2 GiB

# ── Lightweight file-list cache ──────────────────────────────────────
# Avoids re-scanning the filesystem on every paginated request.
# Invalidated after _CACHE_TTL seconds or by clear_cache().
# Guarded by _file_list_lock — shared across web request threads (read/
# write in the /list route) and the cache-clear handler.
_CACHE_TTL = 60.0
file_list_cache: dict[str, tuple[float, list[Path], int, int]] = {}
_file_list_lock = threading.Lock()
# key = str(media_folder) → (timestamp, paths, img_count, vid_count)


def resolve_cache_dir(state) -> Path:
    """Resolve the cache directory from config."""
    config = state.config
    cache_dir = Path(config.system.get("cache_dir", "cache/"))
    return resolve_install_path(cache_dir)


def probe_image(path: Path) -> tuple[int, int]:
    """Get image dimensions without a full decode."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


def probe_video(path: Path) -> tuple[int, int]:
    """Get video dimensions via ffprobe (JSON format — field-order safe)."""
    try:
        import json
        import subprocess

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            if streams:
                s = streams[0]
                return (s.get("width", 0) or 0, s.get("height", 0) or 0)
    except Exception:
        pass
    return (0, 0)


def lookup_thumbnail(path: Path, thumb_dir: Path) -> str | None:
    """Check if a cached thumbnail exists and return its URL."""
    try:
        file_hash = content_hash(path)
        thumb_path = thumb_dir / f"{file_hash}.jpg"
        if thumb_path.exists():
            return f"/api/media/thumbnail/{file_hash}.jpg"
    except OSError:
        pass
    return None


def relative_to_any(file_path: Path, roots: list[Path]) -> str:
    """Return ``file_path`` relative to the first matching root, or its name."""
    for root in roots:
        try:
            return str(file_path.relative_to(root))
        except ValueError:
            continue
    return file_path.name


def watch_folder_name(file_path: Path, roots: list[Path]) -> str:
    """Return the name of the watch folder that contains ``file_path``.

    Falls back to the parent directory name if no watch root matches.
    """
    resolved = file_path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return root.name
        except ValueError:
            continue
    # Not inside any watch root — use immediate parent directory name
    return file_path.parent.name


def resolve_upload_dir(state) -> Path:
    """Return the user-upload folder (``media/my_media``), creating it if needed.

    ``my_media`` is an enabled watch path in the default config, so files
    written here are picked up by the FolderWatcher and flow through the
    optimisation pipeline into the slideshow.
    """
    config = state.config
    media_dir = Path(config.system.get("media_dir", "media/"))
    media_dir = resolve_install_path(media_dir)
    upload_dir = media_dir / UPLOAD_SUBDIR
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def sanitize_filename(name: str) -> str:
    """Strip path components and characters that are unsafe in a filesystem."""
    name = Path(name or "").name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name or "upload"


def unique_path(directory: Path, name: str) -> Path:
    """Return a non-colliding path, appending ``-1``, ``-2``, … on collision."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(name)
    i = 1
    while (directory / f"{stem}-{i}{suffix}").exists():
        i += 1
    return directory / f"{stem}-{i}{suffix}"


def has_free_space(path: Path, size_bytes: int) -> bool:
    """True if writing ``size_bytes`` keeps at least 5% of the disk free."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return False
    return (usage.free - size_bytes) >= usage.total * FREE_SPACE_BUFFER_FRACTION


def stream_size(stream) -> int:
    """Return the byte length of a seekable stream, restoring its position."""
    try:
        stream.seek(0, os.SEEK_END)
        size = int(stream.tell())
        stream.seek(0)
        return size
    except (OSError, AttributeError, ValueError):
        return 0


def convert_heic(source, out_path: Path) -> bool:
    """Convert a HEIC/HEIF image to JPEG, preserving EXIF orientation.

    iPhones default to HEIC; the media pipeline only handles the classic
    formats, so we normalise to JPEG on arrival.  Returns True on success.
    """
    try:
        import pillow_heif  # type: ignore[import-not-found, import-untyped]  # optional dep, no stubs
        from PIL import Image, ImageOps

        pillow_heif.register_heif_opener()
        with Image.open(source) as opened:
            img: Image.Image = ImageOps.exif_transpose(opened)
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            img.save(out_path, "JPEG", quality=90)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("HEIC conversion failed for %s", out_path.name, exc_info=True)
        return False


def serve_resized_frame_bytes(path: Path) -> bytes | None:
    """Downscale a full-resolution video frame to thumbnail bytes.

    Returns raw JPEG bytes, or ``None`` if the frame could not be decoded.
    """
    THUMB = 320  # noqa: N806
    try:
        from PIL import Image

        img: Image.Image = Image.open(path)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        img.thumbnail((THUMB, THUMB), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        logger.warning("Failed to resize frame: %s", path, exc_info=True)
        return None


def clear_cache(state) -> tuple[int, int]:
    """Delete all processed cache files, returning ``(deleted_files, freed_bytes)``.

    Also resets the processing journal, clears the backend playlist, and
    invalidates the in-memory file-list cache so the next listing re-scans.
    """
    config = state.config
    cache_dir = resolve_install_path(Path(config.system.get("cache_dir", "cache/")))

    cache_subdirs = ["images", "thumbnails", "videos"]

    deleted_files = 0
    freed_bytes = 0

    for subdir in cache_subdirs:
        cache_path = cache_dir / subdir
        if cache_path.exists() and cache_path.is_dir():
            try:
                for entry in cache_path.iterdir():
                    if entry.is_file():
                        try:
                            file_size = entry.stat().st_size
                            entry.unlink()
                            deleted_files += 1
                            freed_bytes += file_size
                        except OSError:
                            logger.warning("Failed to delete cache file: %s", entry)
            except OSError:
                logger.warning("Failed to iterate cache dir: %s", cache_path)

    logger.info(
        "All caches cleared: %d files deleted, %d bytes freed (%s, %s, %s)",
        deleted_files,
        freed_bytes,
        *cache_subdirs,
    )

    # Reset processing journal — every cached file is gone, so all journal
    # outcomes are stale.  Wipe it so the next scan re-discovers everything.
    import contextlib

    with contextlib.suppress(Exception):
        state.journal.clear()

    # Reset backend playlist — all MediaItems point to deleted cached files.
    state.clear_playlist()

    # Invalidate the file-list cache so the next list_media() re-scans.
    with _file_list_lock:
        file_list_cache.clear()

    return deleted_files, freed_bytes
