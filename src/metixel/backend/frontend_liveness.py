# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Frontend liveness — did the renderer actually come up?

The OTA health-check (``scripts/update.sh``) used to be a bare ``curl`` of
``/api/health``, which only ever proved the *backend* was listening.  A
release whose frontend crash-looped therefore passed the gate, was declared a
success, and left the frame on a black screen with no rollback.

This module supplies the missing half of the signal.  The render loop
publishes a heartbeat (``frontend_heartbeat.json`` in ``run_dir()``) and this
tracker turns that file into an honest ``alive`` / ``stale`` / ``churning``
verdict for :func:`metixel.backend.web.routes.health.health_check`.

Why a plain mtime check is not enough
-------------------------------------
``metixel-cage.service`` is ``Restart=always`` with ``RestartSec=5``.  A
frontend crash-looping at startup therefore rewrites the heartbeat file every
few seconds and its mtime *never* goes stale — a "is the file fresh?" check
would pass forever on a black screen.  The usable signal is not "a file is
fresh" but "the SAME process is still beating", so the tracker watches the
``(pid, boot_id)`` pair published in the file and requires that identity to
persist for :data:`FrontendLiveness.STABLE_AFTER` seconds before declaring the
frontend alive.  A 6-second crash loop can never accumulate that much
consecutive identity, so it is correctly reported as dead.

Once a crash loop has been OBSERVED (an identity flip, not merely a first
sighting), the stability bar is multiplied by :data:`FrontendLiveness.CHURN_PENALTY`
and stays raised for the life of the tracker.  Without that second tier a
renderer crashing on a ~10s period would be reported alive for 8s out of every
10 — and because the OTA gate succeeds on the first 200 it sees, an oscillating
frontend would sail through the gate.

Deliberately NOT a generic "stale file" utility: the identity-stability rule
is the whole point of this module, and a caller wanting a plain freshness
check can read the file directly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from metixel.shared.io import read_json
from metixel.shared.paths import FRONTEND_HEARTBEAT_FILE

logger = logging.getLogger(__name__)

#: Heartbeat filename inside ``run_dir()``.  Written by the frontend render
#: loop, read by the health endpoint.  Re-exported from
#: :mod:`metixel.shared.paths` so the writer and reader share one contract.
HEARTBEAT_FILENAME = FRONTEND_HEARTBEAT_FILE


def _known(boot_id: str) -> bool:
    """True when *boot_id* is a real identifier rather than the "unknown" stub.

    ``boot_identity()`` returns ``"unknown"`` where ``/proc`` is unavailable
    (desktop development).  Comparisons against that stub must be skipped
    entirely rather than treated as a mismatch.
    """
    return bool(boot_id) and boot_id != "unknown"


class FrontendLiveness:
    """Derives frontend liveness from the heartbeat file's identity stability.

    The tracker is cheap to poll (one small file read) and holds only two
    scalars, so the heartbeat's own interval — not this class — controls how
    often the filesystem is touched.
    """

    #: No heartbeat within this many seconds means the frontend is gone.
    #: Must comfortably exceed the frontend's write interval so a busy frame
    #: (e.g. one mid-transcode) is not mistaken for a dead one.
    STALE_AFTER = 30.0

    #: How long the published ``(pid, boot_id)`` must persist before the
    #: frontend is considered genuinely up rather than merely restarting.
    #: This is what defeats the ``Restart=always`` / ``RestartSec=5`` loop: a
    #: crash-looping renderer never accumulates this much consecutive
    #: identity, however fresh its heartbeat file looks.
    STABLE_AFTER = 8.0

    #: Multiplier applied to :data:`STABLE_AFTER` once an identity FLIP has
    #: been observed.  A flip is the crash-loop signature, so after one we stop
    #: trusting the heartbeat's own ``uptime`` and demand this much *observed*
    #: stability instead.  Without it, a renderer that crashes every ~10s would
    #: be reported alive for 8s out of every 10 — and since the OTA gate
    #: succeeds on the first 200 it sees, an oscillating frontend would pass.
    CHURN_PENALTY = 3.0

    def __init__(
        self,
        heartbeat_path: Path | str,
        *,
        stale_after: float = STALE_AFTER,
        stable_after: float = STABLE_AFTER,
        churn_penalty: float = CHURN_PENALTY,
    ) -> None:
        self._path = Path(heartbeat_path)
        self._stale_after = stale_after
        self._stable_after = stable_after
        self._churn_penalty = churn_penalty
        # The identity currently being tracked and when it was first seen
        # (monotonic — immune to clock steps and to tmpfs being cleared).
        self._identity: str | None = None
        self._identity_since: float = 0.0
        # Latched once an identity flip is seen; cleared only after the longer
        # post-churn stability period has been earned.
        self._churned: bool = False
        self._last_boot_id: str = ""
        self._last_pid: int = 0

    # -- Public -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return the current liveness verdict and refresh internal state.

        Call once per health poll.  The returned dict is JSON-serialisable and
        safe to publish on ``/api/health``:

        ``alive``      - the same frontend process has been beating long enough
        ``state``      - ``"alive" | "starting" | "stale" | "missing" | "churning"``
        ``reason``     - human-readable explanation for the OTA log
        ``age_seconds``- how long ago the heartbeat was written (``None`` if absent)
        ``pid``        - frontend process id (``None`` if unknown)
        ``boot_id``    - the CURRENT boot identifier the heartbeat is checked
                         against (``None`` if unknown) — not the one stored in
                         the heartbeat file
        """
        now = time.monotonic()
        data = self._read_heartbeat()
        if data is None:
            self._last_pid = 0
            return self._verdict("missing", False, "no heartbeat file (frontend has never run)")

        raw_pid = data.get("pid")
        if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or raw_pid <= 0:
            self._last_pid = 0
            return self._verdict("missing", False, "heartbeat file has no valid pid")
        pid = raw_pid
        self._last_pid = pid

        boot_id = str(data.get("boot_id", ""))
        # Reject a heartbeat left over from an EARLIER boot.  Only when both
        # sides are genuinely known: boot_identity() reports "unknown" on
        # systems without /proc (desktop dev), and treating that as a mismatch
        # would reject a live frontend — a false negative, which is the one
        # failure direction a safety net must never have.  The pid alone still
        # carries crash-loop detection in that case.
        if _known(boot_id) and _known(self._last_boot_id) and boot_id != self._last_boot_id:
            return self._verdict(
                "missing",
                False,
                f"heartbeat is from an earlier boot ({boot_id} != {self._last_boot_id})",
            )

        age = self._age_seconds()
        if age is None or age > self._stale_after:
            return self._verdict(
                "stale",
                False,
                (
                    f"last heartbeat {age:.0f}s ago (frontend exited or hung)"
                    if age is not None
                    else "heartbeat timestamp unavailable"
                ),
            )

        identity = f"{pid}@{boot_id or 'unknown'}"
        previous = self._identity
        if identity != self._identity:
            self._identity = identity
            self._identity_since = now
            # An identity FLIP (not the first sighting) is the crash-loop
            # signature, so latch it: the stability bar is raised for the rest
            # of this tracker's life rather than reset per process.
            if previous is not None:
                self._churned = True

        if previous is not None and identity != previous:
            return self._verdict(
                "churning",
                False,
                f"frontend identity changed ({previous} → {identity}) "
                f"— it is restarting, not running",
            )

        # ── Stability ──────────────────────────────────────────────────
        # How long THIS identity has been observed beating.  The window starts
        # at our first sighting, and that is the correct basis: during an OTA
        # both metixel-backend and metixel-cage are restarted together, so the
        # frontend really is as young as our observation of it — and the gate
        # polls for up to 60s (HEALTH_TIMEOUT), far longer than this window.
        #
        # We deliberately do NOT trust the heartbeat's self-reported `uptime` to
        # shortcut this.  It would only save ~8s inside a 60s budget, while
        # requiring us to accept a self-attested number from the very file we
        # are trying to validate.  Observed stability is the honest signal.
        #
        # After an observed flip the requirement is multiplied (CHURN_PENALTY).
        # A renderer crashing on a ~10s period would otherwise satisfy the plain
        # 8s window and be reported alive between crashes — and because the OTA
        # gate succeeds on the FIRST 200 it sees, an oscillating frontend would
        # sail through the gate.  The longer post-churn bar outlasts any
        # plausible crash period, so the oscillation can never be certified.
        required = self._stable_after * (self._churn_penalty if self._churned else 1.0)
        stable_for = now - self._identity_since
        if stable_for < required:
            return self._verdict(
                "starting",
                False,
                f"frontend pid {pid} only stable for {stable_for:.0f}s "
                f"(needs {required:.0f}s"
                + (" after a restart" if self._churned else " — a crash loop never gets there")
                + ")",
            )

        return self._verdict(
            "alive",
            True,
            f"frontend pid {pid} stable for {stable_for:.0f}s",
        )

    # -- Internals ----------------------------------------------------------

    def _read_heartbeat(self) -> dict[str, Any] | None:
        """Read the heartbeat file, or ``None`` if absent/malformed.

        Refreshes the expected boot id on each poll so the check stays correct
        across a reboot.
        """
        from metixel.shared.platform import boot_identity

        self._last_boot_id = boot_identity()
        data = read_json(self._path, None)
        if not isinstance(data, dict):
            return None
        return data

    def _age_seconds(self) -> float | None:
        """Seconds since the heartbeat was written, or ``None`` if unknown."""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return None
        # st_mtime is a wall-clock value, so compare it against time.time() —
        # NOT against the monotonic clock used for the identity timing.
        return max(0.0, time.time() - mtime)

    def _verdict(
        self,
        state: str,
        alive: bool,
        reason: str,
    ) -> dict[str, Any]:
        age = self._age_seconds()
        verdict: dict[str, Any] = {
            "alive": alive,
            "state": state,
            "reason": reason,
            "age_seconds": round(age, 1) if age is not None else None,
            "pid": self._last_pid or None,
            "boot_id": self._last_boot_id or None,
        }
        if state != "alive":
            logger.debug("Frontend liveness: %s — %s", state, reason)
        return verdict
