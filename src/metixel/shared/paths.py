# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Install-root and run-directory path resolution.

Centralises the ``/opt/metixel`` base-dir logic and the ``/run/metixel``
runtime-dir logic that were previously inlined (and drifted) across ~17
files.  On non-Linux/dev machines the install root falls back to the
current working directory so the desktop (tk) backend works unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path


def install_root() -> Path:
    """Return the Metixel install root.

    ``/opt/metixel`` on Linux (posix), ``Path.cwd()`` otherwise (desktop
    development on Windows/macOS).
    """
    return Path("/opt/metixel") if os.name == "posix" else Path.cwd()


def resolve_install_path(path: Path | str) -> Path:
    """Resolve a possibly-relative path against :func:`install_root`.

    Absolute paths are returned unchanged; relative paths are joined onto
    the install root (e.g. ``"media/"`` -> ``/opt/metixel/media/``).
    """
    p = Path(path)
    if p.is_absolute():
        return p
    return install_root() / p


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
