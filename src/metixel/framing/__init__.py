# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Metixel adaptive framing engine.

Pure geometry / logic layer for adaptive photo framing on digital displays.

Two modules:

``framing_engine``
    The engine.  Explicit **millimetres** in, rectangles out.  Holds no style
    knowledge.

``framing_templates``
    All style knowledge: ring presets, screen presets, ``build_request()``.

The specification is ``docs/geometry-model.md``, which is authoritative.

    from metixel import MediaDescriptor, calculate_framing
    from metixel import framing_templates as templates

    screen = templates.METIXEL_16_10_1920x1200
    request = templates.build_request(screen, MediaDescriptor(3, 2), "gallery")
    result = calculate_framing(request)
"""

from __future__ import annotations

from . import framing_templates
from .framing_engine import (
    MAX_FOCAL_SHIFT_X,
    MAX_FOCAL_SHIFT_Y,
    PANORAMA_THRESHOLD,
    PORTRAIT_THRESHOLD,
    SQUARE_MAX,
    AmbientFillResult,
    AmbientFillSpec,
    ArtworkResult,
    Effects,
    Face,
    FocalPoint,
    FrameResult,
    FramingRequest,
    FramingResult,
    Insets,
    MatResult,
    MediaDescriptor,
    MediaType,
    Metrics,
    Orientation,
    Rect,
    Screen,
    WhitespaceResult,
    WhitespaceSpec,
    calculate_framing,
    check_invariants,
    classify_aspect,
    ring_target_mm,
)

__version__ = "1.0.0"

__all__ = [
    "framing_templates",
    # constants
    "PORTRAIT_THRESHOLD",
    "SQUARE_MAX",
    "PANORAMA_THRESHOLD",
    "MAX_FOCAL_SHIFT_X",
    "MAX_FOCAL_SHIFT_Y",
    # helpers
    "classify_aspect",
    "ring_target_mm",
    "check_invariants",
    # types
    "MediaType",
    "Orientation",
    # value types
    "Rect",
    "Insets",
    "FocalPoint",
    "Face",
    # inputs
    "MediaDescriptor",
    "Screen",
    "WhitespaceSpec",
    "AmbientFillSpec",
    "Effects",
    "FramingRequest",
    # outputs
    "FrameResult",
    "MatResult",
    "WhitespaceResult",
    "AmbientFillResult",
    "ArtworkResult",
    "Metrics",
    "FramingResult",
    # entry point
    "calculate_framing",
    "__version__",
]
