# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for PresentationEngine video integration."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from metixel.shared.config import Config
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus


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

    def test_scan_folder_excludes_videos(self, mock_backend, config):
        """scan_folder should NOT pick up video files (backend-pipeline-only)."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _make_valid_jpeg(tmp / "photo.jpg")
            (tmp / "clip.mp4").touch()

            items = engine.scan_folder(tmp)

            types = {item.media_type for item in items}
            assert MediaType.IMAGE in types
            assert MediaType.VIDEO not in types, "scan_folder should exclude videos"

    def test_scan_folder_videos_excluded(self, mock_backend, config):
        """Video files should not appear in scan_folder results."""
        from metixel.frontend.presentation.engine import PresentationEngine

        engine = PresentationEngine(config, mock_backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _make_valid_jpeg(tmp / "still.jpg")
            (tmp / "movie.mov").touch()

            items = engine.scan_folder(tmp)

            video_items = [i for i in items if i.media_type == MediaType.VIDEO]
            assert len(video_items) == 0

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

        # Set max duration in both legacy slideshow and new video section
        config.update("slideshow", {"video_max_duration_seconds": 30})
        config.update("video", {"max_duration_seconds": 30})
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
        config.update("video", {"max_duration_seconds": 0})
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

        # Disable shuffle so the queue order is deterministic — the video
        # must be at index 0 so that _advance() lands on the image, not
        # the video (which would trigger _video_launch and its side effects).
        config.update("slideshow", {"shuffle": False})
        engine = PresentationEngine(config, mock_backend)

        item1 = MediaItem(
            id="v1",
            original_path=Path("/t/v.mp4"),
            cached_path=Path("/t/v.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            transcode_status=TranscodeStatus.NOT_TRANSCODED,
            first_frame_path=Path("/t/v.1.frame.jpg"),
            last_frame_path=Path("/t/v.2.frame.jpg"),
        )
        item2 = MediaItem(
            id="img1",
            original_path=Path("/t/1.jpg"),
            cached_path=Path("/t/1.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
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
                id="v1",
                original_path=Path("/t/v.mp4"),
                cached_path=Path("/t/v.mp4"),
                media_type=MediaType.VIDEO,
                width=1920,
                height=1080,
                duration_seconds=5.0,
                transcode_status=TranscodeStatus.NOT_TRANSCODED,
                first_frame_path=Path("/t/v.1.frame.jpg"),
                last_frame_path=Path("/t/v.2.frame.jpg"),
            ),
            MediaItem(
                id="img1",
                original_path=Path("/t/1.jpg"),
                cached_path=Path("/t/1.jpg"),
                media_type=MediaType.IMAGE,
                width=1920,
                height=1080,
            ),
        ]
        engine.set_queue(items)
        assert len(engine._queue) == 2

    def test_set_queue_blocks_videos_in_portrait(self, mock_backend, config):
        """Videos should be excluded from the queue at 90/270° rotation even
        when playback is otherwise enabled (backend guard)."""
        from metixel.frontend.presentation.engine import PresentationEngine

        config.update("video", {"playback_enabled": True})
        config.update("display", {"rotation": 90})
        engine = PresentationEngine(config, mock_backend)

        video = MediaItem(
            id="v1",
            original_path=Path("/t/v.mp4"),
            cached_path=Path("/t/v.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=Path("/t/v.1.frame.jpg"),
            last_frame_path=Path("/t/v.2.frame.jpg"),
        )
        image = MediaItem(
            id="img1",
            original_path=Path("/t/1.jpg"),
            cached_path=Path("/t/1.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )

        # At 90° the video is filtered out even though playback is enabled;
        # the image remains.
        engine.set_queue([video, image])
        assert [i.id for i in engine._queue] == ["img1"]

    def test_set_queue_keeps_videos_at_landscape(self, mock_backend, config):
        """Videos should remain in the queue at 0/180° rotation."""
        from metixel.frontend.presentation.engine import PresentationEngine

        config.update("video", {"playback_enabled": True})
        config.update("display", {"rotation": 0})
        engine = PresentationEngine(config, mock_backend)

        video = MediaItem(
            id="v1",
            original_path=Path("/t/v.mp4"),
            cached_path=Path("/t/v.mp4"),
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=Path("/t/v.1.frame.jpg"),
            last_frame_path=Path("/t/v.2.frame.jpg"),
        )
        image = MediaItem(
            id="img1",
            original_path=Path("/t/1.jpg"),
            cached_path=Path("/t/1.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )

        # Both remain at 0°.
        engine.set_queue([video, image])
        assert len(engine._queue) == 2


class TestCurrentMediaStateFile:
    """``current_media.json`` drives the dashboard's "Now Playing" card.

    The dashboard polls ``/api/health``, which reports ``current_media`` from
    this file.  A queue change that does not republish it leaves the UI showing
    a stale item (or "No media playing") indefinitely, because nothing else
    rewrites the file until the next slide advance.
    """

    @pytest.fixture
    def engine(self, tmp_path, monkeypatch):
        from metixel.frontend.presentation.engine import PresentationEngine
        from metixel.shared.config import Config

        # run_path() honours METIXEL_RUN_DIR — point it at the temp dir.
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
        cfg = Config()
        cfg.update("slideshow", {"shuffle": False})
        backend = mock.MagicMock()
        backend.width = 1920
        backend.height = 1080
        return PresentationEngine(cfg, backend)

    def _read_state(self, tmp_path) -> dict | None:
        import json

        path = tmp_path / "run" / "current_media.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    @staticmethod
    def _image(item_id: str, path: Path) -> MediaItem:
        return MediaItem(
            id=item_id,
            original_path=path,
            cached_path=path,
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )

    def test_set_queue_publishes_current_item(self, engine, tmp_path):
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        engine.set_queue([self._image("a", a), self._image("b", b)])

        state = self._read_state(tmp_path)
        assert state is not None
        assert state["file"] == "a.jpg"
        assert state["index"] == 0
        assert state["total"] == 2

    def test_remove_items_republishes_state(self, engine, tmp_path):
        """Removing items must rewrite the file, not leave the stale index."""
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        c = tmp_path / "c.jpg"
        engine.set_queue([self._image("a", a), self._image("b", b), self._image("c", c)])
        engine.next_item()  # now on b (index 1)
        assert self._read_state(tmp_path)["file"] == "b.jpg"

        # Remove a non-current item — the file must still reflect the queue.
        removed = engine.remove_items({"c"})

        assert removed == 1
        state = self._read_state(tmp_path)
        assert state is not None
        assert state["file"] == "b.jpg"
        assert state["total"] == 2

    def test_remove_all_items_unpublishes_state(self, engine, tmp_path):
        """An emptied queue must not publish an index of -1.

        A published record with index=-1 is invisible to the dashboard (which
        keeps its last rendered value), so the UI stuck on the previous item
        while the pipeline rebuilt the playlist.
        """
        a = tmp_path / "a.jpg"
        engine.set_queue([self._image("a", a)])
        assert self._read_state(tmp_path) is not None

        engine.remove_items({"a"})

        assert engine._current_idx == -1
        assert self._read_state(tmp_path) is None

    def test_empty_queue_at_startup_publishes_nothing(self, engine, tmp_path):
        engine.set_queue([])

        assert self._read_state(tmp_path) is None


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
        cfg.update(
            "slideshow",
            {
                "video_playback_enabled": True,
            },
        )
        return cfg

    def test_video_finish_sets_post_playback_state(self, mock_backend, config_vlc, tmp_path):
        """After VLC exits, the state machine returns to IDLE, ``_item_start_time``
        is set so that elapsed ≈ video duration (placing the render loop at the
        start of the transition phase), and ``_current_idx`` is NOT advanced —
        ``render()`` handles the advance to avoid double-advance bugs when the
        Pi plays slower than real-time."""
        import time

        from metixel.frontend.presentation.engine import PresentationEngine
        from metixel.frontend.presentation.video_player import VlcVideoPlayer
        from metixel.frontend.presentation.video_state import _VIDEO_IDLE, _VIDEO_WAITING

        first = tmp_path / "v.1.frame.jpg"
        last = tmp_path / "v.2.frame.jpg"
        _make_valid_jpeg(first)
        _make_valid_jpeg(last)
        config_vlc.update("slideshow", {"shuffle": False})
        engine = PresentationEngine(config_vlc, mock_backend)
        item = MediaItem(
            id="v1",
            original_path=tmp_path / "v.mp4",
            cached_path=tmp_path / "v.mp4",
            media_type=MediaType.VIDEO,
            width=1920,
            height=1080,
            duration_seconds=5.0,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=first,
            last_frame_path=last,
        )
        img_item = MediaItem(
            id="i1",
            original_path=tmp_path / "1.jpg",
            cached_path=tmp_path / "1.jpg",
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )
        engine.set_queue([item, img_item])
        original_idx = engine._current_idx

        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        with mock.patch.object(VlcVideoPlayer, "play", return_value=mock_proc):
            engine._video_launch(item)

        assert engine._video_state == _VIDEO_WAITING
        assert engine._video_proc is mock_proc

        # VLC exits — the tick notices and finishes the video.
        mock_proc.poll.return_value = 0
        engine._video_tick()

        assert engine._video_state == _VIDEO_IDLE
        assert engine._video_proc is None
        item_duration = engine._get_item_duration(item)
        expected_start = time.monotonic() - item_duration
        assert abs(engine._item_start_time - expected_start) < 1.0, (
            f"_item_start_time should be ≈ now - duration "
            f"(got {engine._item_start_time}, expected ≈ {expected_start})"
        )
        assert engine._current_idx == original_idx, (
            "_current_idx should NOT change when the video finishes"
        )
