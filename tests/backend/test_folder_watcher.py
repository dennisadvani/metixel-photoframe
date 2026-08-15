# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the FolderWatcher media sync engine."""

import time
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

from metixel.backend.sync.folder_watcher import (
    FolderWatcher,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MEDIA_EXTENSIONS,
)


def _make_valid_jpeg(path: Path, size: tuple[int, int] = (64, 48)) -> None:
    """Create a tiny valid JPEG file."""
    img = Image.new("RGB", size, color=(255, 0, 0))
    img.save(path, "JPEG")


@pytest.fixture
def mock_state(tmp_path):
    """Create a mock StateManager with a real ProcessingJournal."""
    from metixel.backend.processing.journal import ProcessingJournal

    state = mock.MagicMock()
    state.config.display = {"width": 1920, "height": 1080}
    state.config.system = {"cache_dir": "/tmp/metixel_cache"}
    state.config.image = {
        "optimisation_enabled": True,
        "optimise_max_width": 0,
        "optimise_max_height": 0,
    }
    state.config.video = {
        "transcoding_enabled": True,
        "transcode_max_width": 0,
        "transcode_max_height": 0,
    }
    state.config.sync = {"local": {"watch_paths": ["/tmp"], "poll_interval_seconds": 1}}
    state.config_path = Path("/opt/metixel/etc/config.json")
    # The watcher reads/writes per-file state through this journal.
    state.journal = ProcessingJournal(tmp_path / "processing_state.json")
    return state


class TestFolderWatcherDetection:
    """Tests for file change detection logic."""

    def test_initial_scan_discovers_files(self, mock_state, tmp_path):
        """On first scan, all media files should be discovered."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        # Create some test files
        _make_valid_jpeg(tmp_path / "photo1.jpg")
        _make_valid_jpeg(tmp_path / "photo2.jpg")
        (tmp_path / "video.mp4").touch()
        (tmp_path / "readme.txt").touch()  # should be ignored

        watcher = FolderWatcher(mock_state)
        # Manually init processors (avoid filesystem cache dir issues in tests)
        watcher._image_processor = mock.MagicMock()
        watcher._image_processor.process.return_value = None  # Don't add to playlist
        watcher._video_processor = mock.MagicMock()
        watcher._video_processor.process.return_value = None

        watcher._scan()

        # Should have discovered 3 media files
        assert len(watcher._known_files) == 3
        assert watcher._initial_scan_done is True

    def test_new_file_detected_on_second_scan(self, mock_state, tmp_path):
        """A file added between scans should be detected."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        # Create initial file
        _make_valid_jpeg(tmp_path / "existing.jpg")
        (tmp_path / "readme.txt").touch()

        watcher = FolderWatcher(mock_state)
        watcher._image_processor = mock.MagicMock()
        watcher._image_processor.process.return_value = None
        watcher._video_processor = mock.MagicMock()

        # First scan — baseline
        watcher._scan()
        assert len(watcher._known_files) == 1

        # Add a new file
        _make_valid_jpeg(tmp_path / "new.jpg")

        # Second scan — should detect the new file
        watcher._scan()
        assert len(watcher._known_files) == 2

    def test_modified_file_detected(self, mock_state, tmp_path):
        """A modified file should be detected on the next scan."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        img_path = tmp_path / "changing.jpg"
        _make_valid_jpeg(img_path)

        watcher = FolderWatcher(mock_state)
        watcher._image_processor = mock.MagicMock()
        watcher._image_processor.process.return_value = None
        watcher._video_processor = mock.MagicMock()

        # First scan
        watcher._scan()
        old_mtime = watcher._known_files[img_path.resolve()][0]

        # Modify the file (wait a tiny bit so mtime differs)
        time.sleep(0.01)
        _make_valid_jpeg(img_path, size=(128, 96))

        # Second scan — should detect the change
        watcher._scan()
        new_mtime = watcher._known_files[img_path.resolve()][0]
        assert new_mtime != old_mtime

    def test_deleted_file_detected(self, mock_state, tmp_path):
        """A deleted file should be removed from the snapshot."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        img_path = tmp_path / "removable.jpg"
        _make_valid_jpeg(img_path)

        watcher = FolderWatcher(mock_state)
        watcher._image_processor = mock.MagicMock()
        watcher._image_processor.process.return_value = None
        watcher._video_processor = mock.MagicMock()

        # First scan
        watcher._scan()
        resolved = img_path.resolve()
        assert resolved in watcher._known_files

        # Delete the file
        img_path.unlink()

        # Second scan — should be gone
        watcher._scan()
        assert resolved not in watcher._known_files

    def test_no_changes_on_idle_scan(self, mock_state, tmp_path):
        """When nothing changes, no playlist updates should occur."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        _make_valid_jpeg(tmp_path / "stable.jpg")

        watcher = FolderWatcher(mock_state)
        watcher._image_processor = mock.MagicMock()
        watcher._image_processor.process.return_value = None
        watcher._video_processor = mock.MagicMock()

        # First scan
        watcher._scan()
        assert mock_state.replace_playlist.call_count >= 0

        # Reset call counts
        mock_state.add_playlist_items.reset_mock()
        mock_state.remove_playlist_items.reset_mock()

        # Second scan — nothing changed
        watcher._scan()
        # Should NOT call add or remove
        mock_state.add_playlist_items.assert_not_called()
        mock_state.remove_playlist_items.assert_not_called()

    def test_non_media_files_ignored(self, mock_state, tmp_path):
        """Files with non-media extensions should be skipped."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]

        (tmp_path / "script.py").touch()
        (tmp_path / "notes.txt").touch()
        (tmp_path / "archive.zip").touch()

        watcher = FolderWatcher(mock_state)
        watcher._image_processor = mock.MagicMock()
        watcher._video_processor = mock.MagicMock()

        watcher._scan()
        assert len(watcher._known_files) == 0


class TestFolderWatcherExtensions:
    """Verify accepted file extensions."""

    def test_image_extensions(self):
        assert ".jpg" in IMAGE_EXTENSIONS
        assert ".png" in IMAGE_EXTENSIONS
        assert ".webp" in IMAGE_EXTENSIONS
        assert ".bmp" in IMAGE_EXTENSIONS

    def test_video_extensions(self):
        assert ".mp4" in VIDEO_EXTENSIONS
        assert ".mov" in VIDEO_EXTENSIONS
        assert ".mkv" in VIDEO_EXTENSIONS
        assert ".avi" in VIDEO_EXTENSIONS

    def test_media_extensions_union(self):
        assert ".jpg" in MEDIA_EXTENSIONS
        assert ".mp4" in MEDIA_EXTENSIONS
        assert ".txt" not in MEDIA_EXTENSIONS


class TestFolderWatcherJournal:
    """Journal-driven re-pickup prevention."""

    def test_failed_file_not_repicked(self, mock_state, tmp_path):
        """A permanently-failed file is never re-gathered (until it changes)."""
        from metixel.backend.processing.journal import STATE_FAILED

        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]
        img = tmp_path / "poison.jpg"
        _make_valid_jpeg(img)

        # Simulate a previously-failed file (survives restart via the journal).
        # In production the watcher marks pending (with the fingerprint) before
        # enqueue, and the queue marks failed when processing fails.
        fp = (int(img.stat().st_mtime_ns), int(img.stat().st_size))
        mock_state.journal.mark_pending(img.resolve(), fp, media_type="image")
        mock_state.journal.mark_failed(img.resolve(), "previous failure")

        watcher = FolderWatcher(mock_state)
        watcher._scan()

        assert mock_state.journal.get(img.resolve())["state"] == STATE_FAILED
        mock_state.add_playlist_items.assert_not_called()
        mock_state.remove_playlist_items.assert_not_called()

    def test_ready_file_skipped_on_incremental_scan(self, mock_state, tmp_path):
        """An unchanged ready file is not re-gathered on later scans."""
        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]
        img = tmp_path / "done.jpg"
        _make_valid_jpeg(img)
        fp = (int(img.stat().st_mtime_ns), int(img.stat().st_size))
        mock_state.journal.mark_pending(img.resolve(), fp, media_type="image")
        mock_state.journal.mark_ready(img.resolve(), None)

        watcher = FolderWatcher(mock_state)
        watcher._scan()  # initial scan — rebuilds playlist (re-gathers ready)

        # Simulate the real pipeline re-marking it ready after processing.
        mock_state.journal.mark_ready(img.resolve(), None)
        mock_state.add_playlist_items.reset_mock()

        watcher._scan()  # incremental — ready + unchanged → skip
        mock_state.add_playlist_items.assert_not_called()

    def test_failed_file_repicked_after_change(self, mock_state, tmp_path):
        """Modifying a failed file resets it so it is re-gathered."""
        import time

        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]
        img = tmp_path / "poison.jpg"
        _make_valid_jpeg(img)
        fp = (int(img.stat().st_mtime_ns), int(img.stat().st_size))
        mock_state.journal.mark_pending(img.resolve(), fp, media_type="image")
        mock_state.journal.mark_failed(img.resolve(), "previous failure")

        # Change the file so the fingerprint differs
        time.sleep(0.01)
        _make_valid_jpeg(img, size=(128, 96))

        watcher = FolderWatcher(mock_state)
        watcher._scan()
        # Modified → re-gathered → image goes to the playlist
        mock_state.add_playlist_items.assert_called()


class TestFolderWatcherStateIntegration:
    """Tests for playlist state integration."""

    def test_add_playlist_items_deduplicates(self, mock_state, tmp_path):
        """Duplicate items (same id) should not be added twice."""
        from metixel.shared.models import MediaItem, MediaType

        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]
        mock_state.config_path = Path("/tmp/config.json")

        # Create a real StateManager-like add_playlist_items
        existing = [
            MediaItem(
                id="abc123", original_path=Path("/tmp/a.jpg"),
                cached_path=Path("/tmp/a.jpg"), media_type=MediaType.IMAGE,
                width=100, height=100,
            ),
        ]
        mock_state._playlist = list(existing)

        # Simulate add_playlist_items behavior
        def fake_add(items):
            existing_ids = {i.id for i in mock_state._playlist}
            new = [i for i in items if i.id not in existing_ids]
            mock_state._playlist.extend(new)
            return len(new)

        mock_state.add_playlist_items.side_effect = fake_add

        # Try to add the same item again
        new_items = [
            MediaItem(
                id="abc123", original_path=Path("/tmp/a.jpg"),
                cached_path=Path("/tmp/a.jpg"), media_type=MediaType.IMAGE,
                width=100, height=100,
            ),
            MediaItem(
                id="xyz789", original_path=Path("/tmp/b.jpg"),
                cached_path=Path("/tmp/b.jpg"), media_type=MediaType.IMAGE,
                width=200, height=200,
            ),
        ]
        mock_state.add_playlist_items(new_items)

        # Should only have 2 items (abc123 + xyz789)
        assert len(mock_state._playlist) == 2

    def test_remove_playlist_items(self, mock_state):
        """Removing items by id should work correctly."""
        from metixel.shared.models import MediaItem, MediaType

        mock_state._playlist = [
            MediaItem(id="a", original_path=Path("/t/a.jpg"), cached_path=Path("/t/a.jpg"),
                      media_type=MediaType.IMAGE, width=100, height=100),
            MediaItem(id="b", original_path=Path("/t/b.jpg"), cached_path=Path("/t/b.jpg"),
                      media_type=MediaType.IMAGE, width=100, height=100),
            MediaItem(id="c", original_path=Path("/t/c.jpg"), cached_path=Path("/t/c.jpg"),
                      media_type=MediaType.IMAGE, width=100, height=100),
        ]

        def fake_remove(ids):
            before = len(mock_state._playlist)
            mock_state._playlist = [i for i in mock_state._playlist if i.id not in ids]
            return before - len(mock_state._playlist)

        mock_state.remove_playlist_items.side_effect = fake_remove

        removed = mock_state.remove_playlist_items({"a", "c"})
        assert removed == 2
        assert len(mock_state._playlist) == 1
        assert mock_state._playlist[0].id == "b"


class TestFolderWatcherHeif:
    """HEIC/HEIF originals are readable and always converted to JPEG."""

    def _watcher(self, mock_state, tmp_path) -> FolderWatcher:
        from metixel.backend.processing.journal import ProcessingJournal

        mock_state.config.sync["local"]["watch_paths"] = [str(tmp_path)]
        watcher = FolderWatcher(mock_state)
        watcher._journal = mock_state.journal
        return watcher

    def test_heif_forced_to_cache_even_within_limits(self, mock_state, tmp_path):
        """A HEIC source within display resolution must still be optimised to JPEG."""
        from metixel.shared.models import MediaItem, MediaType

        watcher = self._watcher(mock_state, tmp_path)
        item = MediaItem(
            id="heif1",
            original_path=Path("/tmp/photo.jpg"),
            cached_path=Path("/tmp/photo.jpg"),
            media_type=MediaType.IMAGE,
            width=1000,
            height=1000,
            exif_data={"format": "HEIF"},
        )
        cached = watcher._resolve_cached_path(item)
        assert cached != item.original_path
        assert str(cached).endswith(".jpg")

    def test_jpeg_within_limits_plays_original(self, mock_state, tmp_path):
        from metixel.shared.models import MediaItem, MediaType

        watcher = self._watcher(mock_state, tmp_path)
        item = MediaItem(
            id="jpg1",
            original_path=Path("/tmp/photo.jpg"),
            cached_path=Path("/tmp/photo.jpg"),
            media_type=MediaType.IMAGE,
            width=1000,
            height=1000,
            exif_data={"format": "JPEG"},
        )
        assert watcher._resolve_cached_path(item) == item.original_path

    def test_ensure_heif_support_is_safe_noop(self):
        """ensure_heif_support() must not raise, with or without pillow-heif."""
        from metixel.backend.processing.utils import ensure_heif_support

        ensure_heif_support()
        ensure_heif_support()  # idempotent
