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

    # If the caller asked to apply a WiFi country code, run iw reg set
    # immediately so the radio uses correct channels without a reboot.
    country = request.args.get("apply_wifi_country", "").strip().upper()
    if section == "network" and country and len(country) == 2:
        try:
            subprocess.run(
                ["sudo", "iw", "reg", "set", country],
                capture_output=True, timeout=5,
            )
            logger.info("WiFi regulatory domain set to: %s", country)
        except Exception:
            logger.warning("Failed to set WiFi country to %s", country, exc_info=True)

    return jsonify(config.to_dict()[section])


@config_bp.route("/video/profiles", methods=["GET"])
def video_profiles():
    """Return available transcoding profiles with current selection."""
    from metixel.backend.processing.video import VideoProcessor, _detect_pi_model
    state = current_app.config["METIXEL_STATE"]
    video_cfg = state.config.video
    profiles = []
    for key, prof in VideoProcessor.PROFILES.items():
        profiles.append({
            "key": key,
            "label": prof["label"],
            "codec": prof["codec"],
            "max_width": prof["max_width"],
            "max_height": prof["max_height"],
            "max_fps": prof["max_fps"],
            "max_bitrate": prof["max_bitrate"],
            "h264_profile": prof["h264_profile"],
            "h264_level": prof["h264_level"],
            "color_depth": prof["color_depth"],
            "hdr_support": prof["hdr_support"],
        })
    # Add custom
    profiles.append({"key": "custom", "label": "Custom"})
    return jsonify({
        "profiles": profiles,
        "current": video_cfg.get("transcoding_profile", ""),
        "detected_model": _detect_pi_model(),
        "keep_audio": video_cfg.get("keep_audio", False),
        "custom_settings": {
            "transcode_max_width": video_cfg.get("transcode_max_width", 0),
            "transcode_max_height": video_cfg.get("transcode_max_height", 0),
            "transcode_quality": video_cfg.get("transcode_quality", 23),
            "transcode_use_software_encoder": video_cfg.get("transcode_use_software_encoder", True),
            "transcode_timeout_seconds": video_cfg.get("transcode_timeout_seconds", 7200),
            "cpu_throttle_enabled": video_cfg.get("cpu_throttle_enabled", True),
            "cpu_throttle_percent": video_cfg.get("cpu_throttle_percent", 200),
            "transcoding_enabled": video_cfg.get("transcoding_enabled", True),
        },
    })


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

        # Trigger a full pipeline reset when config changes affect
        # what media is playable — simpler and more robust than
        # trying to incrementally update items mid-pipeline.
        # "sync" is included so that enabling/disabling watch folders
        # and toggling local sync on/off clears the playlist and
        # re-scans with the correct set of active paths.
        daemon = current_app.config.get("METIXEL_DAEMON")
        if daemon is not None and section in ("video", "image", "display", "sync"):
            try:
                daemon.reset_pipeline()
            except Exception:
                logger.debug("Pipeline reset failed", exc_info=True)

        # When the welcome banner is dismissed (system.first_run → false),
        # also dismiss all on-screen welcome messages so they don't linger.
        ipc = current_app.config.get("METIXEL_IPC")
        if ipc is not None and section == "system" and data.get("first_run") is False:
            try:
                from metixel.shared.ipc import ControlMessage
                ipc.send(ControlMessage(cmd="dismiss_all_messages"))
                logger.info("Welcome banner dismissed — clearing on-screen messages")
            except Exception:
                pass

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


@config_bp.route("/time", methods=["GET"])
def get_server_time():
    """Return the current server time in ISO 8601 and local formats.

    Used by the web dashboard to display a live clock without relying
    on the browser's clock (which may differ from the frame's timezone).
    """
    import datetime

    now = datetime.datetime.now().astimezone()
    return jsonify({
        "iso": now.isoformat(),
        "unix": now.timestamp(),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "timezone": now.tzname() or "",
        "utc_offset": now.strftime("%z"),
    })


@config_bp.route("/timezone", methods=["POST"])
def set_timezone():
    """Set the system timezone via ``sudo timedatectl set-timezone``.

    Accepts JSON: ``{"timezone": "Australia/Sydney"}``

    Requires a NOPASSWD sudoers entry for timedatectl.
    """
    data = request.get_json(silent=True)
    if data is None or "timezone" not in data:
        return jsonify({"error": "Missing 'timezone' in JSON body"}), 400

    tz = data["timezone"].strip()
    if not tz:
        return jsonify({"error": "Timezone cannot be empty"}), 400

    try:
        result = subprocess.run(
            ["sudo", "-n", "timedatectl", "set-timezone", tz],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-300:]
            logger.error("timedatectl set-timezone failed (rc=%d): %s", result.returncode, tail)
            return jsonify({"status": "error", "message": tail[:200]}), 500

        logger.info("System timezone set to %s", tz)
        return jsonify({"status": "ok", "timezone": tz})
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Command timed out"}), 500
    except FileNotFoundError:
        return jsonify({"status": "error", "message": "timedatectl not found"}), 500
    except Exception as exc:
        logger.exception("Failed to set timezone: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@config_bp.route("/timezones", methods=["GET"])
def list_timezones():
    """Return a list of common timezone identifiers for the dropdown.

    Reads from ``/usr/share/zoneinfo/zone.tab`` if available, otherwise
    falls back to a curated shortlist.
    """
    shortlist = [
        "UTC",
        "US/Eastern", "US/Central", "US/Mountain", "US/Pacific",
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "America/Toronto", "America/Vancouver",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
        "Europe/Rome", "Europe/Amsterdam", "Europe/Stockholm", "Europe/Warsaw",
        "Europe/Athens", "Europe/Moscow",
        "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore", "Asia/Kolkata",
        "Asia/Dubai", "Asia/Jerusalem", "Asia/Seoul",
        "Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane",
        "Australia/Perth", "Australia/Adelaide",
        "Pacific/Auckland", "Pacific/Fiji",
        "Africa/Johannesburg", "Africa/Cairo", "Africa/Lagos",
        "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
        "America/Mexico_City",
    ]
    try:
        with open("/usr/share/zoneinfo/zone.tab") as f:
            zones = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 3:
                    zones.append(parts[2])
            if zones:
                return jsonify({"timezones": zones})
    except (OSError, FileNotFoundError):
        pass
    return jsonify({"timezones": shortlist})


@config_bp.route("/ntp", methods=["POST"])
def configure_ntp():
    """Enable/disable NTP and set NTP servers via systemd-timesyncd.

    Accepts JSON: ``{"enabled": true, "servers": ["0.pool.ntp.org", ...]}``

    When enabled, writes NTP server list to ``/etc/systemd/timesyncd.conf``
    and restarts ``systemd-timesyncd``.  When disabled, stops the service.

    Requires a NOPASSWD sudoers entry for systemctl and tee.
    """
    data = request.get_json(silent=True)
    if data is None or "enabled" not in data:
        return jsonify({"error": "Missing 'enabled' in JSON body"}), 400

    enabled = bool(data["enabled"])
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        servers = []

    try:
        if enabled:
            # Write timesyncd.conf with custom NTP servers
            ntp_lines = "\n".join(f"NTP={s}" for s in servers if s.strip())
            conf = f"[Time]\n{ntp_lines}\n" if ntp_lines else "[Time]\n"
            # Write to temp file, then sudo cp to /etc
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as tf:
                tf.write(conf)
                tmp_path = tf.name
            subprocess.run(
                ["sudo", "-n", "cp", tmp_path, "/etc/systemd/timesyncd.conf"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            os.unlink(tmp_path)
            subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "systemd-timesyncd"],
                capture_output=True, text=True, timeout=15,
            )
            logger.info("NTP enabled with %d server(s)", len([s for s in servers if s.strip()]))
            return jsonify({"status": "ok", "ntp": "enabled", "servers": servers})
        else:
            subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "systemd-timesyncd"],
                capture_output=True, text=True, timeout=15,
            )
            subprocess.run(
                ["sudo", "-n", "systemctl", "disable", "systemd-timesyncd"],
                capture_output=True, text=True, timeout=15,
            )
            logger.info("NTP disabled")
            return jsonify({"status": "ok", "ntp": "disabled"})
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip()[-300:]
        logger.error("NTP config failed: %s", tail)
        return jsonify({"status": "error", "message": tail[:200]}), 500
    except FileNotFoundError:
        return jsonify({"status": "error", "message": "systemctl not found"}), 500
    except Exception as exc:
        logger.exception("NTP config failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@config_bp.route("/quiet-boot", methods=["POST"])
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
            return jsonify({
                "status": "error",
                "message": "Script failed. Check server logs.",
                "detail": tail[:300],
            }), 500
        logger.info(
            "Quiet boot %s via quiet_boot.sh",
            "enabled" if enabled else "disabled",
        )
        return jsonify({
            "status": "ok",
            "quiet_boot": enabled,
            "message": (
                "Quiet boot enabled."
                if enabled
                else "Quiet boot disabled."
            ),
        })
    except subprocess.TimeoutExpired:
        logger.error("quiet_boot.sh timed out after 30s")
        return jsonify({"status": "error", "message": "Script timed out."}), 500
    except FileNotFoundError:
        logger.error("quiet_boot.sh not found at %s", script)
        return jsonify({"status": "error", "message": "quiet_boot.sh not found."}), 500
    except Exception as exc:
        logger.exception("quiet_boot.sh failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


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

    # Update daemon's display state so the Web UI button reflects reality
    if cmd in ("power_on", "power_off"):
        daemon = current_app.config.get("METIXEL_DAEMON")
        if daemon is not None:
            daemon._display_on = (cmd == "power_on")

    return jsonify({"status": "ok", "cmd": cmd})


@config_bp.route("/health", methods=["GET"])
def health_check():
    """System health endpoint."""
    state = current_app.config["METIXEL_STATE"]
    health = state.get_system_health()
    # Read current media info from the frontend's state file
    health["current_media"] = _read_current_media()
    health["config_path"] = str(state.config_path)
    # Include display power state so the Web UI button reflects
    # the actual state (e.g. when the schedule turns it off).
    daemon = current_app.config.get("METIXEL_DAEMON")
    health["display_on"] = getattr(daemon, "_display_on", True) if daemon else True
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


# ── Background processing status (per-phase progress bars) ─────────────────

@config_bp.route("/processing-status", methods=["GET"])
def processing_status():
    """Return per-phase processing progress for the dashboard.

    Each phase (``scanning``, ``optimising_images``, ``transcoding``)
    tracks its own ``total``/``processed`` independently.  The web UI
    renders a separate progress bar for each phase so the user can see
    all queue states at once, without flickering between them.
    """
    try:
        path = Path("/run/metixel/processing_status.json")
        if not path.exists():
            return jsonify({"active": None, "phases": {}})
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"active": None, "phases": {}})
