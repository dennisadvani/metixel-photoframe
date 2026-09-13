# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the frontend."""

from metixel.framing.layout import LayoutEngine, RenderPlan
from metixel.framing.resolve import resolve
from metixel.frontend.presentation.presenter import Presenter
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.frontend.renderer import FrontendRenderer


def test_imports():
    """Verify the frontend modules that exist are importable.

    This module previously asserted ``presentation.engine`` and
    ``presentation.layout``.  Both were removed in the 2.0.0 rework — the mixin
    engine collapsed into ``Presenter`` and the layout geometry moved to
    ``metixel.framing`` — so the test was asserting the existence of deleted
    modules and would have failed if it had been collected.  It was not, which is
    why the staleness survived; it now imports at module scope so a future removal
    fails loudly instead of silently.
    """
    assert FrontendRenderer is not None
    assert Presenter is not None
    assert TransitionEngine is not None
    # Geometry now lives in metixel.framing and is DisplayBackend-free.
    assert LayoutEngine is not None
    assert RenderPlan is not None
    assert resolve is not None
