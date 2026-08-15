# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Video player backend — VLC subprocess (VlcVideoPlayer).

This module is a facade; the implementation lives in ``vlc_player.py``.
External importers keep using this path.
"""

from metixel.frontend.presentation.vlc_player import VlcVideoPlayer
from metixel.shared.media import VIDEO_EXTENSIONS

__all__ = ["VlcVideoPlayer", "VIDEO_EXTENSIONS"]
