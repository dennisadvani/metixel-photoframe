# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Thumbnail and first/last frame extraction execution for video files.

Extracted from ``VideoProcessor`` (Phase 2 decomposition).  Command lists are
built by ``ffmpeg_cmds``; this module runs them and manages the cache files.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Callable

from metixel.backend.processing.ffmpeg_cmds import first_frame_cmd, last_frame_cmd, thumbnail_cmd
from metixel.backend.processing.utils import nice_cmd

logger = logging.getLogger(__name__)


def extract_thumbnail(source: Path, dest: Path, screen_w: int, screen_h: int, timeout: int) -> None:
    """Extract a thumbnail frame at 2 seconds into the video.

    Uses fast (keyframe) seeking with ``-ss`` before ``-i`` plus
    ``-noaccurate_seek`` to avoid decoding from the start.  Frame is
    downscaled to the display resolution to match image optimisation
    limits and avoid wasting GPU memory.
    """
    cmd = nice_cmd(thumbnail_cmd(source, dest, screen_w, screen_h))
    # Single-frame extraction — use nice only (no cpulimit).
    # Thumbnails are quick one-shot operations; cpulimit is for
    # long-running transcodes.
    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def extract_video_frames(
    source: Path,
    file_hash: str,
    frame_dir: Path,
    screen_w: int,
    screen_h: int,
    timeout_fn: Callable[[str, int], int],
) -> tuple[Path | None, Path | None]:
    """Extract first (t=0) and last (``-sseof``) frame JPEGs.

    Returns ``(first_frame_path, last_frame_path)``.  Either may be
    ``None`` if extraction fails for that frame.

    Called during Phase 2 (OPTIMISE) so the frontend never needs to run
    ffmpeg — it just loads the pre-generated cache files.
    """
    first_path: Path | None = frame_dir / f"{file_hash}.1.frame.jpg"
    last_path: Path | None = frame_dir / f"{file_hash}.2.frame.jpg"

    # ── First frame (t=0, keyframe seek) ──────────────────────────────
    if first_path is not None and (not first_path.exists() or first_path.stat().st_size == 0):
        cmd = nice_cmd(first_frame_cmd(source, first_path, screen_w, screen_h))
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_fn("frame_extract_first", 180),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.warning("Failed to extract first frame: %s", source.name)
            with contextlib.suppress(OSError):
                first_path.unlink()
            first_path = None

    # ── Last frame (sseof -1, decode final second) ────────────────────
    # Decode ALL frames from 1s before EOF to the actual end and keep only
    # the last one.  ``-update 1`` tells ffmpeg to overwrite the output
    # file with each frame, so the file on disk is always the *latest*
    # decoded frame — i.e. the true final frame regardless of keyframe
    # placement.  Using ``-vframes 1`` would grab the *first* frame after
    # the seek position, which is typically a keyframe several seconds
    # before the real end — causing a visible jitter when VLC exits and
    # the last frame appears underneath.
    if last_path is not None and (not last_path.exists() or last_path.stat().st_size == 0):
        cmd = nice_cmd(last_frame_cmd(source, last_path, screen_w, screen_h))
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_fn("frame_extract_last", 120),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.warning("Failed to extract last frame: %s", source.name)
            with contextlib.suppress(OSError):
                last_path.unlink()
            last_path = None

    return first_path, last_path


def cleanup_cached_video(cached_path: Path, file_hash: str) -> None:
    """Delete a corrupt cached video and its frame cache files.

    The thumbnail is NOT deleted — it's generated from the source file
    and is independent of the transcode output.
    """
    # Delete the corrupt video
    with contextlib.suppress(OSError):
        cached_path.unlink()
    # Delete frame files (named with the content hash, same as
    # extract_video_frames uses).
    frame_dir = cached_path.parent
    for frame_num in (1, 2):
        frame_file = frame_dir / f"{file_hash}.{frame_num}.frame.jpg"
        if frame_file.exists():
            with contextlib.suppress(OSError):
                frame_file.unlink()
                logger.debug("Cleaned up stale frame file: %s", frame_file.name)
