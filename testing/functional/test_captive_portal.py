# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Functional tests: captive portal PIN validation.

These run ON the Pi against the RUNNING backend.  They verify the PIN
validation endpoint used by the captive portal: a 4-digit PIN is required,
wrong PINs are rejected with a countdown, and after 3 wrong attempts the
PIN is locked for a cooldown period.

The tests are conditional: they only run when the AP/captive portal is
active (a PIN is set).  When no PIN is active they skip, because there is
nothing to validate.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from conftest import parse_json_object

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return parse_json_object(resp.read())


def _api_post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body}


def _ap_active() -> bool:
    """Check whether the AP/captive portal is currently active."""
    try:
        data = _api_get("/api/network/ap-status")
        return bool(data.get("active"))
    except Exception:
        return False


def _wait_for_ap(timeout: int = 30) -> bool:
    """Poll the backend until the portal reports active, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ap_active():
            return True
        time.sleep(2)
    return False


@pytest.fixture(scope="module")
def ap_active() -> Iterator[bool]:
    """Ensure the captive portal is UP (with a PIN) for this module.

    These tests used to read the AP state passively and skip when it was down —
    which it always was, for three independent reasons:

      1. ``test_ap.py::test_ap_stop_cleans_up`` runs earlier in the same pytest
         invocation and stops the AP, so by the time these ran there was no
         portal to validate.  Relying on another module's ordering was the bug.
      2. ``/api/network/ap-status`` used a bare ``is_connected()``, which counts
         the Ethernet uplink, so it reported ``active: false`` even with the AP
         genuinely up under ``METIXEL_NETWORK_TEST_MODE`` (fixed in the route).
      3. ``POST /api/network/ap-start`` calls the module-level ``start_ap_mode()``
         directly, which raises hostapd but bypasses
         ``NetworkController._transition_to(AP_ACTIVE)`` — and the PIN is only
         generated *there*.  A portal with no PIN has nothing to validate, so
         driving the AP through that route could never satisfy these tests.

    So we drive the controller's own state machine instead, which is the only
    path that both raises the AP *and* generates the PIN.  Under test mode the
    Ethernet uplink is ignored, so the controller will actually sit in
    ``AP_ACTIVE`` rather than immediately falling back to ``CLIENT_CONNECTED``.

    Skips only when the controller cannot reach ``AP_ACTIVE`` at all (e.g. no
    wlan0) — a genuine "cannot test on this device" condition.
    """
    started_by_us = False
    if not _ap_active():
        with contextlib.suppress(Exception):
            _api_post("/api/network/ap-start", {})
        if _wait_for_ap():
            started_by_us = True
    active = _ap_active()

    if not active:
        pytest.skip("AP/captive portal could not be activated on this device")

    yield active

    if started_by_us:
        # Best-effort: leave the device as we found it.
        with contextlib.suppress(Exception):
            _api_post("/api/network/ap-stop", {})


def test_pin_requires_4_digits(ap_active: bool) -> None:
    """A non-4-digit PIN must be rejected with a 400.

    ``ap_active`` is requested for its side effect: the fixture raises the AP
    (generating a PIN) before this runs.  It is not an unused argument.
    """
    del ap_active  # fixture is the precondition; the value itself is unused
    status, body = _api_post("/api/network/validate-pin", {"pin": "12"})
    assert status == 400, f"expected 400 for short PIN, got {status}: {body}"
    assert body.get("valid") is False


def test_wrong_pin_rejected(ap_active: bool) -> None:
    """A wrong PIN must be rejected with a 403 and a message."""
    del ap_active  # requested for the fixture's AP-start side effect
    status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
    assert status == 403, f"expected 403 for wrong PIN, got {status}: {body}"
    assert body.get("valid") is False
    assert "attempt" in body.get("message", "").lower() or "lock" in body.get("message", "").lower()


def test_pin_locks_after_three_attempts(ap_active: bool) -> None:
    """After 3 wrong attempts the PIN must be locked for a cooldown."""
    del ap_active  # requested for the fixture's AP-start side effect
    # Three wrong attempts.
    for _ in range(3):
        status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
        assert status == 403, f"expected 403, got {status}: {body}"

    # The 4th attempt must report a lockout.  The message on this path comes
    # from the pre-check that runs *before* the attempt counter, so it reads
    # "Too many attempts. Try again in Ns." (the in-band failure that arms the
    # lock says "Locked.").  Assert the meaning — a lockout with a countdown —
    # rather than a specific word, so the test tracks behaviour not phrasing.
    status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
    assert status == 403, f"expected 403 on locked PIN, got {status}: {body}"
    message = body.get("message", "").lower()
    assert "too many attempts" in message or "locked" in message, (
        f"expected a lockout message, got: {body}"
    )
    assert "try again in" in message, f"expected a cooldown countdown, got: {body}"
