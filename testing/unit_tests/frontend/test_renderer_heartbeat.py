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

from metixel.frontend.renderer import HEARTBEAT_INTERVAL, FrontendRenderer


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

    def test_write_is_throttled(self, renderer: FrontendRenderer, tmp_path: Path) -> None:
        """Second immediate call must be a no-op — this is not a per-frame write."""
        renderer._write_heartbeat()
        path = tmp_path / "run" / "frontend_heartbeat.json"
        first = path.stat().st_mtime_ns

        renderer._write_heartbeat()

        assert path.stat().st_mtime_ns == first

    def test_rewrites_once_the_interval_elapses(
        self, renderer: FrontendRenderer, tmp_path: Path, monkeypatch
    ) -> None:
        """After the interval the beat must happen — that is the whole signal."""
        clock = [1000.0]
        monkeypatch.setattr("metixel.frontend.renderer.time.monotonic", lambda: clock[0])

        renderer._write_heartbeat()
        path = tmp_path / "run" / "frontend_heartbeat.json"
        path.write_text("{}", encoding="utf-8")  # clobber to prove a rewrite

        clock[0] += HEARTBEAT_INTERVAL + 0.1
        renderer._write_heartbeat()

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
