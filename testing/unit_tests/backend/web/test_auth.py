# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Web auth endpoints — login/logout/me, the auth gate, and exemptions."""

from __future__ import annotations

import json


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

    def test_network_status_requires_auth_when_ap_inactive(self, client, mock_state, monkeypatch):
        """/api/network/* is no longer blanket-exempt: with no AP/PIN gate up
        the captive-portal paths need a session like everything else."""
        import metixel.backend.web.server as server_mod

        monkeypatch.setattr(server_mod, "_is_ap_mode", lambda: False)
        _set_web_password(mock_state, "secret123")
        resp = client.get("/api/network/status")
        assert resp.status_code == 401

    def test_captive_portal_paths_exempt_while_ap_active(self, client, mock_state, monkeypatch):
        """The four routes captive.html calls stay reachable while the setup
        hotspot / PIN gate is active (a phone on the AP cannot log in)."""
        import metixel.backend.web.routes.network as net_mod
        import metixel.backend.web.server as server_mod

        monkeypatch.setattr(server_mod, "_is_ap_mode", lambda: True)
        monkeypatch.setattr(net_mod, "get_connection_status", lambda: {"ip": ""})
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: True)
        monkeypatch.setattr(net_mod, "is_connected", lambda: False)
        monkeypatch.setattr(net_mod, "scan_networks", lambda: [])
        _set_web_password(mock_state, "secret123")

        assert client.get("/api/network/status").status_code == 200
        assert client.get("/api/network/scan").status_code == 200
        # validate-pin: reaches the route (400 = "enter a 4-digit PIN", not 401)
        assert client.post("/api/network/validate-pin", json={"pin": "x"}).status_code == 400
        # connect: reaches the route (400 = "SSID is required", not 401)
        assert client.post("/api/network/connect", json={}).status_code == 400

    def test_non_portal_network_routes_require_auth_even_in_ap_mode(
        self, client, mock_state, monkeypatch
    ):
        import metixel.backend.web.server as server_mod

        monkeypatch.setattr(server_mod, "_is_ap_mode", lambda: True)
        _set_web_password(mock_state, "secret123")
        assert client.post("/api/network/forget", json={"ssid": "x"}).status_code == 401
        assert client.post("/api/network/radio", json={"enabled": True}).status_code == 401
        assert client.post("/api/network/ap-start").status_code == 401
        assert client.post("/api/network/ap-stop").status_code == 401
        assert client.get("/api/network/ap-status").status_code == 401

    def test_control_exempt_for_loopback_only(self, client, mock_state):
        """/api/control is exempt only for local (loopback) callers."""
        _set_web_password(mock_state, "secret123")
        # The Flask test client defaults to REMOTE_ADDR=127.0.0.1.
        resp = client.post("/api/control", json={"cmd": "next"})
        assert resp.status_code == 200
        resp = client.post(
            "/api/control",
            json={"cmd": "next"},
            environ_base={"REMOTE_ADDR": "::1"},
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/control",
            json={"cmd": "next"},
            environ_base={"REMOTE_ADDR": "192.168.1.50"},
        )
        assert resp.status_code == 401

    def test_control_from_lan_works_with_session(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        client.post("/api/auth/login", json={"password": "secret123"})
        resp = client.post(
            "/api/control",
            json={"cmd": "next"},
            environ_base={"REMOTE_ADDR": "192.168.1.50"},
        )
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


class TestSessionInvalidation:
    """Changing or clearing the password must invalidate other sessions."""

    def test_other_session_invalidated_on_password_change(self, app, mock_state):
        _set_web_password(mock_state, "secret123")
        first = app.test_client()
        second = app.test_client()
        first.post("/api/auth/login", json={"password": "secret123"})
        second.post("/api/auth/login", json={"password": "secret123"})
        assert second.get("/api/config").status_code == 200

        resp = first.post("/api/auth/password", json={"password": "newpass123"})
        assert resp.status_code == 200
        # The session that made the change stays logged in ...
        assert first.get("/api/config").status_code == 200
        # ... every other pre-existing session is rejected.
        assert second.get("/api/config").status_code == 401
        # And a fresh login with the new password works.
        second.post("/api/auth/login", json={"password": "newpass123"})
        assert second.get("/api/config").status_code == 200

    def test_stale_session_rejected_after_clear_and_reset(self, app, mock_state):
        _set_web_password(mock_state, "secret123")
        first = app.test_client()
        second = app.test_client()
        first.post("/api/auth/login", json={"password": "secret123"})
        second.post("/api/auth/login", json={"password": "secret123"})

        assert first.post("/api/auth/password", json={"password": ""}).status_code == 200
        # Auth disabled: open to everyone.
        assert second.get("/api/config").status_code == 200
        # Password set again out-of-band: the old cookie must not count.
        _set_web_password(mock_state, "again12345")
        assert second.get("/api/config").status_code == 401

    def test_generation_rotates_on_set_and_clear(self, mock_state):
        from metixel.backend.web.auth import WebAuthService

        svc = WebAuthService(mock_state)
        assert svc.generation() == ""
        svc.set_password("secret123")
        g1 = svc.generation()
        assert g1
        svc.set_password("secret456")
        g2 = svc.generation()
        assert g2 and g2 != g1
        svc.clear_password()
        assert svc.generation() not in ("", g1, g2)

    def test_non_string_password_rejected(self, client, mock_state):
        _set_web_password(mock_state, "secret123")
        assert client.post("/api/auth/login", json={"password": 123}).status_code == 400
        client.post("/api/auth/login", json={"password": "secret123"})
        assert client.post("/api/auth/password", json={"password": ["x"]}).status_code == 400


class TestCsrfGate:
    """State-changing /api/* requests must come from this origin."""

    def test_cross_origin_post_rejected(self, client):
        resp = client.post(
            "/api/config/reload",
            headers={"Origin": "http://evil.example"},
        )
        assert resp.status_code == 403
        assert json.loads(resp.data)["status"] == "error"

    def test_same_origin_post_allowed(self, client):
        resp = client.post("/api/config/reload", headers={"Origin": "http://localhost"})
        assert resp.status_code == 200

    def test_same_origin_with_port_allowed(self, client):
        resp = client.post(
            "/api/config/reload",
            headers={"Origin": "http://metixel.local:8080", "Host": "metixel.local:8080"},
        )
        assert resp.status_code == 200

    def test_origin_null_rejected(self, client):
        resp = client.post("/api/config/reload", headers={"Origin": "null"})
        assert resp.status_code == 403

    def test_cross_origin_referer_rejected_when_no_origin(self, client):
        resp = client.put(
            "/api/config/slideshow",
            json={"shuffle": False},
            headers={"Referer": "http://evil.example/page"},
        )
        assert resp.status_code == 403

    def test_same_origin_referer_allowed(self, client):
        resp = client.put(
            "/api/config/slideshow",
            json={"shuffle": False},
            headers={"Referer": "http://localhost/"},
        )
        assert resp.status_code == 200

    def test_no_headers_allowed(self, client):
        # curl / tests / the frontend's urllib signal send neither header.
        assert client.post("/api/config/reload").status_code == 200

    def test_get_never_checked(self, client):
        resp = client.get("/api/config", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 200

    def test_non_api_post_not_checked(self, client):
        resp = client.post("/captive", headers={"Origin": "http://evil.example"})
        # 405 (route is GET-only) proves the CSRF gate did not answer 403.
        assert resp.status_code == 405
