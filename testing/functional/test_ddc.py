# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: DDC/CI monitor control.

These run ON the Pi against the RUNNING backend's DDC/CI API (`/api/ddc/*`).
They verify the whole DDC path end-to-end against real hardware:

    1. status            — DDC/CI is enabled, available, and a monitor is seen
    2. capabilities      — probe the monitor and get its VCP features
    3. brightness write  — set a test brightness and READ IT BACK to confirm
                           the write actually took effect on the monitor
    4. contrast write    — same round-trip for contrast
    5. factory reset     — POST /api/ddc/reset LAST to restore the monitor to
                           its factory defaults (so the test leaves the
                           display at defaults, not the test values)

If DDC is unavailable (no monitor, no ddcutil, disabled) the suite skips —
it cannot run without a real DDC-capable monitor.  Brightness (0x10) and
contrast (0x12) are used because they are user-facing, continuous features
present on virtually every DD/CI-capable display.

The factory-reset test runs last (pytest file order) so whatever the write
tests changed is reverted to factory defaults before the suite ends.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

import pytest

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"

#: VCP codes under test (brightness + contrast — user-facing, continuous).
#: VCP codes are hex feature codes (0x10 = 16, 0x12 = 18 decimal).
VCP_BRIGHTNESS = 0x10
VCP_CONTRAST = 0x12

#: A test target value safely within the typical continuous range.  We pick a
#: value that differs from whatever the monitor is currently set to, so the
#: readback actually proves a write happened.  The factory-reset test restores
#: the default afterwards regardless.
_BRIGHTNESS_TEST = 50
_CONTRAST_TEST = 50


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def _api_put(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _api_post(path: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


@pytest.fixture(scope="module")
def ddc_available() -> dict:
    """Probe DDC availability; skip the suite if no monitor is reachable."""
    try:
        status = _api_get("/api/ddc/status")
    except urllib.error.URLError:
        pytest.skip("DDC status endpoint unreachable")
    if not status.get("available"):
        pytest.skip(f"DDC/CI unavailable: {status.get('reason', 'unknown reason')}")
    if not status.get("monitors"):
        pytest.skip("No DDC-capable monitor detected")
    return status


def test_ddc_status_lists_monitor(ddc_available: dict) -> None:
    """The DDC status endpoint must report DDC enabled + a real monitor."""
    status = _api_get("/api/ddc/status")
    assert status.get("available") is True, status
    assert status.get("enabled") is True, status
    assert status.get("monitors"), f"no monitors reported: {status}"
    monitor = status["monitors"][0]
    # The monitor should carry a model + display id.
    assert monitor.get("display"), f"monitor missing display id: {monitor}"
    assert monitor.get("model"), f"monitor missing model: {monitor}"
    logger.info("DDC monitor: %s (display %s)", monitor.get("model"), monitor.get("display"))


def test_ddc_capabilities_expose_brightness(ddc_available: dict) -> None:
    """The capabilities probe must expose brightness + contrast features."""
    caps = _api_get("/api/ddc/capabilities")
    assert caps.get("available") is True, caps
    features = caps.get("features", [])
    codes = {f.get("code") for f in features}
    assert VCP_BRIGHTNESS in codes, f"brightness (0x{VCP_BRIGHTNESS:02X}) not in features: {sorted(codes)}"
    assert VCP_CONTRAST in codes, f"contrast (0x{VCP_CONTRAST:02X}) not in features: {sorted(codes)}"


def _read_vcp(code: int) -> dict:
    return _api_get(f"/api/ddc/vcp/{code:02X}")


def test_ddc_brightness_read_write_roundtrip(ddc_available: dict) -> None:
    """Set brightness, then read it back to confirm the write took effect.

    We write a value and immediately poll the readback until it matches (the
    monitor may settle over a moment).  The factory-reset test restores the
    default afterwards.
    """
    # Read the current value first (so we know the monitor range / shape).
    before = _read_vcp(VCP_BRIGHTNESS)
    assert "current" in before, f"unexpected getvcp shape: {before}"
    maximum = before.get("maximum") or 100

    target = min(_BRIGHTNESS_TEST, maximum)
    assert target != before["current"], (
        f"brightness already at test value ({target}) — cannot prove a write"
    )

    written = _api_put(f"/api/ddc/vcp/{VCP_BRIGHTNESS:02X}", {"value": target})
    assert written.get("status", "ok") == "ok", f"setvcp failed: {written}"

    # Read back (poll briefly for the monitor to settle).
    deadline = time.monotonic() + 15
    readback = _read_vcp(VCP_BRIGHTNESS)
    while readback.get("current") != target and time.monotonic() < deadline:
        time.sleep(1)
        readback = _read_vcp(VCP_BRIGHTNESS)

    assert readback.get("current") == target, (
        f"brightness readback did not match written value {target}: {readback}"
    )
    logger.info("brightness write+readback OK: %s → %s", before["current"], target)


def test_ddc_contrast_read_write_roundtrip(ddc_available: dict) -> None:
    """Set contrast, then read it back to confirm the write took effect."""
    before = _read_vcp(VCP_CONTRAST)
    assert "current" in before, f"unexpected getvcp shape: {before}"
    maximum = before.get("maximum") or 100

    target = min(_CONTRAST_TEST, maximum)
    assert target != before["current"], (
        f"contrast already at test value ({target}) — cannot prove a write"
    )

    written = _api_put(f"/api/ddc/vcp/{VCP_CONTRAST:02X}", {"value": target})
    assert written.get("status", "ok") == "ok", f"setvcp failed: {written}"

    deadline = time.monotonic() + 15
    readback = _read_vcp(VCP_CONTRAST)
    while readback.get("current") != target and time.monotonic() < deadline:
        time.sleep(1)
        readback = _read_vcp(VCP_CONTRAST)

    assert readback.get("current") == target, (
        f"contrast readback did not match written value {target}: {readback}"
    )
    logger.info("contrast write+readback OK: %s → %s", before["current"], target)


def test_ddc_factory_reset_restores_defaults(ddc_available: dict) -> None:
    """Factory-reset the monitor (last) to revert to defaults.

    POST /api/ddc/reset performs a VCP factory reset (0x04).  We confirm it
    succeeds and that the reset actually changed the picture settings — the
    brightness value observed just before the reset (set by an earlier test)
    should not be identical after it.
    """
    # Capture the value the previous write test left behind.
    before = _read_vcp(VCP_BRIGHTNESS).get("current")

    result = _api_post("/api/ddc/reset")
    assert result.get("status", "ok") == "ok", f"factory reset failed: {result}"

    # Read brightness back after the reset; it should have reverted from the
    # value the write test set.  Only assert a change when we have a baseline
    # to compare against (avoids false failures if the default equals the test
    # value on some monitor).
    time.sleep(1)  # let the monitor settle after reset
    readback = _read_vcp(VCP_BRIGHTNESS).get("current")

    if before is not None:
        assert readback != before, (
            f"brightness unchanged ({before}) after factory reset: {readback}"
        )
    logger.info("factory reset OK: brightness %s → %s", before, readback)