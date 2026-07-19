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

    @property
    def aspect_ratio(self) -> float:
        if self.height > 0:
            return self.width / self.height
        return 1.0


@dataclass
class Album:
    """A named collection of media items."""

    id: str
    name: str
    items: list[MediaItem] = field(default_factory=list)
    source: str = "local"  # "local" or "immich"
    last_synced: float = 0.0  # Unix timestamp
