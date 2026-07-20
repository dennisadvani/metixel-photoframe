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

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)


class VideoProcessor:
    """Processes video files for display: transcode, extract thumbnail.

    Phase 1: Uses ``h264_v4l2m2m`` hardware encoder on Raspberry Pi.
    Phase 2: Uses VA-API or software encoding depending on platform.
    """

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

    @property
    def transcoding_enabled(self) -> bool:
        return self._transcoding_enabled

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

            # If cached file already exists, it's already transcoded
            if cached_path.exists():
                logger.debug("Video already cached (transcoded): %s", file_hash)
                return self._build_item(
                    source_path, cached_path, thumb_path, info, source, file_hash,
                    status=TranscodeStatus.TRANSCODED,
                )

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
            ]

            # -- Decoder thread limit (applies to ALL encoders) ---------
            # Limits CPU used for decoding + scaling, which is the main
            # CPU cost on Pi (hardware encoder handles the actual encode).
            if thread_limit is not None and thread_limit > 0:
                cmd += ["-threads", str(thread_limit)]

            cmd += [
                "-i", str(source),
                "-c:v", encoder,
                "-vf", scale_filter,
            ]

            # Quality: CRF for software, bitrate for hardware
            if encoder == "libx264":
                crf = max(0, min(51, self._transcode_quality))
                cmd += ["-preset", "fast", "-crf", str(crf)]
                # Software encoder: also limit encoding threads
                if thread_limit is not None and thread_limit > 0:
                    # libx264 uses a separate -threads after the codec
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
                proc = subprocess.run(
                    final_cmd, check=True, capture_output=True, timeout=timeout,
                )
                logger.debug(
                    "Transcoded with %s (threads=%s, timeout=%ds): %s",
                    encoder, thread_limit, timeout, source.name,
                )
                return  # Success — done
            except subprocess.CalledProcessError as e:
                stderr_tail = (
                    e.stderr.decode(errors="replace")[-200:]
                    if e.stderr else "(no stderr)"
                )
                logger.warning(
                    "Encoder %s failed for %s (rc=%d): %s",
                    encoder, source.name, e.returncode, stderr_tail,
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
        """Wrap an ffmpeg command with CPU throttling if enabled.

        Layered strategy:
        1. ``cpulimit -l N%`` — precise percentage-based (requires install)
        2. ``nice -n 19`` — lowest scheduling priority (always available)
        The ``-threads`` limit is applied directly to ffmpeg args in
        :meth:`_transcode`, so it works even without external tools.
        """
        if not self._cpu_throttle_enabled:
            return cmd

        # Strategy 1: cpulimit — precise percentage-based throttling
        if shutil.which("cpulimit"):
            limit = max(5, min(100, self._cpu_throttle_pct))
            logger.debug("Throttling transcode to %d%% CPU via cpulimit", limit)
            return [
                "cpulimit",
                "-l", str(limit),
                "--"] + cmd

        # Strategy 2: nice — lowest scheduling priority
        if shutil.which("nice"):
            logger.debug("Throttling transcode via nice -n 19 (lowest priority) + ffmpeg -threads")
            return ["nice", "-n", "19"] + cmd

        # Neither available — -threads alone provides some limiting
        logger.debug(
            "CPU throttling: using ffmpeg -threads limit only "
            "(cpulimit not installed, nice not found)"
        )
        return cmd

    def _compute_thread_limit(self) -> int | None:
        """Compute ffmpeg thread limit from the throttle percentage.

        Maps the user-facing 0-100% slider to a concrete thread count.
        Returns ``None`` if no limit should be applied (100% or disabled).

        Mapping (for a 4-core Pi):
        - 10-25%  → 1 thread  (light load)
        - 30-50%  → 2 threads (half cores)
        - 55-75%  → 3 threads
        - 80-100% → None (auto, use all cores)
        """
        if not self._cpu_throttle_enabled:
            return None

        pct = max(5, min(100, self._cpu_throttle_pct))
        if pct >= 85:
            return None  # Let ffmpeg auto-detect

        import os
        cores = os.cpu_count() or 4

        if pct <= 30:
            return 1
        elif pct <= 55:
            return max(1, cores // 2)
        elif pct <= 80:
            return max(1, (cores * 3) // 4)
        else:
            return None

    def _extract_thumbnail(self, source: Path, dest: Path) -> None:
        """Extract a thumbnail frame at 2 seconds into the video."""
        cmd = [
            "ffmpeg",
            "-y",
            "-ss", "2",
            "-i", str(source),
            "-vframes", "1",
            "-q:v", "2",
            str(dest),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)

    def _probe(self, path: Path) -> dict:
        """Probe video file for metadata using ffprobe."""
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
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
