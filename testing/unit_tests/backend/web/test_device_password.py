# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Device-password endpoint — mocked sudo, exact command assertions, partial failure."""

from __future__ import annotations

import json
from unittest import mock

import pytest


def _make_result(returncode: int = 0, stderr: str = "") -> mock.Mock:
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = ""
    r.stderr = stderr
    return r


class TestDevicePassword:
    def _login(self, client, mock_state):
        from metixel.shared.security import hash_secret

        mock_state.update_config("web", {"password": hash_secret("secret123")})
        client.post("/api/auth/login", json={"password": "secret123"})

    def test_changes_both_stores(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        calls = []
        monkeypatch.setattr(
            sec_mod,
            "_run_privileged",
            lambda cmd, input=None: calls.append((cmd, input)) or _make_result(),
        )
        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)

        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"
        assert data["console"] == "ok"
        assert data["samba"] == "ok"

        # Both privileged commands must run, in order, with the right args +
        # stdin.  _run_privileged receives the BARE command (it wraps it in
        # ``sudo systemd-run --pipe`` internally), so we assert those here.
        assert len(calls) == 2
        assert calls[0][0] == ["chpasswd"]
        assert calls[0][1] == "pi:newpass123\n"
        assert calls[1][0] == ["smbpasswd", "-a", "-s", "pi"]
        assert calls[1][1] == "newpass123\nnewpass123\n"

    def test_partial_failure_reported(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        results = iter([_make_result(0), _make_result(1, stderr="smb error")])
        monkeypatch.setattr(sec_mod, "_run_privileged", lambda cmd, input=None: next(results))
        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)

        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert data["status"] == "partial"
        assert data["console"] == "ok"
        assert data["samba"] == "failed"

    def test_console_failure(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        monkeypatch.setattr(
            sec_mod,
            "_run_privileged",
            lambda cmd, input=None: _make_result(1, stderr="chpasswd error"),
        )
        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)

        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert data["status"] == "error"

    def test_mismatch_rejected(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "different"},
        )
        assert resp.status_code == 400

    def test_short_password_rejected(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "short", "confirm_password": "short"},
        )
        assert resp.status_code == 400

    def test_non_pi_rejected(self, client, mock_state, monkeypatch):
        self._login(client, mock_state)
        import metixel.backend.web.routes.security as sec_mod

        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: False)
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        assert resp.status_code == 400

    def test_requires_auth(self, client, mock_state):
        # Set a password but do NOT log in — the auth gate should block.
        from metixel.shared.security import hash_secret

        mock_state.update_config("web", {"password": hash_secret("secret123")})
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "newpass123", "confirm_password": "newpass123"},
        )
        assert resp.status_code == 401


class TestDevicePasswordInputHardening:
    """The password is piped raw to chpasswd/smbpasswd stdin, so control
    characters (newline, CR, NUL, ...) must never reach them."""

    def _login(self, client, mock_state):
        from metixel.shared.security import hash_secret

        mock_state.update_config("web", {"password": hash_secret("secret123")})
        client.post("/api/auth/login", json={"password": "secret123"})

    def _arm(self, monkeypatch):
        import metixel.backend.web.routes.security as sec_mod

        calls = []
        monkeypatch.setattr(
            sec_mod,
            "_run_privileged",
            lambda cmd, input=None: calls.append((cmd, input)) or _make_result(),
        )
        monkeypatch.setattr(sec_mod, "is_raspberry_pi", lambda: True)
        return calls

    @pytest.mark.parametrize(
        "pw",
        [
            "newpass1\nroot:pwned",
            "newpass1\r\n",
            "newpass1\x00x",
            "newpass\tone",
            "newpass1\x7f",
            "newpass1\x1b[0m",
        ],
    )
    def test_control_chars_rejected(self, client, mock_state, monkeypatch, pw):
        self._login(client, mock_state)
        calls = self._arm(monkeypatch)
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": pw, "confirm_password": pw},
        )
        assert resp.status_code == 400
        assert "control" in json.loads(resp.data)["error"].lower()
        assert calls == []

    def test_colon_allowed(self, client, mock_state, monkeypatch):
        """chpasswd splits on the FIRST colon, so a colon in the password is
        fine (the user name comes first)."""
        self._login(client, mock_state)
        calls = self._arm(monkeypatch)
        resp = client.post(
            "/api/system/device-password",
            json={"new_password": "pass:word1", "confirm_password": "pass:word1"},
        )
        assert resp.status_code == 200
        assert calls[0][1] == "pi:pass:word1\n"

    @pytest.mark.parametrize(
        "body",
        [
            {"new_password": 12345678, "confirm_password": 12345678},
            {"new_password": ["a"] * 8, "confirm_password": ["a"] * 8},
            {"new_password": "newpass123", "confirm_password": None},
        ],
    )
    def test_non_string_rejected(self, client, mock_state, monkeypatch, body):
        self._login(client, mock_state)
        calls = self._arm(monkeypatch)
        resp = client.post("/api/system/device-password", json=body)
        assert resp.status_code == 400
        assert calls == []
