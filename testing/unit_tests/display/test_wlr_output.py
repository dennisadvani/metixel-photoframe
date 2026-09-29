# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Tests for wlr-randr output auto-detection and display-power control.

The Raspberry Pi exposes two HDMI connectors (e.g. ``HDMI-A-1`` and
``HDMI-A-2``) but only one usually has a real monitor attached.  The
backend must target the output with a real EDID (non-null make/model)
instead of blindly using the first connector.
"""

import json
from types import SimpleNamespace

import pytest

from metixel.display.hardware import WlrOutput


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
        "metixel.display.hardware.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.hardware.subprocess.run", fake_run)
    return calls


def test_detect_picks_real_monitor(wlr_available):
    """HDMI-A-2 (real EDID) is chosen over the phantom HDMI-A-1 port.

    ``detect()`` is the bare detection helper (a staticmethod, callable without
    an instance); ``resolve()`` layers the ``METIXEL_WLR_OUTPUT`` override on top
    of it.  With no override set, the two must agree.
    """
    assert WlrOutput.detect() == "HDMI-A-2"
    assert WlrOutput().resolve() == "HDMI-A-2"


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
        "metixel.display.hardware.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr(
        "metixel.display.hardware.subprocess.run",
        lambda cmd, *a, **k: _result(json.dumps(outputs)),
    )
    assert WlrOutput.detect() == "HDMI-A-1"


def test_env_override_wins(wlr_available, monkeypatch):
    """METIXEL_WLR_OUTPUT explicitly selects the output to control.

    ``resolve()`` is the override-aware accessor; ``detect()`` deliberately
    ignores the environment variable so a caller can always ask what is actually
    plugged in.  Asserting the override on ``detect()`` would conflate the two.
    """
    monkeypatch.setenv("METIXEL_WLR_OUTPUT", "HDMI-A-1")
    try:
        wlr = WlrOutput()
        assert wlr.resolve() == "HDMI-A-1"
        # detect() still reports the real monitor, independent of the override.
        assert WlrOutput.detect() == "HDMI-A-2"
    finally:
        monkeypatch.delenv("METIXEL_WLR_OUTPUT")


def test_wlr_randr_targets_detected_output(wlr_available):
    """Turning the panel OFF targets the auto-detected output (HDMI-A-2)."""
    wlr = WlrOutput()
    assert wlr.set_power(False) is True
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
        "metixel.display.hardware.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.hardware.subprocess.run", fake_run)

    wlr = WlrOutput()
    wlr._set_cached("HDMI-A-1")  # stale cached output
    # Turning the panel ON: the cached output has gone, so the call must
    # re-detect and retry against the real monitor.
    assert wlr.set_power(True) is True
    # First toggle failed on HDMI-A-1, then re-detected and retried HDMI-A-2
    assert calls[-1] == ["/usr/bin/wlr-randr", "--output", "HDMI-A-2", "--on"]


def test_wlr_randr_missing_binary(monkeypatch):
    """Gracefully returns False when wlr-randr is not installed."""
    monkeypatch.setattr(
        "metixel.display.hardware.os.path.exists",
        lambda p: False,
    )
    wlr = WlrOutput()
    # No binary at all, so neither direction can succeed.
    assert wlr.set_power(True) is False
    assert wlr.set_power(False) is False


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
        "metixel.display.hardware.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.hardware.subprocess.run", fake_run)

    WlrOutput().disable_empty_outputs()
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
        "metixel.display.hardware.os.path.exists",
        lambda p: p == "/usr/bin/wlr-randr",
    )
    monkeypatch.setattr("metixel.display.hardware.subprocess.run", fake_run)

    WlrOutput().disable_empty_outputs()
    # Never issues an --off for the real monitor
    assert all("HDMI-A-2" not in c for c in calls)


def test_disable_empty_outputs_skipped_with_override(monkeypatch):
    """METIXEL_WLR_OUTPUT skips the phantom-output cleanup entirely."""
    monkeypatch.setenv("METIXEL_WLR_OUTPUT", "HDMI-A-1")

    def boom(*a, **k):
        raise AssertionError("should not touch wlr-randr when overridden")

    monkeypatch.setattr("metixel.display.hardware.os.path.exists", boom)
    monkeypatch.setattr("metixel.display.hardware.subprocess.run", boom)

    WlrOutput().disable_empty_outputs()  # must return early
    monkeypatch.delenv("METIXEL_WLR_OUTPUT")


# ---------------------------------------------------------------------------
# wlr-randr set_mode (resolution / refresh rate / rotation)
# ---------------------------------------------------------------------------


def test_wlr_transform_mapping():
    """Clockwise rotations map to wlr-randr --transform values."""
    from metixel.display.hardware import _wlr_transform

    assert _wlr_transform(0) == "normal"
    assert _wlr_transform(90) == "90"
    assert _wlr_transform(180) == "180"
    assert _wlr_transform(270) == "270"
    # Unsupported values fall back to normal
    assert _wlr_transform(45) == "normal"


def test_set_mode_applies_refresh_and_rotation(wlr_available):
    """set_mode combines refresh rate into the mode string + rotation."""
    wlr = WlrOutput()
    assert wlr.set_mode(width=1920, height=1080, refresh_rate=60, rotation=90) is True
    # First call is auto-detection (--json); the mode-set is the last call.
    assert wlr_available[-1] == [
        "/usr/bin/wlr-randr",
        "--output",
        "HDMI-A-2",
        "--mode",
        "1920x1080@60Hz",
        "--transform",
        "90",
    ]


def test_set_mode_applies_resolution(wlr_available):
    """set_mode builds the correct command for a resolution override."""
    wlr = WlrOutput()
    assert wlr.set_mode(width=1920, height=1080) is True
    assert wlr_available[-1] == [
        "/usr/bin/wlr-randr",
        "--output",
        "HDMI-A-2",
        "--mode",
        "1920x1080",
    ]


def test_set_mode_refresh_without_resolution_skips_mode(wlr_available):
    """A refresh rate without width/height cannot be expressed — no --mode."""
    wlr = WlrOutput()
    assert wlr.set_mode(refresh_rate=60) is True
    assert wlr_available[-1] == ["/usr/bin/wlr-randr", "--output", "HDMI-A-2"]


def test_list_modes_returns_real_monitor_modes(wlr_available):
    """list_modes returns the resolved output's supported modes."""
    wlr = WlrOutput()
    modes = wlr.list_modes()
    # The fake outputs have HDMI-A-2 (real monitor) with 2 modes.
    assert len(modes) == 2
    assert modes[0]["width"] == 1920
    assert modes[0]["height"] == 1200
    assert modes[0]["preferred"] is True


def test_list_modes_empty_when_no_binary(monkeypatch):
    """list_modes returns [] when wlr-randr is not installed."""
    monkeypatch.setattr(
        "metixel.display.hardware.os.path.exists",
        lambda p: False,
    )
    wlr = WlrOutput()
    assert wlr.list_modes() == []


def test_set_mode_noop_when_nothing_requested(wlr_available):
    """set_mode with all defaults still targets the output (no mode flags)."""
    wlr = WlrOutput()
    assert wlr.set_mode() is True
    assert wlr_available[-1] == ["/usr/bin/wlr-randr", "--output", "HDMI-A-2"]


def test_set_mode_missing_binary(monkeypatch):
    """Gracefully returns False when wlr-randr is not installed."""
    monkeypatch.setattr(
        "metixel.display.hardware.os.path.exists",
        lambda p: False,
    )
    wlr = WlrOutput()
    assert wlr.set_mode(refresh_rate=60, rotation=90) is False
