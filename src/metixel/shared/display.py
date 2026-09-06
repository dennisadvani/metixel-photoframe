# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Rotation-aware screen-size resolution for optimisation targets.

The frontend renders at a resolution that is the *native* panel rotated by the
configured ``display.rotation`` (0/90/180/270).  On a 1920x1200 panel with
``rotation: 90`` the on-screen (effective) canvas is **1200x1920** — that is
the size images/videos should be optimised to fill, NOT the raw config dims or
a hardcoded 1920x1080 fallback.

This module is the single source of truth for "what size should media be
optimised to", so the optimiser, folder watcher and any other component agree
even when the user changes the rotation at runtime.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Fallback used only when neither the config nor the frontend can tell us the
#: real resolution (off-screen / headless / very early boot).
DEFAULT_SCREEN_W = 1920
DEFAULT_SCREEN_H = 1080


def _read_display_info() -> dict[str, Any] | None:
    """Read the frontend's ``display_info.json`` status file, if present.

    The frontend writes this with the **effective (already-rotated)**
    resolution plus the applied rotation, e.g. ``{"width":1200,"height":1920,
    "rotation":90}`` for a 1920x1200 panel rotated to portrait.
    """
    path = Path(os.environ.get("METIXEL_RUN_DIR", "/run/metixel")) / "display_info.json"
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - best-effort, never break optimisation
        logger.debug("Could not read display_info from %s", path, exc_info=True)
        return None


def _apply_rotation(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Swap width/height when the rotation is 90 or 270 degrees.

    ``rotation`` is clockwise degrees.  90/270 turn the native panel sideways,
    so the effective width/height swap.  0/180 keep them as-is.
    """
    rot = int(rotation or 0) % 360
    if rot in (90, 270):
        return height, width
    return width, height


def effective_screen_size(
    display_cfg: dict[str, Any] | None = None,
    *,
    use_config_first: bool = False,
) -> tuple[int, int]:
    """Return the effective (post-rotation) on-screen resolution.

    Resolution precedence:
      1. If ``display_cfg`` has explicit nonzero ``width``/``height`` (native
         panel dims), use them and apply the configured rotation.
      2. Otherwise use the frontend's ``display_info.json`` effective size
         (already rotated) when available.
      3. Fall back to :data:`DEFAULT_SCREEN_W` x :data:`DEFAULT_SCREEN_H`.

    ``use_config_first`` forces config dims to win even when the frontend has
    detected a size (only the caller decides the precedence).
    """
    display_cfg = display_cfg or {}
    width = display_cfg.get("width") or 0
    height = display_cfg.get("height") or 0
    rotation = display_cfg.get("rotation") or 0

    # Explicit config dims — treat as native panel, apply rotation.
    if width > 0 and height > 0:
        return _apply_rotation(int(width), int(height), rotation)

    # Auto-detected: display_info.json already reflects the rotated size.
    info = _read_display_info() if not use_config_first else None
    if info:
        dw = info.get("width") or 0
        dh = info.get("height") or 0
        if dw > 0 and dh > 0:
            return int(dw), int(dh)

    return DEFAULT_SCREEN_W, DEFAULT_SCREEN_H


__all__ = ["effective_screen_size", "DEFAULT_SCREEN_W", "DEFAULT_SCREEN_H"]
