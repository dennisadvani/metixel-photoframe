# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the backend daemon."""


def test_backend_imports():
    """Verify backend modules can be imported."""
    from metixel.backend.daemon import BackendDaemon
    from metixel.backend.state import StateManager

    assert BackendDaemon is not None
    assert StateManager is not None


def test_state_manager_init(tmp_path):
    """Verify StateManager initializes and loads config."""
    from metixel.backend.state import StateManager
    from metixel.shared.config import Config

    # Create a config file
    config_path = tmp_path / "config.json"
    config = Config()
    config.save(config_path)

    run_dir = tmp_path / "run"
    state = StateManager(config_path, run_dir)

    assert state.config is not None
    assert state.config.display["width"] == 1920
