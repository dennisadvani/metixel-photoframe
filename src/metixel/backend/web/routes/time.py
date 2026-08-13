# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clock, timezone and NTP endpoints."""

from __future__ import annotations

import logging
import os
import subprocess

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

time_bp = Blueprint("time", __name__)


@time_bp.route("/time", methods=["GET"])
def get_server_time():
    """Return the current server time in ISO 8601 and local formats.

    Used by the web dashboard to display a live clock without relying
    on the browser's clock (which may differ from the frame's timezone).
    """
    import datetime

    now = datetime.datetime.now().astimezone()
    return jsonify(
        {
            "iso": now.isoformat(),
            "unix": now.timestamp(),
            "time": now.strftime("%H:%M:%S"),
            "date": now.strftime("%Y-%m-%d"),
            "timezone": now.tzname() or "",
            "utc_offset": now.strftime("%z"),
        }
    )


@time_bp.route("/timezone", methods=["POST"])
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
            capture_output=True,
            text=True,
            timeout=15,
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


@time_bp.route("/timezones", methods=["GET"])
def list_timezones():
    """Return a list of common timezone identifiers for the dropdown.

    Reads from ``/usr/share/zoneinfo/zone.tab`` if available, otherwise
    falls back to a curated shortlist.
    """
    shortlist = [
        "UTC",
        "US/Eastern",
        "US/Central",
        "US/Mountain",
        "US/Pacific",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Toronto",
        "America/Vancouver",
        "Europe/London",
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Madrid",
        "Europe/Rome",
        "Europe/Amsterdam",
        "Europe/Stockholm",
        "Europe/Warsaw",
        "Europe/Athens",
        "Europe/Moscow",
        "Asia/Tokyo",
        "Asia/Shanghai",
        "Asia/Singapore",
        "Asia/Kolkata",
        "Asia/Dubai",
        "Asia/Jerusalem",
        "Asia/Seoul",
        "Australia/Sydney",
        "Australia/Melbourne",
        "Australia/Brisbane",
        "Australia/Perth",
        "Australia/Adelaide",
        "Pacific/Auckland",
        "Pacific/Fiji",
        "Africa/Johannesburg",
        "Africa/Cairo",
        "Africa/Lagos",
        "America/Sao_Paulo",
        "America/Argentina/Buenos_Aires",
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


@time_bp.route("/ntp", methods=["POST"])
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
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            os.unlink(tmp_path)
            subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "systemd-timesyncd"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            logger.info("NTP enabled with %d server(s)", len([s for s in servers if s.strip()]))
            return jsonify({"status": "ok", "ntp": "enabled", "servers": servers})
        else:
            subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "systemd-timesyncd"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            subprocess.run(
                ["sudo", "-n", "systemctl", "disable", "systemd-timesyncd"],
                capture_output=True,
                text=True,
                timeout=15,
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
