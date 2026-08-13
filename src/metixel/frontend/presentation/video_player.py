# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video player backends — SDL2 + python-vlc (VlcVideoPlayer) and ffmpeg (VideoPlayer).

This module is a facade; the implementations live in ``vlc_player.py`` and
``ffmpeg_player.py``. External importers keep using this path.
"""

from metixel.frontend.presentation.ffmpeg_player import VideoPlayer
from metixel.frontend.presentation.vlc_player import VlcVideoPlayer

# Accepted video file extensions
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}

__all__ = ["VlcVideoPlayer", "VideoPlayer", "VIDEO_EXTENSIONS"]
