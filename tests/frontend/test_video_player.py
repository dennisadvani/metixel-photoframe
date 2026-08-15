# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the VLC subprocess video player and accepted video extensions."""

from unittest import mock

import pytest

from metixel.frontend.presentation.video_player import (
    VIDEO_EXTENSIONS,
    VlcVideoPlayer,
)


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
        """_vlc_available should return False when the vlc binary is missing."""
        with mock.patch("shutil.which", return_value=None):
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
