# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for PresentationEngine video integration."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType


def _make_valid_jpeg(path: Path) -> None:
    """Create a tiny valid JPEG file."""
    img = Image.new("RGB", (16, 16), color=(255, 0, 0))
    img.save(path, "JPEG")


class TestEngineVideoIntegration:
    """Tests for video-related PresentationEngine behaviour."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock DisplayBackend."""
        backend = mock.MagicMock()
        backend.width = 1920
        backend.height = 1080
        return backend

    @pytest.fixture
    def config(self):
        """Create a Config with video playback enabled."""
        cfg = Config()
        cfg.update("slideshow", {"video_playback_enabled": True})
        return cfg

    def test_scan_folder_includes_videos(self, mock_backend, config):
        """scan_folder() should pick up video files alongside images."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Create a valid JPEG
            _make_valid_jpeg(tmp / "photo.jpg")
            # Create a dummy video file
            (tmp / "clip.mp4").touch()

            items = engine.scan_folder(tmp)

            types = {item.media_type for item in items}
            assert MediaType.IMAGE in types
            assert MediaType.VIDEO in types

    def test_scan_folder_video_has_correct_type(self, mock_backend, config):
        """Video files should get MediaType.VIDEO."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _make_valid_jpeg(tmp / "still.jpg")
            (tmp / "movie.mov").touch()

            items = engine.scan_folder(tmp)

            video_items = [i for i in items if i.media_type == MediaType.VIDEO]
            assert len(video_items) == 1
            assert video_items[0].source == "local"

    def test_scan_folder_skips_unknown_extensions(self, mock_backend, config):
        """Files with unknown extensions should be skipped."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            (tmp / "readme.txt").touch()
            (tmp / "script.py").touch()

            items = engine.scan_folder(tmp)

            assert len(items) == 0

    def test_get_item_duration_for_image(self, mock_backend, config):
        """Images should use image_duration_seconds."""
        from metixel.frontend.presentation.engine import PresentationEngine

        config.update("slideshow", {"image_duration_seconds": 45})
        engine = PresentationEngine(config, mock_backend)

        item = MediaItem(
            id="test",
            original_path=Path("/tmp/photo.jpg"),
            cached_path=Path("/tmp/photo.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )

        assert engine._get_item_duration(item) == 45.0

    def test_get_item_duration_for_video_with_metadata(self, mock_backend, config):
        """Videos with duration_seconds set should use that duration."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        item = MediaItem(
            id="test",
            original_path=Path("/tmp/clip.mp4"),
            cached_path=Path("/tmp/clip.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=15.5,
        )

        assert engine._get_item_duration(item) == 15.5

    def test_get_item_duration_for_video_capped(self, mock_backend, config):
        """Video duration should be capped at video_max_duration_seconds."""
        from metixel.frontend.presentation.engine import PresentationEngine

        config.update("slideshow", {"video_max_duration_seconds": 30})
        engine = PresentationEngine(config, mock_backend)

        item = MediaItem(
            id="test",
            original_path=Path("/tmp/clip.mp4"),
            cached_path=Path("/tmp/clip.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=120.0,  # Very long video
        )

        assert engine._get_item_duration(item) == 30.0

    def test_get_item_duration_for_video_no_cap(self, mock_backend, config):
        """When cap is 0, video plays for its full duration."""
        from metixel.frontend.presentation.engine import PresentationEngine

        config.update("slideshow", {"video_max_duration_seconds": 0})
        engine = PresentationEngine(config, mock_backend)

        item = MediaItem(
            id="test",
            original_path=Path("/tmp/clip.mp4"),
            cached_path=Path("/tmp/clip.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=300.0,
        )

        assert engine._get_item_duration(item) == 300.0

    def test_next_item_does_not_crash_with_video_in_queue(self, mock_backend, config):
        """next_item should handle video items gracefully (no crash)."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        item1 = MediaItem(
            id="v1", original_path=Path("/t/v.mp4"),
            cached_path=Path("/t/v.mp4"), media_type=MediaType.VIDEO,
            width=1920, height=1080, duration_seconds=5.0,
        )
        item2 = MediaItem(
            id="img1", original_path=Path("/t/1.jpg"),
            cached_path=Path("/t/1.jpg"), media_type=MediaType.IMAGE,
            width=1920, height=1080,
        )
        engine.set_queue([item1, item2])
        # Should not crash — just advance the index
        engine.next_item()
        assert engine._current_idx == 1

    def test_set_queue_handles_video_items(self, mock_backend, config):
        """set_queue should handle video items correctly."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        items = [
            MediaItem(
                id="v1", original_path=Path("/t/v.mp4"),
                cached_path=Path("/t/v.mp4"), media_type=MediaType.VIDEO,
                width=1920, height=1080, duration_seconds=5.0,
            ),
            MediaItem(
                id="img1", original_path=Path("/t/1.jpg"),
                cached_path=Path("/t/1.jpg"), media_type=MediaType.IMAGE,
                width=1920, height=1080,
            ),
        ]
        engine.set_queue(items)
        assert len(engine._queue) == 2


class TestEngineVlcIntegration:
    """Tests for VLC-based video playback in PresentationEngine."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock DisplayBackend."""
        backend = mock.MagicMock()
        backend.width = 1920
        backend.height = 1080
        return backend

    @pytest.fixture
    def config_vlc(self):
        """Config with video playback enabled."""
        from metixel.shared.config import Config
        cfg = Config()
        cfg.update("slideshow", {
            "video_playback_enabled": True,
        })
        return cfg

    @pytest.mark.skip(
        reason="TODO: rewrite for non-blocking video state machine "
               "(_video_launch + _video_tick + _video_finish). "
               "The old _start_video_vlc method was replaced."
    )
    def test_start_video_vlc_sets_post_playback_state(
        self, mock_backend, config_vlc,
    ):
        """After VLC finishes, _video_playing is cleared, _item_start_time
        is set so that elapsed ≈ video duration (placing the render loop
        at the start of the transition phase), and _current_idx is NOT
        advanced (render() handles the advance to avoid double-advance
        bugs when Pi plays slower than real-time)."""
        from metixel.frontend.presentation.engine import PresentationEngine
        from metixel.frontend.presentation.video_player import VlcVideoPlayer

        engine = PresentationEngine(config_vlc, mock_backend)
        item = MediaItem(
            id="v1", original_path=Path("/t/v.mp4"),
            cached_path=Path("/t/v.mp4"), media_type=MediaType.VIDEO,
            width=1920, height=1080, duration_seconds=5.0,
        )
        img_item = MediaItem(
            id="i1", original_path=Path("/t/1.jpg"),
            cached_path=Path("/t/1.jpg"), media_type=MediaType.IMAGE,
            width=1920, height=1080,
        )
        engine.set_queue([item, img_item])
        original_idx = engine._current_idx

        # Mock ffprobe, frame cache, and VLC play.
        mock_proc = mock.MagicMock()
        mock_proc.wait.return_value = 0
        ffprobe_result = mock.MagicMock()
        ffprobe_result.returncode = 0
        ffprobe_result.stdout = "1920,1080,5.0"
        with mock.patch(
            "metixel.frontend.presentation.engine._get_or_create_video_frame",
            return_value=None,
        ), mock.patch(
            "metixel.frontend.presentation.engine.subprocess.run",
            return_value=ffprobe_result,
        ), mock.patch.object(
            VlcVideoPlayer, "play", return_value=mock_proc,
        ):
            engine._start_video_vlc(item, "/t/v.mp4")

        # After VLC: _video_playing cleared.
        assert engine._video_playing is False, (
            "_video_playing should be False after VLC exits"
        )
        # _item_start_time is set so elapsed ≈ video duration.
        # On the next render() call this places the loop at the
        # start of the transition phase (crossfade from last frame).
        import time
        now = time.monotonic()
        item_duration = engine._get_item_duration(item)
        expected_start = now - item_duration
        assert abs(engine._item_start_time - expected_start) < 1.0, (
            f"_item_start_time should be ≈ now - duration "
            f"(got {engine._item_start_time}, expected ≈ {expected_start})"
        )
        # _current_idx still points to the video item — _advance() is NOT
        # called from _start_video_vlc.  The render loop's timer logic
        # (elapsed >= duration + transition_s) triggers the advance on
        # the next render() call.
        assert engine._current_idx == original_idx, (
            "_current_idx should NOT change inside _start_video_vlc"
        )
