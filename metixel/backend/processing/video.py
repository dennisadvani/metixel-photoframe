# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video processor — ffmpeg-based transcoding and thumbnail extraction.

Uses hardware-accelerated encoding (h264_v4l2m2m) on Raspberry Pi where
available, with software fallback (libx264) on other platforms.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from metixel.shared.models import MediaItem, MediaType

logger = logging.getLogger(__name__)


class VideoProcessor:
    """Processes video files for display: transcode, extract thumbnail.

    Phase 1: Uses ``h264_v4l2m2m`` hardware encoder on Raspberry Pi.
    Phase 2: Uses VA-API or software encoding depending on platform.
    """

    def __init__(self, cache_dir: Path, screen_width: int = 1920, screen_height: int = 1080) -> None:
        self._cache_dir = cache_dir
        self._video_cache = cache_dir / "videos"
        self._thumb_cache = cache_dir / "thumbnails"
        self._screen_w = screen_width
        self._screen_h = screen_height
        self._video_cache.mkdir(parents=True, exist_ok=True)
        self._thumb_cache.mkdir(parents=True, exist_ok=True)

    def process(self, source_path: Path, source: str = "local") -> MediaItem | None:
        """Process a single video file. Returns MediaItem or None on failure.

        Thumbnail extraction is tried first (a quick one-shot ffmpeg frame
        grab that works even when transcoding fails).  Transcoding to a
        cache-friendly H.264 MP4 is best-effort — if it fails, the original
        file is used for playback and a warning is logged.
        """
        try:
            file_hash = self._hash_file(source_path)
            cached_path = self._video_cache / f"{file_hash}.mp4"
            thumb_path = self._thumb_cache / f"{file_hash}.jpg"

            if cached_path.exists() and thumb_path.exists():
                logger.debug("Video already cached: %s", file_hash)
                info = self._probe(source_path)
                return self._build_item(source_path, cached_path, thumb_path, info, source, file_hash)

            # Probe source
            info = self._probe(source_path)
            logger.info("Processing video: %s (%sx%s, %ss)",
                        source_path.name, info.get("width"), info.get("height"), info.get("duration"))

            # Extract thumbnail first — always works (one-shot ffmpeg frame grab).
            # This is critical: even if transcoding fails later, the engine can
            # show a thumbnail instead of crashing with PIL.UnidentifiedImageError.
            self._extract_thumbnail(source_path, thumb_path)

            # Transcode — best-effort.  If all encoders fail we still return a
            # MediaItem pointing at the original file so the video can be played
            # directly (like picframe does).
            transcoded = False
            try:
                self._transcode(source_path, cached_path)
                transcoded = True
            except RuntimeError as e:
                logger.warning("Transcode failed for %s: %s — will play original file", source_path.name, e)

            playback_path = cached_path if transcoded else source_path
            logger.info(
                "Video processed: %s → %s (transcoded=%s)",
                source_path.name, file_hash, transcoded,
            )
            return self._build_item(source_path, playback_path, thumb_path, info, source, file_hash)

        except Exception:
            logger.exception("Failed to process video: %s", source_path)
            return None

    # -- Helpers -------------------------------------------------------------

    def _transcode(self, source: Path, dest: Path) -> None:
        """Transcode video to H.264 at screen resolution.

        Tries the hardware encoder first (e.g. h264_v4l2m2m on Pi).
        Falls back to software libx264 if the hardware encoder fails
        (which can happen even when the codec is listed as available).
        """
        encoders = self._detect_encoders()

        # Scale to fit within screen, preserve aspect ratio, then pad to
        # even dimensions.  Hardware encoders (V4L2, VA-API) require even
        # width & height; the pad filter adds at most 1 px to each axis.
        # Finally, force yuv420p — the only pixel format all H.264 encoders
        # guarantee support for.
        scale_filter = (
            f"scale='min({self._screen_w},iw)':'min({self._screen_h},ih)'"
            f":force_original_aspect_ratio=decrease"
            f",pad='ceil(iw/2)*2:ceil(ih/2)*2:(ow-iw)/2:(oh-ih)/2'"
            f",format=yuv420p"
        )

        for encoder in encoders:
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-c:v", encoder,
                "-vf", scale_filter,
            ]

            # Hardware encoders don't support CRF; use bitrate instead.
            # libx264 gets CRF + preset for a good quality/size trade-off.
            if encoder == "libx264":
                cmd += ["-preset", "fast", "-crf", "23"]
            else:
                cmd += ["-b:v", "2M"]

            cmd += [
                "-r", "30",
                "-an",  # Strip audio (photo frame doesn't need it)
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                str(dest),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
                logger.debug("Transcoded with %s: %s", encoder, source.name)
                return  # Success — done
            except subprocess.CalledProcessError as e:
                stderr_tail = e.stderr.decode(errors="replace")[-200:] if e.stderr else "(no stderr)"
                logger.warning(
                    "Encoder %s failed for %s (rc=%d): %s",
                    encoder, source.name, e.returncode, stderr_tail,
                )
                # Continue to next encoder
            except Exception:
                logger.exception("Unexpected error during transcode with %s", encoder)
                # Continue to next encoder

        raise RuntimeError(f"All encoders failed for: {source.name}")

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

    @staticmethod
    def _detect_encoders() -> list[str]:
        """Return available H.264 encoders in priority order.

        Hardware encoders first, with software ``libx264`` always last
        as a guaranteed fallback.
        """
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
        self, source: Path, cached: Path, thumb: Path, info: dict, source_name: str, file_hash: str
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
        )
