# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the frontend."""


def test_imports():
    """Verify frontend modules can be imported."""
    from metixel.frontend.presentation.engine import PresentationEngine
    from metixel.frontend.presentation.layout import LayoutEngine
    from metixel.frontend.presentation.transitions import TransitionEngine
    from metixel.frontend.renderer import FrontendRenderer

    assert FrontendRenderer is not None
    assert PresentationEngine is not None
    assert TransitionEngine is not None
    assert LayoutEngine is not None
