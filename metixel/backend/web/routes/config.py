# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Configuration API endpoints."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

config_bp = Blueprint("config", __name__)


@config_bp.route("", methods=["GET"])
def get_config():
    """Get the full current configuration."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify(state.config.to_dict())


@config_bp.route("/<section>", methods=["GET"])
def get_config_section(section: str):
    """Get a specific config section."""
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    if section not in config.to_dict():
        return jsonify({"error": f"Unknown config section: {section}"}), 404
    return jsonify(config.to_dict()[section])


@config_bp.route("/<section>", methods=["PUT"])
def update_config_section(section: str):
    """Update a config section. Triggers hot reload in the frontend."""
    state = current_app.config["METIXEL_STATE"]
    data = request.get_json(silent=True)
    if data is None:
        logger.warning("PUT /%s: invalid or missing JSON body (Content-Type: %s)",
                       section, request.content_type)
        return jsonify({"error": "Invalid JSON body", "hint": "Send JSON with Content-Type: application/json"}), 400

    try:
        logger.info("PUT /%s: updating with keys=%s", section, list(data.keys()))
        state.update_config(section, data)
        logger.info("Config section '%s' updated via API — saved to %s", section, state.config_path)

        # Immediately notify the OptimisationQueue of config changes
        # so queued items are re-classified without waiting for the
        # 30-second periodic reload cycle.  This is especially important
        # when the user toggles transcoding on/off.
        opt_queue = current_app.config.get("METIXEL_OPT_QUEUE")
        if opt_queue is not None and section in ("video", "image"):
            try:
                opt_queue.reload_config()
            except Exception:
                logger.debug("OptimisationQueue reload failed", exc_info=True)

        return jsonify({
            "status": "ok",
            "section": section,
            "config_path": str(state.config_path),
        })
    except KeyError:
        return jsonify({
            "error": f"Unknown config section: {section}",
            "valid_sections": list(state.config.to_dict().keys()),
        }), 404
    except Exception as e:
        logger.exception("Failed to update config section '%s'", section)
        return jsonify({
            "error": str(e),
            "hint": "Check server logs for details",
        }), 500


@config_bp.route("/reload", methods=["POST"])
def reload_config():
    """Reload configuration from disk."""
    state = current_app.config["METIXEL_STATE"]
    state.reload_config()
    return jsonify({"status": "ok"})


@config_bp.route("/restart", methods=["POST"])
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
                capture_output=True, text=True, timeout=15,
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


@config_bp.route("/reboot", methods=["POST"])
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
                capture_output=True, text=True, timeout=15,
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


@config_bp.route("/shutdown", methods=["POST"])
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
                capture_output=True, text=True, timeout=15,
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


@config_bp.route("/control", methods=["POST"])
def send_control():
    """Send a real-time control command to the frontend via IPC.

    Accepts JSON: {"cmd": "next|prev|pause|resume|switch_album|power_off|power_on"}
    """
    ipc = current_app.config.get("METIXEL_IPC")
    data = request.get_json(silent=True)
    if data is None or "cmd" not in data:
        return jsonify({"error": "Missing 'cmd' in JSON body"}), 400

    cmd = data["cmd"]
    valid_cmds = {"next", "prev", "pause", "resume", "switch_album", "power_off", "power_on", "show_message", "dismiss_message", "dismiss_all_messages"}
    if cmd not in valid_cmds:
        return jsonify({"error": f"Unknown command: {cmd}. Valid: {sorted(valid_cmds)}"}), 400

    if ipc is not None:
        from metixel.shared.ipc import ControlMessage
        ipc.send(ControlMessage(cmd=cmd, args=data.get("args", {})))
        logger.info("Control command '%s' sent via IPC", cmd)
    else:
        logger.warning("IPC not available — control command '%s' ignored", cmd)

    return jsonify({"status": "ok", "cmd": cmd})


@config_bp.route("/health", methods=["GET"])
def health_check():
    """System health endpoint."""
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    return jsonify(health)


@config_bp.route("/path", methods=["GET"])
def get_config_path():
    """Return the config file path (for debugging)."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify({
        "config_path": str(state.config_path),
        "exists": state.config_path.exists(),
    })


@config_bp.route("/info", methods=["GET"])
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
            capture_output=True, text=True, timeout=5,
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


@config_bp.route("/display/info", methods=["GET"])
def get_display_info():
    """Return the current display resolution detected by the frontend."""
    info = _read_display_info()
    if info is None:
        state = current_app.config["METIXEL_STATE"]
        dc = state.config.display
        info = {
            "width": dc.get("width", 0),
            "height": dc.get("height", 0),
            "backend": "unknown",
            "stale": True,
        }
    return jsonify(info)


@config_bp.route("/processing", methods=["GET"])
def get_processing_status():
    """Return the current background processing status.

    Reads from the same file the frontend uses for its splash screen
    progress bar (``/run/metixel/processing_status.json``).
    """
    import os

    path = "/run/metixel/processing_status.json"
    try:
        if os.path.isfile(path):
            import json as _json
            with open(path) as f:
                data = _json.load(f)
            return jsonify(data)
    except (OSError, ValueError):
        pass
    return jsonify({"phase": "unknown", "total": 0, "processed": 0, "current_file": ""})

@config_bp.route("/browse", methods=["GET"])
def browse_folder():
    """Browse the filesystem for folder selection in the web UI.

    Query params:
        path (str): The directory to browse.  Defaults to the metixel
            install root (``/opt/metixel/``).  Relative paths are
            resolved against ``/opt/metixel/``.

    Returns:
        JSON with ``current_path``, ``parent_path``, and ``entries`` —
        a list of subdirectory names (no files, no hidden dirs).
    """
    import os as _os

    requested = request.args.get("path", "/opt/metixel/")
    requested_path = Path(requested)
    base = Path("/opt/metixel")

    if not requested_path.is_absolute():
        requested_path = base / requested_path

    # Resolve and security-check: don't allow escaping the base path
    try:
        resolved = requested_path.resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "Invalid path"}), 400

    # Allow browsing anywhere readable — the user is configuring their
    # own system via the dashboard.  Just ensure the path exists.
    if not resolved.exists():
        return jsonify({"error": f"Path not found: {resolved}"}), 404
    if not resolved.is_dir():
        return jsonify({"error": f"Not a directory: {resolved}"}), 400

    # List subdirectories (no files, no hidden dirs)
    entries = []  # type: list
    try:
        for entry in sorted(resolved.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            entries.append({
                "name": entry.name + "/",
                "path": str(entry),
            })
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": str(resolved)}), 403
    except OSError as e:
        return jsonify({"error": str(e), "path": str(resolved)}), 500

    parent = str(resolved.parent) if resolved != resolved.anchor else None
    return jsonify({
        "current_path": str(resolved),
        "parent_path": parent,
        "entries": entries,
    })


def _read_current_media() -> dict | None:
    """Read the current media state file written by the frontend.

    Resolves any thumbnail path into a URL the dashboard can fetch.
    """
    import os

    path = "/run/metixel/current_media.json"
    try:
        if os.path.isfile(path):
            import json

            with open(path) as f:
                data = json.load(f)

            # Convert thumbnail_path → thumbnail_url
            thumb_path = data.get("thumbnail_path")
            if thumb_path:
                # The path could be a thumbnail hash (cache/thumbnails/<hash>.jpg)
                # or a video frame cache (<video>.<N>.frame).
                fname = os.path.basename(thumb_path)
                data["thumbnail_url"] = f"/api/media/thumbnail/{fname}"
            else:
                data["thumbnail_url"] = None

            return data
    except (OSError, ValueError):
        pass
    return None


def _read_display_info() -> dict | None:
    """Read the display info status file written by the frontend."""
    try:
        info_path = "/run/metixel/display_info.json"
        if os.path.isfile(info_path):
            with open(info_path) as f:
                return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None
