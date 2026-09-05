# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared /proc system + GPU stats helpers.

These helpers deduplicate the /proc/meminfo, /proc/stat and /proc/loadavg
parsers previously scattered across the renderer, optimisation queue, state
manager, probe helpers, and ffmpeg command builders.
"""

from __future__ import annotations

import io

import pytest

from metixel.shared.system_stats import (
    available_ram_bytes,
    format_gpu_stats,
    read_cpu_percent,
    read_loadavg,
    read_meminfo,
    read_system_stats,
)


class _FakeOpen:
    """Stand-in for ``builtins.open`` keyed by absolute path."""

    def __init__(self, contents: dict[str, str]) -> None:
        self._contents = contents

    def __call__(self, path, *args, **kwargs):
        key = str(path)
        if key in self._contents:
            return io.StringIO(self._contents[key])
        raise FileNotFoundError(key)


def _patch_proc(monkeypatch, files: dict[str, str]) -> None:
    """Point builtins.open at in-memory /proc content."""
    monkeypatch.setattr("builtins.open", _FakeOpen(files))


class TestReadMeminfo:
    def test_parses_kb_values(self, monkeypatch):
        _patch_proc(
            monkeypatch,
            {
                "/proc/meminfo": (
                    "MemTotal: 8123456 kB\nMemAvailable: 5000000 kB\nSwapTotal: 1000000 kB\n"
                )
            },
        )
        mem = read_meminfo()
        assert mem["MemTotal"] == 8123456
        assert mem["MemAvailable"] == 5000000
        assert mem["SwapTotal"] == 1000000

    def test_missing_file_returns_empty(self, monkeypatch):
        _patch_proc(monkeypatch, {})
        assert read_meminfo() == {}

    def test_malformed_value_discards_parse(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/meminfo": "MemTotal: not-a-number\n"})
        assert read_meminfo() == {}


class TestAvailableRamBytes:
    def test_returns_bytes(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/meminfo": "MemAvailable: 512000 kB\n"})
        assert available_ram_bytes() == 512000 * 1024

    def test_missing_returns_none(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/meminfo": "MemTotal: 1000 kB\n"})
        assert available_ram_bytes() is None


class TestReadCpuPercent:
    def test_parses_proc_stat(self, monkeypatch):
        # total = 100 + 20 + 30 + 850 = 1000; active = 150 → 15%
        _patch_proc(monkeypatch, {"/proc/stat": "cpu  100 20 30 850 0 0 0 0 0 0\n"})
        assert read_cpu_percent() == 15.0

    def test_missing_returns_minus_one(self, monkeypatch):
        _patch_proc(monkeypatch, {})
        assert read_cpu_percent() == -1.0


class TestReadLoadavg:
    def test_returns_three_values(self, monkeypatch):
        _patch_proc(monkeypatch, {"/proc/loadavg": "0.50 0.42 0.35 1/234 12345\n"})
        assert read_loadavg() == ("0.50", "0.42", "0.35")

    def test_missing_raises_oserror(self, monkeypatch):
        _patch_proc(monkeypatch, {})
        with pytest.raises(OSError):
            read_loadavg()


class TestReadSystemStats:
    def test_missing_returns_none(self, monkeypatch):
        _patch_proc(monkeypatch, {})
        assert read_system_stats() is None

    def test_snapshot(self, monkeypatch):
        _patch_proc(
            monkeypatch,
            {
                "/proc/meminfo": (
                    "MemTotal: 2048000 kB\n"
                    "MemAvailable: 1024000 kB\n"
                    "SwapTotal: 512000 kB\n"
                    "SwapFree: 256000 kB\n"
                ),
                "/proc/stat": "cpu  100 20 30 850 0 0 0 0 0 0\n",
                "/proc/loadavg": "0.50 0.42 0.35 1/234 12345\n",
            },
        )
        s = read_system_stats()
        assert s is not None
        assert s["mem_total_mb"] == 2048000 // 1024
        assert s["mem_used_mb"] == (2048000 - 1024000) // 1024
        assert s["swap_total_mb"] == 512000 // 1024
        assert s["swap_used_mb"] == (512000 - 256000) // 1024
        assert s["cpu_percent"] == 15.0
        assert s["loadavg"] == ("0.50", "0.42", "0.35")


class TestFormatGpuStats:
    def test_none(self):
        assert format_gpu_stats(None) == "unavailable"

    def test_formats_all_fields_with_pct(self):
        info = {
            "gpu_total_mb": 128,
            "reloc_used_mb": 64,
            "v3d_bo_kb": 1024,
            "v3d_bo_count": 8,
            "texture_count": 2,
            "max_textures": 3,
        }
        out = format_gpu_stats(info)
        assert "total=128M" in out
        assert "reloc=64M" in out
        assert "(50%)" in out
        assert "V3D=1024kb/8BOs" in out
        assert "textures=2/3" in out

    def test_missing_keys_use_question_mark(self):
        assert (
            format_gpu_stats({"gpu_total_mb": 128})
            == "total=128M reloc=?M V3D=?kb/?BOs textures=?/?"
        )
