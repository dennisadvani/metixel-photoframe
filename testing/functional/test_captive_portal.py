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

import json
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.functional

BACKEND_PORT = 8080
BASE = f"http://127.0.0.1:{BACKEND_PORT}"


def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


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


@pytest.fixture(scope="module")
def ap_active() -> bool:
    return _ap_active()


def test_pin_requires_4_digits(ap_active: bool) -> None:
    """A non-4-digit PIN must be rejected with a 400."""
    if not ap_active:
        pytest.skip("AP/captive portal not active — no PIN to validate")

    status, body = _api_post("/api/network/validate-pin", {"pin": "12"})
    assert status == 400, f"expected 400 for short PIN, got {status}: {body}"
    assert body.get("valid") is False


def test_wrong_pin_rejected(ap_active: bool) -> None:
    """A wrong PIN must be rejected with a 403 and a message."""
    if not ap_active:
        pytest.skip("AP/captive portal not active — no PIN to validate")

    status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
    assert status == 403, f"expected 403 for wrong PIN, got {status}: {body}"
    assert body.get("valid") is False
    assert "attempt" in body.get("message", "").lower() or "lock" in body.get("message", "").lower()


def test_pin_locks_after_three_attempts(ap_active: bool) -> None:
    """After 3 wrong attempts the PIN must be locked for a cooldown."""
    if not ap_active:
        pytest.skip("AP/captive portal not active — no PIN to validate")

    # Three wrong attempts.
    for _ in range(3):
        status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
        assert status == 403, f"expected 403, got {status}: {body}"

    # The 4th attempt must report a lockout.
    status, body = _api_post("/api/network/validate-pin", {"pin": "0000"})
    assert status == 403, f"expected 403 on locked PIN, got {status}: {body}"
    assert "lock" in body.get("message", "").lower(), f"expected lockout message, got: {body}"
