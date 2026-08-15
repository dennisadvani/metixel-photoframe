# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Processing journal — single-writer persisted state for the media pipeline.

Records per-file lifecycle + processing outcome so that:

* the folder watcher never re-picks-up the same file twice within a run
  (a file in a terminal state with an unchanged fingerprint is skipped),
* permanently-failed transcodes are remembered across restarts and are
  **not** retried forever (no doomed re-encode on every boot),
* the web UI can show *why* an item is missing from the slideshow
  (failed / skipped with a reason).

This is a **single-writer controller**: every mutation goes through this
class — it owns the only lock and the only write path to disk — so the
concurrent threads (folder watcher, optimisation queue, web API) can never
interleave writes to the journal file.

The journal lives inside the cache directory (``<cache_dir>/processing_state.json``)
so that "Clear cache" wipes it along with the processed files, forcing a clean
full rebuild.  Writes are debounced and atomic (temp file + ``os.replace()``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: File states as it flows through the pipeline.
STATE_PENDING = "pending"  # discovered / queued, not yet finished
STATE_PROCESSING = "processing"  # actively being optimised / transcoded
STATE_READY = "ready"  # in the slideshow playlist (playable)
STATE_FAILED = "failed"  # processing/transcode failed — not playable
STATE_SKIPPED = "skipped"  # unreadable/unsupported — excluded

#: Terminal states that are never re-picked-up while the fingerprint is unchanged.
_TERMINAL_STATES = frozenset({STATE_READY, STATE_FAILED, STATE_SKIPPED})

#: States the status area reports as "issues".
ISSUE_STATES = frozenset({STATE_FAILED, STATE_SKIPPED})


class ProcessingJournal:
    """Thread-safe, single-writer journal of per-file processing state.

    Keyed by the **resolved absolute path** of the original media file.
    Each entry carries the file's stat fingerprint ``(mtime_ns, size)`` so
    the watcher can detect modifications without re-reading content.
    """

    def __init__(self, path: Path, save_after: float = 1.0) -> None:
        self._path = Path(path)
        self._save_after = save_after
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._save_timer: threading.Timer | None = None
        self._load()

    # -- Persistence --------------------------------------------------------

    def _load(self) -> None:
        """Load a previously persisted journal (missing/corrupt → empty)."""
        try:
            if self._path.is_file():
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("files", {}) if isinstance(data, dict) else {}
                self._entries = {
                    str(key): value for key, value in raw.items() if isinstance(value, dict)
                }
                if self._entries:
                    logger.debug(
                        "Loaded processing journal: %d file(s) from %s",
                        len(self._entries),
                        self._path,
                    )
        except (OSError, ValueError):
            logger.warning("Could not load processing journal from %s", self._path)

    def _persist(self) -> None:
        """Atomically write the journal (temp file + os.replace)."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                payload = {"version": 1, "files": self._entries}
                tmp = self._path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp, self._path)
                self._dirty = False
            except OSError:
                logger.warning("Could not persist processing journal to %s", self._path)

    def _schedule_save(self) -> None:
        """Mark dirty and coalesce disk writes with a debounce timer."""
        with self._lock:
            self._dirty = True
            if self._save_timer is not None:
                return
            self._save_timer = threading.Timer(self._save_after, self._flush_timer)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _flush_timer(self) -> None:
        with self._lock:
            self._save_timer = None
        self._persist()

    def flush(self) -> None:
        """Persist any pending changes immediately (call on shutdown)."""
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        self._persist()

    # -- Reads --------------------------------------------------------------

    @staticmethod
    def _key(path: Path | str) -> str:
        """Normalise any path form to the journal's resolved-string key."""
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(Path(path))

    def get(self, path: Path | str) -> dict[str, Any] | None:
        """Return a copy of an entry (``None`` if unknown)."""
        key = self._key(path)
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def paths(self) -> list[str]:
        """Return all known file paths (resolved absolute strings)."""
        with self._lock:
            return list(self._entries.keys())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a deep-ish copy of the whole journal (for tests/debug)."""
        with self._lock:
            return {key: dict(value) for key, value in self._entries.items()}

    def stats(self) -> dict[str, int]:
        """Return a per-state count (for the web UI / debugging)."""
        with self._lock:
            counts = {
                STATE_PENDING: 0,
                STATE_PROCESSING: 0,
                STATE_READY: 0,
                STATE_FAILED: 0,
                STATE_SKIPPED: 0,
            }
            for entry in self._entries.values():
                state = entry.get("state")
                if state in counts:
                    counts[state] += 1
            return counts

    def issues(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return failed/skipped entries, newest first, for the status area.

        Each item exposes ``path``, ``name``, ``media_type``, ``state``,
        ``reason`` and ``updated_at`` for the dashboard.
        """
        with self._lock:
            items = [
                dict(value)
                for value in self._entries.values()
                if value.get("state") in ISSUE_STATES
            ]
        items.sort(key=lambda entry: entry.get("updated_at", 0.0), reverse=True)
        return items[:limit]

    def is_handled(self, path: Path | str, fingerprint: tuple[int, int]) -> bool:
        """Whether *path* is in a terminal state with an unchanged fingerprint.

        Used by the folder watcher to skip files that never need to be
        re-picked-up: already ready, permanently failed, or skipped, as
        long as the file on disk hasn't changed.
        """
        key = self._key(path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            if entry.get("state") not in _TERMINAL_STATES:
                return False
            return entry.get("mtime_ns") == fingerprint[0] and entry.get("size") == fingerprint[1]

    # -- Mutations (all funnel through the controller lock) ----------------

    def _upsert(self, key: str, **fields: Any) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = {"path": key}
                self._entries[key] = entry
            entry.update(fields)
            entry["updated_at"] = time.time()
            self._schedule_save()

    def mark_pending(
        self,
        path: Path | str,
        fingerprint: tuple[int, int],
        media_type: str | None = None,
        name: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Record that a file was discovered and will be queued."""
        self._upsert(
            self._key(path),
            mtime_ns=fingerprint[0],
            size=fingerprint[1],
            state=STATE_PENDING,
            media_type=media_type,
            name=name,
            content_hash=content_hash,
            transcode_status=None,
            reason=None,
        )

    def mark_processing(self, path: Path | str) -> None:
        """Record that a file is actively being processed."""
        self._upsert(self._key(path), state=STATE_PROCESSING)

    def mark_ready(self, path: Path | str, transcode_status: str | None = None) -> None:
        """Record that a file reached the slideshow playlist."""
        self._upsert(
            self._key(path),
            state=STATE_READY,
            transcode_status=transcode_status,
            reason=None,
        )

    def mark_failed(self, path: Path | str, reason: str) -> None:
        """Record a permanent processing/transcode failure (not playable)."""
        key = self._key(path)
        with self._lock:
            attempts = int(self._entries.get(key, {}).get("attempts", 0) or 0) + 1
        self._upsert(key, state=STATE_FAILED, reason=reason, attempts=attempts)

    def mark_skipped(self, path: Path | str, reason: str) -> None:
        """Record that a file was excluded (unreadable / unsupported)."""
        self._upsert(self._key(path), state=STATE_SKIPPED, reason=reason)

    def retry(self, path: Path | str) -> None:
        """Forget a failed entry so the next folder scan re-discovers it.

        The file is re-gathered and re-processed on the next scan cycle.
        """
        self.remove(path)
        logger.info("Processing journal: retry scheduled for %s", self._key(path))

    def remove(self, path: Path | str) -> None:
        """Drop a file from the journal (deleted from disk, or retried)."""
        key = self._key(path)
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                self._schedule_save()

    def clear(self) -> None:
        """Wipe the entire journal (cache clear / full pipeline reset)."""
        with self._lock:
            if self._entries:
                self._entries.clear()
                self._schedule_save()
                logger.debug("Processing journal cleared")
