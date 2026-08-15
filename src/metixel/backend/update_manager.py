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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from metixel import __version__
from metixel.backend.state import StateManager
from metixel.shared.adapters import RequestsHttpGateway
from metixel.shared.ports import HttpGateway

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
        """Background loop: periodically check for updates.

        Intended to be run in a daemon thread started by
        :class:`BackendDaemon`.
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
                interval_hours = max(0.17, float(update_cfg.get("check_interval_hours", 6)))
                interval_seconds = interval_hours * 3600

                if update_cfg.get("auto_check", True):
                    self.check_for_updates()

                # Sleep in small chunks so we can respond to shutdown quickly
                deadline = time.monotonic() + max(interval_seconds, MIN_CHECK_INTERVAL)
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
                "github_repo": self.repo,
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

            # ── Stable channel: latest non-prerelease tag ──────────
            try:
                releases = self._fetch_releases(per_page=20)
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
        self, channel: str | None = None, version: str | None = None
    ) -> dict[str, Any]:
        """Apply an update via a detached shell script.

        The update cannot run inside the Python process because stopping
        the backend service kills this process.  Instead we write a
        self-contained shell script to ``/tmp/metixel-update.sh`` and
        launch it with ``nohup``.  The script survives the backend
        shutdown, performs git + pip + restart, and cleans itself up.

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

            logger.info("Applying update: channel=%s target=%s", target_channel, target_ref)
            self._write_and_launch_update_script(str(self._repo_root), target_ref, target_channel)

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
    def _build_update_script(repo_root: str, target_ref: str, channel: str) -> str:
        """Build the OTA update shell script (pure, testable).

        The script:
        1. Waits for the backend to fully stop
        2. Stops cage explicitly
        3. ``git fetch`` + ``git reset --hard``
        4. ``pip install --break-system-packages -e .``
        5. Installs runtime deps from ``requirements-pip.txt``
        6. Restarts both services
        7. Removes itself
        """
        log_path = "/opt/metixel/cache/metixel-update.log"
        # All externally-influenced values are shell-quoted so they can never
        # break out of the embedded bash string literals (defence-in-depth —
        # target_ref/channel come from GitHub API data / user config).
        repo_q = shlex.quote(repo_root)
        ref_q = shlex.quote(target_ref)
        channel_q = shlex.quote(channel)
        log_q = shlex.quote(log_path)
        return f"""#!/bin/bash
# Metixel OTA update — launched as a transient systemd service
# so it survives the backend being stopped (runs in its own cgroup).
# Uses a trap to guarantee services are restarted even if git/pip fail.
set -uo pipefail

REPO={repo_q}
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

# ── Git operations ──
echo "Fetching from origin…"
cd "$REPO"
# systemd-run executes as root without HOME set, so --global fails.
# Use --system to write to /etc/gitconfig instead.
git config --system --add safe.directory "$REPO" 2>/dev/null || true
git fetch --tags --force origin || echo "WARNING: git fetch failed (continuing)"

echo "Checking out $REF…"
git reset --hard "$REF" || echo "WARNING: git checkout failed (continuing)"

# ── Install missing system packages ──
# New releases may require additional apt packages (e.g. python3-evdev).
# This is idempotent — already-installed packages are skipped.
if [ -f "$REPO/requirements-system.txt" ]; then
    echo "Checking system packages…"
    while IFS= read -r pkg; do
        [ -z "$pkg" ] && continue
        [[ "$pkg" =~ ^# ]] && continue
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "  Installing: $pkg"
            sudo -n apt-get install -y -qq "$pkg" 2>/dev/null \
                || echo "  WARNING: failed to install $pkg"
        fi
    done < "$REPO/requirements-system.txt"
fi

# ── Reinstall Python package ──
echo "Reinstalling Python package…"
pip install --break-system-packages -e "$REPO" \
    || echo "WARNING: pip install failed (continuing)"

# ── Install / update runtime pip dependencies ──
# `pip install -e .` above only installs the package itself — the runtime
# deps live in the phase1/phase2 optional extras, not main [project]
# dependencies — so it never applies new/changed deps (e.g. pillow-heif).
# Install the canonical requirements-pip.txt so upgrades also update deps.
if [ -f "$REPO/requirements-pip.txt" ]; then
    echo "Installing Python dependencies…"
    pip install --break-system-packages -r "$REPO/requirements-pip.txt" \
        || echo "WARNING: pip dependency install failed (continuing)"
fi

echo "Update finished: $(date)"
echo "New version: $(python3 -c 'import metixel; print(metixel.__version__)' \
    2>/dev/null || echo unknown)"

# ── Clean up script (trap handles restart next) ──
rm -f "$0"
"""

    @staticmethod
    def _write_and_launch_update_script(repo_root: str, target_ref: str, channel: str) -> None:
        """Write and detach the OTA update script (see ``_build_update_script``)."""
        import stat

        script_path = "/opt/metixel/cache/metixel-update.sh"
        script = UpdateManager._build_update_script(repo_root, target_ref, channel)

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

        Valid channels: ``stable``, ``beta``, ``dev``.
        """
        valid = {"stable", "beta", "dev"}
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

    # -- Internal: Git Operations --------------------------------------------

    def _resolve_repo_root(self) -> Path | None:
        """Find the git repository root (the directory containing ``.git``).

        Layout-agnostic: checks the canonical install location first, then
        walks up from this module's file — so it works whether the package
        lives at the repo root (flat layout) or under ``src/`` (src/ layout).
        """
        # Fast path: the canonical install location (Pi deployments).
        if (Path("/opt/metixel") / ".git").is_dir():
            return Path("/opt/metixel")

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

        return None
