# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Pure ffmpeg/ffprobe command builders — no subprocess execution here.

Extracted from ``VideoProcessor`` (Phase 2 decomposition).  The transcode,
thumbnail, frame-extraction and probe command lists are assembled here so
they can be unit-tested and reviewed without running ffmpeg.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from metixel.backend.processing.utils import nice_cmd
from metixel.shared.system_stats import read_meminfo

logger = logging.getLogger(__name__)


def _scale_filter(screen_w: int, screen_h: int) -> str:
    """Aspect-preserving scale + even-dimension pad filter (screen-sized)."""
    return (
        f"scale='min({screen_w},iw)':'min({screen_h},ih)'"
        f":force_original_aspect_ratio=decrease,"
        f"pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'"
    )


def probe_cmd(path: Path) -> list[str]:
    """ffprobe JSON command for a video file."""
    return [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def validate_cmd(path: Path) -> list[str]:
    """Quick ffprobe command checking a file has a readable video stream."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]


def thumbnail_cmd(source: Path, dest: Path, screen_w: int, screen_h: int) -> list[str]:
    """Extract a thumbnail frame at 2 seconds (fast keyframe seek)."""
    return [
        "ffmpeg",
        "-y",
        "-noaccurate_seek",
        "-ss",
        "2",
        "-i",
        str(source),
        "-vf",
        _scale_filter(screen_w, screen_h),
        "-vframes",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]


def first_frame_cmd(source: Path, dest: Path, screen_w: int, screen_h: int) -> list[str]:
    """Extract the first frame (t=0, keyframe seek)."""
    return [
        "ffmpeg",
        "-y",
        "-noaccurate_seek",
        "-ss",
        "0",
        "-i",
        str(source),
        "-vf",
        _scale_filter(screen_w, screen_h),
        "-vframes",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2",
        str(dest),
    ]


def last_frame_cmd(source: Path, dest: Path, screen_w: int, screen_h: int) -> list[str]:
    """Extract the final frame (``-sseof -1`` + ``-update 1``)."""
    return [
        "ffmpeg",
        "-y",
        "-sseof",
        "-1",
        "-i",
        str(source),
        "-vf",
        _scale_filter(screen_w, screen_h),
        "-q:v",
        "2",
        "-f",
        "image2",
        "-update",
        "1",
        str(dest),
    ]


def transcode_cmd(
    source: Path,
    dest: Path,
    encoder: str,
    profile: dict[str, Any],
    info: dict | None,
    *,
    transcode_quality: int,
    thread_limit: int | None,
    keep_audio: bool,
    fallback_max_w: int,
    fallback_max_h: int,
) -> list[str]:
    """Build the ffmpeg transcode command for a single encoder (no execution).

    ``fallback_max_w`` / ``fallback_max_h`` are used when the profile omits
    the resolution keys (mirrors the original ``VideoProcessor._transcode``).
    """
    max_w = profile.get("max_width", fallback_max_w)
    max_h = profile.get("max_height", fallback_max_h)
    max_fps = profile.get("max_fps", 0)
    h264_level = str(profile.get("h264_level", ""))
    h264_profile = profile.get("h264_profile", "high")
    color_depth = profile.get("color_depth", 8)
    hdr_support = profile.get("hdr_support", False)

    scale_filter = (
        f"scale='min({max_w},iw)':'min({max_h},ih)'"
        f":force_original_aspect_ratio=decrease"
        f",pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'"
    )
    # Color depth: use source depth, capped to profile limit.
    # Never upscale 8-bit → 10-bit — just wastes bitrate.
    src_depth = (info or {}).get("color_depth", 8) or 8
    out_depth = min(src_depth, color_depth)
    if out_depth >= 10:
        scale_filter += ",format=yuv420p10le"
    else:
        scale_filter += ",format=yuv420p"

    cmd = ["ffmpeg", "-y", "-i", str(source), "-c:v", encoder, "-vf", scale_filter]

    if encoder in ("libx264", "libx265"):
        crf = max(0, min(51, transcode_quality))
        preset = "fast"
        if encoder == "libx265":
            preset = _libx265_preset()
        # Use profile CRF if set, otherwise fall back to the global
        # transcode_quality config value.
        effective_crf = profile.get("crf", crf)
        cmd += ["-preset", preset, "-crf", str(effective_crf)]

        if thread_limit is not None and thread_limit > 0:
            param_key = "-x264-params" if encoder == "libx264" else "-x265-params"
            cmd += [param_key, f"threads={thread_limit}"]
    else:
        q = transcode_quality
        bitrate = "2M" if q <= 24 else "1M"
        cmd += ["-b:v", bitrate]

    # Profile constraints for smooth Pi playback
    if encoder == "libx264" and h264_level:
        cmd += ["-level", h264_level]
    if encoder == "libx264" and h264_profile:
        cmd += ["-profile:v", h264_profile]
    if encoder in ("libx264", "libx265"):
        cmd += ["-refs", "2", "-g", "30"]

    # Framerate: always set explicitly to prevent ffmpeg from silently
    # changing the output FPS (observed: 23.98→29.97).  Cap to profile
    # max, but never upscale.
    src_fps = (info or {}).get("fps", 0) or 0
    if src_fps > 0:
        target_fps = min(src_fps, max_fps) if max_fps else src_fps
        cmd += ["-r", str(target_fps)]

    # Audio: keep or strip
    if not keep_audio:
        cmd += ["-an"]

    # HDR → SDR downgrade
    if not hdr_support:
        cmd += [
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
        ]

    # Max bitrate constraint — never exceed source quality.
    max_br = profile.get("max_bitrate", 0)
    src_br = (info or {}).get("bitrate", 0) or 0
    if max_br and max_br > 0:
        effective_max = min(src_br, max_br) if src_br else max_br
        cmd += ["-maxrate", f"{effective_max}M", "-bufsize", f"{effective_max * 2}M"]

    cmd += [
        "-movflags",
        "+faststart",
        str(dest),
    ]
    return cmd


def _libx265_preset() -> str:
    """Pick a lighter libx265 preset on memory-constrained devices (≤3GB).

    libx265 uses 2–3× more RAM than libx264 at the same preset, so use a
    lighter preset on devices with ≤2GB to avoid OOM.
    """
    total_ram = read_meminfo().get("MemTotal", 0) * 1024
    return "ultrafast" if total_ram > 0 and total_ram <= 3 * 1024 * 1024 * 1024 else "superfast"


def wrap_with_throttle(
    cmd: list[str],
    cpu_throttle_enabled: bool,
    cpu_throttle_pct: int,
) -> list[str]:
    """Wrap an ffmpeg command with CPU throttling.

    Layered strategy:
    1. ``nice -n 19`` — lowest scheduling priority (ALWAYS applied when
       available, regardless of throttle settings).  The kernel gives the
       frontend render loop priority, so the slideshow never stutters —
       even at 100% CPU with throttling disabled.
    2. ``cpulimit -l N%`` — hard percentage cap (only when
       ``cpu_throttle_enabled`` is true).
    """
    # Strategy 1: nice — ALWAYS apply via the shared utility.
    cmd = nice_cmd(cmd)

    if not cpu_throttle_enabled:
        return cmd

    # Strategy 2: cpulimit — hard CPU ceiling (only when enabled).
    if shutil.which("cpulimit"):
        limit = max(5, min(1000, cpu_throttle_pct))
        logger.debug("Throttling transcode to %d%% CPU via cpulimit", limit)
        return [
            "cpulimit",
            "-l",
            str(limit),
            "-f",  # foreground: wait for child to exit (CRITICAL!)
            "--",
        ] + cmd

    logger.debug("cpulimit not installed — using nice + -threads only")
    return cmd


def compute_thread_limit(cpu_throttle_enabled: bool, cpu_throttle_pct: int) -> int | None:
    """Compute ffmpeg thread limit from the throttle percentage.

    Maps the user-facing percentage (0-1000, representing percentage of a
    single core) to a concrete thread count.  Returns ``None`` if no limit
    should be applied (throttle disabled or > 4 cores).

    Mapping:
    -   1–100  → 1 thread  (up to 1 core)
    - 101–200  → 2 threads (up to 2 cores)
    - 201–300  → 3 threads (up to 3 cores)
    - 301–400  → 4 threads (up to 4 cores)
    - 401+     → None (auto, use all cores)
    """
    if not cpu_throttle_enabled:
        return None

    pct = max(1, min(1000, cpu_throttle_pct))
    cores = os.cpu_count() or 4

    if pct >= 401:
        return None  # Let ffmpeg auto-detect (4+ cores worth)

    # Each 100 = 1 core worth of CPU
    threads = (pct + 99) // 100  # ceil division
    return min(threads, cores)


def select_encoders(force_software_encoder: bool, timeout: int) -> list[str]:
    """Return the H.264 encoder(s) to try, in priority order.

    When ``force_software_encoder`` is True (the default), only libx264 is
    used — it produces far better quality at the same bitrate than Pi
    hardware encoders.

    When False, hardware encoders are tried first with libx264 as a
    fallback.
    """
    if force_software_encoder:
        logger.debug("Software encoder forced — using libx264 only")
        return ["libx264"]

    # Detect available hardware encoders
    encoders: list[str] = []
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if "h264_v4l2m2m" in result.stdout:
            encoders.append("h264_v4l2m2m")
        if "h264_mmal" in result.stdout:
            encoders.append("h264_mmal")
        if "h264_vaapi" in result.stdout:
            encoders.append("h264_vaapi")
    except Exception:
        pass
    # Software fallback always available
    encoders.append("libx264")
    logger.debug("Available video encoders: %s", encoders)
    return encoders
