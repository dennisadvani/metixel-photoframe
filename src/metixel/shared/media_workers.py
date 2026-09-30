# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

"""Reap orphaned media workers so a restart is not delayed by them.

The backend spawns ``ffmpeg``/``ffprobe`` for optimisation and thumbnail work,
and the frontend spawns a Pillow-based blur worker for the ambient backdrop.
Every one of those is wrapped in ``nice`` and/or ``cpulimit`` (see
:func:`metixel.backend.processing.utils.nice_cmd` and
:mod:`metixel.shared.throttle`), which means the process that is actually doing
the work is a *grandchild* of the one we hold a handle to.

That matters on shutdown.  A transcode is measured in minutes: ``ffmpeg`` runs
until it is told to stop, and nothing about the parent exiting kills it.  When
systemd restarts the service the old worker survives as an orphan, keeps its CPU
on the SD card's very limited I/O budget, and — because a new process is
starting at the same time — the restart appears to hang.  The frame then takes
noticeably longer to come back than the ~2 s the backend teardown itself costs.

So the sweep is deliberately a **lookup by name**, not a bookkeeping exercise.
Tracking every ``Popen`` would mean threading a registry through the image, video,
thumbnail and blur paths and keeping it correct through ``cpulimit`` re-exec —
and it would still miss a worker started before the current process began, which
is exactly the orphan left by the *previous* restart.  Matching on the executable
name catches all of them, including ones this process never spawned.

Safety is the whole design, so it is worth stating what is NOT done:

* only processes whose *executable* is ``ffmpeg``/``ffprobe`` are signalled —
  never ``nice``, ``cpulimit``, or anything matched by a substring, so a
  similarly-named binary is not caught;
* processes owned by other users are skipped, so this cannot reach into another
  account's work;
* our own PID and its ancestors are skipped, so the sweep can never kill the
  process running it;
* ``SIGTERM`` is tried first with a short grace period, then ``SIGKILL`` — a
  worker mid-write to the cache is given the chance to exit cleanly, and its
  partial output is cleaned up by the existing ``_cleanup_partial_transcodes``
  pass on the next startup.

The frontend calls this too, and that is not redundant: a restart of the frontend
alone (or an OTA that only bounces cage) would otherwise leave the backend's
orphans running through the new process's first slides.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time

logger = logging.getLogger(__name__)

#: Executable basenames the sweep is allowed to signal.
#:
#: Matched against ``/proc/<pid>/exe``'s target, so this is the *program*, not a
#: command line.  ``cpulimit`` re-execs its child, so the worker's ``exe`` is the
#: real ``ffmpeg`` — which is precisely why a name match works here and a
#: parent-PID walk would not.
_REAPABLE_EXECUTABLES = frozenset({"ffmpeg", "ffprobe"})

#: How long to let a worker exit on ``SIGTERM`` before escalating.
#:
#: Short on purpose: this runs during a restart, and a transcode that has not
#: honoured ``SIGTERM`` within this window is one that is going to be killed
#: anyway.  ``ffmpeg`` exits promptly on ``SIGTERM``.
_GRACE_SECONDS = 2.0


def find_media_workers(skip: set[int] | None = None) -> list[int]:
    """Return the PIDs of running ``ffmpeg``/``ffprobe`` processes we may signal.

    Reads ``/proc`` directly rather than shelling out to ``pgrep``/``ps``:
    ``pgrep`` is not guaranteed present on a minimal Pi image, ``ps`` output
    differs between busybox and procps, and a shell round-trip adds a fork to a
    path that runs during shutdown.  ``/proc`` is always there on Linux.

    Best-effort throughout — a process that exits mid-scan, or whose ``exe``
    link cannot be read (a kernel thread, or another user's process), is skipped
    rather than raising.  This runs while the machine is going down; a failure
    here must never be the thing that stops the shutdown.

    Args:
        skip: PIDs not to return, in addition to this process and its ancestors.

    Returns:
        A list of PIDs, possibly empty.  Never raises.
    """
    protected = set(skip or ())
    protected.update(_own_process_tree())

    try:
        entries = os.listdir("/proc")
    except OSError:
        logger.debug("Cannot list /proc — skipping the media-worker sweep")
        return []

    found: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in protected:
            continue
        if _is_reapable_worker(pid):
            found.append(pid)
    return found


def reap_media_workers(skip: set[int] | None = None) -> int:
    """Terminate every orphaned media worker.  Returns the number signalled.

    Called from both the backend and the frontend shutdown paths.  Idempotent and
    safe to call when nothing is running — which is the common case, since most
    restarts happen between transcodes.
    """
    pids = find_media_workers(skip)
    if not pids:
        return 0

    logger.info(
        "Reaping %d orphaned media worker(s) so the restart is not held up: %s",
        len(pids),
        ", ".join(str(p) for p in pids),
    )

    for pid in pids:
        _signal(pid, signal.SIGTERM)

    # One shared grace period for the whole set rather than per process: they are
    # all doing the same kind of work and start shutting down together, so a
    # per-PID wait would multiply the delay by the worker count.
    deadline = time.monotonic() + _GRACE_SECONDS
    while time.monotonic() < deadline:
        if not any(_still_alive(pid) for pid in pids):
            break
        time.sleep(0.05)

    survivors = [pid for pid in pids if _still_alive(pid)]
    for pid in survivors:
        logger.warning("Media worker %d ignored SIGTERM — sending SIGKILL", pid)
        _signal(pid, signal.SIGKILL)

    return len(pids)


# -- Internals --------------------------------------------------------------


def _own_process_tree() -> set[int]:
    """This process and every ancestor, so the sweep cannot kill its caller.

    Walks ``/proc/<pid>/stat``'s ``ppid`` field up to PID 1.  The guard is not
    theoretical: a sweep that matches by executable name would otherwise be free
    to signal a process in its own chain if anything along it were ever named
    ``ffmpeg`` (a wrapper script, say).
    """
    chain: set[int] = set()
    pid = os.getpid()
    while pid > 1 and pid not in chain:
        chain.add(pid)
        pid = _parent_pid(pid)
    return chain


def _parent_pid(pid: int) -> int:
    """Return *pid*'s parent, or 0 when it cannot be determined.

    ``/proc/<pid>/stat``'s second field is the command *in parentheses*, and it
    may itself contain spaces and parentheses, so the fields cannot simply be
    split on whitespace.  Splitting after the LAST ``)`` is the standard way to
    get them reliably.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return 0
    try:
        tail = data.rsplit(b")", 1)[1].split()
        # Fields after the command name are: state ppid pgrp session ...
        return int(tail[1])
    except (IndexError, ValueError):
        return 0


def _is_reapable_worker(pid: int) -> bool:
    """Whether *pid* is an ``ffmpeg``/``ffprobe`` process belonging to this user."""
    if not _owned_by_us(pid):
        return False
    return _executable_basename(pid) in _REAPABLE_EXECUTABLES


def _owned_by_us(pid: int) -> bool:
    """Whether *pid* has our effective UID.

    Deliberately strict.  Without this the sweep would happily signal another
    user's transcode — and on a shared box that is someone else's work, not
    garbage from our restart.  Compares the *effective* UID so a setuid helper is
    judged by what it is running as.
    """
    try:
        stat = os.stat(f"/proc/{pid}")
    except OSError:
        return False
    with contextlib.suppress(AttributeError):
        return stat.st_uid == os.geteuid()
    return stat.st_uid == os.getuid()


def _executable_basename(pid: int) -> str:
    """The basename of *pid*'s executable, or ``""`` when it cannot be read.

    ``os.readlink`` on ``/proc/<pid>/exe`` gives the real binary even for a
    process that was re-exec'd by ``cpulimit``, and it is empty for a zombie —
    which is the correct outcome, since a zombie has already exited.
    """
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return ""


def _signal(pid: int, sig: int) -> None:
    """Send *sig* to *pid*, ignoring the races that make this best-effort.

    A process that exited between the scan and the signal raises
    ``ProcessLookupError``; one we may not signal raises ``PermissionError``.
    Neither is an error worth propagating out of a shutdown path.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


def _still_alive(pid: int) -> bool:
    """Whether *pid* still exists and has not been reaped.

    ``os.kill(pid, 0)`` is the POSIX existence probe.  A zombie — exited but not
    yet reaped by its parent — still answers, so a ``Z`` state is treated as gone;
    otherwise the sweep would wait out the full grace period for a process that
    has already stopped doing work, and then log a misleading ``SIGKILL`` warning.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Whether *pid* has exited and is waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return True  # gone, which for our purposes is the same thing
    try:
        return data.rsplit(b")", 1)[1].split()[0] == b"Z"
    except (IndexError, ValueError):
        return False
