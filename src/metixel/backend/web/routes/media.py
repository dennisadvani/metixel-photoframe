# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media management API endpoints.

Thin Flask handlers — the filesystem logic lives in
:mod:`metixel.backend.web.media_service`.  Underscore aliases are kept so
existing tests that monkeypatch ``media_mod._resolve_cache_dir`` /
``media_mod._convert_heic`` keep working.
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory

from metixel.backend.web.media_service import (
    clear_cache,
    convert_heic,
    has_free_space,
    lookup_thumbnail,
    probe_image,
    probe_video,
    relative_to_any,
    resolve_cache_dir,
    resolve_upload_dir,
    sanitize_filename,
    serve_resized_frame_bytes,
    stream_size,
    unique_path,
    watch_folder_name,
)
from metixel.shared.media import (
    HEIC_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    content_hash,
)
from metixel.shared.paths import resolve_install_path

logger = logging.getLogger(__name__)

media_bp = Blueprint("media", __name__)

# Backwards-compatible aliases (logic lives in media_service.py).
UPLOAD_SUBDIR = "my_media"
FREE_SPACE_BUFFER_FRACTION = 0.05
_CACHE_TTL = 60.0
_file_list_cache = __import__(
    "metixel.backend.web.media_service", fromlist=["file_list_cache"]
).file_list_cache
_file_list_lock = __import__(
    "metixel.backend.web.media_service", fromlist=["_file_list_lock"]
)._file_list_lock
_resolve_cache_dir = resolve_cache_dir
_probe_image = probe_image
_probe_video = probe_video
_lookup_thumbnail = lookup_thumbnail
_relative_to_any = relative_to_any
_watch_folder_name = watch_folder_name
_resolve_upload_dir = resolve_upload_dir
_sanitize_filename = sanitize_filename
_unique_path = unique_path
_has_free_space = has_free_space
_stream_size = stream_size
_convert_heic = convert_heic


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
    if not (
        safe_name.endswith(".jpg")
        or safe_name.endswith(".jpeg")
        or safe_name.endswith(".frame.jpg")
    ):
        return jsonify({"error": "Invalid file type"}), 403

    # 1. Try the thumbnail cache directory (already 320 px)
    cache_dir = _resolve_cache_dir(state)
    thumb_dir = cache_dir / "thumbnails"
    thumb_path = thumb_dir / safe_name
    if thumb_path.exists() and thumb_path.is_file():
        return send_from_directory(str(thumb_dir), safe_name, mimetype="image/jpeg")

    # 2. Try the media folder for video frame caches
    from metixel.shared.config import resolve_watch_paths

    config = state.config
    watch_paths = resolve_watch_paths(config)
    media_folder = watch_paths[0] if watch_paths else resolve_install_path("media/")

    if media_folder.exists():
        for candidate in media_folder.rglob(safe_name):
            if candidate.is_file():
                # Video frames are full-resolution — downscale to
                # thumbnail size before serving.
                if safe_name.endswith(".frame"):
                    return _serve_resized_frame(candidate)
                return send_from_directory(
                    str(candidate.parent),
                    safe_name,
                    mimetype="image/jpeg",
                )

    return jsonify({"error": "Thumbnail not found"}), 404


def _serve_resized_frame(path: Path) -> Response:
    """Resize a full-resolution video frame to thumbnail size and serve it."""
    data = serve_resized_frame_bytes(path)
    if data is not None:
        return Response(data, mimetype="image/jpeg")
    # Fall back to serving the original
    return send_from_directory(str(path.parent), path.name, mimetype="image/jpeg")


@media_bp.route("/list", methods=["GET"])
def list_media():
    """List media items with pagination across all configured watch paths.

    Query params:
        offset (int): 0-based start index (default 0)
        limit  (int): max items per page (default 20, max 200)

    Returns:
        JSON: ``{items, total, offset, limit, has_more, images, videos}``

    A lightweight in-memory cache avoids re-scanning the filesystem
    on every request.  Only the requested page is processed (PIL open,
    hash, ffprobe) — not the entire directory.
    """
    state = current_app.config["METIXEL_STATE"]
    config = state.config

    # Resolve all watch paths — multiple directories are supported
    from metixel.shared.config import resolve_watch_paths

    watch_paths: list[Path] = resolve_watch_paths(config)

    # Parse pagination
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = max(1, min(200, int(request.args.get("limit", 20))))
    except (ValueError, TypeError):
        limit = 20

    # Parse filters (server-side — keeps the browser from downloading the
    # whole library just to filter it, which matters on low-power Pis).
    name_filter = (request.args.get("name") or "").strip().lower()
    folder_filter = (request.args.get("folder") or "").strip()
    type_filter = (request.args.get("type") or "").strip()

    cache_dir = _resolve_cache_dir(state)
    thumb_dir = cache_dir / "thumbnails"

    # ── Get or populate the file-list cache ──────────────────────────
    # Cache key is the sorted tuple of resolved paths — invalidates if
    # the watch_paths config changes.
    cache_key = str(tuple(sorted(str(p) for p in watch_paths)))
    now = time.monotonic()

    with _file_list_lock:
        cached = _file_list_cache.get(cache_key)

        if cached is not None and (now - cached[0]) < _CACHE_TTL:
            all_paths, img_count, vid_count = cached[1], cached[2], cached[3]
        else:
            all_paths: list[Path] = []
            img_count = 0
            vid_count = 0
            for media_folder in watch_paths:
                if not media_folder.exists():
                    continue
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

    # ── Apply filters to the full path list ──────────────────────────
    # Filtering happens server-side (before pagination) so the browser only
    # ever receives the page it displays, not the entire library.
    filtered_paths: list[Path] = []
    for entry in all_paths:
        suffix = entry.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        if type_filter == "video" and not is_video:
            continue
        if type_filter == "image" and is_video:
            continue
        if name_filter and name_filter not in entry.name.lower():
            continue
        if folder_filter and _watch_folder_name(entry, watch_paths) != folder_filter:
            continue
        filtered_paths.append(entry)

    total = len(filtered_paths)
    img_count = sum(
        1 for p in filtered_paths if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    vid_count = total - img_count

    # ── Snapshot video transcode queue status ─────────────────────────
    # Cross-reference file hashes so the web UI can show "Queued" /
    # "Transcoding" tags on video items.
    video_status: dict[str, str] = {}
    opt_queue = current_app.config.get("METIXEL_OPT_QUEUE")
    if opt_queue is not None:
        try:
            video_status = opt_queue.get_video_queue_status()
        except Exception:
            logger.debug("Could not query video queue status", exc_info=True)

    # ── Slice the requested page ─────────────────────────────────────
    page_paths = filtered_paths[offset : offset + limit]

    items = []
    for entry in page_paths:
        suffix = entry.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        try:
            if is_video:
                w, h = _probe_video(entry)
            else:
                w, h = _probe_image(entry)

            # Look up the thumbnail (generated by FolderWatcher during scan)
            thumbnail_url = _lookup_thumbnail(entry, thumb_dir)

            # Determine the containing watch folder
            folder = _watch_folder_name(entry, watch_paths)

            # Show path relative to the first matching watch path
            rel_path = _relative_to_any(entry, watch_paths)

            item_data: dict = {
                "name": entry.name,
                "path": rel_path,
                "folder": folder,
                "width": w,
                "height": h,
                "size_kb": round(entry.stat().st_size / 1024, 1),
                "media_type": "video" if is_video else "image",
                "thumbnail_url": thumbnail_url,
            }

            # Attach transcode queue status for videos
            if is_video and video_status:
                file_hash = content_hash(entry)
                status = video_status.get(file_hash)
                if status:
                    item_data["transcode_status"] = status

            items.append(item_data)
        except Exception:
            folder = _watch_folder_name(entry, watch_paths)
            rel_path = _relative_to_any(entry, watch_paths)
            items.append(
                {
                    "name": entry.name,
                    "path": rel_path,
                    "folder": folder,
                    "width": 0,
                    "height": 0,
                    "size_kb": round(entry.stat().st_size / 1024, 1),
                    "media_type": "video" if is_video else "image",
                    "thumbnail_url": None,
                }
            )

    return jsonify(
        {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
            "images": img_count,
            "videos": vid_count,
        }
    )


@media_bp.route("/upload", methods=["POST"])
def upload_media():
    """Upload media files into the user-media watch folder.

    Accepts ``multipart/form-data`` with multiple files under the ``files``
    field name.  Files land in ``media/my_media/`` (an enabled watch path),
    are auto-renamed on name collision, and must satisfy the extension
    whitelist.  HEIC/HEIF images are converted to JPEG on arrival because
    the media pipeline only handles the classic image formats.  Uploads are
    rejected when they would leave less than 5% of the filesystem free.

    Returns:
        JSON ``{saved: [...], errors: [...]}`` with per-file results.
    """
    state = current_app.config["METIXEL_STATE"]
    upload_dir = _resolve_upload_dir(state)

    files = request.files.getlist("files")
    if not files:
        return (
            jsonify({"saved": [], "errors": [{"name": None, "error": "No files supplied"}]}),
            400,
        )

    saved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for f in files:
        original = _sanitize_filename(f.filename or "")
        if not original:
            errors.append({"name": f.filename, "error": "Invalid filename"})
            continue

        suffix = os.path.splitext(original)[1].lower()
        if suffix not in (IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | HEIC_EXTENSIONS):
            errors.append({"name": original, "error": f"Unsupported file type: {suffix}"})
            continue

        size = _stream_size(f.stream)
        if size <= 0:
            errors.append({"name": original, "error": "Empty file"})
            continue
        if not _has_free_space(upload_dir, size):
            errors.append({"name": original, "error": "Insufficient disk space"})
            continue

        if suffix in HEIC_EXTENSIONS:
            out_name = os.path.splitext(original)[0] + ".jpg"
            out_path = _unique_path(upload_dir, out_name)
            if not _convert_heic(f.stream, out_path):
                errors.append({"name": original, "error": "HEIC conversion failed"})
                continue
            saved.append(
                {"name": original, "saved_as": out_path.name, "size": out_path.stat().st_size}
            )
        else:
            out_path = _unique_path(upload_dir, original)
            try:
                f.save(str(out_path))
            except OSError:
                errors.append({"name": original, "error": "Failed to save file"})
                continue
            saved.append({"name": original, "saved_as": out_path.name, "size": size})

    status = 201 if saved else 400
    return (
        jsonify(
            {
                "saved": saved,
                "errors": errors,
                "saved_count": len(saved),
                "error_count": len(errors),
            }
        ),
        status,
    )


@media_bp.route("/cache/clear", methods=["POST"])
def clear_image_cache():
    """Clear all processed media caches.

    Deletes all files in cache/images, cache/thumbnails, and cache/videos.
    Clears the backend playlist and signals the frontend to reset its queue.
    The next folder scan will re-process source files from scratch.

    Returns:
        JSON: ``{"status": "ok", "deleted_files": N, "freed_bytes": B}``
    """
    state = current_app.config["METIXEL_STATE"]

    # Delete cache files, reset journal/playlist, and invalidate the
    # file-list cache (all handled by the media service).
    deleted_files, freed_bytes = clear_cache(state)

    # ── Signal frontend to reset its queue ──────────────────────────
    # The frontend watches playlist.json — an empty file triggers a
    # full queue reset.  The next folder-watcher scan will re-discover
    # source files, re-process them, and repopulate the playlist.
    ipc = current_app.config.get("METIXEL_IPC")
    if ipc is not None:
        from metixel.shared.ipc import ControlMessage

        ipc.send(ControlMessage(cmd="pause"))
        logger.debug(
            "Sent pause via IPC before cache clear — frontend will reset on empty playlist"
        )

    # ── Schedule services restart ────────────────────────────────────
    # Restart both services after a short delay so the HTTP response
    # is sent before the backend process is killed.  Uses a detached
    # subprocess so the restart survives the backend's own shutdown.
    subprocess.Popen(
        ["bash", "-c", "sleep 1.5 && sudo systemctl restart metixel-backend metixel-cage"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("Services restart scheduled (metixel-backend + metixel-cage)")

    return jsonify(
        {
            "status": "ok",
            "deleted_files": deleted_files,
            "freed_bytes": freed_bytes,
            "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        }
    )
