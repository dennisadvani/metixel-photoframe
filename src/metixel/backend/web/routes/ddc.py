# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""DDC/CI monitor-control API endpoints."""

from __future__ import annotations

import logging
import re

from flask import Blueprint, current_app, jsonify, request

from metixel.backend.display_control.ddc_service import DdcFeatureError, DdcUnavailableError
from metixel.backend.web.helpers import get_body, jsonify_error

logger = logging.getLogger(__name__)

ddc_bp = Blueprint("ddc", __name__)

_CODE_RE = re.compile(r"^(?:0x)?([0-9A-Fa-f]{1,2})$")


def _service():
    svc = current_app.config.get("METIXEL_DDC")
    if svc is not None:
        return svc
    daemon = current_app.config.get("METIXEL_DAEMON")
    if daemon is not None:
        return getattr(daemon, "_ddc_service", None)
    return None


def _require_service():
    svc = _service()
    if svc is None:
        return None, jsonify_error("DDC/CI service not available", 503)
    return svc, None


def _parse_code(raw: str) -> int | None:
    m = _CODE_RE.match(str(raw).strip())
    if not m:
        return None
    return int(m.group(1), 16)


@ddc_bp.route("/status", methods=["GET"])
def ddc_status():
    """Return DDC enablement, availability, and detected monitors."""
    svc, err = _require_service()
    if err:
        return err
    return jsonify(svc.status())


@ddc_bp.route("/capabilities", methods=["GET"])
def ddc_capabilities():
    """Return user-facing VCP features for the configured/selected display."""
    svc, err = _require_service()
    if err:
        return err
    display = request.args.get("display", type=int)
    try:
        return jsonify(svc.capabilities(display=display))
    except Exception:
        logger.exception("DDC capabilities probe failed")
        return jsonify_error("Failed to probe monitor capabilities", 500)


@ddc_bp.route("/vcp/<code>", methods=["GET"])
def ddc_get_vcp(code: str):
    """Read a single VCP feature."""
    svc, err = _require_service()
    if err:
        return err
    parsed = _parse_code(code)
    if parsed is None:
        return jsonify_error(f"Invalid VCP code: {code}", 400)
    display = request.args.get("display", type=int)
    try:
        return jsonify(svc.get_vcp(parsed, display=display))
    except DdcUnavailableError as exc:
        return jsonify({"available": False, "reason": str(exc)}), 200
    except DdcFeatureError as exc:
        return jsonify_error(str(exc), 400)
    except Exception:
        logger.exception("DDC getvcp failed for 0x%02X", parsed)
        return jsonify_error("Failed to read VCP feature", 500)


@ddc_bp.route("/vcp/<code>", methods=["PUT"])
def ddc_set_vcp(code: str):
    """Write a single VCP feature. Body: ``{"value": N}``."""
    svc, err = _require_service()
    if err:
        return err
    parsed = _parse_code(code)
    if parsed is None:
        return jsonify_error(f"Invalid VCP code: {code}", 400)
    body = get_body()
    if "value" not in body:
        return jsonify_error("Missing required field(s): value", 400)
    try:
        value = int(body["value"])
    except (TypeError, ValueError):
        return jsonify_error("value must be an integer", 400)
    display = body.get("display")
    if display is not None:
        try:
            display = int(display)
        except (TypeError, ValueError):
            return jsonify_error("display must be an integer", 400)
    try:
        result = svc.set_vcp(parsed, value, display=display)
        return jsonify({"status": "ok", **result})
    except DdcUnavailableError as exc:
        return jsonify({"status": "error", "available": False, "reason": str(exc)}), 200
    except DdcFeatureError as exc:
        return jsonify_error(str(exc), 400)
    except Exception:
        logger.exception("DDC setvcp failed for 0x%02X", parsed)
        return jsonify_error("Failed to set VCP feature", 500)


@ddc_bp.route("/refresh", methods=["POST"])
def ddc_refresh():
    """Invalidate caches and re-probe the monitor."""
    svc, err = _require_service()
    if err:
        return err
    try:
        return jsonify(svc.refresh())
    except Exception:
        logger.exception("DDC refresh failed")
        return jsonify_error("Failed to refresh DDC state", 500)


@ddc_bp.route("/reset", methods=["POST"])
def ddc_reset():
    """Restore the monitor to factory defaults (VCP 0x04)."""
    svc, err = _require_service()
    if err:
        return err
    body = get_body() or {}
    display = body.get("display")
    if display is not None:
        try:
            display = int(display)
        except (TypeError, ValueError):
            return jsonify_error("display must be an integer", 400)
    try:
        return jsonify(svc.reset_factory(display=display))
    except DdcUnavailableError as exc:
        return jsonify({"status": "error", "available": False, "reason": str(exc)}), 200
    except DdcFeatureError as exc:
        return jsonify_error(str(exc), 400)
    except Exception:
        logger.exception("DDC factory reset failed")
        return jsonify_error("Failed to reset monitor to factory defaults", 500)
