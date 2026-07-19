# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media management API endpoints."""

import hashlib
import io
import logging
import shutil
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, Response, send_from_directory

logger = logging.getLogger(__name__)

media_bp = Blueprint("media", __name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}

# ── Lightweight file-list cache ──────────────────────────────────────
# Avoids re-scanning the filesystem on every paginated request.
# Invalidated after _CACHE_TTL seconds or by clear_image_cache().
_CACHE_TTL = 60.0
_file_list_cache: dict[str, tuple[float, list[Path], int, int]] = {}
# key = str(media_folder) → (timestamp, paths, img_count, vid_count)


def _hash_file(path: Path) -> str:
    """Compute a short content hash for a file (first 1MB + last 1KB).

    Mirrors ``ImageProcessor._hash_file()`` so we can check for
    cached thumbnails without importing the processing module.
    Handles files smaller than 1KB gracefully.
    """
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        chunk = f.read(1024 * 1024)
        sha.update(chunk)
        # Only hash the tail if the file is large enough
        if len(chunk) >= 1024:
            f.seek(-1024, 2)
            sha.update(f.read(1024))
    return sha.hexdigest()[:16]


def _resolve_cache_dir(state) -> Path:
    """Resolve the cache directory from config."""
    config = state.config
    cache_dir = Path(config.system.get("cache_dir", "cache/"))
    if not cache_dir.is_absolute():
        cache_dir = Path("/opt/metixel") / cache_dir
    return cache_dir


@media_bp.route("/thumbnail/<path:filename>")
def serve_thumbnail(filename: str):
    """Serve a cached thumbnail or video frame image.

    Looks in two locations (in order):

    1. ``<cache_dir>/thumbnails/<filename>`` — image thumbnails (already 320px).
    2. ``<media_folder>/**/<filename>`` — video frame caches
       (``.1.frame`` / ``.2.frame`` files stored next to videos).

    Video frame files are full-resolution — they are downscaled to
    320 px max before serving, matching image thumbnail sizing.
    """
    state = current_app.config["METIXEL_STATE"]
    safe_name = Path(filename).name

    # Security: only allow known safe extensions
    if not (safe_name.endswith(".jpg") or safe_name.endswith(".jpeg")
            or safe_name.endswith(".frame")):
        return jsonify({"error": "Invalid file type"}), 403

    # 1. Try the thumbnail cache directory (already 320 px)
    cache_dir = _resolve_cache_dir(state)
    thumb_dir = cache_dir / "thumbnails"
    thumb_path = thumb_dir / safe_name
    if thumb_path.exists() and thumb_path.is_file():
        return send_from_directory(str(thumb_dir), safe_name, mimetype="image/jpeg")

    # 2. Try the media folder for video frame caches
    config = state.config
    media_folder = Path(config.system.get("media_folder", "media"))
    if not media_folder.is_absolute():
        media_folder = Path("/opt/metixel") / media_folder

    if media_folder.exists():
        for candidate in media_folder.rglob(safe_name):
            if candidate.is_file():
                # Video frames are full-resolution — downscale to
                # thumbnail size before serving.
                if safe_name.endswith(".frame"):
                    return _serve_resized_frame(candidate)
                return send_from_directory(
                    str(candidate.parent), safe_name, mimetype="image/jpeg",
                )

    return jsonify({"error": "Thumbnail not found"}), 404


def _serve_resized_frame(path: Path) -> Response:
    """Resize a full-resolution video frame to thumbnail size and serve it."""
    THUMB = 320
    try:
        from PIL import Image

        img = Image.open(path)
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        img.thumbnail((THUMB, THUMB), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        buf.seek(0)
        return Response(buf.read(), mimetype="image/jpeg")
    except Exception:
        logger.warning("Failed to resize frame: %s", path, exc_info=True)
        # Fall back to serving the original
        return send_from_directory(
            str(path.parent), path.name, mimetype="image/jpeg",
        )


@media_bp.route("/list", methods=["GET"])
def list_media():
    """List media items with pagination.

    Query params:
        offset (int): 0-based start index (default 0)
        limit  (int): max items per page (default 50, max 200)

    Returns:
        JSON: ``{items, total, offset, limit, has_more, images, videos}``

    A lightweight in-memory cache avoids re-scanning the filesystem
    on every request.  Only the requested page is processed (PIL open,
    hash, ffprobe) — not the entire directory.
    """
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    media_folder = Path(config.system.get("media_folder", "media"))
    if not media_folder.is_absolute():
        media_folder = Path("/opt/metixel") / media_folder

    # Parse pagination
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except (ValueError, TypeError):
        limit = 50

    cache_dir = _resolve_cache_dir(state)
    thumb_dir = cache_dir / "thumbnails"

    # ── Get or populate the file-list cache ──────────────────────────
    cache_key = str(media_folder)
    cached = _file_list_cache.get(cache_key)
    now = time.monotonic()

    if cached is not None and (now - cached[0]) < _CACHE_TTL:
        all_paths, img_count, vid_count = cached[1], cached[2], cached[3]
    else:
        all_paths: list[Path] = []
        img_count = 0
        vid_count = 0
        if media_folder.exists():
            for entry in sorted(media_folder.rglob("*")):
                if not entry.is_file():
                    continue
                suffix = entry.suffix.lower()
                if suffix in IMAGE_EXTENSIONS:
                    all_paths.append(entry)
                    img_count += 1
                elif suffix in VIDEO_EXTENSIONS:
                    all_paths.append(entry)
                    vid_count += 1
        _file_list_cache[cache_key] = (now, all_paths, img_count, vid_count)

    total = len(all_paths)

    # ── Slice the requested page ─────────────────────────────────────
    page_paths = all_paths[offset: offset + limit]

    # ── Process only the page items ──────────────────────────────────
    items = []
    for entry in page_paths:
        suffix = entry.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        try:
            if is_video:
                w, h = _probe_video(entry)
                thumbnail_url = _lookup_thumbnail(entry, thumb_dir)
            else:
                w, h = _probe_image(entry)
                thumbnail_url = _lookup_thumbnail(entry, thumb_dir)

            items.append({
                "name": entry.name,
                "path": str(entry.relative_to(media_folder)),
                "width": w,
                "height": h,
                "size_kb": round(entry.stat().st_size / 1024, 1),
                "media_type": "video" if is_video else "image",
                "thumbnail_url": thumbnail_url,
            })
        except Exception:
            items.append({
                "name": entry.name,
                "path": str(entry.relative_to(media_folder)),
                "width": 0,
                "height": 0,
                "size_kb": round(entry.stat().st_size / 1024, 1),
                "media_type": "video" if is_video else "image",
                "thumbnail_url": None,
            })

    return jsonify({
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
        "images": img_count,
        "videos": vid_count,
    })


def _probe_image(path: Path) -> tuple[int, int]:
    """Get image dimensions without a full decode."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


def _probe_video(path: Path) -> tuple[int, int]:
    """Get video dimensions via ffprobe."""
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            return (int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return (0, 0)


def _lookup_thumbnail(path: Path, thumb_dir: Path) -> str | None:
    """Check if a cached thumbnail exists and return its URL."""
    try:
        file_hash = _hash_file(path)
        thumb_path = thumb_dir / f"{file_hash}.jpg"
        if thumb_path.exists():
            return f"/api/media/thumbnail/{file_hash}.jpg"
    except OSError:
        pass
    return None


@media_bp.route("/cache/clear", methods=["POST"])
def clear_image_cache():
    """Clear the processed image cache.

    Deletes all files in the cache/images and cache/thumbnails directories.
    The next slideshow cycle will re-process source images on demand.

    Returns:
        JSON: ``{"status": "ok", "deleted_files": N, "freed_bytes": B}``
    """
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    cache_dir = Path(config.system.get("cache_dir", "cache/"))
    if not cache_dir.is_absolute():
        cache_dir = Path("/opt/metixel") / cache_dir

    image_cache = cache_dir / "images"
    thumb_cache = cache_dir / "thumbnails"

    deleted_files = 0
    freed_bytes = 0

    for cache_path in (image_cache, thumb_cache):
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
        "Image cache cleared: %d files deleted, %d bytes freed",
        deleted_files, freed_bytes,
    )

    # Also invalidate the file-list cache so the next list_media()
    # call re-scans the filesystem.
    _file_list_cache.clear()

    return jsonify({
        "status": "ok",
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    })
