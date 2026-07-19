# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Backend daemon — sync engines, web server, MQTT, input handlers."""

from metixel.backend.daemon import BackendDaemon
from metixel.backend.state import StateManager

__all__ = ["BackendDaemon", "StateManager"]
