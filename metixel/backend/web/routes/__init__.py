# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web API routes."""

from metixel.backend.web.routes.config import config_bp
from metixel.backend.web.routes.media import media_bp
from metixel.backend.web.routes.logs import logs_bp

__all__ = ["config_bp", "media_bp", "logs_bp"]
