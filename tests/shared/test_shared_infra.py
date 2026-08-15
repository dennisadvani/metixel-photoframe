# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared infrastructure modules (io, paths, media, subprocess, retry)."""

import json
import os

import pytest

from metixel.shared.io import atomic_write_json, read_json
from metixel.shared.media import (
    HEIC_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MEDIA_EXTENSIONS,
    VIDEO_EXTENSIONS,
    content_hash,
    fingerprint,
    is_image,
    is_media,
    is_video,
)
from metixel.shared.paths import install_root, resolve_install_path, run_dir, run_path
from metixel.shared.retry import backoff_delays, retry

# ---------------------------------------------------------------------------
# io.py
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    def test_writes_and_replaces(self, tmp_path):
        """atomic_write_json should persist data and overwrite atomically."""
        path = tmp_path / "sub" / "file.json"
        atomic_write_json(path, {"a": 1, "b": [2, 3]})
        assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}
        # No leftover temp file
        assert not (path.parent / "file.json.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        """A subsequent write should replace the previous content."""
        path = tmp_path / "f.json"
        atomic_write_json(path, {"v": 1})
        atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}


class TestReadJson:
    def test_returns_data(self, tmp_path):
        """read_json should return parsed content for a valid file."""
        path = tmp_path / "f.json"
        path.write_text(json.dumps({"x": 1}))
        assert read_json(path) == {"x": 1}

    def test_missing_file_returns_default(self, tmp_path):
        """A missing file should return the default without raising."""
        assert read_json(tmp_path / "nope.json", default={}) == {}

    def test_malformed_returns_default(self, tmp_path):
        """A malformed file should return the default without raising."""
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        assert read_json(path, default=None) is None


# ---------------------------------------------------------------------------
# paths.py
# ---------------------------------------------------------------------------


class TestPaths:
    def test_install_root_is_absolute(self):
        """install_root() should always return an absolute Path."""
        assert install_root().is_absolute()

    def test_resolve_install_path_relative(self):
        """Relative paths should be joined onto the install root."""
        resolved = resolve_install_path("media/")
        assert resolved.is_absolute()
        assert resolved.name == "media"

    def test_resolve_install_path_absolute_unchanged(self, tmp_path):
        """Absolute paths should be returned unchanged."""
        assert resolve_install_path(tmp_path) == tmp_path

    def test_run_dir_honours_env(self, tmp_path, monkeypatch):
        """run_dir() should honour the METIXEL_RUN_DIR env var."""
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path))
        assert run_dir() == tmp_path

    def test_run_path_joins_name(self, monkeypatch, tmp_path):
        """run_path() should join a name onto the run dir."""
        monkeypatch.setenv("METIXEL_RUN_DIR", str(tmp_path))
        assert run_path("playlist.json") == tmp_path / "playlist.json"


# ---------------------------------------------------------------------------
# media.py
# ---------------------------------------------------------------------------


class TestMediaExtensions:
    def test_sets_are_distinct(self):
        """Image and video extension sets should not overlap."""
        assert IMAGE_EXTENSIONS.isdisjoint(VIDEO_EXTENSIONS)
        assert MEDIA_EXTENSIONS == IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
        assert ".heic" in HEIC_EXTENSIONS

    def test_is_helpers(self):
        """is_image/is_video/is_media should classify by extension."""
        assert is_image("a.JPG")
        assert is_video("a.mp4")
        assert is_media("a.webp")
        assert not is_image("a.mp4")
        assert not is_media("a.txt")


class TestContentHash:
    def test_stable_and_short(self, tmp_path):
        """content_hash should be 16 hex chars and stable across reads."""
        path = tmp_path / "f.bin"
        path.write_bytes(os.urandom(4096))
        h1 = content_hash(path)
        h2 = content_hash(path)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_content_different_hash(self, tmp_path):
        """Different file content should produce different hashes."""
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"alpha" * 1000)
        b.write_bytes(b"beta" * 1000)
        assert content_hash(a) != content_hash(b)

    def test_small_file_hashed(self, tmp_path):
        """Files smaller than 1 KB should still hash (no seek error)."""
        path = tmp_path / "tiny.bin"
        path.write_bytes(b"tiny")
        assert len(content_hash(path)) == 16


class TestFingerprint:
    def test_returns_mtime_and_size(self, tmp_path):
        """fingerprint should return (mtime_ns, size)."""
        path = tmp_path / "f.bin"
        path.write_bytes(b"hello")
        mtime_ns, size = fingerprint(path)
        assert size == 5
        assert mtime_ns > 0


# ---------------------------------------------------------------------------
# retry.py
# ---------------------------------------------------------------------------


class TestRetry:
    def test_returns_on_success(self):
        """retry should return the function result on the first success."""
        assert retry(lambda: 42, attempts=3) == 42

    def test_retries_then_succeeds(self):
        """retry should keep calling until the function succeeds."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("boom")
            return "ok"

        result = retry(flaky, attempts=5, base=0.01)
        assert result == "ok"
        assert calls["n"] == 3

    def test_raises_last_exception(self):
        """retry should re-raise the last exception after exhausting attempts."""

        def always_fail():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            retry(always_fail, attempts=2, base=0.01)

    def test_backoff_delays(self):
        """backoff_delays should yield attempts-1 increasing delays."""
        delays = list(backoff_delays(4, base=1.0, factor=2.0))
        assert delays == [1.0, 2.0, 4.0]

    def test_backoff_cap(self):
        """backoff_delays should cap delays."""
        delays = list(backoff_delays(4, base=5.0, factor=2.0, cap=6.0))
        assert delays == [5.0, 6.0, 6.0]
