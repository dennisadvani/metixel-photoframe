# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Clock, timezone and NTP endpoints."""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import subprocess
import time
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify

from metixel.backend.web.helpers import get_body, jsonify_error

logger = logging.getLogger(__name__)

time_bp = Blueprint("time", __name__)

# Default NTP servers used when the user leaves the server list blank.
# Mirrors the placeholders shown in the web dashboard.
DEFAULT_NTP_SERVERS = [
    "0.debian.pool.ntp.org",
    "1.debian.pool.ntp.org",
    "2.debian.pool.ntp.org",
]

_WHITESPACE_RE = re.compile(r"\s")

#: How long ``POST /api/time/sync`` waits for timesyncd to report a sync.
_SYNC_WAIT_SECONDS = 8.0
_SYNC_POLL_SECONDS = 0.5


def system_timezone_name() -> str:
    """Return the IANA name of the *system* timezone (e.g. ``Europe/London``).

    Read from the ``/etc/localtime`` symlink (what ``timedatectl
    set-timezone`` rewrites), falling back to ``/etc/timezone``.  Returns
    ``""`` when neither is available (non-Linux dev machines).

    This deliberately bypasses the process's own notion of local time:
    glibc caches the zone on first use, so a long-running backend keeps
    reporting the zone it *started* in (typically UTC) after the user picks
    a new one in the dashboard.
    """
    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        idx = target.rfind(marker)
        if idx >= 0:
            name = target[idx + len(marker) :].strip("/")
            if name:
                return name
    except OSError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    return ""


def _now_local() -> tuple[datetime.datetime, str]:
    """Current time in the system timezone, plus that zone's IANA name.

    Falls back to the process-local zone when the system zone is unknown
    or not present in the zoneinfo database.
    """
    name = system_timezone_name()
    if name:
        try:
            return datetime.datetime.now(ZoneInfo(name)), name
        except Exception:
            logger.debug("Unknown system timezone %r — using process zone", name)
    return datetime.datetime.now().astimezone(), name


def _time_payload() -> dict:
    now, name = _now_local()
    return {
        "iso": now.isoformat(),
        "unix": now.timestamp(),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "timezone": now.tzname() or "",
        "timezone_name": name,
        "utc_offset": now.strftime("%z"),
    }


def _apply_process_timezone(tz: str) -> None:
    """Make the running backend adopt ``tz`` without a restart.

    ``timedatectl`` only rewrites ``/etc/localtime``; glibc never re-reads
    it, so scheduled display on/off times, log timestamps and anything else
    using local time would stay on the old zone until the service restarts.
    """
    os.environ["TZ"] = tz
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()


@time_bp.route("", methods=["GET"])
def get_server_time():
    """Return the current server time in ISO 8601 and local formats.

    Used by the web dashboard to display a live clock without relying
    on the browser's clock (which may differ from the frame's timezone).
    ``timezone_name`` is the IANA zone the *system* is set to (see
    :func:`system_timezone_name`); ``timezone`` is its abbreviation.
    """
    return jsonify(_time_payload())


@time_bp.route("/timezone", methods=["POST"])
def set_timezone():
    """Set the system timezone via ``sudo timedatectl set-timezone``.

    Accepts JSON: ``{"timezone": "Australia/Sydney"}``

    Requires a NOPASSWD sudoers entry for timedatectl.
    """
    data = get_body()
    tz = data.get("timezone", "")
    if not isinstance(tz, str):
        return jsonify_error("'timezone' must be a string", 400)
    tz = tz.strip()
    if not tz:
        return jsonify_error("Missing or empty 'timezone' in JSON body", 400)

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

        _apply_process_timezone(tz)
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
                return jsonify({"timezones": sorted(set(zones))})
    except (OSError, FileNotFoundError):
        pass
    return jsonify({"timezones": sorted(shortlist)})


@time_bp.route("/sync", methods=["POST"])
def sync_time_now():
    """Force an immediate NTP synchronisation via systemd-timesyncd.

    Restarts ``systemd-timesyncd`` (which polls its servers straight away
    and steps the clock) and then waits up to ~8 s for ``timedatectl`` to
    report ``NTPSynchronized=yes``.  The response carries the fresh server
    time so the dashboard clock can update at once.

    Returns ``{"status": "ok", "synchronized": bool, ...time fields}``.
    ``synchronized`` is ``False`` when timesyncd did not confirm within the
    wait — usually no network, or the NTP servers are unreachable.

    Requires a NOPASSWD sudoers entry for systemctl.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "systemd-timesyncd"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-300:]
            logger.error("timesyncd restart failed (rc=%d): %s", result.returncode, tail)
            return jsonify_error(tail[:200] or "Could not restart systemd-timesyncd", 500)

        synchronized = False
        deadline = time.monotonic() + _SYNC_WAIT_SECONDS
        while True:
            probe = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0 and (probe.stdout or "").strip().lower() == "yes":
                synchronized = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_SYNC_POLL_SECONDS)

        if synchronized:
            logger.info("NTP sync completed on request")
        else:
            logger.warning(
                "NTP sync requested but timesyncd did not confirm within %.0fs", _SYNC_WAIT_SECONDS
            )
        payload = _time_payload()
        payload.update({"status": "ok", "synchronized": synchronized})
        return jsonify(payload)
    except subprocess.TimeoutExpired:
        return jsonify_error("Command timed out", 500)
    except FileNotFoundError:
        return jsonify_error("systemctl/timedatectl not found", 500)
    except Exception as exc:
        logger.exception("NTP sync failed: %s", exc)
        return jsonify_error(str(exc), 500)


@time_bp.route("/ntp", methods=["POST"])
def configure_ntp():
    """Enable/disable NTP and set NTP servers via systemd-timesyncd.

    Accepts JSON: ``{"enabled": true, "servers": ["0.pool.ntp.org", ...]}``

    When enabled, writes NTP server list to ``/etc/systemd/timesyncd.conf``
    and restarts ``systemd-timesyncd``.  When disabled, stops the service.

    If ``servers`` is empty or all blank, the Debian pool defaults are used
    (``0/1/2.debian.pool.ntp.org``) so NTP still works out of the box.

    Requires a NOPASSWD sudoers entry for systemctl and tee.
    """
    data = get_body()
    if "enabled" not in data:
        return jsonify_error("Missing 'enabled' in JSON body", 400)

    enabled = bool(data["enabled"])
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        servers = []
    # Each entry is written verbatim as an ``NTP=`` line in timesyncd.conf,
    # so it must be a plain hostname-like token: a string with no whitespace
    # (a newline would inject an extra config line).  Blank entries are
    # simply dropped (the defaults kick in below).
    cleaned: list[str] = []
    for entry in servers:
        if not isinstance(entry, str):
            return jsonify_error("'servers' entries must be strings", 400)
        stripped = entry.strip()
        if not stripped:
            continue
        if _WHITESPACE_RE.search(stripped):
            return jsonify_error("'servers' entries must not contain whitespace", 400)
        cleaned.append(stripped)
    servers = cleaned

    try:
        if enabled:
            # Fall back to the Debian pool defaults when no servers given.
            if not servers:
                servers = list(DEFAULT_NTP_SERVERS)
            # Write timesyncd.conf with custom NTP servers
            ntp_lines = "\n".join(f"NTP={s}" for s in servers)
            conf = f"[Time]\n{ntp_lines}\n"
            # Write to temp file, then sudo cp to /etc.  The temp file is
            # removed whether or not the copy succeeds.
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as tf:
                tf.write(conf)
                tmp_path = tf.name
            try:
                subprocess.run(
                    ["sudo", "-n", "cp", tmp_path, "/etc/systemd/timesyncd.conf"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "systemd-timesyncd"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            logger.info("NTP enabled with %d server(s)", len(servers))
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
