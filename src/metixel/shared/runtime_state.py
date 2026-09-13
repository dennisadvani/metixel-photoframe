# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Transient runtime state backed by tmpfs.

Some values look like configuration but are really fleeting runtime facts —
"when did we last check for an update?" is the motivating example.  Persisting
those to ``config.json`` rewrites the SD card on every check (the update loop
runs every 60 s), which is needless flash wear, and each rewrite also fires an
inotify event that makes the frontend reload its config.

Such values live here instead, in ``run_dir()`` (``/run/metixel``), which is a
tmpfs mount on the Pi: writes cost RAM, not flash, and are discarded on reboot —
which is correct, because a fresh boot re-derives them anyway.

What must NOT live here: anything that has to survive a reboot, such as the
weekly auto-update bookkeeping (losing it would let the schedule re-fire) or a
user preference like the selected channel.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from metixel.shared.io import atomic_write_json
from metixel.shared.paths import run_path

logger = logging.getLogger(__name__)

#: Name of the runtime state file inside ``run_dir()``.
UPDATE_STATE_FILE = "update_state.json"

#: Minimum seconds between writes of the same key.
#:
#: Belt-and-braces against flash wear and write amplification: even though this
#: file is on tmpfs, an unbounded write loop would still burn CPU and churn
#: inotify.  Callers that need an unconditional write pass ``force=True``.
DEFAULT_WRITE_INTERVAL_SECONDS = 30

#: In-process write throttle: {path -> {key -> monotonic time of last write}}.
_last_write: dict[str, dict[str, float]] = {}


def _state_path(name: str = UPDATE_STATE_FILE) -> Path:
    return run_path(name)


def read_runtime_state(name: str = UPDATE_STATE_FILE) -> dict[str, Any]:
    """Return the runtime state dict, or ``{}`` if unavailable.

    Never raises: this is best-effort telemetry, so a missing or corrupt file
    (e.g. cleared tmpfs after a reboot) must not break the caller.
    """
    path = _state_path(name)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not read runtime state %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def write_runtime_state(
    updates: dict[str, Any],
    *,
    name: str = UPDATE_STATE_FILE,
    min_interval: float = DEFAULT_WRITE_INTERVAL_SECONDS,
    force: bool = False,
) -> bool:
    """Merge *updates* into the runtime state file.

    Returns ``True`` when a write happened, ``False`` when it was skipped.

    Writes are throttled per key so a fast caller cannot hammer the file.  The
    throttle is deliberately per-key: writing ``last_check`` must not suppress an
    unrelated key that is due.
    """
    path = _state_path(name)
    now = time.monotonic()
    seen = _last_write.setdefault(str(path), {})

    if not force:
        due = {
            key: value for key, value in updates.items() if now - seen.get(key, 0.0) >= min_interval
        }
        if not due:
            return False
        updates = due

    merged = read_runtime_state(name)
    # A None value CLEARS the key (rather than persisting a JSON null, which
    # would be indistinguishable from a real value on the next read).
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    try:
        atomic_write_json(path, merged, indent=2)
    except OSError as exc:
        # Same graceful-degradation rule as everywhere else: never crash over a
        # state file.  /run may be unwritable on a desktop or a hardened run.
        logger.debug("Could not write runtime state %s: %s", path, exc)
        return False

    for key in updates:
        seen[key] = now
    return True
