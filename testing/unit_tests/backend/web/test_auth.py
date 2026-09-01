# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web auth endpoints — login/logout/me, the auth gate, and exemptions."""

from __future__ import annotations

import json
from unittest import mock

import pytest


def _set_web_password(mock_state, password: str) -> None:
    """Persist a web password hash into the state's config."""
    from metixel.shared.security import hash_secret

    mock_state.update_config("web", {"password": hash_secret(password)})


class TestAuthDisabled:
    def test_me_reports_disabled(self, client):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is False
        assert data["authenticated"] is False

    def test_protected_route_open_when_disabled(self, client):
        # With no password, /api/config is reachable without a session.
        resp = client.get("/api/config")
        assert resp.status_code == 200


class TestLogin:
    def test_login_success(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/auth/login", json={"password": "secret123"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["authenticated"] is True

    def test_login_wrong_password(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/auth/login", json={"password": "wrong"})
        assert resp.status_code == 401
        data = json.loads(resp.data)
        assert data.get("authenticated") is not True

    def test_login_missing_password(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 400

    def test_login_when_disabled(self, client):
        resp = client.post("/api/auth/login", json={"password": "anything"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["authenticated"] is True

    def test_login_lockout_after_attempts(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        for _ in range(5):
            client.post("/api/auth/login", json={"password": "wrong"})
        resp = client.post("/api/auth/login", json={"password": "secret123"})
        assert resp.status_code == 429
        data = json.loads(resp.data)
        assert data.get("locked") is True


class TestAuthGate:
    def test_protected_route_requires_auth(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.get("/api/config")
        assert resp.status_code == 401

    def test_authenticated_session_can_access(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_health_exempt(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_slideshow_started_exempt(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/slideshow-started")
        assert resp.status_code == 200

    def test_network_exempt(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.get("/api/network/status")
        assert resp.status_code == 200

    def test_control_exempt(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/control", json={"cmd": "next"})
        assert resp.status_code == 200

    def test_password_change_requires_auth(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        resp = client.post("/api/auth/password", json={"password": "newpass123"})
        assert resp.status_code == 401


class TestLogout:
    def test_logout_clears_session(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        assert client.get("/api/config").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/config").status_code == 401


class TestSetPassword:
    def test_set_password(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        resp = client.post("/api/auth/password", json={"password": "newpass123"})
        assert resp.status_code == 200
        # Old password no longer works.
        client.post("/api/auth/logout")
        assert client.post("/api/auth/login", json={"password": "secret123"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "newpass123"}).status_code == 200

    def test_clear_password(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        resp = client.post("/api/auth/password", json={"password": ""})
        assert resp.status_code == 200
        # Auth now disabled — protected route open.
        client.post("/api/auth/logout")
        assert client.get("/api/config").status_code == 200

    def test_short_password_rejected(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        resp = client.post("/api/auth/password", json={"password": "short"})
        assert resp.status_code == 400


class TestSessionTimeout:
    def test_timeout_expires_session(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        mock_state.update_config("web", {"session_timeout_minutes": 1})
        client.post("/api/auth/login", json={"password": "secret123"})
        assert client.get("/api/config").status_code == 200

        # Rewrite the session's login_time to be 2 minutes in the past.
        with client.session_transaction() as sess:
            sess["login_time"] = sess["login_time"] - 120
        assert client.get("/api/config").status_code == 401

    def test_zero_timeout_is_forever(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        mock_state.update_config("web", {"session_timeout_minutes": 0})
        client.post("/api/auth/login", json={"password": "secret123"})
        assert client.get("/api/config").status_code == 200

        # Even a very old login_time stays valid when timeout is 0.
        with client.session_transaction() as sess:
            sess["login_time"] = sess["login_time"] - 999999
        assert client.get("/api/config").status_code == 200