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


def _detect_pi_model() -> str | None:
    """Detect the Raspberry Pi model from ``/proc/device-tree/model``.

    Returns the profile key (``pi2``, ``pi3``, ``pi4``, ``pi5``) or
    ``None`` if the model can't be determined.
    """
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00").strip()
    except (OSError, FileNotFoundError):
        return None

    model_lower = model.lower()
    if "raspberry pi 5" in model_lower:
        return "pi5"
    if "raspberry pi 4" in model_lower or "raspberry pi 400" in model_lower:
        return "pi4"
    if "raspberry pi 3" in model_lower:
        return "pi3"
    if "raspberry pi 2" in model_lower:
        return "pi2"
    if "raspberry pi zero 2" in model_lower:
        return "pi3"   # Zero 2 W has similar VideoCore IV to Pi 3
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

    #: Known H.265 / HEVC codec names.
    HEVC_CODECS = {"hevc", "h265", "h.265", "hev1", "hvc1"}

    # -- Transcoding profiles -------------------------------------------------

    PROFILES: dict[str, dict[str, Any]] = {
        "pi2": {
            "label": "Raspberry Pi 2",
            "codec": "h264",
            "encoder": "libx264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 8,   # Mbps
            "bitrate_target": True,  # Use -b:v (target bitrate) instead of CRF
            "h264_profile": "high",
            "h264_level": "4.0",
            "color_depth": 8,
            "hdr_support": False,
        },
        "pi3": {
            "label": "Raspberry Pi 3 / 3B+",
            "codec": "h264",
            "encoder": "libx264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,        # Hard limit — see project requirements
            "max_bitrate": 8,      # Software decode limit: ARM cores tap out ~8-10 Mbps
            "bitrate_target": True,  # Use -b:v (target bitrate) instead of CRF
            "h264_profile": "high",
            "h264_level": "4.0",   # Pi 3 VideoCore IV HW decode limit: Level 4.1
            "color_depth": 8,
            "hdr_support": False,
        },
        "pi4": {
            "label": "Raspberry Pi 4 / 400",
            "codec": "h265",
            "encoder": "libx265",
            "max_width": 3840,
            "max_height": 2160,
            "max_fps": 60,
            "max_bitrate": 40,
            "h264_profile": "high",    # Not used for H.265, informational
            "h264_level": "5.1",
            "color_depth": 10,
            "hdr_support": True,
        },
        "pi5": {
            "label": "Raspberry Pi 5",
            "codec": "h265",
            "encoder": "libx265",
            "max_width": 3840,
            "max_height": 2160,
            "max_fps": 60,
            "max_bitrate": 80,
            "h264_profile": "high",
            "h264_level": "5.2",
            "color_depth": 10,
            "hdr_support": True,
        },
    }

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
        self._cpu_throttle_pct = self._cfg.get("cpu_throttle_percent", 100)

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

    def _resolve_profile(self) -> dict[str, Any] | None:
        """Resolve the effective transcoding profile from config.

        Returns the full profile dict (with codec, max_width, etc.) or
        None if transcoding is disabled or no profile is configured.
        """
        if not self._transcoding_enabled:
            return None

        profile_key = (self._cfg.get("transcoding_profile") or "").strip()
        if not profile_key:
            profile_key = _detect_pi_model() or "pi3"

        if profile_key == "custom":
            return {
                "profile": "custom",
                "label": "Custom",
                "codec": self._cfg.get("transcode_codec", "h264"),
                "encoder": "libx264" if self._cfg.get("transcode_codec", "h264") == "h264" else "libx265",
                "max_width": self._cfg.get("transcode_max_width", 0) or self._screen_w,
                "max_height": self._cfg.get("transcode_max_height", 0) or self._screen_h,
                "max_fps": self._cfg.get("transcode_max_fps", 30),
                "max_bitrate": self._cfg.get("transcode_max_bitrate", 20),
                "h264_profile": self._cfg.get("transcode_h264_profile", "high"),
                "h264_level": str(self._cfg.get("transcode_h264_level", "4.2")),
                "color_depth": self._cfg.get("transcode_color_depth", 8),
                "hdr_support": self._cfg.get("transcode_hdr_support", False),
            }

        return dict(VideoProcessor.PROFILES.get(profile_key, VideoProcessor.PROFILES["pi3"]))

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
        self._cpu_throttle_pct = self._cfg.get("cpu_throttle_percent", 100)
        logger.debug(
            "VideoProcessor config updated: transcode=%s, max=%dx%d, quality=%d, "
            "sw_encoder=%s, cpu_throttle=%s (%d%%)",
            self._transcoding_enabled, self._transcode_max_w, self._transcode_max_h,
            self._transcode_quality, self._force_software_encoder,
            self._cpu_throttle_enabled, self._cpu_throttle_pct,
        )

    @staticmethod
    def needs_optimisation(
        probe_info: dict,
        profile: dict[str, Any] | None = None,
    ) -> bool:
        """Check whether a video needs transcoding against profile limits.

        Args:
            probe_info: Dict from ``_probe()`` with width, height, codec_name,
                        fps, bitrate, color_depth, h264_profile, h264_level,
                        color_primaries, color_trc, colorspace, pix_fmt.
            profile: Resolved transcoding profile dict.  If None, falls back
                     to basic H.264 + resolution check.

        Returns:
            True if the video needs transcoding.
        """
        w = probe_info.get("width", 0) or 0
        h = probe_info.get("height", 0) or 0
        if w <= 0 or h <= 0:
            return True

        # Basic check (no profile — legacy behaviour)
        if profile is None:
            codec_lower = (probe_info.get("codec_name", "") or "").lower()
            return codec_lower not in VideoProcessor.H264_CODECS

        # ── Profile-based check ───────────────────────────────────
        target_codec = profile.get("codec", "h264")
        source_codec = (probe_info.get("codec_name", "") or "").lower()

        # Codec check
        if target_codec == "h264" and source_codec not in VideoProcessor.H264_CODECS:
            return True
        if target_codec == "h265" and source_codec not in VideoProcessor.HEVC_CODECS:
            return True

        # Resolution
        if profile.get("max_width", 0) and w > profile["max_width"]:
            return True
        if profile.get("max_height", 0) and h > profile["max_height"]:
            return True

        # Framerate
        max_fps = profile.get("max_fps", 0)
        src_fps = probe_info.get("fps", 0) or 0
        if max_fps and src_fps > max_fps:
            return True

        # Bitrate (Mbps) — allow 10 % tolerance because CRF encoding
        # cannot precisely target an average bitrate.
        max_br = profile.get("max_bitrate", 0)
        src_br = probe_info.get("bitrate", 0) or 0
        if max_br and src_br > int(max_br * 1.1):
            return True

        # Color depth
        target_depth = profile.get("color_depth", 8)
        src_depth = probe_info.get("color_depth", 8) or 8
        if src_depth > target_depth:
            return True

        # HDR → SDR downgrade
        if not profile.get("hdr_support", False) and probe_info.get("color_trc", ""):
            trc = probe_info["color_trc"]
            if trc in ("smpte2084", "arib-std-b67", "smpte428", "bt2020-10"):
                return True  # HDR source on non-HDR-capable Pi

        # H.264 Profile/Level check (for H.264 sources on H.264 profiles)
        if target_codec == "h264" and source_codec in VideoProcessor.H264_CODECS:
            target_level = profile.get("h264_level", "")
            src_level = probe_info.get("h264_level", "") or ""
            if target_level and src_level:
                # Compare numeric level (e.g. "4.2" > "4.0" → needs transcode)
                try:
                    if float(src_level) > float(target_level):
                        return True
                except (ValueError, TypeError):
                    pass

        # Within all limits — no optimisation needed
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

            # Extract first + last frames for slideshow preload/swap.
            # These are required by the frontend — videos without cached
            # frames are excluded from the playlist via is_ready_to_play.
            first_frame, last_frame = self._extract_video_frames(
                source_path, file_hash,
            )

            # If the source is within all profile limits, skip transcode.
            codec_name = info.get("codec_name", "")
            vw = info.get("width", 0) or 0
            vh = info.get("height", 0) or 0

            # Resolve the effective transcoding profile
            profile = self._resolve_profile()

            # Determine if optimised enough
            needs_opt = VideoProcessor.needs_optimisation(info, profile)
            if not needs_opt:
                logger.debug(
                    "Video already optimal (%s %dx%d) — "
                    "skipping transcode for %s",
                    codec_name, vw, vh, source_path.name,
                )
                return self._build_item(
                    source_path, source_path, thumb_path, info, source, file_hash,
                    status=TranscodeStatus.NOT_TRANSCODED,
                    first_frame=first_frame, last_frame=last_frame,
                )

            # If transcoding is disabled, use original file
            if not self._transcoding_enabled:
                logger.debug("Transcoding disabled — using original file for %s", source_path.name)
                return self._build_item(
                    source_path, source_path, thumb_path, info, source, file_hash,
                    status=TranscodeStatus.NOT_TRANSCODED,
                    first_frame=first_frame, last_frame=last_frame,
                )

            # If cached file already exists, validate it against the current
            # profile.  A profile change (e.g. Pi 4 → Pi 3) makes previously
            # cached files too large / wrong codec — they must be re-transcoded.
            if cached_path.exists():
                if self._validate_cached_video(cached_path):
                    cached_info = self._probe(cached_path)
                    if not VideoProcessor.needs_optimisation(cached_info, profile):
                        logger.debug("Cached video still valid for current profile: %s", file_hash)
                        return self._build_item(
                            source_path, cached_path, thumb_path, info, source, file_hash,
                            status=TranscodeStatus.TRANSCODED,
                            first_frame=first_frame, last_frame=last_frame,
                        )
                    else:
                        logger.info(
                            "Cached video exceeds new profile limits — re-transcoding: %s",
                            cached_path.name,
                        )
                        self._cleanup_cached_video(cached_path, thumb_path)
                else:
                    logger.warning(
                        "Cached video is corrupt — will re-transcode: %s",
                        cached_path.name,
                    )
                    self._cleanup_cached_video(cached_path, thumb_path)

            # Mark as transcoding, then transcode
            self._transcoding.add(file_hash)
            try:
                self._transcode(source_path, cached_path, info)
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
                first_frame=first_frame, last_frame=last_frame,
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

    def _transcode(self, source: Path, dest: Path, info: dict | None = None) -> None:
        """Transcode video to the profile's optimal format."""
        profile = self._resolve_profile()
        if profile is None:
            profile = VideoProcessor.PROFILES.get("pi3", VideoProcessor.PROFILES["pi3"])

        avail = _get_available_ram_bytes()
        if avail is not None and avail < self._MIN_FREE_RAM_FOR_TRANSCODE:
            raise RuntimeError(
                f"Insufficient free RAM for transcode "
                f"(available: {avail // (1024*1024)} MB, "
                f"need: {self._MIN_FREE_RAM_FOR_TRANSCODE // (1024*1024)} MB)"
            )

        max_w = profile.get("max_width", self._transcode_max_w)
        max_h = profile.get("max_height", self._transcode_max_h)
        max_fps = profile.get("max_fps", 0)
        target_encoder = profile.get("encoder", "libx264")
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

        thread_limit = self._compute_thread_limit()

        encoders = [target_encoder]
        if target_encoder not in ("libx264", "libx265"):
            encoders.append("libx264")
        if "libx264" not in encoders:
            encoders.append("libx264")

        for encoder in encoders:
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-c:v", encoder,
                "-vf", scale_filter,
            ]

            if encoder in ("libx264", "libx265"):
                crf = max(0, min(51, self._transcode_quality))
                preset = "fast"
                if encoder == "libx265":
                    # libx265 uses 2-3× more RAM than libx264 at the same
                    # preset.  Use a lighter preset on memory-constrained
                    # devices (Pi with ≤2GB) to avoid OOM.
                    avail = _get_available_ram_bytes()
                    total_ram = 0
                    try:
                        with open("/proc/meminfo") as f:
                            for line in f:
                                if line.startswith("MemTotal:"):
                                    total_ram = int(line.split()[1]) * 1024
                                    break
                    except Exception:
                        pass
                    if total_ram > 0 and total_ram <= 3 * 1024 * 1024 * 1024:  # ≤3GB
                        preset = "ultrafast"
                    else:
                        preset = "superfast"

                # Profiles with ``bitrate_target`` (Pi 2/3, software decode)
                # use targeted bitrate encoding instead of CRF.  CRF cannot
                # reliably cap average bitrate — the output can exceed the
                # profile limit by 30-50 %, choking software decode.
                if profile.get("bitrate_target"):
                    target_br = profile.get("max_bitrate", 10)
                    cmd += ["-preset", preset, "-b:v", f"{target_br}M"]
                else:
                    cmd += ["-preset", preset, "-crf", str(crf)]

                if thread_limit is not None and thread_limit > 0:
                    param_key = "-x264-params" if encoder == "libx264" else "-x265-params"
                    cmd += [param_key, f"threads={thread_limit}"]
            else:
                q = self._transcode_quality
                bitrate = "2M" if q <= 24 else "1M"
                cmd += ["-b:v", bitrate]

            # Profile constraints for smooth Pi playback
            if encoder == "libx264" and h264_level:
                cmd += ["-level", h264_level]
            if encoder == "libx264" and h264_profile:
                cmd += ["-profile:v", h264_profile]
            if encoder in ("libx264", "libx265"):
                cmd += ["-refs", "2", "-bf", "0", "-g", "30"]

            # Framerate cap — only reduce, never increase
            # A 30fps source should not be upscaled to 60fps by -r.
            # Only apply the cap when the source exceeds max_fps.
            src_fps = info.get("fps", 0) or 0
            if max_fps and max_fps > 0 and src_fps > max_fps:
                cmd += ["-r", str(max_fps)]

            # Audio: keep or strip
            keep_audio = self._cfg.get("keep_audio", False)
            if not keep_audio:
                cmd += ["-an"]

            # HDR → SDR downgrade
            if not hdr_support:
                cmd += [
                    "-colorspace", "bt709",
                    "-color_primaries", "bt709",
                    "-color_trc", "bt709",
                ]

            # Max bitrate constraint
            max_br = profile.get("max_bitrate", 0)
            if max_br and max_br > 0:
                cmd += ["-maxrate", f"{max_br}M", "-bufsize", f"{max_br * 2}M"]

            cmd += [
                "-movflags", "+faststart",
                str(dest),
            ]

            final_cmd = self._wrap_with_throttle(cmd)
            timeout = max(60, self._transcode_timeout)

            try:
                subprocess.run(
                    final_cmd, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                logger.debug(
                    "Transcoded with %s (threads=%s, timeout=%ds): %s",
                    encoder, thread_limit, timeout, source.name,
                )
                return
            except subprocess.CalledProcessError as e:
                logger.warning("Encoder %s failed for %s (rc=%d)", encoder, source.name, e.returncode)
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
            except subprocess.TimeoutExpired:
                logger.warning("Encoder %s timed out for %s (%ds limit)", encoder, source.name, timeout)
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
        cmd = self._wrap_with_throttle([
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
        """Probe video file for metadata using ffprobe.

        Returns a dict with keys: width, height, duration, codec_name,
        fps, bitrate, color_depth, h264_profile, h264_level,
        color_primaries, color_trc, colorspace, pix_fmt.
        """
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
        info: dict = {
            "width": 0, "height": 0, "duration": 0.0,
            "codec_name": "", "fps": 0.0, "bitrate": 0,
            "color_depth": 8, "h264_profile": "", "h264_level": "",
            "color_primaries": "", "color_trc": "", "colorspace": "",
            "pix_fmt": "",
        }
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
                info["codec_name"] = stream.get("codec_name", "")
                info["pix_fmt"] = stream.get("pix_fmt", "")
                info["h264_profile"] = stream.get("profile", "")
                info["h264_level"] = stream.get("level", "")
                info["color_primaries"] = stream.get("color_primaries", "")
                info["color_trc"] = stream.get("color_transfer", "")
                info["colorspace"] = stream.get("color_space", "")

                # Framerate
                fps_str = stream.get("r_frame_rate", "0/1")
                try:
                    num, den = fps_str.split("/")
                    info["fps"] = round(float(num) / float(den), 2)
                except (ValueError, ZeroDivisionError):
                    info["fps"] = 0.0

                # Bitrate (prefer stream-level, fall back to format-level)
                if stream.get("bit_rate"):
                    info["bitrate"] = int(stream["bit_rate"]) // 1_000_000
                break

        # Format-level bitrate fallback
        fmt = data.get("format", {})
        if info["bitrate"] == 0 and fmt.get("bit_rate"):
            info["bitrate"] = int(fmt["bit_rate"]) // 1_000_000
        info["duration"] = float(fmt.get("duration", 0))

        # Detect color depth from pixel format
        pf = info["pix_fmt"]
        if pf and "10" in pf:
            info["color_depth"] = 10
        elif pf and "12" in pf:
            info["color_depth"] = 12

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

    def _extract_video_frames(
        self, source: Path, file_hash: str,
    ) -> tuple[Path | None, Path | None]:
        """Extract first (t=0) and last (``-sseof``) frame JPEGs.

        Returns ``(first_frame_path, last_frame_path)``.  Either may be
        ``None`` if extraction fails for that frame.

        This is called during Phase 2 (OPTIMISE) so the frontend never
        needs to run ffmpeg — it just loads the pre-generated cache files.
        """
        frame_dir = self._video_cache
        first_path = frame_dir / f"{file_hash}.1.frame"
        last_path = frame_dir / f"{file_hash}.2.frame"

        # ── First frame (t=0, keyframe seek) ──────────────────────────
        if not first_path.exists() or first_path.stat().st_size == 0:
            cmd = self._wrap_with_throttle([
                "ffmpeg", "-y",
                "-noaccurate_seek", "-ss", "0",
                "-i", str(source),
                "-vframes", "1", "-q:v", "2",
                "-f", "image2",
                str(first_path),
            ])
            try:
                subprocess.run(
                    cmd, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.warning("Failed to extract first frame: %s", source.name)
                with contextlib.suppress(OSError):
                    first_path.unlink()
                first_path = None  # type: ignore[assignment]

        # ── Last frame (sseof -1, decode final second) ────────────────
        # Decode ALL frames from 1 s before EOF to the actual end and
        # keep only the last one.  ``-update 1`` tells ffmpeg to
        # overwrite the output file with each frame, so the file on
        # disk is always the *latest* decoded frame — i.e. the true
        # final frame regardless of keyframe placement.
        # Using ``-vframes 1`` would grab the *first* frame after the
        # seek position, which is typically a keyframe several seconds
        # before the real end — causing a visible jitter when VLC
        # exits and the last frame appears underneath.
        if not last_path.exists() or last_path.stat().st_size == 0:
            cmd = self._wrap_with_throttle([
                "ffmpeg", "-y",
                "-sseof", "-1",
                "-i", str(source),
                "-q:v", "2",
                "-f", "image2",
                "-update", "1",
                str(last_path),
            ])
            try:
                subprocess.run(
                    cmd, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                logger.warning("Failed to extract last frame: %s", source.name)
                with contextlib.suppress(OSError):
                    last_path.unlink()
                last_path = None  # type: ignore[assignment]

        return first_path, last_path

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
        first_frame: Path | None = None,
        last_frame: Path | None = None,
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
            first_frame_path=first_frame,
            last_frame_path=last_frame,
            source=source_name,
            transcode_status=status,
        )
