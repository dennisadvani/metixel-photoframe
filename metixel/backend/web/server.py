# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Flask web server for the Metixel Photoframe dashboard.

Serves the responsive SPA dashboard on port 8080 and exposes REST API
endpoints for configuration, media management, and system monitoring.
"""

from __future__ import annotations

import logging

from flask import Flask, render_template, request, send_from_directory

from metixel import __version__
from metixel.backend.state import StateManager
from metixel.shared.ipc import IPCClient

logger = logging.getLogger(__name__)


def create_app(state: StateManager, ipc: IPCClient) -> Flask:
    """Create and configure the Flask application.

    Args:
        state: The shared StateManager instance.
        ipc: IPC client for sending commands to the frontend.

    Returns:
        A configured Flask application instance.
    """
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    app.config["METIXEL_STATE"] = state
    app.config["METIXEL_IPC"] = ipc

    # Silence Flask's HTTP access logs (they flood the log output)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Register route blueprints
    from metixel.backend.web.routes.config import config_bp
    from metixel.backend.web.routes.immich import immich_bp
    from metixel.backend.web.routes.logs import logs_bp
    from metixel.backend.web.routes.media import media_bp

    app.register_blueprint(config_bp, url_prefix="/api/config")
    app.register_blueprint(media_bp, url_prefix="/api/media")
    app.register_blueprint(logs_bp, url_prefix="/api/logs")
    app.register_blueprint(immich_bp, url_prefix="/api/immich")

    # Serve the SPA — inject version into the root template
    @app.route("/")
    def index() -> str:
        return render_template("index.html", version=__version__)

    @app.route("/<path:path>")
    def serve_spa(path: str) -> str:
        # Try static files first, fall back to index.html for SPA routing
        try:
            return send_from_directory(app.static_folder or "static", path)
        except Exception:
            return render_template("index.html", version=__version__)

    # Disable caching for static assets in development so the browser
    # always picks up the latest JS/CSS after a page refresh.
    @app.after_request
    def _add_cache_headers(response):
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    logger.info("Flask application created")
    return app
