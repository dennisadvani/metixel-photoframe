# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Shared utilities for process throttling used by the processing pipeline.

Provides a single ``nice``-wrapping helper so every ffmpeg / ffprobe
invocation — whether from the folder watcher, thumbnail generator, or
video processor — runs at the lowest scheduling priority.  This keeps
the frontend slideshow responsive even when the backend is scanning
or optimising media.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Cached availability check — shutil.which is cheap but we call this
# once on import to avoid repeated filesystem lookups.
_NICE_BINARY: str | None = shutil.which("nice")


def ensure_heif_support() -> None:
    """Register pillow-heif so PIL can decode HEIC/HEIF images (optional dep).

    iPhone/HEIC originals often arrive with a ``.jpg`` extension (e.g. via
    Immich sync); Pillow cannot decode them unless the optional
    ``pillow_heif`` package registers its opener.  Safe to call multiple
    times; no-op when ``pillow_heif`` is not installed.
    """
    try:
        import pillow_heif  # type: ignore[import-not-found, import-untyped]

        pillow_heif.register_heif_opener()
    except ImportError:
        pass


def nice_cmd(cmd: Sequence[str]) -> list[str]:
    """Wrap a command with ``nice -n 19`` if available.

    ``nice -n 19`` is the lowest possible scheduling priority.  The
    kernel gives the frontend render loop priority over any ``nice``'d
    process, so the slideshow never stutters — even when ffmpeg or
    ffprobe is running at 100 % CPU.

    Args:
        cmd: The command and arguments as a sequence (e.g. ``["ffmpeg", "-i", ...]``).

    Returns:
        A new list with ``["nice", "-n", "19"]`` prepended if ``nice``
        is available; otherwise the original command unchanged.
    """
    if _NICE_BINARY is not None:
        return ["nice", "-n", "19", *cmd]
    return list(cmd)


def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
    """Kill *proc* and every process in its session (POSIX) — SIGCONT first.

    ``cpulimit`` throttles by alternately SIGSTOPping and SIGCONTing its
    child, so a plain SIGKILL to the leader can leave a stopped ffmpeg
    orphaned.  SIGCONT wakes the whole group so SIGKILL is delivered.
    """
    if os.name != "posix":
        with contextlib.suppress(Exception):
            proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(Exception):
            proc.kill()
        return
    for sig in (signal.SIGCONT, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, sig)


def run_in_session(
    cmd: Sequence[str],
    *,
    timeout: float,
    check: bool = False,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` that kills the WHOLE process group on timeout.

    ``subprocess.run(timeout=...)`` only kills the direct child.  When the
    command is wrapped (``cpulimit -- nice ... ffmpeg``) the real worker is
    orphaned — and, because cpulimit throttles with SIGSTOP, possibly left
    frozen forever.  Starting the child in its own session
    (``start_new_session=True``) makes the wrapper the group leader, so on
    :class:`subprocess.TimeoutExpired` the entire tree is killed via
    :func:`os.killpg` (SIGCONT then SIGKILL) before the exception is
    re-raised.  With ``check=True`` a non-zero exit raises
    :class:`subprocess.CalledProcessError`, mirroring ``subprocess.run``.
    """
    args = list(cmd)
    proc = subprocess.Popen(args, start_new_session=True, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        raise
    except BaseException:
        _kill_process_group(proc)
        raise
    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout, stderr)
    return result
