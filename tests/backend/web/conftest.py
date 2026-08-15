# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Shared fixtures for the web API test suites.

These build the *real* Flask application via ``create_app()`` — registering
every blueprint exactly as production does — with a real ``StateManager``
backed by a temp config file and mocked outbound dependencies (IPC channel,
OTA update manager).  Routes are then exercised through ``app.test_client()``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def mock_state(tmp_path: Path):
    """A real StateManager backed by a temp config file."""
    from metixel.backend.state import StateManager

    config_path = tmp_path / "config.json"
    # Point the cache dir (and therefore the processing journal) at the temp
    # directory so tests never write into the workspace.
    config_path.write_text(
        json.dumps({"system": {"cache_dir": str(tmp_path / "cache")}}),
        encoding="utf-8",
    )
    return StateManager(config_path, run_dir=tmp_path / "run")


@pytest.fixture
def mock_ipc():
    """MagicMock stand-in for the IPC client (frontend command channel)."""
    return mock.MagicMock()


@pytest.fixture
def mock_update_manager():
    """MagicMock stand-in for the OTA UpdateManager."""
    return mock.MagicMock()


@pytest.fixture
def app(mock_state, mock_ipc, mock_update_manager):
    """The real Flask app wired with mocked outbound dependencies."""
    from metixel.backend.web.server import create_app

    return create_app(
        mock_state,
        mock_ipc,
        opt_queue=None,
        update_mgr=mock_update_manager,
        daemon=None,
    )


@pytest.fixture
def client(app):
    """Flask test client bound to the wired app."""
    return app.test_client()
