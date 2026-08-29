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


class TestProcessingDelete:
    def test_delete_removes_file_and_journal(self, client, mock_state, tmp_path) -> None:
        # Make the temp dir a watch path so the file is deletable
        mock_state.update_config(
            "sync",
            {"local": {"watch_paths": [{"path": str(tmp_path), "enabled": True}]}},
        )
        media = tmp_path / "broken.jpg"
        media.write_bytes(b"x" * 128)
        mock_state.journal.mark_skipped(media.resolve(), "Could not read media metadata")
        assert mock_state.journal.get(media.resolve()) is not None

        resp = client.post("/api/processing/delete", json={"path": str(media)})
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] is True
        assert not media.exists()
        assert mock_state.journal.get(media.resolve()) is None

    def test_delete_removes_playlist_item(self, client, mock_state, tmp_path) -> None:
        from metixel.shared.models import MediaItem, MediaType

        mock_state.update_config(
            "sync",
            {"local": {"watch_paths": [{"path": str(tmp_path), "enabled": True}]}},
        )
        media = tmp_path / "broken.jpg"
        media.write_bytes(b"x" * 128)
        item = MediaItem(
            id="broken",
            original_path=media.resolve(),
            cached_path=media.resolve(),
            media_type=MediaType.IMAGE,
            width=10,
            height=10,
        )
        mock_state.add_playlist_items([item])
        mock_state.journal.mark_skipped(media.resolve(), "Could not read media metadata")

        resp = client.post("/api/processing/delete", json={"path": str(media)})
        assert resp.status_code == 200
        assert all(i.id != "broken" for i in mock_state.get_playlist())

    def test_delete_refuses_outside_watch_path(self, client, tmp_path) -> None:
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"x" * 128)
        resp = client.post("/api/processing/delete", json={"path": str(outside)})
        assert resp.status_code == 400
        assert outside.exists()

    def test_delete_requires_path(self, client) -> None:
        resp = client.post("/api/processing/delete", json={})
        assert resp.status_code == 400


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
