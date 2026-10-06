# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Google Photos synchronization client (Photos Picker API).

Google removed the Library API's ``readonly``/``sharing``/``search`` scopes on
31 March 2025, so no third-party app can enumerate a user's albums the way the
Immich client does.  The supported replacement is the **Photos Picker API**:
the frame opens a *picking session*, the user picks photos (or every item in an
album) in Google's own UI, and the frame downloads exactly what was picked.

There are two ways to choose what syncs:

* ``mode="picker"`` (default) — the supported path.  A *picking session* is a
  fixed snapshot of what the user chose, so new photos need a new pick.
* ``mode="shared_url"`` — a public shared-album link, read through
  :mod:`metixel.backend.sync.google_photos_album`.  This one is a live album
  (photos added later arrive on the next poll, like Immich) but it reads
  **undocumented** endpoints, so it can break when Google changes its web UI.

Everything else mirrors :mod:`metixel.backend.sync.immich` — a background
thread on a poll interval, a sync directory under ``data/media/sync/``, live
progress and status files for the dashboard, cancellation, and an optional
mirror ("strict") mode.

Authentication uses an OAuth refresh token minted once by
``scripts/google_photos_auth.py`` (the Pi has no browser of its own, and Google
only allows loopback redirects for installed-app clients).  The frame exchanges
it for a short-lived access token whenever it needs one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

#: Picker API base URL.  Note this is a *different* host from the retired
#: Library API (``photoslibrary.googleapis.com``).
PICKER_API = "https://photospicker.googleapis.com/v1"
_API_SESSIONS = PICKER_API + "/sessions"
_API_MEDIA_ITEMS = PICKER_API + "/mediaItems"

#: OAuth endpoints and the single scope the Picker API needs.
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"

#: Access tokens last an hour; refresh this many seconds before expiry.
_TOKEN_SKEW = 120.0

#: Page size for ``mediaItems.list`` (API maximum is 100).
_PAGE_SIZE = 100

#: How the user chooses what to sync.  ``picker`` uses Google's supported
#: Picker API; ``shared_url`` reads a public shared-album page (undocumented,
#: but the only way to follow an album as it grows).
MODE_PICKER = "picker"
MODE_SHARED_URL = "shared_url"
MODES = (MODE_PICKER, MODE_SHARED_URL)

#: Downloaded files are named ``gphoto_<digest>[_<stem>]<ext>`` so the same
#: picked item always maps to the same filename — re-picking a photo that is
#: already on disk costs nothing.
_FILENAME_PREFIX = "gphoto_"
_DIGEST_CHARS = 16
_STEM_CHARS = 48
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: ``mimeType`` → extension for the types Google actually serves.  Anything
#: else falls back to :mod:`mimetypes`, then to the picked filename.
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
#: The current picking session.  Sessions are short-lived (Google returns an
#: ``expireTime``), so the runtime dir is the right home for them — a reboot
#: would have invalidated the session anyway.
_SESSION_FILE = "google_photos_session.json"


def _sync_status_path() -> Path:
    return run_path(_SYNC_STATUS_FILE)


def _sync_progress_path() -> Path:
    return run_path(_SYNC_PROGRESS_FILE)


def _sync_cancel_path() -> Path:
    return run_path(_SYNC_CANCEL_FILE)


def _session_path() -> Path:
    return run_path(_SESSION_FILE)


def parse_rfc3339(value: Any) -> float | None:
    """Parse an RFC 3339 timestamp into a POSIX timestamp.

    Tolerates the nanosecond precision Google sometimes returns (which
    :meth:`datetime.fromisoformat` rejects) and returns ``None`` for anything
    unparseable, so a surprising timestamp can never raise.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Trim fractional seconds to the 6 digits fromisoformat accepts.
    match = re.match(r"^(.*\.\d{6})\d+(.*)$", text)
    if match:
        text = match.group(1) + match.group(2)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def post_form(url: str, data: dict[str, str]) -> Any:
    """POST *data* as ``application/x-www-form-urlencoded``.

    The OAuth token endpoint is form-encoded, which the JSON-only
    :class:`~metixel.shared.ports.HttpGateway` port does not express — so it
    lives here as a module-level function that tests can monkeypatch.
    """
    return requests.post(url, data=data, timeout=_REQUEST_TIMEOUT)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    """Outcome of a single sync cycle (one import of a picking session)."""

    started_at: float = 0.0
    finished_at: float = 0.0
    session_id: str = ""
    #: Shared-album mode only — what was read, and over how many pages.
    album_title: str = ""
    album_pages: int = 0
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
            "session_id": self.session_id,
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
    """Downloads the media a user picked in Google Photos.

    Sync modes
    ----------
    * **Pull-only** (``strict_sync=False``): picked items missing from the sync
      directory are downloaded; everything already there is left alone.
    * **Strict sync** (``strict_sync=True``): the sync directory becomes an
      exact mirror of the most recent picked selection — files that are not in
      it are **deleted**.
    """

    def __init__(self, state: StateManager, http: HttpGateway | None = None) -> None:
        self._state = state
        self._http = http if http is not None else RequestsHttpGateway()
        self._reload_config()
        self._running: bool = False
        self._last_result: SyncResult | None = None
        self._sync_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._syncing: bool = False
        self._cancel_requested: bool = False
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._token_fingerprint: str = ""

    # -- Public API -----------------------------------------------------------

    def run(self) -> None:
        """Main loop — syncs on the configured interval.

        What "due" means depends on the mode:

        * **shared_url** — a live album, so every cycle re-reads it and pulls
          anything new, exactly like the Immich loop.
        * **picker** — a picking session is a fixed snapshot of what the user
          chose, so there is nothing to poll for.  The cycle only has work when
          a session has been picked but not yet fully imported (an interrupted
          download, or a pick made while scheduled sync was off).
        """
        self._running = True
        logger.info(
            "Google Photos syncer started (mode: %s, interval: %ds, strict: %s, configured: %s)",
            self._mode,
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
                elif self._mode == MODE_SHARED_URL:
                    self._sync()
                elif self._pending_session(refresh=True) is None:
                    logger.debug("No picked Google Photos session awaiting import — nothing to do")
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
        """Import the current picking session synchronously (manual trigger)."""
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
        """True when the active mode has everything it needs to sync."""
        if self._mode == MODE_SHARED_URL:
            return album.is_album_url(self._shared_album_url)
        return bool(self._client_id and self._client_secret and self._refresh_token)

    @property
    def mode(self) -> str:
        """The configured mode (``picker`` or ``shared_url``)."""
        return self._mode

    # -- Config helpers -------------------------------------------------------

    def _reload_config(self) -> None:
        """Refresh config from the state manager (handles hot-reload)."""
        config = self._state.config
        cfg = config.sync.get("google_photos") or {}
        self._enabled: bool = cfg.get("enabled", False)
        mode = str(cfg.get("mode") or MODE_PICKER).strip().lower()
        self._mode: str = mode if mode in MODES else MODE_PICKER
        self._shared_album_url: str = str(cfg.get("shared_album_url") or "").strip()
        self._client_id: str = str(cfg.get("client_id") or "").strip()
        self._client_secret: str = str(cfg.get("client_secret") or "").strip()
        self._refresh_token: str = str(cfg.get("refresh_token") or "").strip()
        self._strict_sync: bool = cfg.get("strict_sync", False)
        self._poll_interval: int = cfg.get("poll_interval_seconds", 3600)
        self._sync_dir_setting: str = str(cfg.get("sync_dir") or "media/sync/google_photos/")
        self._sync_dir = resolve_install_path(Path(self._sync_dir_setting))

    # -- OAuth ----------------------------------------------------------------

    def _access_token(self) -> str:
        """Return a valid access token, refreshing it when needed.

        Raises:
            RuntimeError: when credentials are missing or Google refuses the
                refresh token (revoked, wrong client, missing scope).
        """
        if not self.is_configured():
            raise RuntimeError(
                "Google Photos is not connected — add a client ID, client secret "
                "and refresh token first"
            )

        # Credentials are hot-reloadable, so a cached token must be discarded
        # when any of them changes.
        fingerprint = hashlib.blake2s(
            "\n".join((self._client_id, self._client_secret, self._refresh_token)).encode(),
            digest_size=16,
        ).hexdigest()

        with self._token_lock:
            if (
                self._token
                and fingerprint == self._token_fingerprint
                and time.monotonic() < self._token_expires_at - _TOKEN_SKEW
            ):
                return self._token

            token, lifetime = mint_access_token(
                self._client_id, self._client_secret, self._refresh_token
            )
            self._token = token
            self._token_fingerprint = fingerprint
            self._token_expires_at = time.monotonic() + lifetime
            logger.debug("Refreshed Google Photos access token")
            return self._token

    def forget_token(self) -> None:
        """Drop the cached access token (used after credentials change)."""
        with self._token_lock:
            self._token = ""
            self._token_expires_at = 0.0
            self._token_fingerprint = ""

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token()}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def test_credentials(self) -> dict[str, Any]:
        """Probe whatever the active mode needs to work."""
        if self._mode == MODE_SHARED_URL:
            return probe_shared_album(self._shared_album_url)
        return probe_credentials(
            self._client_id,
            self._client_secret,
            self._refresh_token,
            http=self._http,
        )

    # -- Picking sessions -----------------------------------------------------

    def create_session(self) -> dict[str, Any]:
        """Open a Google Photos picking session and persist it locally.

        Returns the stored session dict (``id``, ``picker_uri``,
        ``expire_time``, ``poll_interval_seconds`` …).

        Raises:
            RuntimeError: when Google refuses the request.
        """
        resp = self._http.post(
            _API_SESSIONS,
            headers=self._headers(content_type="application/json"),
            json={},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Could not start a picking session (HTTP {resp.status_code})")
        data = resp.json() or {}
        session_id = str(data.get("id") or "")
        if not session_id:
            raise RuntimeError("Google did not return a session id")

        session = {
            "id": session_id,
            "picker_uri": str(data.get("pickerUri") or ""),
            "expire_time": str(data.get("expireTime") or ""),
            "media_items_set": bool(data.get("mediaItemsSet")),
            "poll_interval_seconds": _polling_interval_seconds(data),
            "created_at": time.time(),
            "imported_at": 0.0,
            "picked_count": 0,
        }
        self._write_session(session)
        logger.info("Opened Google Photos picking session %s", session_id)
        return session

    def read_session(self) -> dict[str, Any] | None:
        """Return the stored session, or ``None`` when there isn't one."""
        with self._session_lock:
            return self._read_session_unlocked()

    def _read_session_unlocked(self) -> dict[str, Any] | None:
        try:
            path = _session_path()
            if not path.is_file():
                return None
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("id") else None

    def _write_session(self, session: dict[str, Any]) -> None:
        with self._session_lock:
            self._write_session_unlocked(session)

    def _write_session_unlocked(self, session: dict[str, Any]) -> None:
        try:
            path = _session_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(session, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            logger.warning("Could not persist the Google Photos session file")

    def _update_session(self, **changes: Any) -> dict[str, Any] | None:
        """Merge *changes* into the stored session (no-op without one)."""
        with self._session_lock:
            session = self._read_session_unlocked()
            if session is None:
                return None
            session.update(changes)
            self._write_session_unlocked(session)
            return session

    def clear_session(self, *, delete_remote: bool = True) -> bool:
        """Forget the stored session, optionally deleting it at Google too."""
        session = self.read_session()
        if session is None:
            return False
        if delete_remote:
            with contextlib.suppress(Exception):
                self._delete_remote_session(str(session.get("id") or ""))
        with contextlib.suppress(OSError):
            _session_path().unlink(missing_ok=True)
        logger.info("Cleared Google Photos picking session %s", session.get("id"))
        return True

    def _delete_remote_session(self, session_id: str) -> None:
        if not session_id:
            return
        token = self._access_token()
        requests.delete(
            f"{_API_SESSIONS}/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT,
        )

    def refresh_session(self) -> dict[str, Any] | None:
        """Re-read the stored session from Google and update the local copy.

        Returns the merged session dict, or ``None`` when no session is stored.
        An expired session is reported with ``expired: True`` so the dashboard
        can prompt for a fresh pick rather than silently doing nothing.
        """
        session = self.read_session()
        if session is None:
            return None

        session["expired"] = _is_expired(session)
        if session["expired"]:
            return session

        try:
            resp = self._http.get(
                f"{_API_SESSIONS}/{session['id']}",
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.debug("Could not refresh picking session: %s", e)
            return session

        if resp.status_code in (403, 404):
            # Google has forgotten the session — so should we.
            session["expired"] = True
            return session
        if resp.status_code >= 400:
            logger.debug("Picking session refresh returned HTTP %s", resp.status_code)
            return session

        data = resp.json() or {}
        updated = self._update_session(
            picker_uri=str(data.get("pickerUri") or session.get("picker_uri") or ""),
            expire_time=str(data.get("expireTime") or session.get("expire_time") or ""),
            media_items_set=bool(data.get("mediaItemsSet")),
            poll_interval_seconds=_polling_interval_seconds(data)
            or session.get("poll_interval_seconds", 5),
        )
        if updated is None:
            return session
        updated["expired"] = _is_expired(updated)
        return updated

    def _pending_session(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """Return a picked-but-not-yet-imported session, if there is one.

        With *refresh*, a session that is not yet marked as picked is re-read
        from Google first — the user may have picked on their phone with the
        dashboard closed, so nothing local would know about it.
        """
        session = self.read_session()
        if session is None:
            return None
        if refresh and not session.get("media_items_set") and not _is_expired(session):
            with contextlib.suppress(Exception):
                session = self.refresh_session() or session
        if not session.get("media_items_set"):
            return None
        if session.get("imported_at"):
            return None
        if _is_expired(session):
            return None
        return session

    # -- Sync logic -----------------------------------------------------------

    def _sync(self) -> SyncResult:
        """Perform one import cycle, guarding against overlapping runs."""
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
        """Download everything the active mode says should be here."""
        result = SyncResult(started_at=time.time())
        logger.info("=== Google Photos sync cycle starting (mode: %s) ===", self._mode)
        self._write_progress("starting", 0, 0, "")

        if not self.is_configured():
            return self._fail(result, "Google Photos is not connected")

        if self._mode == MODE_SHARED_URL:
            items = self._collect_album_items(result)
        else:
            items = self._collect_picked_items(result)
        if items is None:
            return result  # _collect_* already recorded why

        result.total_remote = len(items)
        if self._is_cancel_requested():
            return self._finish(result, cancelled=True)

        return self._download_all(result, items)

    # -- Item collection ------------------------------------------------------

    def _collect_picked_items(self, result: SyncResult) -> list[dict[str, Any]] | None:
        """List the items in the current picking session (``picker`` mode)."""
        session = self.read_session()
        if session is None:
            self._fail(result, "No picking session — choose photos in Google Photos first")
            return None
        result.session_id = str(session.get("id") or "")

        if _is_expired(session):
            self._fail(
                result, "The Google Photos picking session has expired — choose photos again"
            )
            return None

        # The user may have picked since the session was stored.
        if not session.get("media_items_set"):
            refreshed = self.refresh_session() or session
            if refreshed.get("expired"):
                self._fail(
                    result, "The Google Photos picking session has expired — choose photos again"
                )
                return None
            if not refreshed.get("media_items_set"):
                self._fail(result, "No photos have been picked in Google Photos yet")
                return None
            session = refreshed

        self._write_progress("fetching_items", 0, 0, "")
        try:
            items = self._list_picked_items(result.session_id)
        except RuntimeError as e:
            self._fail(result, str(e))
            return None
        except requests.exceptions.RequestException as e:
            self._fail(result, f"Could not list picked photos: {e}")
            return None

        logger.info("Picking session %s holds %d item(s)", result.session_id, len(items))
        self._update_session(picked_count=len(items))
        return items

    def _collect_album_items(self, result: SyncResult) -> list[dict[str, Any]] | None:
        """Read the shared album page (``shared_url`` mode).

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
        raw_items = listing.get("items") or []
        logger.info(
            "Shared album '%s' holds %d item(s) across %d page(s)%s",
            result.album_title,
            len(raw_items),
            result.album_pages,
            " (truncated)" if listing.get("truncated") else "",
        )
        if listing.get("truncated"):
            result.errors.append(
                f"Only the first {len(raw_items)} photos could be read — "
                "the album is larger than Metixel's page limit, or Google stopped paging"
            )
        return album.as_media_items(raw_items)

    # -- Download ------------------------------------------------------------

    def _download_all(self, result: SyncResult, items: list[dict[str, Any]]) -> SyncResult:
        """Download the missing items, then apply strict mode.

        Files are matched by *stem* (``gphoto_<digest>…``) rather than full
        filename, because in shared-album mode the real file type is only known
        once the download responds — so the extension cannot be part of the
        identity of an item.
        """
        try:
            self._sync_dir.mkdir(parents=True, exist_ok=True)
            local_names = [e.name for e in self._sync_dir.iterdir() if e.is_file()]
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

        result.skipped = result.total_remote - result.downloaded - failed

        # ── Strict (mirror) mode ──────────────────────────────────────
        if self._strict_sync and not self._is_cancel_requested():
            self._write_progress("cleaning", 0, 0, "")
            for stem in sorted(set(local_by_stem) - set(remote_map)):
                if self._is_cancel_requested():
                    break
                for name in local_by_stem[stem]:
                    try:
                        (self._sync_dir / name).unlink()
                        logger.info("Deleted (no longer in the Google Photos selection): %s", name)
                        result.deleted += 1
                    except OSError as e:
                        logger.error("Failed to delete %s: %s", name, e)
                        result.errors.append(f"Delete failed for {name}: {e}")

        cancelled = self._is_cancel_requested()
        if cancelled and "Cancelled by user" not in result.errors:
            result.errors.append("Cancelled by user")

        # A fully-imported picking session needs no further cycles.  A shared
        # album has no session and is simply re-read next time.
        if self._mode == MODE_PICKER and not cancelled and failed == 0:
            self._update_session(imported_at=time.time())

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

    # -- Picker API helpers ---------------------------------------------------

    def _list_picked_items(self, session_id: str) -> list[dict[str, Any]]:
        """Return every ``PickedMediaItem`` in *session_id* (follows paging)."""
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"sessionId": session_id, "pageSize": _PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            resp = self._http.get(
                _API_MEDIA_ITEMS,
                headers=self._headers(),
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code == 400:
                # FAILED_PRECONDITION — the user has not finished picking.
                raise RuntimeError("Google Photos is still waiting for photos to be picked")
            if resp.status_code in (403, 404):
                raise RuntimeError(
                    "The Google Photos picking session has expired — choose photos again"
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"Google returned HTTP {resp.status_code} listing picked photos")

            data = resp.json() or {}
            items.extend(data.get("mediaItems") or [])
            page_token = str(data.get("nextPageToken") or "")
            if not page_token:
                break
        return items

    def _download_item(self, item: dict[str, Any], stem: str) -> Path:
        """Download one item into the sync directory as ``<stem><ext>``.

        Streams to a temp file and renames atomically, so an interrupted
        download never leaves a half-written photo for the frame to render.
        The extension comes from the item when Google described it (picker
        mode) and otherwise from the download's own ``Content-Type``
        (shared-album mode, where the page data carries no MIME type).

        Returns the path written.
        """
        media_file = item.get("mediaFile") or {}
        base_url = str(media_file.get("baseUrl") or "")
        if not base_url:
            raise RuntimeError("item has no download URL")

        is_video = str(item.get("type") or "").upper() == "VIDEO"
        # ``=d`` fetches the original bytes (EXIF intact); videos use ``=dv``.
        url = base_url + ("=dv" if is_video else "=d")

        self._sync_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._sync_dir / f".{stem}.tmp"
        self._check_disk_space(tmp_path)

        known_ext = (
            _extension_for(media_file, item.get("type"))
            if (media_file.get("mimeType") or media_file.get("filename"))
            else ""
        )

        try:
            content_type = self._download_with_retries(url, tmp_path, stem)
            ext = known_ext or _extension_for({"mimeType": content_type}, item.get("type"))
            final_path = self._sync_dir / (stem + ext)
            os.replace(tmp_path, final_path)
            logger.info("Downloaded: %s (%d bytes)", final_path.name, final_path.stat().st_size)
            return final_path
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    def _download_headers(self) -> dict[str, str]:
        """Headers for a media download.

        Picker downloads are authorised with the OAuth access token.  A public
        shared album has no credentials at all — its URLs are fetchable as-is,
        and asking for a token would fail — so only a browser-ish User-Agent
        goes along.
        """
        headers = {"Accept": "application/octet-stream"}
        if self._mode == MODE_SHARED_URL:
            headers["User-Agent"] = album.USER_AGENT
            return headers
        headers["Authorization"] = f"Bearer {self._access_token()}"
        return headers

    def _download_with_retries(self, url: str, tmp_path: Path, stem: str) -> str:
        """Stream *url* into *tmp_path*; returns the response ``Content-Type``."""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with self._http.get(
                    url,
                    headers=self._download_headers(),
                    stream=True,
                    timeout=_REQUEST_TIMEOUT,
                ) as r:
                    if r.status_code in (401, 403):
                        raise RuntimeError(
                            f"Google refused the download (HTTP {r.status_code}) — "
                            + (
                                "the shared album link may have been turned off"
                                if self._mode == MODE_SHARED_URL
                                else "the picking session may have expired"
                            )
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
        """Build the deterministic on-disk *stem* for an item.

        ``gphoto_<digest of the item id>[_<original stem>]`` — the digest keeps
        the name short and unique (Google's ids are long and contain characters
        that are awkward in filenames) while the original stem, when Google
        gives one, keeps the media library readable.  Because it derives only
        from the item id and its filename, an item already on disk is never
        fetched twice — which is also why the extension is deliberately *not*
        part of it: in shared-album mode the real file type only arrives with
        the download.
        """
        item_id = str(item.get("id") or "")
        media_file = item.get("mediaFile") or {}
        original = str(media_file.get("filename") or "")

        digest = hashlib.blake2s(item_id.encode(), digest_size=8).hexdigest()[:_DIGEST_CHARS]
        stem = _SAFE_STEM_RE.sub("_", Path(original).stem)[:_STEM_CHARS].strip("._-")
        return _FILENAME_PREFIX + digest + (f"_{stem}" if stem else "")

    @classmethod
    def _filename_for(cls, item: dict[str, Any]) -> str:
        """The on-disk name for an item whose type is already known."""
        media_file = item.get("mediaFile") or {}
        return cls._stem_for(item) + _extension_for(media_file, item.get("type"))

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


def mint_access_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, float]:
    """Exchange a refresh token for an access token.

    Returns ``(access_token, lifetime_seconds)``.

    Raises:
        RuntimeError: when Google refuses the exchange or answers with
            something unusable.
    """
    resp = post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(_token_error_message(resp))

    try:
        payload = resp.json() or {}
    except ValueError as e:
        raise RuntimeError("Google returned an unreadable token response") from e

    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Google did not return an access token")
    return token, float(payload.get("expires_in") or 3600)


def probe_credentials(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    http: HttpGateway | None = None,
) -> dict[str, Any]:
    """Check that a credential set can actually open a picking session.

    Returns a dashboard-friendly ``{"ok": bool, ...}`` dict rather than
    raising: this is a diagnostic of an *upstream* service, so its failures
    must never look like the dashboard's own auth or backend errors.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not (client_id and client_secret and refresh_token):
        return {
            "ok": False,
            "error": (
                "Client ID, client secret and refresh token are all required — "
                "run scripts/google_photos_auth.py to get them"
            ),
        }

    gateway = http if http is not None else RequestsHttpGateway()
    try:
        token, _ = mint_access_token(client_id, client_secret, refresh_token)
        # Creating (then deleting) a session is the only call that proves the
        # token really carries the Picker scope and that the API is enabled.
        resp = gateway.post(
            _API_SESSIONS,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={},
            timeout=_REQUEST_TIMEOUT,
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Could not reach Google — check the network."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Connection to Google timed out."}
    except Exception as e:  # noqa: BLE001 — a probe must always answer
        return {"ok": False, "error": str(e)}

    if resp.status_code in (401, 403):
        return {
            "ok": False,
            "status": resp.status_code,
            "error": (
                f"Google rejected the credentials (HTTP {resp.status_code}). Check that the "
                "Photos Picker API is enabled for the project and that the refresh token was "
                "granted the photospicker.mediaitems.readonly scope."
            ),
        }
    if resp.status_code >= 400:
        return {
            "ok": False,
            "status": resp.status_code,
            "error": f"Google returned HTTP {resp.status_code}",
        }

    # Throw the probe session away so it can't be mistaken for a real one.
    with contextlib.suppress(Exception):
        session_id = str((resp.json() or {}).get("id") or "")
        if session_id:
            requests.delete(
                f"{_API_SESSIONS}/{session_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT,
            )

    return {"ok": True, "message": "Connected to Google Photos"}


def probe_shared_album(
    url: str,
    *,
    get_text: Any = None,
    post_form: Any = None,
) -> dict[str, Any]:
    """Check that a shared-album URL can be read, without syncing it.

    Only the album page itself is read (no continuation pages), so this stays
    quick on a huge album.  Like :func:`probe_credentials` it always answers
    rather than raising.
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


def _extension_for(media_file: dict[str, Any], media_type: Any) -> str:
    """Pick a file extension from the MIME type, filename, then media type."""
    mime = str(media_file.get("mimeType") or "").split(";")[0].strip().lower()
    if mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime]
    if mime:
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed

    ext = Path(str(media_file.get("filename") or "")).suffix.lower()
    if ext:
        return ext
    return ".mp4" if str(media_type or "").upper() == "VIDEO" else ".jpg"


def _polling_interval_seconds(session: dict[str, Any]) -> int:
    """Seconds between session polls, from Google's ``pollingConfig``.

    Google expresses it as a duration string (``"2.500s"``); fall back to 5
    seconds when it is missing or unparseable, and never poll faster than 1s.
    """
    raw = str(((session.get("pollingConfig") or {}).get("pollInterval")) or "")
    match = re.match(r"^(\d+(?:\.\d+)?)s$", raw)
    if not match:
        return 5
    return max(1, round(float(match.group(1))))


def _is_expired(session: dict[str, Any]) -> bool:
    """True when the session's ``expireTime`` is in the past."""
    expires_at = parse_rfc3339(session.get("expire_time"))
    if expires_at is None:
        return False
    return expires_at <= time.time()


def _token_error_message(resp: Any) -> str:
    """Turn a failed token response into something a user can act on."""
    detail = ""
    with contextlib.suppress(Exception):
        payload = resp.json() or {}
        detail = str(payload.get("error_description") or payload.get("error") or "")
    if resp.status_code in (400, 401):
        return (
            "Google rejected the refresh token"
            + (f" ({detail})" if detail else "")
            + " — re-run scripts/google_photos_auth.py to mint a new one"
        )
    return f"Google token endpoint returned HTTP {resp.status_code}" + (
        f" ({detail})" if detail else ""
    )
