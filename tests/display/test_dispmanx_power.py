# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for Pi3dBackend display-power output auto-detection.

The Raspberry Pi exposes two HDMI connectors (e.g. ``HDMI-A-1`` and
``HDMI-A-2``) but only one usually has a real monitor attached.  The
backend must target the output with a real EDID (non-null make/model)
instead of blindly using the first connector.
"""

import json
from types import SimpleNamespace

import pytest

from metixel.display.dispmanx_backend import Pi3dBackend


def _result(stdout: str, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Build a fake subprocess.CompletedProcess-like object."""
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout.encode(),
        stderr=stderr.encode(),
    )


def _fake_outputs() -> str:
    """Two outputs mirroring a real Pi: a phantom port and a real monitor."""
    return json.dumps(
        [
            {
                "name": "HDMI-A-1",
                "description": "(null) (null) (HDMI-A-1)",
                "make": None,
                "model": None,
                "serial": None,
                "enabled": True,
                "modes": [
                    {
                        "width": 1024,
                        "height": 768,
                        "refresh": 60.0,
                        "preferred": False,
                        "current": True,
                    },
                    {
                        "width": 800,
                        "height": 600,
                        "refresh": 60.0,
                        "preferred": False,
                        "current": False,
                    },
                ],
            },
            {
                "name": "HDMI-A-2",
                "description": "Dell Inc. DELL U2412M (HDMI-A-2)",
                "make": "Dell Inc.",
                "model": "DELL U2412M",
                "serial": "PPNN15CU07VL",
                "enabled": True,
                "modes": [
                    {
                        "width": 1920,
                        "height": 1200,
                        "refresh": 59.95,
                        "preferred": True,
                        "current": True,
                    },
                    {
                        "width": 1920,
                        "height": 1080,
                        "refresh": 60.0,
                        "preferred": False,
                        "current": False,
                    },
                ],
            },
        ]
    )


@pytest.fixture
def wlr_available(monkeypatch):
    """Make wlr-randr appear installed and route subprocess to a mock."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _result(_fake_outputs())

    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.dispmanx_backend.subprocess.run", fake_run)
    return calls


def test_detect_picks_real_monitor(wlr_available):
    """HDMI-A-2 (real EDID) is chosen over the phantom HDMI-A-1 port."""
    backend = Pi3dBackend()
    assert backend._detect_wlr_output() == "HDMI-A-2"
    assert backend.connected_output() == "HDMI-A-2"


def test_detect_falls_back_to_preferred_mode(monkeypatch):
    """With no EDID anywhere, prefer the output advertising a native mode."""
    outputs = [
        {
            "name": "HDMI-A-1",
            "make": None,
            "model": None,
            "enabled": True,
            "modes": [
                {"width": 1024, "height": 768, "refresh": 60.0, "preferred": True, "current": True}
            ],
        },
        {
            "name": "HDMI-A-2",
            "make": None,
            "model": None,
            "enabled": True,
            "modes": [
                {"width": 640, "height": 480, "refresh": 60.0, "preferred": False, "current": True}
            ],
        },
    ]
    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.subprocess.run",
        lambda cmd, *a, **k: _result(json.dumps(outputs)),
    )
    assert Pi3dBackend()._detect_wlr_output() == "HDMI-A-1"


def test_env_override_wins(wlr_available, monkeypatch):
    """METIXEL_WLR_OUTPUT explicitly selects the output to control."""
    monkeypatch.setenv("METIXEL_WLR_OUTPUT", "HDMI-A-1")
    backend = Pi3dBackend()
    assert backend._resolve_wlr_output() == "HDMI-A-1"
    assert backend.connected_output() == "HDMI-A-1"
    monkeypatch.delenv("METIXEL_WLR_OUTPUT")


def test_wlr_randr_targets_detected_output(wlr_available):
    """The toggle command targets the auto-detected output (HDMI-A-2)."""
    backend = Pi3dBackend()
    assert backend._wlr_randr(False) is True
    # First call is auto-detection (--json); the toggle is the last call.
    assert wlr_available[-1] == [
        "/usr/bin/wlr-randr",
        "--output",
        "HDMI-A-2",
        "--off",
    ]


def test_wlr_randr_redetects_after_unknown_output(monkeypatch):
    """If the cached output no longer exists, re-detect and retry once."""
    results = iter(
        [
            _result("", returncode=1, stderr="unknown output HDMI-A-1"),
            _result(_fake_outputs()),  # detection
            _result(""),  # retry with newly detected output
        ]
    )
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return next(results)

    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.dispmanx_backend.subprocess.run", fake_run)

    backend = Pi3dBackend()
    backend._wlr_output = "HDMI-A-1"  # stale cached output
    assert backend._wlr_randr(True) is True
    # First toggle failed on HDMI-A-1, then re-detected and retried HDMI-A-2
    assert calls[-1] == ["/usr/bin/wlr-randr", "--output", "HDMI-A-2", "--on"]


def test_wlr_randr_missing_binary(monkeypatch):
    """Gracefully returns False when wlr-randr is not installed."""
    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: False,
    )
    backend = Pi3dBackend()
    assert backend._wlr_randr(False) is False


def test_disable_empty_outputs(monkeypatch):
    """Phantom outputs (no EDID) get disabled; the real monitor stays on."""
    outputs = [
        {"name": "HDMI-A-1", "make": None, "model": None, "enabled": True, "modes": []},
        {
            "name": "HDMI-A-2",
            "make": "Dell Inc.",
            "model": "DELL U2412M",
            "enabled": True,
            "modes": [],
        },
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd == ["/usr/bin/wlr-randr", "--json"]:
            return _result(json.dumps(outputs))
        return _result("")

    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.dispmanx_backend.subprocess.run", fake_run)

    Pi3dBackend()._disable_empty_outputs()
    assert calls[-1] == ["/usr/bin/wlr-randr", "--output", "HDMI-A-1", "--off"]


def test_disable_empty_outputs_keeps_real_monitor(monkeypatch):
    """Cleanup only turns off non-EDID outputs."""
    outputs = [
        {"name": "HDMI-A-1", "make": None, "model": None, "enabled": True, "modes": []},
        {
            "name": "HDMI-A-2",
            "make": "Dell Inc.",
            "model": "DELL U2412M",
            "enabled": True,
            "modes": [],
        },
    ]
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd == ["/usr/bin/wlr-randr", "--json"]:
            return _result(json.dumps(outputs))
        return _result("")

    monkeypatch.setattr(
        "metixel.display.dispmanx_backend.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.dispmanx_backend.subprocess.run", fake_run)

    Pi3dBackend()._disable_empty_outputs()
    # Never issues an --off for the real monitor
    assert all("HDMI-A-2" not in c for c in calls)


def test_disable_empty_outputs_skipped_with_override(monkeypatch):
    """METIXEL_WLR_OUTPUT skips the phantom-output cleanup entirely."""
    monkeypatch.setenv("METIXEL_WLR_OUTPUT", "HDMI-A-1")

    def boom(*a, **k):
        raise AssertionError("should not touch wlr-randr when overridden")

    monkeypatch.setattr("metixel.display.dispmanx_backend.os.path.exists", boom)
    monkeypatch.setattr("metixel.display.dispmanx_backend.subprocess.run", boom)

    Pi3dBackend()._disable_empty_outputs()  # must return early
    monkeypatch.delenv("METIXEL_WLR_OUTPUT")
