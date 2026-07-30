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
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from metixel import __version__
from metixel.backend.state import StateManager

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
_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.](beta|rc|alpha|pre)\.?(\d+))?$"
)

# Time between update check cycles (when auto_check is enabled)
MIN_CHECK_INTERVAL = 600  # 10 minutes minimum

# Directories to protect during git reset --hard (already in .gitignore,
# but we verify they exist and are not tracked to be safe).
_PROTECTED_PATHS = [
    "etc/config.json",
    "media/",
    "cache/",
    "logs/",
]


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

    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

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
        logger.info("UpdateManager started (channel=%s, repo=%s, version=%s)",
                     self.channel, self.repo, __version__)

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
        return self._state.config.updates.get("channel", "stable")

    @property
    def repo(self) -> str:
        """GitHub repository in owner/repo format."""
        return self._state.config.updates.get(
            "github_repo", "metixel-photoframe/metixel-photoframe"
        )

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

    def check_for_updates(self) -> None:
        """Query GitHub for available updates across all channels.

        Caches results for ``API_CACHE_TTL_SECONDS`` to avoid hitting
        rate limits on rapid re-checks.
        """
        # Avoid concurrent checks
        with self._lock:
            if self._check_in_progress:
                logger.debug("Update check already in progress — skipping")
                return
            self._check_in_progress = True
            self._last_error = None

        try:
            # Check cache freshness
            now = time.monotonic()
            with self._lock:
                cache_age = now - self._cache_time
            if cache_age < API_CACHE_TTL_SECONDS and self._cache.get("available"):
                logger.debug("Using cached GitHub results (%.0fs old)", cache_age)
                with self._lock:
                    self._check_in_progress = False
                return

            logger.info("Checking GitHub for updates (repo=%s, channel=%s)",
                         self.repo, self.channel)
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
            now_iso = datetime.now(timezone.utc).isoformat()
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
                self._last_error = f"Check failed: check logs for details"
            logger.exception("Update check failed")

    # -- Apply Update --------------------------------------------------------

    def apply_update(self, channel: str | None = None, version: str | None = None) -> dict[str, Any]:
        """Apply an update by checking out the target git ref and restarting services.

        Args:
            channel: Which channel to update from.  Defaults to current channel.
            version: Specific version tag or SHA to install.
                     If omitted, installs the latest on the channel.

        Returns:
            A dict with ``status`` ("ok" or "error") and a ``message``.
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
            # Resolve the target git ref
            target_ref = self._resolve_target_ref(target_channel, version)
            if not target_ref:
                return {
                    "status": "error",
                    "message": f"Could not resolve update target for channel '{target_channel}'",
                }

            logger.info("Applying update: channel=%s target=%s", target_channel, target_ref)

            # ── Step 1: Stop the frontend (cage) first ─────────────
            self._stop_frontend()

            # ── Step 2: git fetch + checkout ───────────────────────
            self._git_fetch()
            self._git_checkout(target_ref)

            # ── Step 3: Reinstall Python package (deps may have changed) ──
            self._pip_install()

            # ── Step 4: Restart services ───────────────────────────
            self._restart_services()

            # Record the update
            now_iso = datetime.now(timezone.utc).isoformat()
            self._state.update_config("update", {
                "last_update": now_iso,
                "channel": target_channel,
            })

            with self._lock:
                self._update_in_progress = False

            logger.info("Update applied successfully: %s", target_ref)
            return {
                "status": "ok",
                "message": f"Updated to {target_ref}. Services are restarting.",
            }

        except Exception as exc:
            with self._lock:
                self._update_in_progress = False
                self._last_error = str(exc)
            logger.exception("Update failed")
            # Attempt to restart services even on failure
            try:
                self._restart_services()
            except Exception:
                pass
            return {"status": "error", "message": f"Update failed: {exc}"}

    # -- Channel Management --------------------------------------------------

    def set_channel(self, channel: str) -> dict[str, Any]:
        """Switch the update channel.

        Valid channels: ``stable``, ``beta``, ``dev``.
        """
        valid = {"stable", "beta", "dev"}
        if channel not in valid:
            return {"status": "error", "message": f"Invalid channel: {channel}. Valid: {sorted(valid)}"}

        self._state.update_config("update", {"channel": channel})
        logger.info("Update channel switched to '%s'", channel)

        # Trigger an immediate check on the new channel
        threading.Thread(
            target=self.check_for_updates,
            name="update-check-after-channel-switch",
            daemon=True,
        ).start()

        return {"status": "ok", "channel": channel}

    # -- Internal: Git Operations --------------------------------------------

    def _resolve_repo_root(self) -> Path | None:
        """Find the git repository root (parent of .git)."""
        # Check the install location first
        candidates = [
            Path("/opt/metixel"),
            Path(__file__).resolve().parent.parent.parent,  # repo root from this file
        ]
        for candidate in candidates:
            if (candidate / ".git").is_dir():
                return candidate
        # Try git rev-parse as a last resort
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return Path(result.stdout.strip())
        except Exception:
            pass
        return None

    def _git_fetch(self) -> None:
        """Fetch all tags and branches from origin."""
        if not self._repo_root:
            raise RuntimeError("No git repository found")
        logger.info("Fetching from origin…")
        result = subprocess.run(
            ["git", "fetch", "--tags", "--force", "origin"],
            cwd=str(self._repo_root),
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git fetch failed: {result.stderr.strip()[-500:]}")

    def _git_checkout(self, ref: str) -> None:
        """Reset the working tree to the target ref (in-place, no merge)."""
        if not self._repo_root:
            raise RuntimeError("No git repository found")

        # Verify protected paths are not going to be clobbered
        for rel_path in _PROTECTED_PATHS:
            full_path = self._repo_root / rel_path
            if not full_path.exists():
                continue
            # Check if git tracks this file
            tracked_result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", rel_path],
                cwd=str(self._repo_root),
                capture_output=True,
            )
            if tracked_result.returncode == 0:
                logger.warning(
                    "Protected path '%s' is tracked by git — it will be overwritten!",
                    rel_path,
                )

        logger.info("Checking out %s…", ref)
        result = subprocess.run(
            ["git", "reset", "--hard", ref],
            cwd=str(self._repo_root),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git reset --hard {ref} failed: {result.stderr.strip()[-500:]}")
        logger.info("Working tree reset to %s", ref)

    def _pip_install(self) -> None:
        """Reinstall the metixel package with pip (editable install).

        Uses ``pip install -e .`` which is fast when dependencies
        haven't changed (wheels are cached).
        """
        if not self._repo_root:
            raise RuntimeError("No repository root found")
        logger.info("Reinstalling Python package…")
        result = subprocess.run(
            ["pip", "install", "-e", str(self._repo_root)],
            cwd=str(self._repo_root),
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            # pip install failure is not fatal — the code may still run
            logger.warning("pip install had issues (may be OK): %s",
                           result.stderr.strip()[-500:])
            # Don't raise — the git checkout already updated the code

    # -- Internal: Service Control -------------------------------------------

    @staticmethod
    def _stop_frontend() -> None:
        """Stop the metixel-cage service (frontend renderer)."""
        logger.info("Stopping frontend (metixel-cage)…")
        try:
            subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "metixel-cage"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            logger.warning("Could not stop metixel-cage (may already be stopped): %s", exc)

    @staticmethod
    def _restart_services() -> None:
        """Restart both metixel-backend and metixel-cage services.

        Restarting the backend also restarts the Flask server, so the
        web UI will briefly show a "Reconnecting…" overlay.
        """
        logger.info("Restarting services…")
        try:
            subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "metixel-backend", "metixel-cage"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception as exc:
            logger.warning("Service restart may have failed: %s", exc)
            raise

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
            resp = requests.get(
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

        resp = requests.get(
            url,
            params={"sha": branch, "per_page": 1},
            headers=headers,
            timeout=15,
        )
        if not resp.ok:
            logger.warning("GitHub commits API returned %d for branch '%s'",
                           resp.status_code, branch)
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
