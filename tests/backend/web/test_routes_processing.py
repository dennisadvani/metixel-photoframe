# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the media processing control endpoints and journal status."""

from pathlib import Path

from metixel.backend.processing.journal import STATE_FAILED


def _sample_path() -> str:
    return str(Path("/opt/metixel/media/bad.mp4"))


class TestProcessingRetry:
    def test_retry_removes_failed_entry(self, client, mock_state) -> None:
        mock_state.journal.mark_failed(_sample_path(), "encoder failed")
        assert mock_state.journal.get(_sample_path()) is not None

        resp = client.post("/api/processing/retry", json={"path": _sample_path()})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"
        assert mock_state.journal.get(_sample_path()) is None

    def test_retry_requires_path(self, client) -> None:
        resp = client.post("/api/processing/retry", json={})
        assert resp.status_code == 400

    def test_retry_missing_route_not_handled(self, client) -> None:
        """A POST to an unknown processing path must not hit the retry handler.

        The SPA catch-all (``/<path:path>``) is GET-only, so unknown POSTs
        come back as 405 Method Not Allowed.
        """
        resp = client.post("/api/processing/nope", json={})
        assert resp.status_code == 405


class TestHealthProcessingStatus:
    def test_processing_status_includes_issues(self, client, mock_state) -> None:
        mock_state.journal.mark_failed(_sample_path(), "transcode failed")

        resp = client.get("/api/health/processing-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "issues" in data
        assert "phases" in data
        assert any(
            i.get("state") == STATE_FAILED and i.get("reason") == "transcode failed"
            for i in data["issues"]
        )

    def test_processing_status_empty_issues(self, client) -> None:
        resp = client.get("/api/health/processing-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["issues"] == []
