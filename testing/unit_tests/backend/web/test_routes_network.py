# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Network API endpoints — with ``network_manager`` functions mocked."""

from __future__ import annotations

import json
import time
from unittest import mock


def _wait_for_call(callable_mock, timeout: float = 3.0) -> None:
    """Busy-wait until a background thread has invoked the mock."""
    deadline = time.time() + timeout
    while time.time() < deadline and callable_mock.call_count == 0:
        time.sleep(0.01)


def _wait_for_len(items: list, minimum: int, timeout: float = 3.0) -> None:
    """Busy-wait until a background thread has appended to *items*."""
    deadline = time.time() + timeout
    while time.time() < deadline and len(items) < minimum:
        time.sleep(0.01)


class TestNetworkStatus:
    def test_status(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(
            net_mod,
            "get_connection_status",
            lambda: {"ip": "10.0.0.5", "interface_type": "wifi"},
        )
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: False)
        monkeypatch.setattr(net_mod, "is_connected", lambda: True)

        resp = client.get("/api/network/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ip"] == "10.0.0.5"
        assert data["ap_mode_active"] is False


class TestNetworkScan:
    def test_scan(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "scan_networks", lambda: [{"ssid": "Net1"}])
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: False)

        resp = client.get("/api/network/scan")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["networks"] == [{"ssid": "Net1"}]
        assert data["cached"] is False


class TestNetworkConnect:
    def test_requires_ssid(self, client):
        resp = client.post("/api/network/connect", json={})
        assert resp.status_code == 400

    def test_connect(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        fake = mock.MagicMock(return_value=(False, "could not connect"))
        monkeypatch.setattr(net_mod, "connect_to_network", fake)

        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "secret"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        _wait_for_call(fake)
        fake.assert_called_once_with("MyWiFi", "secret")


class TestNetworkForget:
    def test_requires_ssid(self, client):
        resp = client.post("/api/network/forget", json={})
        assert resp.status_code == 400

    def test_forget(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "forget_network", lambda ssid: True)
        resp = client.post("/api/network/forget", json={"ssid": "MyWiFi"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"


class TestApStatus:
    def test_ap_active(self, client, monkeypatch):
        import metixel.backend.web.routes.network as net_mod

        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: True)
        monkeypatch.setattr(net_mod, "is_connected", lambda: False)

        resp = client.get("/api/network/ap-status")
        assert resp.status_code == 200
        assert json.loads(resp.data) == {"active": True}


class TestNetworkRadio:
    """POST /api/network/radio — the user-facing OS radio toggle.

    This is the only way to change the radio: the backend never re-asserts it
    on boot or on an OTA, so a user's "off" is respected.
    """

    def test_requires_enabled_field(self, client):
        resp = client.post("/api/network/radio", json={})
        assert resp.status_code == 400

    def test_rejects_non_boolean_enabled(self, client):
        """`bool("false")` is True — a string would silently invert the
        request, so non-boolean values must be rejected outright."""
        resp = client.post("/api/network/radio", json={"enabled": "false"})
        assert resp.status_code == 400

    def test_enable_calls_set_wifi_radio(self, client, monkeypatch):
        import metixel.backend.network_manager as nm

        calls = []
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nm, "set_wifi_radio", lambda en: (calls.append(en), True)[1])

        resp = client.post("/api/network/radio", json={"enabled": True})
        assert resp.status_code == 200
        assert json.loads(resp.data)["status"] == "ok"
        assert calls == [True]

    def test_enable_reports_failure(self, client, monkeypatch):
        import metixel.backend.network_manager as nm

        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nm, "set_wifi_radio", lambda en: False)

        resp = client.post("/api/network/radio", json={"enabled": True})
        assert resp.status_code == 500

    def test_400_when_no_wifi_hardware(self, client, monkeypatch):
        import metixel.backend.network_manager as nm

        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: False)

        resp = client.post("/api/network/radio", json={"enabled": True})
        assert resp.status_code == 400

    def test_disable_blocks_when_ap_active(self, client, monkeypatch):
        """Turning the radio off under an active AP would strand the user
        mid-setup - the exact situation the AP exists to resolve."""
        import metixel.backend.network_manager as nm
        import metixel.backend.web.routes.network as net_mod

        called = []
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nm, "set_wifi_radio", lambda en: called.append(en) or True)
        # Patched on net_mod: network.py binds this name at import time.
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: True)
        monkeypatch.setattr(net_mod, "is_connected", lambda: False)

        resp = client.post("/api/network/radio", json={"enabled": False})
        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert data["reason"] == "ap_active"
        assert called == [], "radio must not be touched while the AP is active"

    def test_disable_allowed_when_connected(self, client, monkeypatch):
        """Disabling while connected is allowed (not blocked) — the operator
        may be switching to Ethernet.  The response is flushed first."""
        import metixel.backend.network_manager as nm
        import metixel.backend.web.routes.network as net_mod

        called = []
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nm, "set_wifi_radio", lambda en: called.append(en) or True)
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: True)
        # Connected → the AP-active block is bypassed.
        monkeypatch.setattr(net_mod, "is_connected", lambda: True)

        resp = client.post("/api/network/radio", json={"enabled": False})
        assert resp.status_code == 200
        _wait_for_len(called, 1)
        assert called == [False]

    def test_disable_allowed_when_ap_inactive(self, client, monkeypatch):
        import metixel.backend.network_manager as nm
        import metixel.backend.web.routes.network as net_mod

        called = []
        monkeypatch.setattr(nm, "is_wifi_hardware_present", lambda: True)
        monkeypatch.setattr(nm, "set_wifi_radio", lambda en: called.append(en) or True)
        monkeypatch.setattr(net_mod, "is_ap_mode_active", lambda: False)

        resp = client.post("/api/network/radio", json={"enabled": False})
        assert resp.status_code == 200
        _wait_for_len(called, 1)
        assert called == [False]


class _FakeController:
    """Minimal NetworkController stand-in exposing what the routes use."""

    def __init__(self, pin: str = "1234"):
        self.pin = pin
        self.begun = 0
        self.ended = 0

    def validate_pin(self, candidate: str):
        if candidate == self.pin:
            return True, "ok"
        return False, "Incorrect PIN. 2 attempt(s) remaining."

    def begin_connection(self) -> None:
        self.begun += 1

    def end_connection(self) -> None:
        self.ended += 1

    def on_wifi_connected(self) -> None:
        pass


class _FakeDaemon:
    def __init__(self, controller):
        self._network_controller = controller


class TestPinSessionGate:
    """POST /network/connect is refused until the session has passed
    /network/validate-pin — while the controller has an active PIN."""

    def _wire(self, app, monkeypatch, pin: str = "1234") -> _FakeController:
        import metixel.backend.web.routes.network as net_mod

        ctl = _FakeController(pin)
        app.config["METIXEL_DAEMON"] = _FakeDaemon(ctl)
        monkeypatch.setattr(net_mod, "connect_to_network", lambda s, p: (False, "nope"))
        return ctl

    def test_connect_refused_without_pin_validation(self, app, monkeypatch):
        ctl = self._wire(app, monkeypatch)
        client = app.test_client()
        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "x"})
        assert resp.status_code == 403
        assert ctl.begun == 0

    def test_wrong_pin_does_not_unlock(self, app, monkeypatch):
        self._wire(app, monkeypatch)
        client = app.test_client()
        resp = client.post("/api/network/validate-pin", json={"pin": "9999"})
        assert resp.status_code == 403
        assert json.loads(resp.data)["valid"] is False
        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "x"})
        assert resp.status_code == 403

    def test_validated_session_can_connect_once(self, app, monkeypatch):
        ctl = self._wire(app, monkeypatch)
        client = app.test_client()
        resp = client.post("/api/network/validate-pin", json={"pin": "1234"})
        assert resp.status_code == 200
        assert json.loads(resp.data)["valid"] is True
        with client.session_transaction() as sess:
            assert sess.get("portal_pin_ok") is True

        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "x"})
        assert resp.status_code == 200
        assert ctl.begun == 1
        # The grant is single-use: cleared after the connect kickoff.
        with client.session_transaction() as sess:
            assert "portal_pin_ok" not in sess
        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "x"})
        assert resp.status_code == 403

    def test_no_pin_active_means_no_gate(self, app, monkeypatch):
        ctl = self._wire(app, monkeypatch, pin="")
        client = app.test_client()
        resp = client.post("/api/network/connect", json={"ssid": "MyWiFi", "password": "x"})
        assert resp.status_code == 200
        assert ctl.begun == 1

    def test_other_session_cannot_reuse_grant(self, app, monkeypatch):
        self._wire(app, monkeypatch)
        a = app.test_client()
        b = app.test_client()
        assert a.post("/api/network/validate-pin", json={"pin": "1234"}).status_code == 200
        assert b.post("/api/network/connect", json={"ssid": "W", "password": ""}).status_code == 403


class TestBodyTypes:
    def test_connect_rejects_non_string_ssid(self, client):
        resp = client.post("/api/network/connect", json={"ssid": 123})
        assert resp.status_code == 400

    def test_connect_rejects_non_string_password(self, client):
        resp = client.post("/api/network/connect", json={"ssid": "x", "password": {"a": 1}})
        assert resp.status_code == 400

    def test_forget_rejects_non_string_ssid(self, client):
        resp = client.post("/api/network/forget", json={"ssid": ["x"]})
        assert resp.status_code == 400

    def test_validate_pin_rejects_non_string_pin(self, client):
        resp = client.post("/api/network/validate-pin", json={"pin": 1234})
        assert resp.status_code == 400
