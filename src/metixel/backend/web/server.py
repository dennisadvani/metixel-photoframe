# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Flask web server for the Metixel Photoframe dashboard.

Serves the responsive SPA dashboard on port 8080 and exposes REST API
endpoints for configuration, media management, and system monitoring.
"""

from __future__ import annotations

import logging

from flask import (
    Flask,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from metixel import __version__
from metixel.backend.state import StateManager
from metixel.shared.ipc import IPCClient

logger = logging.getLogger(__name__)


def _is_ap_mode() -> bool:
    """Check whether the captive portal PIN gate is active.

    Serves the captive portal whenever the AP is running or a PIN is
    active.  The NetworkController manages AP lifecycle — if a real
    connection exists, the controller will have already stopped the AP
    and cleared the PIN, so there is no stale-AP case to guard against.

    Calling is_connected() on every HTTP request spawns nmcli, which
    can time out under CPU load and cause the captive portal to flip
    back to the dashboard mid-session.
    """
    try:
        from metixel.backend.network_manager import is_ap_mode_active

        # Check controller PIN first (fast, in-memory)
        app = current_app._get_current_object()  # type: ignore[attr-defined]
        daemon = app.config.get("METIXEL_DAEMON") if app else None
        if daemon is not None:
            controller = getattr(daemon, "_network_controller", None)
            if controller is not None and controller.pin:
                return True

        return is_ap_mode_active()
    except Exception:
        return False


def create_app(
    state: StateManager,
    ipc: IPCClient,
    opt_queue: object | None = None,
    update_mgr: object | None = None,
    daemon: object | None = None,
) -> Flask:
    """Create and configure the Flask application.

    Args:
        state: The shared StateManager instance.
        ipc: IPC client for sending commands to the frontend.
        opt_queue: The OptimisationQueue instance (optional — used by the
            media library API to report per-video transcode status).
        update_mgr: The UpdateManager instance (optional — used by the
            updates API to check for and apply OTA updates).
        daemon: The BackendDaemon instance (optional — used to signal
            slideshow-started for network monitor deferral).

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
    app.config["METIXEL_OPT_QUEUE"] = opt_queue
    app.config["METIXEL_UPDATE_MGR"] = update_mgr
    app.config["METIXEL_DAEMON"] = daemon

    # Hard cap on the request body size.  Media uploads are streamed to disk,
    # but a pathological request must not be allowed to fill tmpfs (RAM) with
    # unbounded multipart data on a small Pi.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3  # 2 GiB

    # Silence Flask's HTTP access logs (they flood the log output)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # Register route blueprints
    from metixel.backend.web.routes.browse import browse_bp
    from metixel.backend.web.routes.config import config_bp
    from metixel.backend.web.routes.control import control_bp
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

    app.register_blueprint(config_bp, url_prefix="/api/config")
    # Each config sub-resource module is registered under its own prefix so
    # the URLs mirror the modules (system/time/input/control/health/browse).
    app.register_blueprint(system_bp, url_prefix="/api/system")
    app.register_blueprint(time_bp, url_prefix="/api/time")
    app.register_blueprint(input_bp, url_prefix="/api/input")
    app.register_blueprint(control_bp, url_prefix="/api/control")
    app.register_blueprint(health_bp, url_prefix="/api/health")
    app.register_blueprint(browse_bp, url_prefix="/api/browse")
    app.register_blueprint(media_bp, url_prefix="/api/media")
    app.register_blueprint(logs_bp, url_prefix="/api/logs")
    app.register_blueprint(immich_bp, url_prefix="/api/immich")
    app.register_blueprint(messages_bp, url_prefix="/api")
    app.register_blueprint(network_bp, url_prefix="/api")
    app.register_blueprint(updates_bp, url_prefix="/api/updates")

    # Serve the SPA — inject version into the root template.
    # When AP mode is active, serve the captive portal instead of the
    # dashboard so users can configure Wi-Fi.
    @app.route("/")
    def index() -> str:
        if _is_ap_mode():
            return render_template("captive.html")
        return render_template("index.html", version=__version__)

    # Preview route for testing the captive portal without activating AP mode
    @app.route("/captive")
    def captive_preview() -> str:
        return render_template("captive.html")

    # ── Frontend → backend signal: slideshow has started ──────────────
    # The frontend renderer calls this once the initial media queue is
    # loaded and the slideshow begins playing.  The network monitor
    # defers its AP-fallback countdown until this signal arrives, so it
    # doesn't compete for CPU / I/O during the initial processing phase.
    @app.route("/api/slideshow-started", methods=["POST"])
    def slideshow_started():
        daemon_obj = current_app.config.get("METIXEL_DAEMON")
        if daemon_obj is not None:
            event = getattr(daemon_obj, "_slideshow_started", None)
            if event is not None and not event.is_set():
                event.set()
                logger.info("Slideshow started signal received — network monitor unblocked")
        return jsonify({"status": "ok"})

    @app.route("/<path:path>")
    def serve_spa(path: str) -> Response | str:
        # Always serve static files, even when PIN gate is active
        try:
            return send_from_directory(app.static_folder or "static", path)
        except Exception:
            pass
        # Block dashboard access when the PIN gate is active
        if _is_ap_mode():
            return render_template("captive.html")
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
