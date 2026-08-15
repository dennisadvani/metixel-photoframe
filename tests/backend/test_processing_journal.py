# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the ProcessingJournal single-writer state controller."""

from pathlib import Path

import pytest

from metixel.backend.processing.journal import (
    STATE_FAILED,
    STATE_PENDING,
    STATE_PROCESSING,
    STATE_READY,
    STATE_SKIPPED,
    ProcessingJournal,
)


@pytest.fixture
def journal(tmp_path: Path) -> ProcessingJournal:
    """A journal persisted inside a temp dir (immediate saves in tests)."""
    return ProcessingJournal(tmp_path / "cache" / "processing_state.json", save_after=0.0)


def _fp(mtime: int = 1000, size: int = 2000) -> tuple[int, int]:
    return (mtime, size)


class TestProcessingJournalBasics:
    def test_new_journal_empty(self, journal: ProcessingJournal) -> None:
        assert journal.paths() == []
        assert journal.snapshot() == {}
        assert journal.stats() == {
            STATE_PENDING: 0,
            STATE_PROCESSING: 0,
            STATE_READY: 0,
            STATE_FAILED: 0,
            STATE_SKIPPED: 0,
        }

    def test_mark_pending_creates_entry(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/a.jpg")
        journal.mark_pending(p, _fp(), media_type="image", name="a.jpg")
        entry = journal.get(p)
        assert entry is not None
        assert entry["state"] == STATE_PENDING
        assert entry["mtime_ns"] == 1000
        assert entry["size"] == 2000
        assert entry["name"] == "a.jpg"
        assert entry["media_type"] == "image"

    def test_get_unknown_path_returns_none(self, journal: ProcessingJournal) -> None:
        assert journal.get("/nonexistent.jpg") is None

    def test_is_handled_false_for_unknown(self, journal: ProcessingJournal) -> None:
        assert journal.is_handled("/opt/metixel/media/a.jpg", _fp()) is False


class TestProcessingJournalStates:
    def test_terminal_states_are_handled(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_pending(p, _fp(), media_type="video")
        journal.mark_ready(p, "transcoded")
        assert journal.is_handled(p, _fp()) is True

        p2 = Path("/opt/metixel/media/bad.mp4")
        journal.mark_pending(p2, _fp(), media_type="video")
        journal.mark_failed(p2, "encoder failed")
        assert journal.is_handled(p2, _fp()) is True

        p3 = Path("/opt/metixel/media/corrupt.jpg")
        journal.mark_pending(p3, _fp(), media_type="image")
        journal.mark_skipped(p3, "unreadable")
        assert journal.is_handled(p3, _fp()) is True

    def test_is_handled_false_when_fingerprint_changes(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_pending(p, _fp(), media_type="video")
        journal.mark_ready(p, "transcoded")
        # Matching fingerprint → handled
        assert journal.is_handled(p, _fp()) is True
        # Different mtime/size → file modified → must be re-picked-up
        assert journal.is_handled(p, _fp(mtime=9999, size=9999)) is False

    def test_is_handled_false_for_non_terminal_states(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_pending(p, _fp(), media_type="video")
        assert journal.is_handled(p, _fp()) is False

        journal.mark_processing(p)
        assert journal.is_handled(p, _fp()) is False

    def test_mark_ready_clears_reason(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_failed(p, "boom")
        journal.mark_ready(p, "transcoded")
        entry = journal.get(p)
        assert entry["state"] == STATE_READY
        assert entry["reason"] is None
        assert entry["transcode_status"] == "transcoded"

    def test_failed_increments_attempts(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_failed(p, "attempt 1")
        journal.mark_failed(p, "attempt 2")
        assert journal.get(p)["attempts"] == 2


class TestProcessingJournalIssues:
    def test_issues_returns_failed_and_skipped(self, journal: ProcessingJournal) -> None:
        journal.mark_failed(Path("/opt/metixel/media/a.mp4"), "transcode failed")
        journal.mark_skipped(Path("/opt/metixel/media/b.jpg"), "unreadable")
        journal.mark_ready(Path("/opt/metixel/media/c.jpg"), None)  # not an issue

        issues = journal.issues()
        assert len(issues) == 2
        states = {i["state"] for i in issues}
        assert states == {STATE_FAILED, STATE_SKIPPED}

    def test_issues_sorted_newest_first(self, journal: ProcessingJournal) -> None:
        journal.mark_failed(Path("/opt/metixel/media/a.mp4"), "x")
        journal.mark_failed(Path("/opt/metixel/media/b.mp4"), "y")
        issues = journal.issues()
        # b was marked after a → newer → first
        assert issues[0]["path"].endswith("b.mp4")


class TestProcessingJournalLifecycle:
    def test_remove_deletes_entry(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/v.mp4")
        journal.mark_pending(p, _fp(), media_type="video")
        assert journal.get(p) is not None
        journal.remove(p)
        assert journal.get(p) is None

    def test_retry_forgets_entry(self, journal: ProcessingJournal) -> None:
        p = Path("/opt/metixel/media/bad.mp4")
        journal.mark_pending(p, _fp(), media_type="video")
        journal.mark_failed(p, "encoder failed")
        assert journal.is_handled(p, _fp()) is True
        journal.retry(p)
        assert journal.get(p) is None
        assert journal.is_handled(p, _fp()) is False

    def test_clear_wipes_everything(self, journal: ProcessingJournal) -> None:
        journal.mark_pending(Path("/opt/metixel/media/a.jpg"), _fp(), media_type="image")
        journal.mark_failed(Path("/opt/metixel/media/b.mp4"), "x")
        assert len(journal.paths()) == 2
        journal.clear()
        assert journal.paths() == []


class TestProcessingJournalPersistence:
    def test_persist_and_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "cache" / "processing_state.json"
        j1 = ProcessingJournal(path, save_after=0.0)
        j1.mark_pending(Path("/opt/metixel/media/a.jpg"), _fp(), media_type="image", name="a.jpg")
        j1.mark_failed(Path("/opt/metixel/media/b.mp4"), "encoder failed")
        j1.flush()

        j2 = ProcessingJournal(path, save_after=0.0)
        assert len(j2.paths()) == 2
        a = j2.get("/opt/metixel/media/a.jpg")
        assert a["state"] == STATE_PENDING
        b = j2.get("/opt/metixel/media/b.mp4")
        assert b["state"] == STATE_FAILED
        assert b["reason"] == "encoder failed"

    def test_load_missing_or_corrupt_is_empty(self, tmp_path: Path) -> None:
        missing = ProcessingJournal(tmp_path / "nope.json")
        assert missing.paths() == []

        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{ not valid json", encoding="utf-8")
        j = ProcessingJournal(corrupt)
        assert j.paths() == []

    def test_flush_writes_file(self, journal: ProcessingJournal) -> None:
        journal.mark_ready(Path("/opt/metixel/media/a.jpg"), None)
        journal.flush()
        assert journal._path.is_file()


class TestStateManagerJournalIntegration:
    """StateManager wires the journal and marks playlist adds as ready."""

    def _make_state(self, tmp_path: Path):
        import json as _json

        from metixel.backend.state import StateManager

        config_path = tmp_path / "config.json"
        config_path.write_text(
            _json.dumps({"system": {"cache_dir": str(tmp_path / "cache")}}),
            encoding="utf-8",
        )
        return StateManager(config_path, run_dir=tmp_path / "run")

    def test_add_playlist_items_marks_journal_ready(self, tmp_path: Path) -> None:
        from metixel.shared.models import MediaItem, MediaType

        sm = self._make_state(tmp_path)
        # The watcher creates the journal on its first scan before any item
        # reaches the playlist — simulate that here.
        _ = sm.journal
        item = MediaItem(
            id="abc123",
            original_path=Path("/opt/metixel/media/a.jpg"),
            cached_path=Path("/opt/metixel/media/a.jpg"),
            media_type=MediaType.IMAGE,
            width=100,
            height=100,
        )
        sm.add_playlist_items([item])

        entry = sm.journal.get("/opt/metixel/media/a.jpg")
        assert entry is not None
        assert entry["state"] == STATE_READY

    def test_add_playlist_items_noop_does_not_mark(self, tmp_path: Path) -> None:
        from metixel.shared.models import MediaItem, MediaType

        sm = self._make_state(tmp_path)
        _ = sm.journal
        item = MediaItem(
            id="abc123",
            original_path=Path("/opt/metixel/media/a.jpg"),
            cached_path=Path("/opt/metixel/media/a.jpg"),
            media_type=MediaType.IMAGE,
            width=100,
            height=100,
        )
        sm.add_playlist_items([item])
        # Adding the same item again is a no-op — still one ready entry
        sm.add_playlist_items([item])
        assert sm.journal.get("/opt/metixel/media/a.jpg")["state"] == STATE_READY

    def test_flush_journal_noop_when_unused(self, tmp_path: Path) -> None:
        sm = self._make_state(tmp_path)
        sm.flush_journal()  # should not raise
