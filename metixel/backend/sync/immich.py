# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Immich API synchronization client (Immich v3 API).

Polls an Immich server for album assets, downloads new media, and optionally
mirrors the album by removing local files no longer present in the remote album.

Key Immich v3 API changes:
- ``GET /api/albums/{id}`` no longer embeds ``assets`` — use
  ``POST /api/search/metadata`` with ``albumIds`` instead.
- Asset download: ``GET /api/assets/{id}/original``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from metixel.backend.state import StateManager

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

_REQUEST_TIMEOUT = (15.0, 120.0)  # (connect, read) seconds
_MAX_RETRIES = 3
_RETRY_DELAY = 5.0  # seconds between retries

# Immich v3 API paths (relative to server_url, e.g. http://host:2283/api)
_API_ALBUMS = "/api/albums"
_API_SEARCH_METADATA = "/api/search/metadata"
_API_ASSET_DOWNLOAD = "/api/assets/{asset_id}/original"

# The status file written after each sync (for the web dashboard).
_SYNC_STATUS_FILE = "/run/metixel/immich_sync_status.json"
# Live progress file updated during a sync cycle.
_SYNC_PROGRESS_FILE = "/run/metixel/immich_sync_progress.json"
# Cancel flag — touch this file to request cancellation.
_SYNC_CANCEL_FILE = "/run/metixel/immich_sync_cancel"


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    """Outcome of a single sync cycle."""

    started_at: float = 0.0
    finished_at: float = 0.0
    album_name: str = ""
    album_id: str = ""
    total_remote: int = 0
    downloaded: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)
    success: bool = False

    @property
    def duration_seconds(self) -> float:
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 1),
            "album_name": self.album_name,
            "album_id": self.album_id,
            "total_remote": self.total_remote,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "errors": self.errors,
            "success": self.success,
        }


# ── ImmichSyncer ────────────────────────────────────────────────────────────


class ImmichSyncer:
    """Polls the Immich REST API (v3) for album and asset changes.

    Sync modes
    ----------
    * **Strict sync** (``strict_sync=True``): The local sync directory becomes
      an exact mirror of the remote Immich album. Assets not in the album are
      **deleted** from the local directory.
    * **Pull-only** (``strict_sync=False``): Only downloads new assets.
      Existing local files that are not in the album are left untouched.
    """

    def __init__(self, state: StateManager) -> None:
        self._state = state
        self._reload_config()
        self._running: bool = False
        self._last_result: SyncResult | None = None
        self._sync_lock = threading.Lock()
        self._syncing: bool = False
        self._cancel_requested: bool = False

    # -- Public API -----------------------------------------------------------

    def run(self) -> None:
        """Main sync loop — polls Immich at the configured interval."""
        self._running = True
        logger.info(
            "Immich syncer started (server: %s, interval: %ds, strict: %s)",
            self._base_url, self._poll_interval, self._strict_sync,
        )

        while self._running:
            try:
                self._reload_config()
                if self._enabled and self._album_name:
                    if self._syncing:
                        logger.debug("Previous sync still in progress — skipping this cycle")
                    else:
                        self._sync()
                else:
                    logger.debug("Immich sync disabled or no album configured — skipping cycle")
            except requests.exceptions.ConnectionError:
                logger.warning("Immich server unreachable — will retry")
            except requests.exceptions.Timeout:
                logger.warning("Immich request timed out — will retry")
            except Exception:
                logger.exception("Immich sync error")

            # Sleep in 5-second chunks so config changes (interval, album,
            # enabled) are picked up promptly rather than waiting for the
            # full poll_interval to expire.
            remaining = self._poll_interval
            while self._running and remaining > 0:
                chunk = min(5, remaining)
                time.sleep(chunk)
                remaining -= chunk

    def stop(self) -> None:
        """Signal the sync loop to stop."""
        self._running = False

    def cancel(self) -> None:
        """Request cancellation of the currently running sync cycle.

        The running sync will finish the current file, then abort gracefully.
        Safe to call from any thread.
        """
        self._cancel_requested = True
        # Also touch the cancel file for external processes
        try:
            Path(_SYNC_CANCEL_FILE).write_text("1")
        except OSError:
            pass
        logger.info("Sync cancellation requested")

    def sync_once(self) -> SyncResult:
        """Perform a single sync cycle synchronously (for manual triggers).

        Returns a ``SyncResult`` describing what happened.
        Skips if a sync is already in progress.
        """
        self._reload_config()
        if self._syncing:
            logger.warning("Sync already in progress — skipping manual trigger")
            result = SyncResult(started_at=time.time(), finished_at=time.time())
            result.errors.append("Sync already in progress")
            return result
        return self._sync()

    def get_last_result(self) -> SyncResult | None:
        """Return the result of the most recent sync, if any."""
        # Also try reading the persisted status file (survives restarts)
        try:
            if os.path.isfile(_SYNC_STATUS_FILE):
                with open(_SYNC_STATUS_FILE) as f:
                    data = json.load(f)
                if self._last_result is None or data.get("finished_at", 0) > self._last_result.finished_at:
                    self._last_result = SyncResult(**{
                        k: v for k, v in data.items() if k in SyncResult.__dataclass_fields__  # type: ignore[arg-type]
                    })  # type: ignore[arg-type]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return self._last_result

    # -- Config helpers -------------------------------------------------------

    def _reload_config(self) -> None:
        """Refresh config from the state manager (handles hot-reload)."""
        config = self._state.config
        immich_cfg = config.sync["immich"]
        self._enabled: bool = immich_cfg.get("enabled", False)
        self._base_url: str = immich_cfg["server_url"].rstrip("/")
        self._api_key: str = immich_cfg["api_key"]
        self._album_name: str = immich_cfg.get("album_name", "")
        self._strict_sync: bool = immich_cfg.get("strict_sync", False)
        self._poll_interval: int = immich_cfg.get("poll_interval_seconds", 3600)

        sync_dir = immich_cfg.get("sync_dir", "media/sync/immich/")
        self._sync_dir: Path = Path(sync_dir)
        if not self._sync_dir.is_absolute():
            self._sync_dir = Path("/opt/metixel") / self._sync_dir

    # -- Sync logic -----------------------------------------------------------

    def _sync(self) -> SyncResult:
        """Perform one full sync cycle."""
        # ── Network connectivity gate ─────────────────────────────────
        # Don't waste time attempting Immich API calls when there's no
        # upstream network — the result would always be a timeout/error.
        try:
            from metixel.backend.network_manager import is_connected
        except ImportError:
            # Non-Linux / dev environment — assume connected.
            pass
        else:
            if not is_connected():
                logger.info("No network connectivity — skipping Immich sync cycle")
                result = SyncResult(started_at=time.time(), finished_at=time.time())
                result.errors.append("No network connectivity")
                result.success = False
                self._persist_result(result)
                return result

        # ── Prevent overlapping syncs ─────────────────────────────────
        with self._sync_lock:
            if self._syncing:
                logger.warning("Sync already in progress — aborting duplicate")
                result = SyncResult(started_at=time.time(), finished_at=time.time())
                result.errors.append("Sync already in progress")
                return result
            self._syncing = True
            self._cancel_requested = False
            # Remove stale cancel file
            try:
                Path(_SYNC_CANCEL_FILE).unlink(missing_ok=True)
            except OSError:
                pass

        try:
            return self._do_sync()
        finally:
            self._syncing = False
            self._clear_progress()

    def _do_sync(self) -> SyncResult:
        """Internal sync implementation — caller must hold the logical lock."""
        result = SyncResult(started_at=time.time())
        logger.info("=== Immich sync cycle starting ===")
        self._write_progress("starting", 0, 0, "")

        # 1. Validate configuration
        if not self._api_key:
            result.errors.append("No API key configured")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, "No API key configured")
            self._persist_result(result)
            return result

        if not self._album_name:
            result.errors.append("No album name configured")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, "No album name configured")
            self._persist_result(result)
            return result

        # 2. Resolve album name → ID
        self._write_progress("resolving_album", 0, 0, self._album_name)
        try:
            album_id = self._resolve_album_id(self._album_name)
        except Exception as e:
            logger.error("Failed to resolve album '%s': %s", self._album_name, e)
            result.errors.append(f"Album resolution failed: {e}")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, str(e))
            self._persist_result(result)
            return result

        if not album_id:
            result.errors.append(f"Album '{self._album_name}' not found on server")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, f"Album '{self._album_name}' not found")
            self._persist_result(result)
            return result

        result.album_name = self._album_name
        result.album_id = album_id
        logger.info("Resolved album '%s' → id=%s", self._album_name, album_id)

        if self._cancel_requested:
            result.errors.append("Cancelled by user")
            result.finished_at = time.time()
            self._write_progress("cancelled", 0, 0, "")
            self._persist_result(result)
            return result

        # 3. Fetch all remote assets
        self._write_progress("fetching_assets", 0, 0, "")
        try:
            remote_assets = self._fetch_album_assets(album_id)
        except Exception as e:
            logger.error("Failed to fetch assets for album %s: %s", album_id, e)
            result.errors.append(f"Asset fetch failed: {e}")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, str(e))
            self._persist_result(result)
            return result

        result.total_remote = len(remote_assets)
        logger.info("Remote album has %d assets", result.total_remote)

        if self._cancel_requested:
            result.errors.append("Cancelled by user")
            result.finished_at = time.time()
            self._write_progress("cancelled", 0, 0, "")
            self._persist_result(result)
            return result

        # 4. Build expected local filenames
        remote_map: dict[str, dict[str, Any]] = {}
        for asset in remote_assets:
            asset_id = asset["id"]
            ext = self._extract_extension(asset)
            filename = f"immich_{asset_id}{ext}"
            remote_map[filename] = asset

        # 5. Scan local sync directory
        self._sync_dir.mkdir(parents=True, exist_ok=True)
        local_files: set[str] = set()
        try:
            for entry in self._sync_dir.iterdir():
                if entry.is_file():
                    local_files.add(entry.name)
        except OSError as e:
            logger.error("Cannot read sync directory %s: %s", self._sync_dir, e)
            result.errors.append(f"Local directory read error: {e}")
            result.finished_at = time.time()
            self._write_progress("error", 0, 0, str(e))
            self._persist_result(result)
            return result

        # 6. Determine downloads needed
        to_download: set[str] = set(remote_map.keys()) - local_files
        download_total = len(to_download)
        logger.info(
            "Local: %d files | Remote: %d assets | To download: %d",
            len(local_files), result.total_remote, download_total,
        )

        # 7. Download new assets
        downloaded = 0
        for filename in sorted(to_download):
            if self._cancel_requested:
                result.errors.append("Cancelled by user")
                logger.info("Sync cancelled — %d/%d downloaded", downloaded, download_total)
                break

            asset = remote_map[filename]
            self._write_progress("downloading", download_total, downloaded, filename)
            try:
                self._download_asset(asset, filename)
                result.downloaded += 1
                downloaded += 1
            except Exception as e:
                logger.error("Failed to download %s: %s", filename, e)
                result.errors.append(f"Download failed for {filename}: {e}")
                self._write_progress("downloading", download_total, downloaded, f"{filename} — FAILED")

        result.skipped = result.total_remote - result.downloaded - len(
            [e for e in result.errors if e.startswith("Download failed")]
        )

        if self._cancel_requested and "Cancelled" not in " ".join(result.errors):
            result.errors.append("Cancelled by user")

        # 8. Strict sync: delete local files not in remote
        if self._strict_sync and not self._cancel_requested:
            to_delete = local_files - set(remote_map.keys())
            if to_delete:
                logger.info("Strict sync: %d local files to delete", len(to_delete))
                self._write_progress("cleaning", len(to_delete), 0, "")
            deleted = 0
            for filename in sorted(to_delete):
                if self._cancel_requested:
                    break
                file_path = self._sync_dir / filename
                try:
                    file_path.unlink()
                    logger.info("Deleted (not in album): %s", filename)
                    result.deleted += 1
                    deleted += 1
                    self._write_progress("cleaning", len(to_delete), deleted, filename)
                except OSError as e:
                    logger.error("Failed to delete %s: %s", filename, e)
                    result.errors.append(f"Delete failed for {filename}: {e}")
        elif not self._strict_sync:
            logger.debug(
                "Pull-only mode — %d local files not in album are preserved",
                len(local_files - set(remote_map.keys())),
            )

        result.success = len(result.errors) == 0
        result.finished_at = time.time()

        logger.info(
            "=== Immich sync complete: %d downloaded, %d skipped, %d deleted, %d errors (%.1fs) ===",
            result.downloaded, result.skipped, result.deleted,
            len(result.errors), result.duration_seconds,
        )

        self._last_result = result
        self._persist_result(result)
        phase = "cancelled" if "Cancelled" in " ".join(result.errors) else "complete"
        self._write_progress(phase, 0, 0, "")
        return result

    # -- Immich API helpers ---------------------------------------------------

    def _get_headers(self, content_type: str | None = None) -> dict[str, str]:
        """Build request headers with API key authentication."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "x-api-key": self._api_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _resolve_album_id(self, album_name: str) -> str | None:
        """Query ``GET /api/albums`` and find the album by name.

        Returns the album ID string, or ``None`` if not found.
        """
        url = f"{self._base_url}{_API_ALBUMS}"
        logger.debug("Listing albums: GET %s", url)

        resp = requests.get(
            url, headers=self._get_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        albums: list[dict[str, Any]] = resp.json()

        # Search case-insensitively
        name_lower = album_name.strip().lower()
        for album in albums:
            if album.get("albumName", "").strip().lower() == name_lower:
                return album["id"]

        logger.warning(
            "Album '%s' not found among %d albums on server", album_name, len(albums),
        )
        return None

    def _list_albums(self) -> list[dict[str, Any]]:
        """Return all albums from the Immich server (for the UI picker)."""
        url = f"{self._base_url}{_API_ALBUMS}"
        resp = requests.get(
            url, headers=self._get_headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def _fetch_album_assets(self, album_id: str) -> list[dict[str, Any]]:
        """Fetch all assets in an album via ``POST /api/search/metadata``.

        Immich v3: ``AlbumResponseDto`` no longer embeds ``assets``.
        Use the search/metadata endpoint with ``albumIds`` filter instead.
        Handles pagination via ``nextPage``.
        """
        url = f"{self._base_url}{_API_SEARCH_METADATA}"
        headers = self._get_headers(content_type="application/json")
        all_assets: list[dict[str, Any]] = []
        page: int | None = None

        while True:
            payload: dict[str, Any] = {"albumIds": [album_id]}
            if page is not None:
                payload["page"] = page

            resp = requests.post(
                url, headers=headers, json=payload,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            items = data.get("assets", {}).get("items", [])
            all_assets.extend(items)

            next_page = data.get("assets", {}).get("nextPage")
            if next_page is None:
                break
            page = int(next_page)

        logger.debug("Fetched %d assets for album %s (%d pages)", len(all_assets), album_id, page or 1)
        return all_assets

    def _download_asset(self, asset: dict[str, Any], filename: str) -> None:
        """Download a single asset to the sync directory.

        Saves as ``immich_{assetId}.{ext}`` in the sync directory.
        Uses a temporary file + atomic rename to avoid partial writes.
        """
        asset_id = asset["id"]
        url = f"{self._base_url}{_API_ASSET_DOWNLOAD.format(asset_id=asset_id)}"
        headers = {
            "Accept": "application/octet-stream",
            "x-api-key": self._api_key,
        }
        tmp_path = self._sync_dir / f".{filename}.tmp"
        final_path = self._sync_dir / filename

        # Check available disk space (rough)
        self._check_disk_space(final_path)

        logger.debug("Downloading asset %s → %s", asset_id, filename)

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with requests.get(
                    url, headers=headers, stream=True,
                    timeout=_REQUEST_TIMEOUT,
                ) as r:
                    r.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())

                # Atomic rename
                os.replace(tmp_path, final_path)
                logger.info("Downloaded: %s (%d bytes)", filename, final_path.stat().st_size)
                return

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status == 401 or status == 403:
                    raise RuntimeError(f"Invalid or unauthorized API key (HTTP {status})") from e
                if status == 404:
                    raise RuntimeError(f"Asset {asset_id} not found on server (HTTP 404)") from e
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "HTTP %s downloading %s, retry %d/%d in %ds",
                        status, filename, attempt, _MAX_RETRIES, _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
                else:
                    raise

            except requests.exceptions.Timeout as e:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Timeout downloading %s, retry %d/%d in %ds",
                        filename, attempt, _MAX_RETRIES, _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY)
                else:
                    raise RuntimeError(f"Timeout downloading {filename} after {_MAX_RETRIES} attempts") from e

            except requests.exceptions.ConnectionError as e:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Connection error downloading %s, retry %d/%d in %ds",
                        filename, attempt, _MAX_RETRIES, _RETRY_DELAY,
                    )
                    time.sleep(_RETRY_DELAY * 2)
                else:
                    raise RuntimeError(f"Connection failed for {filename} after {_MAX_RETRIES} attempts") from e

        # Clean up temp file on failure
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _extract_extension(asset: dict[str, Any]) -> str:
        """Determine the file extension for an asset.

        Uses ``originalPath`` first, falls back to ``originalFileName``.
        Converts HEIC/HEIF → .jpg for photo-frame compatibility.
        """
        original_path = asset.get("originalPath", "")
        original_name = asset.get("originalFileName", "")

        ext = ""
        if original_path:
            ext = os.path.splitext(original_path)[1]
        if not ext:
            ext = os.path.splitext(original_name)[1]

        ext = ext.lower()

        # Photo frames can't display HEIC/HEIF — we'd transcode later,
        # but for naming consistency treat them as .jpg
        if ext in (".heic", ".heif"):
            return ".jpg"

        # If no extension, infer from type
        if not ext:
            if asset.get("type") == "VIDEO":
                return ".mp4"
            return ".jpg"

        return ext

    def _check_disk_space(self, target_path: Path) -> None:
        """Raise ``OSError`` if the target volume has less than 50 MB free."""
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            stat = os.statvfs(target_path.parent)
            free_bytes = stat.f_frsize * stat.f_bavail
            min_free = 50 * 1024 * 1024  # 50 MB
            if free_bytes < min_free:
                raise OSError(
                    f"Insufficient disk space: {free_bytes / (1024**2):.1f} MB free "
                    f"(need at least {min_free / (1024**2):.0f} MB)"
                )
        except OSError as e:
            if "Insufficient" in str(e):
                raise
            # If statvfs fails, proceed anyway (e.g. network share)

    def _persist_result(self, result: SyncResult) -> None:
        """Write the sync result to the status file for the web dashboard."""
        try:
            Path(_SYNC_STATUS_FILE).parent.mkdir(parents=True, exist_ok=True)
            tmp = _SYNC_STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(result.to_dict(), f, indent=2)
            os.replace(tmp, _SYNC_STATUS_FILE)
        except OSError:
            logger.debug("Could not write sync status file — /run/metixel unavailable?")

    def _write_progress(
        self, phase: str, total: int, processed: int, current_file: str,
    ) -> None:
        """Write a live progress snapshot for the web dashboard to poll."""
        try:
            Path(_SYNC_PROGRESS_FILE).parent.mkdir(parents=True, exist_ok=True)
            tmp = _SYNC_PROGRESS_FILE + ".tmp"
            data = {
                "phase": phase,
                "total": total,
                "processed": processed,
                "current_file": current_file,
                "syncing": self._syncing,
                "timestamp": time.time(),
            }
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, _SYNC_PROGRESS_FILE)
        except OSError:
            pass

    def _clear_progress(self) -> None:
        """Remove the live progress file."""
        try:
            Path(_SYNC_PROGRESS_FILE).unlink(missing_ok=True)
        except OSError:
            pass
