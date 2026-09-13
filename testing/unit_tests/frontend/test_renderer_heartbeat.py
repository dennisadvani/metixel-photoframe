# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the render loop's liveness heartbeat.

The heartbeat is what makes the OTA gate able to fail.  It is a periodic
write from a render loop, which the project's "minimise SD-card writes" rule
normally forbids — so these tests also pin that it is bounded (one write per
interval, not per frame) and that it lands in the runtime directory that is
tmpfs on the Pi rather than the persistent data tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metixel.frontend.renderer import FrontendRenderer


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    from metixel.shared.config import Config

    path = tmp_path / "config.json"
    Config().save(path)
    return path


@pytest.fixture
def renderer(config_path: Path, tmp_path: Path, monkeypatch) -> FrontendRenderer:
    """A renderer with no backend — enough to exercise the heartbeat writer."""
    monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path / "run"))
    return FrontendRenderer(config_path=config_path)


class TestHeartbeat:
    def test_writes_pid_and_boot_id(self, renderer: FrontendRenderer, tmp_path: Path) -> None:
        renderer._write_heartbeat()

        path = tmp_path / "run" / "frontend_heartbeat.json"
        assert path.is_file(), "heartbeat must be written into run_dir()"

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pid"] > 0
        # boot_id may be the "unknown" stub off-Linux, but the key must exist:
        # the tracker uses it (with the pid) to spot a restarting frontend.
        assert "boot_id" in data
        assert data["queue_len"] == 0

    def test_lives_in_run_dir_not_the_data_tree(
        self, renderer: FrontendRenderer, tmp_path: Path
    ) -> None:
        """It must be transient telemetry, never a persistent data file.

        Writing this on a timer is only acceptable because run_dir() is tmpfs
        on the Pi (RAM, not flash).  A move into data/ would turn a bounded
        heartbeat into continuous SD-card wear.
        """
        renderer._write_heartbeat()

        assert (tmp_path / "run" / "frontend_heartbeat.json").is_file()
        assert not (tmp_path / "data").exists()

    def test_write_always_writes(self, renderer: FrontendRenderer, tmp_path: Path) -> None:
        """``_write_heartbeat`` is unconditional; the THREAD does the throttling.

        The throttle used to live here as an early return.  It moved into the
        heartbeat thread's ``wait(HEARTBEAT_INTERVAL)`` loop instead, because the
        rate that matters is how often the loop *runs*, and because a conditional
        write here would make an explicit call (like the one on startup) silently
        do nothing.

        The property that must still hold is that the beat is NOT per frame:
        nothing on the render path calls this at all.
        """
        renderer._write_heartbeat()
        path = tmp_path / "run" / "frontend_heartbeat.json"
        first = json.loads(path.read_text(encoding="utf-8"))

        renderer._write_heartbeat()
        second = json.loads(path.read_text(encoding="utf-8"))

        # Same process identity — the tracker keys liveness off (pid, boot_id).
        assert first["pid"] == second["pid"]
        assert first["boot_id"] == second["boot_id"]

    def test_heartbeat_is_not_called_from_the_render_path(self) -> None:
        """The tick must never write the heartbeat — that would be per-frame IO.

        This is the guard that keeps the heartbeat off the render path, which is
        what makes a threaded writer worthwhile rather than a throttled inline one.
        """
        import ast
        import inspect
        import textwrap

        from metixel.frontend import renderer as renderer_mod

        # getsource() returns the method still indented by its class body, which
        # ast.parse rejects; dedent to make it a module-level statement.
        source = textwrap.dedent(inspect.getsource(renderer_mod.FrontendRenderer._tick))
        tree = ast.parse(source)
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_write_heartbeat" not in calls, (
            "_tick must not write the heartbeat; it now runs on its own thread so "
            "that a Qt event-loop stall cannot make a live frontend look dead"
        )

    def test_heartbeat_runs_on_its_own_thread(
        self, renderer: FrontendRenderer, tmp_path: Path
    ) -> None:
        """Deliberate: a GUI-thread stall must not stop the liveness signal.

        On the Qt backend the tick runs on the GUI thread, where GL setup, an mpv
        load or a large decode can block it for seconds.  A heartbeat driven from
        there would stop during exactly the stall it exists to report, and the OTA
        gate would roll back a healthy release.
        """
        renderer._start_heartbeat()
        try:
            assert renderer._heartbeat_thread is not None
            assert renderer._heartbeat_thread.daemon
            assert renderer._heartbeat_thread.name == "frontend-heartbeat"
            # Starting immediately publishes one beat without waiting an interval.
            assert (tmp_path / "run" / "frontend_heartbeat.json").is_file()
        finally:
            renderer._stop_heartbeat()

    def test_rewrites_after_a_clobber(
        self, renderer: FrontendRenderer, tmp_path: Path, monkeypatch
    ) -> None:
        """An explicit beat must rewrite the file — that is the whole signal."""
        renderer._write_heartbeat()
        path = tmp_path / "run" / "frontend_heartbeat.json"
        path.write_text("{}", encoding="utf-8")  # clobber to prove a rewrite

        renderer._write_heartbeat()

        assert path.read_text(encoding="utf-8") != "{}"
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] > 0

        assert json.loads(path.read_text(encoding="utf-8"))["pid"] > 0

    def test_unwritable_run_dir_never_raises(self, renderer: FrontendRenderer, monkeypatch) -> None:
        """A full/read-only run dir must not take the slideshow down."""
        with mock.patch(
            "metixel.frontend.renderer.atomic_write_json",
            side_effect=OSError("read-only file system"),
        ):
            renderer._write_heartbeat()  # must not raise

    def test_shutdown_removes_the_heartbeat(
        self, renderer: FrontendRenderer, tmp_path: Path
    ) -> None:
        """A cleanly stopped frontend is reported "missing" immediately.

        Without this the tracker would call it "stale" only after 30 s, which
        would make a deliberate stop look like a hang.
        """
        renderer._write_heartbeat()
        path = tmp_path / "run" / "frontend_heartbeat.json"
        assert path.is_file()

        renderer._shutdown()

        assert not path.exists()
