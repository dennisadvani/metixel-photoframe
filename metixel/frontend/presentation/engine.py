# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Presentation Engine — two-texture ping-pong slideshow with video support.

Uses exactly two GPU texture slots that alternate: the active slot is
displayed while the inactive slot is preloaded with the next image or
video frame.  Transitions crossfade between the two slots.  Video first
and last frames are cached to disk (``.1.frame`` / ``.2.frame``) and
treated like normal images — VLC simply plays on top of the slideshow.

Video playback is driven by a non-blocking state machine so the render
loop stays responsive to IPC control commands (next/prev/pause/resume)
at all times.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from metixel.display.backend import DisplayBackend
from metixel.frontend.presentation.layout import LayoutEngine
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.frontend.presentation.video_player import VIDEO_EXTENSIONS
from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Video playback state machine
# ---------------------------------------------------------------------------

#: No video is playing.
_VIDEO_IDLE = 0
#: VLC is running; waiting for the last-frame swap time.
_VIDEO_PLAYING = 1
#: Last frame has been swapped under VLC; waiting for VLC to exit.
_VIDEO_SWAPPED = 2

# ---------------------------------------------------------------------------
# Video frame cache helpers (module-level — no GPU/OpenGL dependency)
# ---------------------------------------------------------------------------

def _video_frame_cache_path(video_path: str, frame: int) -> Path:
    """Return the cache path for a video frame.

    ``frame=1`` → first frame, ``frame=2`` → last frame.
    Files are stored next to the video, e.g. ``video.mp4.1.frame``.
    """
    return Path(f"{video_path}.{frame}.frame")


def _video_frame_is_cached(video_path: str, frame: int) -> bool:
    """Check whether a cached frame file exists and is a valid JPEG."""
    p = _video_frame_cache_path(video_path, frame)
    if not p.exists() or p.stat().st_size == 0:
        return False
    # Validate that it's actually a readable JPEG — stale/corrupt
    # cache files from old extraction code would load as black.
    try:
        with Image.open(p) as img:
            img.verify()
        return True
    except Exception:
        logger.warning("Cached frame is corrupt — will regenerate: %s", p)
        with contextlib.suppress(OSError):
            p.unlink()
        return False


def _hash_image_file(path: Path) -> str:
    """Compute a short content hash for an image file.

    Uses first 1MB + last 1KB, matching ``ImageProcessor._hash_file()``.
    Handles files smaller than 1KB gracefully.
    """
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        chunk = f.read(1024 * 1024)
        sha.update(chunk)
        if len(chunk) >= 1024:
            f.seek(-1024, 2)
            sha.update(f.read(1024))
    return sha.hexdigest()[:16]


_THUMBNAIL_SIZE = 320
_THUMBNAIL_QUALITY = 70


def _generate_image_thumbnail(source: Path, thumb_dir: Path) -> Path | None:
    """Create a 320 px thumbnail for an image, saving it to *thumb_dir*.

    Returns the path to the thumbnail, or ``None`` on failure.
    Skips if the thumbnail already exists.
    """
    file_hash = _hash_image_file(source)
    thumb_path = thumb_dir / f"{file_hash}.jpg"
    if thumb_path.exists():
        return thumb_path

    try:
        img = Image.open(source)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.LANCZOS)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = thumb_dir / f".{file_hash}.tmp"
        img.save(tmp_path, "JPEG", quality=_THUMBNAIL_QUALITY)
        os.replace(tmp_path, thumb_path)
        img.close()
        logger.debug("Generated image thumbnail: %s", thumb_path)
        return thumb_path
    except Exception:
        logger.debug(
            "Failed to generate image thumbnail for %s", source, exc_info=True,
        )
        return None


def _generate_video_thumbnail(
    video_path: str, video_w: int, video_h: int, thumb_dir: Path,
) -> Path | None:
    """Extract a frame at t=2s and create a 320 px thumbnail for a video.

    Returns the path to the thumbnail, or ``None`` on failure.
    Skips if the thumbnail already exists.
    """
    # Use a hash of the video path as the thumbnail key
    file_hash = _hash_image_file(Path(video_path))
    thumb_path = thumb_dir / f"{file_hash}.jpg"
    if thumb_path.exists():
        return thumb_path

    arr = _extract_frame_array_cpu(video_path, video_w, video_h, seek_time=2.0)
    if arr is None:
        # Fallback: try t=0
        arr = _extract_frame_array_cpu(video_path, video_w, video_h, seek_time=0.0)
    if arr is None:
        return None

    try:
        img = Image.fromarray(arr)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.LANCZOS)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = thumb_dir / f".{file_hash}.tmp"
        img.save(tmp_path, "JPEG", quality=_THUMBNAIL_QUALITY)
        os.replace(tmp_path, thumb_path)
        img.close()
        logger.debug("Generated video thumbnail: %s", thumb_path)
        return thumb_path
    except Exception:
        logger.debug(
            "Failed to generate video thumbnail for %s", video_path, exc_info=True,
        )
        return None


def _extract_frame_array_cpu(
    video_path: str,
    video_w: int,
    video_h: int,
    *,
    seek_time: float | None = None,
    seek_from_end: float | None = None,
) -> np.ndarray | None:
    """Extract a single video frame as a numpy array (CPU-only, no GPU).

    Safe to call from any thread.

    When *seek_from_end* is set, ffmpeg's ``-sseof`` option seeks to
    *seek_from_end* seconds before EOF.  Because compressed video can
    only be decoded from a keyframe, a seek that is too close to the
    end (e.g. 0.05 s) may silently snap to the previous keyframe —
    potentially **seconds** earlier.  A larger offset (≥ 1 s) combined
    with decoding multiple frames and taking the last one avoids this.
    """
    try:
        if seek_from_end is not None:
            # Decode the last ~1 second of video into individual frames,
            # then keep only the final frame.  This is robust against
            # keyframe placement: even if the nearest keyframe is several
            # seconds before EOF, we decode through to the actual end.
            #
            # MEMORY-SAFE: uses subprocess.Popen with incremental stdout
            # reading and a rolling buffer containing only the last ~2
            # frames.  On a Pi Zero 2 W (512 MB) a single 1080p raw-
            # RGB24 frame is ~6 MB, so the buffer peaks at ~12 MB —
            # compared to the previous capture_output=True approach which
            # could hold **gigabytes** of raw video in RAM when the last
            # keyframe was far from EOF.
            cmd = [
                "ffmpeg", "-y",
                "-sseof", f"-{seek_from_end}",
                "-i", video_path,
                "-f", "image2pipe",
                "-pix_fmt", "rgb24", "-vcodec", "rawvideo", "-",
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            frame_size = video_w * video_h * 3

            # Read stdout incrementally, keeping only the last ~2 frames
            # in a rolling byte buffer.  The rawvideo pipe has no framing
            # — it's just a continuous RGB24 stream — but ffmpeg always
            # writes complete frames, so the *end* of the stream is
            # guaranteed to be frame-aligned.
            buf = b''
            max_buf = 2 * frame_size + 65536  # ~12 MB for 1080p
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_buf:
                    # Drop oldest data; keep ~2 frames = safe margin
                    buf = buf[-(2 * frame_size):]

            proc.wait(timeout=30)
            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg (sseof) returned %d for %s",
                    proc.returncode, video_path,
                )
                return None

            total = len(buf)
            if total < frame_size:
                # Fewer bytes than one frame — maybe a partial frame.
                # Try to salvage whatever we got.
                if total < 3:
                    return None
                pixels = total // 3
                h = max(1, int((pixels / (video_w / max(video_h, 1))) ** 0.5))
                h = min(h, video_h)
                w = pixels // h
                if w < 1 or h < 1:
                    return None
                return np.frombuffer(
                    buf[: w * h * 3], dtype=np.uint8,
                ).reshape((h, w, 3))

            # The last frame_size bytes of buf are the last complete frame
            # (the stream always ends on a frame boundary).
            num_frames = total // frame_size
            frame = np.frombuffer(
                buf[-frame_size:], dtype=np.uint8,
            ).reshape((video_h, video_w, 3))
            logger.debug(
                "Extracted last frame from %d frames (%.1fs of video) for %s",
                num_frames, num_frames / 30.0, video_path,
            )
            return frame

        elif seek_time is not None:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(seek_time),
                "-i", video_path,
                "-vframes", "1", "-f", "image2pipe",
                "-pix_fmt", "rgb24", "-vcodec", "rawvideo", "-",
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=30)
            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg (ss) returned %d for %s (stderr: %.200s)",
                    proc.returncode, video_path,
                    proc.stderr.decode(errors="replace") if proc.stderr else "",
                )
                return None

            actual = len(proc.stdout)
            expected = video_w * video_h * 3
            if actual < 3:
                return None
            if actual >= expected:
                frame = np.frombuffer(
                    proc.stdout[:expected], dtype=np.uint8,
                ).reshape((video_h, video_w, 3))
            else:
                pixels = actual // 3
                h = max(1, int((pixels / (video_w / max(video_h, 1))) ** 0.5))
                h = min(h, video_h)
                w = pixels // h
                if w < 1 or h < 1:
                    return None
                frame = np.frombuffer(
                    proc.stdout[: w * h * 3], dtype=np.uint8,
                ).reshape((h, w, 3))
            return frame
        else:
            return None

    except Exception:
        logger.warning(
            "Frame extraction failed for %s", video_path, exc_info=True,
        )
        return None


def _get_or_create_video_frame(
    video_path: str,
    frame: int,
    video_w: int,
    video_h: int,
    duration: float,
    *,
    screen_w: int = 1920,
    screen_h: int = 1080,
) -> Path | None:
    """Return the path to a cached video frame JPEG, creating it if needed.

    ``frame=1`` → first frame (t=0).
    ``frame=2`` → last frame.  Uses ``-sseof -1.0`` to decode the final
    second of video and take the very last complete frame.  This is
    robust against keyframe placement — even if the nearest I-frame is
    several seconds before EOF, all frames are decoded and the last one
    is kept.  Falls back to ``-ss duration-1.0`` if ``-sseof`` is
    unavailable (older ffmpeg).

    The JPEG is downscaled to *screen_w* × *screen_h* and saved next to
    the video file.  Returns ``None`` if extraction fails.
    """
    cache_path = _video_frame_cache_path(video_path, frame)
    if _video_frame_is_cached(video_path, frame):
        return cache_path

    if frame == 1:
        arr = _extract_frame_array_cpu(video_path, video_w, video_h, seek_time=0.0)
    else:
        # Primary: decode the last ~1 second of video, keep the final frame.
        # -sseof -1.0 → start 1 second before EOF, decode all remaining frames.
        # This is keyframe-robust: ffmpeg snaps to the nearest previous I-frame
        # (which could be seconds earlier), but since we decode everything to EOF
        # and take the last frame, we always get the true final frame.
        arr = _extract_frame_array_cpu(
            video_path, video_w, video_h, seek_from_end=1.0,
        )
        if arr is None and duration > 1.0:
            logger.debug("-sseof failed for %s, falling back to -ss", video_path)
            arr = _extract_frame_array_cpu(
                video_path, video_w, video_h,
                seek_time=max(0.0, duration - 1.0),
            )

    if arr is None:
        logger.warning("Cannot extract frame %d for %s", frame, video_path)
        return None

    try:
        img = Image.fromarray(arr)
        if img.width > screen_w or img.height > screen_h:
            img.thumbnail((screen_w, screen_h), Image.LANCZOS)
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False, dir=cache_path.parent,
        ) as tmp:
            try:
                img.save(tmp.name, "JPEG", quality=92)
                os.replace(tmp.name, cache_path)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)
                raise

        # Also generate a 320 px thumbnail for the web dashboard.
        # Use a hash of the video path as the thumbnail key, matching
        # _generate_video_thumbnail() and VideoProcessor conventions.
        try:
            file_hash = _hash_image_file(Path(video_path))
            # Resolve cache/thumbnails/ relative to the video's directory
            # tree — fall back to a sibling thumbnails/ directory.
            thumb_dir = cache_path.parent.parent / "thumbnails"
            if not thumb_dir.exists():
                # Try /opt/metixel/cache/thumbnails/ as a convention
                thumb_dir = Path("/opt/metixel/cache/thumbnails")
            thumb_path = thumb_dir / f"{file_hash}.jpg"
            if not thumb_path.exists():
                thumb_img = img.copy()
                thumb_img.thumbnail(
                    (_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.LANCZOS,
                )
                thumb_dir.mkdir(parents=True, exist_ok=True)
                thumb_tmp = thumb_dir / f".{file_hash}.tmp"
                thumb_img.save(thumb_tmp, "JPEG", quality=_THUMBNAIL_QUALITY)
                os.replace(thumb_tmp, thumb_path)
                thumb_img.close()
                logger.debug("Generated video thumbnail: %s", thumb_path)
        except Exception:
            logger.debug(
                "Failed to generate thumbnail for %s", video_path, exc_info=True,
            )

        logger.debug("Cached video frame %d: %s", frame, cache_path)
        return cache_path
    except Exception:
        logger.warning(
            "Failed to cache frame %d for %s", frame, video_path, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# PresentationEngine
# ---------------------------------------------------------------------------

class PresentationEngine:
    """Two-texture ping-pong slideshow.

    Exactly two GPU texture slots are used — the *active* slot is drawn
    on screen while the *inactive* slot is preloaded with the next image
    or video frame.  Transitions crossfade between the two slots.

    Video frames are cached to disk as ``.1.frame`` / ``.2.frame`` JPEGs
    so extraction happens at most once per video file.  VLC plays on top
    of the slideshow; frame swaps underneath are invisible to the user.
    """

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

    def __init__(self, config: Config, backend: DisplayBackend) -> None:
        self._config = config
        self._backend = backend

        sw = backend.width or config.display.get("width") or 1920
        sh = backend.height or config.display.get("height") or 1080

        fit_mode = config.slideshow.get("fit_mode", "contain")

        logger.info(
            "PresentationEngine: resolution=%dx%d fit_mode=%s transition=%s "
            "slide_duration=%ds",
            sw, sh, fit_mode,
            config.slideshow.get("transition_style", "crossfade"),
            config.slideshow.get("image_duration_seconds", 30),
        )

        self._layout = LayoutEngine(screen_w=sw, screen_h=sh)
        self._transitions = TransitionEngine(config)

        # --- Two-texture slots ---
        self._tex: list[Any | None] = [None, None]
        self._tex_item: list[MediaItem | None] = [None, None]  # which item each slot holds
        self._active: int = 0

        # --- Queue state ---
        self._queue: list[MediaItem] = []
        self._current_idx: int = -1
        self._paused: bool = False
        self._item_start_time: float = 0.0

        # --- Preload (CPU worker → GPU upload on main thread) ---
        self._preload_thread: threading.Thread | None = None
        self._preload_lock = threading.Lock()
        self._preload_array: np.ndarray | None = None
        self._preload_cache_key: str = ""

        # --- Layout cache ---
        self._layout_cache: dict[tuple[int, str], dict] = {}
        self._fit_mode_cache: str = fit_mode
        self._screen_ratio: float = sw / max(sh, 1)

        # --- Rate-limited warnings ---
        self._transition_stall_logged: bool = False

        # --- Non-blocking video state machine ---
        self._video_state: int = _VIDEO_IDLE
        self._video_proc: subprocess.Popen[bytes] | None = None
        self._video_swap_at: float = 0.0      # monotonic timestamp for last-frame swap
        self._video_item: MediaItem | None = None
        self._video_path: str = ""
        self._video_vw: int = 0
        self._video_vh: int = 0
        self._video_duration: float = 0.0
        self._video_paused: bool = False       # True when SIGSTOP sent to VLC
        self._video_last_frame_loaded: bool = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _inactive(self) -> int:
        """Index of the texture slot NOT currently displayed."""
        return 1 - self._active

    # ------------------------------------------------------------------
    # Fit mode
    # ------------------------------------------------------------------

    def _resolve_fit_mode(self, item: MediaItem) -> str:
        mode = self._fit_mode_cache
        if mode != "cover":
            return mode
        if not self._config.slideshow.get("smart_cover", True):
            return mode
        if item.width <= 0 or item.height <= 0:
            return mode
        img_ratio = item.width / max(item.height, 1)
        if self._screen_ratio > 1.0 and img_ratio <= 1.0:
            return "contain"
        if self._screen_ratio < 1.0 and img_ratio >= 1.0:
            return "contain"
        return mode

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def set_queue(self, items: list[MediaItem]) -> None:
        self._queue = list(items)

        # Stop any running video before replacing the queue.
        if self._video_state != _VIDEO_IDLE:
            self._video_stop()

        # ── Video guardrails ─────────────────────────────────────────
        # Read video config (new section; fall back to slideshow legacy keys)
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        skipped_playback: int = 0
        skipped_transcode: int = 0
        skipped_duration: int = 0
        skipped_ready: int = 0

        for item in self._queue:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
                continue

            # 1. Video playback master switch
            if not playback_enabled:
                skipped_playback += 1
                continue

            # 2. Max duration filter
            if max_duration > 0 and item.duration_seconds > max_duration:
                skipped_duration += 1
                continue

            # 3. Transcoding guardrails
            if transcoding_enabled:
                # Only play transcoded videos (or failed ones that
                # will be played as original)
                if not item.is_ready_to_play:
                    skipped_transcode += 1
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

        if skipped_playback:
            logger.info(
                "Video playback disabled — filtered %d videos", skipped_playback,
            )
        if skipped_duration:
            logger.info(
                "Max video duration (%ds) — filtered %d videos",
                max_duration, skipped_duration,
            )
        if skipped_transcode:
            logger.info(
                "Videos not yet transcoded — filtered %d videos "
                "(transcoding is enabled; they will appear after processing)",
                skipped_transcode,
            )
        if skipped_ready:
            logger.info(
                "Videos not ready to play — filtered %d videos", skipped_ready,
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
        logger.info("Media queue set: %d items", len(self._queue))

    def add_items(self, items: list[MediaItem]) -> int:
        """Add new items to the existing queue (deduplicating by id).

        Does NOT reset the current slideshow position — new items are
        appended to the end.  This is designed for hot-reload from the
        backend playlist without interrupting the currently displayed image.

        Applies the same video guardrails as ``set_queue()``: respects
        ``video.playback_enabled``, ``video.transcoding_enabled``, and
        ``video.max_duration_seconds``.

        Returns the number of items actually added.
        """
        existing_ids = {item.id for item in self._queue}
        new_items = [item for item in items if item.id not in existing_ids]
        if not new_items:
            return 0

        # ── Video guardrails ─────────────────────────────────────────
        video_cfg = self._config.video if hasattr(self._config, "video") else {}
        playback_enabled = video_cfg.get(
            "playback_enabled",
            self._config.slideshow.get("video_playback_enabled", True),
        )
        transcoding_enabled = video_cfg.get("transcoding_enabled", True)
        max_duration = video_cfg.get(
            "max_duration_seconds",
            self._config.slideshow.get("video_max_duration_seconds", 0),
        )

        filtered: list[MediaItem] = []
        for item in new_items:
            if item.media_type != MediaType.VIDEO:
                filtered.append(item)
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

        self._queue.extend(filtered)
        if self._config.slideshow.get("shuffle", True):
            # Shuffle only the new items into existing positions — insert
            # each at a random index after the current position.
            for item in filtered:
                if self._current_idx >= 0 and len(self._queue) > self._current_idx + 1:
                    pos = random.randint(self._current_idx + 1, len(self._queue) - 1)
                else:
                    pos = len(self._queue) - 1
                # Move the last element (the new item) to the random position
                self._queue.pop()
                self._queue.insert(pos, item)

        added = len(filtered)
        skipped = len(new_items) - added
        if skipped:
            logger.info(
                "Added %d new items (filtered %d by video guardrails) — "
                "total: %d, current idx: %d",
                added, skipped, len(self._queue), self._current_idx,
            )
        else:
            logger.info(
                "Added %d new items to queue (total: %d, current idx: %d)",
                added, len(self._queue), self._current_idx,
            )
        return added

    def _advance(self) -> None:
        """Move to the next item in the queue.

        Swaps active ↔ inactive slots: the preloaded texture becomes the
        displayed one, and the old displayed texture is freed.
        """
        if not self._queue:
            return
        logger.debug(
            "advance: %d → %d  active_slot=%d→%d",
            self._current_idx,
            (self._current_idx + 1) % len(self._queue),
            self._active, self._inactive,
        )
        self._unload_texture(self._tex[self._active])
        self._tex[self._active] = None
        self._tex_item[self._active] = None
        self._active = self._inactive
        self._current_idx = (self._current_idx + 1) % len(self._queue)
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False  # reset for new slide

        # If the new active slot (old inactive) has no texture, the
        # preload either failed or hasn't completed — screen will be
        # blank until the next texture loads.
        if self._tex[self._active] is None and self._current_idx >= 0:
            logger.warning(
                "Active slot %d has no texture after advance "
                "(item=%s, idx=%d) — preload may have failed or not completed. "
                "Screen will be blank until next texture loads.",
                self._active,
                getattr(self._queue[self._current_idx], 'original_path', '?'),
                self._current_idx,
            )
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""
        self._preload_into_inactive()
        self._write_current_media()

    def next_item(self) -> None:
        """Skip to the next item in the queue.

        If a video is playing, the VLC process is killed and the
        preloaded next item is promoted immediately.  Implicitly
        resumes if the slideshow was paused.
        """
        if not self._queue:
            return

        # If a video is playing, stop it first.
        if self._video_state != _VIDEO_IDLE:
            logger.info("next_item: stopping video to advance")
            self._video_stop()

        self._paused = False
        self._advance()

    def prev_item(self) -> None:
        """Go back to the previous item in the queue.

        If a video is playing, the VLC process is killed.  The previous
        item is loaded directly into the active slot for an immediate
        cut (no transition animation — the user asked to jump).
        Implicitly resumes if the slideshow was paused.
        """
        if not self._queue:
            return

        # If a video is playing, stop it first.
        if self._video_state != _VIDEO_IDLE:
            logger.info("prev_item: stopping video to go back")
            self._video_stop()

        self._paused = False

        prev_idx = (self._current_idx - 1) % len(self._queue)
        prev = self._queue[prev_idx]

        # Unload both slots — we're doing a hard jump.
        for slot in (0, 1):
            self._unload_texture(self._tex[slot])
            self._tex[slot] = None
            self._tex_item[slot] = None

        self._current_idx = prev_idx
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False

        # Load the previous item directly into the active slot.
        if prev.media_type == MediaType.VIDEO:
            self._load_texture_for_slot(self._active, prev)
        else:
            self._tex[self._active] = self._load_texture_for_item(prev)
            self._tex_item[self._active] = prev

        # Preload the item that follows the new current position.
        with self._preload_lock:
            self._preload_array = None
            self._preload_cache_key = ""
        self._preload_into_inactive()
        self._write_current_media()

    def switch_album(self, album_id: str) -> None:
        logger.info("Album switch requested: %s (handled by backend)", album_id)

    def pause(self) -> None:
        """Pause the slideshow.

        If a video is playing, the VLC process is paused via SIGSTOP
        so playback freezes in place.
        """
        self._paused = True
        if self._video_state == _VIDEO_PLAYING and self._video_proc is not None:
            try:
                os.kill(self._video_proc.pid, signal.SIGSTOP)
                self._video_paused = True
                logger.info("VLC paused via SIGSTOP (pid=%d)", self._video_proc.pid)
            except OSError:
                logger.warning("Failed to SIGSTOP VLC", exc_info=True)
        self._write_current_media()

    def resume(self) -> None:
        """Resume the slideshow.

        If a video was paused, the VLC process is resumed via SIGCONT
        and the slide timer is reset.
        """
        self._paused = False
        if self._video_paused and self._video_proc is not None:
            try:
                os.kill(self._video_proc.pid, signal.SIGCONT)
                self._video_paused = False
                logger.info("VLC resumed via SIGCONT (pid=%d)", self._video_proc.pid)
            except OSError:
                logger.warning("Failed to SIGCONT VLC", exc_info=True)
                self._video_paused = False
        self._item_start_time = time.monotonic()
        self._write_current_media()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        if not self._queue or self._current_idx < 0:
            return

        self._upload_pending_preload()

        # --- Non-blocking video tick -----------------------------------
        # Drive the video state machine each frame so the render loop
        # stays responsive to IPC control commands even during playback.
        if self._video_state != _VIDEO_IDLE:
            self._video_tick()
            # Still render the underlying frame (first/last frame of
            # video) at full opacity — VLC's window covers it.
            current_item = self._queue[self._current_idx]
            self._render_item(current_item, 1.0)
            return

        current_item = self._queue[self._current_idx]
        elapsed = time.monotonic() - self._item_start_time
        duration = self._get_item_duration(current_item)
        transition_style = self._config.slideshow.get("transition_style", "crossfade")
        transition_ms = self._config.slideshow.get("transition_duration_ms", 1500)
        # When transition style is "none", the slide cuts immediately
        # with no animation — treat the transition duration as zero.
        if transition_style == "none":
            transition_s = 0.0
        else:
            transition_s = transition_ms / 1000.0

        if self._paused:
            self._render_item(current_item, 1.0)
            return

        if elapsed >= (duration + transition_s):
            self._advance()
            if self._current_idx >= 0:
                new_item = self._queue[self._current_idx]
                if new_item.media_type == MediaType.VIDEO:
                    # Transition just completed — the video's first
                    # frame is now in the active slot.  Launch VLC
                    # via the non-blocking state machine.
                    self._video_launch(new_item)
                else:
                    self._render_item(new_item, 1.0)
            return

        # During transition, crossfade between the two slots.
        next_tex = self._tex[self._inactive]
        if elapsed >= duration and next_tex is not None:
            progress = (elapsed - duration) / transition_s
            self._render_transition(current_item, progress, next_tex)
        else:
            if elapsed >= duration and next_tex is None and self._current_idx >= 0:
                # Preload hasn't finished — log once per slide, not every frame.
                if not self._transition_stall_logged:
                    self._transition_stall_logged = True
                    next_idx = (self._current_idx + 1) % len(self._queue)
                    next_item = self._queue[next_idx]
                    logger.warning(
                        "Transition stalled: inactive slot %d has no texture "
                        "(next item=%s, elapsed=%.1fs, slide_duration=%.1fs). "
                        "Preload likely still in progress — holding current slide.",
                        self._inactive,
                        getattr(next_item, 'original_path', next_item),
                        elapsed, duration,
                    )
            self._render_item(current_item, 1.0)

    # ------------------------------------------------------------------
    # Item rendering
    # ------------------------------------------------------------------

    def _render_item(
        self, item: MediaItem, alpha: float,
        with_matte: bool = True,
        texture: Any = None,
        layout: dict | None = None,
    ) -> None:
        """Draw a single media item with layout and matte bars."""
        if texture is None:
            tex = self._tex[self._active]
            if tex is None and self._current_idx >= 0:
                logger.warning(
                    "Active slot %d has no texture — attempting sync load for %s",
                    self._active, getattr(item, 'original_path', item),
                )
                self._load_texture_for_slot(self._active, item)
                tex = self._tex[self._active]
            if tex is None:
                logger.warning(
                    "No texture for active slot %d (item=%s, idx=%d) — "
                    "rendering blank frame (black screen)",
                    self._active,
                    getattr(item, 'original_path', item),
                    self._current_idx,
                )
                return
            texture = tex

        if layout is None:
            # Use the texture's source item for layout when it differs
            # from the current queue item.  This happens after video
            # playback: the active slot holds the last frame, but the
            # queue has already advanced to the next image.
            layout_source = self._tex_item[self._active] or item
            resolved = self._resolve_fit_mode(layout_source)
            cache_key = (id(layout_source), resolved)
            if cache_key in self._layout_cache:
                layout = self._layout_cache[cache_key]
            else:
                layout = self._layout.compute(layout_source, fit_mode=resolved)
                if len(self._layout_cache) < 16:
                    self._layout_cache[cache_key] = layout

        if with_matte:
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
            for mx, my, mw, mh in layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx, my, mw, mh, (*matte_color, alpha), z=-1,
                )

        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture, ix, iy, iw, ih,
            alpha=alpha,
            uv_offset=(0.0, 0.0),
            uv_scale=(1.0, 1.0),
            z=0.0,
        )

    def _render_transition(
        self, current_item: MediaItem, progress: float, next_tex: Any,
    ) -> None:
        """Crossfade between active and inactive texture slots."""
        next_item = self._queue[
            (self._current_idx + 1) % len(self._queue)
        ]
        style = self._config.slideshow.get("transition_style", "crossfade")

        # Use the texture's source item for layout when it differs from
        # the queue item (e.g. last frame of a video transitioning to
        # the next photo).
        cur_src = self._tex_item[self._active] or current_item
        next_src = self._tex_item[self._inactive] or next_item

        if style == "crossfade":
            current_layout = self._layout.compute(
                cur_src, fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src, fit_mode=self._resolve_fit_mode(next_src),
            )
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])

            # Full-screen black background ensures any partially-
            # transparent pixels from the crossfade shader blend
            # against solid black rather than showing framebuffer
            # artefacts or PNG transparency edges.
            self._backend.draw_rect(
                0, 0, self._backend.width, self._backend.height,
                (*matte_color, 1.0), z=-2,
            )
            for mx, my, mw, mh in current_layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx, my, mw, mh, (*matte_color, 1.0), z=-1,
                )
            self._backend.draw_crossfade(
                tex_current=self._tex[self._active],
                tex_next=next_tex,
                blend=progress,
                current_rect=current_layout["image_rect"],
                next_rect=next_layout["image_rect"],
            )
        elif style == "fade_through_black":
            # Compute layouts for both items explicitly.  _render_item
            # defaults to the *active* slot's source for layout, which
            # is still the current item during transition.  Without
            # explicit layouts, the second half would draw the next
            # texture with the current item's aspect ratio.
            cur_layout = self._layout.compute(
                cur_src, fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src, fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item, 1.0 - progress * 2,
                    texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(
                    next_item, (progress - 0.5) * 2, texture=next_tex,
                    layout=next_layout,
                )
        elif style == "none":
            # No transition — just show the next item immediately.
            # Layouts are still computed explicitly so the next
            # texture isn't drawn with the current item's aspect ratio.
            next_layout = self._layout.compute(
                next_src, fit_mode=self._resolve_fit_mode(next_src),
            )
            self._render_item(next_item, 1.0, texture=next_tex,
                              layout=next_layout)
        else:
            # Hard cut: show current until midpoint, then next.
            # Explicit layouts prevent the next texture from being drawn
            # with the current item's aspect ratio during the second half.
            cur_layout = self._layout.compute(
                cur_src, fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src, fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item, 1.0, texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(next_item, 1.0, texture=next_tex,
                                  layout=next_layout)

    # ------------------------------------------------------------------
    # Texture loading
    # ------------------------------------------------------------------

    def _load_texture_for_slot(self, slot: int, item: MediaItem) -> None:
        """Ensure ``_tex[slot]`` has a valid texture for *item*.

        For images: loads + downscales the JPEG.
        For videos: launches VLC via the non-blocking state machine.
        """
        if item.media_type == MediaType.VIDEO:
            self._video_launch(item)
            return
        self._tex[slot] = self._load_texture_for_item(item)
        self._tex_item[slot] = item

    def _load_texture_for_item(self, item: MediaItem) -> Any:
        """Load an image as a GPU texture (videos are handled elsewhere)."""
        path_to_load = item.cached_path

        max_w = int(self._backend.width * 1.2)
        max_h = int(self._backend.height * 1.2)

        try:
            from PIL import ImageFile
            ImageFile.LOAD_TRUNCATED_IMAGES = True

            img = Image.open(path_to_load)
            img = ImageOps.exif_transpose(img)

            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode not in ("RGB", "L") or img.mode == "L":
                img = img.convert("RGB")

            if img.width > max_w or img.height > max_h:
                orig_w, orig_h = img.width, img.height
                img.thumbnail((max_w, max_h), Image.LANCZOS)
                logger.debug(
                    "Downscaled [%s]: %dx%d → %dx%d",
                    path_to_load, orig_w, orig_h, img.width, img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
            logger.debug("Texture loaded [%s]: tex=%s", path_to_load, id(texture))
            return texture
        except Exception:
            logger.exception("Failed to load texture: %s", path_to_load)
            return None

    # ------------------------------------------------------------------
    # Preload system (CPU worker → GPU upload on main thread)
    # ------------------------------------------------------------------

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

            img = Image.open(path_to_load)
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
                img.thumbnail((max_w, max_h), Image.LANCZOS)
                logger.debug(
                    "Preload downscaled [%s]: %dx%d → %dx%d",
                    path_to_load, orig_w, orig_h, img.width, img.height,
                )

            arr = np.asarray(img, dtype=np.uint8)
            img.close()

            with self._preload_lock:
                self._preload_array = arr
                self._preload_cache_key = str(path_to_load)
            logger.debug("Preload ready [%s]", path_to_load)
        except Exception:
            logger.exception("Preload failed: %s", getattr(item, 'cached_path', item.original_path))
            with self._preload_lock:
                self._preload_array = None

    def _preload_video_first_frame(self, item: MediaItem) -> Path | None:
        """Ensure the first frame of a video is cached and return its path.

        Called from the preload worker thread — CPU-only, no GPU.
        """
        video_path = str(item.cached_path or item.original_path)
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return None
            parts = result.stdout.strip().split(",")
            vw, vh = int(parts[0]), int(parts[1])
            duration = float(parts[2]) if len(parts) > 2 else 0.0
        except Exception:
            logger.warning("ffprobe failed for %s", video_path, exc_info=True)
            return None

        screen_w = int(self._backend.width)
        screen_h = int(self._backend.height)
        return _get_or_create_video_frame(
            video_path, 1, vw, vh, duration,
            screen_w=screen_w, screen_h=screen_h,
        )

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
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            try:
                Image.fromarray(arr).save(tmp.name, "JPEG", quality=92)
                texture = self._backend.load_texture(Path(tmp.name))
            finally:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

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

    # ------------------------------------------------------------------
    # Video playback (non-blocking state machine)
    # ------------------------------------------------------------------

    def _video_launch(self, item: MediaItem) -> None:
        """Launch VLC for a video item without blocking the render loop.

        The first frame should already be in the active texture slot
        (loaded via the normal preload → advance flow).  VLC plays on
        top.  The state machine in :meth:`_video_tick` handles the
        last-frame under-swap and post-playback transition.
        """
        if not self._config.slideshow.get("video_playback_enabled", True):
            # Also check new video section
            video_cfg = self._config.video if hasattr(self._config, "video") else {}
            if video_cfg:
                if not video_cfg.get("playback_enabled", True):
                    logger.debug("Video playback disabled — skipping %s", item.original_path)
                    return
            else:
                logger.debug("Video playback disabled — skipping %s", item.original_path)
                return

        video_path = str(item.cached_path or item.original_path)

        # --- Probe -------------------------------------------------------
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError("ffprobe failed")
            parts = result.stdout.strip().split(",")
            vw, vh = int(parts[0]), int(parts[1])
            duration = float(parts[2]) if len(parts) > 2 else 0.0
        except Exception:
            logger.warning("Cannot probe video: %s — skipping", video_path)
            self._advance()
            return

        screen_w = int(self._backend.width)
        screen_h = int(self._backend.height)

        # --- Ensure first frame in active slot --------------------------
        if self._tex[self._active] is None:
            first_cache = _get_or_create_video_frame(
                video_path, 1, vw, vh, duration,
                screen_w=screen_w, screen_h=screen_h,
            )
            if first_cache is not None:
                self._tex[self._active] = self._backend.load_texture(first_cache)
                self._tex_item[self._active] = item

        # --- Compute layout (needed for both the pre-VLC draw and VLC) ---
        resolved_fit = self._resolve_fit_mode(item)

        # --- Draw first frame to both buffers before VLC starts ----------
        # VLC creates its own window on top of the pi3d display.  The
        # first frame must be visible in BOTH pi3d buffers before VLC
        # appears — otherwise there is a flash of black between the
        # preloaded texture and VLC's window.
        if self._tex[self._active] is not None:
            layout = self._layout.compute(item, fit_mode=resolved_fit)
            self._draw_frame_to_buffer(self._tex[self._active], layout)
            self._backend.loop_running()
            self._draw_frame_to_buffer(self._tex[self._active], layout)
        else:
            logger.warning(
                "No first-frame texture for %s — VLC will appear over black",
                video_path,
            )

        # --- Launch VLC -------------------------------------------------
        from metixel.frontend.presentation.video_player import VlcVideoPlayer

        vlc_player = VlcVideoPlayer()
        logger.info(
            "Starting VLC: %s (duration=%.1fs, fit_mode=%s)",
            video_path, item.duration_seconds, resolved_fit,
        )
        vlc_proc = vlc_player.play(
            video_path,
            screen_w=screen_w, screen_h=screen_h,
            block=False, loop=False, fit_mode=resolved_fit,
        )
        if vlc_proc is None:
            logger.warning("VLC failed to start: %s", video_path)
            self._advance()
            return

        # --- Enter state machine ----------------------------------------
        self._video_state = _VIDEO_PLAYING
        self._video_proc = vlc_proc
        self._video_item = item
        self._video_path = video_path
        self._video_vw = vw
        self._video_vh = vh
        self._video_duration = duration
        self._video_paused = False
        self._video_last_frame_loaded = False
        # Swap the last frame ~1s before the video ends (or immediately
        # for very short videos).
        swap_delay = max(0.0, duration - 1.0) if duration > 0 else 3.0
        self._video_swap_at = time.monotonic() + swap_delay
        logger.debug(
            "Video state machine: PLAYING (swap_at=%.1f, duration=%.1f)",
            self._video_swap_at, duration,
        )

    def _video_tick(self) -> None:
        """Drive the video playback state machine — called once per frame.

        This is the heart of the non-blocking video playback.  It
        replaces the old ``time.sleep()`` + ``vlc_proc.wait()`` pattern
        so the render loop stays responsive.
        """
        now = time.monotonic()

        # --- Check for VLC crash / early exit ---------------------------
        if self._video_proc is not None and not self._video_paused:
            rc = self._video_proc.poll()
            if rc is not None:
                # VLC exited (normally or crashed)
                logger.info("VLC exited with code %s: %s", rc, self._video_path)
                self._video_finish()
                return

        if self._video_state == _VIDEO_PLAYING:
            # --- Phase 1: VLC running, wait for swap time ---------------
            if now >= self._video_swap_at:
                self._video_do_last_frame_swap()

        elif self._video_state == _VIDEO_SWAPPED:
            # --- Phase 2: Last frame swapped, VLC still running ---------
            # Nothing to do — just wait for VLC to exit (polled above).
            pass

    # ------------------------------------------------------------------
    # Shared helper: extract + upload the video's last frame.
    # Used both during normal playback (under VLC) and as an emergency
    # fallback when VLC exits before the scheduled swap time.
    # ------------------------------------------------------------------

    def _load_last_frame_into_active(
        self, item: MediaItem, video_path: str,
        video_vw: int, video_vh: int, video_duration: float,
    ) -> bool:
        """Extract & upload the last frame into the active texture slot.

        Returns ``True`` on success, ``False`` if extraction failed.
        Does **not** modify ``_video_state`` or ``_video_last_frame_loaded``
        — callers are responsible for state management.
        """
        screen_w = int(self._backend.width)
        screen_h = int(self._backend.height)

        last_cache = _get_or_create_video_frame(
            video_path, 2,
            video_vw, video_vh, video_duration,
            screen_w=screen_w, screen_h=screen_h,
        )
        if last_cache is None:
            logger.warning(
                "No last frame for %s — first frame stays in pi3d",
                video_path,
            )
            return False

        self._unload_texture(self._tex[self._active])
        try:
            self._tex[self._active] = self._backend.load_texture(last_cache)
        except Exception:
            logger.exception(
                "Failed to upload last-frame texture for %s: %s",
                video_path, last_cache,
            )
            self._tex[self._active] = None
            return False
        self._tex_item[self._active] = item

        # Draw to front buffer, swap, then draw to back buffer
        # so both hold the last frame (avoids first-frame flash
        # on the next render-cycle loop_running swap).
        resolved = self._resolve_fit_mode(item)
        layout = self._layout.compute(item, fit_mode=resolved)
        self._draw_frame_to_buffer(self._tex[self._active], layout)
        self._backend.loop_running()
        self._draw_frame_to_buffer(self._tex[self._active], layout)
        logger.debug(
            "Last frame loaded into active slot %d: %s",
            self._active, video_path,
        )
        return True

    def _video_do_last_frame_swap(self) -> None:
        """Load the video's last frame into the active slot under VLC.

        The VLC window covers the display, so the swap is invisible.
        The last frame goes into the *active* slot — the inactive slot
        holds the preloaded next item and must NOT be overwritten.
        """
        if self._video_last_frame_loaded:
            return
        self._video_last_frame_loaded = True

        item = self._video_item
        if item is None:
            return

        self._load_last_frame_into_active(
            item, self._video_path,
            self._video_vw, self._video_vh, self._video_duration,
        )

        self._video_state = _VIDEO_SWAPPED
        logger.debug("Video state machine: SWAPPED (waiting for VLC exit)")

    def _video_finish(self) -> None:
        """Clean up after VLC exits and set up the post-video transition.

        The last frame is in the active slot; the next item is preloaded
        in the inactive slot.  We set ``_item_start_time`` so the render
        loop enters the transition phase on the next ``render()`` call.

        Two edge cases are handled here that the non-blocking state
        machine cannot guarantee on its own:

        1. **VLC exited before swap time** — If ``_video_swap_at`` has
           not been reached yet (e.g. ffprobe over-reported the
           duration), the last frame was never loaded.  We do it now
           synchronously.

        2. **No transition animation** — When ``transition_style`` is
           ``"none"``, ``elapsed >= duration + 0`` triggers an immediate
           ``_advance()``, skipping the last frame entirely.  We reserve
           a 0.5 s linger so the viewer sees the final frame.
        """
        item = self._video_item
        video_path = self._video_path

        # --- Emergency last-frame load (VLC exited before swap) --------
        if not self._video_last_frame_loaded and video_path and item is not None:
            logger.warning(
                "VLC exited before last-frame swap — loading now: %s",
                video_path,
            )
            try:
                self._load_last_frame_into_active(
                    item, video_path,
                    self._video_vw, self._video_vh, self._video_duration,
                )
            except Exception:
                logger.exception(
                    "Emergency last-frame swap failed for %s", video_path,
                )

        # --- Clear video state -----------------------------------------
        self._video_state = _VIDEO_IDLE
        self._video_proc = None
        self._video_item = None
        self._video_path = ""
        self._video_paused = False
        self._video_last_frame_loaded = False

        if item is not None:
            item_duration = self._get_item_duration(item)

            # --- Handle transition_style = "none" ------------------
            # Without a transition animation the _item_start_time
            # trick would cause _advance() to fire immediately,
            # never showing the last frame.  Reserve a brief linger.
            transition_style = self._config.slideshow.get(
                "transition_style", "crossfade",
            )
            if transition_style == "none":
                linger = 0.5
                self._item_start_time = (
                    time.monotonic() - item_duration + linger
                )
                logger.debug(
                    "Post-VLC (no transition): lingering %.1fs "
                    "on last frame (active=%d, inactive=%d)",
                    linger, self._active, self._inactive,
                )
            else:
                self._item_start_time = time.monotonic() - item_duration
                logger.debug(
                    "Post-VLC: item_start_time set for transition "
                    "(active=%d, inactive=%d)",
                    self._active, self._inactive,
                )

    def _video_stop(self) -> None:
        """Force-stop video playback (used by next/prev controls).

        Kills the VLC subprocess, cleans up state, and ensures the
        slideshow can continue from the current position.
        """
        if self._video_proc is not None:
            pid = self._video_proc.pid
            try:
                # Resume if paused, then terminate
                if self._video_paused:
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except OSError:
                        pass
                self._video_proc.terminate()
                try:
                    self._video_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._video_proc.kill()
                    self._video_proc.wait(timeout=1.0)
            except OSError:
                pass
            logger.info("VLC stopped (pid=%d)", pid)

        self._video_state = _VIDEO_IDLE
        self._video_proc = None
        self._video_item = None
        self._video_path = ""
        self._video_paused = False
        self._video_last_frame_loaded = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_item_duration(self, item: MediaItem) -> float:
        if item.media_type == MediaType.VIDEO:
            if item.duration_seconds > 0:
                duration = item.duration_seconds
            else:
                duration = self._config.slideshow.get("image_duration_seconds", 30)
            # Read max duration from new video section, fall back to legacy slideshow key
            video_cfg = self._config.video if hasattr(self._config, "video") else {}
            max_video = video_cfg.get(
                "max_duration_seconds",
                self._config.slideshow.get("video_max_duration_seconds", 0),
            )
            if max_video > 0 and duration > max_video:
                duration = float(max_video)
            return duration
        return self._config.slideshow.get("image_duration_seconds", 30)

    def _unload_texture(self, texture: Any) -> None:
        if texture is not None:
            self._backend.unload_texture(texture)

    def _draw_frame_to_buffer(self, texture: Any, layout: dict) -> None:
        """Draw a texture with matte bars at the given layout position."""
        matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
        for mx, my, mw, mh in layout.get("matte_rects", []):
            self._backend.draw_rect(
                mx, my, mw, mh, (*matte_color, 1.0), z=-1,
            )
        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture, ix, iy, iw, ih,
            alpha=1.0,
            uv_offset=(0.0, 0.0), uv_scale=(1.0, 1.0), z=0.0,
        )

    def _write_current_media(self) -> None:
        try:
            if self._current_idx < 0 or not self._queue:
                data = {
                    "file": None, "index": -1, "total": 0,
                    "paused": self._paused,
                    "media_type": None,
                    "thumbnail_path": None,
                }
            else:
                item = self._queue[self._current_idx]
                # Resolve the thumbnail path:
                # 1. Use the item's thumbnail_path (set by ImageProcessor
                #    or scan_folder with hash-based key).
                # 2. For videos, fall back to the hash-based thumbnail
                #    in cache/thumbnails/.
                # 3. Last resort: the raw first-frame cache (.1.frame).
                thumb = None
                if item.thumbnail_path is not None:
                    thumb = str(item.thumbnail_path)
                elif item.media_type == MediaType.VIDEO:
                    video_path = str(item.cached_path or item.original_path)
                    # Try the hash-based thumbnail first (320 px)
                    try:
                        file_hash = _hash_image_file(Path(video_path))
                        hash_thumb = Path("/opt/metixel/cache/thumbnails") / f"{file_hash}.jpg"
                        if hash_thumb.exists():
                            thumb = str(hash_thumb)
                    except OSError:
                        pass
                    # Fall back to the raw first-frame cache
                    if thumb is None:
                        first_frame = _video_frame_cache_path(video_path, 1)
                        if first_frame.exists():
                            thumb = str(first_frame)
                data = {
                    "file": str(item.original_path.name) if item.original_path else "unknown",
                    "index": self._current_idx,
                    "total": len(self._queue),
                    "paused": self._paused,
                    "media_type": item.media_type.value,
                    "thumbnail_path": thumb,
                }
            run_dir = os.environ.get("METIXEL_RUN_DIR", "/run/metixel")
            os.makedirs(run_dir, exist_ok=True)
            tmp = os.path.join(run_dir, ".current_media.tmp")
            dst = os.path.join(run_dir, "current_media.json")
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, dst)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Folder scanning
    # ------------------------------------------------------------------

    def scan_folder(self, folder_path: Path) -> list[MediaItem]:
        items: list[MediaItem] = []
        if not folder_path.exists():
            logger.warning("Media folder not found: %s", folder_path)
            return items

        # Resolve the thumbnail cache directory
        cache_dir = Path(
            self._config.system.get("cache_dir", "cache/"),
        )
        if not cache_dir.is_absolute():
            cache_dir = Path("/opt/metixel") / cache_dir
        thumb_dir = cache_dir / "thumbnails"

        all_extensions = self.IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
        for entry in sorted(folder_path.rglob("*")):
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix not in all_extensions:
                continue
            try:
                media_type = (
                    MediaType.VIDEO if suffix in VIDEO_EXTENSIONS
                    else MediaType.IMAGE
                )

                # Resolve or generate a thumbnail
                thumbnail: Path | None = None
                if media_type == MediaType.IMAGE:
                    with Image.open(entry) as img:
                        w, h = img.size
                    duration = 0.0

                    # Generate thumbnail if missing
                    thumbnail = _generate_image_thumbnail(entry, thumb_dir)
                else:
                    w, h = 0, 0
                    duration = 0.0
                    try:
                        from metixel.frontend.presentation.video_player import (
                            VideoPlayer as VP,
                        )
                        info = VP._probe(str(entry))
                        if info:
                            w = info.get("width", 0)
                            h = info.get("height", 0)
                            duration = info.get("duration", 0.0)
                    except Exception:
                        pass

                    # Generate thumbnail if missing (uses hash-based key
                    # in cache/thumbnails/ rather than .frame file)
                    if w > 0 and h > 0:
                        thumbnail = _generate_video_thumbnail(
                            str(entry), w, h, thumb_dir,
                        )

                items.append(MediaItem(
                    id=str(entry),
                    original_path=entry,
                    cached_path=entry,
                    media_type=media_type,
                    width=w, height=h,
                    duration_seconds=duration,
                    thumbnail_path=thumbnail,
                    source="local",
                ))
            except Exception:
                logger.warning("Skipping unreadable file: %s", entry)

        vc = sum(1 for i in items if i.media_type == MediaType.VIDEO)
        logger.info(
            "Folder scan: %d images + %d videos in %s",
            len(items) - vc, vc, folder_path,
        )
        return items

    # ------------------------------------------------------------------
    # Hot reload
    # ------------------------------------------------------------------

    def reload_config(self, config: Config) -> None:
        # Determine old/new video playback status from new video section
        # with fallback to legacy slideshow keys
        def _get_playback(cfg):
            if hasattr(cfg, "video") and cfg.video:
                return cfg.video.get("playback_enabled", True)
            return cfg.slideshow.get("video_playback_enabled", True)

        old_video = _get_playback(self._config)
        new_video = _get_playback(config)
        self._config = config
        self._transitions.reload_config(config)
        self._fit_mode_cache = config.slideshow.get("fit_mode", "contain")
        self._layout_cache.clear()

        if old_video != new_video:
            logger.info(
                "Video playback toggled (%s → %s) — regenerating queue",
                old_video, new_video,
            )
            media_folder = config.sync.get("local", {}).get(
                "watch_paths", ["media/"],
            )[0]
            folder_path = Path(media_folder)
            if not folder_path.is_absolute():
                folder_path = Path.cwd() / folder_path
            items = self.scan_folder(folder_path)
            self.set_queue(items)

        logger.debug("Presentation engine config reloaded")
