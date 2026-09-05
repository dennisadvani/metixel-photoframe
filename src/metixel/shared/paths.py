# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Install-root, data-root, release and run-directory path resolution.

Centralises the ``/opt/metixel`` base-dir logic and the ``/run/metixel``
runtime-dir logic that were previously inlined (and drifted) across ~17
files.  On non-Linux/dev machines the install root falls back to the
current working directory so the desktop (tk) backend works unchanged.

Blue/Green layout
-----------------
The install root is the *container* that holds everything:

* ``/opt/metixel/data/``      — persistent state (config, logs, media, cache).
                               NEVER overwritten by updates.
* ``/opt/metixel/releases/``  — versioned application code (``v1.0.0/``, …).
* ``/opt/metixel/live``       — symlink to the currently active release.

Persistent data (config, logs, media, cache) resolves against
:func:`data_dir`; disposable application code lives under a release and is
reached through the ``live`` symlink.
"""

from __future__ import annotations

import os
from pathlib import Path


def install_root() -> Path:
    """Return the Metixel install root (the persistent container).

    ``/opt/metixel`` on Linux (posix), ``Path.cwd()`` otherwise (desktop
    development on Windows/macOS).
    """
    return Path("/opt/metixel") if os.name == "posix" else Path.cwd()


def data_dir() -> Path:
    """Return the persistent data directory.

    Holds config, logs, media, and cache — never overwritten by updates.
    ``/opt/metixel/data`` on Linux, ``Path.cwd()`` otherwise (desktop).
    """
    return install_root() / "data"


def releases_dir() -> Path:
    """Return the directory holding versioned application releases."""
    return install_root() / "releases"


def live_dir() -> Path:
    """Return the path of the ``live`` symlink to the active release.

    The symlink itself lives at ``install_root() / "live"`` and points at a
    folder under :func:`releases_dir`. On desktop (no symlink) this falls
    back to the install root so the tk backend works unchanged.
    """
    live = install_root() / "live"
    if live.is_symlink() and live.exists():
        return live.resolve()
    return install_root()


def release_dir(version: str) -> Path:
    """Return the release folder for *version* under :func:`releases_dir`."""
    return releases_dir() / version


def resolve_install_path(path: Path | str) -> Path:
    """Resolve a possibly-relative path against :func:`data_dir`.

    Absolute paths are returned as-is; relative paths are joined onto the
    persistent data directory (e.g. ``"media/"`` -> ``/opt/metixel/data/media/``).
    """
    p = Path(path)
    if p.is_absolute():
        return p
    return data_dir() / p


def ensure_data_dirs() -> None:
    """Create the persistent data subdirectories if they do not exist.

    Called on startup so logs, media, and cache are always writable even on a
    fresh install.  Note: config files (config.json, logging.conf) live
    directly under ``data_dir()`` / ``data_dir()/etc`` — there is no
    ``data/config`` subdirectory.
    """
    for sub in ("logs", "media", "cache", "backups"):
        (data_dir() / sub).mkdir(parents=True, exist_ok=True)


def run_dir() -> Path:
    """Return the runtime directory for status/state files.

    Defaults to ``/run/metixel`` (the systemd default on the Pi), but
    honours the ``METIXEL_RUN_DIR`` environment variable so desktop runs
    and tests can use a writable directory.
    """
    return Path(os.environ.get("METIXEL_RUN_DIR", "/run/metixel"))


def run_path(name: str) -> Path:
    """Return ``run_dir() / name`` for a runtime state file."""
    return run_dir() / name
