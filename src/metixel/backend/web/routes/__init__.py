# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web API routes."""

from metixel.backend.web.routes.browse import browse_bp
from metixel.backend.web.routes.config import config_bp
from metixel.backend.web.routes.control import control_bp
from metixel.backend.web.routes.ddc import ddc_bp
from metixel.backend.web.routes.health import health_bp
from metixel.backend.web.routes.immich import immich_bp
from metixel.backend.web.routes.input import input_bp
from metixel.backend.web.routes.logs import logs_bp
from metixel.backend.web.routes.media import media_bp
from metixel.backend.web.routes.messages import messages_bp
from metixel.backend.web.routes.network import network_bp
from metixel.backend.web.routes.system import system_bp
from metixel.backend.web.routes.time import time_bp
from metixel.backend.web.routes.updates import updates_bp

__all__ = [
    "browse_bp",
    "config_bp",
    "control_bp",
    "ddc_bp",
    "health_bp",
    "immich_bp",
    "input_bp",
    "logs_bp",
    "media_bp",
    "messages_bp",
    "network_bp",
    "system_bp",
    "time_bp",
    "updates_bp",
]
