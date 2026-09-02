# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests that the frontend renderer passes refresh_rate/rotation to the backend."""

from __future__ import annotations

import json
from unittest import mock

import pytest


@pytest.fixture
def fake_backend():
    """A minimal fake DisplayBackend recording create() kwargs."""
    backend = mock.MagicMock()
    backend.width = 1920
    backend.height = 1080
    backend.is_running = False
    backend.connected_output.return_value = "HDMI-A-2"
    return backend


def _write_config(tmp_path, display_overrides=None):
    """Write a config file with the given display overrides."""
    from metixel.shared.config import DEFAULT_CONFIG

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if display_overrides:
        cfg["display"].update(display_overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_renderer_passes_refresh_and_rotation(tmp_path, fake_backend, monkeypatch):
    """create() receives refresh_rate and rotation from config."""
    from metixel.frontend.renderer import FrontendRenderer

    config_path = _write_config(
        tmp_path,
        {"refresh_rate": 60, "rotation": 90},
    )

    # Stub out the heavy subsystems so run() only exercises the display init.
    monkeypatch.setattr(
        "metixel.frontend.renderer.PresentationEngine",
        lambda *a, **k: mock.MagicMock(),
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.OverlayManager",
        lambda *a, **k: mock.MagicMock(),
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.IPCServer",
        lambda *a, **k: mock.MagicMock(),
    )
    # Prevent the render loop from blocking.
    monkeypatch.setattr(
        "metixel.frontend.renderer.FrontendRenderer._render_loop",
        lambda self: None,
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.FrontendRenderer._shutdown",
        lambda self: None,
    )

    renderer = FrontendRenderer(config_path, backend=fake_backend)
    renderer.run()

    _, kwargs = fake_backend.create.call_args
    assert kwargs["refresh_rate"] == 60
    assert kwargs["rotation"] == 90


def test_renderer_defaults_when_omitted(tmp_path, fake_backend, monkeypatch):
    """create() defaults refresh_rate/rotation to 0 when config omits them."""
    from metixel.frontend.renderer import FrontendRenderer

    config_path = _write_config(tmp_path, {})  # no new keys

    monkeypatch.setattr(
        "metixel.frontend.renderer.PresentationEngine",
        lambda *a, **k: mock.MagicMock(),
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.OverlayManager",
        lambda *a, **k: mock.MagicMock(),
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.IPCServer",
        lambda *a, **k: mock.MagicMock(),
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.FrontendRenderer._render_loop",
        lambda self: None,
    )
    monkeypatch.setattr(
        "metixel.frontend.renderer.FrontendRenderer._shutdown",
        lambda self: None,
    )

    renderer = FrontendRenderer(config_path, backend=fake_backend)
    renderer.run()

    _, kwargs = fake_backend.create.call_args
    assert kwargs["refresh_rate"] == 0
    assert kwargs["rotation"] == 0
