# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Overlay Manager - orchestrates rendering layers above the slideshow.

Layers (closest to farthest):
    Boot Screen (z=0.0) -> Messages (z=0.01) -> Widgets (z=0.02) -> Video (z=0.03)
"""

from __future__ import annotations

import logging
from typing import Any

from metixel.display.backend import DisplayBackend
from metixel.frontend.overlay.layer import OverlayLayer

logger = logging.getLogger(__name__)


class OverlayManager:
    """Manages overlay layers drawn on top of the slideshow."""

    def __init__(self) -> None:
        self._layers: list[OverlayLayer] = []

    def add_layer(self, layer: OverlayLayer) -> None:
        self._layers.append(layer)
        self._layers.sort(key=lambda ly: ly.z_base, reverse=True)
        logger.info("Layer registered: %s (z_base=%.4f)", layer.name, layer.z_base)

    def get_layer(self, name: str) -> OverlayLayer | None:
        for ly in self._layers:
            if ly.name == name:
                return ly
        return None

    def update(self, shared_state: dict[str, Any] | None = None) -> None:
        state = shared_state or {}
        for layer in self._layers:
            if layer.visible:
                try:
                    layer.update(state)
                except Exception:
                    logger.exception("Layer update failed: %s", layer.name)

    def draw(self, backend: DisplayBackend) -> None:
        if not backend:
            return
        for layer in self._layers:
            if layer.visible:
                try:
                    layer.draw(backend)
                except Exception:
                    logger.exception("Layer draw failed: %s", layer.name)
