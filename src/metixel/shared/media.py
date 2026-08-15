# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media constants, content hashing, and file fingerprints.

Centralises the accepted-file-extension sets, the "first 1 MB + last 1 KB
SHA-256" content hash, and the ``(mtime_ns, size)`` file fingerprint that
were previously re-declared/re-implemented across the optimisation queue,
folder watcher, state manager, web media route, engine, video player and
pre-cache script.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Accepted image file extensions (lowercase, with dot).
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"})

#: Accepted video file extensions (lowercase, with dot).
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"})

#: All accepted media file extensions.
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

#: HEIC/HEIF image extensions (transcoded to JPEG on upload).
HEIC_EXTENSIONS = frozenset({".heic", ".heif"})

#: Bytes read from the head of a file when content-hashing.
_HASH_HEAD_BYTES = 1024 * 1024
#: Bytes read from the tail of a file when content-hashing.
_HASH_TAIL_BYTES = 1024


def is_image(path: Path | str) -> bool:
    """True if *path* has an accepted image extension (case-insensitive)."""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path | str) -> bool:
    """True if *path* has an accepted video extension (case-insensitive)."""
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def is_media(path: Path | str) -> bool:
    """True if *path* has an accepted image or video extension."""
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def content_hash(path: Path | str) -> str:
    """Return a stable content hash for a media file.

    Hashes the first 1 MB plus the last 1 KB of the file (SHA-256,
    truncated to 16 hex chars) — fast for large files while still
    distinguishing different media.  Files smaller than 1 KB are hashed
    in full.  Raises ``OSError`` if the file cannot be read.
    """
    p = Path(path)
    digest = hashlib.sha256()
    with open(p, "rb") as f:
        chunk = f.read(_HASH_HEAD_BYTES)
        digest.update(chunk)
        # Only hash the tail if the file is large enough to have one.
        if len(chunk) >= _HASH_TAIL_BYTES:
            f.seek(-_HASH_TAIL_BYTES, 2)
            digest.update(f.read(_HASH_TAIL_BYTES))
    return digest.hexdigest()[:16]


def fingerprint(path: Path | str) -> tuple[int, int]:
    """Return the ``(mtime_ns, size)`` fingerprint of a file.

    Used by the processing journal and folder watcher to detect file
    changes without hashing.  Raises ``OSError`` if stat fails.
    """
    st = Path(path).stat()
    return (st.st_mtime_ns, st.st_size)
