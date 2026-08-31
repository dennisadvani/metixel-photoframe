# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""OTA Update Manager — git-based self-update via GitHub releases.

Checks the configured GitHub repository for new versions on the
selected channel (stable / beta / dev), compares against the installed
version, and applies updates in-place via ``git fetch`` + ``git reset --hard``
followed by a service restart.

The Web UI's "Updates" card under Advanced consumes the REST API
exposed by :class:`UpdateManager`.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from metixel import __version__
from metixel.backend.state import StateManager
from metixel.shared.adapters import RequestsHttpGateway
from metixel.shared.paths import data_dir, install_root, live_dir, release_dir, releases_dir
from metixel.shared.ports import HttpGateway
from metixel.shared.subprocess import run_sudo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"
RELEASES_ENDPOINT = "/repos/{repo}/releases"
COMMITS_ENDPOINT = "/repos/{repo}/commits"

# How long to cache GitHub API responses before re-fetching
API_CACHE_TTL_SECONDS = 300  # 5 minutes

# Semver regex: matches v1.2.3, v1.2.3-beta.4, v1.2.3-rc1
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.](beta|rc|alpha|pre)\.?(\d+))?$")

# Time between update check cycles (when auto_check is enabled)
MIN_CHECK_INTERVAL = 600  # 10 minutes minimum

# The earliest atomic (Blue/Green) release.  Releases older than this used the
# monolithic layout and cannot be installed/rolled-back via the release dirs.
MIN_ATOMIC_VERSION = (1, 2, 3)

# How often the background loop re-evaluates the schedule (seconds).
SCHEDULE_POLL_SECONDS = 60

# Directories to protect during git reset --hard (already in .gitignore)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_semver(version_str: str) -> tuple[int, ...] | None:
    """Parse a semver string into a comparable tuple.

    Returns ``None`` if the string is not a valid semver.
    """
    m = _SEMVER_RE.match(version_str.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_label = m.group(4)  # beta, rc, alpha, pre — or None
    pre_num = int(m.group(5)) if m.group(5) else 0

    if pre_label:
        # Pre-release: sort before the corresponding release
        # beta < rc < (none = release)
        pre_order = {"alpha": 0, "beta": 1, "pre": 2, "rc": 3}
        pre_rank = pre_order.get(pre_label, 99)
        return (major, minor, patch, 0, pre_rank, pre_num)
    return (major, minor, patch, 1, 0, 0)


def _is_newer(candidate: str, current: str) -> bool:
    """Return True if *candidate* is a newer version than *current*."""
    c = _parse_semver(candidate)
    cur = _parse_semver(current)
    if c is None:
        return False
    if cur is None:
        return True  # can't parse current, assume candidate is newer
    return c > cur


# ---------------------------------------------------------------------------
# UpdateManager
# ---------------------------------------------------------------------------


class UpdateManager:
    """Manages OTA self-updates via git and the GitHub Releases API.

    Runs a background thread that periodically checks for updates on
    the configured channel.  Exposes methods for the web API to query
    status, trigger checks, switch channels, and apply updates.

    Thread Safety
    -------------
    All mutable state is protected by ``self._lock``.  The background
    thread acquires the lock briefly to update cached results; API
    callers acquire it to read status or queue operations.
    """

    def __init__(self, state: StateManager, http: HttpGateway | None = None) -> None:
        self._state = state
        self._http = http if http is not None else RequestsHttpGateway()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

        # Bounded background-check worker — guards against unbounded thread
        # spawn when a user rapidly triggers checks (manual button, channel
        # switch).  At most one on-demand check thread runs at a time; extra
        # triggers are coalesced into a no-op.
        self._check_spawned = False

        # Cached GitHub API results
        self._cache: dict[str, Any] = {}
        self._cache_time: float = 0.0
        self._check_in_progress = False
        self._update_in_progress = False
        self._last_error: str | None = None

        # Resolve the repository root (where .git lives)
        self._repo_root = self._resolve_repo_root()

    # -- Public API ----------------------------------------------------------

    def run(self) -> None:
        """Background loop: periodically check for updates and honour the
        weekly auto-update schedule.

        Intended to be run in a daemon thread started by
        :class:`BackendDaemon`.

        Two responsibilities:
        1. Periodic *check* for available updates (when ``auto_check`` is on).
        2. Weekly *auto-install* at the configured time on the configured day
           (when ``auto_update`` is on).
        """
        self._running = True
        logger.info(
            "UpdateManager started (channel=%s, repo=%s, version=%s)",
            self.channel,
            self.repo,
            __version__,
        )

        while self._running:
            try:
                update_cfg = self._state.config.updates

                if update_cfg.get("auto_check", True):
                    self.check_for_updates()

                # Weekly auto-update: install the latest available version
                # during the configured window, once per week.
                if update_cfg.get("auto_update", True):
                    self._maybe_auto_update()

                # Sleep in small chunks so we can respond to shutdown quickly.
                # Poll the schedule every SCHEDULE_POLL_SECONDS so the weekly
                # auto-update window is entered promptly.  check_for_updates()
                # is itself cache-bounded (API_CACHE_TTL_SECONDS), so frequent
                # polling does not hammer the GitHub API.
                deadline = time.monotonic() + SCHEDULE_POLL_SECONDS
                while self._running and time.monotonic() < deadline:
                    time.sleep(5)
            except Exception:
                logger.warning("Update check cycle failed", exc_info=True)
                if self._running:
                    time.sleep(60)

        logger.info("UpdateManager stopped")

    def shutdown(self) -> None:
        """Signal the background thread to stop."""
        self._running = False

    # -- Properties ----------------------------------------------------------

    @property
    def channel(self) -> str:
        """Current update channel (stable, beta, dev)."""
        return str(self._state.config.updates.get("channel", "stable"))

    @property
    def repo(self) -> str:
        """GitHub repository in owner/repo format."""
        return str(self._state.config.updates.get("github_repo", "dennisadvani/metixel-photoframe"))

    @property
    def installed_version(self) -> str:
        """Currently installed Metixel version."""
        return __version__

    # -- Status --------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return the full update status for the web API.

        Returns a dict with:
        - installed_version
        - current_channel
        - available: dict of channel -> {version, tag, url, is_newer}
        - last_check: ISO timestamp or None
        - last_update: ISO timestamp or None
        - check_in_progress
        - update_in_progress
        - last_error
        - repo_root: path to the git repository (or None)
        """
        with self._lock:
            update_cfg = self._state.config.updates
            available = self._cache.get("available", {})
            return {
                "installed_version": self.installed_version,
                "current_channel": self.channel,
                "available": available,
                "last_check": update_cfg.get("last_check"),
                "last_update": update_cfg.get("last_update"),
                "check_in_progress": self._check_in_progress,
                "update_in_progress": self._update_in_progress,
                "last_error": self._last_error,
                "repo_root": str(self._repo_root) if self._repo_root else None,
                "auto_check": update_cfg.get("auto_check", True),
                "auto_update": update_cfg.get("auto_update", True),
                "auto_update_day": update_cfg.get("auto_update_day", 0),
                "auto_update_time": update_cfg.get("auto_update_time", "04:30"),
                "last_auto_update": update_cfg.get("last_auto_update"),
                "check_interval_hours": update_cfg.get("check_interval_hours", 6),
                "github_repo": self.repo,
                "releases": self._cache.get("releases", []),
                "local_releases": self._list_local_releases(),
                "current_release": self._current_release(),
            }

    # -- Check for Updates ---------------------------------------------------

    def check_for_updates(self, force: bool = False) -> None:
        """Query GitHub for available updates across all channels.

        Caches results for ``API_CACHE_TTL_SECONDS`` to avoid hitting
        rate limits on rapid re-checks.  Set *force* to ``True`` to
        bypass the cache (used by the manual "Check for Updates" button).
        """
        # Avoid concurrent checks
        with self._lock:
            if self._check_in_progress:
                logger.debug("Update check already in progress — skipping")
                return
            self._check_in_progress = True
            self._last_error = None

        try:
            # Check cache freshness (skip if force=True)
            if not force:
                now = time.monotonic()
                with self._lock:
                    cache_age = now - self._cache_time
                if cache_age < API_CACHE_TTL_SECONDS and self._cache.get("available"):
                    logger.debug("Using cached GitHub results (%.0fs old)", cache_age)
                    with self._lock:
                        self._check_in_progress = False
                    return

            logger.info(
                "Checking GitHub for updates (repo=%s, channel=%s)", self.repo, self.channel
            )
            available: dict[str, dict[str, Any]] = {}
            release_list: list[dict[str, Any]] = []

            # ── Stable channel: latest non-prerelease tag ──────────
            try:
                releases = self._fetch_releases(per_page=50)
                stable = self._find_latest_stable(releases)
                if stable:
                    available["stable"] = {
                        "version": stable["version"],
                        "tag": stable["tag"],
                        "url": stable["html_url"],
                        "published_at": stable["published_at"],
                        "is_newer": _is_newer(stable["version"], self.installed_version),
                    }
                # ── Beta channel: latest pre-release tag ───────────
                beta = self._find_latest_prerelease(releases)
                if beta:
                    available["beta"] = {
                        "version": beta["version"],
                        "tag": beta["tag"],
                        "url": beta["html_url"],
                        "published_at": beta["published_at"],
                        "is_newer": _is_newer(beta["version"], self.installed_version),
                    }
                # ── Full release list for the manual selector ──────
                # Only atomic-era releases (>= 1.2.3) are installable via the
                # Blue/Green release dirs, so filter out older monolithic ones.
                release_list = self._build_release_list(releases)
            except Exception:
                logger.warning("Failed to fetch GitHub releases", exc_info=True)

            # ── Dev channel: HEAD of dev branch ────────────────────
            try:
                dev_commit = self._fetch_latest_commit("dev")
                if dev_commit:
                    available["dev"] = {
                        "version": "commit " + dev_commit["sha"][:7],
                        "sha": dev_commit["sha"],
                        "message": dev_commit["message"],
                        "url": dev_commit["html_url"],
                        "date": dev_commit["date"],
                        "is_newer": True,  # dev is always considered newer
                    }
            except Exception:
                logger.warning("Failed to fetch dev branch commit", exc_info=True)

            # ── Update cache and config ────────────────────────────
            now_iso = datetime.now(UTC).isoformat()
            with self._lock:
                self._cache["available"] = available
                self._cache["releases"] = release_list
                self._cache_time = time.monotonic()
                self._check_in_progress = False

            # Persist last_check timestamp
            try:
                self._state.update_config("update", {"last_check": now_iso})
            except Exception:
                logger.debug("Could not persist last_check timestamp", exc_info=True)

            # Log findings
            for ch, info in available.items():
                if info.get("is_newer"):
                    logger.info("Update available on %s channel: %s", ch, info.get("version"))
            if not any(v.get("is_newer") for v in available.values()):
                logger.info("Metixel is up to date (installed=%s)", self.installed_version)

        except Exception:
            with self._lock:
                self._check_in_progress = False
                self._last_error = "Check failed: check logs for details"
            logger.exception("Update check failed")

    def check_for_updates_async(self, force: bool = False) -> None:
        """Trigger an update check on a single bounded worker thread.

        Unlike a raw ``threading.Thread`` per call, this coalesces rapid
        triggers (manual button mashing, repeated channel switches) so at
        most one background check thread is alive at any time.  A trigger
        while a check is already running is dropped — the in-flight check
        already covers it.  ``check_for_updates`` remains callable directly
        (the background loop uses it synchronously).
        """
        with self._lock:
            if self._check_spawned:
                logger.debug("Update check already running — coalescing trigger")
                return
            self._check_spawned = True

        def _run() -> None:
            try:
                self.check_for_updates(force=force)
            finally:
                with self._lock:
                    self._check_spawned = False

        threading.Thread(target=_run, name="update-check-on-demand", daemon=True).start()

    # -- Apply Update --------------------------------------------------------

    def apply_update(
        self,
        channel: str | None = None,
        version: str | None = None,
        keep_existing: bool = False,
    ) -> dict[str, Any]:
        """Apply an update via a detached shell script.

        The update cannot run inside the Python process because stopping
        the backend service kills this process.  Instead we write a
        self-contained shell script to ``/tmp/metixel-update.sh`` and
        launch it with ``nohup``.  The script survives the backend
        shutdown, performs git + pip + restart, and cleans itself up.

        If the target release already exists locally (e.g. it was installed
        then rolled back), it is deleted first so the fresh install proceeds —
        unless *keep_existing* is ``True`` (the caller has already confirmed
        with the user).

        Returns a ``{"status": "ok"}`` response immediately — the actual
        work happens after the HTTP response is sent and the backend stops.
        """
        if not self._repo_root:
            return {"status": "error", "message": "Git repository not found — cannot self-update"}

        target_channel = channel or self.channel

        with self._lock:
            if self._update_in_progress:
                return {"status": "error", "message": "An update is already in progress"}
            self._update_in_progress = True
            self._last_error = None

        try:
            target_ref = self._resolve_target_ref(target_channel, version)
            if not target_ref:
                return {
                    "status": "error",
                    "message": f"Could not resolve update target for channel '{target_channel}'",
                }

            # If the target release already exists locally (e.g. it was
            # previously installed then rolled back), the Blue/Green updater
            # would abort.  Unless the caller explicitly asked to keep it,
            # delete the stale local copy first so the fresh install proceeds.
            existing = self._release_dir_for_ref(target_ref)
            if existing is not None and not keep_existing:
                logger.info(
                    "Release %s already present locally — deleting before reinstall",
                    existing.name,
                )
                self._delete_local_release(existing)

            logger.info("Applying update: channel=%s target=%s", target_channel, target_ref)
            self._write_and_launch_update_script(target_ref, target_channel)

            # Record the update attempt
            now_iso = datetime.now(UTC).isoformat()
            try:
                self._state.update_config(
                    "update",
                    {
                        "last_update": now_iso,
                        "channel": target_channel,
                    },
                )
            except Exception:
                logger.debug("Could not persist last_update timestamp", exc_info=True)

            with self._lock:
                self._update_in_progress = False

            logger.info("Update script launched for %s — backend will restart now", target_ref)
            return {
                "status": "ok",
                "message": f"Updating to {target_ref}. Services will restart momentarily.",
            }

        except Exception as exc:
            with self._lock:
                self._update_in_progress = False
                self._last_error = str(exc)
            logger.exception("Update launch failed")
            return {"status": "error", "message": f"Update failed: {exc}"}

    # -- Internal: Update Script ---------------------------------------------

    @staticmethod
    def _build_update_script(target_ref: str, channel: str) -> str:
        """Build the OTA bootstrap shell script (pure, testable).

        The bootstrap is deliberately THIN: it stops services, then hands off
        to ``scripts/update.sh <ref>`` in the pipeline. ``update.sh`` performs
        the atomic Blue/Green staging, install, symlink swap, health-check and
        rollback. Because the install logic lives in the repo, a device
        upgrading from an older release runs the NEW version's ``update.sh``
        steps — so new/changed system and pip dependencies are always applied.

        Note: the actual ``update.sh`` invoked is the one under the **live**
        symlink at bootstrap time (the currently running release). The staging
        clone + fresh ``ota_install.sh`` happen inside ``update.sh``.

        Steps:
        1. Stops cage + backend
        2. Runs ``bash scripts/update.sh <target_ref>`` (atomic swap + rollback)
        3. Restarts services (via EXIT trap in the bootstrap as a safety net)
        4. Removes itself
        """
        log_path = str(data_dir() / "cache" / "metixel-update.log")
        # All externally-influenced values are shell-quoted so they can never
        # break out of the embedded bash string literals (defence-in-depth —
        # target_ref/channel come from GitHub API data / user config).
        script_q = shlex.quote(str(live_dir() / "scripts" / "update.sh"))
        ref_q = shlex.quote(target_ref)
        channel_q = shlex.quote(channel)
        log_q = shlex.quote(log_path)
        return f"""#!/bin/bash
# Metixel OTA bootstrap — launched as a transient systemd service
# so it survives the backend being stopped (runs in its own cgroup).
# Stops services, then delegates to scripts/update.sh (the atomic
# Blue/Green updater: staging → install → swap → health-check → rollback).
set -uo pipefail

UPDATE_SH={script_q}
REF={ref_q}
CHANNEL={channel_q}
LOG={log_q}

exec > >(tee -a "$LOG") 2>&1
echo "=== Metixel OTA Update ==="
echo "Target: $REF  Channel: $CHANNEL"
echo "Started: $(date)"

# ── Guaranteed restart (trap fires on EXIT, success or failure) ──
_restart() {{
    echo ""
    echo "--- Restarting services (final) ---"
    sudo -n systemctl restart metixel-backend metixel-cage 2>/dev/null || true
    echo "=== End: $(date) ==="
}}
trap _restart EXIT

# ── Stop services ──
echo "Stopping metixel services…"
sudo -n systemctl stop metixel-cage metixel-backend 2>/dev/null || true
sleep 2

# ── Delegate to the atomic Blue/Green updater ──
# Performs: staging → install (strict) → remove obsolete → config backup →
# symlink swap → restart + health-check → rollback on failure.
echo "Running atomic update: $REF"
bash "$UPDATE_SH" "$REF"

echo "Update finished: $(date)"
echo "New version: $(python3 -c 'import metixel; print(metixel.__version__)' \
    2>/dev/null || echo unknown)"

# ── Clean up script (trap handles restart next) ──
rm -f "$0"
"""

    @staticmethod
    def _write_and_launch_update_script(target_ref: str, channel: str) -> None:
        """Write and detach the OTA update script (see ``_build_update_script``)."""
        import stat

        script_path = str(data_dir() / "cache" / "metixel-update.sh")
        script = UpdateManager._build_update_script(target_ref, channel)

        # Write the script
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)

        logger.info("Update script written to %s — launching via systemd-run", script_path)
        # systemd-run creates a transient service in its own cgroup, so the
        # script survives even when the backend is stopped.
        subprocess.run(
            [
                "sudo",
                "-n",
                "systemd-run",
                "--unit=metixel-update",
                "--description=Metixel OTA Update",
                "--collect",  # drop unit after it exits (don't leave garbage)
                "/bin/bash",
                script_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )

    # -- Channel Management --------------------------------------------------

    def set_channel(self, channel: str) -> dict[str, Any]:
        """Switch the update channel.

        Valid channels: ``stable``, ``beta``, ``dev``, ``main``.
        """
        valid = {"stable", "beta", "dev", "main"}
        if channel not in valid:
            return {
                "status": "error",
                "message": f"Invalid channel: {channel}. Valid: {sorted(valid)}",
            }

        self._state.update_config("update", {"channel": channel})
        logger.info("Update channel switched to '%s'", channel)

        # Trigger an immediate check on the new channel (bounded — coalesces
        # rapid switches so we never accumulate unbounded check threads).
        self.check_for_updates_async()

        return {"status": "ok", "channel": channel}

    # -- Auto-update schedule ------------------------------------------------

    def set_auto_update(
        self,
        enabled: bool | None = None,
        day: int | None = None,
        time_str: str | None = None,
    ) -> dict[str, Any]:
        """Configure the weekly auto-update schedule.

        Args:
            enabled: Whether auto-update is on/off.
            day: Day of week (0=Monday … 6=Sunday).
            time_str: ``HH:MM`` (any time of day).

        Returns a ``{"status": "ok"}`` dict, or an error dict.
        """
        values: dict[str, Any] = {}
        if enabled is not None:
            values["auto_update"] = bool(enabled)
        if day is not None:
            if not isinstance(day, int) or not 0 <= day <= 6:
                return {
                    "status": "error",
                    "message": "day must be an integer 0 (Monday) … 6 (Sunday)",
                }
            values["auto_update_day"] = day
        if time_str is not None:
            parsed = self._parse_time(time_str)
            if parsed is None:
                return {
                    "status": "error",
                    "message": "time must be a valid HH:MM (e.g. 04:30)",
                }
            values["auto_update_time"] = time_str

        if not values:
            return {"status": "error", "message": "Nothing to update"}

        self._state.update_config("update", values)
        logger.info("Auto-update schedule updated: %s", values)
        return {"status": "ok", **values}

    # -- Release management --------------------------------------------------

    def list_releases(self) -> list[dict[str, Any]]:
        """Return the list of GitHub releases available for manual install.

        Only atomic-era releases (semver >= 1.2.3) are returned, since older
        monolithic releases cannot be installed via the Blue/Green release
        dirs.  Each entry includes the version, tag, prerelease flag, and
        whether it is already present locally.
        """
        with self._lock:
            cached = self._cache.get("releases")
        if cached:
            return list(cached)
        # Not cached yet — trigger a background check and return empty.
        self.check_for_updates_async()
        return []

    def rollback(self, version: str) -> dict[str, Any]:
        """Roll back the live symlink to a previously installed release.

        Simply flips ``/opt/metixel/live`` to point at ``releases/<version>``
        and restarts services.  The target must already exist locally (it was
        installed at some point).  No download or install is performed.

        Returns a ``{"status": "ok"}`` dict, or an error dict.
        """
        target = release_dir(version)
        if not target.is_dir():
            return {
                "status": "error",
                "message": f"Release '{version}' is not installed locally — cannot roll back",
            }

        current = self._current_release()
        if current == version:
            return {
                "status": "error",
                "message": f"Release '{version}' is already the active release",
            }

        with self._lock:
            if self._update_in_progress:
                return {"status": "error", "message": "An update is already in progress"}
            self._update_in_progress = True

        try:
            logger.info("Rolling back live symlink to release '%s'", version)
            self._flip_live_symlink(target)
            self._restart_services()

            now_iso = datetime.now(UTC).isoformat()
            try:
                self._state.update_config(
                    "update",
                    {
                        "last_update": now_iso,
                        "last_rollback": now_iso,
                        "channel": self.channel,
                    },
                )
            except Exception:
                logger.debug("Could not persist rollback timestamp", exc_info=True)

            logger.info("Rollback to '%s' complete", version)
            return {
                "status": "ok",
                "message": f"Rolled back to {version}. Services are restarting.",
            }
        except Exception as exc:
            logger.exception("Rollback to '%s' failed", version)
            return {"status": "error", "message": f"Rollback failed: {exc}"}
        finally:
            with self._lock:
                self._update_in_progress = False

    def apt_upgrade(self) -> dict[str, Any]:
        """Run a full OS ``apt update && apt upgrade`` and reboot afterwards.

        Runs in a detached background thread (the reboot kills this process).
        Returns immediately with ``{"status": "ok"}``.
        """
        with self._lock:
            if self._update_in_progress:
                return {"status": "error", "message": "An update is already in progress"}
            self._update_in_progress = True

        def _run() -> None:
            try:
                logger.info("Starting full OS apt upgrade…")
                # apt update first, then upgrade.  Use --no-install-recommends
                # to keep the footprint minimal.  Non-zero exits are logged.
                for cmd in (
                    ["apt-get", "update"],
                    ["apt-get", "upgrade", "-y", "--no-install-recommends"],
                ):
                    result = run_sudo(cmd, timeout=1800)
                    if result.returncode != 0:
                        tail = (result.stderr or result.stdout or "").strip()[-500:]
                        logger.error("apt %s failed (rc=%d): %s", cmd[1], result.returncode, tail)
                        return
                logger.info("apt upgrade complete — rebooting system")
                # Reboot after a short delay so the response can flush.
                time.sleep(2)
                run_sudo(["reboot", "now"], timeout=15)
            except Exception:
                logger.exception("apt upgrade failed")
            finally:
                with self._lock:
                    self._update_in_progress = False

        threading.Thread(target=_run, name="apt-upgrade", daemon=True).start()
        return {
            "status": "ok",
            "message": "Full OS upgrade started. The system will reboot when complete.",
        }

    # -- Internal: Auto-update schedule --------------------------------------

    def _maybe_auto_update(self) -> None:
        """Install the latest available update if we're inside the weekly window.

        The update runs at the configured ``auto_update_time`` on the
        configured day of week, in local time.  Once an auto-update has run
        this week (tracked via ``last_auto_update``), it is skipped until the
        next week.
        """
        update_cfg = self._state.config.updates
        if not update_cfg.get("auto_update", True):
            return

        day = int(update_cfg.get("auto_update_day", 0))
        time_str = str(update_cfg.get("auto_update_time", "04:30"))
        start = self._parse_time(time_str)
        if start is None:
            logger.warning("Invalid auto_update_time '%s' — skipping auto-update", time_str)
            return

        now = datetime.now().astimezone()
        if now.weekday() != day:
            return

        # Run at the exact configured time (within a small grace window so a
        # slightly-late poll still catches it).
        target = now.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        grace_end = target + timedelta(minutes=10)
        if not (target <= now < grace_end):
            return

        # Already auto-updated this week?
        last = update_cfg.get("last_auto_update")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=now.tzinfo)
                week_start = now - timedelta(days=now.weekday())
                week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
                if last_dt >= week_start:
                    logger.debug("Auto-update already ran this week — skipping")
                    return
            except ValueError:
                pass

        logger.info("Auto-update time reached — installing latest %s update", self.channel)

        # Only install if a newer version is actually available on the channel.
        with self._lock:
            available = self._cache.get("available", {})
        ch_info = available.get(self.channel, {})
        if not ch_info.get("is_newer"):
            logger.info(
                "Auto-update time reached but no newer %s version available — skipping",
                self.channel,
            )
            return

        result = self.apply_update(channel=self.channel)
        if result.get("status") == "ok":
            now_iso = datetime.now(UTC).isoformat()
            try:
                self._state.update_config("update", {"last_auto_update": now_iso})
            except Exception:
                logger.debug("Could not persist last_auto_update", exc_info=True)

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int] | None:
        """Parse an ``HH:MM`` string into ``(hour, minute)``.

        Returns ``None`` if the string is not a valid 24-hour time.
        """
        try:
            hour, minute = (int(x) for x in time_str.split(":"))
        except (ValueError, AttributeError):
            return None
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return (hour, minute)

    # -- Internal: Release management ----------------------------------------

    def _build_release_list(self, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the installable release list from GitHub API data.

        Filters to atomic-era releases (semver >= 1.2.3), sorts newest-first,
        and annotates each with whether it's already present locally.
        """
        out: list[dict[str, Any]] = []
        for rel in releases:
            tag = (rel.get("tag_name") or "").strip()
            version = tag.lstrip("v")
            parsed = _parse_semver(version)
            if parsed is None:
                continue
            if parsed[:3] < MIN_ATOMIC_VERSION:
                continue
            out.append(
                {
                    "version": version,
                    "tag": tag,
                    "prerelease": bool(rel.get("prerelease")),
                    "url": rel.get("html_url", ""),
                    "published_at": rel.get("published_at", ""),
                    "installed": self._is_release_installed(version),
                }
            )
        out.sort(key=lambda r: _parse_semver(r["version"]) or (0, 0, 0, 0, 0, 0), reverse=True)
        return out

    def _list_local_releases(self) -> list[dict[str, Any]]:
        """List locally installed release folders under ``releases/``.

        Returns a list of ``{"version", "current"}`` dicts, newest-first.
        """
        rd = releases_dir()
        if not rd.is_dir():
            return []
        current = self._current_release()
        out: list[dict[str, Any]] = []
        for child in sorted(rd.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            if child.name.startswith(".staging-"):
                continue
            out.append({"version": child.name, "current": child.name == current})
        return out

    def _current_release(self) -> str | None:
        """Return the version name of the currently active release, or None."""
        live = install_root() / "live"
        if live.is_symlink() and live.exists():
            return live.resolve().name
        return None

    def _is_release_installed(self, version: str) -> bool:
        """Return True if a release folder for *version* exists locally."""
        return release_dir(version).is_dir()

    def _release_dir_for_ref(self, target_ref: str) -> Path | None:
        """Map a git ref to an existing local release folder, if any.

        Handles ``refs/tags/v1.2.3`` → ``releases/v1.2.3`` and
        ``origin/main`` → ``releases/main``.
        """
        name = self._ref_to_release_name(target_ref)
        p = release_dir(name)
        return p if p.is_dir() else None

    @staticmethod
    def _ref_to_release_name(target_ref: str) -> str:
        """Map a git ref to a release folder name (pure, testable).

        ``refs/tags/v1.2.3`` → ``1.2.3``, ``origin/main`` → ``main``,
        ``v2.0.0`` → ``2.0.0``.
        """
        name = target_ref
        if name.startswith("refs/tags/"):
            name = name[len("refs/tags/") :]
        elif name.startswith("origin/"):
            name = name[len("origin/") :]
        return name.lstrip("v")

    def _delete_local_release(self, release: Path) -> None:
        """Delete a local release folder (used before reinstalling a version
        that already exists locally)."""
        if not release.is_dir():
            return
        logger.info("Deleting local release %s", release)
        result = run_sudo(["rm", "-rf", str(release)], timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not delete existing release {release.name}: "
                f"{(result.stderr or result.stdout or '').strip()[-300:]}"
            )

    def _flip_live_symlink(self, target: Path) -> None:
        """Atomically point the live symlink at *target* and fix ownership."""
        live = install_root() / "live"
        result = run_sudo(["ln", "-sfn", str(target), str(live)], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not flip live symlink: "
                f"{(result.stderr or result.stdout or '').strip()[-300:]}"
            )
        run_sudo(["chown", "-h", "pi:pi", str(live)], timeout=30)

    def _restart_services(self) -> None:
        """Restart the metixel services via sudo systemctl."""
        result = run_sudo(
            ["systemctl", "restart", "metixel-backend", "metixel-cage"],
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "Service restart returned rc=%d: %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[-300:],
            )

    # -- Internal: Git Operations --------------------------------------------

    def _resolve_repo_root(self) -> Path | None:
        """Find the git repository root (the directory containing ``.git``).

        Layout-agnostic: follows the ``live`` symlink to the active release
        first (Pi deployments), then walks up from this module's file — so it
        works whether the package lives at the repo root (flat layout) or
        under ``src/`` (src/ layout).
        """
        # Fast path: the live symlink resolves to the active release's git repo.
        if (live_dir() / ".git").is_dir():
            return live_dir()

        # Walk up from this module to the nearest directory containing .git.
        current = Path(__file__).resolve().parent
        while True:
            if (current / ".git").is_dir():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent

        # Last resort: ask git directly.
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
        except Exception:
            pass
        return None

    # -- Internal: GitHub API ------------------------------------------------

    def _fetch_releases(self, per_page: int = 20) -> list[dict[str, Any]]:
        """Fetch releases from the GitHub API.

        Returns a list of release dicts, each containing:
        tag_name, name, prerelease, html_url, published_at, assets.
        """
        url = f"{GITHUB_API_BASE}{RELEASES_ENDPOINT.format(repo=self.repo)}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        all_releases: list[dict[str, Any]] = []
        page = 1

        while len(all_releases) < per_page:
            resp = self._http.get(
                url,
                params={"per_page": min(per_page, 100), "page": page},
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 403 and "rate limit" in (resp.text or "").lower():
                logger.warning("GitHub API rate limit reached")
                break
            if not resp.ok:
                logger.warning("GitHub API returned %d: %s", resp.status_code, resp.text[:200])
                break

            page_releases: list[dict[str, Any]] = resp.json()
            if not page_releases:
                break

            all_releases.extend(page_releases)
            if len(page_releases) < 100:
                break
            page += 1

        return all_releases

    def _fetch_latest_commit(self, branch: str) -> dict[str, Any] | None:
        """Fetch the latest commit SHA and metadata for a branch."""
        url = f"{GITHUB_API_BASE}{COMMITS_ENDPOINT.format(repo=self.repo)}"
        headers = {"Accept": "application/vnd.github.v3+json"}

        resp = self._http.get(
            url,
            params={"sha": branch, "per_page": 1},
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            logger.warning(
                "GitHub commits API returned %d for branch '%s'", resp.status_code, branch
            )
            return None

        commits: list[dict[str, Any]] = resp.json()
        if not commits:
            return None

        commit = commits[0]
        return {
            "sha": commit["sha"],
            "message": (commit.get("commit", {}).get("message", "") or "").split("\n")[0][:120],
            "html_url": commit.get("html_url", ""),
            "date": commit.get("commit", {}).get("committer", {}).get("date", ""),
        }

    @staticmethod
    def _find_latest_stable(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Find the latest non-prerelease from a list of GitHub releases."""
        for rel in releases:
            if rel.get("prerelease") or rel.get("draft"):
                continue
            tag = (rel.get("tag_name") or "").strip()
            version = tag.lstrip("v")
            if _parse_semver(version) is None:
                continue
            return {
                "version": version,
                "tag": tag,
                "html_url": rel.get("html_url", ""),
                "published_at": rel.get("published_at", ""),
            }
        return None

    @staticmethod
    def _find_latest_prerelease(releases: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Find the latest pre-release from a list of GitHub releases."""
        best: dict[str, Any] | None = None
        best_ver: tuple[int, ...] | None = None

        for rel in releases:
            if not rel.get("prerelease") or rel.get("draft"):
                continue
            tag = (rel.get("tag_name") or "").strip()
            version = tag.lstrip("v")
            parsed = _parse_semver(version)
            if parsed is None:
                continue
            if best_ver is None or parsed > best_ver:
                best_ver = parsed
                best = {
                    "version": version,
                    "tag": tag,
                    "html_url": rel.get("html_url", ""),
                    "published_at": rel.get("published_at", ""),
                }
        return best

    # -- Internal: Target Resolution -----------------------------------------

    def _resolve_target_ref(self, channel: str, version: str | None) -> str | None:
        """Resolve a channel (+ optional version) to a git ref.

        Args:
            channel: stable, beta, or dev.
            version: If given, a specific tag or SHA.  If None, uses latest.

        Returns:
            A git ref suitable for ``git reset --hard``, or ``None``.
        """
        if version:
            # Explicit version: try as tag first
            tag = version if version.startswith("v") else f"v{version}"
            # Verify it exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/{tag}"],
                cwd=str(self._repo_root),
                capture_output=True,
            )
            if result.returncode == 0:
                return f"refs/tags/{tag}"
            # Try as branch
            result2 = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/remotes/origin/{version}"],
                cwd=str(self._repo_root),
                capture_output=True,
            )
            if result2.returncode == 0:
                return f"origin/{version}"
            # Maybe it's a raw SHA
            result3 = subprocess.run(
                ["git", "cat-file", "-t", version],
                cwd=str(self._repo_root),
                capture_output=True,
            )
            if result3.returncode == 0:
                return version
            logger.warning("Could not resolve version ref '%s'", version)
            return None

        # No explicit version — use latest from cache
        with self._lock:
            available = self._cache.get("available", {})

        if channel == "stable":
            info = available.get("stable", {})
            tag = info.get("tag", "")
            if tag:
                return f"refs/tags/{tag}"
            # Fallback: try origin/main
            return "origin/main"

        elif channel == "beta":
            info = available.get("beta", {})
            tag = info.get("tag", "")
            if tag:
                return f"refs/tags/{tag}"
            # Fallback: try origin/beta
            return "origin/beta"

        elif channel == "dev":
            return "origin/dev"

        elif channel == "main":
            return "origin/main"

        return None
