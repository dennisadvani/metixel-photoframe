# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video processor — ffmpeg-based transcoding and thumbnail extraction.

Facade for the video processing pipeline.  The heavy lifting now lives in:
- ``probe`` — ffprobe metadata probes plus RAM / Pi model detection
- ``ffmpeg_cmds`` — pure ffmpeg/ffprobe command builders
- ``frames`` — thumbnail and first/last frame extraction execution

``VideoProcessor`` keeps the public API, profile/threshold logic and the
process orchestration, delegating the mechanics to those modules.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metixel.backend.processing.ffmpeg_cmds import (
    compute_thread_limit,
    select_encoders,
    transcode_cmd,
    wrap_with_throttle,
)
from metixel.backend.processing.frames import (
    cleanup_cached_video,
    extract_thumbnail,
    extract_video_frames,
)
from metixel.backend.processing.probe import (
    available_ram_bytes,
    probe_video,
    validate_cached_video,
)
from metixel.backend.processing.probe import (
    detect_pi_model as _detect_pi_model,
)
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus

logger = logging.getLogger(__name__)


@dataclass
class VideoScan:
    """Result of the Phase A video scan (probe + thumbnail + frames).

    Carried into Phase B (:meth:`VideoProcessor.transcode`) so the encode
    reuses the probe info and frame/thumbnail paths instead of re-probing.
    Non-fatal problems (e.g. a frame failing to extract) are recorded in
    ``errors`` so the caller can exclude the video if frames are required.
    """

    source_path: Path
    source: str
    file_hash: str
    info: dict
    thumbnail_path: Path
    first_frame_path: Path | None
    last_frame_path: Path | None
    needs_transcode: bool
    errors: list = field(default_factory=list)

    @property
    def has_frames(self) -> bool:
        """Whether both first and last frames are available (playable)."""
        return self.first_frame_path is not None and self.last_frame_path is not None


class VideoProcessor:
    """Processes video files for display: transcode, extract thumbnail.

    Phase 1: Uses software libx264 by default for the best quality.
    Hardware-accelerated encoding (h264_v4l2m2m on Pi) is available as an
    opt-in alternative when speed is preferred over quality.

    Threshold gating:
    - Use :meth:`needs_optimisation` to check whether a video needs
      transcoding BEFORE calling :meth:`process`.
    - Videos already in H.264 within the resolution limits skip transcoding.
    """

    #: Known H.264 codec names that skip transcoding when within limits.

    H264_CODECS = {"h264", "avc", "avc1", "h.264", "avc1."}

    #: Known H.265 / HEVC codec names.

    HEVC_CODECS = {"hevc", "h265", "h.265", "hev1", "hvc1"}

    # -- Transcoding profiles -----------------------------------------

    PROFILES: dict[str, dict[str, Any]] = {
        "pi2": {
            "label": "Raspberry Pi 2",
            "codec": "h264",
            "encoder": "libx264",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "max_bitrate": 7,  # Mbps
            "crf": 28,  # Higher CRF = lower bitrate for software decode
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
            "max_fps": 30,  # Hard limit — see project requirements
            "max_bitrate": 7,  # Software decode limit: ARM cores tap out ~8-10 Mbps
            "crf": 28,  # Higher CRF = lower bitrate for software decode
            "h264_profile": "high",
            "h264_level": "4.0",  # Pi 3 VideoCore IV HW decode limit: Level 4.1
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
            "crf": 23,  # Standard quality for hardware decode
            "h264_profile": "high",  # Not used for H.265, informational
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
            "crf": 23,  # Standard quality for hardware decode
            "h264_profile": "high",
            "h264_level": "5.2",
            "color_depth": 10,
            "hdr_support": True,
        },
    }

    # Minimum free RAM (bytes) required before attempting a transcode.

    _MIN_FREE_RAM_FOR_TRANSCODE: int = 192 * 1024 * 1024  # 192 MB

    def __init__(
        self,
        cache_dir: Path,
        screen_width: int = 1920,
        screen_height: int = 1080,
        video_config: dict[str, Any] | None = None,
        timeouts: dict[str, Any] | None = None,
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

        # Centralised timeouts (from config.timeouts, with defaults)
        self._t = timeouts or {}

        def _to(key: str, fallback: int) -> int:
            v = int(self._t.get(key, fallback))
            return v if v > 0 else fallback

        self._timeout = _to  # helper: self._timeout("key", default)

        # Track currently transcoding files (by hash) so we can check guardrails
        self._transcoding: set[str] = set()

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
        return bool(self._transcoding_enabled)

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
                "encoder": "libx264"
                if self._cfg.get("transcode_codec", "h264") == "h264"
                else "libx265",
                "max_width": self._cfg.get("transcode_max_width", 0) or self._screen_w,
                "max_height": self._cfg.get("transcode_max_height", 0) or self._screen_h,
                "max_fps": self._cfg.get("transcode_max_fps", 30),
                "max_bitrate": self._cfg.get("transcode_max_bitrate", 20),
                "crf": self._cfg.get("transcode_crf", self._transcode_quality),
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
            self._transcoding_enabled,
            self._transcode_max_w,
            self._transcode_max_h,
            self._transcode_quality,
            self._force_software_encoder,
            self._cpu_throttle_enabled,
            self._cpu_throttle_pct,
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
            logger.info("Needs transcode: codec %s not in H.264 set", source_codec)
            return True
        if target_codec == "h265" and source_codec not in VideoProcessor.HEVC_CODECS:
            logger.info("Needs transcode: codec %s not in HEVC set", source_codec)
            return True

        # Resolution
        max_w = profile.get("max_width", 0)
        if max_w and w > max_w:
            logger.info("Needs transcode: width %d > max %d", w, max_w)
            return True
        max_h = profile.get("max_height", 0)
        if max_h and h > max_h:
            logger.info("Needs transcode: height %d > max %d", h, max_h)
            return True

        # Framerate
        max_fps = profile.get("max_fps", 0)
        src_fps = probe_info.get("fps", 0) or 0
        if max_fps and src_fps > max_fps:
            logger.info("Needs transcode: fps %.2f > max %.2f", src_fps, max_fps)
            return True

        # Bitrate (both in Mbps — probe normalises bps→Mbps)
        max_br = profile.get("max_bitrate", 0)
        src_br = probe_info.get("bitrate", 0) or 0
        if max_br and src_br > int(max_br * 1.1):
            logger.info(
                "Needs transcode: bitrate %d Mbps > max %d Mbps (+10%%=%d)",
                src_br,
                max_br,
                int(max_br * 1.1),
            )
            return True

        # Color depth
        target_depth = profile.get("color_depth", 8)
        src_depth = probe_info.get("color_depth", 8) or 8
        if src_depth > target_depth:
            logger.info("Needs transcode: color depth %d > target %d", src_depth, target_depth)
            return True

        # HDR → SDR downgrade
        if not profile.get("hdr_support", False) and probe_info.get("color_trc", ""):
            trc = probe_info["color_trc"]
            if trc in ("smpte2084", "arib-std-b67", "smpte428", "bt2020-10"):
                logger.info("Needs transcode: HDR source on non-HDR Pi (trc=%s)", trc)
                return True

        # H.264 Profile/Level check (for H.264 sources on H.264 profiles)
        if target_codec == "h264" and source_codec in VideoProcessor.H264_CODECS:
            target_level = profile.get("h264_level", "")
            src_level = probe_info.get("h264_level", "") or ""
            if target_level != "" and src_level != "":
                try:
                    if float(src_level) > float(target_level):
                        logger.info(
                            "Needs transcode: H.264 level %s > target %s",
                            src_level,
                            target_level,
                        )
                        return True
                except (ValueError, TypeError):
                    pass

        # Within all limits — no optimisation needed
        logger.debug("Video within all profile limits — skipping transcode")
        return False

    def process(self, source_path: Path, source: str = "local") -> MediaItem | None:
        """Process a single video end-to-end (scan + transcode).

        Returns a MediaItem or ``None`` on failure.  Convenience wrapper
        around :meth:`scan` + :meth:`transcode` for callers that don't need
        the two-phase (scan-all-then-transcode) pipeline.
        """
        scan = self.scan(source_path, source=source)
        if scan is None:
            return None
        return self.transcode(scan)

    def scan(self, source_path: Path, source: str = "local") -> VideoScan | None:
        """Phase A — probe + thumbnail + first/last frames + transcode decision.

        Extracts everything the frontend needs (thumbnail for the media page,
        first/last frames for slideshow preload/swap) and decides, using the
        full profile check, whether the video must be transcoded.

        Returns a :class:`VideoScan`, or ``None`` if the file cannot be read
        at all.  Non-fatal problems (e.g. a frame failing to extract) are
        recorded in ``scan.errors`` so the caller can exclude the video when
        frames are required for playback.
        """
        try:
            file_hash = self._hash_file(source_path)
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
                source_path,
                file_hash,
            )

            # Full profile check — the same decision the transcode phase uses,
            # so H.264 sources on an H.265 profile are correctly flagged as
            # needing transcoding (previously the coarse watch-stage check
            # under-counted them, hiding encodes under "Scanning video").
            profile = self._resolve_profile()
            needs_transcode = bool(
                profile is not None and VideoProcessor.needs_optimisation(info, profile)
            )

            errors: list[str] = []
            if first_frame is None or last_frame is None:
                errors.append("Frame extraction failed (first/last frame missing)")

            return VideoScan(
                source_path=source_path,
                source=source,
                file_hash=file_hash,
                info=info,
                thumbnail_path=thumb_path,
                first_frame_path=first_frame,
                last_frame_path=last_frame,
                needs_transcode=needs_transcode,
                errors=errors,
            )
        except Exception:
            logger.exception("Failed to scan video: %s", source_path)
            return None

    def transcode(self, scan: VideoScan) -> MediaItem | None:
        """Phase B — produce a playable MediaItem, encoding if necessary.

        * ``scan.needs_transcode`` is False → ``NOT_TRANSCODED`` (play the
          original at its own quality).
        * Needs transcode → reuse a valid cached file, otherwise encode.
          A failed encode returns a ``FAILED`` item — the caller must NOT
          add it to the playlist (no native-resolution fallback).
        """
        source_path = scan.source_path
        file_hash = scan.file_hash
        info = scan.info
        profile = self._resolve_profile()
        cached_path = self._video_cache / f"{file_hash}.mp4"

        # Within all profile limits (or transcoding disabled) — play original.
        if not scan.needs_transcode:
            logger.debug(
                "Video already optimal (%s %dx%d) — skipping transcode for %s",
                info.get("codec_name", ""),
                info.get("width", 0) or 0,
                info.get("height", 0) or 0,
                source_path.name,
            )
            return self._build_item(
                source_path,
                source_path,
                scan.thumbnail_path,
                info,
                scan.source,
                file_hash,
                status=TranscodeStatus.NOT_TRANSCODED,
                first_frame=scan.first_frame_path,
                last_frame=scan.last_frame_path,
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
                        source_path,
                        cached_path,
                        scan.thumbnail_path,
                        info,
                        scan.source,
                        file_hash,
                        status=TranscodeStatus.TRANSCODED,
                        first_frame=scan.first_frame_path,
                        last_frame=scan.last_frame_path,
                    )
                logger.info(
                    "Cached video exceeds new profile limits — re-transcoding: %s",
                    cached_path.name,
                )
                self._cleanup_cached_video(cached_path, scan.thumbnail_path, file_hash)
            else:
                logger.warning(
                    "Cached video is corrupt — will re-transcode: %s",
                    cached_path.name,
                )
                self._cleanup_cached_video(cached_path, scan.thumbnail_path, file_hash)
        else:
            # No cached file exists for this content hash.  This is the
            # silent path — previously indistinguishable from a corrupt
            # or profile-mismatched cache.  Log it explicitly so every
            # re-transcode is attributable to exactly one cause.
            logger.info(
                "No cached video found for %s — transcoding",
                source_path.name,
            )

        # Mark as transcoding, then transcode
        self._transcoding.add(file_hash)
        failure_reason: str | None = None
        try:
            self._transcode(source_path, cached_path, info)
            status = TranscodeStatus.TRANSCODED
            playback_path = cached_path
            logger.info(
                "Video transcoded: %s → %s",
                source_path.name,
                file_hash,
            )
        except RuntimeError as e:
            logger.warning(
                "Transcode failed for %s: %s — video will be excluded from the playlist",
                source_path.name,
                e,
            )
            status = TranscodeStatus.FAILED
            playback_path = source_path
            failure_reason = str(e)
        finally:
            self._transcoding.discard(file_hash)

        return self._build_item(
            source_path,
            playback_path,
            scan.thumbnail_path,
            info,
            scan.source,
            file_hash,
            status=status,
            first_frame=scan.first_frame_path,
            last_frame=scan.last_frame_path,
            failure_reason=failure_reason,
        )

    def requires_encode(self, scan: VideoScan) -> bool:
        """Whether an actual ffmpeg encode is needed for this scan.

        ``scan.needs_transcode`` is a *profile* decision (the video should be
        transcoded); this adds the *cache* state — the cached file is missing,
        corrupt, or no longer within profile limits.  Used to decide which
        videos populate the "Transcoding" progress bar: only real encodes are
        counted, not cache reuse.  Does not mutate anything.
        """
        if not scan.needs_transcode:
            return False
        cached_path = self._video_cache / f"{scan.file_hash}.mp4"
        if not cached_path.exists():
            return True
        if not self._validate_cached_video(cached_path):
            return True
        cached_info = self._probe(cached_path)
        profile = self._resolve_profile()
        return VideoProcessor.needs_optimisation(cached_info, profile)

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
        failure_reason: str | None = None,
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
            failure_reason=failure_reason,
        )

    # -- Helpers (delegated to probe / ffmpeg_cmds / frames) -----------------

    def _transcode(self, source: Path, dest: Path, info: dict | None = None) -> None:
        """Transcode video to the profile's optimal format."""
        profile = self._resolve_profile()
        if profile is None:
            profile = VideoProcessor.PROFILES.get("pi3", VideoProcessor.PROFILES["pi3"])

        avail = available_ram_bytes()
        if avail is not None and avail < self._MIN_FREE_RAM_FOR_TRANSCODE:
            raise RuntimeError(
                f"Insufficient free RAM for transcode "
                f"(available: {avail // (1024 * 1024)} MB, "
                f"need: {self._MIN_FREE_RAM_FOR_TRANSCODE // (1024 * 1024)} MB)"
            )

        thread_limit = self._compute_thread_limit()
        target_encoder = profile.get("encoder", "libx264")
        encoders = [target_encoder]
        if target_encoder not in ("libx264", "libx265"):
            encoders.append("libx264")
        if "libx264" not in encoders:
            encoders.append("libx264")

        timeout = max(60, self._transcode_timeout)
        for encoder in encoders:
            cmd = transcode_cmd(
                source,
                dest,
                encoder,
                profile,
                info,
                transcode_quality=self._transcode_quality,
                thread_limit=thread_limit,
                keep_audio=self._cfg.get("keep_audio", False),
                fallback_max_w=self._transcode_max_w,
                fallback_max_h=self._transcode_max_h,
            )
            final_cmd = self._wrap_with_throttle(cmd)
            try:
                subprocess.run(
                    final_cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
                logger.debug(
                    "Transcoded with %s (threads=%s, timeout=%ds): %s",
                    encoder,
                    thread_limit,
                    timeout,
                    source.name,
                )
                return
            except subprocess.CalledProcessError as e:
                logger.warning(
                    "Encoder %s failed for %s (rc=%d)", encoder, source.name, e.returncode
                )
                if dest.exists():
                    with contextlib.suppress(OSError):
                        dest.unlink()
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Encoder %s timed out for %s (%ds limit)", encoder, source.name, timeout
                )
                if dest.exists():
                    with contextlib.suppress(OSError):
                        dest.unlink()
            except Exception:
                logger.exception("Unexpected error during transcode with %s", encoder)
                if dest.exists():
                    with contextlib.suppress(OSError):
                        dest.unlink()

        raise RuntimeError(f"All encoders failed for: {source.name}")

    def _wrap_with_throttle(self, cmd: list[str]) -> list[str]:
        """Wrap an ffmpeg command with CPU throttling (see ffmpeg_cmds)."""
        return wrap_with_throttle(cmd, self._cpu_throttle_enabled, self._cpu_throttle_pct)

    def _compute_thread_limit(self) -> int | None:
        """Compute ffmpeg thread limit from the throttle percentage."""
        return compute_thread_limit(self._cpu_throttle_enabled, self._cpu_throttle_pct)

    def _extract_thumbnail(self, source: Path, dest: Path) -> None:
        """Extract a thumbnail frame at 2 seconds into the video."""
        extract_thumbnail(
            source,
            dest,
            self._screen_w,
            self._screen_h,
            self._timeout("thumbnail_extract", 300),
        )

    def _probe(self, path: Path) -> dict:
        """Probe video file for metadata using ffprobe (see probe.probe_video)."""
        return probe_video(path, self._timeout("ffprobe_probe", 120))

    def _select_encoders(self) -> list[str]:
        """Return the H.264 encoder(s) to try, in priority order."""
        return select_encoders(self._force_software_encoder, self._timeout("hw_codec_detect", 30))

    def _validate_cached_video(self, path: Path) -> bool:
        """Check that a cached video file is valid (see probe.validate_cached_video)."""
        return validate_cached_video(path, self._timeout("ffprobe_validate", 60))

    def _extract_video_frames(
        self,
        source: Path,
        file_hash: str,
    ) -> tuple[Path | None, Path | None]:
        """Extract first and last frame JPEGs (see frames.extract_video_frames)."""
        return extract_video_frames(
            source,
            file_hash,
            self._video_cache,
            self._screen_w,
            self._screen_h,
            self._timeout,
        )

    @staticmethod
    def _cleanup_cached_video(cached_path: Path, thumb_path: Path, file_hash: str) -> None:
        """Delete a corrupt cached video and its frame cache files.

        The thumbnail is NOT deleted — it's generated from the source
        file and is independent of the transcode output.
        """
        cleanup_cached_video(cached_path, file_hash)
