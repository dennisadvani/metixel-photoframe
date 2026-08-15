# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Shared web-layer helpers.

Centralises the three divergent error-response shapes, the repeated
``request.get_json(silent=True)`` + field-validation boilerplate, and the
``current_app.config.get(...)`` / ``getattr(daemon, "_x", None)`` accessor
patterns that were hand-rolled across every route module.

* :func:`jsonify_error` — a single, consistent error shape that always
  carries ``status``, ``error`` and ``message`` (so both the dashboard JS
  reading ``data.error`` / ``data.message`` and the existing tests
  asserting ``"error" in data`` keep working).
* :func:`get_body` — parse the JSON request body, defaulting to ``{}``.
* :func:`require_fields` — validate required body fields, returning a
  ready-to-return error response if any are missing.
* :func:`get_daemon_component` — read an attribute off the daemon without
  repeating the ``current_app.config.get("METIXEL_DAEMON")`` guard.
* :func:`register_error_handlers` — install global Flask 400/404/405/500
  handlers so unhandled exceptions and malformed requests return the
  unified error shape instead of Flask's default HTML/JSON.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, current_app, jsonify, request

logger = logging.getLogger(__name__)


def jsonify_error(
    message: str,
    status: int = 400,
    **extra: Any,
) -> tuple[Any, int]:
    """Return a JSON error response with a consistent shape.

    Always includes ``status: "error"``, ``error: message`` and
    ``message: message`` so clients and tests can rely on one contract,
    plus any ``extra`` keys (e.g. ``hint``, ``valid_sections``).
    """
    payload: dict[str, Any] = {
        "status": "error",
        "error": message,
        "message": message,
    }
    payload.update(extra)
    return jsonify(payload), status


def get_body() -> dict[str, Any]:
    """Return the parsed JSON request body, or ``{}`` if absent/invalid."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def require_fields(
    body: dict[str, Any],
    *fields: str,
    message: str | None = None,
) -> tuple[Any, int] | None:
    """Validate that *body* contains every *field* (truthy, non-empty).

    Returns ``None`` when all fields are present, otherwise a ready-to-
    return :func:`jsonify_error` response naming the missing fields.
    """
    missing = [f for f in fields if not body.get(f)]
    if missing:
        msg = message or f"Missing required field(s): {', '.join(missing)}"
        return jsonify_error(msg, 400)
    return None


def get_daemon_component(name: str, default: Any = None) -> Any:
    """Return attribute *name* off the daemon, or *default* if unavailable.

    Wraps the ``current_app.config.get("METIXEL_DAEMON")`` guard so route
    modules don't each repeat it.
    """
    daemon = current_app.config.get("METIXEL_DAEMON")
    if daemon is None:
        return default
    return getattr(daemon, name, default)


def register_error_handlers(app: Flask) -> None:
    """Install global error handlers on *app* returning the unified shape.

    Handles:
    * ``400`` — bad request (e.g. malformed JSON, oversized body)
    * ``404`` — unknown route
    * ``405`` — wrong method
    * ``500`` — unhandled exception
    """

    @app.errorhandler(400)
    def _bad_request(exc):
        message = "Bad request"
        if hasattr(exc, "description") and exc.description:
            message = exc.description
        logger.warning("HTTP 400: %s", message)
        return jsonify_error(message, 400)

    @app.errorhandler(404)
    def _not_found(exc):
        logger.info("HTTP 404: %s", request.path)
        return jsonify_error("Not found", 404)

    @app.errorhandler(405)
    def _method_not_allowed(exc):
        return jsonify_error("Method not allowed", 405)

    @app.errorhandler(500)
    def _internal_error(exc):
        logger.exception("HTTP 500 on %s", request.path)
        return jsonify_error("Internal server error", 500)
