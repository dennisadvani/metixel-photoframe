# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Media processing pipeline."""

from metixel.backend.processing.image import ImageProcessor
from metixel.backend.processing.video import VideoProcessor
from metixel.backend.processing.matte import MatteGenerator

__all__ = ["ImageProcessor", "VideoProcessor", "MatteGenerator"]
