# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video processor — ffmpeg-based transcoding and thumbnail extraction.

Uses software encoding (libx264) by default for the best quality.
Hardware-accelerated encoding (h264_v4l2m2m on Pi) is available as an
opt-in alternative when speed is preferred over quality.

Transcoding is configurable:
- On/off toggle (when off, original file is used directly)
- Target resolution (aspect-ratio-preserving scale-to-fit)
- Quality (CRF for software, bitrate for hardware encoders)
- Software vs hardware encoder selection
- CPU throttling via ``cpulimit`` or ``nice`` to keep the photoframe
  responsive during transcoding
- Transcode timeout (default 2 hours)
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from metixel.backend.processing.utils import nice_cmd
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)


def _get_available_ram_bytes() -> int | None:
    """Read available system RAM from ``/proc/meminfo``.

    Returns ``MemAvailable`` in bytes, or ``None`` if the file cannot
    be read (e.g. on a non-Linux dev machine).
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024  # kB → bytes
    except (OSError, ValueError):
        pass
    return None


class VideoProcessor:
    """Processes video files for display: transcode, extract thumbnail.

    Phase 1: Uses software libx264 by default for the best quality.
    Hardware-accelerated encoding (h264_v4l2m2m on Pi) is available as an
    opt-in alternative when speed is preferred over quality.

    Transcoding is configurable:
    - On/off toggle (when off, original file is used directly)
    - Target resolution (aspect-ratio-preserving scale-to-fit)
    - Quality (CRF for software, bitrate for hardware encoders)
    - Software vs hardware encoder selection
    - CPU throttling via ``cpulimit`` or ``nice`` to keep the photoframe
      responsive during transcoding
    - Transcode timeout (default 2 hours)

    Threshold gating:
    - Use :meth:`needs_optimisation` to check whether a video needs
      transcoding BEFORE calling :meth:`process`.
    - Videos already in H.264 within the resolution limits skip transcoding
      and can go directly to the slideshow playlist.
    """

    #: Known H.264 codec names that skip transcoding when within limits.
    H264_CODECS = {"h264", "avc", "avc1", "h.264", "avc1."}

    def __init__(
        self,
        cache_dir: Path,
        screen_width: int = 1920,
        screen_height: int = 1080,
        video_config: dict[str, Any] | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._video_cache = cache_dir / "videos"
        self._thumb_cache = cache_dir / "thumbnails"
        self._screen_w = screen_width
        self._screen_h = screen_height
        self._video_cache.mkdir(parents=True, exist_ok=True)
        self._thumb_cache.mkdir(parents=True, exist_ok=True)

        # Video transcoding configuration
        self._cfg = video_config or {}
        self._transcoding_enabled = self._cfg.get("transcoding_enabled", True)
        self._transcode_max_w = self._cfg.get("transcode_max_width", 0) or self._screen_w
        self._transcode_max_h = self._cfg.get("transcode_max_height", 0) or self._screen_h
        self._transcode_quality = self._cfg.get("transcode_quality", 23)
        self._force_software_encoder = self._cfg.get("transcode_use_software_encoder", True)
        self._transcode_timeout = self._cfg.get("transcode_timeout_seconds", 7200)
        self._cpu_throttle_enabled = self._cfg.get("cpu_throttle_enabled", True)
        self._cpu_throttle_pct = self._cfg.get("cpu_throttle_percent", 50)

        # Track currently transcoding files (by hash) so we can check guardrails
        self._transcoding: set[str] = set()

    # -- Public API ----------------------------------------------------------

    def is_transcoding(self, file_hash: str) -> bool:
        """Check if a specific video is currently being transcoded."""
        return file_hash in self._transcoding

    def active_transcodes(self) -> set[str]:
        """Return the set of file hashes currently being transcoded.

        Returns a **copy** so callers can iterate safely without holding
        any internal locks.
        """
        return set(self._transcoding)

    @property
    def transcoding_enabled(self) -> bool:
        return self._transcoding_enabled

    def update_config(self, video_config: dict[str, Any]) -> None:
        """Update transcoding settings at runtime without recreating the processor.

        Called by the ``OptimisationQueue`` when the user changes video
        settings via the web UI.  Only the config-derived fields are
        updated — the cache directories and screen dimensions are
        immutable after construction.
        """
        self._cfg = video_config
        self._transcoding_enabled = self._cfg.get("transcoding_enabled", True)
        self._transcode_max_w = self._cfg.get("transcode_max_width", 0) or self._screen_w
        self._transcode_max_h = self._cfg.get("transcode_max_height", 0) or self._screen_h
        self._transcode_quality = self._cfg.get("transcode_quality", 23)
        self._force_software_encoder = self._cfg.get("transcode_use_software_encoder", True)
        self._transcode_timeout = self._cfg.get("transcode_timeout_seconds", 7200)
        self._cpu_throttle_enabled = self._cfg.get("cpu_throttle_enabled", True)
        self._cpu_throttle_pct = self._cfg.get("cpu_throttle_percent", 50)
        logger.debug(
            "VideoProcessor config updated: transcode=%s, max=%dx%d, quality=%d, "
            "sw_encoder=%s, cpu_throttle=%s (%d%%)",
            self._transcoding_enabled, self._transcode_max_w, self._transcode_max_h,
            self._transcode_quality, self._force_software_encoder,
            self._cpu_throttle_enabled, self._cpu_throttle_pct,
        )

    @staticmethod
    def needs_optimisation(
        width: int, height: int,
        codec_name: str = "",
        max_width: int = 0, max_height: int = 0,
    ) -> bool:
        """Check whether a video needs transcoding.

        Args:
            width: Video pixel width.
            height: Video pixel height.
            codec_name: Video codec (e.g. "h264", "hevc").  Videos already
                in H.264 skip transcoding when within resolution limits.
            max_width: Threshold width (0 = use display width).
            max_height: Threshold height (0 = use display height).

        Returns:
            True if the video should be transcoded, False if it's already
            in a compatible format within limits.
        """
        if width <= 0 or height <= 0:
            return True  # Unknown dimensions — transcode to be safe
        if max_width > 0 and width > max_width:
            return True
        if max_height > 0 and height > max_height:
            return True
        # If the codec is already H.264, no need to transcode
        codec_lower = codec_name.lower()
        if codec_lower and codec_lower not in VideoProcessor.H264_CODECS:
            return True
        # Within limits AND H.264 — no optimisation needed
        return False

    def process(self, source_path: Path, source: str = "local") -> MediaItem | None:
        """Process a single video file. Returns MediaItem or None on failure.

        Thumbnail extraction is always attempted first.  If transcoding is
        enabled, the video is transcoded to a cache-friendly H.264 MP4 at
        the configured resolution and quality.  If transcoding is disabled,
        the original file is used directly for playback.

        Returns a MediaItem with the appropriate ``transcode_status`` set
        so the presentation engine can enforce guardrails.
        """
        try:
            file_hash = self._hash_file(source_path)
            cached_path = self._video_cache / f"{file_hash}.mp4"
            thumb_path = self._thumb_cache / f"{file_hash}.jpg"

            # Probe source for metadata
            info = self._probe(source_path)
            logger.info(
                "Processing video: %s (%sx%s, %ss)",
                source_path.name,
                info.get("width"),
                info.get("height"),
                info.get("duration"),
            )

            # Extract thumbnail — always works regardless of transcode setting
            if not thumb_path.exists():
                self._extract_thumbnail(source_path, thumb_path)

            # If transcoding is disabled, use original file
            if not self._transcoding_enabled:
                logger.debug("Transcoding disabled — using original file for %s", source_path.name)
                return self._build_item(
                    source_path, source_path, thumb_path, info, source, file_hash,
                    status=TranscodeStatus.NOT_TRANSCODED,
                )

            # If cached file already exists, validate and use it
            if cached_path.exists():
                if self._validate_cached_video(cached_path):
                    logger.debug("Video already cached (transcoded): %s", file_hash)
                    return self._build_item(
                        source_path, cached_path, thumb_path, info, source, file_hash,
                        status=TranscodeStatus.TRANSCODED,
                    )
                else:
                    logger.warning(
                        "Cached video is corrupt — will re-transcode: %s",
                        cached_path.name,
                    )
                    # Delete the corrupt video AND its stale frame files /
                    # thumbnail so they don't show the wrong content after
                    # re-transcode.
                    self._cleanup_cached_video(cached_path, thumb_path)

            # Mark as transcoding, then transcode
            self._transcoding.add(file_hash)
            try:
                self._transcode(source_path, cached_path)
                status = TranscodeStatus.TRANSCODED
                playback_path = cached_path
                logger.info(
                    "Video transcoded: %s → %s",
                    source_path.name, file_hash,
                )
            except RuntimeError as e:
                logger.warning(
                    "Transcode failed for %s: %s — will play original file",
                    source_path.name, e,
                )
                status = TranscodeStatus.FAILED
                playback_path = source_path
            finally:
                self._transcoding.discard(file_hash)

            return self._build_item(
                source_path, playback_path, thumb_path, info, source, file_hash,
                status=status,
            )

        except Exception:
            logger.exception("Failed to process video: %s", source_path)
            return None

    # -- Helpers -------------------------------------------------------------

    # Minimum free RAM (bytes) required before attempting a transcode.
    # On a Pi 3 (1 GB) or Pi Zero 2 W (512 MB), a single ffmpeg process
    # with libx264 can consume 400–800 MB for a 4K source.  If less than
    # this is available, skip transcoding and fall back to the original file.
    _MIN_FREE_RAM_FOR_TRANSCODE: int = 192 * 1024 * 1024  # 192 MB

    def _transcode(self, source: Path, dest: Path) -> None:
        """Transcode video to H.264 at configured resolution and quality.

        Uses software libx264 by default for the best quality.  Hardware
        encoders (h264_v4l2m2m on Pi) are available as an opt-in alternative
        when speed is preferred over quality.

        The source framerate is always preserved — no forced FPS conversion.

        CPU throttling uses a layered strategy:
        1. ``cpulimit`` — percentage-based limit (requires ``apt install cpulimit``)
        2. ``nice`` + ffmpeg ``-threads`` — priority + thread cap (no extra deps)
        3. ffmpeg ``-threads`` alone — limits decoder/encoder parallelism
        """
        # ── Pre-flight memory check ───────────────────────────────
        avail = _get_available_ram_bytes()
        if avail is not None and avail < self._MIN_FREE_RAM_FOR_TRANSCODE:
            raise RuntimeError(
                f"Insufficient free RAM for transcode "
                f"(available: {avail // (1024*1024)} MB, "
                f"need: {self._MIN_FREE_RAM_FOR_TRANSCODE // (1024*1024)} MB)"
            )

        encoders = self._select_encoders()

        # Build scale filter: scale to fit within target dimensions,
        # preserve aspect ratio, pad to even dimensions.
        max_w = self._transcode_max_w
        max_h = self._transcode_max_h
        scale_filter = (
            f"scale='min({max_w},iw)':'min({max_h},ih)'"
            f":force_original_aspect_ratio=decrease"
            f",pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'"
            f",format=yuv420p"
        )

        # Determine thread limit from throttle percentage
        thread_limit = self._compute_thread_limit()

        for encoder in encoders:
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-c:v", encoder,
                "-vf", scale_filter,
            ]

            # Quality: CRF for software, bitrate for hardware
            if encoder == "libx264":
                crf = max(0, min(51, self._transcode_quality))
                cmd += ["-preset", "fast", "-crf", str(crf)]
                # Apply thread limit to x264 encoder if throttling is active.
                if thread_limit is not None and thread_limit > 0:
                    cmd += ["-x264-params", f"threads={thread_limit}"]
            else:
                # Map CRF-like quality to bitrate: lower CRF → higher bitrate
                q = self._transcode_quality
                if q <= 20:
                    bitrate = "4M"
                elif q <= 24:
                    bitrate = "2M"
                else:
                    bitrate = "1M"
                cmd += ["-b:v", bitrate]

            cmd += [
                "-an",  # Strip audio (photo frame doesn't need it)
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                str(dest),
            ]

            # Apply CPU throttling wrapper (cpulimit or nice)
            final_cmd = self._wrap_with_throttle(cmd)

            timeout = max(60, self._transcode_timeout)
            try:
                # MEMORY-SAFE: redirect stdout+stderr to DEVNULL instead
                # of capturing them in RAM.  ffmpeg writes extensive
                # progress lines (frame count, fps, bitrate, …) to stderr
                # — for a long transcode (up to 2 hours) this could
                # accumulate hundreds of MB in memory on a Pi with only
                # 512 MB RAM.  The actual video output goes to a file, so
                # nothing of value is lost.
                subprocess.run(
                    final_cmd, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                logger.debug(
                    "Transcoded with %s (threads=%s, timeout=%ds): %s",
                    encoder, thread_limit, timeout, source.name,
                )
                return  # Success — done
            except subprocess.CalledProcessError as e:
                logger.warning(
                    "Encoder %s failed for %s (rc=%d)",
                    encoder, source.name, e.returncode,
                )
                # Clean up partial output
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                # Continue to next encoder
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Encoder %s timed out for %s (%ds limit)",
                    encoder, source.name, timeout,
                )
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
            except Exception:
                logger.exception("Unexpected error during transcode with %s", encoder)
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass

        raise RuntimeError(f"All encoders failed for: {source.name}")

    def _wrap_with_throttle(self, cmd: list[str]) -> list[str]:
        """Wrap an ffmpeg command with CPU throttling.

        Layered strategy:
        1. ``nice -n 19`` — lowest scheduling priority (ALWAYS applied
           when available, regardless of throttle settings).  The kernel
           gives the frontend render loop priority, so the slideshow
           never stutters — even at 100% CPU with throttling disabled.
        2. ``cpulimit -l N%`` — hard percentage cap (only when
           ``cpu_throttle_enabled`` is true).  Adds a ceiling when you
           want guaranteed headroom beyond what nice provides.

        The ``-threads`` limit is applied directly to ffmpeg args in
        :meth:`_transcode` when throttling is enabled.
        """
        # Strategy 1: nice — ALWAYS apply via the shared utility.
        # This ensures the frontend slideshow always gets CPU priority
        # over transcoding.  It costs nothing and prevents stutter
        # without any hard CPU cap.
        cmd = nice_cmd(cmd)

        if not self._cpu_throttle_enabled:
            return cmd

        # Strategy 2: cpulimit — hard CPU ceiling (only when enabled).
        if shutil.which("cpulimit"):
            limit = max(5, min(1000, self._cpu_throttle_pct))
            logger.debug("Throttling transcode to %d%% CPU via cpulimit", limit)
            return [
                "cpulimit",
                "-l", str(limit),
                "-f",  # foreground: wait for child to exit (CRITICAL!)
                "--"] + cmd

        logger.debug("cpulimit not installed — using nice + -threads only")
        return cmd

    def _compute_thread_limit(self) -> int | None:
        """Compute ffmpeg thread limit from the throttle percentage.

        Maps the user-facing percentage (0-1000, representing percentage
        of a single core) to a concrete thread count.  Returns ``None``
        if no limit should be applied (throttle disabled or > 4 cores).

        Mapping:
        -   1–100  → 1 thread  (up to 1 core)
        - 101–200  → 2 threads (up to 2 cores)
        - 201–300  → 3 threads (up to 3 cores)
        - 301–400  → 4 threads (up to 4 cores)
        - 401+     → None (auto, use all cores)
        """
        if not self._cpu_throttle_enabled:
            return None

        pct = max(1, min(1000, self._cpu_throttle_pct))
        cores = os.cpu_count() or 4

        if pct >= 401:
            return None  # Let ffmpeg auto-detect (4+ cores worth)

        # Each 100 = 1 core worth of CPU
        threads = (pct + 99) // 100  # ceil division
        return min(threads, cores)

    def _extract_thumbnail(self, source: Path, dest: Path) -> None:
        """Extract a thumbnail frame at 2 seconds into the video.

        Uses fast (keyframe) seeking with ``-ss`` before ``-i`` plus
        ``-noaccurate_seek`` to avoid decoding from the start.  A
        generous timeout accommodates heavy 4K sources on a Pi 2/3
        where software decode of a single frame can take >30 seconds.
        """
        cmd = nice_cmd([
            "ffmpeg",
            "-y",
            "-noaccurate_seek",
            "-ss", "2",
            "-i", str(source),
            "-vframes", "1",
            "-q:v", "2",
            str(dest),
        ])
        # Use the same timeout as probing (both are one-shot ffmpeg
        # invocations that shouldn't take minutes, but 4K on a Pi 3
        # can legitimately need over 30 seconds to initialise the
        # decoder and seek to the first keyframe).
        subprocess.run(
            cmd, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120,
        )

    def _probe(self, path: Path) -> dict:
        """Probe video file for metadata using ffprobe."""
        cmd = nice_cmd([
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        import json

        data = json.loads(result.stdout)
        info: dict = {"width": 0, "height": 0, "duration": 0.0}
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
                break
        info["duration"] = float(data.get("format", {}).get("duration", 0))
        return info

    def _select_encoders(self) -> list[str]:
        """Return the H.264 encoder(s) to try, in priority order.

        When ``transcode_use_software_encoder`` is True (the default),
        only libx264 is used — it produces far better quality at the same
        bitrate than Pi hardware encoders.

        When False, hardware encoders are tried first with libx264 as a
        fallback.  This is useful when transcoding speed matters more
        than quality (e.g. batch-processing many short clips).
        """
        if self._force_software_encoder:
            logger.debug("Software encoder forced — using libx264 only")
            return ["libx264"]

        # Detect available hardware encoders
        encoders: list[str] = []
        try:
            result = subprocess.run(
                ["ffmpeg", "-encoders"],
                capture_output=True, text=True, timeout=10,
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

    @staticmethod
    def _validate_cached_video(path: Path) -> bool:
        """Check that a cached video file is valid (not corrupt/partial).

        Runs a quick ``ffprobe`` to verify the file has a readable video
        stream.  Returns ``True`` if the file is valid.
        """
        try:
            result = subprocess.run(
                nice_cmd(["ffprobe", "-v", "error",
                 "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0",
                 str(path)]),
                capture_output=True, text=True, timeout=15,
            )
            return result.returncode == 0 and "video" in result.stdout.lower()
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _cleanup_cached_video(cached_path: Path, thumb_path: Path) -> None:
        """Delete a corrupt cached video and all associated artifacts.

        Removes the video file, its thumbnail, and any frame cache files
        (``.1.frame`` / ``.2.frame``) so stale frames don't appear after
        re-transcode.
        """
        # Delete the corrupt video
        with contextlib.suppress(OSError):
            cached_path.unlink()
        # Delete the thumbnail
        with contextlib.suppress(OSError):
            thumb_path.unlink()
        # Delete frame files (stored in the same directory with a
        # path-based hash that differs from the content hash).
        frame_dir = cached_path.parent
        path_hash = hashlib.sha256(str(cached_path).encode()).hexdigest()[:16]
        for frame_num in (1, 2):
            frame_file = frame_dir / f"{path_hash}.{frame_num}.frame"
            if frame_file.exists():
                with contextlib.suppress(OSError):
                    frame_file.unlink()
                    logger.debug("Cleaned up stale frame file: %s", frame_file.name)

    @staticmethod
    def _hash_file(path: Path) -> str:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            sha.update(f.read(1024 * 1024))
            f.seek(-1024, 2)
            sha.update(f.read(1024))
        return sha.hexdigest()[:16]

    def _build_item(
        self,
        source: Path,
        cached: Path,
        thumb: Path,
        info: dict,
        source_name: str,
        file_hash: str,
        *,
        status: TranscodeStatus,
    ) -> MediaItem:
        return MediaItem(
            id=file_hash,
            original_path=source,
            cached_path=cached,
            media_type=MediaType.VIDEO,
            width=info.get("width", 0),
            height=info.get("height", 0),
            duration_seconds=info.get("duration", 0.0),
            thumbnail_path=thumb,
            source=source_name,
            transcode_status=status,
        )
