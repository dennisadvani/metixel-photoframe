# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""FFmpeg pipeline video player (fallback) — decodes frames as numpy arrays."""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)


class VideoPlayer:
    """Plays video files by piping ffmpeg-decoded frames as numpy arrays.

    Frames are decoded at the target display resolution and returned as
    numpy arrays (H, W, 3) ready for GPU texture upload. The caller is
    responsible for uploading frames to the display backend.

    Usage::

        player = VideoPlayer()
        player.play("/path/to/video.mp4", target_w=1920, target_h=1080)
        while player.is_playing and not player.is_finished:
            frame = player.get_frame()
            if frame is not None:
                backend.update_texture(video_tex, frame)

    The player paces itself — ``get_frame()`` returns ``None`` if it's not
    yet time for the next frame (based on the source video's FPS).
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._playing: bool = False
        self._finished: bool = False
        self._width: int = 0
        self._height: int = 0
        self._fps: float = 30.0
        self._frame_bytes: int = 0
        self._last_frame_time: float = 0.0
        self._frame_period: float = 1.0 / 30.0
        self._duration: float = 0.0
        self._start_time: float = 0.0
        self._frames_delivered: int = 0
        self._frames_skipped_pacing: int = 0
        self._video_path: str = ""
        self._frame_queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=4)
        self._reader_thread: threading.Thread | None = None

    # -- Public API ----------------------------------------------------------

    def play(
        self,
        video_path: str,
        target_w: int = 1920,
        target_h: int = 1080,
        loop: bool = False,
    ) -> tuple[int, int] | None:
        """Start video playback via ffmpeg subprocess.

        Launches ffmpeg to decode the video to raw RGB24 frames at the
        target resolution. Frames are read from stdout as numpy arrays.

        Args:
            video_path: Path to the video file.
            target_w: Target width for decoded frames (screen width).
            target_h: Target height for decoded frames (screen height).
            loop: If True, restart playback when the video ends.

        Returns:
            (width, height) of decoded frames, or None on failure.
        """
        self.stop()

        try:
            # Probe video metadata with ffprobe
            info = self._probe(video_path)
            if info is None:
                logger.error("Cannot probe video: %s", video_path)
                return None

            src_w = info.get("width", target_w)
            src_h = info.get("height", target_h)
            self._fps = info.get("fps", 30.0)
            self._duration = info.get("duration", 0.0)
            self._frame_period = 1.0 / max(self._fps, 1.0)

            # Compute scale: fit within target, maintain aspect ratio,
            # ensure even dimensions (required by many codecs).
            scale_w, scale_h = self._compute_scale(
                src_w,
                src_h,
                target_w,
                target_h,
            )

            cmd = [
                "ffmpeg",
                "-loglevel",
                "error",
                "-hwaccel",
                "drm",
                "-i",
                str(video_path),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-vf",
                (
                    f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=decrease,"
                    f"pad={scale_w}:{scale_h}:(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-an",  # No audio
                "-vsync",
                "passthrough",
                "-",
            ]

            logger.info(
                "VideoPlayer starting: %s -> %dx%d @ %.1f fps (src: %dx%d)",
                video_path,
                scale_w,
                scale_h,
                self._fps,
                src_w,
                src_h,
            )

            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self._width = scale_w
            self._height = scale_h
            self._frame_bytes = scale_w * scale_h * 3
            self._playing = True
            self._finished = False
            self._start_time = time.monotonic()
            self._last_frame_time = 0.0
            self._frames_delivered = 0
            self._frames_skipped_pacing = 0
            self._video_path = video_path

            self._frame_queue = queue.Queue(maxsize=4)
            self._reader_thread = threading.Thread(
                target=self._reader_worker,
                daemon=True,
                name="video-reader",
            )
            self._reader_thread.start()

            logger.debug(
                "ffmpeg cmd: %s | frame_bytes=%d | fps=%.1f | period=%.3fs",
                " ".join(cmd),
                self._frame_bytes,
                self._fps,
                self._frame_period,
            )

            return (scale_w, scale_h)

        except FileNotFoundError:
            logger.error("ffmpeg not found — install ffmpeg for video support")
            self._playing = False
            return None
        except Exception:
            logger.exception("Failed to start video playback: %s", video_path)
            self._playing = False
            return None

    def stop(self) -> None:
        """Stop video playback and terminate the ffmpeg subprocess."""
        self._playing = False
        self._finished = True
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break
        if self._process:
            with contextlib.suppress(Exception):
                if self._process.stdout is not None:
                    self._process.stdout.close()
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                with contextlib.suppress(Exception):
                    self._process.kill()
            self._process = None
        self._reader_thread = None

    def get_frame(self) -> np.ndarray | None:
        """Get the next video frame as a numpy array.

        Pulls from a background queue — never blocks on ffmpeg stdout.

        Frame pacing & skipping:
        - If we're ahead of schedule (delivered more frames than the
          source FPS would have produced by now), returns ``None``.
        - If we're behind schedule (render loop can't keep up), drains
          the queue to skip intermediate frames and returns only the
          most recent one — maintaining real-time playback at the cost
          of dropped frames.
        - Returns ``None`` if no frame is available yet or playback ended.
        """
        if not self._playing or self._process is None:
            return None

        now = time.monotonic()
        elapsed = now - self._start_time
        expected_frame = int(elapsed / self._frame_period)

        # -- Ahead of schedule: pace ourselves ---------------------------
        if self._frames_delivered > expected_frame:
            self._frames_skipped_pacing += 1
            return None

        # -- On time or behind: grab next frame from queue ---------------
        try:
            frame = self._frame_queue.get_nowait()
        except queue.Empty:
            return None

        if frame is None:
            self._finished = True
            return None

        self._frames_delivered += 1

        # -- Behind schedule: drain queue to catch up --------------------
        # If we're more than 1 frame behind the source clock, drain
        # intermediate frames and keep only the latest.  This prevents
        # slow-motion playback on underpowered hardware (Pi 3, Pi Zero 2 W)
        # by trading temporal accuracy for real-time pacing.
        catch_up_frames = 0
        while self._frames_delivered < expected_frame:
            try:
                skipped = self._frame_queue.get_nowait()
            except queue.Empty:
                break
            if skipped is None:
                self._finished = True
                return frame  # return the last good frame we had
            frame = skipped
            self._frames_delivered += 1
            catch_up_frames += 1

        if catch_up_frames > 0:
            logger.debug(
                "Skipped %d frames (behind by %d, elapsed=%.2fs, delivered=%d, expected=%d) [%s]",
                catch_up_frames,
                expected_frame - self._frames_delivered,
                elapsed,
                self._frames_delivered,
                expected_frame,
                self._video_path,
            )

        self._last_frame_time = elapsed

        if self._frames_delivered <= 3:
            logger.debug(
                "Frame #%d [%s]: %dx%d, first 12 bytes=%s",
                self._frames_delivered,
                self._video_path,
                self._width,
                self._height,
                frame.flat[:12].tolist(),
            )

        return frame

    def _reader_worker(self) -> None:
        """Background thread: read raw RGB24 frames from ffmpeg stdout."""
        try:
            while self._process and self._process.stdout:
                raw = self._process.stdout.read(self._frame_bytes)
                if not raw or len(raw) < self._frame_bytes:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                    (self._height, self._width, 3),
                )
                self._frame_queue.put(frame)
        except Exception:
            pass
        finally:
            with contextlib.suppress(queue.Full):
                self._frame_queue.put_nowait(None)

    # -- Properties ----------------------------------------------------------

    @property
    def is_playing(self) -> bool:
        """Whether a video is currently loaded and playing."""
        return self._playing

    @property
    def is_finished(self) -> bool:
        """Whether the video has reached its end."""
        return self._finished

    @property
    def fps(self) -> float:
        """The source video's frame rate."""
        return self._fps

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def duration(self) -> float:
        """Video duration in seconds (from metadata)."""
        return self._duration

    @property
    def elapsed(self) -> float:
        """Elapsed playback time in seconds."""
        if not self._playing:
            return 0.0
        return time.monotonic() - self._start_time

    # -- Internal helpers ----------------------------------------------------

    @staticmethod
    def _probe(video_path: str) -> dict | None:
        """Probe video metadata using ffprobe (JSON format — field-order safe)."""
        try:
            import json

            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,r_frame_rate,duration",
                    "-of",
                    "json",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            probe = json.loads(result.stdout)
            streams = probe.get("streams", [])
            if not streams:
                return None

            s = streams[0]
            w = s.get("width", 0) or 0
            h = s.get("height", 0) or 0

            fps_str = s.get("r_frame_rate", "30/1") or "30/1"
            fps = 30.0
            if "/" in fps_str:
                num, den = fps_str.split("/")
                if int(den) != 0:
                    fps = float(num) / float(den)

            dur = float(s.get("duration", 0) or 0)

            return {"width": w, "height": h, "fps": fps, "duration": dur}
        except Exception:
            logger.exception("ffprobe failed for: %s", video_path)
            return None

    @staticmethod
    def _compute_scale(
        src_w: int,
        src_h: int,
        target_w: int,
        target_h: int,
    ) -> tuple[int, int]:
        """Compute the output frame size that fits within the target."""
        if src_w <= 0 or src_h <= 0:
            return (target_w, target_h)

        ratio = min(target_w / src_w, target_h / src_h)
        ratio = min(ratio, 1.0)

        w = max(2, int(src_w * ratio))
        h = max(2, int(src_h * ratio))
        w = w - (w % 2)
        h = h - (h % 2)
        return (w, h)

    @staticmethod
    def _detect_hw_decoder() -> str | None:
        """Return a hardware H.264 decoder codec name, or None for auto."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-decoders"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "h264_v4l2m2m" in result.stdout:
                return "h264_v4l2m2m"
        except Exception:
            pass
        return None
