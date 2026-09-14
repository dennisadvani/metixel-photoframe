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
