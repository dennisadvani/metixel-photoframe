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
        """Composite every visible layer's elements onto the display.

        Layers are asked for a declarative element list (:meth:`OverlayLayer.
        render`) rather than drawing directly.  The manager flattens those into
        one z-ordered pass, so the backend sees a single plan instead of a
        sequence of stateful draw calls — which is what the reduced
        ``DisplayBackend`` interface requires.

        A layer that raises is logged and skipped: an overlay must never be able
        to stop the slideshow rendering underneath it.
        """
        if not backend:
            return

        elements: list[dict[str, Any]] = []
        for layer in self._layers:
            if not layer.visible:
                continue
            try:
                layer.reset_z()
                # A layer may need the backend to size itself and load its
                # assets (the boot layer cannot know the display dimensions
                # otherwise).  render() takes no arguments because it runs every
                # frame, so any one-off preparation happens here.
                prepare = getattr(layer, "ensure_ready", None)
                if callable(prepare):
                    prepare(backend)
                elements.extend(layer.render())
            except Exception:
                logger.exception("Layer render failed: %s", layer.name)

        if not elements:
            return

        # Descending z: the largest z paints first, the smallest last (closest to
        # the viewer).  Matches the old GL_LESS convention so existing z-offsets
        # in the layers keep their meaning.
        elements.sort(key=lambda e: float(e.get("z", 0.0)), reverse=True)
        try:
            backend.present_overlay(elements)
        except AttributeError:
            # A backend without overlay compositing (or an older one) simply
            # shows no overlay — better than failing the frame.
            logger.debug("Backend does not support overlay compositing")
        except Exception:
            logger.exception("Overlay compositing failed")
