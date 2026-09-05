# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Authentication endpoints — login, logout, and session status.

These routes back the optional web-dashboard password (Part A).  The
screen-PIN set/change/clear routes live in ``routes/security.py`` alongside
the device-password routes; this module only handles the web session.
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, jsonify, session

from metixel import __version__
from metixel.backend.web.auth import WebAuthService
from metixel.backend.web.helpers import get_body, jsonify_error

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

#: Session keys
_SESSION_AUTH = "authenticated"
_SESSION_LOGIN_TIME = "login_time"


def get_auth_service() -> WebAuthService:
    """Return the shared WebAuthService from the Flask app config."""
    service = current_app.config.get("METIXEL_AUTH")
    if service is None:
        # Fall back to a service bound to the state manager (should not
        # happen in normal operation, but keeps the route safe).
        state = current_app.config["METIXEL_STATE"]
        service = WebAuthService(state)
        current_app.config["METIXEL_AUTH"] = service
    return service


def is_authenticated() -> bool:
    """Return whether the current session is authenticated (and not expired)."""
    if not session.get(_SESSION_AUTH):
        return False
    service = get_auth_service()
    timeout = service.session_timeout_minutes()
    if timeout <= 0:
        return True  # 0 = no idle timeout (forever)
    login_time = session.get(_SESSION_LOGIN_TIME, 0.0)
    if not login_time:
        return False
    return (time.time() - float(login_time)) < timeout * 60


@auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate against the optional web password.

    Sets a signed session cookie (HttpOnly, SameSite=Lax) on success.
    The session id is regenerated on login to prevent session fixation.
    """
    service = get_auth_service()
    if not service.is_enabled():
        return jsonify({"authenticated": True, "message": "Auth disabled"})

    if service.is_locked():
        remaining = service.lock_remaining_seconds()
        return jsonify_error(
            f"Too many attempts. Try again in {remaining}s.",
            429,
            locked=True,
            retry_after=remaining,
        )

    body = get_body()
    password = body.get("password", "")
    if not password:
        return jsonify_error("Password required", 400)

    if service.verify(password):
        # Regenerate the session id to prevent session fixation.
        session.clear()
        session[_SESSION_AUTH] = True
        session[_SESSION_LOGIN_TIME] = time.time()
        logger.info("Web login successful")
        return jsonify({"authenticated": True, "message": "ok"})

    return jsonify_error("Incorrect password", 401)


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Clear the session cookie (log out)."""
    session.clear()
    return jsonify({"status": "ok", "message": "Logged out"})


@auth_bp.route("/me", methods=["GET"])
def me():
    """Return auth status + frame identity for the SPA boot gate."""
    service = get_auth_service()
    return jsonify(
        {
            "enabled": service.is_enabled(),
            "authenticated": is_authenticated(),
            "session_timeout_minutes": service.session_timeout_minutes(),
            "version": __version__,
        }
    )


@auth_bp.route("/password", methods=["POST"])
def set_password():
    """Set or change the web dashboard password (from the Settings page).

    Only reachable when already authenticated (the auth gate protects this
    route).  An empty password clears it (auth disabled).
    """
    service = get_auth_service()
    body = get_body()
    password = body.get("password", "")
    if password:
        if len(password) < 8:
            return jsonify_error("Password must be at least 8 characters", 400)
        service.set_password(password)
        return jsonify({"status": "ok", "message": "Web password set"})
    service.clear_password()
    return jsonify({"status": "ok", "message": "Web password cleared"})
