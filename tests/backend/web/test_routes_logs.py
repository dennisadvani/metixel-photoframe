# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""System log API endpoints — ring buffer reads, file tail, log-level control."""

from __future__ import annotations

import json
import logging
from pathlib import Path


class TestTailFile:
    def test_returns_last_lines(self, tmp_path: Path) -> None:
        from metixel.backend.web.routes.logs import _tail_file

        path = tmp_path / "metixel.log"
        path.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
        assert _tail_file(str(path), lines=5) == [
            "line45",
            "line46",
            "line47",
            "line48",
            "line49",
        ]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from metixel.backend.web.routes.logs import _tail_file

        assert _tail_file(str(tmp_path / "nope.log")) == []


class TestRecentLogs:
    def test_reads_from_ring_buffer(self, client):
        from metixel.shared.log_buffer import LogRingBuffer

        logger = logging.getLogger("metixel")
        # The root logger defaults to WARNING, which would drop INFO records
        # before they reach the ring buffer — enable DEBUG for the test.
        logger.setLevel(logging.DEBUG)
        buf = LogRingBuffer(capacity=100)
        buf.setLevel(logging.DEBUG)
        # Mirror production (__main__ attaches a formatter) so entries carry
        # a formatted timestamp.
        buf.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(buf)
        try:
            logger.info("hello from web test")
            resp = client.get("/api/logs/recent")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["total"] >= 1
            messages = [entry.get("message") for entry in data["logs"]]
            assert "hello from web test" in messages
        finally:
            logger.removeHandler(buf)
            logger.setLevel(logging.NOTSET)

    def test_falls_back_to_log_file(self, client, tmp_path, monkeypatch):
        import metixel.backend.web.routes.logs as logs_mod

        log_path = tmp_path / "metixel.log"
        log_path.write_text("alpha\nbeta\n", encoding="utf-8")
        monkeypatch.setattr(logs_mod, "_read_from_ring_buffer", lambda count: [])
        monkeypatch.setattr(logs_mod, "_find_log_file", lambda: str(log_path))

        resp = client.get("/api/logs/recent")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["logs"] == ["alpha", "beta"]
        assert data["total"] == 2


class TestSetLogLevel:
    def test_missing_level_returns_400(self, client):
        resp = client.post("/api/logs/level", json={})
        assert resp.status_code == 400

    def test_invalid_level_returns_400(self, client):
        resp = client.post("/api/logs/level", json={"level": "BOGUS"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "valid" in data

    def test_valid_level_persisted(self, client, mock_state):
        resp = client.post("/api/logs/level", json={"level": "WARNING"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["level"] == "WARNING"
        assert mock_state.config.system["log_level"] == "WARNING"
