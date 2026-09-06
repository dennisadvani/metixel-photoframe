# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Security endpoints — synced device password + screen PIN.

* ``POST /api/system/device-password`` — change the Pi console password
  (``chpasswd``) and the Samba share password (``smbpasswd``) together, so
  the two stores stay in sync as a single "device password".  Runs via
  ``sudo -n`` (NOPASSWD sudoers entry).  Because sudo is non-interactive,
  the backend cannot verify the current password — authenticity is bound to
  the authenticated web session instead (no "current password" field).
* ``POST /api/auth/screen-pin`` — set/change/clear the optional on-screen
  UI PIN (Part C).  Independent of the web password and device password.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.auth import (
    ScreenPinService,
    pins_match,
    validate_pin_input,
)
from metixel.backend.web.helpers import get_body, jsonify_error
from metixel.shared.platform import is_raspberry_pi
from metixel.shared.subprocess import run_cmd

logger = logging.getLogger(__name__)

security_bp = Blueprint("security", __name__)

#: Minimum device-password length.
DEVICE_PASSWORD_MIN_LENGTH = 8
#: The system user whose console + Samba passwords are kept in sync.
DEVICE_USER = "pi"


def _run_privileged(cmd: list[str], input: str | None = None):
    """Run a password command as root in a fresh, non-hardened namespace.

    The backend service is hardened (`ProtectHome=yes` + `ProtectSystem=full`),
    so a plain ``sudo -n`` child inherits the read-only mounts and cannot write
    ``/etc/shadow`` (chpasswd/PAM) or Samba's passdb.  This escapes the
    hardened mount namespace by launching a transient root unit via
    ``sudo -n systemd-run`` — the same mechanism the OTA / dependency self-heal
    use.  ``--pipe`` connects the unit's stdio to ours so ``chpasswd`` /
    ``smbpasswd`` can read the password from stdin.
    """
    return run_cmd(
        [
            "sudo",
            "-n",
            "systemd-run",
            "--wait",
            "--collect",
            "--pipe",
            "--unit=metixel-passwd",
            *cmd,
        ],
        input=input,
        timeout=30,
    )


def _get_screen_pin_service() -> ScreenPinService:
    """Return the shared ScreenPinService from the Flask app config."""
    service = current_app.config.get("METIXEL_SCREEN_PIN")
    if service is None:
        state = current_app.config["METIXEL_STATE"]
        service = ScreenPinService(state)
        current_app.config["METIXEL_SCREEN_PIN"] = service
    return service


@security_bp.route("/system/device-password", methods=["POST"])
def change_device_password():
    """Change the synced device password (SSH console + Samba share).

    Runs ``sudo -n chpasswd`` (console) then ``sudo -n smbpasswd -a -s pi``
    (Samba).  Both must succeed to keep the stores in sync; if the second
    fails after the first succeeds, the partial state is reported explicitly
    (never silently left out of sync).
    """
    if not is_raspberry_pi():
        return jsonify_error("Device password is only supported on Raspberry Pi", 400)

    body = get_body()
    new_password = body.get("new_password", "")
    confirm = body.get("confirm_password", "")

    if not new_password:
        return jsonify_error("New password required", 400)
    if len(new_password) < DEVICE_PASSWORD_MIN_LENGTH:
        return jsonify_error(
            f"Password must be at least {DEVICE_PASSWORD_MIN_LENGTH} characters", 400
        )
    if new_password != confirm:
        return jsonify_error("Passwords do not match", 400)

    # 1. Console password via chpasswd (reads "user:password" from stdin) and
    # 2. Samba password via smbpasswd -a -s (reads "new\nnew" from stdin).
    #
    # The backend service is hardened (ProtectHome=yes + ProtectSystem=full),
    # so a plain sudo child inherits the read-only '/' and cannot write
    # /etc/shadow or Samba's passdb.  Run the password commands in a fresh,
    # non-hardened transient unit via systemd-run (same mechanism the OTA and
    # dependency self-heal use) so they can actually apply.
    console_result = _run_privileged(
        ["chpasswd"],
        input=f"{DEVICE_USER}:{new_password}\n",
    )
    if console_result.returncode != 0:
        tail = (console_result.stderr or console_result.stdout or "").strip()[-300:]
        logger.error("chpasswd failed (rc=%d): %s", console_result.returncode, tail)
        return jsonify_error("Failed to change console password", 500)

    samba_result = _run_privileged(
        ["smbpasswd", "-a", "-s", DEVICE_USER],
        input=f"{new_password}\n{new_password}\n",
    )
    if samba_result.returncode != 0:
        tail = (samba_result.stderr or samba_result.stdout or "").strip()[-300:]
        logger.error(
            "smbpasswd failed (rc=%d) after chpasswd succeeded — stores out of sync: %s",
            samba_result.returncode,
            tail,
        )
        return jsonify(
            {
                "status": "partial",
                "message": (
                    "Console password changed, but the Samba password could not be "
                    "updated. The two are now out of sync."
                ),
                "console": "ok",
                "samba": "failed",
                "detail": tail[:300],
            }
        ), 500

    logger.info("Device password changed (console + Samba) for user %s", DEVICE_USER)
    return jsonify(
        {
            "status": "ok",
            "message": "Device password changed. It now applies to SSH login and the Samba share.",
            "console": "ok",
            "samba": "ok",
        }
    )


@security_bp.route("/auth/screen-pin", methods=["POST"])
def set_screen_pin():
    """Set, change, or clear the optional on-screen UI PIN.

    Body: ``{"pin": "...", "confirm": "..."}`` to set/change, or
    ``{"clear": true}`` to clear.  PINs are 4-6 digits, stored as a salted
    hash, and are independent of the web password and device password.
    """
    service = _get_screen_pin_service()
    body = get_body()

    if body.get("clear"):
        service.clear_pin()
        return jsonify({"status": "ok", "message": "Screen PIN cleared"})

    pin = body.get("pin", "")
    confirm = body.get("confirm", "")
    if not validate_pin_input(pin):
        return jsonify_error("PIN must be 4-6 digits", 400)
    if not pins_match(pin, confirm):
        return jsonify_error("PINs do not match", 400)

    service.set_pin(pin)
    return jsonify({"status": "ok", "message": "Screen PIN set"})


@security_bp.route("/auth/screen-pin/status", methods=["GET"])
def screen_pin_status():
    """Return whether a screen PIN is set (not the PIN itself)."""
    service = _get_screen_pin_service()
    return jsonify(
        {
            "enabled": service.is_enabled(),
            "timeout_minutes": service.timeout_minutes(),
        }
    )
