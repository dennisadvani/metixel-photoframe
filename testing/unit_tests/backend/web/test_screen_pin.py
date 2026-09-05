# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Screen-PIN service + routes — string compare, attempt limit, cooldown, length."""

from __future__ import annotations

import json
import time
from unittest import mock

import pytest

from metixel.backend.web.auth import (
    MAX_PIN_ATTEMPTS,
    PIN_COOLDOWN_SECONDS,
    SCREEN_PIN_TIMEOUT_MAX_MINUTES,
    ScreenPinService,
)
from metixel.shared.security import hash_secret


class _FakeState:
    """Minimal StateManager-like fake exposing config + update_config."""

    def __init__(self, web: dict | None = None):
        from metixel.shared.config import Config

        self.config = Config()
        if web:
            self.config.update("web", web)

    def update_config(self, section: str, values: dict) -> None:
        self.config.update(section, values)


class TestScreenPinService:
    def test_disabled_by_default(self):
        svc = ScreenPinService(_FakeState())
        assert svc.is_enabled() is False
        ok, msg = svc.validate("1234")
        assert ok is False
        assert "No screen PIN set" in msg

    def test_set_and_validate(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        assert svc.is_enabled() is True
        ok, msg = svc.validate("123456")
        assert ok is True
        assert msg == "ok"

    def test_wrong_pin_rejected(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        ok, _ = svc.validate("000000")
        assert ok is False

    def test_leading_zeros_preserved(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("0123")
        ok, _ = svc.validate("0123")
        assert ok is True
        # "123" (int coercion) must NOT match.
        ok2, _ = svc.validate("123")
        assert ok2 is False

    def test_stored_as_hash_not_plaintext(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        stored = state.config.web.get("screen_pin")
        assert "123456" not in stored
        assert stored.startswith("scrypt$")

    def test_attempt_limit_triggers_cooldown(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        for _ in range(MAX_PIN_ATTEMPTS):
            ok, _ = svc.validate("000000")
            assert ok is False
        # Now locked — even the correct PIN is rejected during cooldown.
        ok, msg = svc.validate("123456")
        assert ok is False
        assert "Too many attempts" in msg

    def test_cooldown_expires(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        for _ in range(MAX_PIN_ATTEMPTS):
            svc.validate("000000")
        # Simulate cooldown elapsing.
        svc._pin_locked_until = time.monotonic() - 1
        ok, _ = svc.validate("123456")
        assert ok is True

    def test_clear_pin(self):
        state = _FakeState()
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        svc.clear_pin()
        assert svc.is_enabled() is False

    def test_timeout_capped_at_24h(self):
        state = _FakeState({"screen_pin_timeout_minutes": 99999})
        svc = ScreenPinService(state)
        assert svc.timeout_minutes() == SCREEN_PIN_TIMEOUT_MAX_MINUTES

    def test_timeout_default(self):
        svc = ScreenPinService(_FakeState())
        assert svc.timeout_minutes() == 60

    def test_unlock_window(self):
        state = _FakeState({"screen_pin_timeout_minutes": 5})
        svc = ScreenPinService(state)
        svc.set_pin("123456")
        assert svc.is_unlocked() is False
        svc.validate("123456")
        assert svc.is_unlocked() is True
        svc.lock()
        assert svc.is_unlocked() is False


class TestScreenPinRoutes:
    def _login(self, client, mock_state):
        mock_state.update_config("web", {"password": hash_secret("secret123")})
        client.post("/api/auth/login", json={"password": "secret123"})

    def test_set_pin(self, client, mock_state):
        self._login(client, mock_state)
        resp = client.post(
            "/api/auth/screen-pin", json={"pin": "123456", "confirm": "123456"}
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"

    def test_clear_pin(self, client, mock_state):
        self._login(client, mock_state)
        client.post("/api/auth/screen-pin", json={"pin": "123456", "confirm": "123456"})
        resp = client.post("/api/auth/screen-pin", json={"clear": True})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"

    def test_invalid_length(self, client, mock_state):
        self._login(client, mock_state)
        resp = client.post(
            "/api/auth/screen-pin", json={"pin": "123", "confirm": "123"}
        )
        assert resp.status_code == 400

    def test_mismatch(self, client, mock_state):
        self._login(client, mock_state)
        resp = client.post(
            "/api/auth/screen-pin", json={"pin": "123456", "confirm": "654321"}
        )
        assert resp.status_code == 400

    def test_status(self, client, mock_state):
        self._login(client, mock_state)
        resp = client.get("/api/auth/screen-pin/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is False

    def test_requires_auth(self, client, mock_state):
        # Set a password but do NOT log in — the auth gate should block.
        mock_state.update_config("web", {"password": hash_secret("secret123")})
        resp = client.post(
            "/api/auth/screen-pin", json={"pin": "123456", "confirm": "123456"}
        )
        assert resp.status_code == 401