# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Data models for Metixel Photoframe."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MediaType(enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class TranscodeStatus(enum.Enum):
    """Transcoding state for a video media item."""
    NOT_TRANSCODED = "not_transcoded"  # Original file, transcoding not attempted
    TRANSCODING = "transcoding"       # Transcode in progress
    TRANSCODED = "transcoded"         # Cached transcoded file ready
    FAILED = "failed"                 # Transcode failed, use original


@dataclass
class MediaItem:
    """Represents a single media asset ready for display."""

    id: str  # SHA-256 hash of original file
    original_path: Path
    cached_path: Path  # Path to processed (resized/transcoded) file
    media_type: MediaType
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0  # For videos
    thumbnail_path: Path | None = None
    exif_data: dict[str, Any] = field(default_factory=dict)
    source: str = "local"  # "local" or "immich"
    transcode_status: TranscodeStatus | None = None  # Only relevant for videos

    @property
    def aspect_ratio(self) -> float:
        if self.height > 0:
            return self.width / self.height
        return 1.0

    @property
    def is_ready_to_play(self) -> bool:
        """Whether this item can be played in the slideshow.

        Images are always ready.  Videos are ready if transcoding is
        disabled (original file), or transcoding is enabled and the
        cached file exists and is marked as transcoded.
        """
        if self.media_type == MediaType.IMAGE:
            return True
        # Video: ready if not transcoded (original file used when
        # transcoding is off) OR transcoded successfully
        if self.transcode_status is None:
            return True  # Unknown status — allow playback
        return self.transcode_status in (
            TranscodeStatus.TRANSCODED,
            TranscodeStatus.FAILED,
        )


@dataclass
class Album:
    """A named collection of media items."""

    id: str
    name: str
    items: list[MediaItem] = field(default_factory=list)
    source: str = "local"  # "local" or "immich"
    last_synced: float = 0.0  # Unix timestamp
