# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for shared data models (MediaItem, Album)."""

from __future__ import annotations

from pathlib import Path

import pytest

from metixel.shared.models import Album, MediaItem, MediaType, TranscodeStatus


# ---------------------------------------------------------------------------
# MediaItem.aspect_ratio
# ---------------------------------------------------------------------------

class TestMediaItemAspectRatio:
    """Tests for MediaItem.aspect_ratio computed property."""

    def test_standard_16_9(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=1080,
        )
        assert item.aspect_ratio == pytest.approx(16.0 / 9.0)

    def test_square(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            width=800,
            height=800,
        )
        assert item.aspect_ratio == 1.0

    def test_zero_height_returns_1(self) -> None:
        """Divide-by-zero safety: height=0 returns 1.0."""
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            width=1920,
            height=0,
        )
        assert item.aspect_ratio == 1.0

    def test_zero_height_video(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            width=0,
            height=0,
        )
        assert item.aspect_ratio == 1.0


# ---------------------------------------------------------------------------
# MediaItem.is_ready_to_play
# ---------------------------------------------------------------------------

class TestMediaItemIsReadyToPlay:
    """Tests for MediaItem.is_ready_to_play — all transcode state combos."""

    # -- Images are always ready --

    def test_image_always_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
        )
        assert item.is_ready_to_play is True

    def test_image_ready_even_with_transcode_status(self) -> None:
        """Transcode status is irrelevant for images."""
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/a.jpg"),
            cached_path=Path("/tmp/a.jpg"),
            media_type=MediaType.IMAGE,
            transcode_status=TranscodeStatus.TRANSCODING,
        )
        assert item.is_ready_to_play is True

    # -- Video: None status → NOT ready (hasn't been through pipeline) --

    def test_video_none_status_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=None,
        )
        assert item.is_ready_to_play is False

    # -- Video: TRANSCODING → NOT ready --

    def test_video_transcoding_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.TRANSCODING,
        )
        assert item.is_ready_to_play is False

    # -- Video: TRANSCODED but missing frames → NOT ready --

    def test_video_transcoded_missing_frames_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=None,
            last_frame_path=None,
        )
        assert item.is_ready_to_play is False

    def test_video_transcoded_missing_first_frame_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=None,
            last_frame_path=Path("/tmp/v.2.frame.jpg"),
        )
        assert item.is_ready_to_play is False

    def test_video_transcoded_missing_last_frame_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=Path("/tmp/v.1.frame.jpg"),
            last_frame_path=None,
        )
        assert item.is_ready_to_play is False

    # -- Video: TRANSCODED + both frames → READY --

    def test_video_transcoded_with_frames_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.TRANSCODED,
            first_frame_path=Path("/tmp/v.1.frame.jpg"),
            last_frame_path=Path("/tmp/v.2.frame.jpg"),
        )
        assert item.is_ready_to_play is True

    # -- Video: FAILED + both frames → READY (play original) --

    def test_video_failed_with_frames_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.FAILED,
            first_frame_path=Path("/tmp/v.1.frame.jpg"),
            last_frame_path=Path("/tmp/v.2.frame.jpg"),
        )
        assert item.is_ready_to_play is True

    def test_video_failed_missing_frames_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.FAILED,
            first_frame_path=None,
            last_frame_path=None,
        )
        assert item.is_ready_to_play is False

    # -- Video: NOT_TRANSCODED + both frames → READY --

    def test_video_not_transcoded_with_frames_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.NOT_TRANSCODED,
            first_frame_path=Path("/tmp/v.1.frame.jpg"),
            last_frame_path=Path("/tmp/v.2.frame.jpg"),
        )
        assert item.is_ready_to_play is True

    def test_video_not_transcoded_missing_frames_not_ready(self) -> None:
        item = MediaItem(
            id="test",
            original_path=Path("/tmp/v.mp4"),
            cached_path=Path("/tmp/v.mp4"),
            media_type=MediaType.VIDEO,
            transcode_status=TranscodeStatus.NOT_TRANSCODED,
            first_frame_path=None,
            last_frame_path=None,
        )
        assert item.is_ready_to_play is False


# ---------------------------------------------------------------------------
# MediaItem defaults
# ---------------------------------------------------------------------------

class TestMediaItemDefaults:
    """Verify dataclass field defaults."""

    def test_default_width_height(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.width == 0
        assert item.height == 0

    def test_default_duration(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.duration_seconds == 0.0

    def test_default_source(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.source == "local"

    def test_default_exif_data_empty_dict(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.exif_data == {}

    def test_default_thumbnail_path_none(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.thumbnail_path is None

    def test_default_frame_paths_none(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.first_frame_path is None
        assert item.last_frame_path is None

    def test_default_transcode_status_none(self) -> None:
        item = MediaItem(
            id="t", original_path=Path("/t"), cached_path=Path("/t"),
            media_type=MediaType.IMAGE,
        )
        assert item.transcode_status is None


# ---------------------------------------------------------------------------
# Album defaults
# ---------------------------------------------------------------------------

class TestAlbumDefaults:
    """Verify Album dataclass field defaults."""

    def test_default_items_empty(self) -> None:
        album = Album(id="a1", name="Test")
        assert album.items == []

    def test_default_source_local(self) -> None:
        album = Album(id="a1", name="Test")
        assert album.source == "local"

    def test_default_last_synced_zero(self) -> None:
        album = Album(id="a1", name="Test")
        assert album.last_synced == 0.0
