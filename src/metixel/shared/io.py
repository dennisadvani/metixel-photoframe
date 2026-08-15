# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Atomic filesystem I/O helpers.

Centralises the temp-file + ``os.replace()`` write pattern and the
read-a-JSON-file-with-safe-defaults pattern that were previously hand-
rolled (with inconsistent ``fsync`` discipline) across the optimisation
queue, folder watcher, immich sync, state manager, and config module.

* :func:`atomic_write_json` — write JSON to a temp file, ``fsync`` it,
  then atomically ``os.replace`` over the destination.  Every writer here
  uses the same durability guarantees (parent dir created, data flushed
  and fsynced before the rename) so a crash never leaves a torn file.
* :func:`atomic_write_text` — same contract for plain text.
* :func:`read_json` — read a JSON file, returning a caller-supplied
  default (never raising) if it is missing or malformed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Serialises concurrent read-modify-write cycles on JSON state files so two
# writers (e.g. the folder watcher's `scanning` phase and the optimisation
# queue's `optimising_images` phase, both patching processing_status.json)
# never clobber each other's updates with a stale snapshot.
_JSON_MERGE_LOCK = threading.Lock()


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
) -> None:
    """Atomically write ``data`` as JSON to *path*.

    Creates parent directories, writes to a sibling ``.tmp`` file, flushes
    and ``fsync``es it, then ``os.replace``s it over the destination so
    readers (e.g. the web dashboard or an inotify watcher) never observe a
    partially-written file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, sort_keys=sort_keys)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
    logger.debug("Atomically wrote JSON to %s", path)


def atomic_write_text(path: Path | str, text: str) -> None:
    """Atomically write plain text to *path* (fsync + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
    logger.debug("Atomically wrote text to %s", path)


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read a JSON file, returning *default* if missing or malformed.

    Never raises — callers can treat the default as "no data yet".
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def merge_json(
    path: Path | str,
    update: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically read-modify-write a JSON state file under a shared lock.

    Holds a process-wide lock while it reads *path*, applies *update* to
    the loaded dict, and writes the result back atomically.  This prevents
    the lost-update race where two threads both ``read_json``, each patch
    a different key, and the second ``atomic_write_json`` clobbers the
    first thread's change.

    ``update`` receives the current dict (or *default* if the file is
    missing/malformed) and must return the dict to persist.  Returns the
    merged result.
    """
    with _JSON_MERGE_LOCK:
        data = read_json(path, default)
        if not isinstance(data, dict):
            data = dict(default or {})
        result = update(data)
        atomic_write_json(path, result)
        return result
