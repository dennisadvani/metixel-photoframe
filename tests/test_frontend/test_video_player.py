# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the VideoPlayer subprocess-based frame extraction and VlcVideoPlayer."""

import io
import struct
from unittest import mock

import numpy as np
import pytest

from metixel.frontend.presentation.video_player import (
    VIDEO_EXTENSIONS,
    VlcVideoPlayer,
    VideoPlayer,
)


class TestVideoPlayer:
    """Tests for VideoPlayer."""

    def test_initial_state(self):
        """A freshly created VideoPlayer should not be playing."""
        player = VideoPlayer()
        assert not player.is_playing
        assert player.fps == 30.0
        assert player.width == 0
        assert player.height == 0

    def test_stop_when_not_playing(self):
        """Calling stop() when nothing is playing should not raise."""
        player = VideoPlayer()
        player.stop()
        assert not player.is_playing

    def test_get_frame_when_not_playing(self):
        """get_frame() should return None when nothing is playing."""
        player = VideoPlayer()
        assert player.get_frame() is None

    def test_compute_scale_downscale(self):
        """Video larger than target should be downscaled maintaining AR."""
        w, h = VideoPlayer._compute_scale(3840, 2160, 1920, 1080)
        assert w == 1920
        assert h == 1080

    def test_compute_scale_smaller_than_target(self):
        """Video smaller than target should not be upscaled."""
        w, h = VideoPlayer._compute_scale(640, 480, 1920, 1080)
        assert w == 640
        assert h == 480

    def test_compute_scale_wide_aspect(self):
        """Ultrawide video should fit within target, preserving AR."""
        w, h = VideoPlayer._compute_scale(2560, 1080, 1920, 1080)
        # Should scale to screen width, height adjusted
        assert w == 1920
        assert h == pytest.approx(810, abs=2)  # 2560:1080 → 1920:810

    def test_compute_scale_tall_aspect(self):
        """Tall (portrait) video should fit within target."""
        w, h = VideoPlayer._compute_scale(1080, 1920, 1920, 1080)
        # Should scale to screen height, width adjusted
        assert h == 1080
        assert w == pytest.approx(607, abs=2)  # 1080:1920 → 607:1080

    def test_compute_scale_even_dimensions(self):
        """Output dimensions should always be even."""
        for sw, sh in [(1921, 1079), (1366, 768), (100, 77)]:
            w, h = VideoPlayer._compute_scale(sw, sh, 1920, 1080)
            assert w % 2 == 0, f"Width {w} is not even"
            assert h % 2 == 0, f"Height {h} is not even"

    def test_compute_scale_zero_input(self):
        """Zero or negative source dimensions should return target."""
        w, h = VideoPlayer._compute_scale(0, 0, 1920, 1080)
        assert w == 1920
        assert h == 1080

    def test_probe_nonexistent_file(self):
        """Probing a nonexistent file should return None."""
        info = VideoPlayer._probe("/nonexistent/video.mp4")
        assert info is None

    def test_detect_hw_decoder_returns_string_or_none(self):
        """Hardware decoder detection should not raise."""
        result = VideoPlayer._detect_hw_decoder()
        assert result is None or isinstance(result, str)


class TestVideoExtensions:
    """Verify accepted video file extensions."""

    def test_common_formats(self):
        assert ".mp4" in VIDEO_EXTENSIONS
        assert ".mov" in VIDEO_EXTENSIONS
        assert ".avi" in VIDEO_EXTENSIONS
        assert ".mkv" in VIDEO_EXTENSIONS

    def test_no_image_formats(self):
        """Video extensions should not overlap with image extensions."""
        assert ".jpg" not in VIDEO_EXTENSIONS
        assert ".png" not in VIDEO_EXTENSIONS


class TestVideoPlayerFrameParsing:
    """Test raw frame parsing from simulated ffmpeg output."""

    def test_frame_from_bytes(self):
        """Verify a raw RGB24 buffer can be converted to the expected array."""
        w, h = 32, 24
        frame_bytes = w * h * 3
        # Create a simple gradient pattern
        raw = bytearray(frame_bytes)
        for i in range(0, frame_bytes, 3):
            raw[i] = i % 256       # R
            raw[i + 1] = (i + 1) % 256  # G
            raw[i + 2] = (i + 2) % 256  # B

        frame = np.frombuffer(bytes(raw), dtype=np.uint8).reshape((h, w, 3))
        assert frame.shape == (h, w, 3)
        assert frame.dtype == np.uint8

    def test_incomplete_frame_buffer(self):
        """An incomplete buffer (< expected bytes) should not be reshaped."""
        w, h = 64, 48
        expected = w * h * 3
        short = bytes(expected - 10)
        # Should not try to reshape incomplete data
        with pytest.raises(ValueError):
            np.frombuffer(short, dtype=np.uint8).reshape((h, w, 3))


class TestVideoPlayerPlayFramePacing:
    """Test frame pacing logic in get_frame()."""

    def test_get_frame_when_no_process(self):
        """With no subprocess, get_frame returns None."""
        player = VideoPlayer()
        player._playing = True
        player._width = 64
        player._height = 48
        player._frame_bytes = 64 * 48 * 3
        player._frame_period = 1.0 / 30.0
        player._start_time = 0.0  # Won't match monotonic()
        # No process → returns None
        assert player.get_frame() is None


# ---------------------------------------------------------------------------
# VlcVideoPlayer tests
# ---------------------------------------------------------------------------


class TestVlcVideoPlayer:
    """Tests for VlcVideoPlayer."""

    def test_initial_state(self):
        """Fresh VlcVideoPlayer should not be playing."""
        player = VlcVideoPlayer()
        assert not player.is_playing
        assert not player.is_finished
        assert player.duration == 0.0

    def test_stop_when_not_playing(self):
        """Calling stop() when nothing is playing should not raise."""
        player = VlcVideoPlayer()
        player.stop()
        assert not player.is_playing

    def test_poll_with_no_player(self):
        """poll() should return None when no VLC player was started."""
        player = VlcVideoPlayer()
        assert player.poll() is None

    def test_is_available_returns_bool(self):
        """is_available() should return True or False without raising."""
        result = VlcVideoPlayer.is_available()
        assert isinstance(result, bool)

    def test_vlc_not_available_without_deps(self):
        """_vlc_available should return False when VLC not installed."""
        with mock.patch("builtins.__import__", side_effect=ImportError):
            assert not VlcVideoPlayer._vlc_available()

    def test_sdl2_not_available_without_deps(self):
        """_sdl2_available should return False when SDL2 not installed."""
        with mock.patch("builtins.__import__", side_effect=ImportError):
            assert not VlcVideoPlayer._sdl2_available()

    def test_hw_codecs_initially_empty(self):
        """Fresh player should have empty hw_codecs list."""
        player = VlcVideoPlayer()
        assert player.hw_codecs == []

    def test_play_returns_none_when_deps_missing(self):
        """play() should return None when VLC/SDL2 unavailable."""
        player = VlcVideoPlayer()
        with mock.patch.object(VlcVideoPlayer, "_vlc_available", return_value=False):
            result = player.play("/tmp/test.mp4", block=False)
            assert result is None
            assert not player.is_playing

    def test_poll_returns_none_when_idle(self):
        """poll() should return None when no VLC player is active."""
        player = VlcVideoPlayer()
        assert player.poll() is None

    def test_duration_while_playing(self):
        """duration should report elapsed wall-clock time."""
        import time
        player = VlcVideoPlayer()
        player._playing = True
        player._start_time = time.monotonic() - 5.0  # Started 5s ago
        assert player.duration == pytest.approx(5.0, abs=0.5)

    def test_duration_when_finished(self):
        """duration should return the recorded duration after finishing."""
        player = VlcVideoPlayer()
        player._finished = True
        player._duration = 10.5
        assert player.duration == 10.5

    def test_duration_when_idle(self):
        """duration should be 0.0 when never started."""
        player = VlcVideoPlayer()
        assert player.duration == 0.0
