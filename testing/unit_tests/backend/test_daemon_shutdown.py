# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""BackendDaemon SIGTERM/SIGINT handling and idempotent shutdown.

systemd stops the service with SIGTERM.  Without a handler Python dies on
the spot and the journal flush / UpdateManager shutdown / IPC close never
run.  ``run()`` now installs a handler that calls ``shutdown()`` and then
raises ``KeyboardInterrupt`` (which werkzeug's ``serve_forever`` swallows so
``run()`` can finish its normal teardown).
"""

from __future__ import annotations

import signal
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


class FakeIPC:
    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False

    def send(self, msg) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch):
    import metixel.backend.daemon as daemon_mod
    from metixel.shared.config import Config

    config_path = tmp_path / "config.json"
    Config().save(config_path)
    monkeypatch.setattr(daemon_mod, "IPCClient", FakeIPC)
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return daemon_mod.BackendDaemon(config_path)


class TestSignalHandlers:
    def test_run_installs_handlers_before_web_server(self, daemon, monkeypatch) -> None:
        installed: dict[int, object] = {}

        def fake_signal(sig, handler):
            installed[sig] = handler

        monkeypatch.setattr("metixel.backend.daemon.signal.signal", fake_signal)
        daemon._install_signal_handlers()

        assert signal.SIGTERM in installed
        assert signal.SIGINT in installed
        assert installed[signal.SIGTERM] is installed[signal.SIGINT]

    def test_handler_calls_shutdown_then_raises_keyboard_interrupt(
        self, daemon, monkeypatch
    ) -> None:
        installed: dict[int, Any] = {}
        monkeypatch.setattr(
            "metixel.backend.daemon.signal.signal", lambda sig, h: installed.__setitem__(sig, h)
        )
        daemon._install_signal_handlers()
        flush = mock.Mock()
        monkeypatch.setattr(daemon._state, "flush_journal", flush)
        update_mgr = mock.Mock()
        daemon._update_mgr = update_mgr

        with pytest.raises(KeyboardInterrupt):
            installed[signal.SIGTERM](signal.SIGTERM, None)

        flush.assert_called_once()
        update_mgr.shutdown.assert_called_once()
        assert daemon._running is False

    def test_install_is_noop_outside_main_thread(self, daemon, monkeypatch) -> None:
        def boom(sig, handler):
            raise ValueError("signal only works in main thread")

        monkeypatch.setattr("metixel.backend.daemon.signal.signal", boom)
        daemon._install_signal_handlers()  # must not raise


class TestShutdownIdempotent:
    def test_second_shutdown_is_noop(self, daemon, monkeypatch) -> None:
        flush = mock.Mock()
        monkeypatch.setattr(daemon._state, "flush_journal", flush)
        update_mgr = mock.Mock()
        daemon._update_mgr = update_mgr
        opt_queue = mock.Mock()
        daemon._opt_queue = opt_queue

        daemon.shutdown()
        daemon.shutdown()

        flush.assert_called_once()
        update_mgr.shutdown.assert_called_once()
        opt_queue.stop.assert_called_once()

    def test_shutdown_survives_failing_service(self, daemon, monkeypatch) -> None:
        daemon._opt_queue = mock.Mock(stop=mock.Mock(side_effect=RuntimeError("boom")))
        flush = mock.Mock()
        monkeypatch.setattr(daemon._state, "flush_journal", flush)
        daemon.shutdown()
        flush.assert_called_once()


class TestShutdownIsPrompt:
    """Shutdown must not wait out a worker's sleep.

    Regression guard for a 10-20 s backend outage on every
    ``systemctl restart``.  The polling loops used a bare ``time.sleep(N)``
    and only re-checked ``self._running`` afterwards, so a SIGTERM landing
    early in a 30 s sleep (the display scheduler) was not noticed until the
    sleep expired.  Every such thread was then joined with a 5 s cap
    *sequentially*, so three stalled threads cost 3 x 5 s of dead time.

    Two independent fixes are pinned here:
      1. ``_sleep()`` waits on an Event, so shutdown wakes it immediately.
      2. ``_join_threads()`` joins against ONE shared deadline, so the total
         wait is bounded by ``_JOIN_TIMEOUT_S`` however many threads are slow.
    """

    def test_shutdown_sets_stop_event_so_sleep_wakes_early(self, daemon) -> None:
        """A thread parked in ``_sleep`` must be released by ``shutdown()``."""
        assert not daemon._stop_event.is_set()

        # Park a thread for far longer than the test will wait.  Before the
        # fix this would block for the full 300 s because ``_sleep`` could not
        # be interrupted.
        released = threading.Event()

        def waiter() -> None:
            daemon._running = True
            daemon._sleep(300)
            released.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        # Give the thread a moment to enter the wait.
        time.sleep(0.05)

        daemon.shutdown()

        assert daemon._stop_event.is_set()
        assert released.wait(timeout=2.0), (
            "shutdown() did not wake a thread parked in _sleep() — it will "
            "sleep out its full timeout and stall the service restart"
        )
        t.join(timeout=1.0)

    def test_sleep_reports_stopping_so_loops_can_return(self, daemon) -> None:
        """``_sleep`` returns False once stopping, True while running."""
        daemon._running = True
        assert daemon._sleep(0) is True, "a running daemon should report True"

        daemon.shutdown()
        assert daemon._sleep(0) is False, (
            "_sleep must report False after shutdown so loops return instead "
            "of running another iteration"
        )

    def test_sleep_honours_its_timeout_when_running(self, daemon) -> None:
        """Without a shutdown it still waits roughly the requested time."""
        daemon._running = True
        start = time.monotonic()
        assert daemon._sleep(0.2) is True
        assert time.monotonic() - start >= 0.15

    def test_join_threads_bounds_total_wait_across_many_slow_threads(
        self, daemon, monkeypatch
    ) -> None:
        """Joining is concurrent, not sequential.

        Three threads that each ignore their stop signal must not cost
        3 x the per-thread cap.  Constructed so the OLD sequential
        implementation would clearly fail: with three 1 s-slow threads and a
        0.5 s overall budget, sequential joining needs >=3 s.
        """
        monkeypatch.setattr("metixel.backend.daemon._JOIN_TIMEOUT_S", 0.5)

        for _ in range(3):
            t = threading.Thread(target=time.sleep, args=(3.0,), daemon=True)
            t.start()
            daemon._threads.append(t)

        start = time.monotonic()
        daemon._join_threads()
        elapsed = time.monotonic() - start

        assert elapsed < 1.5, (
            f"_join_threads took {elapsed:.2f}s for 3 slow threads with a 0.5s "
            "budget — it is joining sequentially (per-thread timeout), which "
            "multiplies the service-restart delay by the thread count"
        )

    def test_join_threads_returns_immediately_when_nothing_is_running(self, daemon) -> None:
        start = time.monotonic()
        daemon._join_threads()
        assert time.monotonic() - start < 0.2
