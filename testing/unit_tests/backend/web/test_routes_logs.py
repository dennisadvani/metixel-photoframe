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

    def test_chunk_boundary_exactly_on_newline_does_not_fuse_lines(self, tmp_path: Path) -> None:
        """Regression: with 4096-byte backwards chunks, a boundary that falls
        right after a ``\\n`` used to glue the line before it onto the line
        after it.  Build a file whose LAST 4096 bytes are exactly one line."""
        from metixel.backend.web.routes.logs import _tail_file

        first = "A" * 10
        last = "B" * 4095  # + "\n" = 4096 bytes = exactly one chunk
        path = tmp_path / "metixel.log"
        path.write_text(f"{first}\n{last}\n", encoding="utf-8")

        assert _tail_file(str(path), lines=5) == [first, last]

    def test_boundary_on_newline_mid_file(self, tmp_path: Path) -> None:
        """Same seam, but with many lines on both sides of it."""
        from metixel.backend.web.routes.logs import _tail_file

        lines = [f"line{i:04d}" for i in range(20)]
        # A final 4095-byte line + "\n" is exactly one 4096-byte chunk, so the
        # seam between it and the preceding chunk sits right after a newline.
        content = "\n".join(lines) + "\n" + "B" * 4095 + "\n"
        path = tmp_path / "metixel.log"
        path.write_text(content, encoding="utf-8")
        result = _tail_file(str(path), lines=3)
        assert result == ["line0018", "line0019", "B" * 4095]

    def test_tail_matches_naive_split_for_various_sizes(self, tmp_path: Path) -> None:
        """Property-style: for many file sizes the chunked tail must equal
        the trivial ``splitlines()[-n:]``."""
        from metixel.backend.web.routes.logs import _tail_file

        path = tmp_path / "metixel.log"
        for width in (1, 7, 100, 4095, 4096, 4097, 5000):
            body = "\n".join(f"{i}:" + "x" * (width % 50) for i in range(400))
            for trailing in ("", "\n"):
                path.write_text(body + trailing, encoding="utf-8")
                for n in (1, 3, 200, 1000):
                    assert _tail_file(str(path), lines=n) == body.splitlines()[-n:]


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

        backend_log = tmp_path / "metixel-backend.log"
        backend_log.write_text("alpha\nbeta\n", encoding="utf-8")
        monkeypatch.setattr(logs_mod, "_read_from_ring_buffer", lambda count: [])
        monkeypatch.setattr(logs_mod, "_log_files", lambda: [str(backend_log)])

        resp = client.get("/api/logs/recent")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["logs"] == ["alpha", "beta"]
        assert data["total"] == 2

    def test_merges_backend_and_frontend_logs(self, client, tmp_path, monkeypatch):
        """Each process writes its own file; the Logs page shows BOTH, merged
        chronologically.  A single-file tail would hide half the picture."""
        import metixel.backend.web.routes.logs as logs_mod

        backend = tmp_path / "metixel-backend.log"
        frontend = tmp_path / "metixel-frontend.log"
        backend.write_text(
            "2026-09-11 08:00:01 [INFO] metixel.backend: b1\n"
            "2026-09-11 08:00:03 [INFO] metixel.backend: b2\n",
            encoding="utf-8",
        )
        frontend.write_text(
            "2026-09-11 08:00:02 [INFO] metixel.frontend: f1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs_mod, "_read_from_ring_buffer", lambda count: [])
        monkeypatch.setattr(logs_mod, "_log_files", lambda: [str(backend), str(frontend)])

        resp = client.get("/api/logs/recent")
        data = json.loads(resp.data)
        # Interleaved by timestamp, not concatenated per file.
        assert [ln[-2:] for ln in data["logs"]] == ["b1", "f1", "b2"]

    def test_no_log_files_returns_empty(self, client, monkeypatch):
        import metixel.backend.web.routes.logs as logs_mod

        monkeypatch.setattr(logs_mod, "_read_from_ring_buffer", lambda count: [])
        monkeypatch.setattr(logs_mod, "_log_files", lambda: [])

        resp = client.get("/api/logs/recent")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"logs": [], "total": 0}


class TestSetLogLevel:
    def test_missing_level_returns_400(self, client):
        resp = client.post("/api/logs/level", json={})
        assert resp.status_code == 400

    def test_invalid_level_returns_400(self, client):
        resp = client.post("/api/logs/level", json={"level": "BOGUS"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "valid" in data

    def test_non_string_level_returns_400(self, client):
        resp = client.post("/api/logs/level", json={"level": 10})
        assert resp.status_code == 400

    def test_valid_level_persisted(self, client, mock_state):
        resp = client.post("/api/logs/level", json={"level": "WARNING"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["level"] == "WARNING"
        assert mock_state.config.system["log_level"] == "WARNING"
