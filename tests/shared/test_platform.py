# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for the shared Raspberry Pi platform + vcgencmd helpers.

These deduplicate the /proc/device-tree/model reads and vcgencmd get_mem
invocations previously scattered across the probe helpers, display backend
auto-detection, dispmanx backend, and the system-info endpoint.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest import mock

import pytest

from metixel.shared import platform as platform_mod
from metixel.shared.platform import (
    detect_pi_model,
    is_raspberry_pi,
    read_device_tree_model,
    read_vcgencmd_mem,
    read_vcgencmd_mem_str,
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


def _patch_model(monkeypatch, model: str | None) -> None:
    contents = {}
    if model is not None:
        contents["/proc/device-tree/model"] = model
    monkeypatch.setattr("builtins.open", _FakeOpen(contents))


class TestReadDeviceTreeModel:
    def test_reads_and_strips(self, monkeypatch):
        _patch_model(monkeypatch, "Raspberry Pi 4 Model B Rev 1.4\x00\n")
        assert read_device_tree_model() == "Raspberry Pi 4 Model B Rev 1.4"

    def test_missing_returns_none(self, monkeypatch):
        _patch_model(monkeypatch, None)
        assert read_device_tree_model() is None


class TestIsRaspberryPi:
    def test_true_for_pi(self, monkeypatch):
        _patch_model(monkeypatch, "Raspberry Pi 5 Model B Rev 1.0\n")
        assert is_raspberry_pi() is True

    def test_false_for_other_device(self, monkeypatch):
        _patch_model(monkeypatch, "ODROID-M1\n")
        assert is_raspberry_pi() is False

    def test_missing_with_legacy_fallback(self, monkeypatch):
        _patch_model(monkeypatch, None)
        monkeypatch.setattr(platform_mod.os.path, "exists", lambda p: p == "/opt/vc/lib/libEGL.so")
        assert is_raspberry_pi() is True

    def test_missing_without_fallback(self, monkeypatch):
        _patch_model(monkeypatch, None)
        monkeypatch.setattr(platform_mod.os.path, "exists", lambda p: False)
        assert is_raspberry_pi() is False


class TestDetectPiModel:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("Raspberry Pi 5 Model B Rev 1.0\n", "pi5"),
            ("Raspberry Pi 4 Model B Rev 1.4\n", "pi4"),
            ("Raspberry Pi 400 Rev 1.0\n", "pi4"),
            ("Raspberry Pi 3 Model B Plus Rev 1.3\n", "pi3"),
            ("Raspberry Pi 2 Model B Rev 1.1\n", "pi2"),
            ("Raspberry Pi Zero 2 W Rev 1.0\n", "pi3"),
            ("Raspberry Pi Model B Plus Rev 1.2\n", None),
        ],
    )
    def test_detect(self, monkeypatch, model, expected):
        _patch_model(monkeypatch, model)
        assert detect_pi_model() == expected

    def test_missing_returns_none(self, monkeypatch):
        _patch_model(monkeypatch, None)
        assert detect_pi_model() is None


class TestVcgencmdMem:
    def test_read_mem_parses_mb(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="gpu=512M"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem("gpu") == 512

    def test_read_mem_bad_returncode(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=1, stdout="gpu=512M"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem("gpu") is None

    def test_read_mem_no_equals(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="512M"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem("gpu") is None

    def test_read_mem_raises_returns_none(self, monkeypatch):
        fake = mock.MagicMock(side_effect=OSError("nope"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem("gpu") is None

    def test_read_mem_str(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="gpu=512M"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem_str("gpu") == "gpu=512M"

    def test_read_mem_str_bad_returncode_falls_back(self, monkeypatch):
        fake = mock.MagicMock(return_value=SimpleNamespace(returncode=1, stdout="gpu=512M"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem_str("gpu", fallback="unavailable") == "unavailable"

    def test_read_mem_str_raises_falls_back(self, monkeypatch):
        fake = mock.MagicMock(side_effect=OSError("nope"))
        monkeypatch.setattr("metixel.shared.platform.subprocess.run", fake)
        assert read_vcgencmd_mem_str("gpu", fallback="unavailable") == "unavailable"
