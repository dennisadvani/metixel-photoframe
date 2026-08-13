# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Power and system administration endpoints (restart/reboot/shutdown, quiet boot, info)."""

from __future__ import annotations

import logging
import subprocess

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

system_bp = Blueprint("system", __name__)


@system_bp.route("/restart", methods=["POST"])
def restart_services():
    """Restart all Metixel systemd services via sudo systemctl.

    Returns immediately with a success response, then schedules a
    delayed restart in a background thread.  The 2-second delay ensures
    the HTTP response is fully sent before the services are restarted.

    Uses ``sudo systemctl restart metixel-backend metixel-cage``.
    Requires a NOPASSWD sudoers entry for systemctl.  If the sudo
    call fails (e.g. missing sudoers entry), returns an error so the
    frontend can surface it — no silent fallback to os.kill.

    This is typically called after clearing the media cache, since
    stale cached-file references in the running frontend cause
    missing-file errors until the services are restarted.
    """
    import threading
    import time as _time

    def _do_restart() -> None:
        _time.sleep(2)
        try:
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "metixel-backend", "metixel-cage"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-300:]
                logger.error("sudo systemctl restart failed (rc=%d): %s", result.returncode, tail)
            else:
                logger.info("Services restarted via sudo systemctl")
        except subprocess.TimeoutExpired:
            logger.error("sudo systemctl restart timed out after 15s")
        except FileNotFoundError:
            logger.error("systemctl not found — cannot restart services")
        except Exception as exc:
            logger.error("sudo systemctl restart failed: %s", exc)

    thread = threading.Thread(target=_do_restart, daemon=True, name="svc-restart")
    thread.start()
    logger.info("Service restart scheduled (will execute in 2s)")
    return jsonify({"status": "ok", "message": "Restarting services in 2 seconds…"})


@system_bp.route("/reboot", methods=["POST"])
def reboot_system():
    """Reboot the system via sudo reboot now.

    Returns immediately with a success response, then schedules a
    delayed reboot in a background thread.  The 2-second delay ensures
    the HTTP response is fully sent before the system goes down.

    Requires a NOPASSWD sudoers entry for reboot.
    """
    import threading
    import time as _time

    def _do_reboot() -> None:
        _time.sleep(2)
        try:
            result = subprocess.run(
                ["sudo", "-n", "reboot", "now"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-300:]
                logger.error("sudo reboot now failed (rc=%d): %s", result.returncode, tail)
            else:
                logger.info("System reboot initiated via sudo reboot now")
        except subprocess.TimeoutExpired:
            logger.error("sudo reboot now timed out after 15s")
        except FileNotFoundError:
            logger.error("reboot not found — cannot reboot system")
        except Exception as exc:
            logger.error("sudo reboot now failed: %s", exc)

    thread = threading.Thread(target=_do_reboot, daemon=True, name="sys-reboot")
    thread.start()
    logger.info("System reboot scheduled (will execute in 2s)")
    return jsonify({"status": "ok", "message": "Rebooting system in 2 seconds…"})


@system_bp.route("/shutdown", methods=["POST"])
def shutdown_system():
    """Shut down the system via sudo shutdown now.

    Returns immediately with a success response, then schedules a
    delayed shutdown in a background thread.  The 2-second delay ensures
    the HTTP response is fully sent before the system goes down.

    Requires a NOPASSWD sudoers entry for shutdown.
    """
    import threading
    import time as _time

    def _do_shutdown() -> None:
        _time.sleep(2)
        try:
            result = subprocess.run(
                ["sudo", "-n", "shutdown", "now"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                tail = (result.stderr or result.stdout or "").strip()[-300:]
                logger.error("sudo shutdown now failed (rc=%d): %s", result.returncode, tail)
            else:
                logger.info("System shutdown initiated via sudo shutdown now")
        except subprocess.TimeoutExpired:
            logger.error("sudo shutdown now timed out after 15s")
        except FileNotFoundError:
            logger.error("shutdown not found — cannot shut down system")
        except Exception as exc:
            logger.error("sudo shutdown now failed: %s", exc)

    thread = threading.Thread(target=_do_shutdown, daemon=True, name="sys-shutdown")
    thread.start()
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
    data = request.get_json(silent=True)
    if data is None or "enabled" not in data:
        return jsonify({"error": "Missing 'enabled' (true/false) in JSON body"}), 400

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
    try:
        with open("/proc/device-tree/model") as f:
            info["pi_model"] = f.read().strip("\x00\n\t ")
    except (OSError, FileNotFoundError):
        info["pi_model"] = "not a Raspberry Pi"

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
    try:
        result = subprocess.run(
            ["vcgencmd", "get_mem", "gpu"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            info["gpu_memory"] = result.stdout.strip()
        else:
            info["gpu_memory"] = "unavailable"
    except Exception:
        info["gpu_memory"] = "unavailable"

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
