# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Shared utilities for process throttling used by the processing pipeline.

Provides a single ``nice``-wrapping helper so every ffmpeg / ffprobe
invocation — whether from the folder watcher, thumbnail generator, or
video processor — runs at the lowest scheduling priority.  This keeps
the frontend slideshow responsive even when the backend is scanning
or optimising media.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Cached availability check — shutil.which is cheap but we call this
# once on import to avoid repeated filesystem lookups.
_NICE_BINARY: str | None = shutil.which("nice")


def nice_cmd(cmd: Sequence[str]) -> list[str]:
    """Wrap a command with ``nice -n 19`` if available.

    ``nice -n 19`` is the lowest possible scheduling priority.  The
    kernel gives the frontend render loop priority over any ``nice``'d
    process, so the slideshow never stutters — even when ffmpeg or
    ffprobe is running at 100 % CPU.

    Args:
        cmd: The command and arguments as a sequence (e.g. ``["ffmpeg", "-i", ...]``).

    Returns:
        A new list with ``["nice", "-n", "19"]`` prepended if ``nice``
        is available; otherwise the original command unchanged.
    """
    if _NICE_BINARY is not None:
        return ["nice", "-n", "19", *cmd]
    return list(cmd)
