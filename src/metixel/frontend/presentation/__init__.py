# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Presentation — slideshow timing, media caching and transitions.

The slideshow itself is :class:`~metixel.frontend.presentation.presenter.Presenter`.
It was previously spread across a facade plus five mixins, because the pi3d
backend kept mutable GPU-texture state that made a clean class boundary risky to
introduce in one step.  That state is gone, so the split is gone with it.

Layout is **not** re-exported here on purpose: geometry lives in
:mod:`metixel.framing.layout`, which has no dependency on the display layer and is
testable without Qt.  Re-exporting it would blur that boundary.
"""

from metixel.frontend.presentation.image_cache import ImageCache
from metixel.frontend.presentation.presenter import Presenter
from metixel.frontend.presentation.transitions import TransitionEngine

__all__ = ["ImageCache", "Presenter", "TransitionEngine"]
