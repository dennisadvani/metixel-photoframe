# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the frontend."""


def test_imports():
    """Verify frontend modules can be imported.

    The frontend used to expose this through ``presentation.engine`` (and
    ``presentation.layout``); 2.0.0 replaced that with ``presentation.presenter``
    and moved the layout engine into the ``framing`` package, so this lists the
    modules that actually exist rather than the ones the old renderer had.
    """
    from metixel.framing.layout import LayoutEngine
    from metixel.frontend.presentation.presenter import Presenter
    from metixel.frontend.presentation.transitions import TransitionEngine
    from metixel.frontend.renderer import FrontendRenderer

    assert FrontendRenderer is not None
    assert Presenter is not None
    assert TransitionEngine is not None
    assert LayoutEngine is not None
