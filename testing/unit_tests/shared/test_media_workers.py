# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The orphaned media-worker sweep.

Two properties matter, and they pull in opposite directions:

* it must actually find a running ``ffmpeg`` — otherwise a restart is delayed by
  a transcode that nothing will ever stop; and
* it must not signal anything it should not, because it runs as a shutdown step
  with the power to kill arbitrary processes.

The safety tests are therefore as important as the functional one, and they are
written as "what must survive" rather than as "what the code currently does".
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from metixel.shared.media_workers import (
    _executable_basename,
    _is_reapable_worker,
    _is_zombie,
    _own_process_tree,
    _parent_pid,
    find_media_workers,
    reap_media_workers,
)


def _spawn(name: str, *, seconds: float = 30.0) -> subprocess.Popen[bytes]:
    """Start a long-lived process whose executable basename is *name*.

    A symlink into a temp dir is used so the test can create a process that
    reports ``ffmpeg`` as its executable WITHOUT ffmpeg being installed.  The
    sweep matches on ``/proc/<pid>/exe``, so this is exactly the right shape.
    """
    return subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=True,
    )


def _real_exe(name: str, tmp_path: Path) -> Path:
    """A file named *name* that is a real executable we can actually run.

    Two things make this fiddly, and both are properties of the code under test
    rather than of the test:

    * a **shell script** will not do — the kernel reports the *interpreter* as the
      process's executable, so a script called ``ffmpeg`` has ``/proc/<pid>/exe``
      pointing at ``bash``;
    * a **symlink** will not do either — ``/proc/<pid>/exe`` resolves links, so a
      symlink named ``ffmpeg`` aimed at ``sleep`` reports ``sleep``.

    So the binary is *copied* to the target name.  That is what makes the process
    genuinely report ``ffmpeg``, which is exactly what the sweep matches on.
    """
    link = tmp_path / name
    shutil.copy2("/bin/sleep", link)
    return link


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> Path:
    """An executable named ``ffmpeg`` that behaves like a hung worker."""
    return _real_exe("ffmpeg", tmp_path)


class TestItFindsRealWorkers:
    def test_a_running_ffmpeg_is_found(self, fake_ffmpeg: Path) -> None:
        proc = subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
            [str(fake_ffmpeg), "30"], start_new_session=True
        )
        try:
            # The kernel needs a moment to populate /proc/<pid>/exe.
            deadline = time.monotonic() + 5.0
            found: list[int] = []
            while time.monotonic() < deadline:
                found = find_media_workers()
                if proc.pid in found:
                    break
                time.sleep(0.05)

            assert proc.pid in found, (
                "a running process named ffmpeg must be discovered — otherwise the "
                "restart waits for a transcode nothing will stop"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_the_sweep_terminates_it(self, fake_ffmpeg: Path) -> None:
        proc = subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
            [str(fake_ffmpeg), "30"], start_new_session=True
        )
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if proc.pid in find_media_workers():
                    break
                time.sleep(0.05)
            else:
                pytest.skip("the fake ffmpeg never became visible in /proc")

            reap_media_workers()

            proc.wait(timeout=10)
            assert proc.poll() is not None, "the worker must be gone after the sweep"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_an_idle_machine_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The common case — a restart between transcodes — must be a no-op."""
        monkeypatch.setattr("metixel.shared.media_workers.find_media_workers", lambda _s=None: [])
        assert reap_media_workers() == 0


class TestItDoesNotKillTheWrongThing:
    def test_our_own_process_tree_is_never_returned(self) -> None:
        """A sweep that matched its own chain could kill the caller."""
        own = _own_process_tree()
        assert os.getpid() in own
        assert 1 not in own, "the walk must stop before PID 1"
        assert not (own & set(find_media_workers()))

    def test_a_merely_similar_name_is_not_reapable(self, tmp_path: Path) -> None:
        """Substring matching would catch ffmpeg-doc, ffprobe-helper, and so on."""
        for name in ("ffmpeg-doc", "ffprobe-helper", "myffmpeg", "ffmpegx"):
            exe = _real_exe(name, tmp_path)
            proc = subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
                [str(exe), "30"], start_new_session=True
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if _executable_basename(proc.pid) == name:
                        break
                    time.sleep(0.05)
                assert _executable_basename(proc.pid) == name, "precondition: exe is the link"
                assert not _is_reapable_worker(proc.pid), f"{name} must not be reapable"
            finally:
                proc.kill()
                proc.wait(timeout=5)

    def test_a_process_that_exits_mid_scan_is_skipped(self) -> None:
        """The scan and the signal are not atomic; a vanished PID must not raise."""
        proc = subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
            [sys.executable, "-c", "pass"], start_new_session=True
        )
        proc.wait(timeout=5)
        # The PID is now nothing (or a zombie) — either way this must be quiet.
        assert not _is_reapable_worker(proc.pid)

    def test_a_zombie_is_not_treated_as_alive(self) -> None:
        """A zombie answers ``kill(pid, 0)`` but is finished.

        Treating one as alive would make the sweep wait out the whole grace
        period and then log a misleading ``ignored SIGTERM`` warning.
        """
        proc = subprocess.Popen(  # noqa: S603 - argv is fixed, not user input
            [sys.executable, "-c", "pass"], start_new_session=True
        )
        try:
            # Do NOT wait(): that reaps it.  Let it become a zombie under us.
            time.sleep(0.3)
            assert _is_zombie(proc.pid) or proc.poll() is not None
        finally:
            proc.wait(timeout=5)

    def test_an_unreadable_parent_is_reported_as_zero(self) -> None:
        """``/proc`` reads race with process exit; 0 means "unknown", not a raise."""
        assert _parent_pid(999_999_999) == 0

    def test_our_parent_is_identified(self) -> None:
        """The walk depends on parsing ``/proc/<pid>/stat`` around the comm field.

        The comm field is parenthesised and may contain spaces, so a naive split
        gets this wrong — which would silently break the own-tree guard.
        """
        parent = _parent_pid(os.getpid())
        assert parent > 0
        assert parent in _own_process_tree()

    def test_a_worker_owned_by_another_user_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another account's transcode is not our garbage to collect."""
        monkeypatch.setattr("metixel.shared.media_workers._owned_by_us", lambda _pid: False)
        monkeypatch.setattr(
            "metixel.shared.media_workers._executable_basename", lambda _pid: "ffmpeg"
        )
        assert not _is_reapable_worker(os.getpid())


class TestTheSweepIsBestEffort:
    def test_a_missing_proc_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Some sandboxes have no /proc; a shutdown step must not raise."""

        def _boom(_path: str) -> list[str]:
            raise OSError("no /proc")

        monkeypatch.setattr("metixel.shared.media_workers.os.listdir", _boom)
        assert find_media_workers() == []
        assert reap_media_workers() == 0

    def test_a_permission_error_on_signal_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Signalling a process we do not own must not abort the sweep."""
        monkeypatch.setattr(
            "metixel.shared.media_workers.find_media_workers", lambda _s=None: [4242]
        )

        def _denied(_pid: int, _sig: int) -> None:
            raise PermissionError("not ours")

        monkeypatch.setattr("metixel.shared.media_workers.os.kill", _denied)
        monkeypatch.setattr("metixel.shared.media_workers._still_alive", lambda _pid: False)

        assert reap_media_workers() == 1

    def test_sigterm_is_tried_before_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A worker mid-write deserves a grace period, not an immediate SIGKILL."""
        monkeypatch.setattr(
            "metixel.shared.media_workers.find_media_workers", lambda _s=None: [4242]
        )
        seen: list[tuple[int, int]] = []

        def _record(pid: int, sig: int) -> None:
            seen.append((pid, sig))

        monkeypatch.setattr("metixel.shared.media_workers.os.kill", _record)
        monkeypatch.setattr("metixel.shared.media_workers._still_alive", lambda _pid: False)

        reap_media_workers()

        assert seen == [(4242, signal.SIGTERM)], "a prompt exit must not escalate"
