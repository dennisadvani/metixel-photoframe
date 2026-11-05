# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Google Photos synchronization client (public shared album).

The frame shows **one Google Photos album, all of it**: the user shares the
album by link, and every cycle re-reads that album through
:mod:`metixel.backend.sync.google_photos_album` and downloads what is new.
With strict sync on, photos removed from the album are removed from the frame
too, so the sync folder is an exact mirror of the album.

No Google account access is involved — no OAuth client, no tokens — only the
public share link.  (Google's Library API can no longer read user albums since
31 March 2025, and the Picker API only hands over a one-off snapshot of what a
person ticks, which cannot follow an album as it changes.)  The share page is
undocumented, so the reader is defensive and reports a clear error if Google
changes it.

Everything else mirrors :mod:`metixel.backend.sync.immich` — a background
thread on a poll interval, a sync directory under ``data/media/sync/``, live
progress and status files for the dashboard, cancellation, and an optional
mirror ("strict") mode.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from metixel.backend.state import StateManager
from metixel.backend.sync import google_photos_album as album
from metixel.shared.adapters import RequestsHttpGateway
from metixel.shared.paths import resolve_install_path, run_path
from metixel.shared.ports import HttpGateway

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

_REQUEST_TIMEOUT = (15.0, 120.0)  # (connect, read) seconds
_MAX_RETRIES = 3
_RETRY_DELAY = 5.0  # seconds between retries
#: Floor for the scheduled re-read, whatever the config says.
_MIN_POLL_INTERVAL = 60

#: Downloaded files are named ``gphoto_<digest>[<ext>]`` so the same album item
#: always maps to the same filename — an item already on disk is never fetched
#: twice.
_FILENAME_PREFIX = "gphoto_"
_DIGEST_CHARS = 16

#: ``Content-Type`` → extension for the types Google actually serves.  Anything
#: else falls back to :mod:`mimetypes`, then to the item's media type.
_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/tiff": ".tif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/mpeg": ".mpg",
    "video/x-msvideo": ".avi",
}

# Runtime files under ``run_dir()`` — resolved through :func:`run_path` at call
# time so the writer (syncer) and the readers (web layer) always agree.
_SYNC_STATUS_FILE = "google_photos_sync_status.json"
_SYNC_PROGRESS_FILE = "google_photos_sync_progress.json"
_SYNC_CANCEL_FILE = "google_photos_sync_cancel"


def _sync_status_path() -> Path:
    return run_path(_SYNC_STATUS_FILE)


def _sync_progress_path() -> Path:
    return run_path(_SYNC_PROGRESS_FILE)


def _sync_cancel_path() -> Path:
    return run_path(_SYNC_CANCEL_FILE)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    """Outcome of a single sync cycle (one read of the shared album)."""

    started_at: float = 0.0
    finished_at: float = 0.0
    album_title: str = ""
    album_pages: int = 0
    #: False when the album listing stopped before Google's last page; strict
    #: sync must then not delete anything (not part of the persisted result).
    album_complete: bool = True
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
            "album_title": self.album_title,
            "album_pages": self.album_pages,
            "total_remote": self.total_remote,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "errors": self.errors,
            "success": self.success,
        }


# ── GooglePhotosSyncer ──────────────────────────────────────────────────────


class GooglePhotosSyncer:
    """Mirrors a public Google Photos shared album into the sync directory.

    Sync modes
    ----------
    * **Pull-only** (``strict_sync=False``): album items missing from the sync
      directory are downloaded; everything already there is left alone.
    * **Strict sync** (``strict_sync=True``): the sync directory becomes an
      exact mirror of the album — files that are no longer in it are
      **deleted**, but only after a listing that reached the album's end.
    """

    def __init__(self, state: StateManager, http: HttpGateway | None = None) -> None:
        self._state = state
        self._http = http if http is not None else RequestsHttpGateway()
        self._reload_config()
        self._running: bool = False
        self._last_result: SyncResult | None = None
        self._sync_lock = threading.Lock()
        self._syncing: bool = False
        self._cancel_requested: bool = False

    # -- Public API -----------------------------------------------------------

    def run(self) -> None:
        """Main loop — re-reads the album on the configured interval."""
        self._running = True
        logger.info(
            "Google Photos syncer started (interval: %ds, strict: %s, configured: %s)",
            self._poll_interval,
            self._strict_sync,
            self.is_configured(),
        )

        while self._running:
            try:
                self._reload_config()
                if not self._enabled or not self.is_configured():
                    logger.debug("Google Photos sync disabled or unconfigured — skipping cycle")
                elif self._syncing:
                    logger.debug("Previous sync still in progress — skipping this cycle")
                else:
                    self._sync()
            except requests.exceptions.ConnectionError:
                logger.warning("Google Photos unreachable — will retry")
            except requests.exceptions.Timeout:
                logger.warning("Google Photos request timed out — will retry")
            except Exception:
                logger.exception("Google Photos sync error")

            # Sleep in 5-second chunks so config changes are picked up promptly.
            remaining = self._poll_interval
            while self._running and remaining > 0:
                chunk = min(5, remaining)
                time.sleep(chunk)
                remaining -= chunk

    def stop(self) -> None:
        """Signal the sync loop to stop."""
        self._running = False

    def cancel(self) -> None:
        """Request cancellation of the running sync cycle (safe from any thread)."""
        with self._sync_lock:
            self._cancel_requested = True
        with contextlib.suppress(OSError):
            _sync_cancel_path().write_text("1")
        logger.info("Google Photos sync cancellation requested")

    def _is_cancel_requested(self) -> bool:
        """Thread-safe read of the cancellation flag."""
        with self._sync_lock:
            return self._cancel_requested

    def sync_once(self) -> SyncResult:
        """Sync the album synchronously (manual trigger)."""
        self._reload_config()
        if self._syncing:
            logger.warning("Google Photos sync already in progress — skipping manual trigger")
            result = SyncResult(started_at=time.time(), finished_at=time.time())
            result.errors.append("Sync already in progress")
            return result
        return self._sync()

    def get_last_result(self) -> SyncResult | None:
        """Return the result of the most recent sync, if any."""
        try:
            status_path = _sync_status_path()
            if status_path.is_file():
                with open(status_path) as f:
                    data = json.load(f)
                if (
                    self._last_result is None
                    or data.get("finished_at", 0) > self._last_result.finished_at
                ):
                    self._last_result = SyncResult(
                        **{
                            k: v
                            for k, v in data.items()
                            if k in SyncResult.__dataclass_fields__  # type: ignore[arg-type]
                        }
                    )
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return self._last_result

    def is_configured(self) -> bool:
        """True when a readable shared-album link is set."""
        return album.is_album_url(self._shared_album_url)

    # -- Config helpers -------------------------------------------------------

    def _reload_config(self) -> None:
        """Refresh config from the state manager (handles hot-reload).

        A no-op while a sync is running: the web routes and the daemon share
        this instance and the dashboard polls every 1.5 s, so reloading
        mid-cycle would switch the album or sync folder under downloads (and
        strict deletes) that are already in flight.  The next cycle picks the
        change up.
        """
        if getattr(self, "_syncing", False):
            return
        config = self._state.config
        cfg = config.sync.get("google_photos") or {}
        self._enabled: bool = cfg.get("enabled", False)
        self._shared_album_url: str = str(cfg.get("shared_album_url") or "").strip()
        self._strict_sync: bool = cfg.get("strict_sync", False)
        try:
            interval = int(cfg.get("poll_interval_seconds", 3600))
        except (TypeError, ValueError):
            interval = 3600
        # A zero/negative interval would re-read the album back-to-back.
        self._poll_interval: int = max(_MIN_POLL_INTERVAL, interval)
        self._sync_dir_setting: str = str(cfg.get("sync_dir") or "media/sync/google_photos/")
        self._sync_dir = resolve_install_path(Path(self._sync_dir_setting))

    # -- Sync logic -----------------------------------------------------------

    def _sync(self) -> SyncResult:
        """Perform one sync cycle, guarding against overlapping runs."""
        # ── Network connectivity gate ─────────────────────────────────
        try:
            from metixel.backend.network_manager import is_connected
        except ImportError:
            pass  # Non-Linux / dev environment — assume connected.
        else:
            if not is_connected():
                logger.info("No network connectivity — skipping Google Photos sync cycle")
                result = SyncResult(started_at=time.time(), finished_at=time.time())
                result.errors.append("No network connectivity")
                self._persist_result(result)
                return result

        with self._sync_lock:
            if self._syncing:
                logger.warning("Google Photos sync already in progress — aborting duplicate")
                result = SyncResult(started_at=time.time(), finished_at=time.time())
                result.errors.append("Sync already in progress")
                return result
            self._syncing = True
            self._cancel_requested = False
            with contextlib.suppress(OSError):
                _sync_cancel_path().unlink(missing_ok=True)

        try:
            return self._do_sync()
        finally:
            self._syncing = False
            self._clear_progress()

    def _do_sync(self) -> SyncResult:
        """Read the album and download whatever is missing."""
        result = SyncResult(started_at=time.time())
        logger.info("=== Google Photos sync cycle starting ===")
        self._write_progress("starting", 0, 0, "")

        if not self.is_configured():
            return self._fail(result, "No Google Photos shared album link is set")

        items = self._collect_album_items(result)
        if items is None:
            return result  # _collect_album_items already recorded why

        result.total_remote = len(items)
        if self._is_cancel_requested():
            return self._finish(result, cancelled=True)

        return self._download_all(result, items)

    def _collect_album_items(self, result: SyncResult) -> list[dict[str, Any]] | None:
        """Read the shared album page and its continuation pages.

        A partial read is still useful — the items already collected are
        downloaded and the reason the rest is missing is reported — so a
        truncated album is an error on the result, not an abort.
        """
        self._write_progress("fetching_album", 0, 0, "")

        def _progress(count: int, pages: int) -> None:
            self._write_progress("fetching_album", 0, count, f"{count} photos · page {pages}")

        try:
            listing = album.fetch_album(
                self._shared_album_url,
                on_progress=_progress,
                should_cancel=self._is_cancel_requested,
            )
        except album.SharedAlbumError as e:
            self._fail(result, str(e))
            return None
        except requests.exceptions.RequestException as e:
            self._fail(result, f"Could not read the shared album: {e}")
            return None

        result.album_title = str(listing.get("title") or "")
        result.album_pages = int(listing.get("pages") or 0)
        result.album_complete = bool(listing.get("complete", not listing.get("truncated")))
        items: list[dict[str, Any]] = list(listing.get("items") or [])
        logger.info(
            "Shared album '%s' holds %d item(s) across %d page(s)%s",
            result.album_title,
            len(items),
            result.album_pages,
            " (truncated)" if listing.get("truncated") else "",
        )
        if listing.get("truncated"):
            result.errors.append(
                f"Only the first {len(items)} photos could be read — "
                "the album is larger than Metixel's page limit, or Google stopped paging"
            )
        return items

    # -- Download ------------------------------------------------------------

    def _download_all(self, result: SyncResult, items: list[dict[str, Any]]) -> SyncResult:
        """Download the missing items, then apply strict mode.

        Files are matched by *stem* (``gphoto_<digest>``) rather than full
        filename, because the real file type is only known once the download
        responds — so the extension cannot be part of the identity of an item.
        """
        try:
            self._sync_dir.mkdir(parents=True, exist_ok=True)
            # Only files this syncer wrote are ours to match or delete: the
            # folder is user-configurable, and strict sync must never remove
            # someone's own uploads from it.
            local_names = [
                e.name
                for e in self._sync_dir.iterdir()
                if e.is_file() and e.name.startswith(_FILENAME_PREFIX)
            ]
        except OSError as e:
            return self._fail(result, f"Local directory error: {e}")

        local_by_stem: dict[str, list[str]] = {}
        for name in local_names:
            local_by_stem.setdefault(Path(name).stem, []).append(name)

        remote_map: dict[str, dict[str, Any]] = {self._stem_for(item): item for item in items}
        to_download = sorted(set(remote_map) - set(local_by_stem))
        download_total = len(to_download)
        logger.info(
            "Google Photos: local=%d, remote=%d, to download=%d",
            len(local_by_stem),
            result.total_remote,
            download_total,
        )

        downloaded = 0
        failed = 0
        for stem in to_download:
            if self._is_cancel_requested():
                result.errors.append("Cancelled by user")
                logger.info(
                    "Google Photos sync cancelled — %d/%d downloaded", downloaded, download_total
                )
                break
            self._write_progress("downloading", download_total, downloaded, stem)
            try:
                self._download_item(remote_map[stem], stem)
                result.downloaded += 1
                downloaded += 1
            except Exception as e:  # noqa: BLE001 — reported, never fatal
                failed += 1
                logger.error("Failed to download %s: %s", stem, e)
                result.errors.append(f"Download failed for {stem}: {e}")
                self._write_progress("downloading", download_total, downloaded, f"{stem} — FAILED")

        # "Already here" is what was found on disk — never items a cancel
        # left unattempted.
        result.skipped = len(remote_map) - download_total

        # ── Strict (mirror) mode ──────────────────────────────────────
        if self._strict_sync and not result.album_complete:
            # Deleting against a partial listing would remove every photo
            # beyond the pages that were read.
            logger.warning("Strict sync skipped — the shared album was only partly read")
            result.errors.append(
                "Strict sync skipped this cycle: the album was only partly read, "
                "so nothing was deleted"
            )
        elif self._strict_sync and not self._is_cancel_requested():
            self._write_progress("cleaning", 0, 0, "")
            for stem in sorted(set(local_by_stem) - set(remote_map)):
                if self._is_cancel_requested():
                    break
                for name in local_by_stem[stem]:
                    try:
                        (self._sync_dir / name).unlink()
                        logger.info("Deleted (no longer in the shared album): %s", name)
                        result.deleted += 1
                    except OSError as e:
                        logger.error("Failed to delete %s: %s", name, e)
                        result.errors.append(f"Delete failed for {name}: {e}")

        cancelled = self._is_cancel_requested()
        if cancelled and "Cancelled by user" not in result.errors:
            result.errors.append("Cancelled by user")

        if result.downloaded:
            self._ensure_watch_path()

        return self._finish(result, cancelled=cancelled)

    def _finish(self, result: SyncResult, *, cancelled: bool = False) -> SyncResult:
        """Stamp, log and persist a completed cycle."""
        result.success = not result.errors
        result.finished_at = time.time()
        logger.info(
            "=== Google Photos sync complete: %d downloaded, %d skipped, "
            "%d deleted, %d errors (%.1fs) ===",
            result.downloaded,
            result.skipped,
            result.deleted,
            len(result.errors),
            result.duration_seconds,
        )
        self._last_result = result
        self._persist_result(result)
        self._write_progress("cancelled" if cancelled else "complete", 0, 0, "")
        return result

    def _fail(self, result: SyncResult, message: str) -> SyncResult:
        """Record *message* as the reason this cycle could not run."""
        result.errors.append(message)
        result.finished_at = time.time()
        result.success = False
        logger.warning("Google Photos sync aborted: %s", message)
        self._write_progress("error", 0, 0, message)
        self._last_result = result
        self._persist_result(result)
        return result

    def _download_item(self, item: dict[str, Any], stem: str) -> Path:
        """Download one album item into the sync directory as ``<stem><ext>``.

        Streams to a temp file and renames atomically, so an interrupted
        download never leaves a half-written photo for the frame to render.
        The share page carries no MIME type, so the extension comes from the
        download's own ``Content-Type``.

        Returns the path written.
        """
        base_url = str(item.get("base_url") or "")
        if not base_url:
            raise RuntimeError("item has no download URL")

        is_video = bool(item.get("is_video"))
        # ``=d`` fetches the original bytes (EXIF intact); videos use ``=dv``.
        url = base_url + ("=dv" if is_video else "=d")

        self._sync_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._sync_dir / f".{stem}.tmp"
        self._check_disk_space(tmp_path)

        try:
            content_type = self._download_with_retries(url, tmp_path, stem)
            final_path = self._sync_dir / (stem + _extension_for(content_type, is_video))
            os.replace(tmp_path, final_path)
            logger.info("Downloaded: %s (%d bytes)", final_path.name, final_path.stat().st_size)
            return final_path
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    def _download_with_retries(self, url: str, tmp_path: Path, stem: str) -> str:
        """Stream *url* into *tmp_path*; returns the response ``Content-Type``.

        A public shared album's media URLs need no credentials, only a
        browser-ish User-Agent.
        """
        headers = {"Accept": "application/octet-stream", "User-Agent": album.USER_AGENT}
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with self._http.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=_REQUEST_TIMEOUT,
                ) as r:
                    if r.status_code in (401, 403):
                        raise RuntimeError(
                            f"Google refused the download (HTTP {r.status_code}) — "
                            "the shared album link may have been turned off"
                        )
                    r.raise_for_status()

                    content_type = ""
                    getter = getattr(r, "headers", None)
                    if getter is not None:
                        content_type = str(getter.get("Content-Type") or "")
                    # An HTML body here is a sign-in or error page, not media —
                    # writing it would put a broken "photo" in the library.
                    if content_type.split(";")[0].strip().lower().startswith("text/"):
                        raise RuntimeError(
                            f"Google returned {content_type.split(';')[0]} instead of media"
                        )

                    with open(tmp_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())

                return content_type

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if attempt >= _MAX_RETRIES:
                    raise
                logger.warning(
                    "HTTP %s downloading %s, retry %d/%d in %ds",
                    status,
                    stem,
                    attempt,
                    _MAX_RETRIES,
                    _RETRY_DELAY,
                )
                time.sleep(_RETRY_DELAY)

            except requests.exceptions.Timeout as e:
                if attempt >= _MAX_RETRIES:
                    raise RuntimeError(
                        f"Timeout downloading {stem} after {_MAX_RETRIES} attempts"
                    ) from e
                logger.warning(
                    "Timeout downloading %s, retry %d/%d in %ds",
                    stem,
                    attempt,
                    _MAX_RETRIES,
                    _RETRY_DELAY,
                )
                time.sleep(_RETRY_DELAY)

            except requests.exceptions.ConnectionError as e:
                if attempt >= _MAX_RETRIES:
                    raise RuntimeError(
                        f"Connection failed for {stem} after {_MAX_RETRIES} attempts"
                    ) from e
                logger.warning(
                    "Connection error downloading %s, retry %d/%d in %ds",
                    stem,
                    attempt,
                    _MAX_RETRIES,
                    _RETRY_DELAY * 2,
                )
                time.sleep(_RETRY_DELAY * 2)

        # Every branch above returns or raises; this is a safety net.
        raise RuntimeError(f"Download failed for {stem} after {_MAX_RETRIES} attempts")

    # -- Helpers --------------------------------------------------------------

    @staticmethod
    def _stem_for(item: dict[str, Any]) -> str:
        """Build the deterministic on-disk *stem* for an album item.

        ``gphoto_<digest of the media key>`` — the digest keeps the name short
        and unique (Google's keys are long and contain characters that are
        awkward in filenames).  Because it derives only from the media key, an
        item already on disk is never fetched twice — which is also why the
        extension is deliberately *not* part of it: the real file type only
        arrives with the download.
        """
        media_key = str(item.get("media_key") or "")
        digest = hashlib.blake2s(media_key.encode(), digest_size=8).hexdigest()[:_DIGEST_CHARS]
        return _FILENAME_PREFIX + digest

    def _ensure_watch_path(self) -> None:
        """Add the sync directory to the local watch paths if it is missing.

        Without this, a frame upgraded from an older config downloads photos
        that the folder watcher never looks at — the user sees a successful
        sync and an unchanged slideshow.
        """
        try:
            config = self._state.config
            local = config.sync.get("local") or {}
            watch_paths = list(local.get("watch_paths") or [])
            target = self._sync_dir.resolve()
            for entry in watch_paths:
                existing = str((entry or {}).get("path") or "")
                if not existing:
                    continue
                with contextlib.suppress(OSError):
                    if resolve_install_path(Path(existing)).resolve() == target:
                        return
            watch_paths.append({"path": self._sync_dir_setting, "enabled": True})
            self._state.update_config("sync", {"local": {"watch_paths": watch_paths}})
            logger.info(
                "Added the Google Photos sync folder to the local watch paths: %s",
                self._sync_dir_setting,
            )
        except Exception:  # noqa: BLE001 — a convenience, never a sync failure
            logger.warning("Could not add the Google Photos sync folder to the watch paths")

    def _check_disk_space(self, target_path: Path) -> None:
        """Raise ``OSError`` if the target volume has less than 50 MB free."""
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(target_path.parent).free
            min_free = 50 * 1024 * 1024  # 50 MB
            if free_bytes < min_free:
                raise OSError(
                    f"Insufficient disk space: {free_bytes / (1024**2):.1f} MB free "
                    f"(need at least {min_free / (1024**2):.0f} MB)"
                )
        except OSError as e:
            if "Insufficient" in str(e):
                raise
            # If statvfs fails, proceed anyway (e.g. network share).

    def _persist_result(self, result: SyncResult) -> None:
        """Write the sync result to the status file for the web dashboard."""
        try:
            status_path = _sync_status_path()
            status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = status_path.with_name(status_path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(result.to_dict(), f, indent=2)
            os.replace(tmp, status_path)
        except OSError:
            logger.debug("Could not write sync status file — /run/metixel unavailable?")

    def _write_progress(self, phase: str, total: int, processed: int, current_file: str) -> None:
        """Write a live progress snapshot for the web dashboard to poll."""
        try:
            progress_path = _sync_progress_path()
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = progress_path.with_name(progress_path.name + ".tmp")
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
            os.replace(tmp, progress_path)
        except OSError:
            pass

    def _clear_progress(self) -> None:
        """Remove the live progress file."""
        with contextlib.suppress(OSError):
            _sync_progress_path().unlink(missing_ok=True)


# ── Module helpers ──────────────────────────────────────────────────────────


def probe_shared_album(
    url: str,
    *,
    get_text: Any = None,
    post_form: Any = None,
) -> dict[str, Any]:
    """Check that a shared-album URL can be read, without syncing it.

    Only the album page itself is read (no continuation pages), so this stays
    quick on a huge album.  It always answers rather than raising, so an
    upstream failure never looks like the dashboard's own backend failing.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "Paste the album's share link first"}
    if not album.is_album_url(url):
        return {
            "ok": False,
            "error": (
                "That doesn't look like a Google Photos share link — expected "
                "https://photos.google.com/share/… or https://photos.app.goo.gl/…"
            ),
        }

    try:
        listing = album.fetch_album(url, max_pages=1, get_text=get_text, post_form=post_form)
    except album.SharedAlbumError as e:
        return {"ok": False, "error": str(e)}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Could not reach Google — check the network."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Connection to Google timed out."}
    except Exception as e:  # noqa: BLE001 — a probe must always answer
        return {"ok": False, "error": str(e)}

    count = len(listing.get("items") or [])
    title = str(listing.get("title") or "")
    more = bool(listing.get("truncated"))
    message = (
        f"Read “{title}” — {count} photo(s) on the first page"
        if title
        else (f"Read the album — {count} photo(s) on the first page")
    )
    if more:
        message += "; the rest will be paged in on sync"
    return {
        "ok": True,
        "message": message,
        "count": count,
        "title": title,
        "has_more_pages": more,
    }


def _extension_for(content_type: str, is_video: bool) -> str:
    """Pick a file extension from a download's ``Content-Type``.

    Only image/video types are trusted; a generic ``application/octet-stream``
    (which the request's ``Accept`` header invites) would otherwise become
    ``.bin``, a file the slideshow ignores.
    """
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime]
    if mime.startswith(("image/", "video/")):
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".mp4" if is_video else ".jpg"
