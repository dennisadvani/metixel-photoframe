# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the OptimisationQueue two-phase video pipeline.

Verifies the orchestration only — the ``VideoProcessor`` is mocked so no
ffmpeg/ffprobe runs.  Phase A scans every queued video (streaming non-transcode
videos into the playlist immediately); Phase B transcodes the subset only after
all scanning is done.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from metixel.backend.processing.journal import STATE_FAILED, STATE_READY
from metixel.backend.processing.optimisation_queue import OptimisationQueue
from metixel.backend.processing.video import VideoScan
from metixel.backend.state import StateManager
from metixel.shared.models import MediaItem, MediaType, TranscodeStatus


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    """A real StateManager with a temp cache dir (journal lands in tmp)."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"system": {"cache_dir": str(tmp_path / "cache")}}),
        encoding="utf-8",
    )
    return StateManager(config_path, run_dir=tmp_path / "run")


@pytest.fixture
def queue(state: StateManager) -> OptimisationQueue:
    q = OptimisationQueue(state)
    q._video_processor = mock.MagicMock()
    q._image_processor = mock.MagicMock()
    q._video_processor.is_transcoding.return_value = False
    return q


def _scan(path: Path, file_hash: str, needs_transcode: bool, frames: bool = True) -> VideoScan:
    ff = (Path("/tmp/f1.jpg"), Path("/tmp/f2.jpg")) if frames else (None, None)
    return VideoScan(
        source_path=path,
        source="local",
        file_hash=file_hash,
        info={"codec_name": "h264"},
        thumbnail_path=Path("/tmp/t.jpg"),
        first_frame_path=ff[0],
        last_frame_path=ff[1],
        needs_transcode=needs_transcode,
        errors=[] if frames else ["Frame extraction failed (first/last frame missing)"],
    )


def _item(path: Path, file_hash: str) -> MediaItem:
    return MediaItem(
        id=file_hash,
        original_path=path,
        cached_path=path,
        media_type=MediaType.VIDEO,
    )


def _build_media(scan: VideoScan) -> MediaItem:
    """Mock processor.transcode(): build the item a real transcode() would."""
    status = TranscodeStatus.TRANSCODED if scan.needs_transcode else TranscodeStatus.NOT_TRANSCODED
    return MediaItem(
        id=scan.file_hash,
        original_path=scan.source_path,
        cached_path=scan.source_path,
        media_type=MediaType.VIDEO,
        first_frame_path=scan.first_frame_path,
        last_frame_path=scan.last_frame_path,
        transcode_status=status,
    )


class TestTwoPhaseVideoPipeline:
    def test_scan_all_then_transcode_subset(self, queue, tmp_path) -> None:
        """Non-transcode videos stream in during scan; transcode videos only after."""
        v_play = tmp_path / "plays.mp4"
        v_enc = tmp_path / "encodes.mp4"
        for p in (v_play, v_enc):
            p.write_bytes(b"x" * 1024)

        queue._video_processor.scan = mock.Mock(
            side_effect=[
                _scan(v_play, "hash-play", needs_transcode=False),
                _scan(v_enc, "hash-enc", needs_transcode=True),
            ]
        )
        queue._video_processor.transcode = mock.Mock(side_effect=_build_media)
        # the encode video has no valid cache → real encode needed
        queue._video_processor.requires_encode = mock.Mock(
            side_effect=lambda s: s.file_hash == "hash-enc"
        )
        queue._video_queue = [_item(v_play, "hash-play"), _item(v_enc, "hash-enc")]

        queue._process_video_queue()

        ids = {m.id for m in queue._state.get_playlist()}
        assert "hash-play" in ids  # streamed in during scanning
        assert "hash-enc" in ids  # added after Phase B

        # The transcode step ran only for the video that needs it
        transcode_calls = queue._video_processor.transcode.call_args_list
        assert len(transcode_calls) == 2  # 1 build-NOT_TRANSCODED + 1 encode
        encoded_hashes = [c.args[0].file_hash for c in transcode_calls if c.args[0].needs_transcode]
        assert encoded_hashes == ["hash-enc"]

        # Journal: both end up ready
        assert queue._state.journal.get(v_play)["state"] == STATE_READY
        assert queue._state.journal.get(v_enc)["state"] == STATE_READY
        assert queue._vid_scanned == 2
        assert queue._vid_transcoded == 1
        assert queue._video_processing is False

    def test_cache_reuse_skips_transcode_bar(self, queue, tmp_path) -> None:
        """A video needing transcode but with a valid cache is reused, not counted."""
        v = tmp_path / "cached.mp4"
        v.write_bytes(b"x" * 1024)
        scan = _scan(v, "hash-cached", needs_transcode=True)
        queue._video_processor.scan = mock.Mock(return_value=scan)
        queue._video_processor.transcode = mock.Mock(side_effect=_build_media)
        queue._video_processor.requires_encode = mock.Mock(return_value=False)
        queue._video_queue = [_item(v, "hash-cached")]

        queue._process_video_queue()

        assert {m.id for m in queue._state.get_playlist()} == {"hash-cached"}
        assert queue._vid_scanned == 1
        assert queue._vid_transcoded == 0  # cache reuse is NOT counted in the bar
        # transcode was called once (to finalise/reuse), not to encode
        assert queue._video_processor.transcode.call_count == 1

    def test_scan_failure_marks_failed_and_excludes(self, queue, tmp_path) -> None:
        v = tmp_path / "bad.mp4"
        v.write_bytes(b"x" * 1024)
        queue._video_processor.scan = mock.Mock(return_value=None)
        queue._video_queue = [_item(v, "hash-bad")]

        queue._process_video_queue()

        assert queue._state.get_playlist() == []
        assert queue._state.journal.get(v)["state"] == STATE_FAILED
        assert queue._vid_scanned == 1
        assert queue._vid_transcoded == 0

    def test_missing_frames_marks_failed_and_excludes(self, queue, tmp_path) -> None:
        v = tmp_path / "noframes.mp4"
        v.write_bytes(b"x" * 1024)
        scan = _scan(v, "hash-nf", needs_transcode=False, frames=False)
        queue._video_processor.scan = mock.Mock(return_value=scan)
        queue._video_queue = [_item(v, "hash-nf")]

        queue._process_video_queue()

        assert queue._state.get_playlist() == []
        assert queue._state.journal.get(v)["state"] == STATE_FAILED

    def test_transcode_failure_marks_failed_not_playlist(self, queue, tmp_path) -> None:
        v = tmp_path / "fails.mp4"
        v.write_bytes(b"x" * 1024)
        scan = _scan(v, "hash-fail", needs_transcode=True)

        def _failed(scan):
            return MediaItem(
                id=scan.file_hash,
                original_path=scan.source_path,
                cached_path=scan.source_path,
                media_type=MediaType.VIDEO,
                first_frame_path=scan.first_frame_path,
                last_frame_path=scan.last_frame_path,
                transcode_status=TranscodeStatus.FAILED,
                failure_reason="encoder crashed",
            )

        queue._video_processor.scan = mock.Mock(return_value=scan)
        queue._video_processor.transcode = mock.Mock(side_effect=_failed)
        queue._video_processor.requires_encode = mock.Mock(return_value=True)
        queue._video_queue = [_item(v, "hash-fail")]

        queue._process_video_queue()

        assert queue._state.get_playlist() == []
        assert queue._state.journal.get(v)["state"] == STATE_FAILED
        assert "encoder crashed" in queue._state.journal.get(v)["reason"]
