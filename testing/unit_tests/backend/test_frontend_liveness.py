# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the frontend liveness tracker.

This is the signal that lets the OTA health gate FAIL.  The property under
test is the one a naive freshness check gets wrong: ``metixel-cage.service``
is ``Restart=always`` with ``RestartSec=5``, so a crash-looping frontend
rewrites its heartbeat every few seconds and the file's mtime never goes
stale.  Liveness therefore means "the SAME process is still beating", not "a
file is fresh" — so these tests drive the tracker through a crash loop and
assert it never reports healthy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from metixel.backend.frontend_liveness import FrontendLiveness


def _write_heartbeat(
    path: Path,
    *,
    pid: int,
    boot_id: str = "boot-A",
    age: float = 0.0,
    uptime: float = 1.0,
) -> None:
    """Write a heartbeat file, optionally back-dating its mtime by *age*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "boot_id": boot_id, "uptime": uptime, "queue_len": 3}),
        encoding="utf-8",
    )
    if age:
        mtime = path.stat().st_mtime - age
        os.utime(path, (mtime, mtime))


@pytest.fixture
def heartbeat(tmp_path: Path) -> Path:
    return tmp_path / "frontend_heartbeat.json"


@pytest.fixture
def tracker(heartbeat: Path) -> FrontendLiveness:
    # Small windows so the tests are fast and the crash-loop case is explicit.
    return FrontendLiveness(heartbeat, stale_after=5.0, stable_after=2.0)


class TestMissingHeartbeat:
    def test_absent_file_is_not_alive(self, tracker: FrontendLiveness) -> None:
        """A frontend that has never run must not pass the gate."""
        snap = tracker.snapshot()

        assert snap["alive"] is False
        assert snap["state"] == "missing"

    def test_file_without_valid_pid_is_not_alive(self, heartbeat: Path, tracker) -> None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(json.dumps({"boot_id": "boot-A"}), encoding="utf-8")

        assert tracker.snapshot()["alive"] is False

    def test_malformed_file_is_not_alive(self, heartbeat: Path, tracker) -> None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text("{not json", encoding="utf-8")

        assert tracker.snapshot()["alive"] is False


class TestStaleness:
    def test_fresh_heartbeat_is_not_yet_alive(self, heartbeat: Path, tracker) -> None:
        """First sight of a process is 'starting', not 'alive'.

        The tracker cannot know yet whether that process will survive, so it
        must not vouch for it on the strength of a single beat.
        """
        _write_heartbeat(heartbeat, pid=100)

        snap = tracker.snapshot()

        assert snap["alive"] is False
        assert snap["state"] == "starting"

    def test_stale_heartbeat_is_not_alive(self, heartbeat: Path, tracker) -> None:
        """No beat inside the stale window means the frontend exited or hung."""
        _write_heartbeat(heartbeat, pid=100, age=60.0)

        snap = tracker.snapshot()

        assert snap["alive"] is False
        assert snap["state"] == "stale"

    def test_heartbeat_from_an_earlier_boot_is_not_alive(
        self, heartbeat: Path, tracker, monkeypatch
    ) -> None:
        """A leftover heartbeat from before a reboot must not read as live."""
        import metixel.shared.platform as platform

        _write_heartbeat(heartbeat, pid=100, boot_id="old-boot")
        monkeypatch.setattr(platform, "boot_identity", lambda: "new-boot")

        snap = tracker.snapshot()

        assert snap["alive"] is False
        assert snap["state"] == "missing"


class TestAlive:
    def test_alive_once_identity_is_stable(self, heartbeat: Path, tracker, monkeypatch) -> None:
        """The same process, beating across the stability window, is alive."""
        clock = [1000.0]
        monkeypatch.setattr("metixel.backend.frontend_liveness.time.monotonic", lambda: clock[0])

        _write_heartbeat(heartbeat, pid=100)
        assert tracker.snapshot()["alive"] is False  # first sight → starting

        clock[0] += 3.0  # past stable_after (2.0)
        _write_heartbeat(heartbeat, pid=100)
        snap = tracker.snapshot()

        assert snap["alive"] is True
        assert snap["state"] == "alive"
        assert snap["pid"] == 100


class TestCrashLoop:
    """The case the original gate could not see at all."""

    def test_restarting_frontend_is_never_alive(
        self, heartbeat: Path, tracker, monkeypatch
    ) -> None:
        """A crash loop keeps the file fresh but must never be declared healthy.

        Mirrors metixel-cage.service: Restart=always, RestartSec=5 — a new pid
        every ~6s, so the file is always fresh and its mtime never goes stale.
        """
        clock = [1000.0]
        monkeypatch.setattr("metixel.backend.frontend_liveness.time.monotonic", lambda: clock[0])

        states = []
        for pid in (100, 101, 102, 103, 104):
            _write_heartbeat(heartbeat, pid=pid)
            states.append(tracker.snapshot())
            clock[0] += 6.0  # RestartSec=5 + startup

        assert all(snap["alive"] is False for snap in states)
        # After the first identity the state must name the churn explicitly.
        assert states[0]["state"] == "starting"
        assert all(snap["state"] == "churning" for snap in states[1:])
        assert "restarting" in states[1]["reason"]

    def test_oscillating_frontend_is_never_alive(
        self, heartbeat: Path, tracker, monkeypatch
    ) -> None:
        """A frontend crashing on a ~10s period must NEVER be reported alive.

        This is the case a single stability window misses, and the reason
        ``CHURN_PENALTY`` exists.  A ~10s crash period comfortably exceeds the
        plain ``stable_after`` (2s here, 8s in production), so after each
        restart the process is observed beating for LONGER than the plain
        window — and because the OTA gate succeeds on the FIRST 200 it sees, an
        oscillating frontend would be declared healthy and the release blessed.

        With the penalty the post-restart bar outlasts any plausible crash
        period, so the oscillation can never be certified.
        """
        clock = [1000.0]
        monkeypatch.setattr("metixel.backend.frontend_liveness.time.monotonic", lambda: clock[0])
        # stable_after=2.0 * churn_penalty=3.0  =>  post-churn bar is 6s.
        # Each process below lives 5s then is replaced: that clears the plain 2s
        # window with room to spare, but never reaches 6s.
        _write_heartbeat(heartbeat, pid=100)
        tracker.snapshot()  # first sighting establishes the identity

        # Observe a genuine restart so the penalty latches.
        clock[0] += 1.0
        _write_heartbeat(heartbeat, pid=101)
        assert tracker.snapshot()["alive"] is False  # churning

        for pid in range(101, 109):
            # The same process observed for 5s: past the plain 2s window...
            clock[0] += 5.0
            _write_heartbeat(heartbeat, pid=pid)
            snap = tracker.snapshot()
            # ...but NOT past the 6s post-churn bar, so it must not be certified.
            assert snap["alive"] is False, f"pid {pid} wrongly reported alive: {snap}"

            # Then it crashes and is replaced, repeating the cycle.
            clock[0] += 1.0
            _write_heartbeat(heartbeat, pid=pid + 1)
            assert tracker.snapshot()["state"] == "churning"

    def test_long_lived_process_survives_a_single_restart(
        self, heartbeat: Path, tracker, monkeypatch
    ) -> None:
        """One genuine restart must not be fatal — only sustained churn is.

        Otherwise a slow-starting or deliberately restarted frontend could
        never recover its health verdict.  Note the longer wait after the
        restart: an observed identity flip multiplies the required stability
        window (``CHURN_PENALTY``), which is what stops an OSCILLATING frontend
        from being reported alive between crashes.
        """
        clock = [1000.0]
        monkeypatch.setattr("metixel.backend.frontend_liveness.time.monotonic", lambda: clock[0])

        _write_heartbeat(heartbeat, pid=100)
        tracker.snapshot()
        clock[0] += 10.0
        _write_heartbeat(heartbeat, pid=100)
        assert tracker.snapshot()["alive"] is True

        # Restart once: identity flips, so it is unhealthy again...
        clock[0] += 1.0
        _write_heartbeat(heartbeat, pid=200)
        assert tracker.snapshot()["alive"] is False

        # ...and the post-restart bar is higher than the plain stable window,
        # so a short run is still not enough.
        clock[0] += 3.0
        _write_heartbeat(heartbeat, pid=200)
        assert tracker.snapshot()["alive"] is False

        # A genuinely stable run afterwards recovers liveness.
        clock[0] += 10.0
        _write_heartbeat(heartbeat, pid=200)
        assert tracker.snapshot()["alive"] is True
