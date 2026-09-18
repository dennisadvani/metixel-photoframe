# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""CPU throttling for out-of-process workers — one implementation.

Every heavy operation in Metixel runs in a short-lived child process so it can
be kept out of the render loop's way.  Two layers keep the slideshow smooth:

* ``nice -n 19`` — the lowest scheduling priority.  The kernel then always
  prefers the frontend render loop, so the slideshow keeps its frame budget even
  while a worker saturates a core.  This is a *hint*, applied whenever ``nice``
  exists, and it costs nothing.
* ``cpulimit -l N`` — a *hard* ceiling on CPU time, needed because a ``nice``
  process still consumes every idle cycle.  It sleeps the child periodically to
  enforce the cap, so it is only used where a limit is actually wanted.

Both are Unix-only.  Where they are unavailable the command is returned
unchanged, which is the correct degradation: the work still happens, it is
simply not throttled (desktop development, mostly).

This module is the single home for that wrapping so the backend's media workers
and the frontend's ambient-blur worker cannot drift apart.
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Lowest possible scheduling priority.
NICE_LEVEL = 19

#: Bounds for ``cpulimit -l``.  Below a few percent a job makes no measurable
#: progress and would hold a slide for minutes; above ten cores there is nothing
#: left to cap.
MIN_CPU_LIMIT = 5
MAX_CPU_LIMIT = 1000


def apply_nice(cmd: Sequence[str], nice_path: str | None) -> list[str]:
    """Prepend ``nice -n 19`` to *cmd* when *nice_path* is a usable binary.

    The path is a parameter rather than a lookup so callers that cache the
    answer at import time keep working through this one implementation.
    """
    if nice_path:
        return ["nice", "-n", str(NICE_LEVEL), *cmd]
    return list(cmd)


def nice_cmd(cmd: Sequence[str]) -> list[str]:
    """Wrap *cmd* with ``nice -n 19`` if ``nice`` is installed."""
    return apply_nice(cmd, shutil.which("nice"))


def throttle_cmd(cmd: Sequence[str], cpu_limit: int | None = None) -> list[str]:
    """Wrap *cmd* with ``nice``, plus a hard ``cpulimit`` ceiling on request.

    Args:
        cmd: The command and its arguments.
        cpu_limit: Percentage of ONE core to allow — ``50`` means half a core,
            ``200`` two cores.  ``None`` means "nice only", which is the right
            choice for short, latency-critical work where a hard cap would only
            make the job slower to finish.

    Returns:
        A new list.  ``cpulimit -l N -f -- nice -n 19 <cmd>`` when a limit was
        asked for and ``cpulimit`` is installed; otherwise ``nice -n 19 <cmd>``;
        otherwise *cmd* unchanged.
    """
    niced = nice_cmd(cmd)
    if cpu_limit is None:
        return niced

    cpulimit = shutil.which("cpulimit")
    if cpulimit is None:
        logger.debug("cpulimit not installed — throttling with nice only")
        return niced

    limit = max(MIN_CPU_LIMIT, min(MAX_CPU_LIMIT, cpu_limit))
    logger.debug("Throttling %s to %d%% CPU via cpulimit", cmd[0] if cmd else "worker", limit)
    # ``-f`` keeps cpulimit in the foreground, where it owns the child's
    # lifetime and its exit status reaches the caller unchanged.
    return [cpulimit, "-l", str(limit), "-f", "--", *niced]
