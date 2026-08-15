# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Startup dependency self-heal.

Ensures the runtime Python dependencies declared in ``requirements-pip.txt``
are installed. On a normal boot every dependency is already present, so this is
a fast no-op. After an OTA from an older release, newly-required deps (e.g.
``pillow-heif`` for HEIC/HEIF media) may be missing; installing them here means
a single OTA can also resolve the missing runtime dependencies — the
"one-step upgrade" guarantee, even for devices upgrading from code that
predates the OTA install-script hand-off.

Graceful by design: failures are logged and never raised, and the backend
always continues to start.
"""

from __future__ import annotations

import importlib.metadata
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from metixel.shared.subprocess import run_cmd

logger = logging.getLogger(__name__)

# The pip invocation used to install missing deps. Kept explicit so tests can
# inject a fake; runs as the current user (which works for the Pi ``pi`` user,
# and needs no sudo because pip installs into the user site-packages).
#
# ``--no-cache-dir`` avoids pip failing when an OTA (which runs pip as root via
# systemd-run) leaves a root-owned cache that the backend user cannot write —
# the self-heal must install deps regardless of cache state.
_DEFAULT_PIP = (
    "python3",
    "-m",
    "pip",
    "install",
    "--break-system-packages",
    "--no-cache-dir",
)


def _parse_requirement_name(line: str) -> str | None:
    """Best-effort extraction of the distribution name from a requirement line.

    Handles ``name``, ``name==1.0``, ``name>=1.0,<2.0``, ``name[extra]>=1.0``
    and ``name ; python_version < "3.12"``. Returns ``None`` for blanks,
    comments, or lines without a usable name.
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    # Drop any environment marker after ';'
    text = text.split(";", 1)[0].strip()
    # Drop extras '[..]'
    text = text.split("[", 1)[0].strip()
    # Name is everything up to the first comparison operator / whitespace.
    name = ""
    for ch in text:
        if ch in "=<>~! \t":
            break
        name += ch
    name = name.strip()
    return name or None


def missing_requirements(req_file: Path) -> list[str]:
    """Return the names of runtime requirements that are not installed."""
    if not req_file.is_file():
        return []
    missing: list[str] = []
    for raw in req_file.read_text(encoding="utf-8").splitlines():
        name = _parse_requirement_name(raw)
        if name is None:
            continue
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return missing


def ensure_runtime_dependencies(
    repo_root: Path,
    *,
    pip: tuple[str, ...] = _DEFAULT_PIP,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_cmd,
    log: logging.Logger = logger,
) -> list[str]:
    """Install any missing runtime dependencies from ``requirements-pip.txt``.

    Returns the list of package names that were (attempted to be) installed,
    or ``[]`` when everything is already present. Never raises — failures are
    logged and the missing packages are returned so callers can report them.
    """
    req_file = repo_root / "requirements-pip.txt"
    missing = missing_requirements(req_file)
    if not missing:
        log.debug("Runtime dependencies present (requirements-pip.txt satisfied)")
        return []

    log.warning(
        "Missing runtime dependencies (%s) — installing from %s…",
        ", ".join(sorted(missing)),
        req_file,
    )
    try:
        result = run([*pip, "-r", str(req_file)], timeout=600)
    except Exception as exc:  # noqa: BLE001
        log.error("Runtime dependency install failed: %s", exc)
        return missing

    if result.returncode != 0:
        tail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()[-400:]
        log.error(
            "Runtime dependency install failed (rc=%d): %s",
            result.returncode,
            tail,
        )
        return missing

    log.info("Installed missing runtime dependencies: %s", ", ".join(sorted(missing)))
    return missing
