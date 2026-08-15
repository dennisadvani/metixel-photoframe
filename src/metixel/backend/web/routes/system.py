# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Power and system administration endpoints (restart/reboot/shutdown, quiet boot, info)."""

from __future__ import annotations

import logging
import subprocess

from flask import Blueprint, current_app, jsonify

from metixel.backend.web.helpers import get_body, get_daemon_component, jsonify_error
from metixel.shared.platform import read_device_tree_model, read_vcgencmd_mem_str
from metixel.shared.subprocess import schedule_sudo

logger = logging.getLogger(__name__)

system_bp = Blueprint("system", __name__)


def _schedule_sudo(
    cmd: list[str],
    *,
    ok_message: str,
    fail_message: str,
    thread_name: str,
    delay: float = 2.0,
) -> None:
    """Run ``sudo -n <cmd>`` in a background thread after a short delay.

    The delay lets the HTTP response flush before the service reboots
    or the system shuts down.  Requires a NOPASSWD sudoers entry for
    the command.  Failures are logged (never raised) so the endpoint
    returns immediately and errors surface in the journal.
    """
    schedule_sudo(
        cmd,
        ok_message=ok_message,
        fail_message=fail_message,
        thread_name=thread_name,
        delay=delay,
    )


@system_bp.route("/mqtt-status", methods=["GET"])
def mqtt_status():
    """Report the MQTT broker connection state for the dashboard."""
    client = get_daemon_component("_mqtt_client")
    if client is not None and hasattr(client, "status"):
        return jsonify(client.status())

    state = current_app.config.get("METIXEL_STATE")
    if state is None:
        return jsonify({"enabled": False, "status": "unknown"})
    enabled = bool(state.config.mqtt.get("enabled"))
    return jsonify(
        {
            "enabled": enabled,
            "status": "disabled" if not enabled else "unknown",
            "broker": state.config.mqtt.get("broker"),
            "port": state.config.mqtt.get("port"),
        }
    )


@system_bp.route("/restart", methods=["POST"])
def restart_services():
    """Restart all Metixel systemd services via sudo systemctl.

    Returns immediately, then restarts ``metixel-backend`` and
    ``metixel-cage`` after a 2-second delay so the response is fully
    sent first.  Typically called after clearing the media cache so
    stale cached-file references are dropped.
    """
    _schedule_sudo(
        ["systemctl", "restart", "metixel-backend", "metixel-cage"],
        ok_message="Services restarted via sudo systemctl",
        fail_message="sudo systemctl restart",
        thread_name="svc-restart",
    )
    logger.info("Service restart scheduled (will execute in 2s)")
    return jsonify({"status": "ok", "message": "Restarting services in 2 seconds…"})


@system_bp.route("/reboot", methods=["POST"])
def reboot_system():
    """Reboot the system via ``sudo reboot now``.

    Returns immediately, then reboots after a 2-second delay so the
    response is fully sent first.  Requires a NOPASSWD sudoers entry
    for reboot.
    """
    _schedule_sudo(
        ["reboot", "now"],
        ok_message="System reboot initiated via sudo reboot now",
        fail_message="sudo reboot now",
        thread_name="sys-reboot",
    )
    logger.info("System reboot scheduled (will execute in 2s)")
    return jsonify({"status": "ok", "message": "Rebooting system in 2 seconds…"})


@system_bp.route("/shutdown", methods=["POST"])
def shutdown_system():
    """Shut down the system via ``sudo shutdown now``.

    Returns immediately, then shuts down after a 2-second delay so the
    response is fully sent first.  Requires a NOPASSWD sudoers entry
    for shutdown.
    """
    _schedule_sudo(
        ["shutdown", "now"],
        ok_message="System shutdown initiated via sudo shutdown now",
        fail_message="sudo shutdown now",
        thread_name="sys-shutdown",
    )
    logger.info("System shutdown scheduled (will execute in 2s)")
    return jsonify({"status": "ok", "message": "Shutting down system in 2 seconds…"})


@system_bp.route("/quiet-boot", methods=["POST"])
def toggle_quiet_boot():
    """Enable or disable quiet boot via the quiet_boot.sh script.

    Accepts JSON: ``{"enabled": true|false}``

    Runs ``sudo bash /opt/metixel/scripts/quiet_boot.sh`` (or ``--revert``)
    to modify kernel cmdline parameters and mask/unmask getty on tty1.
    The change takes effect on the **next reboot** — it does NOT reboot
    the system automatically.

    Requires a NOPASSWD sudoers entry for bash.
    """
    data = get_body()
    if "enabled" not in data:
        return jsonify_error("Missing 'enabled' (true/false) in JSON body", 400)

    enabled = bool(data["enabled"])
    script = "/opt/metixel/scripts/quiet_boot.sh"
    args = ["sudo", "-n", "bash", script]
    if not enabled:
        args.append("--revert")
    args.append("/")

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            logger.error("quiet_boot.sh failed (rc=%d): %s", result.returncode, tail)
            return jsonify(
                {
                    "status": "error",
                    "message": "Script failed. Check server logs.",
                    "detail": tail[:300],
                }
            ), 500
        logger.info(
            "Quiet boot %s via quiet_boot.sh",
            "enabled" if enabled else "disabled",
        )
        return jsonify(
            {
                "status": "ok",
                "quiet_boot": enabled,
                "message": ("Quiet boot enabled." if enabled else "Quiet boot disabled."),
            }
        )
    except subprocess.TimeoutExpired:
        logger.error("quiet_boot.sh timed out after 30s")
        return jsonify({"status": "error", "message": "Script timed out."}), 500
    except FileNotFoundError:
        logger.error("quiet_boot.sh not found at %s", script)
        return jsonify({"status": "error", "message": "quiet_boot.sh not found."}), 500
    except Exception as exc:
        logger.exception("quiet_boot.sh failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@system_bp.route("/info", methods=["GET"])
def get_system_info():
    """Return system and version information for the Updates card.

    Gathers app version, Pi hardware model, OS release, kernel version,
    Python version, pi3d version, GPU memory, and DRM driver — all via
    local /proc, /sys, and vcgencmd reads.  No external dependencies.
    """
    import os as _os
    import platform as _platform
    import sys as _sys

    info: dict[str, str] = {}

    # -- App version ---------------------------------------------------------
    try:
        from metixel import __version__

        info["app_version"] = __version__
    except Exception:
        info["app_version"] = "unknown"

    # -- Pi hardware model ---------------------------------------------------
    info["pi_model"] = read_device_tree_model() or "not a Raspberry Pi"

    # -- OS release ----------------------------------------------------------
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os_release"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except (OSError, FileNotFoundError):
        pass
    if "os_release" not in info:
        info["os_release"] = _platform.platform() or "unknown"

    # -- Kernel version ------------------------------------------------------
    info["kernel"] = _platform.release() or "unknown"

    # -- Python version ------------------------------------------------------
    info["python_version"] = _sys.version.split()[0]

    # -- pi3d version --------------------------------------------------------
    try:
        import pi3d

        info["pi3d_version"] = getattr(pi3d, "__version__", "installed")
    except ImportError:
        info["pi3d_version"] = "not installed"

    # -- GPU memory ----------------------------------------------------------
    info["gpu_memory"] = read_vcgencmd_mem_str("gpu", fallback="unavailable")

    # -- DRM driver ----------------------------------------------------------
    try:
        for entry in _os.listdir("/sys/class/drm"):
            if entry.startswith("card"):
                driver_link = f"/sys/class/drm/{entry}/device/driver"
                if _os.path.islink(driver_link):
                    info["drm_driver"] = _os.path.basename(_os.readlink(driver_link))
                    break
        else:
            info["drm_driver"] = "none"
    except Exception:
        info["drm_driver"] = "unknown"

    # -- Hostname ------------------------------------------------------------
    info["hostname"] = _platform.node() or "unknown"

    return jsonify(info)
